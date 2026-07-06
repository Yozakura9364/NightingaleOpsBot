param(
  [string]$TaskName = 'NightingaleSilence NS Ops Runner',
  [switch]$StartNow
)

$ErrorActionPreference = 'Stop'

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
$scriptPath = Join-Path $projectRoot 'runner\start-runner.ps1'
$configPath = Join-Path $projectRoot '.local\runner.local.json'

if (-not (Test-Path -LiteralPath $configPath)) {
  throw "Missing local config: $configPath"
}

$powershellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$action = New-ScheduledTaskAction -Execute $powershellExe -Argument ('-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $scriptPath)
$trigger = New-ScheduledTaskTrigger -AtLogOn
$principalUser = if ($env:USERDOMAIN) { "$env:USERDOMAIN\$env:USERNAME" } else { $env:USERNAME }
$principal = New-ScheduledTaskPrincipal -UserId $principalUser -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null

if ($StartNow) {
  Start-ScheduledTask -TaskName $TaskName
}

Write-Host "Registered scheduled task: $TaskName"
