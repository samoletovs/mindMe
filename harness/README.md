# mindMe / harness

Azure Functions (Python v2 programming model) that host the cloud-side of
mindMe. Replaces the long-poll `scripts/dev/telegram_bridge.py` in production.

## Endpoints

| Trigger | Name | Purpose |
|---|---|---|
| HTTP POST `/api/telegram_webhook` | `telegram_webhook` | Telegram update receiver. Verifies `X-Telegram-Bot-Api-Secret-Token`. Enforces allowlist. |
| Timer `0 30 7 * * *` | `morning_briefing_timer` | Sends the daily briefing at 07:30 (server time). |
| Queue `capture-events` | `capture_drain` | Phase 3 placeholder. Receives Telegram captures forwarded by the webhook. |
| HTTP GET `/api/health` | `health` | Uptime probe. |
| HTTP POST `/api/tools/briefing_context?tier=core|extended|deep&include_meta=true|false` | `tool_briefing_context` | Foundry agent tool: returns the requested sanitized briefing view built in-process from `personal-os/` (`core` default). |
| HTTP GET `/api/tools/weather?location=...` | `tool_weather` | Foundry agent tool: wttr.in passthrough. |

## Local dev

```powershell
cd harness
cp local.settings.json.example local.settings.json
# fill in storage account name, Foundry endpoint, etc.
func start
```

## Deploy

After `az deployment group create -g foundrylab-rg -f infrastructure/main.bicep`:

```powershell
func azure functionapp publish func-mindme-<suffix> --python
```

## Secrets

The Function App pulls these from Key Vault via `@Microsoft.KeyVault(...)`
references in App Settings:

- `telegram-bot-token` — bot token from @BotFather.
- `telegram-webhook-secret` — random string passed to Telegram `setWebhook`.
The user-assigned managed identity (`id-mindme`) has `Key Vault Secrets User`.

Personal OS markdown is read directly from the private `personal-os/` blob
container via managed identity. The old AES-GCM `briefing-encryption-key` flow
is legacy-only and no longer part of the active runtime.

## Once the App is live, wire Telegram

```powershell
$token = "<your bot token>"
$secret = "<your webhook secret>"
$url    = "https://func-mindme-<suffix>.azurewebsites.net/api/telegram_webhook"

Invoke-RestMethod -Method Post `
  -Uri "https://api.telegram.org/bot$token/setWebhook" `
  -Body @{ url = $url; secret_token = $secret } -ErrorAction Stop
```

Then stop running `scripts/dev/telegram_bridge.py` — Telegram will deliver to
the webhook instead.
