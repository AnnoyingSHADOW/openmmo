#!/usr/bin/env bash
# Start VoiceClone Studio on http://127.0.0.1:7860 (macOS / Linux; on Windows use run.bat)
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "Creating Python 3.12 environment..."
  if command -v uv >/dev/null 2>&1; then
    uv venv --python 3.12 .venv
    uv pip install --python .venv/bin/python -r requirements.txt
  else
    python3.12 -m venv .venv
    .venv/bin/pip install -r requirements.txt
  fi
fi

command -v ffmpeg >/dev/null 2>&1 || { echo "ffmpeg is required: brew install ffmpeg (Linux: sudo apt install ffmpeg)"; exit 1; }

OPEN=open; command -v open >/dev/null 2>&1 || OPEN=xdg-open
(sleep 3 && "$OPEN" "http://127.0.0.1:${PORT:-7860}") >/dev/null 2>&1 &
exec .venv/bin/python server.py
