# Installe ou met a jour la tache planifiee JobRadar sous Windows.
# Usage : .\install_daily.ps1 -At 08:00
[CmdletBinding()]
param(
    [Parameter()]
    [ValidatePattern('^(?:[01][0-9]|2[0-3]):[0-5][0-9]$')]
    [string]$At = '08:00'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

try {
    if (-not $PSScriptRoot) {
        throw 'Impossible de determiner le dossier du projet.'
    }

    $projectDir = [System.IO.Path]::GetFullPath($PSScriptRoot)
    $python = Join-Path $projectDir '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw 'Lancez d’abord run.bat pour creer l’environnement Python.'
    }

    # L'import doit etre resolu depuis le projet, meme si le script est appele
    # depuis un autre repertoire de travail.
    Push-Location -LiteralPath $projectDir
    try {
        & $python -c 'import jobradar' 2>$null
        if ($LASTEXITCODE -ne 0) {
            throw 'L’installation JobRadar est incomplete ; relancez run.bat.'
        }
    }
    finally {
        Pop-Location
    }

    Get-Command Register-ScheduledTask -ErrorAction Stop | Out-Null
    $action = New-ScheduledTaskAction `
        -Execute $python `
        -Argument '-m jobradar.cli refresh' `
        -WorkingDirectory $projectDir
    $trigger = New-ScheduledTaskTrigger -Daily -At $At
    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Hours 3)

    # -Force rend les mises a jour de l'heure et du chemin idempotentes.
    Register-ScheduledTask `
        -TaskName 'JobRadar' `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Description 'Actualisation quotidienne des recherches JobRadar' `
        -Force | Out-Null

    Write-Host "Tache 'JobRadar' planifiee chaque jour a $At."
    Write-Host "Desinstallation : Unregister-ScheduledTask -TaskName 'JobRadar' -Confirm:`$false"
}
catch {
    Write-Error "Installation de la tache JobRadar impossible : $($_.Exception.Message)"
    exit 1
}
