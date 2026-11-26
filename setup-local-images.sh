#!/usr/bin/env bash
# One-time setup for "On this Mac" image generation in the Video maker.
# Installs mflux (Apple MLX image models) into its own environment, .venv-img,
# so its PyTorch/transformers versions never clash with the voice app's .venv,
# then downloads the image models into ~/.cache/huggingface.
#
#   ./setup-local-images.sh          FLUX.2 klein 4B, fast (4.6 GB)
#   ./setup-local-images.sh best     Z-Image Turbo, photographic but ~3.5x slower (5.9 GB)
#   ./setup-local-images.sh all      both
set -euo pipefail
cd "$(dirname "$0")"

MFLUX_VERSION="${MFLUX_VERSION:-0.24.0}"
FAST="mflux-community/flux2-klein-4b-mflux-q4"
BEST="filipstrand/Z-Image-Turbo-mflux-4bit"
case "${1:-fast}" in
  fast) MODELS=("$FAST") ;;
  best) MODELS=("$BEST") ;;
  all)  MODELS=("$FAST" "$BEST") ;;
  *) echo "Usage: $0 [fast|best|all]" >&2; exit 2 ;;
esac

if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
  echo "Local image generation needs an Apple Silicon Mac." >&2
  exit 1
fi

if command -v uv >/dev/null 2>&1; then
  [[ -x .venv-img/bin/python ]] || uv venv --python 3.12 .venv-img
  uv pip install --python .venv-img/bin/python "mflux==${MFLUX_VERSION}"
else
  PY="$(command -v python3.12 || command -v python3.13 || true)"
  if [[ -z "$PY" ]]; then
    echo "Python 3.12 or 3.13 is required (or install uv: https://docs.astral.sh/uv/)." >&2
    exit 1
  fi
  [[ -x .venv-img/bin/python ]] || "$PY" -m venv .venv-img
  .venv-img/bin/python -m pip install --upgrade pip
  .venv-img/bin/python -m pip install "mflux==${MFLUX_VERSION}"
fi

for MODEL in "${MODELS[@]}"; do
  echo "Downloading ${MODEL} (first time only)..."
  # Plain HTTPS: the Xet transfer protocol has stalled on this network before.
  HF_HUB_DISABLE_XET="${HF_HUB_DISABLE_XET:-1}" .venv-img/bin/hf download "$MODEL" --exclude "*.png" --exclude "*.jpg"
done

echo
echo "Done. Restart ./run.sh, then pick Images: \"On this Mac\" in the Video maker."
