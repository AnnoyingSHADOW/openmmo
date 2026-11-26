@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PORT=7860
rem Uses Claude Code Pro (claude.ai subscription) for script writing.
rem Run "claude" once in a terminal to sign in before starting.
start "" /b powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep 4; Start-Process 'http://127.0.0.1:7860'"
echo Starting VoiceClone Studio on http://127.0.0.1:7860
.venv\Scripts\python.exe server.py
pause
