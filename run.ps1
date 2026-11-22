# Windows: one command from a fresh clone to the app in a browser:  .\run.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Write-Host "uv is required. Install it with:`n  powershell -ExecutionPolicy ByPass -c `"irm https://astral.sh/uv/install.ps1 | iex`"`nthen open a NEW terminal and run this again."; exit 1
}
$port = if ($env:PORT) { $env:PORT } else { "8000" }
uv sync
if (-not (Test-Path places.sqlite3)) { uv run distnsimilarity-import }
Start-Job { Start-Sleep 3; Start-Process "http://127.0.0.1:$using:port" } | Out-Null
uv run distnsimilarity-serve --data-dir . --port $port
