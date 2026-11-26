@echo off
rem Start VoiceClone Studio on Windows: http://127.0.0.1:7860
rem   run.bat                      first run creates .venv (PyTorch with CUDA if an NVIDIA GPU is found)
rem   set TORCH_CUDA=cu126 ^& run.bat   older NVIDIA driver: pick another PyTorch CUDA build before the first run
setlocal
cd /d "%~dp0"
if not defined PORT set "PORT=7860"
if not defined TORCH_CUDA set "TORCH_CUDA=cu132"
rem UTF-8 everywhere so Khmer text survives pipes, logs and subprocesses
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PY=.venv\Scripts\python.exe"

if not exist ".venv\.ready" (
  call :setup
  if errorlevel 1 goto :fail
)

where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo ffmpeg is required. Install it with:  winget install Gyan.FFmpeg
  echo Then open a new terminal window and run run.bat again.
  goto :fail
)

rem open the browser once the server has had a few seconds to start
start "" /b powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep 4; Start-Process 'http://127.0.0.1:%PORT%'"
"%PY%" server.py
exit /b %errorlevel%


:setup
echo Creating the Python 3.12 environment (first run only, downloads a few GB)...
set "TORCH_INDEX="
where nvidia-smi >nul 2>nul
if not errorlevel 1 (
  set "TORCH_INDEX=https://download.pytorch.org/whl/%TORCH_CUDA%"
  echo NVIDIA GPU found: installing PyTorch with CUDA ^(%TORCH_CUDA%^).
) else (
  echo No NVIDIA GPU found: using PyTorch on the CPU ^(works, but speech is several times slower^).
)

where uv >nul 2>nul
if not errorlevel 1 (
  if not exist "%PY%" uv venv --python 3.12 .venv || exit /b 1
  if defined TORCH_INDEX uv pip install --python "%PY%" torch --index-url "%TORCH_INDEX%" || exit /b 1
  uv pip install --python "%PY%" -r requirements.txt || exit /b 1
) else (
  where py >nul 2>nul
  if errorlevel 1 (
    echo Python 3.12 is required. Install uv ^(winget install astral-sh.uv^) or Python 3.12 from python.org.
    exit /b 1
  )
  if not exist "%PY%" py -3.12 -m venv .venv || exit /b 1
  "%PY%" -m pip install --upgrade pip || exit /b 1
  if defined TORCH_INDEX "%PY%" -m pip install torch --index-url "%TORCH_INDEX%" || exit /b 1
  "%PY%" -m pip install -r requirements.txt || exit /b 1
)
echo ok> ".venv\.ready"
exit /b 0


:fail
echo.
pause
exit /b 1
