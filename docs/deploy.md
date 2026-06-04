# Deploying the Function App

> **Pending action for the human.** Everything below is safe to run after
> reviewing the code changes in this commit. Code is validated locally; the
> only remaining step is pushing it to the live Function App and watching the
> first 07:30 execution.

## Pre-deploy checklist

```powershell
# 1. Active subscription is the personal one
az account show --query name -o tsv
# Expect: Visual Studio Enterprise Subscription

# 2. Function App is the right one
az functionapp show -g foundrylab-rg -n func-mindme-ymcpt --query '{name:name, state:state, kind:kind}' -o jsonc
# Expect: state=Running, kind=functionapp,linux

# 3. The personal-os container is populated
az storage blob list --account-name stmindmeymcpt --container-name personal-os --auth-mode login --query 'length([])'
# Expect: 68 (67 markdown files + _manifest.json) or more if you've re-synced

# 4. Run the local smoke test one more time
cd c:\vsCode\.nauroLabs\mindMe
.\.venv\Scripts\python.exe scripts\local\test_briefing_snapshot.py
# Expect: JSON with today's date, dashboard slices, area H1s
```

## Deploy

The harness uses Azure Functions Core Tools (`func`). From the harness folder:

```powershell
cd c:\vsCode\.nauroLabs\mindMe\harness
func azure functionapp publish func-mindme-ymcpt --python
```

Expect ~2–4 minutes for a Python Flex Consumption publish. Watch for these
markers in the output:

- `Remote build succeeded`
- `Functions in func-mindme-ymcpt: telegram_webhook, morning_briefing_timer, capture_drain, health, tool_briefing_context, tool_weather`

If publish fails with a build error about a missing package, double-check
`harness/requirements.txt` — the 2026-05-16 rev removed `cryptography` and
`azure-keyvault-secrets`. Both are confirmed unused by the new code.

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
$fn = az functionapp show -g foundrylab-rg -n func-mindme-ymcpt --query defaultHostName -o tsv
curl.exe -sS -X POST "https://$fn/api/tools/briefing_context" -H "Content-Type: application/json" -d '{}' | jq
```

Expect a JSON snapshot with `date`, `top_goals`, `this_week`, `today_focus`,
`yesterday`, `areas`. Same shape `scripts/local/test_briefing_snapshot.py`
produces.

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
  --uri "https://management.azure.com/subscriptions/$($env:AZURE_SUBSCRIPTION_ID)/resourceGroups/foundrylab-rg/providers/Microsoft.Web/sites/func-mindme-ymcpt/host/default/triggers/morning_briefing_timer?api-version=2022-03-01" `
  --body '{}'
```

(The manual invoke will send a real Telegram message to the allowlisted chat.
Be ready for it.)

## Rolling back if something goes wrong

The previous implementation read `briefing-context/today.bin` decrypted from
the KV-stored key. To roll back:

1. `git revert <this commit>` in the mindMe repo, OR check out the previous
   `harness/function_app.py` from git.
2. `func azure functionapp publish func-mindme-ymcpt --python` again.
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
