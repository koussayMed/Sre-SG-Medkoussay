# Pannes simulees en production, pour la demo (Windows PowerShell 5.1 ou 7) :
#   powershell -ExecutionPolicy Bypass -File .\scripts\panne.ps1 -Action etat -Suivre
#   powershell -ExecutionPolicy Bypass -File .\scripts\panne.ps1 -Action api-panne
#
# Actions :
#   etat        version, replica qui repond, et source de la meteo de chaque ville (cache, live, stale...)
#               -Suivre : repete toutes les 5 s (Ctrl+C pour arreter)
#   api-panne   l'API meteo devient injoignable pour le backend (adresses .invalid, qui ne resolvent jamais)
#               et le cache meteo expire tout de suite : on voit le mode degrade sans attendre 10 min
#   api-retour  retour aux adresses et au cache normaux
#   bd-panne    arret du serveur PostgreSQL (1 a 3 min)
#   bd-retour   redemarrage de PostgreSQL ; le pool de connexions du backend se reconnecte tout seul
#
# Chaque changement de variable cree une nouvelle revision du backend (environ 1 min, sans coupure).
# IMPORTANT : toujours finir par api-retour / bd-retour. Le pipeline de deploiement ne change que l'image :
# une panne laissee active le resterait apres un deploiement.

param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("etat", "api-panne", "api-retour", "bd-panne", "bd-retour")]
    [string]$Action,
    [switch]$Suivre
)

$ErrorActionPreference = "Stop"
$Rg      = "rg-sre-meteo"
$Backend = "meteo-backend"
$Pg      = "pg-sre-meteo-koussay"
$Visitor = "00000000-0000-4000-8000-000000000001"   # visiteur du test de charge, avec 3 villes

function Invoke-AzCli {
    $out = & az @args
    if ($LASTEXITCODE -ne 0) { throw "ECHEC : az $($args -join ' ')" }
    return $out
}

function Get-Json([string]$Url, [hashtable]$Headers = @{}) {
    # Decodage UTF-8 explicite : Windows PowerShell 5.1 se trompe sur les accents sinon
    $r = Invoke-WebRequest -Uri $Url -Headers $Headers -UseBasicParsing -TimeoutSec 20
    return [System.Text.Encoding]::UTF8.GetString($r.RawContentStream.ToArray()) | ConvertFrom-Json
}

function Show-State([string]$AppUrl) {
    $time = Get-Date -Format "HH:mm:ss"
    try {
        $info = Get-Json "$AppUrl/api/info"
        Write-Host "$time  version $($info.version)  replica $($info.instance.replica)" -ForegroundColor Cyan
    } catch {
        Write-Host "$time  /api/info en erreur : $($_.Exception.Message)" -ForegroundColor Red
    }
    try {
        # Pas de @(...) ici : PowerShell 5.1 renvoie la liste JSON comme un seul objet, @() l'emboiterait
        $cities = Get-Json "$AppUrl/api/weather" @{ "X-Visitor-Id" = $Visitor }
        foreach ($city in $cities) {
            if ($city.weather) {
                Write-Host ("    {0,-12} {1,6} C   source = {2}" -f $city.name, $city.weather.temperature_c, $city.weather.source)
            } else {
                Write-Host ("    {0,-12} pas de meteo : {1}" -f $city.name, $city.error) -ForegroundColor Yellow
            }
        }
    } catch {
        Write-Host "    /api/weather en erreur : $($_.Exception.Message)" -ForegroundColor Red
    }
}

$fqdn = Invoke-AzCli containerapp show -n meteo-frontend -g $Rg --query properties.configuration.ingress.fqdn -o tsv --only-show-errors
$AppUrl = "https://$fqdn"

switch ($Action) {
    "etat" {
        do {
            Show-State $AppUrl
            if ($Suivre) { Start-Sleep -Seconds 5 }
        } while ($Suivre)
    }
    "api-panne" {
        Write-Host "Panne de l'API meteo : nouvelle revision du backend (environ 1 min)..." -ForegroundColor Yellow
        Invoke-AzCli containerapp update -n $Backend -g $Rg --only-show-errors -o none --set-env-vars `
            "FORECAST_API_URL=https://api.open-meteo.invalid/v1/forecast" `
            "GEOCODING_API_URL=https://geocoding-api.open-meteo.invalid/v1/search" `
            "WEATHER_CACHE_TTL_S=0"
        Write-Host "Attendu : villes affichees avec la derniere valeur connue (source = stale), suggestions deja"
        Write-Host "connues toujours servies, nouvelle ville impossible a ajouter (503), circuit breaker ouvert."
    }
    "api-retour" {
        Write-Host "Retour de l'API meteo : nouvelle revision du backend (environ 1 min)..." -ForegroundColor Green
        Invoke-AzCli containerapp update -n $Backend -g $Rg --only-show-errors -o none `
            --remove-env-vars FORECAST_API_URL GEOCODING_API_URL WEATHER_CACHE_TTL_S
        Write-Host "Attendu : le circuit se referme au premier appel reussi, source = live puis cache."
    }
    "bd-panne" {
        Write-Host "Arret de PostgreSQL (1 a 3 min)..." -ForegroundColor Yellow
        Invoke-AzCli postgres flexible-server stop -g $Rg -n $Pg --only-show-errors -o none
        Write-Host "Attendu : la page s'affiche, la meteo 'autour de vous' marche encore (pas de base),"
        Write-Host "la liste des villes repond 503 rapidement (pas de blocage), alerte 5xx sous 5 min."
    }
    "bd-retour" {
        Write-Host "Redemarrage de PostgreSQL (1 a 3 min)..." -ForegroundColor Green
        Invoke-AzCli postgres flexible-server start -g $Rg -n $Pg --only-show-errors -o none
        Write-Host "Attendu : retour a la normale sans redemarrer le backend (le pool se reconnecte)."
    }
}
