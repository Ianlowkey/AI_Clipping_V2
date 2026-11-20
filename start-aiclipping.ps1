$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
  throw "Python environment not found at $python. Create it with: py -3.12 -m venv .venv"
}

$pythonVersion = & $python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($LASTEXITCODE -ne 0 -or $pythonVersion.Trim() -ne "3.12") {
  throw "AIClipping requires the project .venv to use Python 3.12; found '$($pythonVersion.Trim())'. Recreate .venv with Python 3.12. No other Python installation was changed."
}

& $python -c "import uvicorn" 2>$null
if ($LASTEXITCODE -ne 0) {
  throw "Backend dependencies are missing. Install them with: .\.venv\Scripts\python.exe -m pip install -e '.[dev]' (from the backend folder)"
}

if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) {
  throw "npm.cmd was not found. Install Node.js and reopen PowerShell."
}

$backend = Start-Process -FilePath $python `
  -ArgumentList @("-m", "uvicorn", "app.main:app", "--app-dir", "backend", "--host", "127.0.0.1", "--port", "8765", "--reload") `
  -WorkingDirectory $projectRoot -PassThru

$frontend = Start-Process -FilePath "cmd.exe" `
  -ArgumentList @("/k", "npm.cmd run dev -- --host 127.0.0.1") `
  -WorkingDirectory $projectRoot -PassThru

Write-Host "AIClipping Local Browser Mode started."
Write-Host "Backend PID: $($backend.Id)  API: http://127.0.0.1:8765/api/health  Docs: http://127.0.0.1:8765/docs"
Write-Host "Frontend PID: $($frontend.Id)  App: http://127.0.0.1:1420"
Write-Host "Use Ctrl+C in each service window to stop it."
