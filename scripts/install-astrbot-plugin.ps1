param(
  [string]$AstrBotRoot = 'H:\NightingaleSilenceWeb\astrbot'
)

$ErrorActionPreference = 'Stop'

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
$sourceBase = Resolve-Path (Join-Path $projectRoot 'astrbot-plugin')
$targetBase = Join-Path $AstrBotRoot 'data\plugins'

if (-not (Test-Path -LiteralPath $AstrBotRoot)) {
  throw "AstrBot root not found: $AstrBotRoot"
}

Get-ChildItem -LiteralPath $sourceBase -Directory |
  Where-Object { $_.Name -like 'astrbot_plugin_*' } |
  ForEach-Object {
    $sourceRoot = $_
    $targetRoot = Join-Path $targetBase $_.Name
    New-Item -ItemType Directory -Force -Path $targetRoot | Out-Null

    Get-ChildItem -LiteralPath $sourceRoot.FullName -Recurse -File |
      Where-Object { $_.FullName -notmatch '\\__pycache__\\' -and $_.FullName -notmatch '\\.local\\' } |
      ForEach-Object {
        $relativePath = $_.FullName.Substring($sourceRoot.FullName.Length).TrimStart('\', '/')
        $targetPath = Join-Path $targetRoot $relativePath
        $targetDir = Split-Path -Parent $targetPath
        New-Item -ItemType Directory -Force -Path $targetDir | Out-Null
        Copy-Item -LiteralPath $_.FullName -Destination $targetPath -Force
      }

    Write-Host "Installed $($sourceRoot.Name) to $targetRoot"
  }
