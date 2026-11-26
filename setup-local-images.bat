@echo off
rem One-time setup for local image generation on Windows with an NVIDIA GPU.
rem Makes its own environment, .venv-img (PyTorch + diffusers), so it never clashes with the voice app's .venv,
rem then downloads the image models into %USERPROFILE%\.cache\huggingface.
rem
rem   setup-local-images.bat          SD-Turbo, fast, fits 4 GB GPUs (about 2.6 GB)
rem   setup-local-images.bat best     SDXL-Turbo, better but slower (about 7 GB)
rem   setup-local-images.bat all      both
rem   set TORCH_CUDA=cu126 ^& setup-local-images.bat    older NVIDIA driver: another PyTorch CUDA build
setlocal
cd /d "%~dp0"
if not defined TORCH_CUDA set "TORCH_CUDA=cu132"
set "PYTHONUTF8=1"
set "PY=.venv-img\Scripts\python.exe"
set "FAST=stabilityai/sd-turbo"
set "BEST=stabilityai/sdxl-turbo"
set "MODELS=%FAST%"
if /i "%~1"=="best" set "MODELS=%BEST%"
if /i "%~1"=="all" set "MODELS=%FAST% %BEST%"

where nvidia-smi >nul 2>nul
if errorlevel 1 (
  echo No NVIDIA GPU found. Local images on Windows need one.
  goto :fail
)

where uv >nul 2>nul
if not errorlevel 1 (
  if not exist "%PY%" uv venv --python 3.12 .venv-img || goto :fail
  uv pip install --python "%PY%" torch --index-url "https://download.pytorch.org/whl/%TORCH_CUDA%" || goto :fail
  uv pip install --python "%PY%" diffusers transformers accelerate safetensors pillow huggingface_hub || goto :fail
) else (
  where py >nul 2>nul
  if errorlevel 1 (
    echo Python 3.12 is required. Install uv ^(winget install astral-sh.uv^) or Python 3.12 from python.org.
    goto :fail
  )
  if not exist "%PY%" py -3.12 -m venv .venv-img || goto :fail
  "%PY%" -m pip install --upgrade pip || goto :fail
  "%PY%" -m pip install torch --index-url "https://download.pytorch.org/whl/%TORCH_CUDA%" || goto :fail
  "%PY%" -m pip install diffusers transformers accelerate safetensors pillow huggingface_hub || goto :fail
)

"%PY%" -c "import torch,sys; sys.exit(0 if torch.cuda.is_available() else 1)"
if errorlevel 1 (
  echo PyTorch cannot see the NVIDIA GPU. Update the NVIDIA driver, or set TORCH_CUDA=cu126 and run this again.
  goto :fail
)

for %%M in (%MODELS%) do (
  echo Downloading %%M ^(first time only^)...
  rem Only the fp16 weights, in the formats diffusers needs.
  "%PY%" -I -c "import sys; from huggingface_hub import snapshot_download as d; d(sys.argv[1], allow_patterns=['*.json','*.txt','*fp16.safetensors'], ignore_patterns=['sd_*','*.onnx*'])" %%M || goto :fail
)

echo.
echo Done. Restart the app, then pick Images: "On this computer" in the Video maker.
exit /b 0

:fail
echo.
echo Setup failed.
pause
exit /b 1
