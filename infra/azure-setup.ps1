# Creation de l'infrastructure Azure, depuis le poste : .\infra\azure-setup.ps1
# Relancable sans risque : chaque etape reprend ce qui existe deja.
# Prerequis : Azure CLI (az version), `az login`, Docker demarre.
#
#   Navigateur --HTTPS--> meteo-frontend (Container App, ingress externe, nginx)
#                              --HTTPS interne--> meteo-backend (Container App, ingress interne, Flask)
#                                                      --TLS--> PostgreSQL Flexible Server (manage)
#   Images dans Azure Container Registry, tirees par une identite managee (aucun mot de passe de registre).
#   Logs des conteneurs dans Log Analytics (cree avec l'environnement Container Apps).
#
# COUT : PostgreSQL B1ms + ACR Basic + 2 apps avec 1 replica minimum = quelques dollars par jour.
# Apres le test : az group delete -n rg-sre-meteo --yes --no-wait
#
# Note Windows : az est un fichier .cmd ; on evite donc parentheses, | et & dans ses arguments.

$ErrorActionPreference = "Stop"

# --- Parametres --------------------------------------------------------------------------------
$Location = "francecentral"
$Suffix   = "koussay"                # ACR et PostgreSQL exigent un nom unique au monde
$Rg       = "rg-sre-meteo"
$Acr      = "acrsremeteo$Suffix"     # lettres et chiffres uniquement
$Pg       = "pg-sre-meteo-$Suffix"
$CaeEnv   = "cae-sre-meteo"
$Identity = "id-sre-meteo-apps"
$DbName   = "meteo"
$DbUser   = "meteoadmin"
# Mot de passe aleatoire (lettres et chiffres) : jamais ecrit dans le depot, stocke seulement en secret Azure.
# A chaque execution, il est regenere puis applique a la base ET au secret du backend.
$DbPassword = -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 28 | ForEach-Object { [char]$_ })

function Invoke-AzCli {
    # Lance az et arrete le script si la commande echoue (sans jamais afficher de secret)
    $out = & az @args
    if ($LASTEXITCODE -ne 0) {
        $safe = ($args -join ' ') -replace 'postgresql://\S+', 'postgresql://***' -replace '(--admin-password\s+)\S+', '$1***'
        throw "ECHEC : az $safe"
    }
    return $out
}

function Get-AzOrNull {
    # Lance az et renvoie $null si la commande echoue (ex. ressource absente), sans arreter le script.
    # Windows PowerShell 5.1 transforme stderr en erreur bloquante avec 2>$null si ErrorActionPreference=Stop.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $out = & az @args 2>$null
    $code = $LASTEXITCODE
    $ErrorActionPreference = $previous
    if ($code -ne 0) { return $null }
    return $out
}

function Deploy-ContainerApp {
    # Cree ou met a jour une Container App, avec reprise : une app restee "Failed" est supprimee puis
    # recreee, et une erreur passagere d'Azure (InternalServerError...) donne lieu a 2 nouveaux essais.
    param([string]$Name, [scriptblock]$Create, [scriptblock]$Update)
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        $state = Get-AzOrNull containerapp show -n $Name -g $Rg --query properties.provisioningState -o tsv
        if ($state -eq "Failed") {
            Write-Host "  $Name est en echec : suppression avant recreation" -ForegroundColor Yellow
            Invoke-AzCli containerapp delete -n $Name -g $Rg --yes -o none
            $state = $null
        }
        try {
            if ($state) { & $Update } else { & $Create }
            return
        } catch {
            if ($attempt -eq 3) { throw }
            Write-Host "  Tentative $attempt echouee, nouvel essai dans 30 s" -ForegroundColor Yellow
            Start-Sleep -Seconds 30
        }
    }
}

Write-Host "== 1/7 Fournisseurs et extension" -ForegroundColor Cyan
az extension add --name containerapp --upgrade --only-show-errors
foreach ($ns in "Microsoft.App", "Microsoft.OperationalInsights", "Microsoft.ContainerRegistry",
                "Microsoft.DBforPostgreSQL", "Microsoft.ManagedIdentity") {
    Invoke-AzCli provider register --namespace $ns --wait | Out-Null
}

Write-Host "== 2/7 Groupe de ressources, registre et identite managee" -ForegroundColor Cyan
Invoke-AzCli group create -n $Rg -l $Location -o none
if (-not (Get-AzOrNull acr show -n $Acr -g $Rg --query id -o tsv)) {
    Invoke-AzCli acr create -n $Acr -g $Rg --sku Basic --admin-enabled false -o none
}
if (-not (Get-AzOrNull identity show -n $Identity -g $Rg --query id -o tsv)) {
    Invoke-AzCli identity create -n $Identity -g $Rg -o none
}
$IdentityId  = Invoke-AzCli identity show -n $Identity -g $Rg --query id -o tsv
$PrincipalId = Invoke-AzCli identity show -n $Identity -g $Rg --query principalId -o tsv
$AcrId       = Invoke-AzCli acr show -n $Acr -g $Rg --query id -o tsv
# L'identite des apps peut seulement TIRER les images (moindre privilege)
$pullIds = Invoke-AzCli role assignment list --assignee $PrincipalId --role AcrPull --scope $AcrId --query "[].id" -o tsv
if (-not $pullIds) {
    Invoke-AzCli role assignment create --assignee-object-id $PrincipalId --assignee-principal-type ServicePrincipal `
        --role AcrPull --scope $AcrId -o none
}

Write-Host "== 3/7 PostgreSQL manage (5 a 10 minutes a la creation)" -ForegroundColor Cyan
# --public-access 0.0.0.0 : seuls les services Azure passent le pare-feu (amelioration : VNet + endpoint prive)
if (Get-AzOrNull postgres flexible-server show -g $Rg -n $Pg --query name -o tsv) {
    # Le serveur existe deja : on lui applique le mot de passe genere par cette execution
    Invoke-AzCli postgres flexible-server update -g $Rg -n $Pg --admin-password $DbPassword -o none
} else {
    Invoke-AzCli postgres flexible-server create -g $Rg -n $Pg -l $Location `
        --tier Burstable --sku-name Standard_B1ms --storage-size 32 --version 16 `
        --admin-user $DbUser --admin-password $DbPassword --public-access 0.0.0.0 --yes -o none
}
$dbNames = Invoke-AzCli postgres flexible-server db list -g $Rg --server-name $Pg --query "[].name" -o tsv
if (-not ($dbNames -contains $DbName)) {
    Invoke-AzCli postgres flexible-server db create --resource-group $Rg --server-name $Pg --name $DbName -o none
}
$DatabaseUrl = "postgresql://${DbUser}:${DbPassword}@$Pg.postgres.database.azure.com:5432/${DbName}?sslmode=require"

Write-Host "== 4/7 Images construites et poussees dans le registre" -ForegroundColor Cyan
$Registry = "$Acr.azurecr.io"
# Tag unique a chaque execution : Azure tire bien la nouvelle image (un tag reutilise peut rester en cache)
$Tag = Get-Date -Format "yyyyMMddHHmmss"
Invoke-AzCli acr login -n $Acr | Out-Null
docker build -t "$Registry/meteo-backend:$Tag" --build-arg APP_VERSION=$Tag ./backend
if ($LASTEXITCODE -ne 0) { throw "ECHEC : build backend" }
docker push "$Registry/meteo-backend:$Tag"
if ($LASTEXITCODE -ne 0) { throw "ECHEC : push backend" }
docker build -t "$Registry/meteo-frontend:$Tag" ./frontend
if ($LASTEXITCODE -ne 0) { throw "ECHEC : build frontend" }
docker push "$Registry/meteo-frontend:$Tag"
if ($LASTEXITCODE -ne 0) { throw "ECHEC : push frontend" }

Write-Host "== 5/7 Environnement Container Apps (+ Log Analytics)" -ForegroundColor Cyan
if (-not (Get-AzOrNull containerapp env show -n $CaeEnv -g $Rg --query name -o tsv)) {
    Invoke-AzCli containerapp env create -n $CaeEnv -g $Rg -l $Location -o none
}

Write-Host "== 6/7 Backend (ingress interne : pas joignable depuis Internet)" -ForegroundColor Cyan
$DeployedAt = $Tag   # change a chaque execution : force une nouvelle revision
Deploy-ContainerApp -Name "meteo-backend" -Create {
    # Max 8 replicas : budget de connexions de PostgreSQL B1ms (35) / 4 connexions par replica (voir README)
    Invoke-AzCli containerapp create -n meteo-backend -g $Rg --environment $CaeEnv `
        --image "$Registry/meteo-backend:$Tag" `
        --registry-server $Registry --registry-identity $IdentityId --user-assigned $IdentityId `
        --ingress internal --target-port 8000 `
        --cpu 0.5 --memory 1Gi --min-replicas 1 --max-replicas 8 `
        --scale-rule-name http-concurrency --scale-rule-type http --scale-rule-http-concurrency 10 `
        --secrets "database-url=$DatabaseUrl" `
        --env-vars "DATABASE_URL=secretref:database-url" "APP_ENV=azure" "LOG_LEVEL=INFO" "DEPLOYED_AT=$DeployedAt" `
        -o none
} -Update {
    # L'app existe : nouveau mot de passe dans le secret, puis nouvelle revision qui le relit
    Invoke-AzCli containerapp secret set -n meteo-backend -g $Rg --secrets "database-url=$DatabaseUrl" -o none
    Invoke-AzCli containerapp update -n meteo-backend -g $Rg --image "$Registry/meteo-backend:$Tag" `
        --set-env-vars "DEPLOYED_AT=$DeployedAt" -o none
}
$BackendFqdn = Invoke-AzCli containerapp show -n meteo-backend -g $Rg --query properties.configuration.ingress.fqdn -o tsv

Write-Host "== 7/7 Frontend (ingress externe : URL publique HTTPS)" -ForegroundColor Cyan
Deploy-ContainerApp -Name "meteo-frontend" -Create {
    Invoke-AzCli containerapp create -n meteo-frontend -g $Rg --environment $CaeEnv `
        --image "$Registry/meteo-frontend:$Tag" `
        --registry-server $Registry --registry-identity $IdentityId --user-assigned $IdentityId `
        --ingress external --target-port 8080 `
        --cpu 0.25 --memory 0.5Gi --min-replicas 1 --max-replicas 5 `
        --scale-rule-name http-concurrency --scale-rule-type http --scale-rule-http-concurrency 50 `
        --env-vars "BACKEND_URL=https://$BackendFqdn" "DEPLOYED_AT=$DeployedAt" -o none
} -Update {
    Invoke-AzCli containerapp update -n meteo-frontend -g $Rg --image "$Registry/meteo-frontend:$Tag" `
        --set-env-vars "BACKEND_URL=https://$BackendFqdn" "DEPLOYED_AT=$DeployedAt" -o none
}
$FrontendFqdn = Invoke-AzCli containerapp show -n meteo-frontend -g $Rg --query properties.configuration.ingress.fqdn -o tsv

Write-Host "`nApplication : https://$FrontendFqdn" -ForegroundColor Green
Write-Host "Registre    : $Registry"
Write-Host "Deploiement automatique (une seule fois) : .\infra\github-oidc-setup.ps1"
