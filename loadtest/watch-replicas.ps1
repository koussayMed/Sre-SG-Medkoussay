# Suit le nombre de replicas pendant le test de charge (a lancer dans un 2e terminal) :
#   powershell -ExecutionPolicy Bypass -File .\loadtest\watch-replicas.ps1
# Ctrl+C pour arreter. Chaque ligne : heure, replicas backend et frontend en cours d'execution.
# Les replicas en trop sont retires environ 5 minutes apres la fin de la charge (delai de Container Apps).

param([int]$IntervalSeconds = 10)

$Rg = "rg-sre-meteo"

function Get-ReplicaCount([string]$App) {
    # --query "[].name" puis comptage en PowerShell : length() contient des parentheses, mal passees par az.cmd
    $names = & az containerapp replica list -n $App -g $Rg --query "[].name" -o tsv --only-show-errors 2>$null
    if ($LASTEXITCODE -ne 0) { return "?" }
    return @($names | Where-Object { $_ }).Count
}

Write-Host "Heure      backend  frontend" -ForegroundColor Cyan
while ($true) {
    $backend  = Get-ReplicaCount "meteo-backend"
    $frontend = Get-ReplicaCount "meteo-frontend"
    $bar = if ($backend -is [int]) { "#" * $backend } else { "" }
    Write-Host ("{0}  {1,7}  {2,8}  {3}" -f (Get-Date -Format "HH:mm:ss"), $backend, $frontend, $bar)
    Start-Sleep -Seconds $IntervalSeconds
}
