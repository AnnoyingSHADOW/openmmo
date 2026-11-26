"""Local voice-clone + TTS web app powered by VoxCPM2.

Run:  ./run.sh on macOS/Linux, run.bat on Windows (or: python server.py inside .venv)
Then open http://127.0.0.1:7860
"""

from __future__ import annotations

import os
import sys

# Must be set before torch is imported: lets unsupported MPS ops fall back to CPU.
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")


def _ram_gb() -> float:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30
    except (AttributeError, ValueError, OSError):  # Windows has no sysconf
        return 0.0


# VoxCPM runs float32 on Apple GPUs by default (~13 GB). On Macs with 24 GB of RAM or
# less that pushes the system into swap and generation slows down 5-10x, so use
# bfloat16 there (~6 GB; tested to give the same output quality on torch 2.15).
# Override with VOXCPM_MPS_DTYPE=float32 (or bfloat16) in the environment.
# Only Apple GPUs (MPS) read this; on Windows/Linux VoxCPM uses CUDA or the CPU.
if sys.platform == "darwin" and "VOXCPM_MPS_DTYPE" not in os.environ and 0 < _ram_gb() <= 24.5:
    os.environ["VOXCPM_MPS_DTYPE"] = "bfloat16"

import json
import logging
import random
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import numpy as np
import soundfile as sf
import torch
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
VOICES_DIR = DATA / "voices"
OUTPUTS_DIR = DATA / "outputs"
HISTORY_FILE = DATA / "history.json"
STATIC_DIR = ROOT / "static"

MODEL_ID = os.environ.get("VOXCPM_MODEL", "openbmb/VoxCPM2")
DEVICE = os.environ.get("VOXCPM_DEVICE", "auto")
MAX_REF_SECONDS = 40  # single upload used for transcription
MAX_CLIP_SECONDS = 60
MAX_VOICE_SECONDS = 120  # all clips of one voice together
CLIP_GAP_SECONDS = 0.4
CHUNK_CHARS = int(os.environ.get("CHUNK_CHARS", "260"))
KHMER_ASR_MODEL = os.environ.get("KHMER_ASR_MODEL", "seanghay/whisper-small-khmer-v2")
GAP_SECONDS = 0.22

ID_RE = re.compile(r"^[a-f0-9]{12}$")
CLIP_RE = re.compile(r"^[a-f0-9]{12}\.wav$")

def accel_device() -> str:
    """Best torch device for the helper models: an NVIDIA GPU, the Apple GPU, else the CPU."""
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("voiceclone")

for d in (VOICES_DIR, OUTPUTS_DIR):
    d.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
# Model management
# --------------------------------------------------------------------------- #
class Engine:
    """Holds the VoxCPM model plus lazily-loaded helpers (ASR, denoiser)."""

    def __init__(self) -> None:
        self.model = None
        self.status = "loading"
        self.error: Optional[str] = None
        self.device: Optional[str] = None
        self.dtype: Optional[str] = None
        self.sample_rate = 48000
        self.gpu_lock = threading.Lock()
        self._asr = None
        self._asr_lock = threading.Lock()
        self._km_asr = None
        self._denoiser = None
        self._denoiser_lock = threading.Lock()

    def load(self) -> None:
        try:
            from voxcpm import VoxCPM

            t0 = time.time()
            log.info("Loading %s on device=%s ...", MODEL_ID, DEVICE)
            self.model = VoxCPM.from_pretrained(
                MODEL_ID,
                load_denoiser=False,
                optimize=False,  # torch.compile is CUDA-only in VoxCPM
                device=None if DEVICE == "auto" else DEVICE,
            )
            tm = self.model.tts_model
            self.device = str(getattr(tm, "device", "?"))
            self.sample_rate = int(tm.sample_rate)
            self.dtype = str(getattr(tm.config, "dtype", "?"))
            # Short warm-up so the first real request is not slow.
            with self.gpu_lock:
                self.model.generate(text="Hello, 1 2 3.", max_len=20, normalize=True, retry_badcase=False)
            self.status = "ready"
            log.info("Model ready on %s/%s in %.1fs (sr=%d)", self.device, self.dtype, time.time() - t0, self.sample_rate)
        except Exception as e:  # noqa: BLE001
            log.exception("Model failed to load")
            self.status = "error"
            self.error = f"{type(e).__name__}: {e}"

    # ---- ASR: SenseVoice (CPU) for en/zh/ja/ko/yue, Whisper-Khmer (GPU) for km #
    def transcribe(self, wav_path: str, lang: str = "auto") -> str:
        if lang == "km":
            return self._transcribe_khmer(wav_path)
        with self._asr_lock:
            if self._asr is None:
                from funasr import AutoModel

                log.info("Loading SenseVoiceSmall ASR ...")
                # Hugging Face mirror: ModelScope downloads are very slow outside China.
                self._asr = AutoModel(
                    model="FunAudioLLM/SenseVoiceSmall", hub="hf", disable_update=True, device="cpu"
                )
            res = self._asr.generate(input=wav_path, language="auto", use_itn=True)
        text = res[0]["text"] if res else ""
        return re.sub(r"<\|[^|]*\|>", "", text).strip()

    def _transcribe_khmer(self, wav_path: str) -> str:
        import librosa

        with self._asr_lock:
            if self._km_asr is None:
                from transformers import WhisperForConditionalGeneration, WhisperProcessor

                log.info("Loading %s ...", KHMER_ASR_MODEL)
                dev = accel_device()
                self._km_asr = (
                    WhisperProcessor.from_pretrained(KHMER_ASR_MODEL),
                    WhisperForConditionalGeneration.from_pretrained(KHMER_ASR_MODEL).eval().to(dev),
                    dev,
                )
            proc, model, dev = self._km_asr
            sr = 16000
            audio, _ = librosa.load(wav_path, sr=sr, mono=True)
            # Khmer needs many tokens per second of speech, so decode ~9 s windows
            # (Whisper's 448-token limit truncates longer ones), cutting at pauses.
            spans = librosa.effects.split(audio, top_db=35)
            windows, start, end = [], None, None
            for s, e in spans:
                if start is None:
                    start = s
                elif e - start > 9 * sr:
                    windows.append((start, end))
                    start = s
                end = e
            if start is not None:
                windows.append((start, end))
            parts = []
            for s, e in windows:
                for o in range(s, e, 12 * sr):  # a single pause-free stretch > 12 s: hard cut
                    feats = proc(audio[o : min(e, o + 12 * sr)], sampling_rate=sr, return_tensors="pt").input_features
                    with torch.inference_mode(), self.gpu_lock:
                        ids = model.generate(feats.to(dev), max_new_tokens=440)
                    parts.append(proc.batch_decode(ids, skip_special_tokens=True)[0].strip())
        # The model puts spaces between words; Khmer writing normally does not.
        text = " ".join(p for p in parts if p)
        return re.sub(r"(?<=[ក-៿]) (?=[ក-៿])", "", text)

    # ---- Denoiser (ZipEnhancer) ------------------------------------------- #
    def denoise(self, in_path: str, out_path: str) -> None:
        with self._denoiser_lock:
            if self._denoiser is None:
                from voxcpm.zipenhancer import ZipEnhancer

                log.info("Loading ZipEnhancer denoiser ...")
                self._denoiser = ZipEnhancer("iic/speech_zipenhancer_ans_multiloss_16k_base")
                # Upstream reads/writes the result with torchaudio, which needs FFmpeg's shared
                # libraries (torchcodec); those are usually missing on Windows. Use soundfile.
                self._denoiser._normalize_loudness = _normalize_loudness
                # ModelScope pins it to CPU, where it is ~40x slower than on a GPU.
                dev = accel_device()
                if dev != "cpu":
                    pipe = self._denoiser._pipeline
                    pipe.model.to(dev)
                    pipe.device = torch.device(dev)
            with self.gpu_lock:
                self._denoiser.enhance(in_path, output_path=out_path)


def _normalize_loudness(wav_path: str) -> None:
    """Bring a denoised clip to -20 LUFS (same as VoxCPM's ZipEnhancer, without torchaudio I/O)."""
    import torchaudio.functional as taf

    audio, sr = sf.read(wav_path, dtype="float32", always_2d=True)
    t = torch.from_numpy(np.ascontiguousarray(audio.T))
    t = taf.gain(t, -20 - float(taf.loudness(t, sr)))
    sf.write(wav_path, t.numpy().T, sr, subtype="FLOAT")


engine = Engine()
worker = ThreadPoolExecutor(max_workers=1)  # one generation at a time on the GPU


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def new_id() -> str:
    return uuid.uuid4().hex[:12]


def check_id(value: str) -> str:
    if not ID_RE.match(value or ""):
        raise HTTPException(400, "bad id")
    return value


def ffmpeg_to_wav(src: str, dst: str, max_seconds: Optional[float] = MAX_REF_SECONDS) -> None:
    """Convert any audio container to mono 44.1 kHz wav, trimming edge silence."""
    trim = (
        "silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.1,"
        "areverse,silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.15,areverse"
    )
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", src, "-ac", "1", "-ar", "44100", "-af", trim]
    if max_seconds:
        cmd += ["-t", str(max_seconds)]
    cmd += [dst]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not os.path.exists(dst):
        raise HTTPException(400, f"Could not decode audio: {proc.stderr.strip()[:300]}")


def audio_duration(path: Path) -> float:
    try:
        info = sf.info(str(path))
        return round(info.frames / info.samplerate, 2)
    except Exception:  # noqa: BLE001
        return 0.0


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:  # noqa: BLE001
        return default


def write_json(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    for attempt in range(20):
        try:
            tmp.replace(path)
            return
        except PermissionError:  # Windows: can't replace a file another thread is reading right now
            if attempt == 19:
                raise
            time.sleep(0.05)


def voice_meta(voice_id: str) -> dict:
    meta = read_json(VOICES_DIR / voice_id / "meta.json", None)
    if meta is None:
        raise HTTPException(404, "voice not found")
    return meta


_history_lock = threading.Lock()
_voice_lock = threading.Lock()


def load_history() -> list:
    return read_json(HISTORY_FILE, [])


def add_history(entry: dict) -> None:
    with _history_lock:
        hist = load_history()
        hist.insert(0, entry)
        write_json(HISTORY_FILE, hist[:500])


# Sentence enders: Latin, CJK, Khmer (។ ៕), Myanmar (။), Devanagari (। ॥), Arabic (؟ ۔).
_SENT_END = ".!?。！？；;…។៕။।॥؟۔"
_CLAUSE_END = ",，、:：៖၊،؛"
_SENT_RE = re.compile(rf"[^{_SENT_END}\n]+(?:[{_SENT_END}]+|\n+|$)\s*")
_CLAUSE_RE = re.compile(rf"[^{_CLAUSE_END}]+(?:[{_CLAUSE_END}]+|$)\s*")

# The bundled text normalizer (wetext) only understands English and Chinese: it would
# turn "25" inside Khmer/Thai/Japanese/... text into "twenty five". Skip it for those.
_NON_EN_ZH_SCRIPT = re.compile(
    "[Ͱ-ϿЀ-ӿ֐-׿؀-ۿऀ-෿"  # Greek Cyrillic Hebrew Arabic Indic
    "฀-໿က-႟ក-៿᧠-᧿"  # Thai Lao Myanmar Khmer
    "ᄀ-ᇿ぀-ヿ가-힯]"  # Hangul Kana
)


def can_normalize(text: str) -> bool:
    return not _NON_EN_ZH_SCRIPT.search(text)


def _safe_cut(s: str, cut: int) -> int:
    """Move a hard cut left so it never splits a grapheme cluster (Khmer, Thai, ...)."""
    while cut > 1 and (unicodedata.category(s[cut]).startswith("M") or s[cut - 1] == "្"):
        cut -= 1
    return cut


def split_text(text: str, max_chars: int = CHUNK_CHARS) -> list[str]:
    """Split long text into sentence-aligned chunks no longer than max_chars."""
    text = text.strip()
    if len(text) <= max_chars:
        return [text]

    pieces: list[str] = []
    for sent in _SENT_RE.findall(text):
        if len(sent) <= max_chars:
            pieces.append(sent)
            continue
        for clause in _CLAUSE_RE.findall(sent):
            while len(clause) > max_chars:  # no punctuation at all: hard split on a space
                cut = clause.rfind(" ", 0, max_chars)
                cut = cut if cut > max_chars // 2 else _safe_cut(clause, max_chars)
                pieces.append(clause[:cut])
                clause = clause[cut:]
            if clause:
                pieces.append(clause)

    chunks: list[str] = []
    cur = ""
    for p in pieces:
        if cur and len(cur) + len(p) > max_chars:
            chunks.append(cur.strip())
            cur = ""
        cur += p
    if cur.strip():
        chunks.append(cur.strip())
    return [c for c in chunks if c]


def clean_style(style: str) -> str:
    return re.sub(r"[()（）]", "", style or "").strip()


# --------------------------------------------------------------------------- #
# Generation jobs
# --------------------------------------------------------------------------- #
class TTSRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=20000)
    voice_id: Optional[str] = None  # None => voice design from the style description
    style: str = Field("", max_length=300)
    use_transcript: bool = True
    cfg_value: float = Field(2.0, ge=0.5, le=5.0)
    inference_timesteps: int = Field(10, ge=4, le=50)
    normalize: bool = True
    seed: Optional[int] = None


JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def resolve_voice(voice_id: Optional[str], use_transcript: bool = True) -> dict:
    """Paths/text VoxCPM needs to speak in a saved voice (all None for voice design)."""
    spec = {"voice": None, "ref_path": None, "prompt_path": None, "prompt_text": None}
    if not voice_id:
        return spec
    voice = voice_meta(voice_id)
    vdir = VOICES_DIR / voice_id
    spec["voice"] = voice
    spec["ref_path"] = str(vdir / "ref.wav")  # every clip: voice identity
    transcript = (voice.get("transcript") or "").strip()
    prompt_file = vdir / "prompt.wav"  # only clips that have a transcript
    if use_transcript and transcript:
        spec["prompt_path"] = str(prompt_file) if prompt_file.exists() else spec["ref_path"]
        spec["prompt_text"] = transcript
    return spec


def tts_chunk(
    text: str,
    spec: dict,
    *,
    style: str = "",
    cfg_value: float = 2.0,
    inference_timesteps: int = 10,
    normalize: bool = True,
    seed: int = 0,
) -> np.ndarray:
    """Synthesize one chunk of text (already split to a sensible length)."""
    if engine.status != "ready":
        raise RuntimeError("Model is not ready yet")
    torch.manual_seed(seed)
    np.random.seed(seed % (2**32))
    random.seed(seed)
    with engine.gpu_lock:
        wav = engine.model.generate(
            text=f"({style}){text}" if style else text,
            prompt_wav_path=spec["prompt_path"],
            prompt_text=spec["prompt_text"],
            reference_wav_path=spec["ref_path"],
            cfg_value=cfg_value,
            inference_timesteps=inference_timesteps,
            normalize=normalize and can_normalize(text),
            denoise=False,
        )
    return np.asarray(wav, dtype=np.float32).reshape(-1)


def run_job(job_id: str, req: TTSRequest) -> None:
    job = JOBS[job_id]
    job["status"] = "running"
    job["started"] = time.time()
    tmp_ref = None
    try:
        if engine.status != "ready":
            raise RuntimeError("Model is not ready yet")

        spec = resolve_voice(req.voice_id, req.use_transcript)
        voice, prompt_text = spec["voice"], spec["prompt_text"]

        style = clean_style(req.style)
        if not req.voice_id and not style:
            raise RuntimeError("Pick a voice, or describe one in the Style field to design a new voice.")

        chunks = split_text(req.text)
        job["total"] = len(chunks)
        seed = req.seed if req.seed is not None else random.randint(0, 2**31 - 1)
        job["seed"] = seed

        sr = engine.sample_rate
        gap = np.zeros(int(sr * GAP_SECONDS), dtype=np.float32)
        parts: list[np.ndarray] = []
        for i, chunk in enumerate(chunks):
            if job.get("cancel"):
                raise RuntimeError("Cancelled")
            job["done"] = i
            wav = tts_chunk(
                chunk, spec, style=style, cfg_value=req.cfg_value,
                inference_timesteps=req.inference_timesteps, normalize=req.normalize, seed=seed + i,
            )
            if parts:
                parts.append(gap)
            parts.append(wav)

            # Voice design over several chunks: lock the designed voice in by using
            # the first generated chunk as the reference for the rest.
            if not req.voice_id and i == 0 and len(chunks) > 1:
                fd, tmp_ref = tempfile.mkstemp(suffix=".wav")
                os.close(fd)
                sf.write(tmp_ref, wav, sr)
                spec["ref_path"] = tmp_ref

        audio = np.concatenate(parts) if parts else np.zeros(1, dtype=np.float32)
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        if peak > 0.99:
            audio = audio * (0.99 / peak)

        out_id = new_id()
        out_path = OUTPUTS_DIR / f"{out_id}.wav"
        sf.write(str(out_path), audio, sr, subtype="PCM_16")
        elapsed = time.time() - job["started"]
        duration = round(len(audio) / sr, 2)
        entry = {
            "id": out_id,
            "voice_id": req.voice_id,
            "voice_name": voice["name"] if voice else "Designed voice",
            "text": req.text,
            "style": style,
            "seed": seed,
            "cfg_value": req.cfg_value,
            "inference_timesteps": req.inference_timesteps,
            "mode": "max-similarity" if prompt_text else ("reference" if req.voice_id else "design"),
            "duration": duration,
            "gen_seconds": round(elapsed, 1),
            "created": time.time(),
        }
        add_history(entry)
        job.update(status="done", done=len(chunks), result=entry)
        log.info("Job %s done: %.1fs audio in %.1fs (RTF %.2f)", job_id, duration, elapsed, elapsed / max(duration, 0.01))
    except Exception as e:  # noqa: BLE001
        msg = e.detail if isinstance(e, HTTPException) else str(e)
        if msg != "Cancelled":
            log.exception("Job %s failed", job_id)
        job.update(status="cancelled" if msg == "Cancelled" else "error", error=msg)
    finally:
        job["finished"] = time.time()
        if tmp_ref and os.path.exists(tmp_ref):
            os.unlink(tmp_ref)


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
app = FastAPI(title="VoiceClone (VoxCPM2)")


@app.middleware("http")
async def _no_cache_ui(request, call_next):
    # Always revalidate the UI files so edits show up without a hard refresh.
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.on_event("startup")
def _startup() -> None:
    migrate_voices()
    threading.Thread(target=engine.load, daemon=True).start()


@app.get("/api/status")
def status():
    return {
        "status": engine.status,
        "error": engine.error,
        "device": engine.device,
        "dtype": engine.dtype,
        "model": MODEL_ID,
        "sample_rate": engine.sample_rate,
    }


@app.get("/api/voices")
def list_voices():
    voices = []
    for d in VOICES_DIR.iterdir():
        meta = read_json(d / "meta.json", None) if d.is_dir() else None
        if meta:
            voices.append(meta)
    voices.sort(key=lambda v: v.get("created", 0), reverse=True)
    return voices


def _ingest_clips(vdir: Path, uploads: list[UploadFile], transcripts: list[str], denoise: bool) -> tuple[list[dict], list[str]]:
    """Decode, trim, optionally denoise each upload into vdir/clips. Returns (clips, warnings)."""
    clips_dir = vdir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    taken = {p.name for p in clips_dir.iterdir()}
    clips, warnings = [], []
    with tempfile.TemporaryDirectory() as td:
        for i, up in enumerate(uploads):
            label = up.filename or f"clip {i + 1}"
            suffix = Path(up.filename or "audio").suffix or ".bin"
            raw = Path(td) / f"upload{i}{suffix}"
            with raw.open("wb") as f:
                shutil.copyfileobj(up.file, f)
            fname = f"{new_id()}.wav"
            while fname in taken:
                fname = f"{new_id()}.wav"
            out = clips_dir / fname
            ffmpeg_to_wav(str(raw), str(out), max_seconds=MAX_CLIP_SECONDS)
            if denoise:
                cleaned = Path(td) / f"clean{i}.wav"
                engine.denoise(str(out), str(cleaned))
                shutil.move(str(cleaned), out)
            dur = audio_duration(out)
            text = (transcripts[i] if i < len(transcripts) else "").strip()
            if dur < 1.0:
                out.unlink(missing_ok=True)
                warnings.append(f"Skipped “{label}”: less than 1 second of speech.")
                continue
            if dur >= MAX_CLIP_SECONDS - 0.05 and text:
                warnings.append(f"“{label}” was cut to {MAX_CLIP_SECONDS} s, so its transcript was dropped.")
                text = ""
            clips.append({"file": fname, "transcript": text, "duration": dur, "source": label[:80]})
    return clips, warnings


def _load_level(path: Path, sr: int) -> np.ndarray:
    import librosa

    y, _ = librosa.load(str(path), sr=sr, mono=True)
    rms = float(np.sqrt(np.mean(y**2))) if y.size else 0.0
    if rms > 1e-5:  # match loudness across clips (about -20 dBFS RMS), then guard the peak
        y = y * (0.1 / rms)
        peak = float(np.max(np.abs(y)))
        if peak > 0.97:
            y = y * (0.97 / peak)
    return y.astype(np.float32)


def rebuild_voice(vdir: Path, meta: dict) -> dict:
    """Join all clips into ref.wav (voice identity) and the transcribed ones into
    prompt.wav + combined transcript (used by Max similarity)."""
    sr = 44100
    gap = np.zeros(int(sr * CLIP_GAP_SECONDS), dtype=np.float32)

    def join(clips: list[dict]) -> np.ndarray:
        parts: list[np.ndarray] = []
        for c in clips:
            if parts:
                parts.append(gap)
            parts.append(_load_level(vdir / "clips" / c["file"], sr))
        return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)

    clips = meta["clips"]
    ref = join(clips)
    sf.write(str(vdir / "ref.wav"), ref, sr, subtype="PCM_16")
    with_text = [c for c in clips if c.get("transcript", "").strip()]
    prompt_file = vdir / "prompt.wav"
    if with_text:
        sf.write(str(prompt_file), join(with_text), sr, subtype="PCM_16")
    else:
        prompt_file.unlink(missing_ok=True)
    meta["transcript"] = " ".join(c["transcript"].strip() for c in with_text)
    meta["transcribed_clips"] = len(with_text)
    meta["duration"] = round(len(ref) / sr, 2)
    write_json(vdir / "meta.json", meta)
    return meta


def migrate_voices() -> None:
    """Voices saved before multi-clip support: turn their single ref.wav into clip 1."""
    for d in VOICES_DIR.iterdir():
        meta = read_json(d / "meta.json", None) if d.is_dir() else None
        if not meta or "clips" in meta or not (d / "ref.wav").exists():
            continue
        (d / "clips").mkdir(exist_ok=True)
        fname = f"{new_id()}.wav"
        shutil.copy(d / "ref.wav", d / "clips" / fname)
        meta["clips"] = [{
            "file": fname, "transcript": meta.get("transcript", ""),
            "duration": meta.get("duration", 0), "source": "original sample",
        }]
        rebuild_voice(d, meta)
        log.info("Migrated voice %s to multi-clip format", meta.get("id"))


def _parse_transcripts(raw: str) -> list[str]:
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError:
        raise HTTPException(400, "transcripts must be a JSON list of strings")
    if not isinstance(data, list):
        raise HTTPException(400, "transcripts must be a JSON list of strings")
    return [str(x or "")[:3000] for x in data]


def _check_total(clips: list[dict]) -> None:
    total = sum(c["duration"] for c in clips) + CLIP_GAP_SECONDS * max(0, len(clips) - 1)
    if total > MAX_VOICE_SECONDS:
        raise HTTPException(400, f"Clips add up to {total:.0f} s; the limit is {MAX_VOICE_SECONDS} s. Remove a clip or use shorter ones.")


@app.post("/api/voices")
def create_voice(
    name: str = Form(...),
    denoise: bool = Form(False),
    transcripts: str = Form("[]"),
    audio: list[UploadFile] = File(...),
):
    name = name.strip()[:60]
    if not name:
        raise HTTPException(400, "Name is required")
    if not audio:
        raise HTTPException(400, "Add at least one clip")
    voice_id = new_id()
    vdir = VOICES_DIR / voice_id
    vdir.mkdir(parents=True)
    try:
        clips, warnings = _ingest_clips(vdir, audio, _parse_transcripts(transcripts), denoise)
        if not clips:
            raise HTTPException(400, "No usable speech found (each clip needs at least a few seconds of speech).")
        _check_total(clips)
        meta = {"id": voice_id, "name": name, "denoised": bool(denoise), "created": time.time(), "clips": clips}
        meta = rebuild_voice(vdir, meta)
        return {**meta, "warnings": warnings}
    except Exception:
        shutil.rmtree(vdir, ignore_errors=True)
        raise


@app.post("/api/voices/{voice_id}/clips")
def add_clips(
    voice_id: str,
    denoise: bool = Form(False),
    transcripts: str = Form("[]"),
    audio: list[UploadFile] = File(...),
):
    check_id(voice_id)
    vdir = VOICES_DIR / voice_id
    with _voice_lock:
        meta = voice_meta(voice_id)
        new, warnings = _ingest_clips(vdir, audio, _parse_transcripts(transcripts), denoise)
        try:
            if not new:
                raise HTTPException(400, "No usable speech found in the new clips.")
            _check_total(meta["clips"] + new)
        except HTTPException:
            for c in new:
                (vdir / "clips" / c["file"]).unlink(missing_ok=True)
            raise
        meta["clips"] = meta["clips"] + new
        meta["denoised"] = bool(meta.get("denoised")) or bool(denoise)
        meta = rebuild_voice(vdir, meta)
    return {**meta, "warnings": warnings}


@app.delete("/api/voices/{voice_id}/clips/{clip_file}")
def delete_clip(voice_id: str, clip_file: str):
    check_id(voice_id)
    vdir = VOICES_DIR / voice_id
    with _voice_lock:
        meta = voice_meta(voice_id)
        remaining = [c for c in meta["clips"] if c["file"] != clip_file]
        if len(remaining) == len(meta["clips"]):
            raise HTTPException(404, "clip not found")
        if not remaining:
            raise HTTPException(400, "A voice needs at least one clip. Delete the voice instead.")
        (vdir / "clips" / clip_file).unlink(missing_ok=True)
        meta["clips"] = remaining
        return rebuild_voice(vdir, meta)


@app.get("/api/voices/{voice_id}/clips/{clip_file}")
def clip_audio(voice_id: str, clip_file: str):
    check_id(voice_id)
    if not CLIP_RE.match(clip_file):
        raise HTTPException(400, "bad clip id")
    path = VOICES_DIR / voice_id / "clips" / clip_file
    if not path.exists():
        raise HTTPException(404, "not found")
    return FileResponse(path, media_type="audio/wav")


class VoicePatch(BaseModel):
    name: Optional[str] = Field(None, max_length=60)
    clip_transcripts: Optional[dict[str, str]] = None  # clip file -> transcript


@app.patch("/api/voices/{voice_id}")
def update_voice(voice_id: str, patch: VoicePatch):
    check_id(voice_id)
    vdir = VOICES_DIR / voice_id
    with _voice_lock:
        meta = voice_meta(voice_id)
        if patch.name is not None and patch.name.strip():
            meta["name"] = patch.name.strip()[:60]
        if patch.clip_transcripts:
            for c in meta["clips"]:
                if c["file"] in patch.clip_transcripts:
                    c["transcript"] = patch.clip_transcripts[c["file"]].strip()[:3000]
            return rebuild_voice(vdir, meta)
        write_json(vdir / "meta.json", meta)
        return meta


@app.delete("/api/voices/{voice_id}")
def delete_voice(voice_id: str):
    check_id(voice_id)
    shutil.rmtree(VOICES_DIR / voice_id, ignore_errors=True)
    return {"ok": True}


@app.get("/api/voices/{voice_id}/audio")
def voice_audio(voice_id: str):
    check_id(voice_id)
    path = VOICES_DIR / voice_id / "ref.wav"
    if not path.exists():
        raise HTTPException(404, "not found")
    return FileResponse(path, media_type="audio/wav")


@app.post("/api/transcribe")
def transcribe(audio: UploadFile = File(...), lang: str = Form("auto")):
    if lang not in ("auto", "km"):
        raise HTTPException(400, "lang must be auto or km")
    suffix = Path(audio.filename or "audio").suffix or ".bin"
    with tempfile.TemporaryDirectory() as td:
        raw = Path(td) / f"upload{suffix}"
        with raw.open("wb") as f:
            shutil.copyfileobj(audio.file, f)
        wav = Path(td) / "a.wav"
        ffmpeg_to_wav(str(raw), str(wav))
        try:
            text = engine.transcribe(str(wav), lang)
        except Exception as e:  # noqa: BLE001
            log.exception("ASR failed")
            raise HTTPException(500, f"Transcription failed: {e}")
    return {"text": text}


@app.post("/api/tts")
def start_tts(req: TTSRequest):
    if engine.status != "ready":
        raise HTTPException(503, "Model is still loading" if engine.status == "loading" else engine.error)
    if req.voice_id:
        check_id(req.voice_id)
        voice_meta(req.voice_id)
    job_id = new_id()
    with JOBS_LOCK:
        # forget finished jobs older than an hour
        now = time.time()
        for k in [k for k, j in JOBS.items() if j.get("finished") and now - j["finished"] > 3600]:
            JOBS.pop(k, None)
        queued = sum(1 for j in JOBS.values() if j["status"] in ("queued", "running"))
        JOBS[job_id] = {"id": job_id, "status": "queued", "done": 0, "total": 1, "queue_ahead": queued}
    worker.submit(run_job, job_id, req)
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    check_id(job_id)
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    out = {k: v for k, v in job.items() if k != "cancel"}
    if job.get("started"):
        out["elapsed"] = round((job.get("finished") or time.time()) - job["started"], 1)
    return out


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    check_id(job_id)
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    job["cancel"] = True
    return {"ok": True}


@app.get("/api/history")
def history():
    return load_history()


@app.delete("/api/history/{item_id}")
def delete_history(item_id: str):
    check_id(item_id)
    with _history_lock:
        hist = [h for h in load_history() if h["id"] != item_id]
        write_json(HISTORY_FILE, hist)
    for ext in ("wav", "ogg"):
        (OUTPUTS_DIR / f"{item_id}.{ext}").unlink(missing_ok=True)
    return {"ok": True}


@app.get("/api/outputs/{item_id}.wav")
def output_audio(item_id: str, download: bool = False):
    check_id(item_id)
    path = OUTPUTS_DIR / f"{item_id}.wav"
    if not path.exists():
        raise HTTPException(404, "not found")
    return FileResponse(path, media_type="audio/wav", filename=f"voiceclone-{item_id}.wav" if download else None)


_ogg_lock = threading.Lock()


@app.get("/api/outputs/{item_id}.ogg")
def output_voice_note(item_id: str):
    """Ogg/Opus mono 48 kHz: the format Telegram uses for voice messages, so the file
    shows up as a voice note (play button + waveform) instead of a music/file attachment."""
    check_id(item_id)
    wav = OUTPUTS_DIR / f"{item_id}.wav"
    ogg = OUTPUTS_DIR / f"{item_id}.ogg"
    if not wav.exists():
        raise HTTPException(404, "not found")
    with _ogg_lock:
        if not ogg.exists():
            tmp = ogg.with_suffix(".tmp.ogg")
            cmd = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(wav),
                "-vn", "-map_metadata", "-1", "-ac", "1", "-ar", "48000",
                "-c:a", "libopus", "-b:a", "48k", "-vbr", "on", "-application", "voip",
                str(tmp),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if proc.returncode != 0:
                tmp.unlink(missing_ok=True)
                raise HTTPException(500, f"Opus encode failed: {proc.stderr.strip()[:300]}")
            tmp.replace(ogg)
    return FileResponse(ogg, media_type="audio/ogg", filename=f"voice-{item_id}.ogg")


import video  # noqa: E402  (video maker: script -> narration -> images -> captioned MP4)

video.setup(
    data_dir=DATA, engine=engine, worker=worker, jobs=JOBS, jobs_lock=JOBS_LOCK, new_id=new_id, check_id=check_id,
    read_json=read_json, write_json=write_json, resolve_voice=resolve_voice, tts_chunk=tts_chunk,
    split_text=split_text, clean_style=clean_style, log=log,
)
app.include_router(video.router)

app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "7860"))
    log.info("Open http://%s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_level="warning")
