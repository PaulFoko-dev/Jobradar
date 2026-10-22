# Installe une tâche planifiée Windows qui actualise chaque jour les recherches « quotidiennes ».
# Si le PC est éteint à l'heure prévue, la tâche s'exécute dès que possible après le démarrage.
# Usage (PowerShell) :  .\install_daily.ps1            # 08:00 par défaut
#                       .\install_daily.ps1 -At 07:30
param([string]$At = "08:00")
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $dir ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { Write-Error "Lance d'abord run.bat une fois pour créer l'environnement Python."; exit 1 }
$action = New-ScheduledTaskAction -Execute $python -Argument "-m jobradar.cli refresh" -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 3)
Register-ScheduledTask -TaskName "JobRadar" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Host "Tâche « JobRadar » planifiée chaque jour à $At. Désinstaller : Unregister-ScheduledTask -TaskName JobRadar"
