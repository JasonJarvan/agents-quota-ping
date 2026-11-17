# Register daily 07:00, 12:02, 17:04 scheduled triggers in Windows Task Scheduler.
# Run in PowerShell after installing `quota-ping` (`pipx install .` or `uv tool install .`).

$ErrorActionPreference = "Stop"

$Exe = (Get-Command quota-ping -ErrorAction Stop).Source
$TaskName = "AgentsQuotaPing"

$Action = New-ScheduledTaskAction -Execute $Exe
$Triggers = @(
    (New-ScheduledTaskTrigger -Daily -At "07:00"),
    (New-ScheduledTaskTrigger -Daily -At "12:02"),
    (New-ScheduledTaskTrigger -Daily -At "17:04")
)
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Triggers `
    -Settings $Settings `
    -Description "Warm up 5-hour quota windows for Claude Code, Codex, and Antigravity." `
    -Force

Write-Host "Registered scheduled task '$TaskName' -> $Exe"
