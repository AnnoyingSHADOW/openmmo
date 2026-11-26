"""Local image generator for the Video maker ("Images: On this Mac").

Runs inside .venv-img (mflux on Apple MLX), never in the voice app's environment.
video.py starts it, keeps it loaded while pictures are being made, and closes its
stdin when idle, which makes it exit and give its memory back.

Protocol: one JSON object per line.
  stdin  <- {"prompt": str, "seed": int, "width": int, "height": int, "steps": int, "out": "/abs/file.jpg"}
  stdout -> {"ready": true, "load_secs": float}            (once, after the model has loaded)
            {"ok": true, "path": str, "secs": float}       (per picture)
            {"ok": false, "error": str}                    (load or picture failure)
"""

import gc
import json
import os
import sys
import time
import traceback


def main() -> int:
    # Keep stdout for the protocol only; anything libraries print goes to stderr (data/imagegen.log).
    proto = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
    os.dup2(2, 1)
    sys.stdout = sys.stderr

    def send(obj: dict) -> None:
        proto.write(json.dumps(obj, ensure_ascii=False) + "\n")
        proto.flush()

    model_id = os.environ.get("LOCAL_IMAGE_MODEL", "mflux-community/flux2-klein-4b-mflux-q4")
    family = os.environ.get("LOCAL_IMAGE_FAMILY", "flux2")  # flux2 | zimage
    base = os.environ.get("LOCAL_IMAGE_BASE", "flux2-klein-4b")
    t0 = time.time()
    try:
        import mlx.core as mx
        from mflux.models.common.config.model_config import ModelConfig

        mx.set_cache_limit(2 * 1024**3)  # don't let MLX hoard freed buffers next to the voice model
        if family == "zimage":
            from mflux.models.z_image import ZImage

            model = ZImage(model_config=ModelConfig.from_name(base), model_path=model_id)
        else:
            from mflux.models.flux2.variants import Flux2Klein

            model = Flux2Klein(model_config=ModelConfig.from_name(base), model_path=model_id)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        send({"ok": False, "error": f"{type(e).__name__}: {e}"[:400]})
        return 1
    print(f"[imagegen] loaded {model_id} in {time.time() - t0:.1f}s", file=sys.stderr, flush=True)
    send({"ready": True, "load_secs": round(time.time() - t0, 1)})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        t = time.time()
        try:
            req = json.loads(line)
            w, h = int(req["width"]) // 16 * 16, int(req["height"]) // 16 * 16
            image = model.generate_image(
                seed=int(req["seed"]),
                prompt=str(req["prompt"]),
                num_inference_steps=int(req.get("steps") or (9 if family == "zimage" else 4)),
                width=w,
                height=h,
            )
            out = req["out"]
            tmp = out + ".tmp.jpg"
            image.image.convert("RGB").save(tmp, "JPEG", quality=92)
            os.replace(tmp, out)
            del image
            gc.collect()
            mx.clear_cache()
            send({"ok": True, "path": out, "secs": round(time.time() - t, 1)})
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            send({"ok": False, "error": f"{type(e).__name__}: {e}"[:400]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
