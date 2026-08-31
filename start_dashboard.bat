@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Run setup_windows.ps1 first.
    pause
    exit /b 1
)
echo Starting LAN dashboard on port 5050. Open http://YOUR-PC-IP:5050 from another device.
".venv\Scripts\python.exe" dashboard.py
pause
