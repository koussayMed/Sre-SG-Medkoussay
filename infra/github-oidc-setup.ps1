# Connexion GitHub Actions -> Azure par OIDC (federation d'identite), a lancer UNE fois depuis le poste :
#   powershell -ExecutionPolicy Bypass -File .\infra\github-oidc-setup.ps1
# Relancable sans risque. Prerequis : `az login`, et infra\azure-setup.ps1 deja execute.
#
# Principe : aucun mot de passe ni cle Azure n'est stocke dans GitHub. A chaque deploiement, GitHub
# fournit un jeton signe qui dit "je suis le depot X, environnement production" ; Azure l'echange contre
# un jeton d'acces de courte duree, seulement si le depot et l'environnement correspondent.
#
# Droits accordes a l'identite du pipeline (moindre privilege, limite au groupe de ressources) :
#   - AcrPush sur le registre : pousser les images ;
#   - Contributor sur rg-sre-meteo uniquement : mettre a jour les Container Apps (rien d'autre dans
#     l'abonnement, et pas le droit de donner des roles).
#
# Note Windows : az est un fichier .cmd ; on evite donc parentheses, | et & dans ses arguments.

$ErrorActionPreference = "Stop"

# --- Parametres (doivent correspondre a azure-setup.ps1 et au depot GitHub) --------------------
$GitHubRepo  = "koussayMed/Sre-SG-Medkoussay"   # proprietaire/depot, avec la meme casse que sur GitHub
$Environment = "production"             # environnement GitHub du job de deploiement
# Sujet exact du jeton GitHub, visible dans les logs de l'etape azure/login ("subject claim").
# GitHub y inclut les identifiants numeriques du proprietaire et du depot : un depot supprime puis
# recree avec le meme nom n'herite donc pas de la confiance. Nouveau depot = relancer ce script.
$GitHubSubject = "repo:koussayMed@147982194/Sre-SG-Medkoussay@1431466046:environment:$Environment"
$Suffix      = "koussay"
$Rg          = "rg-sre-meteo"
$Acr         = "acrsremeteo$Suffix"
$AppName     = "gh-sre-meteo-deploy"    # nom de l'application Entra ID du pipeline

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

function Grant-Role {
    # Attribue un role s'il manque. Reessaie : un principal tout neuf met parfois une minute a etre visible.
    param([string]$PrincipalId, [string]$Role, [string]$Scope)
    $ids = Get-AzOrNull role assignment list --assignee $PrincipalId --role $Role --scope $Scope --query "[].id" -o tsv
    if ($ids) { Write-Host "  $Role : deja attribue"; return }
    for ($attempt = 1; $attempt -le 5; $attempt++) {
        $ok = Get-AzOrNull role assignment create --assignee-object-id $PrincipalId `
            --assignee-principal-type ServicePrincipal --role $Role --scope $Scope -o none
        if ($LASTEXITCODE -eq 0) { Write-Host "  $Role : attribue"; return }
        Write-Host "  $Role : principal pas encore visible, nouvel essai dans 15 s" -ForegroundColor Yellow
        Start-Sleep -Seconds 15
    }
    throw "ECHEC : attribution du role $Role"
}

Write-Host "== 1/4 Abonnement et ressources" -ForegroundColor Cyan
$SubscriptionId = Invoke-AzCli account show --query id -o tsv
$TenantId       = Invoke-AzCli account show --query tenantId -o tsv
$RgId           = Invoke-AzCli group show -n $Rg --query id -o tsv
$AcrId          = Invoke-AzCli acr show -n $Acr -g $Rg --query id -o tsv

Write-Host "== 2/4 Application Entra ID du pipeline (sans secret)" -ForegroundColor Cyan
$AppId = Invoke-AzCli ad app list --display-name $AppName --query "[0].appId" -o tsv
if (-not $AppId) {
    $AppId = Invoke-AzCli ad app create --display-name $AppName --query appId -o tsv
}
$SpId = Get-AzOrNull ad sp show --id $AppId --query id -o tsv
if (-not $SpId) {
    $SpId = Invoke-AzCli ad sp create --id $AppId --query id -o tsv
}

Write-Host "== 3/4 Confiance envers GitHub : depot $GitHubRepo, environnement $Environment" -ForegroundColor Cyan
$CredName = "github-$Environment"
# Sujet actuellement enregistre (lignes "nom<TAB>sujet") : s'il a change (nouveau depot), on le remplace
$currentSubject = $null
foreach ($line in @(Invoke-AzCli ad app federated-credential list --id $AppId --query "[].[name,subject]" -o tsv)) {
    $parts = "$line" -split "`t"
    if ($parts[0] -eq $CredName) { $currentSubject = $parts[1] }
}
if ($currentSubject -and $currentSubject -ne $GitHubSubject) {
    Write-Host "  ancien sujet $currentSubject : remplacement" -ForegroundColor Yellow
    Invoke-AzCli ad app federated-credential delete --id $AppId --federated-credential-id $CredName -o none
    $currentSubject = $null
}
if ($currentSubject) {
    Write-Host "  deja configuree"
} else {
    $credential = @{
        name        = $CredName
        issuer      = "https://token.actions.githubusercontent.com"
        subject     = $GitHubSubject
        audiences   = @("api://AzureADTokenExchange")
        description = "Deploiement GitHub Actions de $GitHubRepo"
    } | ConvertTo-Json
    # Passe par un fichier : le JSON en argument serait mal transmis par az.cmd. UTF-8 sans BOM.
    $file = Join-Path $env:TEMP "sre-meteo-federated-credential.json"
    [System.IO.File]::WriteAllText($file, $credential)
    try {
        Invoke-AzCli ad app federated-credential create --id $AppId --parameters $file -o none
    } finally {
        Remove-Item $file -ErrorAction SilentlyContinue
    }
    Write-Host "  creee"
}

Write-Host "== 4/4 Droits du pipeline" -ForegroundColor Cyan
Grant-Role -PrincipalId $SpId -Role "AcrPush" -Scope $AcrId
Grant-Role -PrincipalId $SpId -Role "Contributor" -Scope $RgId

Write-Host ""
Write-Host "Termine. A ajouter dans GitHub : Settings > Secrets and variables > Actions > New repository secret" -ForegroundColor Green
Write-Host "  AZURE_CLIENT_ID       = $AppId"
Write-Host "  AZURE_TENANT_ID       = $TenantId"
Write-Host "  AZURE_SUBSCRIPTION_ID = $SubscriptionId"
Write-Host "Ce sont des identifiants, pas des mots de passe : sans jeton GitHub du bon depot, ils ne donnent aucun acces."
