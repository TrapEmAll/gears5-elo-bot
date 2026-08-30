$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

Write-Host "Stopping the running Gears 5 Elo Bot..."
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -like "*$projectRoot*bot.py*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Write-Host "Downloading the latest version from GitHub..."
git pull --ff-only origin main
if ($LASTEXITCODE -ne 0) {
    Write-Host "Update stopped. Git could not fast-forward this folder. Check for local changes." -ForegroundColor Red
    Read-Host "Press Enter to close"
    exit 1
}

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Bot environment not found; running first-time setup..."
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $projectRoot "setup_windows.ps1")
} else {
    Write-Host "Updating dependencies..."
    & (Join-Path $projectRoot ".venv\Scripts\python.exe") -m pip install -r requirements.txt --disable-pip-version-check
}

if (-not (Test-Path ".env")) {
    Write-Host "No .env file found. Run setup_windows.ps1 and add your Discord token first." -ForegroundColor Red
    Read-Host "Press Enter to close"
    exit 1
}

Write-Host "Starting the updated bot..." -ForegroundColor Green
Start-Process -FilePath (Join-Path $projectRoot "start_bot.bat") -WorkingDirectory $projectRoot
Write-Host "Update complete. The bot is running in a new window." -ForegroundColor Green
Start-Sleep -Seconds 2
