# רושם משימה מתוזמנת ב-Windows שתריץ את הדוח כל בוקר ב-11:00.
# הרצה (פעם אחת): לחיצה ימנית על הקובץ -> Run with PowerShell
#   או:  powershell -ExecutionPolicy Bypass -File setup_schedule.ps1

$ErrorActionPreference = "Stop"

$taskName = "WorldCup2026DailyReport"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$runBat = Join-Path $scriptDir "run.bat"

if (-not (Test-Path $runBat)) {
    Write-Error "לא נמצא run.bat בנתיב $runBat"
    exit 1
}

$action  = New-ScheduledTaskAction -Execute $runBat -WorkingDirectory $scriptDir
$trigger = New-ScheduledTaskTrigger -Daily -At 11:00AM
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

# אם המשימה כבר קיימת — מסירים ויוצרים מחדש
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "דוח יומי מונדיאל 2026 + פנטזי" | Out-Null

Write-Host "נרשמה משימה '$taskName' שתרוץ כל יום ב-11:00." -ForegroundColor Green
Write-Host "להרצה מיידית לבדיקה:  Start-ScheduledTask -TaskName $taskName"
Write-Host "להסרה:                Unregister-ScheduledTask -TaskName $taskName -Confirm:`$false"
