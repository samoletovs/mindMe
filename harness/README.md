# mindMe / harness

Azure Functions (Python v2 programming model) that host the cloud-side of
mindMe. Replaces the long-poll `scripts/dev/telegram_bridge.py` in production.

## Endpoints

| Trigger | Name | Purpose |
|---|---|---|
| HTTP POST `/api/telegram_webhook` | `telegram_webhook` | Telegram update receiver. Verifies `X-Telegram-Bot-Api-Secret-Token`. Enforces allowlist. |
| Timer `0 30 7 * * *` | `morning_briefing_timer` | Sends the daily briefing at 07:30 UTC. |
| Timer `0 0 18 * * 0` | `weekly_review_timer` | Weekly-review nudge, Sunday 18:00 UTC. |
| Timer `0 */30 * * * *` | `reaper_poll_timer` | Polls GitHub for finished Copilot-agent PRs in mindVault/familyVault and fires the existing reaper workflow via `workflow_dispatch`. Moves the reapers' idle polling off metered GitHub Actions minutes. See [github_reapers.py](github_reapers.py). |
| Queue `capture-events` | `capture_drain` | Legacy, unsupported. Raises instead of consuming and discarding messages; Functions retries then retains them in the poison queue. Active captures go to memex. |
| HTTP GET `/api/health` | `health` | Uptime probe. |
| HTTP POST `/api/tools/briefing_context?tier=core|extended|deep&include_meta=true|false` | `tool_briefing_context` | Foundry agent tool: returns the requested sanitized briefing view built in-process from `personal-os/` (`core` default). |
| HTTP GET `/api/tools/weather?location=...` | `tool_weather` | Foundry agent tool: wttr.in passthrough. |
| HTTP GET `/api/tools/vault_recent` | `tool_vault_recent` | List recent allowed mindVault files. |
| HTTP GET `/api/tools/vault_read` | `tool_vault_read` | Read an allowed markdown file; truncated responses are labelled. |

All `/api/tools/*` routes require Function authentication through `x-functions-key`.
The Foundry agent uses the `mindme-tools` project connection, not anonymous access.
See [the coordinated authentication rollout](../docs/deploy.md).

Failed memex forwarding returns HTTP 503 so Telegram can retry. Both messages
and callback queries enforce the single-chat allowlist. A successful handoff
means memex accepted the request, not that downstream classification/storage has
finished. The health endpoint is a **liveness probe**, not a dependency check.

The cloud snapshot is flat (`core`); `extended`/`deep` are legacy compatibility
views and currently contain no entries. Storage failures are not represented as
an empty vault, and unavailable open-loop state has null counts. The sync
manifest supplies freshness metadata without exposing its source path.

## Local dev

```powershell
cd harness
cp local.settings.json.example local.settings.json
# fill in storage account name, Foundry endpoint, etc.
func start
```

## Tests

Unit tests live in [`tests/`](tests) and mock every external dependency
(GitHub REST, Telegram, Foundry, Storage) — they need no secrets and touch no
personal data. They import the harness modules directly, so the runtime deps
have to be installed alongside the test deps:

```powershell
python -m pip install -r harness/requirements.txt -r harness/requirements-dev.txt
python -m pytest          # config in pytest.ini at the repo root
```

CI runs the same command on every push and pull request
(`.github/workflows/tests.yml`).

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
- `dig-github-token` — PAT (issues:write on mindVault) for the `/dig` front door.
- `reaper-github-token` — fine-grained PAT with **Actions R/W + Pull requests R + Contents R on both mindVault and familyVault**, used by `reaper_poll_timer`. Falls back to `dig-github-token` (mindVault only) if unset.
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

## Reaper migration & cutover

`reaper_poll_timer` (see [github_reapers.py](github_reapers.py)) replaces the *idle
polling* the six mindVault/familyVault reaper workflows did on GitHub Actions. Those
polls billed a whole minute each — mostly to find nothing — and were the bulk of the
account's Actions-minute spend. The timer runs the polling on Azure for free and only
`workflow_dispatch`-es a reaper when there is a real finished PR (or a non-empty
promotion outbox), so the workflows run on Actions only a handful of times a month.

Migrated reapers: `dig-reaper`, `promote-reaper`, `newsletter-reaper`, `dispatch-reaper`,
`promote-forward` (mindVault) and `promote-reaper` (familyVault).

**Cut over in this order — do NOT remove the crons before the timer is verified, or the
reapers stop running in the gap.**

1. **Seed the PAT.** Create a fine-grained GitHub PAT (Actions R/W, Pull requests R,
   Contents R on `samoletovs/mindVault` and `samoletovs/familyVault`) and store it:

   ```powershell
   az keyvault secret set --vault-name kv-mindme-<suffix> `
     --name reaper-github-token --value "<PAT>"
   ```

2. **Deploy** — Bicep adds the `REAPER_GITHUB_TOKEN` app setting, then publish the code:

   ```powershell
   az deployment group create -g foundrylab-rg -f infrastructure/main.bicep -p infrastructure/main.bicepparam
   func azure functionapp publish func-mindme-<suffix> --python
   ```

3. **Verify.** In App Insights, look for the `reaper_poll_timer` trace
   `reaper poll complete checked=6 dispatched=… errors=0`. To exercise it end-to-end,
   leave a finished Copilot-agent PR open in mindVault and confirm the matching reaper
   workflow gets dispatched in that repo's Actions tab.

4. **Cut over.** Remove the `schedule:` block from each migrated workflow (keep
   `workflow_dispatch:` so the timer can still fire it). After this they consume **0**
   scheduled Actions minutes:

   - `mindVault/.github/workflows/{dig-reaper,promote-reaper,newsletter-reaper,dispatch-reaper,promote-forward}.yml`
   - `familyVault/.github/workflows/promote-reaper.yml`

To roll back to Actions-only, restore each `schedule:` block and disable
`reaper_poll_timer`.
