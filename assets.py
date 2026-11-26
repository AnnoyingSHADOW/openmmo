"""Asset library: logos, stickers, product photos from your computer, reusable in every video.

Files are normalised to trimmed RGBA PNGs in data/assets/<id>.png (index in data/assets/index.json).
Videos place them on top of scenes at render time (see video.render_scene): a logo on every scene,
or chosen assets on chosen scenes.
"""

from __future__ import annotations

import base64
import json
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageOps

import slides

try:  # iPhone HEIC/HEIF photos on every OS (macOS can also fall back to its own `sips` tool)
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:
    pass

router = APIRouter()
ctx = SimpleNamespace()
AID_RE = re.compile(r"^[a-f0-9]{8}$")
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_SIDE = 1600
MAX_ASSETS = 200

# where an asset sits (ffmpeg overlay x, y; M = margin) and how wide it is (share of the frame width)
POSITIONS = {
    "tl": ("M", "M"), "tc": ("(W-w)/2", "M"), "tr": ("W-w-M", "M"),
    "c": ("(W-w)/2", "(H-h)/2"),
    "bl": ("M", "H-h-M"), "bc": ("(W-w)/2", "H-h-M"), "br": ("W-w-M", "H-h-M"),
}
SIZES = {"xs": 0.10, "s": 0.16, "m": 0.25, "l": 0.40, "xl": 0.60}


def setup(data_dir: Path, log) -> None:
    ctx.dir = data_dir / "assets"
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


def get(aid: Optional[str]) -> Optional[dict]:
    """The asset record (with its file path) if it exists, else None."""
    if not aid or not AID_RE.match(aid):
        return None
    for it in _load():
        if it["id"] == aid and (ctx.dir / it["file"]).exists():
            return {**it, "path": ctx.dir / it["file"]}
    return None


def placement(aid: str, pos: str, size: str, frame: tuple[int, int]) -> Optional[dict]:
    """Pixel size and ffmpeg overlay position of an asset on a frame."""
    a = get(aid)
    if not a:
        return None
    fw, fh = frame
    w = max(8, round(fw * SIZES.get(size, SIZES["s"])))
    h = max(8, round(w * a["h"] / max(1, a["w"])))
    max_h = round(fh * 0.45)
    if h > max_h:  # very tall assets: limit by height instead
        w, h = max(8, round(w * max_h / h)), max_h
    w, h = w // 2 * 2, h // 2 * 2
    margin = str(round(min(fw, fh) * 0.045))
    x, y = POSITIONS.get(pos, POSITIONS["tr"])
    return {"path": a["path"], "w": w, "h": h, "x": x.replace("M", margin), "y": y.replace("M", margin)}


# --------------------------------------------------------------------------- #
# Conversion
# --------------------------------------------------------------------------- #
def _svg_to_png(svg: Path, out: Path) -> Path:
    data = base64.b64encode(svg.read_bytes()).decode()
    page = (f'<!doctype html><html style="background:transparent"><body style="margin:0;background:transparent">'
            f'<img src="data:image/svg+xml;base64,{data}" style="width:{MAX_SIDE}px;height:{MAX_SIDE}px;'
            f'object-fit:contain;display:block"></body></html>')
    return slides.render_png(page, (MAX_SIDE, MAX_SIDE), out, transparent=True)


def open_image_file(raw: Path, td: Path) -> Optional[Image.Image]:
    """Any image Pillow (+ pillow-heif) can read; on macOS also whatever `sips` can convert. None if unreadable."""
    try:
        im = Image.open(raw)
        im.load()
        return im
    except Exception:  # noqa: BLE001  e.g. HEIC without pillow-heif installed: let macOS convert it
        pass
    if not shutil.which("sips"):
        return None
    conv = td / "conv.png"
    subprocess.run(["sips", "-s", "format", "png", str(raw), "--out", str(conv)], capture_output=True)
    if not conv.exists():
        return None
    im = Image.open(conv)
    im.load()
    return im


def _open_image(raw: Path, td: Path) -> Image.Image:
    if raw.suffix.lower() == ".svg":
        return Image.open(_svg_to_png(raw, td / "svg.png"))
    im = open_image_file(raw, td)
    if im is None:
        raise HTTPException(400, f"Could not read {raw.name}. Use PNG, JPG, WEBP, SVG or HEIC.")
    return im


def _remove_white(im: Image.Image) -> Image.Image:
    """Make the white background see-through: only near-white areas connected to the image border,
    so white parts inside the logo (letters, highlights) stay. Edges are softened slightly."""
    arr = np.array(im)
    near = arr[..., :3].min(axis=2) >= 232
    mask = Image.fromarray(near.astype(np.uint8) * 255, "L").copy()  # copy: fromarray images are read-only
    w, h = mask.size
    for xy in [(x, 0) for x in range(w)] + [(x, h - 1) for x in range(w)] + \
              [(0, y) for y in range(h)] + [(w - 1, y) for y in range(h)]:
        if mask.getpixel(xy) == 255:
            ImageDraw.floodfill(mask, xy, 128)
    background = (np.array(mask) == 128).astype(np.uint8) * 255
    soft = np.array(Image.fromarray(background, "L").filter(ImageFilter.GaussianBlur(1.0)))
    arr[..., 3] = np.minimum(arr[..., 3], 255 - soft)
    return Image.fromarray(arr, "RGBA")


def ingest(raw: Path, name: str, remove_white: bool) -> dict:
    with tempfile.TemporaryDirectory() as td:
        im = _open_image(raw, Path(td))
        im = ImageOps.exif_transpose(im).convert("RGBA")
        im.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
        if remove_white:
            im = _remove_white(im)
        bbox = im.getchannel("A").getbbox()  # trim see-through borders
        if bbox is None:
            raise HTTPException(400, f"{name} is completely transparent.")
        im = im.crop(bbox)
        aid = secrets.token_hex(4)
        out = ctx.dir / f"{aid}.png"
        im.save(out, "PNG", optimize=True)
    clean = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", Path(name).name)[:60] or "asset"
    return {"id": aid, "name": clean, "file": out.name, "w": im.width, "h": im.height, "created": time.time()}


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@router.get("/api/assets")
def list_assets():
    return [{k: it[k] for k in ("id", "name", "w", "h")} for it in _load() if (ctx.dir / it["file"]).exists()]


@router.post("/api/assets")
def upload_assets(files: list[UploadFile] = File(...), remove_white: bool = Form(False)):
    added = []
    for up in files:
        with tempfile.TemporaryDirectory() as td:
            raw = Path(td) / ("upload" + (Path(up.filename or "").suffix.lower() or ".img"))
            size = 0
            with raw.open("wb") as f:
                while chunk := up.file.read(1 << 20):
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise HTTPException(413, f"{up.filename} is larger than 15 MB.")
                    f.write(chunk)
            added.append(ingest(raw, up.filename or "asset", remove_white))
    with ctx.lock:
        items = _load() + added
        if len(items) > MAX_ASSETS:
            raise HTTPException(400, f"The library holds up to {MAX_ASSETS} assets. Delete some first.")
        _save(items)
    ctx.log.info("Added %d asset(s)", len(added))
    return list_assets()


@router.get("/api/assets/{aid}.png")
def asset_file(aid: str):
    a = get(aid)
    if not a:
        raise HTTPException(404, "not found")
    return FileResponse(a["path"], media_type="image/png")


@router.delete("/api/assets/{aid}")
def delete_asset(aid: str):
    if not AID_RE.match(aid):
        raise HTTPException(404, "not found")
    with ctx.lock:
        items = _load()
        for it in items:
            if it["id"] == aid:
                (ctx.dir / it["file"]).unlink(missing_ok=True)
        _save([it for it in items if it["id"] != aid])
    return list_assets()
