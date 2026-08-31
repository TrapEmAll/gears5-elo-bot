@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Run setup_windows.ps1 first.
    pause
    exit /b 1
)
echo Starting local dashboard at http://127.0.0.1:5050
".venv\Scripts\python.exe" dashboard.py
pause
