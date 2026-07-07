param(
  [string]$Name = 'xproxy-tunnel'
)

$ErrorActionPreference = 'Stop'

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
$safeName = $Name -replace '[^A-Za-z0-9_.-]', '-'
$pidPath = Join-Path $projectRoot ".local\$safeName.pid"

if (-not (Test-Path -LiteralPath $pidPath)) {
  Write-Host "X proxy tunnel '$safeName': stopped"
  exit 0
}

$pidText = (Get-Content -LiteralPath $pidPath -Raw).Trim()
if ($pidText -match '^\d+$') {
  $process = Get-Process -Id ([int]$pidText) -ErrorAction SilentlyContinue
  if ($process) {
    Write-Host "X proxy tunnel '$safeName': running. PID: $pidText"
    exit 0
  }
}

Write-Host "X proxy tunnel '$safeName': stale PID file"
