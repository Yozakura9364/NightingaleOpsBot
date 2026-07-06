$ErrorActionPreference = 'Stop'

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Resolve-Path (Join-Path $scriptRoot '..')
$configPath = Join-Path $projectRoot '.local\runner.local.json'
$logDir = Join-Path $projectRoot '.local\logs'
$stdoutLog = Join-Path $logDir 'runner.stdout.log'
$stderrLog = Join-Path $logDir 'runner.stderr.log'

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

if (-not (Test-Path -LiteralPath $configPath)) {
  throw "Missing local config: $configPath"
}

$config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json

if (-not $config.NS_OPS_TOKEN) {
  throw 'runner.local.json must define NS_OPS_TOKEN.'
}

foreach ($property in $config.PSObject.Properties) {
  if ($property.Name -like 'NS_OPS_*' -and $null -ne $property.Value -and $property.Value -ne '') {
    Set-Item -Path "Env:$($property.Name)" -Value ([string]$property.Value)
  }
}

Set-Location -LiteralPath $projectRoot

$node = (Get-Command node -ErrorAction Stop).Source
$server = Join-Path $scriptRoot 'server.mjs'

& $node $server 1>> $stdoutLog 2>> $stderrLog
exit $LASTEXITCODE
