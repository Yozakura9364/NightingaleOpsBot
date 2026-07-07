param(
  [string]$Server = 'root@100.67.17.31',
  [int]$LocalProxyPort = 7890,
  [int]$RemoteTunnelPort = 17890,
  [string]$RemoteBindAddress = '127.0.0.1',
  [string]$Name = 'xproxy-tunnel'
)

$ErrorActionPreference = 'Stop'

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
$localDir = Join-Path $projectRoot '.local'
New-Item -ItemType Directory -Force -Path $localDir | Out-Null

$safeName = $Name -replace '[^A-Za-z0-9_.-]', '-'
$pidPath = Join-Path $localDir "$safeName.pid"
$logPath = Join-Path $localDir "$safeName.log"
$errPath = Join-Path $localDir "$safeName.err.log"

function Test-ProcessAlive([int]$ProcessId) {
  try {
    $process = Get-Process -Id $ProcessId -ErrorAction Stop
    return $null -ne $process
  } catch {
    return $false
  }
}

if (Test-Path -LiteralPath $pidPath) {
  $oldPidText = (Get-Content -LiteralPath $pidPath -Raw).Trim()
  if ($oldPidText -match '^\d+$' -and (Test-ProcessAlive ([int]$oldPidText))) {
    Write-Host "X proxy tunnel is already running. PID: $oldPidText"
    exit 0
  }
  Remove-Item -LiteralPath $pidPath -Force
}

$proxyOpen = Test-NetConnection -ComputerName '127.0.0.1' -Port $LocalProxyPort -InformationLevel Quiet
if (-not $proxyOpen) {
  throw "Local proxy 127.0.0.1:$LocalProxyPort is not reachable. Start your proxy client first."
}

$ssh = (Get-Command ssh -ErrorAction Stop).Source
$arguments = @(
  '-N',
  '-T',
  '-o', 'ExitOnForwardFailure=yes',
  '-o', 'ServerAliveInterval=30',
  '-o', 'ServerAliveCountMax=3',
  '-R', "$RemoteBindAddress`:$RemoteTunnelPort`:127.0.0.1:$LocalProxyPort",
  $Server
)

"$(Get-Date -Format s) starting tunnel: remote $RemoteBindAddress`:$RemoteTunnelPort -> local 127.0.0.1:$LocalProxyPort" |
  Set-Content -LiteralPath $logPath -Encoding UTF8

$process = Start-Process -FilePath $ssh `
  -ArgumentList $arguments `
  -WindowStyle Hidden `
  -PassThru `
  -RedirectStandardOutput $logPath `
  -RedirectStandardError $errPath

$process.Id | Set-Content -LiteralPath $pidPath -Encoding ASCII
Start-Sleep -Seconds 2

if (-not (Test-ProcessAlive $process.Id)) {
  $log = if (Test-Path -LiteralPath $logPath) { Get-Content -LiteralPath $logPath -Raw } else { '' }
  $err = if (Test-Path -LiteralPath $errPath) { Get-Content -LiteralPath $errPath -Raw } else { '' }
  Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
  throw "X proxy tunnel exited immediately. Log: $log $err"
}

Write-Host "Started X proxy tunnel '$safeName'. PID: $($process.Id)"
Write-Host "Remote: $RemoteBindAddress`:$RemoteTunnelPort -> local 127.0.0.1:$LocalProxyPort"
Write-Host "Log: $logPath"
