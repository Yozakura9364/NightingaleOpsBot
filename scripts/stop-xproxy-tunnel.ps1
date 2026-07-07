param(
  [string]$Name = 'xproxy-tunnel'
)

$ErrorActionPreference = 'Stop'

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
$safeName = $Name -replace '[^A-Za-z0-9_.-]', '-'
$pidPath = Join-Path $projectRoot ".local\$safeName.pid"

if (-not (Test-Path -LiteralPath $pidPath)) {
  Write-Host "X proxy tunnel '$safeName' is not running."
  exit 0
}

$pidText = (Get-Content -LiteralPath $pidPath -Raw).Trim()
if ($pidText -match '^\d+$') {
  Stop-Process -Id ([int]$pidText) -ErrorAction SilentlyContinue
}

Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
Write-Host "Stopped X proxy tunnel '$safeName'."
