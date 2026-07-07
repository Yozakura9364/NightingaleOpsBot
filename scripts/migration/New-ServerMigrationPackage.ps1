param(
  [string]$OutputRoot = "",
  [switch]$StopContainers
)

$ErrorActionPreference = "Stop"

$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[Console]::InputEncoding = $utf8NoBom
[Console]::OutputEncoding = $utf8NoBom
$OutputEncoding = $utf8NoBom

$scriptPath = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptPath "..\..")
$workspaceRoot = Resolve-Path (Join-Path $projectRoot "..")
$astrbotRoot = Join-Path $workspaceRoot "astrbot"
$astrbotData = Join-Path $astrbotRoot "data"
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"

if (-not $OutputRoot) {
  $OutputRoot = Join-Path $projectRoot ".local\migration"
}

$packageRoot = Join-Path $OutputRoot "nightingale-qqbot-migration-$timestamp"
$archivePath = "$packageRoot.zip"

function Copy-Tree {
  param(
    [Parameter(Mandatory = $true)][string]$Source,
    [Parameter(Mandatory = $true)][string]$Destination,
    [string[]]$ExcludeDirectoryNames = @(),
    [string[]]$ExcludeFileNames = @()
  )

  $sourcePath = Resolve-Path $Source
  New-Item -ItemType Directory -Force -Path $Destination | Out-Null

  Get-ChildItem -LiteralPath $sourcePath -Force | ForEach-Object {
    if ($_.PSIsContainer -and $ExcludeDirectoryNames -contains $_.Name) {
      return
    }
    if (-not $_.PSIsContainer -and $ExcludeFileNames -contains $_.Name) {
      return
    }

    $target = Join-Path $Destination $_.Name
    if ($_.PSIsContainer) {
      Copy-Tree -Source $_.FullName -Destination $target -ExcludeDirectoryNames $ExcludeDirectoryNames -ExcludeFileNames $ExcludeFileNames
    } else {
      Copy-Item -LiteralPath $_.FullName -Destination $target -Force
    }
  }
}

function Assert-Path {
  param([Parameter(Mandatory = $true)][string]$Path)
  if (-not (Test-Path -LiteralPath $Path)) {
    throw "Missing required path: $Path"
  }
}

function ConvertTo-SystemdEnvValue {
  param([AllowNull()][object]$Value)
  $text = [string]$Value
  $text = $text.Replace('\', '\\').Replace('"', '\"')
  return '"' + $text + '"'
}

Assert-Path $astrbotData
Assert-Path (Join-Path $projectRoot "runner\server.mjs")
Assert-Path (Join-Path $projectRoot ".local\runner.local.json")

$docker = Get-Command docker -ErrorAction SilentlyContinue
if (-not $docker) {
  throw "Docker CLI was not found. NapCat login state export requires docker cp."
}

if ($StopContainers) {
  Write-Host "Stopping local astrbot and napcat before export..."
  docker stop astrbot napcat | Out-Null
} else {
  Write-Warning "Exporting while containers are running. For the final migration package, rerun with -StopContainers."
}

try {
  New-Item -ItemType Directory -Force -Path $packageRoot | Out-Null

  $serverTemplateRoot = Join-Path $projectRoot "migration\server"
  Assert-Path $serverTemplateRoot

  Write-Host "Copying server templates..."
  Copy-Tree -Source $serverTemplateRoot -Destination (Join-Path $packageRoot "server")

  Write-Host "Copying AstrBot data..."
  $packageAstrbotRoot = Join-Path $packageRoot "astrbot"
  New-Item -ItemType Directory -Force -Path $packageAstrbotRoot | Out-Null
  Copy-Item -LiteralPath (Join-Path $serverTemplateRoot "docker-compose.yml") -Destination (Join-Path $packageAstrbotRoot "docker-compose.yml") -Force
  Copy-Tree -Source $astrbotData -Destination (Join-Path $packageAstrbotRoot "data") -ExcludeDirectoryNames @("__pycache__")

  Write-Host "Exporting NapCat QQ login state and config with docker cp..."
  New-Item -ItemType Directory -Force -Path (Join-Path $packageRoot "napcat") | Out-Null
  docker cp "napcat:/app/.config/QQ" (Join-Path $packageRoot "napcat\qq") | Out-Null
  docker cp "napcat:/app/napcat/config" (Join-Path $packageRoot "napcat\config") | Out-Null

  Write-Host "Copying NightingaleOpsBot source..."
  Copy-Tree `
    -Source $projectRoot `
    -Destination (Join-Path $packageRoot "NightingaleOpsBot") `
    -ExcludeDirectoryNames @(".git", "node_modules", ".local", "__pycache__")

  Write-Host "Copying runner local config as runner.env input material..."
  $packageOpsLocal = Join-Path $packageRoot "NightingaleOpsBot\.local"
  New-Item -ItemType Directory -Force -Path $packageOpsLocal | Out-Null
  Copy-Item -LiteralPath (Join-Path $projectRoot ".local\runner.local.json") -Destination (Join-Path $packageRoot "NightingaleOpsBot\.local\runner.local.json") -Force

  $runnerConfig = Get-Content -LiteralPath (Join-Path $projectRoot ".local\runner.local.json") -Raw -Encoding UTF8 | ConvertFrom-Json
  $runnerEnv = @(
    "NS_OPS_TOKEN=$(ConvertTo-SystemdEnvValue $runnerConfig.NS_OPS_TOKEN)",
    'NS_OPS_HOST="127.0.0.1"',
    'NS_OPS_PORT="18766"',
    'NS_OPS_PROJECT_ROOT="/opt/nightingale/NightingaleOpsBot"',
    'NS_OPS_V2_ROOT="/opt/nightingale/NightingaleSilenceWebV2"',
    'NS_OPS_ASTRBOT_ROOT="/opt/nightingale/astrbot"',
    'NS_OPS_LOG_DIR="/opt/nightingale/NightingaleOpsBot/.local/logs"',
    'NS_OPS_FILE_WRITE_ROOT="/opt/nightingale/NightingaleOpsBot/.local/inbox"',
    'NS_OPS_RISINGSTONE_QR_DIR="/opt/nightingale/NightingaleOpsBot/.local/risingstone-qr"'
  )
  [System.IO.File]::WriteAllLines((Join-Path $packageOpsLocal "runner.env"), $runnerEnv, $utf8NoBom)

  $manifest = [ordered]@{
    createdAt = (Get-Date).ToString("o")
    sourceHost = $env:COMPUTERNAME
    containsSensitiveData = $true
    includes = @(
      "astrbot/data",
      "napcat/qq",
      "napcat/config",
      "NightingaleOpsBot",
      "NightingaleOpsBot/.local/runner.local.json",
      "server templates"
    )
    warnings = @(
      "Contains QQ login state, cookies, tokens, and Stone House encrypted credentials.",
      "Do not commit, upload publicly, or place under a web root.",
      "Keep risingstone.sqlite3 and secret.key together."
    )
  }
  $manifest | ConvertTo-Json -Depth 5 | Set-Content -Path (Join-Path $packageRoot "MIGRATION-MANIFEST.json") -Encoding UTF8

  Write-Host "Creating archive..."
  if (Test-Path -LiteralPath $archivePath) {
    Remove-Item -LiteralPath $archivePath -Force
  }
  Compress-Archive -Path (Join-Path $packageRoot "*") -DestinationPath $archivePath -Force

  Write-Host "Migration package created:"
  Write-Host $packageRoot
  Write-Host $archivePath
  Write-Host "Sensitive package. Do not commit or share publicly."
} finally {
  if ($StopContainers) {
    Write-Host "Restarting local astrbot and napcat..."
    docker start astrbot napcat | Out-Null
  }
}
