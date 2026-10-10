# TTS App Launcher Script
$VENV_PYTHON = ".\venv_311\Scripts\python.exe"
$APP_SCRIPT = ".\tts_app.py"

if (Test-Path $VENV_PYTHON) {
    Write-Host "--- Launching Adits Voice Studio ---" -ForegroundColor Green
    & $VENV_PYTHON $APP_SCRIPT
} else {
    Write-Host "Error: Virtual environment not found in .\venv_311\" -ForegroundColor Red
    Write-Host "Please ensure you have created the venv correctly." -ForegroundColor Yellow
    Pause
}