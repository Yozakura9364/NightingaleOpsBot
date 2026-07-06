param(
  [string]$AstrBotRoot = 'H:\NightingaleSilenceWeb\astrbot'
)

$ErrorActionPreference = 'Stop'

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
$sourceRoot = Resolve-Path (Join-Path $projectRoot 'astrbot-plugin\astrbot_plugin_ns_ops')
$targetRoot = Join-Path $AstrBotRoot 'data\plugins\astrbot_plugin_ns_ops'

if (-not (Test-Path -LiteralPath $AstrBotRoot)) {
  throw "AstrBot root not found: $AstrBotRoot"
}

New-Item -ItemType Directory -Force -Path $targetRoot | Out-Null

Get-ChildItem -LiteralPath $sourceRoot -Recurse -File |
  Where-Object { $_.FullName -notmatch '\\__pycache__\\' } |
  ForEach-Object {
    $relativePath = $_.FullName.Substring($sourceRoot.Path.Length).TrimStart('\', '/')
    $targetPath = Join-Path $targetRoot $relativePath
    $targetDir = Split-Path -Parent $targetPath
    New-Item -ItemType Directory -Force -Path $targetDir | Out-Null
    Copy-Item -LiteralPath $_.FullName -Destination $targetPath -Force
  }

Write-Host "Installed astrbot_plugin_ns_ops to $targetRoot"
