# Deploying the Function App

> **Status (2026-06-30): LIVE.** The bot runs on **`func-mindme-ymcptc`** in
> `foundrylab-rg` / `swedencentral`. Telegram webhook is connected and the
> Foundry `companion` agent (currently `companion:3`) calls back to this app.
> The sections below are the maintenance runbook + the hard-won deploy recipe.

## ⚠️ Root cause: the 503 SCM wedge (read before touching infra)

Getting the first deploy live took 4 days because of a Flex Consumption trap:

- **Symptom:** `func azure functionapp publish` fails with
  `Uploading archive... (ServiceUnavailable)` / HTTP 503, and
  `https://<app>.scm.azurewebsites.net` returns **503** permanently. It does
  **not** recover from restart, stop/start, deployment-storage swap, or even
  deleting and recreating the site/plan under the same name.
- **Cause:** the app's **host storage** (`AzureWebJobsStorage`) was configured
  with **managed-identity auth** (`AzureWebJobsStorage__accountName` +
  `__credential=managedidentity` + `__clientId`) against a storage account with
  **shared-key access DISABLED**. On Flex Consumption this wedges the SCM/deploy
  plane for good. A throwaway app created with shared-key storage got SCM **401**
  (healthy) and deployed first try — that's how it was isolated.
- **Fix (the working recipe):** host/deploy storage must use a **shared-key
  connection string**, on a storage account separate from the (MI-only,
  shared-key-disabled) **data** storage. `infrastructure/main.bicep` now encodes
  this: data storage `stmindmeymcpt` stays MI-only; a dedicated
  `stmindmedepymcpt` (shared-key enabled) holds the app package +
  `AzureWebJobsStorage`. Personal data is never on the shared-key account.

The live app was ultimately hand-created with `az functionapp create
--storage-account stmindmedepymcpt` (which wires connection-string storage by
default), then configured + published. The Bicep is the clean-rebuild recipe;
it will create a fresh `plan-mindme-ymcptc` rather than adopt the live
auto-created plan `ASP-foundrylabrg-c0d2`.

## Pre-deploy checklist

```powershell
# 1. Active subscription is the personal one
az account show --query name -o tsv
# Expect: Visual Studio Enterprise Subscription

# 2. Function App is the right one
az functionapp show -g foundrylab-rg -n func-mindme-ymcptc --query '{name:name, state:state, kind:kind}' -o jsonc
# Expect: kind=functionapp,linux (state may show null on Flex — use the health probe instead)

# 3. The personal-os container is populated
az storage blob list --account-name stmindmeymcpt --container-name personal-os --auth-mode login --query 'length([])'
# Expect: 68 (67 markdown files + _manifest.json) or more if you've re-synced

# 4. Run the local smoke test one more time
cd c:\vsCode\.nauroLabs\mindMe
.\.venv\Scripts\python.exe scripts\local\test_briefing_snapshot.py
# Expect: JSON with today's date, dashboard slices, area H1s

# 5. Ensure the dig PAT secret exists (used by `/dig` issue creation)
az keyvault secret show --vault-name kv-mindme-ymcpt --name dig-github-token --query id -o tsv
# Expect: a Key Vault secret resource ID (non-empty)

# 6. Ensure the memex webhook secret exists. MEMEX_WEBHOOK_URL is now a Key Vault
#    reference (note/URL/YouTube/voice captures forward here), so a redeploy resolves
#    it from this secret — a missing/empty value silently breaks the capture flows.
az keyvault secret show --vault-name kv-mindme-ymcpt --name memex-webhook-url --query id -o tsv
# Expect: a Key Vault secret resource ID (non-empty)
```

## Deploy

The harness uses Azure Functions Core Tools (`func`). From the harness folder:

```powershell
cd c:\vsCode\.nauroLabs\mindMe\harness
func azure functionapp publish func-mindme-ymcptc --python
```

Expect ~2–4 minutes for a Python Flex Consumption publish. Watch for these
markers in the output:

- `The deployment was successful!`
- `Functions in func-mindme-ymcptc: telegram_webhook, morning_briefing_timer, capture_drain, health, tool_briefing_context, tool_weather, weekly_review_timer`

> The local Python is 3.14; the app targets 3.11. `func` prints a version-mismatch
> warning — it's harmless for this app (remote build installs against 3.11).

If publish fails with `Uploading archive... (ServiceUnavailable)` / 503, see the
**Root cause** section above — it's the host-storage-MI wedge, not a transient
outage. Do not bother retrying; fix the storage auth.

If publish fails with a build error about a missing package, double-check
`harness/requirements.txt` — the 2026-05-16 rev removed `cryptography` and
`azure-keyvault-secrets`. Both are confirmed unused by the new code.

## Re-register the Foundry agent (when tool URLs or hostname change)

The `companion` agent calls back to this app's `/api/tools/*` endpoints, so its
tool URLs are pinned to the function app hostname. If you rebuild the app under a
new name (as happened here), re-point the agent:

```powershell
# create_agent.py uses DefaultAzureCredential, which on Python 3.14 CANNOT spawn
# the az/PowerShell CLI subprocess (azure-identity bug). Use Python <= 3.13.
$py311 = "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
& $py311 -m venv "$env:TEMP\mmagentvenv"
& "$env:TEMP\mmagentvenv\Scripts\python.exe" -m pip install "azure-ai-projects==2.1.0" azure-identity python-dotenv
$env:MINDME_FUNCTION_APP_HOSTNAME = "func-mindme-ymcptc.azurewebsites.net"
cd c:\vsCode\.nauroLabs\mindMe
& "$env:TEMP\mmagentvenv\Scripts\python.exe" scripts\dev\create_agent.py
# Writes AZURE_AI_AGENT_NAME + AZURE_AI_AGENT_VERSION to .env. The Function App
# resolves the agent by NAME (latest version), so no app-setting change needed.
```

Then re-point the Telegram webhook at the new hostname:

```powershell
$bot = az keyvault secret show --vault-name kv-mindme-ymcpt --name telegram-bot-token --query value -o tsv
$sec = az keyvault secret show --vault-name kv-mindme-ymcpt --name telegram-webhook-secret --query value -o tsv
curl.exe -sS "https://api.telegram.org/bot$bot/setWebhook" `
  -d "url=https://func-mindme-ymcptc.azurewebsites.net/api/telegram_webhook" `
  -d "secret_token=$sec" -d "drop_pending_updates=true"
```

## Post-deploy verification

### 1. Webhook still alive

```powershell
# Edit the value for your bot if it's different.
$bot = az keyvault secret show --vault-name kv-mindme-ymcpt --name telegram-bot-token --query value -o tsv
# (Or just DM /ping to @mindMeTo_bot from Telegram.)
```

DM `/ping` to `@mindMeTo_bot`. Expect `pong` back within a few seconds.

### 2. Briefing-context tool

```powershell
# Hit the Foundry-agent-facing tool endpoint directly.
$fn = az functionapp show -g foundrylab-rg -n func-mindme-ymcptc --query defaultHostName -o tsv
curl.exe -sS -X POST "https://$fn/api/tools/briefing_context" -H "Content-Type: application/json" -d '{}' | jq
```

Expect a JSON snapshot with `date`, `top_goals`, `this_week`, `today_focus`,
`yesterday`, `areas`, and `vault_state` (inbox / projects / reviews /
stale_areas). Same shape `scripts/local/test_briefing_snapshot.py` produces.

### 3. The 07:30 timer (the moment of truth)

The morning briefing timer fires at `0 30 7 * * *` Sweden time. The next run is
the next 07:30 in `Europe/Stockholm`. Watch in two places:

- Telegram: a message from `@mindMeTo_bot` ~07:30.
- App Insights: filter on `mindMe.harness` source and `briefing sent` /
  `briefing failed` log lines.

If you want to verify the wire-up without waiting until morning, you can
manually invoke the timer from the Azure Portal under the Function App's
**Functions → morning_briefing_timer → Code + Test → Run**, or programmatically:

```powershell
$resp = az rest --method post `
  --uri "https://management.azure.com/subscriptions/$($env:AZURE_SUBSCRIPTION_ID)/resourceGroups/foundrylab-rg/providers/Microsoft.Web/sites/func-mindme-ymcptc/host/default/triggers/morning_briefing_timer?api-version=2022-03-01" `
  --body '{}'
```

(The manual invoke will send a real Telegram message to the allowlisted chat.
Be ready for it.)

## Rolling back if something goes wrong

The previous implementation read `briefing-context/today.bin` decrypted from
the KV-stored key. To roll back:

1. `git revert <this commit>` in the mindMe repo, OR check out the previous
   `harness/function_app.py` from git.
2. `func azure functionapp publish func-mindme-ymcptc --python` again.
3. From the laptop:
   `.\.venv\Scripts\python.exe scripts\local\briefing_builder.py` to refresh
   `briefing-context/today.bin` (the script is still there, marked DEPRECATED,
   but functional).
4. Register the laptop scheduled task per the legacy block in git history.

You can verify the old blob is still present:

```powershell
az storage blob show --account-name stmindmeymcpt --container-name briefing-context --name today.bin --auth-mode login --query '{lastModified:properties.lastModified, size:properties.contentLength}' -o jsonc
```

Nothing in the new flow touches the legacy container or KV key, so rollback is
a config flip + a re-publish.

## Cleanup (when you trust the new flow)

After a week or so of successful 07:30 briefings:

```powershell
# Drop the legacy encrypted blob
az storage blob delete --account-name stmindmeymcpt --container-name briefing-context --name today.bin --auth-mode login

# Drop the legacy KV secret
az keyvault secret delete --vault-name kv-mindme-ymcpt --name briefing-encryption-key

# Drop the legacy container itself (purges any residual blobs)
az storage container delete --account-name stmindmeymcpt --name briefing-context --auth-mode login

# Delete the local briefing_builder.py file
Remove-Item c:\vsCode\.nauroLabs\mindMe\scripts\local\briefing_builder.py
```

The Bicep template still provisions some of these (KV secret, briefing-context
container, AES-GCM-related app settings on the Function App). Whether to clean
those up depends on how often the Bicep is re-applied. Easiest path is a
separate small commit to `infrastructure/main.bicep` removing them once the
new flow is confirmed good.
