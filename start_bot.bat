@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Bot setup has not been run yet.
    echo Running setup now...
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_windows.ps1"
    if errorlevel 1 (
        echo Setup failed. See the message above.
        pause
        exit /b 1
    )
)

if not exist ".env" (
    echo .env is missing. Run setup_windows.ps1 first.
    pause
    exit /b 1
)

findstr /b "DISCORD_TOKEN=put-your-bot-token-here" .env >nul
if not errorlevel 1 (
    echo Open .env and replace the placeholder with your Discord bot token, then run this file again.
    pause
    exit /b 1
)

echo Starting Gears 5 Elo Bot. Close this window to stop it.
".venv\Scripts\python.exe" bot.py
if errorlevel 1 (
    echo.
    echo The bot stopped with an error. Read the message above.
    pause
)
