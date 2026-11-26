"""Background music library: tracks from your computer, reusable in every video.

Files are kept as uploaded in data/music/<id>.<ext> (index in data/music/index.json). At the end of a
render the chosen track is looped under the whole video, faded in and out, and automatically turned down
while the narrator speaks (see mix_music).
"""

from __future__ import annotations

import json
import re
import secrets
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

router = APIRouter()
ctx = SimpleNamespace()
MID_RE = re.compile(r"^[a-f0-9]{8}$")
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".oga", ".opus", ".flac", ".webm", ".aif", ".aiff",
             ".mp4", ".m4v", ".mov"}  # video files work too: only their sound is used
MAX_UPLOAD_BYTES = 40 * 1024 * 1024
MAX_TRACKS = 60
MIME = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac", ".wav": "audio/wav", ".ogg": "audio/ogg",
        ".oga": "audio/ogg", ".opus": "audio/ogg", ".flac": "audio/flac", ".webm": "audio/webm", ".aif": "audio/aiff",
        ".aiff": "audio/aiff", ".mp4": "audio/mp4", ".m4v": "audio/mp4", ".mov": "video/quicktime"}


def setup(data_dir: Path, log) -> None:
    ctx.dir = data_dir / "music"
    ctx.dir.mkdir(parents=True, exist_ok=True)
    ctx.index = ctx.dir / "index.json"
    ctx.lock = threading.Lock()
    ctx.log = log


def _load() -> list[dict]:
    try:
        return json.loads(ctx.index.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _save(items: list[dict]) -> None:
    tmp = ctx.index.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(ctx.index)


def get(mid: Optional[str]) -> Optional[dict]:
    if not mid or not MID_RE.match(mid):
        return None
    for it in _load():
        if it["id"] == mid and (ctx.dir / it["file"]).exists():
            return {**it, "path": ctx.dir / it["file"]}
    return None


def _duration(path: Path) -> float:
    """Length in seconds, or 0 if the file has no sound track (e.g. a silent video)."""
    proc = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                           "stream=codec_type:format=duration", "-of", "json", str(path)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        info = json.loads(proc.stdout or "{}")
        return float(info["format"]["duration"]) if info.get("streams") else 0.0
    except (ValueError, KeyError, TypeError):
        return 0.0


def mix_music(video: Path, out: Path, track: Path, volume: float, duration: float) -> None:
    """Loop `track` under the video's own audio, fade it in and out, and duck it whenever the voice is
    speaking (sidechain compression keyed by the narration). The picture is copied, not re-encoded."""
    fade_out = max(0.0, duration - 2.5)
    graph = (f"[0:a]asplit=2[voice][key];"
             f"[1:a]aformat=sample_rates=48000:channel_layouts=stereo,volume={volume:.3f},"
             f"afade=t=in:st=0:d=1.5,afade=t=out:st={fade_out:.2f}:d=2.5[bed];"
             f"[bed][key]sidechaincompress=threshold=0.02:ratio=8:attack=20:release=450[duck];"
             f"[voice][duck]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[a]")
    proc = subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(video), "-stream_loop", "-1", "-i", str(track),
         "-filter_complex", graph, "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
         "-ar", "48000", "-t", f"{duration:.3f}", "-movflags", "+faststart", str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"Adding the background music failed: {proc.stderr.strip()[-300:]}")


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@router.get("/api/music")
def list_music():
    return [{k: it[k] for k in ("id", "name", "seconds")} for it in _load() if (ctx.dir / it["file"]).exists()]


@router.post("/api/music")
def upload_music(file: UploadFile = File(...)):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in AUDIO_EXT:
        raise HTTPException(400, "Use an MP3, M4A, WAV, OGG, FLAC or AIFF file (or an MP4 / MOV video: its sound is used).")
    mid = secrets.token_hex(4)
    path = ctx.dir / f"{mid}{ext}"
    size = 0
    with path.open("wb") as f:
        while chunk := file.file.read(1 << 20):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                f.close()
                path.unlink(missing_ok=True)
                raise HTTPException(413, "That file is larger than 40 MB.")
            f.write(chunk)
    seconds = _duration(path)
    if seconds < 3:
        path.unlink(missing_ok=True)
        raise HTTPException(400, f"Could not read {file.filename} as audio (no sound in it, or shorter than 3 s).")
    name = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", Path(file.filename or "music").name)[:60] or "music"
    with ctx.lock:
        items = _load()
        if len(items) >= MAX_TRACKS:
            path.unlink(missing_ok=True)
            raise HTTPException(400, f"The library holds up to {MAX_TRACKS} tracks. Delete some first.")
        items.append({"id": mid, "name": name, "file": path.name, "seconds": round(seconds, 1), "created": time.time()})
        _save(items)
    ctx.log.info("Added music %s (%.0fs)", name, seconds)
    return list_music()


@router.get("/api/music/{mid}/audio")
def music_file(mid: str):
    m = get(mid)
    if not m:
        raise HTTPException(404, "not found")
    return FileResponse(m["path"], media_type=MIME.get(m["path"].suffix.lower(), "audio/mpeg"))


@router.delete("/api/music/{mid}")
def delete_music(mid: str):
    if not MID_RE.match(mid):
        raise HTTPException(404, "not found")
    with ctx.lock:
        items = _load()
        for it in items:
            if it["id"] == mid:
                (ctx.dir / it["file"]).unlink(missing_ok=True)
        _save([it for it in items if it["id"] != mid])
    return list_music()
