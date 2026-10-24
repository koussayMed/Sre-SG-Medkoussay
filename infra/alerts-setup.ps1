# Alertes Azure Monitor, a lancer une fois (relancable) :
#   powershell -ExecutionPolicy Bypass -File .\infra\alerts-setup.ps1 -Email vous@exemple.com
#
# Quatre alertes sur des metriques (evaluees chaque minute par Azure, sans code dans l'application),
# toutes envoyees par email via un groupe d'actions :
#   1. Erreurs 5xx vues par les utilisateurs (frontend)        > 10 en 5 min      severite 1
#   2. Backend au plafond de replicas (capacite)               8 pendant 15 min   severite 2
#   3. Redemarrages de conteneurs backend (plantages)          > 3 en 15 min      severite 2
#   4. Connexions PostgreSQL proches de la limite B1ms (35)    >= 30 en 5 min     severite 2
# Cout : quelques centimes par mois (metric alerts), emails gratuits.

param([Parameter(Mandatory = $true)][string]$Email)

$ErrorActionPreference = "Stop"
$Rg = "rg-sre-meteo"
$Pg = "pg-sre-meteo-koussay"
$ActionGroup = "ag-sre-meteo"

function Invoke-AzCli {
    $out = & az @args
    if ($LASTEXITCODE -ne 0) { throw "ECHEC : az $($args -join ' ')" }
    return $out
}

function Get-AzOrNull {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $out = & az @args 2>$null
    $code = $LASTEXITCODE
    $ErrorActionPreference = $previous
    if ($code -ne 0) { return $null }
    return $out
}

function New-MetricAlert {
    param([string]$Name, [string]$Scope, [string]$Condition, [string]$Window, [string]$Frequency,
          [int]$Severity, [string]$Description)
    if (Get-AzOrNull monitor metrics alert show -n $Name -g $Rg --query id -o tsv) {
        Write-Host "  $Name : deja configuree"
        return
    }
    Invoke-AzCli monitor metrics alert create -n $Name -g $Rg --scopes $Scope --condition $Condition `
        --window-size $Window --evaluation-frequency $Frequency --severity $Severity `
        --description $Description --action $ActionGroupId --only-show-errors -o none
    Write-Host "  $Name : creee"
}

Write-Host "== 1/3 Fournisseur Azure Monitor" -ForegroundColor Cyan
Invoke-AzCli provider register --namespace Microsoft.Insights --wait | Out-Null

Write-Host "== 2/3 Groupe d'actions (email)" -ForegroundColor Cyan
if (-not (Get-AzOrNull monitor action-group show -n $ActionGroup -g $Rg --query id -o tsv)) {
    Invoke-AzCli monitor action-group create -n $ActionGroup -g $Rg --short-name sremeteo `
        --action email astreinte $Email --only-show-errors -o none
}
$ActionGroupId = Invoke-AzCli monitor action-group show -n $ActionGroup -g $Rg --query id -o tsv

Write-Host "== 3/3 Alertes" -ForegroundColor Cyan
$FrontendId = Invoke-AzCli containerapp show -n meteo-frontend -g $Rg --query id -o tsv --only-show-errors
$BackendId  = Invoke-AzCli containerapp show -n meteo-backend -g $Rg --query id -o tsv --only-show-errors
$PgId       = Invoke-AzCli postgres flexible-server show -n $Pg -g $Rg --query id -o tsv --only-show-errors

New-MetricAlert -Name "meteo-5xx-utilisateurs" -Scope $FrontendId -Severity 1 -Window 5m -Frequency 1m `
    -Condition "total Requests > 10 where statusCodeCategory includes 5xx" `
    -Description "Plus de 10 reponses 5xx en 5 minutes sur l'URL publique"
New-MetricAlert -Name "meteo-backend-plafond-replicas" -Scope $BackendId -Severity 2 -Window 15m -Frequency 5m `
    -Condition "min Replicas >= 8" `
    -Description "Backend au maximum de replicas pendant 15 minutes : capacite a revoir, cote base de donnees"
New-MetricAlert -Name "meteo-backend-redemarrages" -Scope $BackendId -Severity 2 -Window 15m -Frequency 5m `
    -Condition "total RestartCount > 3" `
    -Description "Conteneurs backend qui redemarrent : plantage ou sonde en echec"
New-MetricAlert -Name "meteo-postgres-connexions" -Scope $PgId -Severity 2 -Window 5m -Frequency 1m `
    -Condition "max active_connections >= 30" `
    -Description "Connexions PostgreSQL proches de la limite de l'offre B1ms, 35"

Write-Host ""
Write-Host "Termine. Alertes visibles dans le portail : Monitor > Alerts > Alert rules." -ForegroundColor Green
Write-Host "Un email de confirmation d'Azure arrive a $Email (ajout au groupe d'actions)."
