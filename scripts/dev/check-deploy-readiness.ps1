#!/usr/bin/env pwsh
<#
.SYNOPSIS
  Pre-publish readiness check for the mindMe Function App (Flex Consumption).

.DESCRIPTION
  Run this BEFORE `func azure functionapp publish`. It diagnoses whether a publish
  will succeed and — crucially — distinguishes the two very different 503 failure
  classes so you never panic-rebuild a correctly-configured app:

    * CONFIG WEDGE  (permanent)  — host/deploy storage misconfigured, e.g. managed-
                                   identity auth on a shared-key-DISABLED account.
                                   Fix = rebuild with connection-string storage.
                                   See docs/deploy.md "503 SCM wedge".
    * TRANSIENT 503 (self-heals) — config is correct AND Azure reports the app
                                   `availability: Normal`, but the Flex deploy/SCM
                                   sub-service returns 503. Fix = wait and retry.
                                   Do NOT rebuild.

  Read-only: makes no changes to any Azure resource.

  Exit codes: 0 = ready to publish, 1 = transient deploy-plane 503 (retry later),
  2 = config problem / not ready.

.EXAMPLE
  pwsh scripts/dev/check-deploy-readiness.ps1
#>
[CmdletBinding()]
param(
    [string]$ResourceGroup     = 'foundrylab-rg',
    [string]$FunctionApp       = 'func-mindme-ymcptc',
    [string]$DeployStorage     = 'stmindmedepymcpt',
    [string]$HostStorageSetting = 'AzureWebJobsStorage',
    # A known-healthy sibling Flex app in the same region, used only to tell a
    # regional outage (peer also 503) from an app-specific wedge (peer healthy).
    [string]$PeerApp           = 'func-memex-f5o6h2un2sqiu'
)

$ErrorActionPreference = 'Continue'
$fails = @()
$wedge = $false

function Ok  ($m) { Write-Host "  [ OK ] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  [WARN] $m" -ForegroundColor Yellow }
function Bad ($m, [switch]$Wedge) {
    Write-Host "  [FAIL] $m" -ForegroundColor Red
    $script:fails += $m
    if ($Wedge) { $script:wedge = $true }
}

Write-Host "mindMe deploy-readiness check -> $FunctionApp" -ForegroundColor Cyan

# 1. Azure login / subscription
$sub = az account show --query id -o tsv 2>$null
if (-not $sub) { Bad "Not logged in to az (run 'az login')." }
else { Ok "Subscription: $(az account show --query name -o tsv 2>$null)" }

# 2. App status + Flex deployment config (raw ARM; the CLI doesn't surface this well)
$scm = $null
if ($sub) {
    $app = az rest --method get --url "https://management.azure.com/subscriptions/$sub/resourceGroups/$ResourceGroup/providers/Microsoft.Web/sites/$FunctionApp`?api-version=2024-04-01" 2>$null | ConvertFrom-Json
    if (-not $app) { Bad "Could not read the app via ARM (name/RG correct? access?)." }
    else {
        $p = $app.properties
        if ($p.availabilityState -eq 'Normal') { Ok "Azure availability: Normal" } else { Warn "Azure availability: $($p.availabilityState)" }
        if ($p.state -eq 'Running') { Ok "App state: Running" } else { Warn "App state: $($p.state)" }

        $deployAuth = $p.functionAppConfig.deployment.storage.authentication.type
        if     ($deployAuth -eq 'StorageAccountConnectionString') { Ok "Deploy auth: StorageAccountConnectionString" }
        elseif ($deployAuth) { Bad "Deploy auth: $deployAuth (expected StorageAccountConnectionString)" -Wedge }
        else   { Warn "Deploy auth: <null> (could not read)" }
    }
}

# 3. Host storage must be a connection string, NOT managed identity
$settings = az functionapp config appsettings list -g $ResourceGroup -n $FunctionApp 2>$null | ConvertFrom-Json
$aws = ($settings | Where-Object { $_.name -eq $HostStorageSetting }).value
if     ($aws -like 'DefaultEndpointsProtocol=*') { Ok "$HostStorageSetting = shared-key connection string" }
elseif ($aws) { Bad "$HostStorageSetting is NOT a connection string (MI auth wedges the Flex deploy plane)" -Wedge }
else   { Warn "$HostStorageSetting not found" }

# 4. Deploy storage: shared-key enabled + app-package container present
$dep = az storage account show -n $DeployStorage -g $ResourceGroup --query "{sk:allowSharedKeyAccess, prov:provisioningState}" 2>$null | ConvertFrom-Json
if ($dep) {
    # allowSharedKeyAccess null = default (enabled); only an explicit $false is a wedge.
    if ($dep.sk -eq $false) { Bad "Deploy storage $DeployStorage has shared-key access DISABLED (Flex deploy needs it)" -Wedge }
    else { Ok "Deploy storage $DeployStorage shared-key: enabled" }
} else { Warn "Could not read deploy storage $DeployStorage" }

$container = az storage container show --account-name $DeployStorage --name app-package --auth-mode login --query name -o tsv 2>$null
if ($container -eq 'app-package') { Ok "Deploy container 'app-package' exists" } else { Warn "Deploy container 'app-package' not visible via your identity" }

# 5. Deploy/SCM plane reachability (401 = healthy/needs-auth, 503 = plane down)
$scm = curl.exe -s -o NUL -w "%{http_code}" --max-time 30 "https://$FunctionApp.scm.azurewebsites.net" 2>$null
if     ($scm -eq '401') { Ok "SCM/deploy plane reachable (401 = healthy)" }
elseif ($scm -eq '503') { Warn "SCM/deploy plane: 503 (unavailable)" }
else   { Warn "SCM/deploy plane: HTTP $scm" }

# 5b. On a 503, probe a healthy peer to tell regional-transient from app-specific.
$peerScm = $null
if ($scm -eq '503' -and $PeerApp) {
    $peerScm = curl.exe -s -o NUL -w "%{http_code}" --max-time 30 "https://$PeerApp.scm.azurewebsites.net" 2>$null
    if     ($peerScm -eq '401') { Warn "Peer $PeerApp SCM healthy (401) -> the 503 is APP-SPECIFIC, not regional" }
    elseif ($peerScm -eq '503') { Ok   "Peer $PeerApp SCM also 503 -> looks regional/transient" }
    else   { Warn "Peer $PeerApp SCM: HTTP $peerScm (inconclusive)" }
}

# --- Verdict ---
Write-Host "`n--- Verdict ---" -ForegroundColor Cyan
if ($wedge) {
    Write-Host "CONFIG WEDGE: a storage misconfiguration is present. A rebuild with connection-string storage is required — see docs/deploy.md '503 SCM wedge'. Do this deliberately, not blindly." -ForegroundColor Red
    exit 2
}
elseif ($scm -eq '503') {
    Write-Host "DEPLOY PLANE 503 (config is correct, so NOT the storage wedge)." -ForegroundColor Yellow
    if ($peerScm -eq '401') {
        Write-Host "  A healthy peer means this is app-specific. A brief spell is transient (retry). If it persists for hours, it may be a Flex platform wedge of THIS app's deploy plane — the documented remedy is a fresh-functionSuffix rebuild (docs/deploy.md), which re-points the Telegram webhook + Foundry agent. Decide deliberately; don't rebuild reflexively." -ForegroundColor Yellow
    } else {
        Write-Host "  Peer also affected / inconclusive -> looks regional/transient. Wait and retry 'func azure functionapp publish $FunctionApp --python'." -ForegroundColor Yellow
    }
    exit 1
}
elseif ($fails.Count -gt 0) {
    Write-Host "NOT READY: see [FAIL] items above." -ForegroundColor Red
    exit 2
}
else {
    Write-Host "READY: 'func azure functionapp publish $FunctionApp --python' should succeed." -ForegroundColor Green
    exit 0
}
