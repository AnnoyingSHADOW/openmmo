"""Video maker: topic -> Claude Code script -> cloned-voice narration -> Pollinations
images -> Ken Burns video with burned-in (Khmer-capable) captions, all rendered locally.

server.py calls setup(...) with its shared objects and includes `router`.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import numpy as np
import soundfile as sf

import assets
import music
import sfx
import slides
from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, Field

router = APIRouter()
router.include_router(assets.router)  # /api/assets: logo & asset library
router.include_router(music.router)  # /api/music: background music library
ctx = SimpleNamespace()

POLLINATIONS_URL = "https://gen.pollinations.ai/image/"
IMAGE_MODELS = [  # first = default. Z-Image Turbo still works with a $0 balance (checked 2026-11-15); FLUX schnell now needs pollen
    ("tongyi-mai/z-image-turbo", "Z-Image Turbo: good all-rounder (works with $0 balance)"),
    ("black-forest-labs/flux.1-schnell", "FLUX.1 schnell: fastest, needs pollen"),
    ("black-forest-labs/flux.2-klein-4b", "FLUX.2 klein: sharper"),
    ("black-forest-labs/flux.1.1-pro", "FLUX 1.1 pro: best detail, costs more"),
]
CLAUDE_MODELS = ["sonnet", "opus", "haiku"]
ASPECTS = {  # output size, Pollinations request size
    "9:16": ((1080, 1920), (768, 1344)),
    "16:9": ((1920, 1080), (1344, 768)),
    "1:1": ((1080, 1080), (1024, 1024)),
}
ROOT = Path(__file__).resolve().parent
IS_WINDOWS = sys.platform == "win32"
# Local images ("On this computer"): mflux on Apple Silicon Macs (MLX), or diffusers on an NVIDIA GPU
# (Windows/Linux). Each runs in its own environment, .venv-img, made by setup-local-images.sh / .bat.
if sys.platform == "darwin" and platform.machine() == "arm64":
    LOCAL_BACKEND = "mlx"
elif sys.platform in ("win32", "linux") and shutil.which("nvidia-smi"):
    LOCAL_BACKEND = "cuda"
else:
    LOCAL_BACKEND = None
LOCAL_SUPPORTED = LOCAL_BACKEND is not None
LOCAL_PY = ROOT / ".venv-img" / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")
LOCAL_WORKER = ROOT / ("imagegen_worker_cuda.py" if LOCAL_BACKEND == "cuda" else "imagegen_worker.py")
MLX_MODELS = {  # 4-bit mflux builds; setup-local-images.sh downloads them into ~/.cache/huggingface
    "flux2-klein-4b": {"label": "FLUX.2 klein 4B: recommended, fast", "repo": "mflux-community/flux2-klein-4b-mflux-q4",
                       "family": "flux2", "base": "flux2-klein-4b", "steps": 4, "size_gb": 4.6},
    "z-image-turbo": {"label": "Z-Image Turbo: more photographic, slow", "repo": "filipstrand/Z-Image-Turbo-mflux-4bit",
                      "family": "zimage", "base": "z-image-turbo", "steps": 9, "size_gb": 5.9},
}
CUDA_MODELS = {  # fp16 diffusers models that fit a 4 GB NVIDIA GPU; "area" = pixel budget of one picture
    "sd-turbo": {"label": "SD-Turbo: fastest, fits 4 GB GPUs", "repo": "stabilityai/sd-turbo",
                 "family": "sd", "base": "", "steps": 2, "size_gb": 2.6, "area": 512 * 512},
    "sdxl-turbo": {"label": "SDXL-Turbo: better pictures, slower", "repo": "stabilityai/sdxl-turbo",
                   "family": "sdxl", "base": "", "steps": 3, "size_gb": 7.0, "area": 640 * 640},
}
LOCAL_MODELS = CUDA_MODELS if LOCAL_BACKEND == "cuda" else MLX_MODELS
DEFAULT_LOCAL_MODEL = next(iter(LOCAL_MODELS))
SETUP_CMD = "setup-local-images.bat" if IS_WINDOWS else "./setup-local-images.sh"
LOCAL_IDLE_S = 180
FPS = 30
LEAD_S, SENT_GAP_S, TAIL_S = 0.25, 0.18, 0.55
PART_GAP_S = 0.35  # pause at a "|" in tutorial narration, while the next part of the slide appears
REVEAL_LEAD_S = 0.15  # the next part appears just before the narrator starts talking about it
FILE_RE = re.compile(r"^(scene_\d{2}\.(?:wav|mp4)|(?:ai|own)_[a-f0-9]{8}\.(?:jpg|png)|video\.mp4|thumb\.jpg)$")
SID_RE = re.compile(r"^[a-f0-9]{8}$")
MAX_UPLOAD_BYTES = 30 * 1024 * 1024
COENG = "្"
KHMER_RE = re.compile("[ក-៿]")
SENT_SPLIT_RE = re.compile(r"[^.!?។៕\n]+(?:[.!?។៕]+|\n+|$)\s*")

SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "scenes": {
            "type": "array",
            "minItems": 2,
            "maxItems": 14,
            "items": {
                "type": "object",
                "properties": {"narration": {"type": "string"}, "image_prompt": {"type": "string"}},
                "required": ["narration", "image_prompt"],
            },
        },
    },
    "required": ["title", "scenes"],
}


def setup(**kw) -> None:
    ctx.__dict__.update(kw)
    ctx.videos_dir = ctx.data_dir / "videos"
    ctx.videos_dir.mkdir(parents=True, exist_ok=True)
    ctx.settings_file = ctx.data_dir / "settings.json"
    ctx.claude_cwd = ctx.data_dir / ".claude-scratch"  # empty dir: no CLAUDE.md / project settings
    ctx.claude_cwd.mkdir(exist_ok=True)
    ctx.project_lock = threading.Lock()
    ctx.fonts_dir = ctx.data_dir / "fonts"  # flat copy of static/fonts for libass (fontsdir isn't recursive)
    prepare_fonts()
    assets.setup(ctx.data_dir, ctx.log)
    music.setup(ctx.data_dir, ctx.log)
    slides.ICON_DIR = ctx.data_dir / "icons"  # diagram icons downloaded from the Iconify API, kept here


BUNDLED_FONTS_DIR = Path(__file__).resolve().parent / "static" / "fonts"


def _font_family(path: Path) -> Optional[str]:
    if shutil.which("fc-scan"):
        try:
            out = subprocess.run(["fc-scan", "--format", "%{family}\n", str(path)], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=10).stdout
            fam = out.splitlines()[0].split(",")[0].strip() if out.strip() else ""
            if fam:
                return fam
        except Exception:  # noqa: BLE001
            pass
    try:  # no fontconfig (Windows): read the family name from the font file itself
        from PIL import ImageFont

        return ImageFont.truetype(str(path), 12).getname()[0].strip() or None
    except Exception:  # noqa: BLE001
        return None


def prepare_fonts() -> None:
    ctx.fonts_dir.mkdir(exist_ok=True)
    for f in ctx.fonts_dir.iterdir():
        if f.is_file() or f.is_symlink():
            try:
                f.unlink()
            except PermissionError:
                pass  # Windows Font Cache may lock font files; skip and overwrite below
    ctx.bundled_fonts = []
    if BUNDLED_FONTS_DIR.is_dir():
        for f in sorted(BUNDLED_FONTS_DIR.rglob("*")):
            if f.suffix.lower() in (".ttf", ".otf", ".ttc"):
                try:
                    shutil.copy2(f, ctx.fonts_dir / f.name)
                except PermissionError:
                    pass  # font already in place and locked by Windows Font Cache
                fam = _font_family(f)
                if fam and fam not in ctx.bundled_fonts:
                    ctx.bundled_fonts.append(fam)
    if ctx.bundled_fonts:
        ctx.log.info("Bundled caption fonts: %s", ", ".join(ctx.bundled_fonts))


def caption_fonts() -> list[str]:
    """Fonts from static/fonts first (Koulen), then installed Khmer fonts."""
    bundled = list(getattr(ctx, "bundled_fonts", []))
    return bundled + [f for f in khmer_fonts() if f not in bundled]


# --------------------------------------------------------------------------- #
# Settings (Pollinations key lives only on this machine)
# --------------------------------------------------------------------------- #
def _settings() -> dict:
    return ctx.read_json(ctx.settings_file, {})


def pollinations_key() -> str:
    return os.environ.get("POLLINATIONS_API_KEY") or _settings().get("pollinations_key", "")


def claude_bin() -> Optional[str]:
    found = shutil.which("claude")  # on Windows this also finds claude.exe / claude.cmd (PATHEXT)
    if found:
        return found
    home = Path.home()
    if IS_WINDOWS:
        appdata = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
        candidates = [home / ".local" / "bin" / "claude.exe", home / ".claude" / "local" / "claude.exe",
                      appdata / "npm" / "claude.cmd"]
    else:
        candidates = [Path("/opt/homebrew/bin/claude"), Path("/usr/local/bin/claude"), home / ".local/bin/claude",
                      home / ".claude/local/claude"]
    for p in candidates:
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


def claude_cmd() -> Optional[list[str]]:
    """How to start the Claude Code CLI. A Windows npm shim (claude.cmd) runs through cmd.exe, which mangles
    arguments that contain quotes or special characters, so start its Node script directly when possible."""
    exe = claude_bin()
    if not exe:
        return None
    if IS_WINDOWS and exe.lower().endswith((".cmd", ".bat")):
        script = Path(exe).parent / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
        node = shutil.which("node")
        if script.is_file() and node:
            return [node, str(script)]
    return [exe]


_fonts_cache: Optional[list[str]] = None


def khmer_fonts() -> list[str]:
    global _fonts_cache
    if _fonts_cache is None:
        fams: set[str] = set()
        try:
            out = subprocess.run(["fc-list", ":lang=km", "family"], capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=20).stdout
            for line in out.splitlines():
                name = line.split(",")[0].strip()
                if name and not name.startswith("."):
                    fams.add(name)
        except Exception:  # noqa: BLE001
            pass
        preferred = ["Kantumruy Pro", "Kantumruy", "Battambang", "Khmer OS Battambang", "Hanuman", "Noto Sans Khmer",
                     "Khmer OS Siemreap", "Moul", "Koulen", "Khmer Sangam MN", "Khmer MN", "Khmer UI"]
        if not fams and IS_WINDOWS:
            fams = _windows_khmer_fonts()
        first = [f for f in preferred if f in fams]
        _fonts_cache = first + sorted(fams - set(first)) or ["Khmer UI" if IS_WINDOWS else "Khmer Sangam MN"]
    return _fonts_cache


# Windows has no fc-list: find Khmer fonts by file name in the system and per-user font folders.
_WIN_KHMER_FILES = {"khmerui.ttf", "khmeruib.ttf", "daunpenh.ttf", "moolbor.ttf"}  # fonts that ship with Windows
_WIN_KHMER_HINTS = ("khmer", "kantumruy", "battambang", "hanuman", "koulen", "moul", "siemreap", "bokor", "dangrek",
                    "angkor", "bayon", "chenla", "content", "fasthand", "freehand", "kdam", "metal", "nokora",
                    "odor", "preahvihear", "suwannaphum", "taprom", "chakra")


def _windows_khmer_fonts() -> set[str]:
    dirs = [Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"]
    if os.environ.get("LOCALAPPDATA"):
        dirs.append(Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "Windows" / "Fonts")
    fams: set[str] = set()
    for d in dirs:
        try:
            files = list(d.iterdir())
        except OSError:
            continue
        for f in files:
            name = f.name.lower()
            if f.suffix.lower() in (".ttf", ".otf") and (name in _WIN_KHMER_FILES
                                                         or any(h in name for h in _WIN_KHMER_HINTS)):
                fam = _font_family(f)
                if fam:
                    fams.add(fam)
    return fams


class SettingsIn(BaseModel):
    pollinations_key: Optional[str] = Field(None, max_length=300)


@router.get("/api/video/config")
def get_config():
    key = pollinations_key()
    return {
        "has_pollinations_key": bool(key),
        "key_hint": f"…{key[-4:]}" if len(key) > 8 else "",
        "key_from_env": bool(os.environ.get("POLLINATIONS_API_KEY")),
        "claude_available": bool(claude_bin()),
        "platform": sys.platform,
        "fonts": caption_fonts(),
        "bundled_fonts": list(getattr(ctx, "bundled_fonts", [])),
        "image_models": [{"id": m, "label": label} for m, label in IMAGE_MODELS],
        "local_images": {
            "supported": LOCAL_SUPPORTED,
            "backend": LOCAL_BACKEND,
            "setup_cmd": SETUP_CMD,
            "installed": LocalImages.installed(),
            "models": [{"id": k, "label": m["label"], "size_gb": m["size_gb"],
                        "downloaded": LocalImages.model_cached(k)} for k, m in LOCAL_MODELS.items()],
            "default": DEFAULT_LOCAL_MODEL,
            "loaded": local_images.loaded if local_images.running() else None,
        },
        "claude_models": CLAUDE_MODELS,
        "aspects": list(ASPECTS),
    }


@router.put("/api/video/config")
def put_config(body: SettingsIn):
    if body.pollinations_key is not None:
        s = _settings()
        key = body.pollinations_key.strip()
        if key and not re.fullmatch(r"[A-Za-z0-9_\-\.]{8,300}", key):
            raise HTTPException(400, "That doesn't look like a Pollinations key (sk_…).")
        if key.startswith("pk_"):
            raise HTTPException(400, "pk_ keys can't generate images. Create a secret key (sk_…) at enter.pollinations.ai.")
        if key:
            s["pollinations_key"] = key
        else:
            s.pop("pollinations_key", None)
        ctx.write_json(ctx.settings_file, s)
        os.chmod(ctx.settings_file, 0o600)
    return get_config()


# --------------------------------------------------------------------------- #
# Script writing with Claude Code (headless)
# --------------------------------------------------------------------------- #
def image_prompt_rules(image_style: str, what: str) -> str:
    """How Claude should write image prompts. Small local models follow simple, literal prompts best."""
    return (
        f"Each image_prompt is in English and describes one still picture that {what}, for an AI image "
        f"generator. Begin with the style ({image_style}), then 1-3 plain sentences (about 40-80 words):\n"
        "- Show ONE clear moment with at most 2-3 people. Pick the single most visual action of the scene. "
        "Don't chain several actions or describe a sequence (avoid 'while', 'then', 'after', 'as').\n"
        "- Put the main subject first, then exactly what they are doing and their pose, then the setting, the "
        "lighting and the framing (close-up, medium shot, wide shot, low angle...).\n"
        "- Image generators don't know local culture or story context: describe things by how they look, not "
        "only by name, and never refer to earlier scenes ('the same man', 'again'). For a story set in Cambodia, "
        "for example: a sugar palm (thnot) is 'a very tall, thin, straight Borassus sugar palm with a round "
        "crown of stiff fan-shaped leaves' (not a coconut palm); a village house is 'a wooden Khmer house on tall "
        "stilts with a steep roof'; people are 'Cambodian' with their age, build, skin tone and clothing (for "
        "example a red-and-white checkered krama scarf, a sarong).\n"
        "- Describe each recurring character with the same words every time (age, build, hair, clothing colors) "
        "so they look consistent.\n"
        "- Only describe what should be visible. Never mention text, letters, captions, signs or logos, and don't "
        "write what should be absent: generators tend to draw whatever is named, even after 'no'."
    )

LANG_RULES = {
    "km": (
        "Write the title and every narration in Khmer: natural, spoken, friendly Khmer. Use short sentences, each "
        "ending with ។. Put a normal space between phrases (every 2-5 words) so captions can wrap. Write numbers as "
        "Khmer words. Avoid English words except names.",
        17.0,  # Khmer characters spoken per second (measured with VoxCPM2)
        "Khmer characters",
    ),
    "en": ("Write the title and every narration in natural spoken English. Use short sentences. Spell out numbers.",
           15.0, "characters"),
}


MAX_VIDEO_SECONDS = 300
MAX_SOURCE_CHARS = 20000


def scene_plan(seconds: int) -> tuple[int, float]:
    """How many scenes for a target length: ~6 s each for short videos, ~8 s for long ones."""
    per = 6.0 if seconds <= 60 else 8.0
    n = max(3, min(45, round(seconds / per)))
    return n, seconds / n


def _run_claude_direct(prompt: str, system: str, schema: dict, model: str, timeout: int) -> dict:
    """Call the Anthropic-compatible API directly (bypasses Claude Code CLI)."""
    import httpx
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1").rstrip("/")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(500, "ANTHROPIC_API_KEY is not set.")
    _model_map = {
        "sonnet": "claude-sonnet-5-5",
        "opus":   "claude-opus-4-6",
        "haiku":  "claude-fable-5",
    }
    model = _model_map.get(model, model)
    schema_instruction = (
        "You MUST respond with valid JSON that matches this schema exactly. "
        "Output ONLY the JSON, no other text.\n\nJSON Schema:\n" + json.dumps(schema, indent=2)
    )
    full_system = system + "\n\n" + schema_instruction
    body = {
        "model": model,
        "max_tokens": 16384,
        "system": full_system,
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    try:
        with httpx.Client(timeout=max(timeout, 180)) as client:
            resp = client.post(f"{base_url}/messages", json=body, headers=headers)
    except httpx.TimeoutException:
        raise HTTPException(504, "Claude took too long. Try again, or pick a shorter length / faster model.")
    if resp.status_code != 200:
        err = resp.text[:400]
        raise HTTPException(500, f"Claude API error ({resp.status_code}): {err}")
    result = resp.json()
    text = ""
    for block in result.get("content", []):
        if block.get("type") == "text":
            text += block["text"]
    # Strip markdown code fences if present
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        raise HTTPException(500, f"Claude returned invalid JSON. Response start: {text[:300]}")
    usage = result.get("usage", {})
    ctx.log.info(
        "Claude direct (%s): %s in / %s out tokens",
        model, usage.get("input_tokens", "?"), usage.get("output_tokens", "?"),
    )
    return data


def _run_gemini(prompt: str, system: str, schema: dict, timeout: int) -> dict:
    """Call the Gemini API directly for script writing (reliable free alternative to Claude)."""
    import httpx
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise HTTPException(500, "GEMINI_API_KEY is not set.")
    model = "gemini-2.0-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    body = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": schema,
            "maxOutputTokens": 8192,
            "temperature": 1.0,
        },
    }
    try:
        with httpx.Client(timeout=max(timeout, 120)) as client:
            resp = client.post(url, json=body)
    except httpx.TimeoutException:
        raise HTTPException(504, "Gemini took too long. Try again or pick a shorter length.")
    if resp.status_code != 200:
        raise HTTPException(500, f"Gemini API error ({resp.status_code}): {resp.text[:400]}")
    result = resp.json()
    try:
        text = result["candidates"][0]["content"]["parts"][0]["text"]
        data = json.loads(text)
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        raise HTTPException(500, f"Gemini returned unexpected output: {str(result)[:300]}") from exc
    usage = result.get("usageMetadata", {})
    ctx.log.info(
        "Gemini (%s): %s in / %s out tokens",
        model, usage.get("promptTokenCount", "?"), usage.get("candidatesTokenCount", "?"),
    )
    return data


def _run_claude(prompt: str, system: str, schema: dict, model: str, timeout: int) -> dict:
    # Prefer Gemini when its key is set (reliable, free, fast)
    if os.environ.get("GEMINI_API_KEY"):
        return _run_gemini(prompt, system, schema, timeout)
    # Use direct API call when a custom base URL is configured (Claude Code CLI doesn't support custom model names)
    if os.environ.get("ANTHROPIC_BASE_URL"):
        return _run_claude_direct(prompt, system, schema, model, timeout)
    exe = claude_cmd()
    if not exe:
        raise HTTPException(500, "Claude Code CLI ('claude') was not found on PATH.")
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDECODE")}
    model = model if model in CLAUDE_MODELS else "sonnet"
    # The system prompt goes in a file and the prompt on stdin: long multi-line text never passes through argv
    # (size limits, and Windows cmd.exe shims cut arguments at line breaks). UTF-8 everywhere for Khmer.
    with tempfile.TemporaryDirectory(prefix="claude-", ignore_cleanup_errors=True) as td:
        sys_file = Path(td) / "system.txt"
        sys_file.write_text(system, encoding="utf-8")
        cmd = [
            *exe, "-p",
            "--system-prompt-file", str(sys_file),
            "--output-format", "json",
            "--json-schema", json.dumps(schema, separators=(",", ":")),
            "--tools", "",
            "--strict-mcp-config",
            "--setting-sources", "project",  # skip user plugins/hooks so they can't change the writing style
            "--no-session-persistence",
            "--model", model,
        ]
        try:
            proc = subprocess.run(cmd, input=prompt, cwd=ctx.claude_cwd, env=env, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired:
            raise HTTPException(504, "Claude took too long. Try again, or pick a shorter length / faster model.")
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        err = (proc.stderr or proc.stdout).strip()[-400:]
        raise HTTPException(500, f"Claude Code failed: {err or 'no output'}. Run `claude` once in a terminal to sign in.")
    if out.get("is_error"):
        raise HTTPException(500, f"Claude Code error: {str(out.get('result'))[:400]}")
    data = out.get("structured_output")
    if not data:
        try:
            data = json.loads(out.get("result") or "")
        except json.JSONDecodeError:
            raise HTTPException(500, "Claude returned the script in an unexpected format. Try again.")
    usage = out.get("usage") or {}
    tokens_in = sum(int(usage.get(k) or 0) for k in
                    ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))  # incl. cached prompt
    ctx.log.info(
        "Claude (%s): %s in / %s out tokens%s", model, tokens_in or "?", usage.get("output_tokens", "?"),
        # Claude Code always reports an API-price estimate; it is only billed when running on an API key.
        f" (API-price estimate ${out.get('total_cost_usd')}; not charged if Claude Code is signed in with claude.ai)"
        if not os.environ.get("ANTHROPIC_API_KEY") else f", billed to API key: ${out.get('total_cost_usd')}",
    )
    return data


def write_script(topic: str, language: str, seconds: int, image_style: str, model: str) -> dict:
    """Claude writes the whole script from a topic or from pasted source material."""
    rules, cps, unit = LANG_RULES.get(language, LANG_RULES["km"])
    n, per = scene_plan(seconds)
    kind = "short narrated videos (TikTok, YouTube Shorts, Reels)" if seconds <= 90 else "narrated videos"
    system = (
        f"You write scripts for {kind}.\n"
        f"{rules}\n"
        "The user gives either a short topic or longer source material (an article, notes, a story). If it is "
        "source material, base the script on its content and facts; don't invent facts that contradict it.\n"
        f"The whole video is about {seconds} seconds. Write exactly {n} scenes. Scene 1 is a hook that makes people "
        "keep watching; the last scene is a short, memorable closing line. Content must be accurate and useful"
        + ("; organize longer videos into clear parts that flow naturally.\n" if seconds > 90 else ".\n")
        + f"Each narration is 1-3 short sentences that take about {per:.0f} seconds to say "
        f"(roughly {int(per * cps)} {unit}).\n"
        "No emojis, hashtags, stage directions, scene numbers or speaker labels in narration.\n"
        + image_prompt_rules(image_style, "matches its narration")
    )
    schema = json.loads(json.dumps(SCRIPT_SCHEMA))
    schema["properties"]["scenes"]["minItems"] = max(2, n - 3)
    schema["properties"]["scenes"]["maxItems"] = n + 5
    data = _run_claude(f"Topic or source material:\n\n{topic}", system, schema, model,
                       timeout=240 + seconds * 2)
    scenes = [
        {"id": new_sid(), "narration": s["narration"].strip(), "image_prompt": s["image_prompt"].strip(),
         "seed": random.randint(1, 2**31 - 1)}
        for s in data.get("scenes", []) if s.get("narration", "").strip()
    ]
    if not scenes:
        raise HTTPException(500, "Claude returned an empty script. Try again.")
    return {"title": (data.get("title") or topic[:80]).strip(), "scenes": scenes}


ZWSP_RE = re.compile("[\u200b\u200c\u2060\ufeff]")  # zero-width spaces (Khmer word separators)


def clean_spoken(text: str) -> str:
    """Text for the voice model: no zero-width characters, single spaces."""
    return " ".join(ZWSP_RE.sub("", text).split())


def story_units(text: str, cps: float) -> list[tuple[str, bool]]:
    """Split the user's text into numbered units (sentences; very long sentences are cut at a space)
    without changing any characters. Returns (unit, starts_new_paragraph)."""
    target = cps * 8
    units: list[tuple[str, bool]] = []
    for para in re.split(r"\n\s*\n", text.strip()):
        first = True
        for sent in SENT_SPLIT_RE.findall(para):
            sent = " ".join(sent.split())
            if not sent:
                continue
            pieces = [sent]
            while len(clean_spoken(pieces[-1])) > target * 1.6:  # long sentence: cut at a space near the middle
                last = pieces.pop()
                mid = len(last) // 2
                cut = min((i for i, ch in enumerate(last) if ch == " "), key=lambda i: abs(i - mid), default=-1)
                if cut <= 0:
                    pieces.append(last)
                    break
                pieces += [last[:cut], last[cut + 1:]]
            for pc in pieces:
                units.append((pc, first))
                first = False
    return units


def plan_own_script(text: str, language: str, image_style: str, model: str) -> dict:
    """Keep the user's exact words as narration. Claude groups the numbered sentences into scenes
    along the story's beats (it returns sentence numbers, never text) and writes image prompts."""
    _, cps, _ = LANG_RULES.get(language, LANG_RULES["km"])
    units = story_units(text, cps)
    if not units:
        raise HTTPException(400, "Paste some text first.")
    spoken = sum(len(clean_spoken(u)) for u, _ in units)
    est = spoken / cps + len(units) * 0.25
    if est > MAX_VIDEO_SECONDS + 30:
        raise HTTPException(400, f"That text would make about {est / 60:.1f} minutes of narration; the limit is "
                                 f"{MAX_VIDEO_SECONDS // 60} minutes. Shorten it or split it into two videos.")
    n_units = len(units)
    target_scenes = max(1, min(45, round(est / 8)))
    system = (
        "You turn a story or script into scenes for a narrated video with one picture per scene.\n"
        "You get the text split into numbered sentences (¶ marks the start of a paragraph). Do NOT rewrite "
        "anything: you only decide where scenes start.\n"
        f"Group consecutive sentences into about {target_scenes} scenes (each about 6-12 seconds when read aloud, "
        f"roughly {int(cps * 6)}-{int(cps * 12)} characters). Start a new scene where the picture should change: "
        "a new event, place, character or action. Every sentence belongs to exactly one scene, in order: "
        f"scene 1 starts at sentence 1, each scene ends right before the next scene starts, and the last scene "
        f"ends at sentence {n_units}.\n"
        "Also return a short catchy title in the same language as the text.\n"
        + image_prompt_rules(image_style, "shows what happens in that scene")
    )
    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "scenes": {
                "type": "array", "minItems": max(1, min(n_units, target_scenes - 2)), "maxItems": min(60, n_units),
                "items": {
                    "type": "object",
                    "properties": {
                        "start": {"type": "integer", "minimum": 1, "maximum": n_units},
                        "end": {"type": "integer", "minimum": 1, "maximum": n_units},
                        "image_prompt": {"type": "string"},
                    },
                    "required": ["start", "end", "image_prompt"],
                },
            },
        },
        "required": ["title", "scenes"],
    }
    numbered = "\n".join(f"{'¶ ' if para else ''}[{i + 1}] {clean_spoken(u)}" for i, (u, para) in enumerate(units))
    data = _run_claude(numbered, system, schema, model, timeout=240 + n_units * 4)

    # Rebuild scenes from Claude's start positions only, so coverage is always complete and in order.
    starts: dict[int, str] = {}
    for sc in sorted(data.get("scenes", []), key=lambda x: int(x.get("start", 1))):
        st = min(max(int(sc.get("start", 1)), 1), n_units)
        starts.setdefault(st, str(sc.get("image_prompt", "")).strip())
    starts.setdefault(1, next(iter(starts.values()), image_style))
    bounds = sorted(starts)
    scenes = []
    for k, st in enumerate(bounds):
        en = bounds[k + 1] - 1 if k + 1 < len(bounds) else n_units
        narration = " ".join(u for u, _ in units[st - 1 : en])
        scenes.append({"id": new_sid(), "narration": narration, "image_prompt": starts[st] or image_style,
                       "seed": random.randint(1, 2**31 - 1)})
    joined = "".join("".join(ZWSP_RE.sub("", x["narration"]).split()) for x in scenes)
    assert joined == "".join(ZWSP_RE.sub("", text).split()), "scene split changed the text"
    return {"title": (data.get("title") or clean_spoken(units[0][0])[:60]).strip(), "scenes": scenes}


# --------------------------------------------------------------------------- #
# Tutorials: Claude writes a lesson as slides (code, bullets, output) + narration
# --------------------------------------------------------------------------- #
TUTORIAL_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "logos": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "scenes": {
            "type": "array",
            "minItems": 3,
            "maxItems": 40,
            "items": {
                "type": "object",
                "properties": {
                    "layout": {"type": "string", "enum": ["cover", "slide"]},
                    "slide": {"type": "string"},
                    "narration": {"type": "string"},
                    "image_prompt": {"type": "string"},
                },
                "required": ["layout", "slide", "narration", "image_prompt"],
            },
        },
    },
    "required": ["title", "logos", "scenes"],
}
CODE_LIMITS ={"9:16": (34, 12), "16:9": (60, 10), "1:1": (44, 10)}  # characters per line, lines per block
LANG_NAMES = {"km": "Khmer", "en": "English"}
# Icons Claude may use in diagram boxes (all checked to exist on the Iconify API).
_TABLER = ("user users device-laptop device-desktop device-mobile browser world-www world server server-2 database "
           "cloud cloud-upload cloud-download api code file-code file-text folder cpu router wifi network lock "
           "lock-open key shield-lock mail send inbox message bell shopping-cart building-store truck-delivery "
           "chef-hat tools-kitchen-2 terminal-2 keyboard mouse printer photo camera video microphone headphones "
           "settings bolt repeat refresh arrows-exchange math-function variable binary brackets braces list "
           "list-check table search filter sort-ascending chart-bar git-branch git-commit git-merge package box "
           "stack-2 layout palette link id coin credit-card building-bank receipt home school book bulb target "
           "robot brain cookie puzzle sitemap hierarchy schema calendar clock check x alert-triangle bug "
           "question-mark rocket device-floppy trash edit eye click hand-click map-pin car battery").split()
_LOGOS = ("python javascript typescript-icon html-5 css-3 php java c c-plusplus go rust kotlin-icon swift dart "
          "flutter react nodejs-icon npm-icon bash-icon json markdown git-icon github-icon visual-studio-code "
          "chrome google-icon mysql-icon postgresql sqlite mongodb-icon redis firebase docker-icon nginx aws "
          "cloudflare-icon vercel-icon apple android-icon linux-tux microsoft-windows-icon wordpress-icon figma "
          "openai-icon stackoverflow-icon facebook youtube-icon telegram tiktok-icon whatsapp-icon "
          "instagram-icon spring-icon laravel django-icon flask angular-icon vue svelte-icon tailwindcss-icon "
          "bootstrap jquery c-sharp dotnet ruby rails r-lang scala lua unity arduino raspberry-pi tensorflow "
          "pytorch-icon jupyter numpy pandas-icon google-drive google-gmail microsoft-teams zoom-icon slack-icon "
          "notion-icon claude-icon microsoft-icon gitlab postman-icon graphql mariadb-icon oracle kubernetes "
          "terraform-icon ubuntu godot-icon blender visual-studio intellij-idea pycharm eclipse-icon replit-icon "
          "codepen-icon firefox safari microsoft-edge brave linkedin-icon discord-icon paypal shopify "
          "bitcoin").split()
_OFFICE = ["vscode-icons:file-type-excel", "vscode-icons:file-type-word", "vscode-icons:file-type-powerpoint"]
DIAGRAM_ICONS = ("general things (one-color line icons): " + ", ".join(f"tabler:{n}" for n in _TABLER)
                 + ". Technology and brand logos (in color): " + ", ".join(f"logos:{n}" for n in _LOGOS)
                 + ", " + ", ".join(_OFFICE) + " (Excel, Word, PowerPoint).")


def check_logos(names: list[str]) -> list[str]:
    """Keep the logo names that really exist (downloading them into the icon cache), trying the usual
    spelling variants (logos:x / logos:x-icon / devicon:x) for names Claude guessed. At most 3."""
    out = []
    for raw in names:
        name = str(raw).strip().lower()
        if not slides.ICON_RE.match(name):
            continue
        prefix, base = name.split(":", 1)
        tries = [name]
        if prefix == "logos":
            stem = base[:-5] if base.endswith("-icon") else base
            tries += [f"logos:{stem}", f"logos:{stem}-icon", f"devicon:{stem}", f"skill-icons:{stem}"]
        found = next((t for t in dict.fromkeys(tries) if slides.resolve_icon(t)), None)
        if found and found not in out:
            out.append(found)
        if len(out) == 3:
            break
    return out


def fix_part_marks(slide: str, narration: str) -> str:
    """Claude sometimes puts a +++ straight after the heading although the narration starts by talking about
    the first block, which shifts every later part by one. While the slide has more parts than the narration
    has '|', drop such a +++ (the first block then shows together with the heading)."""
    lines = slide.split("\n")
    for _ in range(3):
        if slides.count_steps("\n".join(lines)) <= narration.count("|"):
            break
        head = next((k for k, ln in enumerate(lines) if re.match(r"^\s*#{1,3}\s+", ln)), None)
        if head is None:
            break
        nxt = next((k for k in range(head + 1, len(lines)) if lines[k].strip()), None)
        if nxt is None or not re.match(r"^\s*\+\+\+\s*$", lines[nxt]):
            break
        del lines[nxt]
    return "\n".join(lines)


def add_cover_logos(slide: str, logos: list[str]) -> str:
    """Put the lesson's logos at the top of a title slide (unless the slide already has a [logo] line)."""
    if not logos or slides.LOGO_RE.search(slide):
        return slide
    return f"[logo] {' '.join(logos)}\n{slide}"


def tutorial_plan(seconds: int) -> tuple[int, float]:
    """Slides need time to read: ~12 s per scene for short lessons, ~14 s for long ones."""
    per = 12.0 if seconds <= 90 else 14.0
    n = max(3, min(30, round(seconds / per)))
    return n, seconds / n


def write_tutorial(topic: str, language: str, seconds: int, image_style: str, model: str, aspect: str) -> dict:
    """Claude writes a beginner lesson: a cover scene, teaching slides, and a recap cover scene."""
    rules, cps, unit = LANG_RULES.get(language, LANG_RULES["km"])
    lang_name = LANG_NAMES.get(language, "the narration language")
    n, per = tutorial_plan(seconds)
    cols, rows = CODE_LIMITS.get(aspect, CODE_LIMITS["9:16"])
    system = (
        "You are a patient, expert teacher who makes short narrated video tutorials that teach basic knowledge to "
        "complete beginners: coding, computers, the internet, math, science, everyday digital skills.\n"
        f"{rules}\n"
        "Exception for tutorials: keep standard technical terms in English, written in Latin letters, when learners "
        "normally say them that way (Python, code, variable, function, loop, Excel, Wi-Fi). Explain each new term "
        "the first time it appears.\n"
        f"The whole video is about {seconds} seconds. Write exactly {n} scenes; each narration takes about "
        f"{per:.0f} seconds to say (roughly {int(per * cps)} {unit}). The user's text is the lesson topic and may "
        "say the level or what to cover; follow it.\n"
        "Each scene shows one slide while the narrator speaks; the slide builds up part by part. Two layouts:\n"
        "- \"cover\": a big title over an AI picture. Use it ONLY for scene 1 (the lesson title plus one short line "
        "saying what the viewer will be able to do) and for the last scene (a recap: a heading, 2-4 short bullet "
        "points and one next step to practise).\n"
        "- \"slide\": a clean teaching slide, for every other scene.\n"
        "Teach well: say why it matters, then one idea per scene, building step by step. Show a tiny concrete "
        "example, then what it does or prints, then a common mistake or tip, and a quick practice idea near the end. "
        "Everything must be correct. For coding topics the code must run as written, and an output block must be "
        "exactly what that code prints.\n"
        "Slide text uses this Markdown subset, one item per line:\n"
        "  # Heading            (required, short)\n"
        "  A short sentence.    (optional)\n"
        "  - bullet             (2-4 bullets, a few words each)\n"
        "  ```python            (a fenced code block with its language name; any language)\n"
        "  ```output            (a fenced block with what the code prints)\n"
        "  `inline code` and **bold** are allowed.\n"
        "  ```diagram           (an animated \"how it works\" diagram, see below)\n"
        "  +++                  (on its own line: everything after it appears later, see below)\n"
        f"Code blocks: at most {rows} lines and at most {cols} characters per line (the video is {aspect}), so "
        "keep examples tiny. A slide may have a heading plus a code block plus an output block, a heading plus "
        "bullets, or a heading plus a diagram. Keep slide text short: it supports the narration and doesn't repeat "
        "it word for word.\n"
        f"Headings, sentences, bullets and diagram labels are in {lang_name}. Code, identifiers and program output "
        f"stay as they really are; short code comments may be in {lang_name}.\n"
        "Build every teaching slide up part by part, in time with the narration:\n"
        "- A line containing only +++ splits the slide into parts. The part before the first +++ (usually just the "
        "heading) is on screen when the scene starts; each later part appears when the narrator reaches it. Code "
        "types itself line by line when its part appears. Put +++ between bullets, before a code block, before its "
        "output block, or inside a code block between groups of lines that you explain one at a time. Use 1-4 +++ "
        "per teaching slide. Cover slides may use them too (for example one recap bullet at a time).\n"
        "- In the narration, put | between sentences exactly where the next part should appear. Count the parts: "
        "every +++ line (also \"+++ focus N\") is one, and every quiz block is one more (its answer). The narration "
        "has exactly that many |. What is said after the k-th | is about the k-th new part. If the narration starts "
        "by talking about the code (\"look at this code\"), don't put +++ between the heading and that code block. "
        "Worked example: a slide with a heading, a 2-line code block, then \"+++ focus 2\", then \"+++\", then an "
        "output block has 2 parts, so its narration has 2 |: \"Look at this code, it greets you. | Line two prints "
        "the name. | And here is the result.\"\n"
        "- When a scene explains how something works (data moving between parts, a request and its response, input "
        "-> processing -> output, the stages of a process), draw it as a diagram instead of bullets. The boxes "
        "appear, then each arrow draws itself while a glowing dot travels along it and the receiving box lights up. "
        "Example:\n"
        "  ```diagram\n"
        "  [browser] tabler:browser Browser\n"
        "  [server] tabler:server Web server | [db] tabler:database Database\n"
        "  +++\n"
        "  browser -> server: request\n"
        "  +++\n"
        "  server -> db: SQL query\n"
        "  +++\n"
        "  db --> server: data\n"
        "  server --> browser: web page\n"
        "  ```\n"
        "  Box lines: [id] icon Label, where id is a short English word and the label is 1-3 words. The icon is a "
        "clean open-source icon; pick the closest one from these lists and write it exactly (or leave the icon out):\n"
        f"  {DIAGRAM_ICONS}\n"
        "  Each box line is one row (top to bottom); put 2-3 boxes side by side in one row with |. At most 6 boxes. "
        "Arrow lines: "
        "a -> b: label (1-4 words); --> is a dashed arrow for a reply or result, <-> goes both ways. Put +++ before "
        "each arrow (or a pair of arrows that happen together) so they play one at a time with the narration.\n"
        "  Use a diagram in at least one scene of every lesson about how something works (websites, the internet, "
        "APIs, databases, Git, how a computer runs a program, how a loop or function call flows). Skip diagrams "
        "when the lesson is only about syntax.\n"
        "- Spotlight: when the narration explains one particular line of a code block, put a line \"+++ focus N\" "
        "(or \"+++ focus N-M\") after the code block, N being the line number inside that block (the slide shows "
        "line numbers). That line lights up, the other lines dim and the view zooms in a little, until the next "
        "part. It counts as a part, so it needs its own | in the narration, placed where you start talking about "
        "that line (\"look at line 2\"). Use it on most code slides, for the 1-3 lines that matter most.\n"
        "- Quiz: add 1 quiz scene to lessons under 90 seconds and 2 to longer ones, after the idea it tests (never "
        "the first or last scene). It is a \"slide\" scene: a short heading (for example \"Quiz\" in the narration "
        "language), usually a tiny code block, then a quiz block:\n"
        "  ```quiz\n"
        "  What does this code print?\n"
        "  - [ ] 10\n"
        "  - [x] 55\n"
        "  - [ ] Error\n"
        "  ```\n"
        "  The question line is optional, then 3-4 short options, exactly one marked [x]. Make the wrong options "
        "the mistakes beginners really make. The options appear in their part; the answer is revealed in the next "
        "part, which counts like a +++ and needs a |. The narration asks the question, tells the viewer to pause "
        "and think, then a | (the video adds a 5-second countdown there by itself, don't count out loud), then "
        "says the right answer and briefly why. Example: \"... What does it print? Pause the video and choose. | "
        "The answer is B, 55, because ...\".\n"
        "- In the last scene (the recap), invite viewers to write their quiz answers or score in the comments.\n"
        "Narration: speak like a friendly teacher looking at the slide with the viewer. Explain the slide in words "
        "and point at it (\"look at the second line\", \"here is the result\", \"now the server answers\"). Never read "
        "code symbol by symbol (no \"open bracket\", no \"colon\"). No emojis, hashtags, stage directions, scene "
        "numbers or speaker labels in narration.\n"
        "image_prompt: for \"cover\" scenes, an English prompt for an AI picture that shows the lesson topic with "
        f"objects, people or places (not a screen full of text, no text in the picture), in this style: "
        f"{image_style}. Begin with the style, then 1-2 plain sentences. For \"slide\" scenes, image_prompt is \"\".\n"
        "logos: the real logos of the technologies this lesson is about, shown big on the title and recap scenes. "
        "For example a Java lesson: [\"logos:java\"]; HTML and CSS: [\"logos:html-5\", \"logos:css-3\"]; Git: "
        "[\"logos:git-icon\"]; Excel: [\"vscode-icons:file-type-excel\"]. Use 1-3 names from the logo list above "
        "(other SVG Logos names on Iconify are fine too). If the lesson isn't about a specific technology or product, "
        "give one fitting tabler: icon from the list instead (for example an algorithm lesson: [\"tabler:list-check\"]).\n"
        "Also return a short catchy lesson title in the narration language."
    )
    schema = json.loads(json.dumps(TUTORIAL_SCHEMA))
    schema["properties"]["scenes"]["minItems"] = max(3, n - 3)
    schema["properties"]["scenes"]["maxItems"] = n + 5
    data = _run_claude(f"Lesson topic:\n\n{topic}", system, schema, model, timeout=300 + seconds * 3)
    raw = [sc for sc in data.get("scenes", []) if str(sc.get("narration", "")).strip()]
    if len(raw) < 2:
        raise HTTPException(502, "Claude returned too few scenes. Try again.")
    logos = check_logos(data.get("logos") or [])
    scenes = []
    for k, sc in enumerate(raw):
        layout = "cover" if k in (0, len(raw) - 1) else "slide"
        prompt = str(sc.get("image_prompt", "")).strip() if layout == "cover" else ""
        if layout == "cover" and not prompt:
            prompt = f"{image_style}, an inviting picture about {topic[:120]}"
        slide = fix_part_marks(str(sc.get("slide", "")).strip(), str(sc["narration"]))
        if layout == "cover":
            slide = add_cover_logos(slide, logos)
        scenes.append({"id": new_sid(), "narration": str(sc["narration"]).strip(), "image_prompt": prompt,
                       "seed": random.randint(1, 2**31 - 1), "layout": layout, "slide": slide})
    return {"title": str(data.get("title") or topic[:60]).strip(), "scenes": scenes}


# --------------------------------------------------------------------------- #
# Projects
# --------------------------------------------------------------------------- #
POS_PATTERN = "^(tl|tc|tr|c|bl|bc|br)$"
SIZE_PATTERN = "^(xs|s|m|l|xl)$"


class Overlay(BaseModel):  # an asset (logo, sticker, product photo) placed on top of one scene
    asset: str = Field(..., pattern="^[a-f0-9]{8}$")
    pos: str = Field("c", pattern=POS_PATTERN)
    size: str = Field("m", pattern=SIZE_PATTERN)


class Scene(BaseModel):
    id: Optional[str] = None  # stable scene id (images are stored per id)
    narration: str = Field(..., max_length=2000)
    image_prompt: str = Field("", max_length=1500)
    seed: Optional[int] = None
    layout: Optional[str] = Field(None, pattern="^(slide|cover)$")  # tutorial scenes; None = picture scene
    slide: str = Field("", max_length=4000)  # slide text (Markdown subset, see slides.py)
    overlays: list[Overlay] = Field(default_factory=list, max_length=4)


IMAGE_FIELDS = ("image_key", "ai_image", "custom_image", "image_v")


def new_sid() -> str:
    return ctx.new_id()[:8]


class VideoSettings(BaseModel):
    voice_id: Optional[str] = None
    voice_style: str = Field("", max_length=200)
    use_transcript: bool = True
    aspect: str = "9:16"
    image_source: str = "pollinations"  # pollinations | local (on this Mac) | gradient
    local_model: str = DEFAULT_LOCAL_MODEL
    image_model: str = IMAGE_MODELS[0][0]
    image_style: str = Field("cinematic photo, soft natural light", max_length=200)
    caption_font: str = Field("", max_length=100)
    caption_style: str = "outline"  # outline | yellow | box
    caption_highlight: str = "word"  # word (current word) | karaoke (spoken so far) | none
    highlight_color: str = Field("#FFD21F", max_length=9)
    show_title: bool = True
    captions: bool = True
    logo_asset: Optional[str] = Field(None, pattern="^[a-f0-9]{8}$")  # shown on every scene
    logo_pos: str = Field("tr", pattern=POS_PATTERN)
    logo_size: str = Field("s", pattern=SIZE_PATTERN)
    logo_opacity: float = Field(0.9, ge=0.1, le=1.0)
    sfx: bool = True  # sound effects on animated tutorial slides
    music: Optional[str] = Field(None, pattern="^[a-f0-9]{8}$")  # background music track (music library id)
    music_volume: float = Field(0.15, ge=0.02, le=0.5)


class ScriptRequest(BaseModel):
    topic: str = Field(..., min_length=2, max_length=20000)
    language: str = "km"
    seconds: int = Field(30, ge=10, le=300)
    mode: str = "write"  # write | keep (use the text as-is) | tutorial (lesson with code slides)
    claude_model: str = "sonnet"
    settings: VideoSettings = VideoSettings()


class ProjectUpdate(BaseModel):
    title: Optional[str] = Field(None, max_length=200)
    scenes: Optional[list[Scene]] = None
    settings: Optional[VideoSettings] = None


def _pdir(pid: str) -> Path:
    ctx.check_id(pid)
    return ctx.videos_dir / pid


def load_project(pid: str) -> dict:
    pdir = _pdir(pid)
    p = ctx.read_json(pdir / "project.json", None)
    if p is None:
        raise HTTPException(404, "video project not found")
    # older projects: give scenes ids and move their scene_NN images to per-id names
    changed = False
    for i, sc in enumerate(p.get("scenes", [])):
        if not sc.get("id"):
            sc["id"] = new_sid()
            changed = True
            for ext in ("jpg", "png"):
                old = pdir / f"scene_{i + 1:02d}.{ext}"
                if old.exists():
                    old.rename(pdir / f"ai_{sc['id']}.{ext}")
                    sc["ai_image"] = f"ai_{sc['id']}.{ext}"
    if changed:
        save_project(p)
    return p


def find_scene(p: dict, sid: str) -> dict:
    if not SID_RE.match(sid or ""):
        raise HTTPException(400, "bad scene id")
    for sc in p["scenes"]:
        if sc.get("id") == sid:
            return sc
    raise HTTPException(404, "scene not found (save the script first)")


def save_project(p: dict) -> dict:
    p["updated"] = time.time()
    ctx.write_json(_pdir(p["id"]) / "project.json", p)
    return p


def summary(p: dict) -> dict:
    r = p.get("render") or {}
    return {
        "id": p["id"], "title": p.get("title"), "topic": p.get("topic"), "language": p.get("language"),
        "scenes": len(p.get("scenes", [])), "status": p.get("status", "draft"),
        "duration": r.get("duration"), "has_video": bool(r.get("video")), "created": p.get("created"),
        "updated": p.get("updated"), "aspect": p.get("settings", {}).get("aspect"),
        "version": r.get("rendered_at"),
    }


@router.get("/api/videos")
def list_projects():
    out = []
    for d in ctx.videos_dir.iterdir():
        p = ctx.read_json(d / "project.json", None) if d.is_dir() else None
        if p:
            out.append(summary(p))
    out.sort(key=lambda x: x.get("updated") or 0, reverse=True)
    return out


@router.post("/api/videos/script")
def create_script(req: ScriptRequest):
    text = req.topic.strip()
    # keep-my-words: the language is whatever the pasted text is written in
    language = ("km" if KHMER_RE.search(text) else "en") if req.mode == "keep" else req.language
    if req.mode == "keep":
        script = plan_own_script(text, language, req.settings.image_style, req.claude_model)
    elif req.mode == "tutorial":
        script = write_tutorial(text, language, req.seconds, req.settings.image_style, req.claude_model,
                                req.settings.aspect)
    else:
        script = write_script(text, language, req.seconds, req.settings.image_style, req.claude_model)
    pid = ctx.new_id()
    _pdir(pid).mkdir(parents=True)
    now = time.time()
    p = {
        "id": pid, "created": now, "topic": text[:300], "mode": req.mode, "language": language,
        "seconds": req.seconds, "claude_model": req.claude_model, "title": script["title"],
        "scenes": script["scenes"], "settings": req.settings.model_dump(), "status": "draft", "render": None,
    }
    return save_project(p)


@router.get("/api/videos/{pid}")
def get_project(pid: str):
    return load_project(pid)


@router.put("/api/videos/{pid}")
def update_project(pid: str, body: ProjectUpdate):
    with ctx.project_lock:
        p = load_project(pid)
        if body.title is not None:
            p["title"] = body.title.strip()
        if body.scenes is not None:
            if not body.scenes:
                raise HTTPException(400, "A video needs at least one scene.")
            old = {sc.get("id"): sc for sc in p["scenes"]}
            scenes = []
            for s_in in body.scenes:
                prev = old.get(s_in.id) if s_in.id and SID_RE.match(s_in.id) else None
                sc = {
                    "id": prev["id"] if prev else new_sid(),
                    "narration": s_in.narration,
                    "image_prompt": s_in.image_prompt,
                    "seed": s_in.seed or (prev or {}).get("seed") or random.randint(1, 2**31 - 1),
                }
                if s_in.layout:
                    sc["layout"] = s_in.layout
                    sc["slide"] = s_in.slide
                if s_in.overlays:
                    sc["overlays"] = [o.model_dump() for o in s_in.overlays]
                for k in IMAGE_FIELDS:  # images belong to the server, never to the client payload
                    if prev and prev.get(k):
                        sc[k] = prev[k]
                scenes.append(sc)
            kept = {sc["id"] for sc in scenes}
            for sid in set(old) - kept:  # removed scenes: drop their images
                if sid and SID_RE.match(sid):
                    for pattern in (f"*_{sid}.*", f"slide_{sid}_*.png", f"motion_{sid}_*"):
                        for f in _pdir(pid).glob(pattern):
                            f.unlink(missing_ok=True)
            p["scenes"] = scenes
        if body.settings is not None:
            p["settings"] = body.settings.model_dump()
        return save_project(p)


@router.delete("/api/videos/{pid}")
def delete_project(pid: str):
    shutil.rmtree(_pdir(pid), ignore_errors=True)
    return {"ok": True}


@router.get("/api/videos/{pid}/files/{name}")
def project_file(pid: str, name: str, download: bool = False):
    if not FILE_RE.match(name):
        raise HTTPException(400, "bad file name")
    path = _pdir(pid) / name
    if not path.exists():
        raise HTTPException(404, "not found")
    media = {"mp4": "video/mp4", "jpg": "image/jpeg", "png": "image/png", "wav": "audio/wav"}[name.rsplit(".", 1)[1]]
    fname = None
    if download:
        p = load_project(pid)
        safe = re.sub(r"[^\wក-៿\- ]+", "", p.get("title") or "video").strip()[:60] or "video"
        fname = f"{safe}.mp4"
    return FileResponse(path, media_type=media, filename=fname)


@router.post("/api/videos/{pid}/render")
def start_render(pid: str):
    p = load_project(pid)
    s = p["settings"]
    if ctx.engine.status != "ready":
        raise HTTPException(503, "The voice model is still loading.")
    if not s.get("voice_id") and not ctx.clean_style(s.get("voice_style", "")):
        raise HTTPException(400, "Pick a narrator voice (or describe one in Voice style).")
    pdir = _pdir(pid)
    if not all(image_ready(pdir, sc, s) for sc in p["scenes"]):
        _check_image_source(s)  # only needed if some images still have to be generated
    job_id = ctx.new_id()
    with ctx.jobs_lock:
        ctx.jobs[job_id] = {"id": job_id, "kind": "video", "status": "queued", "done": 0, "total": 1,
                            "stage": "queued", "message": "Waiting for the voice engine…", "project": pid}
    ctx.worker.submit(render_project, job_id, pid)
    return {"job_id": job_id}


# --------------------------------------------------------------------------- #
# Images
# --------------------------------------------------------------------------- #
class OutOfPollen(Exception):
    """Pollinations answered 402: the account has no pollen left."""


class ImageError(Exception):
    pass


def _image_key(scene: dict, s: dict) -> str:
    model = LOCAL_MODELS[local_model_id(s)]["repo"] if s.get("image_source") == "local" else s.get("image_model")
    raw = json.dumps([scene.get("image_prompt"), scene.get("seed"), s.get("image_source"), model,
                      s.get("image_style"), s.get("aspect")], ensure_ascii=False)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def fetch_pollinations(prompt: str, model: str, size: tuple[int, int], seed: int, dest_base: Path) -> Path:
    key = pollinations_key()
    q = urllib.parse.urlencode({"model": model, "width": size[0], "height": size[1], "seed": seed % 2**31})
    url = POLLINATIONS_URL + urllib.parse.quote(prompt, safe="") + "?" + q
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}", "User-Agent": "voiceclone-studio/1.0"})
    last = ""
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                data = r.read()
            if data[:3] == b"\xff\xd8\xff":
                ext = "jpg"
            elif data[:8] == b"\x89PNG\r\n\x1a\n":
                ext = "png"
            else:
                raise ImageError("Pollinations returned something that is not a JPEG/PNG image.")
            out = dest_base.with_suffix("." + ext)
            out.write_bytes(data)
            return out
        except urllib.error.HTTPError as e:
            body = e.read()[:600].decode("utf-8", "replace")
            try:
                detail = json.loads(body).get("error", {}).get("message") or body
            except Exception:  # noqa: BLE001
                detail = body
            if e.code == 401:
                raise ImageError("Pollinations rejected the key (401). Check the sk_ key in Video settings.")
            if e.code == 402:
                raise OutOfPollen("Your Pollinations balance (pollen) is empty (402). Top up or pick a cheaper model.")
            if e.code in (400, 403, 404, 422):
                raise ImageError(f"Pollinations error {e.code}: {detail[:200]}")
            last = f"HTTP {e.code}: {detail[:200]}"
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = str(e)
        time.sleep(2 * (attempt + 1))
    raise ImageError(f"Pollinations failed after retries ({last}).")


def make_gradient(size: tuple[int, int], seed: int, dest_base: Path) -> Path:
    rng = random.Random(seed)
    palettes = [("0x0f2027", "0x2c5364"), ("0x1d2b64", "0xf8cdda"), ("0x42275a", "0x734b6d"), ("0x134e5e", "0x71b280"),
                ("0x232526", "0x414345"), ("0x355c7d", "0xc06c84"), ("0x0b486b", "0xf56217"), ("0x3a1c71", "0xffaf7b")]
    c0, c1 = rng.choice(palettes)
    out = dest_base.with_suffix(".png")
    w, h = size
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", f"gradients=s={w}x{h}:c0={c0}:c1={c1}:x0=0:y0=0:x1={w}:y1={h}:d=1", "-frames:v", "1", str(out)],
        check=True, capture_output=True,
    )
    return out


def local_model_id(s: dict) -> str:
    m = s.get("local_model")
    return m if m in LOCAL_MODELS else DEFAULT_LOCAL_MODEL


class LocalImages:
    """Image generation on this Mac with mflux (Apple MLX), in a separate process and Python
    environment (.venv-img) so its PyTorch/transformers never clash with the voice model's.

    The worker loads one model, then serves one picture at a time over stdin/stdout.
    It is stopped after LOCAL_IDLE_S seconds without work, right after a render's images,
    or when another local model is picked, so its memory is given back to the voice model."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.proc: Optional[subprocess.Popen] = None
        self.loaded: Optional[str] = None  # LOCAL_MODELS key of the running worker
        self.replies = None
        self.timer: Optional[threading.Timer] = None
        self.last_used = 0.0

    @staticmethod
    def installed() -> bool:
        return LOCAL_SUPPORTED and LOCAL_PY.exists() and LOCAL_WORKER.exists()

    @staticmethod
    def model_cached(model_id: str) -> bool:
        repo = LOCAL_MODELS[model_id]["repo"]
        if Path(repo).is_dir():
            return True
        hub = Path(os.environ.get("HF_HUB_CACHE") or Path(os.environ.get("HF_HOME") or Path.home() / ".cache/huggingface") / "hub")
        snaps = hub / ("models--" + repo.replace("/", "--")) / "snapshots"
        if LOCAL_BACKEND == "cuda":  # diffusers repo: a snapshot with its index, the fp16 UNet, and no unfinished download
            blobs = snaps.parent / "blobs"
            if blobs.is_dir() and any(blobs.glob("*.incomplete")):
                return False
            return any((snap / "model_index.json").exists() and (snap / "unet" / "diffusion_pytorch_model.fp16.safetensors").exists()
                       for snap in snaps.glob("*"))
        for snap in snaps.glob("*"):  # complete = every shard listed in each part's index is present
            try:
                if all((idx := snap / part / "model.safetensors.index.json").exists()
                       and all((snap / part / f).exists() for f in set(json.loads(idx.read_text())["weight_map"].values()))
                       for part in ("transformer", "text_encoder", "vae")):
                    return True
            except (OSError, ValueError, KeyError):
                continue
        return False

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _read(self, timeout: float) -> dict:
        import queue
        try:
            line = self.replies.get(timeout=timeout)
        except queue.Empty:
            self._kill()
            raise ImageError("The local image model stopped responding. See data/imagegen.log.")
        if line is None:
            self._kill()
            raise ImageError("The local image model exited unexpectedly. See data/imagegen.log.")
        return json.loads(line)

    def _start(self, model_id: str) -> None:
        import queue
        if not LOCAL_SUPPORTED:
            raise ImageError("Local images need an Apple Silicon Mac or an NVIDIA GPU. Use Pollinations or Plain gradient.")
        if not self.installed():
            raise ImageError(f"Local images aren't set up. Run {SETUP_CMD} once, then restart the app.")
        m = LOCAL_MODELS[model_id]
        ctx.log.info("Starting local image model %s (%s)…", model_id, m["repo"])
        env = {**os.environ, "LOCAL_IMAGE_MODEL": m["repo"], "LOCAL_IMAGE_FAMILY": m["family"],
               "LOCAL_IMAGE_BASE": m["base"], "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8",
               "HF_HUB_DISABLE_TELEMETRY": "1", "TQDM_DISABLE": "1"}
        env.pop("PYTHONPATH", None)
        with open(ctx.data_dir / "imagegen.log", "ab") as log:
            self.proc = subprocess.Popen([str(LOCAL_PY), "-I", str(LOCAL_WORKER)], stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=log, text=True, encoding="utf-8",
                                         errors="replace", bufsize=1, env=env)
        self.loaded = model_id
        self.replies = queue.Queue()

        def pump(proc=self.proc, q=self.replies):
            for line in proc.stdout:
                if line.strip().startswith("{"):
                    q.put(line)
            q.put(None)

        threading.Thread(target=pump, daemon=True).start()
        msg = self._read(timeout=60 * 60)  # first use may still download the model (5-6 GB)
        if not msg.get("ready"):
            self._kill()
            raise ImageError(f"The local image model failed to load: {msg.get('error', 'unknown error')}")
        ctx.log.info("Local image model ready in %.1fs", msg.get("load_secs", 0))

    def _ensure(self, model_id: str) -> None:
        """Call with the lock held: make sure the worker runs with this model."""
        if self.running() and self.loaded != model_id:
            self._shutdown()  # never two image models in memory at once
        if not self.running():
            self._start(model_id)

    def warm(self, model_id: str) -> None:
        with self.lock:
            self._ensure(model_id)

    def generate(self, prompt: str, size: tuple[int, int], seed: int, dest_base: Path, model_id: str) -> Path:
        out = dest_base.with_suffix(".jpg")
        with self.lock:
            if self.timer:
                self.timer.cancel()
            self._ensure(model_id)
            req = {"prompt": prompt, "seed": int(seed) % 2**31, "width": size[0], "height": size[1],
                   "steps": LOCAL_MODELS[model_id]["steps"], "area": LOCAL_MODELS[model_id].get("area"), "out": str(out)}
            self.proc.stdin.write(json.dumps(req) + "\n")
            self.proc.stdin.flush()
            msg = self._read(timeout=15 * 60)
            self.last_used = time.time()
            self.timer = threading.Timer(LOCAL_IDLE_S, self._stop_if_idle)
            self.timer.daemon = True
            self.timer.start()
        if not msg.get("ok"):
            raise ImageError(f"Local image generation failed: {msg.get('error', 'unknown error')}")
        ctx.log.info("Local image (%s) %dx%d in %.1fs", model_id, size[0], size[1], msg.get("secs", 0))
        return out

    def _stop_if_idle(self) -> None:
        if time.time() - self.last_used >= LOCAL_IDLE_S - 1:
            self.stop()

    def stop(self) -> None:
        """Unload the model (frees its memory). Waits for a picture in progress to finish."""
        with self.lock:
            if self.timer:
                self.timer.cancel()
                self.timer = None
            self._shutdown()

    def _shutdown(self) -> None:
        if self.running():
            try:
                self.proc.stdin.close()  # EOF: the worker exits by itself
                self.proc.wait(timeout=20)
            except Exception:  # noqa: BLE001
                self._kill()
            ctx.log.info("Local image model unloaded")
        self.proc = None
        self.loaded = None

    def _kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                pass
        self.proc = None
        self.loaded = None


local_images = LocalImages()


def needs_picture(scene: dict) -> bool:
    """Tutorial "slide" scenes are drawn from text; every other scene needs a picture."""
    return scene.get("layout") != "slide"


def image_ready(pdir: Path, scene: dict, s: dict) -> bool:
    if not needs_picture(scene):
        return True
    if scene.get("custom_image") and (pdir / scene["custom_image"]).exists():
        return True
    return (scene.get("image_key") == _image_key(scene, s) and bool(scene.get("ai_image"))
            and (pdir / scene["ai_image"]).exists())


def scene_image(pdir: Path, i: int, scene: dict, s: dict, strict: bool = False) -> Optional[Path]:
    """The picture a scene will use: the user's own upload, else a cached or new AI image
    (None for tutorial slide scenes, which have no picture)."""
    if not needs_picture(scene):
        return None
    if scene.get("custom_image") and (pdir / scene["custom_image"]).exists():
        return pdir / scene["custom_image"]
    if image_ready(pdir, scene, s):
        return pdir / scene["ai_image"]
    key = _image_key(scene, s)
    base = pdir / f"ai_{scene['id']}"
    for ext in ("jpg", "png"):
        base.with_suffix("." + ext).unlink(missing_ok=True)
    size = ASPECTS.get(s.get("aspect"), ASPECTS["9:16"])[1]
    # no "no text" suffix: FLUX-style models can't follow negatives and tend to draw what is named
    prompt = f"{scene['image_prompt'].strip().rstrip('.')}. Style: {s.get('image_style') or 'cinematic'}."
    if s.get("image_source") == "gradient":
        path = make_gradient(size, scene.get("seed") or i, base)
    elif s.get("image_source") == "local":
        path = local_images.generate(prompt, size, scene.get("seed") or i, base, local_model_id(s))
    else:
        try:
            path = fetch_pollinations(prompt, s.get("image_model") or IMAGE_MODELS[0][0], size, scene.get("seed") or i, base)
        except OutOfPollen as e:
            if strict:  # image review: tell the user instead of showing gradients
                raise ImageError(str(e))
            # no pollen left: use a plain gradient so the video still renders, and leave
            # image_key unset so a later run (after topping up) makes the real picture
            ctx.log.warning("Pollinations balance empty (402): using a gradient for scene %s", scene.get("id"))
            path = make_gradient(size, scene.get("seed") or i, base)
            scene["ai_image"] = path.name
            scene["image_key"] = None
            scene["image_v"] = int(time.time() * 1000)
            return path
    scene["image_key"] = key
    scene["ai_image"] = path.name
    scene["image_v"] = int(time.time() * 1000)
    return path


def _store_image_fields(pid: str, sc: dict) -> None:
    """Write one scene's image fields back into the saved project (matched by scene id)."""
    with ctx.project_lock:
        fresh = load_project(pid)
        for f in fresh["scenes"]:
            if f.get("id") == sc.get("id"):
                for k in IMAGE_FIELDS:
                    if k in sc:
                        f[k] = sc[k]
                    elif k in f and k != "custom_image":
                        f.pop(k)
                if "seed" in sc:
                    f["seed"] = sc["seed"]
        save_project(fresh)


def _check_image_source(s: dict) -> None:
    src = s.get("image_source", "pollinations")
    if src == "pollinations" and not pollinations_key():
        raise HTTPException(400, "Add your Pollinations key first (or choose Plain gradient images).")
    if src == "local" and not LocalImages.installed():
        raise HTTPException(400, f"Local images aren't set up. Run {SETUP_CMD} once, then restart the app.")


@router.post("/api/videos/{pid}/images")
def start_images(pid: str, regenerate: bool = False):
    """Generate every missing/outdated AI image now, so they can be reviewed before rendering.
    regenerate=true: give every scene a brand-new AI picture (uploaded pictures are kept)."""
    p = load_project(pid)
    _check_image_source(p["settings"])
    job_id = ctx.new_id()
    with ctx.jobs_lock:
        ctx.jobs[job_id] = {"id": job_id, "kind": "images", "status": "queued", "done": 0,
                            "total": len(p["scenes"]), "message": "Starting…", "project": pid}
    threading.Thread(target=_images_job, args=(job_id, pid, regenerate), daemon=True).start()
    return {"job_id": job_id}


def _images_job(job_id: str, pid: str, regenerate: bool = False) -> None:
    job = ctx.jobs[job_id]
    job.update(status="running", started=time.time())
    pdir = _pdir(pid)
    try:
        p = load_project(pid)
        s = p["settings"]
        if regenerate:
            todo = [(i, sc) for i, sc in enumerate(p["scenes"]) if not sc.get("custom_image") and needs_picture(sc)]
            for _, sc in todo:
                sc["seed"] = random.randint(1, 2**31 - 1)  # new seed -> new picture
        else:
            todo = [(i, sc) for i, sc in enumerate(p["scenes"]) if not image_ready(pdir, sc, s)]
        job["total"] = len(p["scenes"])
        job["done"] = len(p["scenes"]) - len(todo)
        errors = []
        local = s.get("image_source") == "local"
        if local and todo:
            if local_images.loaded != local_model_id(s) or not local_images.running():
                job["message"] = "Loading the image model…"
            local_images.warm(local_model_id(s))

        def work(item):
            i, sc = item
            if job.get("cancel"):
                return
            if local:
                job["message"] = f"Painting image {job['done'] + 1}/{job['total']} on this computer…"
            try:
                scene_image(pdir, i, sc, s, strict=True)
                _store_image_fields(pid, sc)
            except ImageError as e:
                errors.append(f"Scene {i + 1}: {e}")
            job["done"] += 1
            job["message"] = f"Image {job['done']}/{job['total']}"

        with ThreadPoolExecutor(max_workers=1 if local else 3) as pool:  # the Mac's GPU makes one at a time
            list(pool.map(work, todo))
        if errors:
            raise RuntimeError(errors[0] + (f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""))
        job.update(status="cancelled" if job.get("cancel") else "done", message="Images ready",
                   result=summary(load_project(pid)))
    except Exception as e:  # noqa: BLE001
        job.update(status="error", error=str(e))
    finally:
        job["finished"] = time.time()


@router.post("/api/videos/{pid}/scenes/{sid}/new-image")
def new_scene_image(pid: str, sid: str):
    """Re-roll one scene's AI image right now (new seed, same prompt). Clears an uploaded image."""
    p = load_project(pid)
    s = p["settings"]
    _check_image_source(s)
    sc = find_scene(p, sid)
    if not needs_picture(sc):
        raise HTTPException(400, "This scene is a slide; it has no picture.")
    i = p["scenes"].index(sc)
    sc["seed"] = random.randint(1, 2**31 - 1)
    if sc.get("custom_image"):
        (_pdir(pid) / sc.pop("custom_image")).unlink(missing_ok=True)
        _store_image_fields(pid, {**sc, "custom_image": None})
    try:
        scene_image(_pdir(pid), i, sc, s, strict=True)
    except ImageError as e:
        raise HTTPException(502, str(e))
    _store_image_fields(pid, sc)
    return find_scene(load_project(pid), sid)


@router.post("/api/videos/{pid}/scenes/{sid}/image")
def upload_scene_image(pid: str, sid: str, image: UploadFile = File(...)):
    """Use your own picture for a scene (any common format; converted to JPEG)."""
    p = load_project(pid)
    sc = find_scene(p, sid)
    pdir = _pdir(pid)
    out = pdir / f"own_{sid}.jpg"
    with tempfile.TemporaryDirectory() as td:
        raw = Path(td) / ("upload" + (Path(image.filename or "").suffix.lower() or ".img"))
        size = 0
        with raw.open("wb") as f:
            while chunk := image.file.read(1 << 20):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "Image is larger than 30 MB.")
                f.write(chunk)
        tmp = Path(td) / "out.jpg"
        # cap at 2x the 1080p frame; keep aspect (the render crops to fill the frame)
        proc = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(raw), "-frames:v", "1",
                               "-vf", "scale='min(2160,iw)':-2:flags=lanczos,format=yuvj420p", "-q:v", "2", str(tmp)],
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        if proc.returncode != 0 or not tmp.exists():  # e.g. HEIC from iPhone: Pillow (pillow-heif) or macOS sips
            im = assets.open_image_file(raw, Path(td))
            if im is not None:
                from PIL import ImageOps

                im = ImageOps.exif_transpose(im).convert("RGB")
                if im.width > 2160:
                    im = im.resize((2160, max(2, round(im.height * 2160 / im.width))))
                im.save(tmp, "JPEG", quality=92)
        if not tmp.exists() or tmp.stat().st_size == 0:
            raise HTTPException(400, "Could not read that image. Use JPG, PNG, WEBP or HEIC.")
        shutil.move(str(tmp), out)
    sc["custom_image"] = out.name
    sc["image_v"] = int(time.time() * 1000)
    _store_image_fields(pid, sc)
    return find_scene(load_project(pid), sid)


def _scene_picture(pdir: Path, sc: dict) -> Optional[Path]:
    """The picture behind a cover slide right now (upload, else the AI picture), if there is one."""
    for f in (sc.get("custom_image"), sc.get("ai_image")):
        if f and (pdir / f).exists():
            return pdir / f
    return None


def _slide_page(p: dict, i: int, sc: dict, s: dict, background: Optional[Path],
                **extra) -> tuple[str, tuple[int, int], tuple]:
    """The slide's HTML page, its pixel size, and the inputs that decide how it looks (for caching)."""
    aspect = s.get("aspect") if s.get("aspect") in ASPECTS else "9:16"
    size = ASPECTS[aspect][0]
    fonts = sorted(f for f in ctx.fonts_dir.glob("*") if f.suffix.lower() in (".ttf", ".otf"))
    heading_font = s.get("caption_font") or (ctx.bundled_fonts[0] if getattr(ctx, "bundled_fonts", None) else "Koulen")
    layout = sc.get("layout") or "slide"
    bg_id = (background.name, background.stat().st_mtime_ns) if background and background.exists() else None
    key_parts = (layout, sc.get("slide", ""), aspect, heading_font, s.get("highlight_color"),
                 bool(s.get("captions", True)), p.get("title", ""), i, len(p["scenes"]), p.get("language"), bg_id,
                 slides.icon_signature(sc.get("slide", "")))
    page = slides.build_html(
        sc.get("slide", ""), layout=layout, size=size, aspect=aspect, accent=s.get("highlight_color") or "#FFD21F",
        heading_font=heading_font, font_files=fonts, lesson=p.get("title", ""), step=f"{i + 1} / {len(p['scenes'])}",
        km=p.get("language") == "km", captions=bool(s.get("captions", True)), background=background, **extra)
    return page, size, key_parts


def slide_frame(pdir: Path, p: dict, i: int, sc: dict, s: dict, background: Optional[Path]) -> Path:
    """Draw (or reuse) the full-size slide image for a tutorial scene (the finished slide, every part shown)."""
    page, size, key_parts = _slide_page(p, i, sc, s, background)
    out = pdir / f"slide_{sc['id']}_{slides.slide_key(*key_parts)}.png"
    if out.exists():
        return out
    for old in pdir.glob(f"slide_{sc['id']}_*.png"):
        old.unlink(missing_ok=True)
    return slides.render_png(page, size, out)


def slide_motion(pdir: Path, p: dict, i: int, sc: dict, s: dict, background: Optional[Path], times: list[float],
                 dur: float, cancel=None, progress=None) -> Path:
    """Film (or reuse) the animated slide: each part appears at its time, code types itself, diagrams play."""
    page, size, key_parts = _slide_page(p, i, sc, s, background)
    key = slides.slide_key(*key_parts, tuple(round(t, 2) for t in times), round(dur, 2), FPS)
    out = pdir / f"motion_{sc['id']}_{key}.mp4"
    if out.exists():
        return out
    for old in pdir.glob(f"motion_{sc['id']}_*"):
        old.unlink(missing_ok=True)
    return slides.render_motion(page, size, out, times, dur, FPS, cancel=cancel, progress=progress)


def motion_events(motion: Path) -> list[dict]:
    """The sound-effect cues saved next to an animated slide (see slides.render_motion)."""
    try:
        return json.loads(motion.with_suffix(".json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def with_sound_effects(wav_path: Path, events: list[dict]) -> Path:
    """The scene's narration with the slide's sound effects mixed in (a new file next to it)."""
    if not events:
        return wav_path
    audio, sr = sf.read(str(wav_path), dtype="float32", always_2d=False)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    mix = audio + sfx.render(events, sr, len(audio))
    peak = float(np.max(np.abs(mix))) if mix.size else 0
    if peak > 0.99:
        mix *= 0.99 / peak
    out = wav_path.with_name(wav_path.stem + "_fx.wav")
    sf.write(str(out), mix, sr, subtype="PCM_16")
    return out


@router.get("/api/icons/search")
def icon_search(q: str = ""):
    """Logo / icon names matching a word (via the Iconify search API), logos first."""
    q = q.strip()[:40]
    if len(q) < 2:
        return []
    return slides.search_icons(q)


@router.get("/api/icons/{prefix}/{name}.svg")
def icon_file(prefix: str, name: str):
    if not slides.ICON_RE.match(f"{prefix}:{name}"):
        raise HTTPException(404, "not found")
    svg = slides._iconify_svg(prefix, name)
    if not svg:
        raise HTTPException(404, "not found")
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "max-age=86400"})


@router.get("/api/videos/{pid}/scenes/{sid}/play")
def scene_slide_play(pid: str, sid: str):
    """The slide as a live page that plays its animation in a loop, timed from the narration's length
    (the real video uses the real voice timing)."""
    p = load_project(pid)
    sc = find_scene(p, sid)
    if not sc.get("layout"):
        raise HTTPException(404, "This scene has no slide.")
    pdir = _pdir(pid)
    timeline, dur = estimate_timeline(sc.get("narration", ""), p.get("language") or "km", scene_quiz_steps(sc))
    times = step_times(timeline, slides.count_steps(sc.get("slide", "")), dur)
    page, _, _ = _slide_page(p, p["scenes"].index(sc), sc, p["settings"],
                             _scene_picture(pdir, sc) if sc["layout"] == "cover" else None,
                             embed_fonts=True, autoplay=(times, dur))
    return HTMLResponse(page, headers={"Cache-Control": "no-cache"})


@router.get("/api/videos/{pid}/scenes/{sid}/slide")
def scene_slide(pid: str, sid: str):
    """Preview of a tutorial scene's slide, drawn with the project's current settings."""
    p = load_project(pid)
    sc = find_scene(p, sid)
    if not sc.get("layout"):
        raise HTTPException(404, "This scene has no slide.")
    pdir = _pdir(pid)
    try:
        path = slide_frame(pdir, p, p["scenes"].index(sc), sc, p["settings"],
                           _scene_picture(pdir, sc) if sc["layout"] == "cover" else None)
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        raise HTTPException(500, str(e))
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-cache"})


@router.delete("/api/videos/{pid}/scenes/{sid}/image")
def remove_scene_image(pid: str, sid: str):
    """Stop using the uploaded picture; go back to the AI image."""
    p = load_project(pid)
    sc = find_scene(p, sid)
    if sc.get("custom_image"):
        (_pdir(pid) / sc["custom_image"]).unlink(missing_ok=True)
    with ctx.project_lock:
        fresh = load_project(pid)
        f = find_scene(fresh, sid)
        f.pop("custom_image", None)
        f["image_v"] = int(time.time() * 1000)
        save_project(fresh)
    return find_scene(load_project(pid), sid)


# --------------------------------------------------------------------------- #
# Captions (ASS, rendered by libass + HarfBuzz so Khmer shapes correctly)
# --------------------------------------------------------------------------- #
PUNCT_ONLY_RE = re.compile(r"^[\s\.,!?;:។៕៖ៗ\"'“”‘’()\[\]\-–—…]+$")


def text_units(s: str) -> int:
    """Approximate on-screen width in glyph clusters (Khmer subscripts/vowels add none)."""
    n, prev = 0, ""
    for ch in s:
        if not (unicodedata.category(ch)[0] in "MC" or ch == COENG or prev == COENG):
            n += 1
        prev = ch
    return n


def _hard_split(word: str, limit: int) -> list[str]:
    parts, cur = [], ""
    for ch in word:
        if cur and text_units(cur + ch) > limit and not (unicodedata.category(ch).startswith("M") or cur[-1] == COENG):
            parts.append(cur)
            cur = ""
        cur += ch
    return parts + ([cur] if cur else [])


_kh_tokenize = None


def khmer_words(phrase: str) -> list[str]:
    """Split a Khmer phrase into words (Khmer writes words without spaces) so captions
    wrap and highlight word by word. Zero-width spaces in the text are used as exact word
    boundaries; otherwise khmer-nltk segments it. Falls back to the whole phrase."""
    global _kh_tokenize
    if ZWSP_RE.search(phrase) or "\u2063" in phrase:
        return [w for w in re.split("[\u200b\u200c\u2060\ufeff\u2063]+", phrase) if w]
    if not KHMER_RE.search(phrase):
        return [phrase]
    if _kh_tokenize is None:
        try:
            import logging
            import warnings

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                from khmernltk import word_tokenize
            logging.getLogger("khmer-nltk").setLevel(logging.WARNING)
            _kh_tokenize = word_tokenize
        except Exception:  # noqa: BLE001
            _kh_tokenize = False
    if not _kh_tokenize:
        return [phrase]
    try:
        words = [w for w in _kh_tokenize(phrase) if w.strip()]
        return words if "".join(words) == phrase.replace(" ", "") else [phrase]
    except Exception:  # noqa: BLE001
        return [phrase]


def sentence_tokens(sentence: str, limit: int) -> list[list[str]]:
    """[[separator, word], ...] for one sentence. Punctuation sticks to the word before it."""
    tokens: list[list[str]] = []
    for pi, phrase in enumerate(sentence.split()):
        for wi, w in enumerate(khmer_words(phrase)):
            for k, piece in enumerate(_hard_split(w, limit) if text_units(w) > limit else [w]):
                sep = " " if (pi and wi == 0 and k == 0) else ""
                if tokens and PUNCT_ONLY_RE.match(piece):
                    tokens[-1][1] += sep + piece
                else:
                    tokens.append([sep, piece])
    return tokens


def wrap_lines(text: str, limit: int) -> list[str]:
    lines = layout_lines(sentence_tokens(text, limit), limit)
    return ["".join((sep if j else "") + w for j, (sep, w) in enumerate(t for t in ln)) for ln in lines]


def layout_lines(tokens: list[list[str]], limit: int) -> list[list[list[str]]]:
    lines, cur, cur_text = [], [], ""
    for tok in tokens:
        cand = cur_text + tok[0] + tok[1] if cur else tok[1]
        if cur and text_units(cand) > limit:
            lines.append(cur)
            cur, cur_text = [tok], tok[1]
        else:
            cur.append(tok)
            cur_text = cand
    return lines + ([cur] if cur else [])


# ---- word timing: forced alignment of the known script to the narration ---- #
_aligner = None
_aligner_lock = threading.Lock()


ALIGNER_MODEL = os.environ.get("ALIGNER_MODEL", "MahmoudAshraf/mms-300m-1130-forced-aligner")


def _load_aligner():
    """Meta MMS multilingual CTC aligner (Hugging Face safetensors) + uroman romanizer.
    Works for Khmer, English and most other languages because it aligns romanized text."""
    global _aligner
    with _aligner_lock:
        if _aligner is None:
            try:
                import torch
                import torchaudio.functional as taf
                import uroman
                from huggingface_hub import hf_hub_download
                from transformers import Wav2Vec2ForCTC

                ctx.log.info("Loading word aligner %s …", ALIGNER_MODEL)
                vocab = json.loads(Path(hf_hub_download(ALIGNER_MODEL, "vocab.json")).read_text())
                pre = json.loads(Path(hf_hub_download(ALIGNER_MODEL, "preprocessor_config.json")).read_text())
                model = Wav2Vec2ForCTC.from_pretrained(ALIGNER_MODEL, use_safetensors=True).eval()
                _aligner = SimpleNamespace(torch=torch, taf=taf, model=model, vocab=vocab,
                                           normalize=bool(pre.get("do_normalize", True)), uroman=uroman.Uroman())
            except Exception as e:  # noqa: BLE001
                ctx.log.warning("Word aligner unavailable, using estimated timings: %s", e)
                _aligner = False
    return _aligner


def align_words(wav: np.ndarray, sr: int, words: list[str]) -> Optional[list[tuple[float, float]]]:
    """Start/end seconds of each word inside wav (CTC forced alignment of the known text),
    or None if alignment isn't possible."""
    al = _load_aligner()
    if not al or not words:
        return None
    try:
        import librosa

        torch = al.torch
        roman = []
        for w in words:
            r = al.uroman.romanize_string(w).lower()
            roman.append("".join(ch for ch in r if ch in al.vocab and len(ch) == 1))
        idx = [i for i, r in enumerate(roman) if r]
        if not idx:
            return None
        y = librosa.resample(wav.astype(np.float32), orig_sr=sr, target_sr=16000)
        if al.normalize:
            y = (y - y.mean()) / np.sqrt(y.var() + 1e-7)
        with torch.inference_mode():
            logits = al.model(torch.from_numpy(y).unsqueeze(0)).logits
        log_probs = torch.log_softmax(logits.float(), dim=-1)
        target = [al.vocab[ch] for i in idx for ch in roman[i]]
        frames = log_probs.size(1)
        if frames < len(target) * 1.05:  # audio too short for the text
            return None
        ali, scores = al.taf.forced_align(log_probs, torch.tensor([target], dtype=torch.int32), blank=0)
        spans = al.taf.merge_tokens(ali[0], scores[0].exp())
        if len(spans) != len(target):
            return None
        sec_per_frame = len(y) / 16000 / frames
        found, pos = {}, 0
        for i in idx:
            n = len(roman[i])
            found[i] = (spans[pos].start * sec_per_frame, spans[pos + n - 1].end * sec_per_frame)
            pos += n
        out, prev_end = [], 0.0
        for i in range(len(words)):
            st, en = found.get(i, (prev_end, prev_end))
            out.append((st, en))
            prev_end = en
        return out
    except Exception as e:  # noqa: BLE001
        ctx.log.warning("Word alignment failed (%s); using estimated timings", e)
        return None


def estimate_words(words: list[str], dur: float) -> list[tuple[float, float]]:
    weights = [max(1, text_units(w)) for w in words]
    total, t, out = sum(weights), 0.0, []
    for wgt in weights:
        d = dur * wgt / total
        out.append((t, t + d))
        t += d
    return out


def _ts(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, sec = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{sec:05.2f}"


def _esc(text: str) -> str:
    return text.replace("{", "(").replace("}", ")").replace("\\", "/")


def _ass_color(hex_color: str, default: str = "#FFD21F") -> str:
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", hex_color or "") or re.fullmatch(r"#?([0-9a-fA-F]{6})", default)
    rr, gg, bb = m.group(1)[0:2], m.group(1)[2:4], m.group(1)[4:6]
    return f"&H{bb}{gg}{rr}&".upper()


def _chunk_text(lines: list[list[list[str]]], hl: Optional[set[int]], color: str, base: str, offset: int) -> str:
    """Caption text with the words whose global index is in `hl` recolored."""
    out_lines, k = [], offset
    for ln in lines:
        parts = []
        for j, (sep, w) in enumerate(ln):
            txt = _esc(w)
            if hl is not None and k in hl:
                txt = f"{{\\1c{color}}}{txt}{{\\1c{base}}}"
            parts.append((_esc(sep) if j else "") + txt)
            k += 1
        out_lines.append("".join(parts))
    return "\\N".join(out_lines)


def build_ass(path: Path, timeline: list[dict], s: dict, size: tuple[int, int],
              title: Optional[str], scene_dur: float) -> None:
    w, h = size
    vertical, wide = h > w, w > h
    font = s.get("caption_font") or caption_fonts()[0]
    cap_size = 80 if vertical else (64 if wide else 70)
    title_size = 84 if vertical else (72 if wide else 76)
    margin_v = 380 if vertical else (70 if wide else 120)
    style = s.get("caption_style", "outline")
    base = "&H004DE1FF" if style == "yellow" else "&H00FFFFFF"
    base_tag = "&H4DE1FF&" if style == "yellow" else "&HFFFFFF&"
    hl_color = _ass_color(s.get("highlight_color") or "#FFD21F")
    mode = s.get("caption_highlight", "word")
    if style == "box":
        border, outline, shadow, back = 3, 16, 0, "&H70000000"
    else:
        border, outline, shadow, back = 1, 6, 2, "&H96000000"
    head = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {w}\nPlayResY: {h}\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding\n"
        f"Style: Cap,{font},{cap_size},{base},&H000000FF,&H00000000,{back},0,0,0,0,100,100,0,0,"
        f"{border},{outline},{shadow},2,70,70,{margin_v},1\n"
        f"Style: Title,{font},{title_size},&H0000D7FF,&H000000FF,&H00000000,&H96000000,0,0,0,0,100,100,0,0,"
        f"1,7,3,8,70,70,{int(h * 0.1)},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    events = []
    if title:
        tl = (18 if vertical else 30) if KHMER_RE.search(title) else (22 if vertical else 38)
        events.append(f"Dialogue: 1,{_ts(0)},{_ts(scene_dur)},Title,,0,0,0,,{{\\fad(300,300)}}"
                      + "\\N".join(_esc(x) for x in wrap_lines(title, tl)))
    if s.get("captions", True):
        for item in timeline:
            start, end, sentence = item["start"], item["end"], item["text"]
            khmer = bool(KHMER_RE.search(sentence))
            limit = (16 if vertical else (30 if wide else 17)) if khmer else (22 if vertical else 40 if wide else 24)
            tokens = item.get("tokens") or sentence_tokens(sentence, limit)
            times = item.get("word_times") or estimate_words([t[1] for t in tokens], end - start)
            times = [(start + a, start + b) for a, b in times]
            lines = layout_lines(tokens, limit)
            # group lines into captions of up to two lines (3 -> 1 + 2, never a lonely last line)
            chunks, i = [], 0
            while i < len(lines):
                take = 2 if len(lines) - i != 3 else 1
                chunks.append(lines[i : i + take])
                i += take
            first = 0
            for ci, chunk in enumerate(chunks):
                n = sum(len(ln) for ln in chunk)
                idxs = list(range(first, first + n))
                c_start = start if ci == 0 else times[first][0]
                c_end = times[first + n][0] if first + n < len(times) else end
                if mode == "none":
                    events.append(f"Dialogue: 0,{_ts(c_start)},{_ts(c_end)},Cap,,0,0,0,,"
                                  f"{_chunk_text(chunk, None, hl_color, base_tag, first)}")
                else:
                    for k in idxs:  # one event per spoken word, that word (or all words so far) recolored
                        w_start = c_start if k == first else times[k][0]
                        w_end = times[k + 1][0] if k + 1 < first + n else c_end
                        if w_end - w_start < 0.01:
                            continue
                        hl = {k} if mode == "word" else set(range(first, k + 1))
                        events.append(f"Dialogue: 0,{_ts(w_start)},{_ts(w_end)},Cap,,0,0,0,,"
                                      f"{_chunk_text(chunk, hl, hl_color, base_tag, first)}")
                first += n
    path.write_text(head + "\n".join(events) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Narration + rendering
# --------------------------------------------------------------------------- #
def split_sentences(text: str) -> list[str]:
    out = []
    for sent in SENT_SPLIT_RE.findall(text.strip()):
        sent = sent.replace("\u200b", "\u2063")  # keep word boundaries for captions; removed before speaking
        sent = sent.strip()
        if sent:
            out.extend(c for c in ctx.split_text(sent) if c.strip())
    return out


def narration_parts(text: str) -> list[str]:
    """Tutorial narration is split by "|" into parts: part k is spoken while part k of the slide is on screen."""
    return [x.strip() for x in (text or "").split("|")]


def step_times(timeline: list[dict], steps: int, dur: float) -> list[float]:
    """When each later part of a slide appears (seconds into the scene), for steps 1..steps.
    A "|" in the narration gives the exact moment; parts without one are spread over the remaining
    sentences (or evenly over the remaining speech)."""
    if steps <= 0:
        return []
    starts: dict[int, float] = {}
    for it in timeline:
        starts.setdefault(it.get("part", 0), it["start"])
    speech_end = timeline[-1]["end"] if timeline else max(0.5, dur - TAIL_S)
    known = max(starts) if starts else 0
    times = []
    for k in range(1, min(steps, known) + 1):
        t = next((starts[j] for j in range(k, known + 1) if j in starts), speech_end)
        times.append(max(0.0, t - REVEAL_LEAD_S))
    rest = steps - len(times)
    if rest:
        last = times[-1] if times else 0.0
        cands = [it["start"] - REVEAL_LEAD_S for it in timeline if it["start"] - REVEAL_LEAD_S > last + 0.8]
        if len(cands) >= rest:
            times += [cands[j * len(cands) // rest] for j in range(rest)]
        else:
            span = max(0.5, speech_end - last)
            times += [last + span * (j + 1) / (rest + 1) for j in range(rest)]
    for k in range(1, len(times)):  # never earlier than the part before
        times[k] = max(times[k], times[k - 1])
    return times


def part_gap(prev_part: int, part_no: int, quiz: set[int]) -> float:
    """Silence between two sentences: longer where a new part of the slide appears, and a countdown's worth
    before a quiz answer is revealed (so viewers can think)."""
    if part_no == prev_part:
        return SENT_GAP_S
    return slides.QUIZ_COUNTDOWN if part_no in quiz else PART_GAP_S


def scene_quiz_steps(scene: dict) -> set[int]:
    return slides.quiz_reveal_steps(scene.get("slide", "")) if scene.get("layout") else set()


def estimate_timeline(narration: str, language: str, quiz: Optional[set[int]] = None) -> tuple[list[dict], float]:
    """Rough sentence timings from text length, for previewing an animated slide before it is narrated."""
    cps = LANG_RULES.get(language, LANG_RULES["km"])[1]
    t, timeline = LEAD_S, []
    for part_no, part in enumerate(narration_parts(narration)):
        for sent in SENT_SPLIT_RE.findall(part):
            sent = sent.strip()
            if not sent:
                continue
            if timeline:
                t += part_gap(timeline[-1]["part"], part_no, quiz or set())
            d = max(0.6, len(ZWSP_RE.sub("", sent)) / cps)
            timeline.append({"start": t, "end": t + d, "part": part_no})
            t += d
    return timeline, t + TAIL_S


def narrate(scene: dict, spec: dict, s: dict, seed: int, cancel) -> tuple[np.ndarray, list[dict]]:
    sr = ctx.engine.sample_rate
    parts = [np.zeros(int(sr * LEAD_S), dtype=np.float32)]
    t = LEAD_S
    timeline = []
    sentences = [(part_no, sent) for part_no, part in enumerate(narration_parts(scene["narration"]))
                 for sent in split_sentences(part)]
    quiz = scene_quiz_steps(scene)
    for k, (part_no, sent) in enumerate(sentences):
        if cancel():
            raise RuntimeError("Cancelled")
        if k:
            gap = part_gap(sentences[k - 1][0], part_no, quiz)
            parts.append(np.zeros(int(sr * gap), dtype=np.float32))
            t += gap
        wav = ctx.tts_chunk(clean_spoken(sent.replace("\u2063", "")), spec,
                            style=ctx.clean_style(s.get("voice_style", "")), seed=seed + k)
        parts.append(wav)
        item = {"start": t, "end": t + len(wav) / sr, "text": sent, "part": part_no}
        if s.get("captions", True) and s.get("caption_highlight", "word") != "none":
            limit = 16 if KHMER_RE.search(sent) else 22
            if s.get("aspect") == "16:9":
                limit = 30 if KHMER_RE.search(sent) else 40
            tokens = sentence_tokens(sent, limit)
            item["tokens"] = tokens
            item["word_times"] = align_words(wav, sr, [w for _, w in tokens])
        timeline.append(item)
        t += len(wav) / sr
    parts.append(np.zeros(int(sr * TAIL_S), dtype=np.float32))
    audio = np.concatenate(parts)
    peak = float(np.max(np.abs(audio))) if audio.size else 0
    if peak > 0.99:
        audio *= 0.99 / peak
    return audio, timeline


def _ff_path(p: Path) -> str:
    """Escape a path for use inside an ffmpeg filter argument."""
    return str(p).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


_RATE = ["-b:v", "8M", "-maxrate", "12M", "-bufsize", "16M", "-profile:v", "high"]
_HW_ENCODERS = {  # tried in order; the first one this ffmpeg + GPU can actually use is kept
    "darwin": [["-c:v", "h264_videotoolbox", *_RATE]],
    "win32": [["-c:v", "h264_nvenc", "-preset", "p5", *_RATE], ["-c:v", "h264_qsv", *_RATE],
              ["-c:v", "h264_amf", "-quality", "quality", *_RATE]],
    "linux": [["-c:v", "h264_nvenc", "-preset", "p5", *_RATE]],
}
_SW_ENCODER = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "19"]
_encoder: Optional[list[str]] = None


def _encode_args() -> list[str]:
    """Hardware H.264 encoder (VideoToolbox on a Mac, NVENC / Quick Sync / AMF on Windows), else x264."""
    global _encoder
    if _encoder is None:
        _encoder = _SW_ENCODER
        for args in _HW_ENCODERS.get(sys.platform, []):
            probe = subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.2",
                 "-pix_fmt", "yuv420p", *args, "-f", "null", "-"], capture_output=True)
            if probe.returncode == 0:
                _encoder = args
                break
        ctx.log.info("Video encoder: %s", _encoder[1])
    return _encoder


def render_scene(img: Path, wav: Path, ass: Path, out: Path, size: tuple[int, int], dur: float, i: int,
                 zoom: float = 0.14, overlays: Optional[list[dict]] = None, logo: Optional[dict] = None,
                 motion: Optional[Path] = None) -> None:
    """zoom: how far the slow zoom/pan moves (0 keeps the picture still, e.g. for code slides).
    overlays: assets placed on this scene (they fade in and stay still while the picture moves).
    logo: an asset shown the whole time, also during the fades between scenes.
    motion: an animated slide clip (from slide_motion) used instead of the still picture `img`."""
    w, h = size
    frames = max(1, int(round(dur * FPS)))
    big_w, big_h = w * 2, h * 2  # zoom on a 2x canvas so the motion is smooth, not jittery
    zm = f"{zoom:.3f}"
    moves = [
        (f"1+{zm}*on/{frames}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),  # slow zoom in
        (f"1+{zm}-{zm}*on/{frames}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),  # slow zoom out
        (f"1+{zm}", f"(iw-iw/zoom)*on/{frames}", "ih/2-(ih/zoom/2)"),  # pan left -> right
        (f"1+{zm}", f"(iw-iw/zoom)*(1-on/{frames})", "ih/2-(ih/zoom/2)"),  # pan right -> left
    ]
    z, x, y = moves[i % len(moves)] if zoom > 0 else ("1", "0", "0")
    fo = max(0.0, dur - 0.3)
    ass_path = _ff_path(ass)
    extra_inputs: list[str] = []
    if motion:
        graph = [f"[0:v]scale={w}:{h},setsar=1,fps={FPS},tpad=stop_mode=clone:stop_duration=2[p0]"]
    else:
        graph = [f"[0:v]scale={big_w}:{big_h}:force_original_aspect_ratio=increase,crop={big_w}:{big_h},"
                 f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={w}x{h}:fps={FPS}[p0]"]
    cur, n_in = "p0", 2
    for k, ov in enumerate(overlays or []):
        extra_inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{dur:.3f}", "-i", str(ov["path"])]
        graph.append(f"[{n_in}:v]scale={ov['w']}:{ov['h']},format=rgba,fade=t=in:st=0.25:d=0.4:alpha=1[o{k}]")
        graph.append(f"[{cur}][o{k}]overlay=x={ov['x']}:y={ov['y']}:eof_action=pass[p{k + 1}]")
        cur, n_in = f"p{k + 1}", n_in + 1
    graph.append(f"[{cur}]format=yuv420p,fade=t=in:st=0:d=0.3,fade=t=out:st={fo:.3f}:d=0.3[faded]")
    cur = "faded"
    if logo:
        extra_inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{dur:.3f}", "-i", str(logo["path"])]
        graph.append(f"[{n_in}:v]scale={logo['w']}:{logo['h']},format=rgba,"
                     f"colorchannelmixer=aa={logo.get('opacity', 0.9):.2f}[lg]")
        graph.append(f"[{cur}][lg]overlay=x={logo['x']}:y={logo['y']}:eof_action=pass[withlogo]")
        cur = "withlogo"
    graph.append(f"[{cur}]ass='{ass_path}':fontsdir='{_ff_path(ctx.fonts_dir)}',format=yuv420p[v]")
    graph.append(f"[1:a]afade=t=in:st=0:d=0.05,afade=t=out:st={fo:.3f}:d=0.3[a]")
    vf = ";".join(graph)
    base = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(motion or img), "-i", str(wav), *extra_inputs,
            "-filter_complex", vf, "-map", "[v]", "-map", "[a]", "-t", f"{dur:.3f}", "-r", str(FPS)]
    tail = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-movflags", "+faststart", str(out)]
    enc = _encode_args()
    proc = subprocess.run(base + enc + tail, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0 and enc is not _SW_ENCODER:  # hardware encoder failed on this scene: x264
        proc = subprocess.run(base + _SW_ENCODER + tail, capture_output=True, text=True, encoding="utf-8",
                              errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed on scene {i + 1}: {proc.stderr.strip()[-300:]}")


def render_project(job_id: str, pid: str) -> None:
    job = ctx.jobs[job_id]
    job.update(status="running", started=time.time())
    cancel = lambda: bool(job.get("cancel"))  # noqa: E731
    pdir = _pdir(pid)
    pool = ThreadPoolExecutor(max_workers=3)
    try:
        with ctx.project_lock:
            p = load_project(pid)
            p["status"] = "rendering"
            save_project(p)
        s = p["settings"]
        scenes = p["scenes"]
        n = len(scenes)
        size = ASPECTS.get(s.get("aspect"), ASPECTS["9:16"])[0]
        job["total"] = n * 3 + 1
        progress = {"img": 0, "voice": 0, "render": 0}

        def bump(kind: str, msg: str) -> None:
            progress[kind] += 1
            job["done"] = sum(progress.values())
            job["message"] = msg

        # 1) online images download in the background while the GPU narrates. Local images are
        #    made first instead (both models on the GPU at once would fight for memory), and the
        #    image model is unloaded before narration starts.
        job.update(stage="images", message="Generating images…")
        futures = {}
        if s.get("image_source") == "local":
            try:
                for i, sc in enumerate(scenes):
                    if cancel():
                        raise RuntimeError("Cancelled")
                    if not image_ready(pdir, sc, s):
                        job["message"] = (f"Painting image {i + 1}/{n} on this computer…" if local_images.running()
                                          else "Loading the image model…")
                    try:
                        path = scene_image(pdir, i, sc, s)
                    except ImageError as e:
                        raise RuntimeError(f"Scene {i + 1} image: {e}")
                    futures[i] = pool.submit(lambda path=path: path)
                    bump("img", f"Image {i + 1}/{n} ready")
            finally:
                local_images.stop()
        else:
            for i, sc in enumerate(scenes):
                def work(i=i, sc=sc):
                    path = scene_image(pdir, i, sc, s)
                    bump("img", f"Image {progress['img'] + 1}/{n} ready")
                    return path
                futures[i] = pool.submit(work)

        # 2) narration in the cloned voice, sentence by sentence (gives exact caption timing)
        spec = ctx.resolve_voice(s.get("voice_id"), s.get("use_transcript", True))
        base_seed = p.get("voice_seed") or random.randint(1, 2**31 - 1)
        p["voice_seed"] = base_seed
        tmp_ref = None
        audio_info = []
        try:
            for i, sc in enumerate(scenes):
                if cancel():
                    raise RuntimeError("Cancelled")
                job.update(stage="voice", message=f"Narrating scene {i + 1}/{n}…")
                audio, timeline = narrate(sc, spec, s, base_seed + i * 100, cancel)
                wav_path = pdir / f"scene_{i + 1:02d}.wav"
                sf.write(str(wav_path), audio, ctx.engine.sample_rate, subtype="PCM_16")
                audio_info.append((wav_path, timeline, len(audio) / ctx.engine.sample_rate))
                if not s.get("voice_id") and i == 0:  # designed voice: keep it identical for later scenes
                    fd, tmp_ref = tempfile.mkstemp(suffix=".wav")
                    os.close(fd)
                    sf.write(tmp_ref, audio, ctx.engine.sample_rate)
                    spec["ref_path"] = tmp_ref
                bump("voice", f"Scene {i + 1}/{n} narrated")
        finally:
            if tmp_ref and os.path.exists(tmp_ref):
                os.unlink(tmp_ref)

        # 3) wait for images
        job.update(stage="images", message="Waiting for images…")
        images = []
        for i in range(n):
            try:
                images.append(futures[i].result())
            except ImageError as e:
                raise RuntimeError(f"Scene {i + 1} image: {e}")
            if cancel():
                raise RuntimeError("Cancelled")

        # 4) render each scene with motion + captions (+ logo / assets), then join
        logo = assets.placement(s.get("logo_asset"), s.get("logo_pos"), s.get("logo_size"), size)
        if logo:
            logo["opacity"] = float(s.get("logo_opacity") or 0.9)
        parts = []
        for i in range(n):
            if cancel():
                raise RuntimeError("Cancelled")
            job.update(stage="render", message=f"Rendering scene {i + 1}/{n}…")
            wav_path, timeline, dur = audio_info[i]
            ass = pdir / f"scene_{i + 1:02d}.ass"
            title = p.get("title") if (i == 0 and s.get("show_title", True)) else None
            build_ass(ass, timeline, s, size, title, dur)
            out = pdir / f"scene_{i + 1:02d}.mp4"
            frame, layout, motion = images[i], scenes[i].get("layout"), None
            if layout in ("slide", "cover"):
                bg = frame if layout == "cover" else None
                n_steps = slides.count_steps(scenes[i].get("slide", ""))
                if n_steps or slides.has_logos(scenes[i].get("slide", "")):  # parts appear with the narration
                    def shown(frac: float, i=i) -> None:
                        job["message"] = f"Animating slide {i + 1}/{n}… {int(frac * 100)}%"
                    job["message"] = f"Animating slide {i + 1}/{n}…"
                    try:
                        motion = slide_motion(pdir, p, i, scenes[i], s, bg, step_times(timeline, n_steps, dur), dur,
                                              cancel, shown)
                    except Exception as e:  # noqa: BLE001
                        if str(e) == "Cancelled":
                            raise
                        ctx.log.warning("Slide %d animation failed, using a still slide: %s", i + 1, e)
                    job["message"] = f"Rendering scene {i + 1}/{n}…"
                frame = None if motion else slide_frame(pdir, p, i, scenes[i], s, bg)
            audio_path = wav_path
            if motion and s.get("sfx", True):  # pops, typing, whooshes, quiz ticks and chime, under the voice
                audio_path = with_sound_effects(wav_path, motion_events(motion))
            # code slides stay still so they're readable; title slides move gently so the text stays in frame
            placed = [pl for o in scenes[i].get("overlays") or []
                      if (pl := assets.placement(o.get("asset"), o.get("pos"), o.get("size"), size))]
            render_scene(frame, audio_path, ass, out, size, dur, i,
                         zoom={"slide": 0.0, "cover": 0.04}.get(layout, 0.14), overlays=placed, logo=logo,
                         motion=motion)
            parts.append(out)
            bump("render", f"Scene {i + 1}/{n} rendered")

        job.update(stage="finish", message="Joining scenes…")
        listing = pdir / "concat.txt"
        listing.write_text("".join(f"file '{x.name}'\n" for x in parts), encoding="utf-8")
        final = pdir / "video.mp4"
        tmp_final = pdir / "video.tmp.mp4"
        proc = subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
             "-c", "copy", "-movflags", "+faststart", str(tmp_final)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=pdir,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"Joining scenes failed: {proc.stderr.strip()[-300:]}")
        track = music.get(s.get("music"))
        if track:  # background music under the whole video, quieter while the narrator speaks
            job["message"] = "Adding background music…"
            with_music = pdir / "video.music.tmp.mp4"
            music.mix_music(tmp_final, with_music, track["path"], float(s.get("music_volume") or 0.15),
                            sum(a[2] for a in audio_info))
            with_music.replace(tmp_final)
        tmp_final.replace(final)
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", "0.8", "-i", str(final),
                        "-frames:v", "1", "-vf", "scale=360:-2", str(pdir / "thumb.jpg")], capture_output=True)
        duration = round(sum(a[2] for a in audio_info), 2)
        with ctx.project_lock:
            fresh = load_project(pid)
            by_id = {sc.get("id"): sc for sc in scenes}
            for f in fresh["scenes"]:  # keep the images generated during this render
                sc = by_id.get(f.get("id"))
                if sc and f.get("image_prompt") == sc.get("image_prompt"):
                    for k in ("image_key", "ai_image", "image_v"):
                        if sc.get(k):
                            f[k] = sc[k]
            fresh.update(status="done", voice_seed=base_seed, error=None,
                         render={"video": "video.mp4", "duration": duration, "rendered_at": time.time(),
                                 "seconds_taken": round(time.time() - job["started"], 1)})
            save_project(fresh)
        job.update(status="done", done=job["total"], stage="done", message="Done", result=summary(fresh))
        ctx.log.info("Video %s rendered: %d scenes, %.1fs in %.1fs", pid, n, duration, time.time() - job["started"])
    except Exception as e:  # noqa: BLE001
        msg = e.detail if isinstance(e, HTTPException) else str(e)
        if msg != "Cancelled":
            ctx.log.exception("Video job %s failed", job_id)
        try:
            with ctx.project_lock:
                p = load_project(pid)
                p.update(status="cancelled" if msg == "Cancelled" else "error", error=msg)
                save_project(p)
        except Exception:  # noqa: BLE001
            pass
        job.update(status="cancelled" if msg == "Cancelled" else "error", error=msg)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        job["finished"] = time.time()
