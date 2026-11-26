"""Local image generator for the Video maker on Windows/Linux with an NVIDIA GPU.

Same protocol as imagegen_worker.py (the Apple one), but runs diffusers on CUDA inside
.venv-img. video.py starts it, keeps it loaded while pictures are made, and closes its
stdin when idle, which makes it exit and give its VRAM back.

Models are picked to fit small GPUs (4 GB):
  sd-turbo    stabilityai/sd-turbo     ~2 GB, fits entirely on the GPU, native 512 px
  sdxl-turbo  stabilityai/sdxl-turbo   better pictures; weights are streamed layer by layer
                                       (sequential CPU offload), so it is slower but needs little VRAM

Protocol: one JSON object per line.
  stdin  <- {"prompt": str, "seed": int, "width": int, "height": int, "steps": int, "area": int, "out": "/abs/file.jpg"}
  stdout -> {"ready": true, "load_secs": float} | {"ok": true, "path": str, "secs": float} | {"ok": false, "error": str}
"""

import gc
import json
import math
import os
import sys
import time
import traceback


def main() -> int:
    proto = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
    os.dup2(2, 1)
    sys.stdout = sys.stderr

    def send(obj: dict) -> None:
        proto.write(json.dumps(obj, ensure_ascii=False) + "\n")
        proto.flush()

    repo = os.environ.get("LOCAL_IMAGE_MODEL", "stabilityai/sd-turbo")
    family = os.environ.get("LOCAL_IMAGE_FAMILY", "sd")  # sd | sdxl
    t0 = time.time()
    try:
        import torch
        from diffusers import AutoPipelineForText2Image

        if not torch.cuda.is_available():
            raise RuntimeError("PyTorch can't see an NVIDIA GPU in .venv-img (reinstall it with setup-local-images.bat).")
        # GTX 16xx cards give NaN (black pictures) in fp16, so default to fp32 with CPU offload.
        # Set LOCAL_IMAGE_DTYPE=float16 on cards where fp16 works (faster, less memory).
        dtype = torch.float16 if os.environ.get("LOCAL_IMAGE_DTYPE") == "float16" else torch.float32
        pipe = AutoPipelineForText2Image.from_pretrained(repo, torch_dtype=dtype, variant="fp16")  # fp16 weights, upcast on load if needed
        pipe.set_progress_bar_config(disable=True)
        pipe.vae.to(torch.float32)  # decode in fp32 regardless
        if family == "sdxl":
            pipe.enable_sequential_cpu_offload()  # SDXL's UNet alone is ~5 GB in fp16
        elif dtype == torch.float32:
            pipe.enable_model_cpu_offload()  # fp32 UNet is ~3.4 GB: keep only the active part on a 4 GB GPU
        else:
            pipe.to("cuda")
        pipe.vae.enable_slicing()
        pipe.vae.enable_tiling()
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        send({"ok": False, "error": f"{type(e).__name__}: {e}"[:400]})
        return 1
    print(f"[imagegen] loaded {repo} in {time.time() - t0:.1f}s", file=sys.stderr, flush=True)
    send({"ready": True, "load_secs": round(time.time() - t0, 1)})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        t = time.time()
        try:
            req = json.loads(line)
            # These models are trained near 512 px: keep the requested shape, but cap the pixel count.
            w0, h0 = int(req["width"]), int(req["height"])
            k = math.sqrt(int(req.get("area") or 512 * 512) / (w0 * h0))
            w, h = max(256, round(w0 * k / 8) * 8), max(256, round(h0 * k / 8) * 8)
            latents = pipe(
                prompt=str(req["prompt"]),
                num_inference_steps=int(req.get("steps") or 2),
                guidance_scale=0.0,  # turbo models are trained without classifier-free guidance
                width=w,
                height=h,
                generator=torch.Generator("cpu").manual_seed(int(req["seed"])),
                output_type="latent",
            ).images
            with torch.no_grad():
                dec = pipe.vae.decode(latents.to(torch.float32) / pipe.vae.config.scaling_factor).sample
            image = pipe.image_processor.postprocess(dec, output_type="pil")[0].convert("RGB")
            del latents, dec
            if image.getextrema() == ((0, 0), (0, 0), (0, 0)):
                raise RuntimeError("The GPU produced a black picture (fp16 problem on this graphics card).")
            out = req["out"]
            tmp = out + ".tmp.jpg"
            image.save(tmp, "JPEG", quality=92)
            os.replace(tmp, out)
            del image
            gc.collect()
            torch.cuda.empty_cache()
            send({"ok": True, "path": out, "secs": round(time.time() - t, 1)})
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            try:
                gc.collect()
                torch.cuda.empty_cache()
            except Exception:  # noqa: BLE001
                pass
            send({"ok": False, "error": f"{type(e).__name__}: {e}"[:400]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
