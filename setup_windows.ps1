$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "Python was not found. Install Python 3.11+ from https://www.python.org/downloads/windows/ and enable 'Add python.exe to PATH'." -ForegroundColor Red
    exit 1
}

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Creating the bot's private Python environment..."
    python -m venv .venv
}

$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
Write-Host "Installing/updating bot dependencies..."
& $python -m pip install --upgrade pip --disable-pip-version-check
& $python -m pip install -r requirements.txt --disable-pip-version-check

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example. Open .env and paste your Discord bot token into DISCORD_TOKEN." -ForegroundColor Yellow
} else {
    Write-Host ".env already exists; leaving it unchanged."
}

Write-Host "Setup complete. Double-click start_bot.bat after adding your token." -ForegroundColor Green
Read-Host "Press Enter to close"
