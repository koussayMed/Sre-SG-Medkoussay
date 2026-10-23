# Scan de sécurité local (Windows / PowerShell) avec Trivy, lancé dans un conteneur.
# Prérequis : `docker compose build` déjà fait. Usage : .\scripts\scan.ps1
# Fichier enregistré en UTF-8 avec BOM : sinon Windows PowerShell 5.1 affiche mal les accents.
#
# Version épinglée : la 0.69.4 (et les images Docker Hub 0.69.5/0.69.6) ont été compromises en mars 2026
# (CVE-2026-33634). N'utilisez jamais `latest` sans vérifier la version sur la page des releases.
$TrivyImage = "aquasec/trivy:0.69.3"
$Root = (Resolve-Path "$PSScriptRoot\..").Path
$Common = @("run", "--rm",
    "-v", "/var/run/docker.sock:/var/run/docker.sock",
    "-v", "trivy-cache:/root/.cache/",
    "-v", "${Root}:/src:ro",
    $TrivyImage)

$failed = $false
foreach ($image in @("sre-meteo-backend", "sre-meteo-frontend")) {
    Write-Host "`n=== Vulnérabilités : $image (bloquant : HIGH/CRITICAL avec correctif) ===" -ForegroundColor Cyan
    docker @Common image --scanners vuln --severity HIGH,CRITICAL --ignore-unfixed `
        --ignorefile /src/.trivyignore --exit-code 1 $image
    if ($LASTEXITCODE -ne 0) { $failed = $true }
}

Write-Host "`n=== Mauvaises configurations : Dockerfiles ===" -ForegroundColor Cyan
docker @Common config --severity HIGH,CRITICAL --exit-code 1 /src
if ($LASTEXITCODE -ne 0) { $failed = $true }

Write-Host "`n=== Secrets commités par erreur ===" -ForegroundColor Cyan
docker @Common fs --scanners secret --skip-dirs /src/backend/.venv --skip-files /src/.env --exit-code 1 /src
if ($LASTEXITCODE -ne 0) { $failed = $true }

if ($failed) { Write-Host "`nÉCHEC : voir les résultats ci-dessus." -ForegroundColor Red; exit 1 }
Write-Host "`nOK : aucune vulnérabilité bloquante." -ForegroundColor Green
