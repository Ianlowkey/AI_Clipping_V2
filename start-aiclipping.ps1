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

$sitePackages = Join-Path $projectRoot ".venv\Lib\site-packages"
$cudaRuntimeBin = Join-Path $sitePackages "nvidia\cuda_runtime\bin"
$cuBlasBin = Join-Path $sitePackages "nvidia\cublas\bin"
$cuDnnBin = Join-Path $sitePackages "ctranslate2"
$cudaDllChecks = @(
  @{ Name = "CUDA runtime"; Path = Join-Path $cudaRuntimeBin "cudart64_12.dll" },
  @{ Name = "cuBLAS"; Path = Join-Path $cuBlasBin "cublas64_12.dll" },
  @{ Name = "cuBLAS Lt"; Path = Join-Path $cuBlasBin "cublasLt64_12.dll" },
  @{ Name = "cuDNN"; Path = Join-Path $cuDnnBin "cudnn64_9.dll" }
)
foreach ($check in $cudaDllChecks) {
  Write-Host ("CUDA DLL {0}: {1}" -f $check.Name, $(if (Test-Path -LiteralPath $check.Path) { "found" } else { "MISSING" }))
}

# Set PATH only while creating the backend process; restore it before launching the frontend.
$originalPath = $env:PATH
$backendDefaults = @{
  DISABLE_SQLALCHEMY_CEXT_RUNTIME = "1"
  AICLIPPING_WHISPER_DEVICE = "cpu"
  AICLIPPING_WHISPER_COMPUTE_TYPE = "int8"
}
$originalBackendEnvironment = @{}
try {
  foreach ($name in $backendDefaults.Keys) {
    $current = [Environment]::GetEnvironmentVariable($name, "Process")
    $originalBackendEnvironment[$name] = $current
    if ($null -eq $current) {
      [Environment]::SetEnvironmentVariable($name, $backendDefaults[$name], "Process")
    }
  }
  $env:PATH = (@($cudaRuntimeBin, $cuBlasBin) + @($originalPath)) -join [IO.Path]::PathSeparator
  $backend = Start-Process -FilePath $python `
    -ArgumentList @("-m", "uvicorn", "app.main:app", "--app-dir", "backend", "--host", "127.0.0.1", "--port", "8765", "--reload") `
    -WorkingDirectory $projectRoot -PassThru
}
finally {
  $env:PATH = $originalPath
  foreach ($name in $backendDefaults.Keys) {
    [Environment]::SetEnvironmentVariable($name, $originalBackendEnvironment[$name], "Process")
  }
}

$originalGeminiKey = [Environment]::GetEnvironmentVariable("AICLIPPING_GEMINI_API_KEY", "Process")
try {
  # The key is needed only by the backend process; do not inherit it into the frontend process.
  [Environment]::SetEnvironmentVariable("AICLIPPING_GEMINI_API_KEY", $null, "Process")
  $frontend = Start-Process -FilePath "cmd.exe" `
    -ArgumentList @("/k", "npm.cmd run dev -- --host 127.0.0.1") `
    -WorkingDirectory $projectRoot -PassThru
}
finally {
  [Environment]::SetEnvironmentVariable("AICLIPPING_GEMINI_API_KEY", $originalGeminiKey, "Process")
}

Write-Host "AIClipping Local Browser Mode started."
Write-Host ("Gemini API key in backend environment: {0}" -f $(if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable("AICLIPPING_GEMINI_API_KEY", "Process"))) { "not configured" } else { "configured" }))
Write-Host "Backend PID: $($backend.Id)  API: http://127.0.0.1:8765/api/health  Docs: http://127.0.0.1:8765/docs"
Write-Host "Frontend PID: $($frontend.Id)  App: http://127.0.0.1:1420"
Write-Host "Use Ctrl+C in each service window to stop it."
