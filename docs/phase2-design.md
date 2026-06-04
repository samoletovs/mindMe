# Phase 2 — Morning briefing capability

> **⚠ Superseded 2026-05-16.** The "07:25 laptop → upload encrypted blob → 07:30
> Function reads blob" design described below was implemented and smoke-tested,
> then deliberately retired before Phase 2 went live. The new design lives in
> [architecture.md](architecture.md): the Function reads the OS markdown
> directly from a private `personal-os/` container that the laptop syncs to
> on-demand. No daily laptop schedule.
>
> This file is kept as a design-decision record. The trade-off discussion
> ("does personal markdown live in cloud at all?") that produced the pivot is
> in [architecture.md §1](architecture.md).

> **Status:** scaffolding (2026-05-11). Code committed; no Azure deployment yet.
> **Goal:** every morning at 07:30 local time, receive a Telegram message with today's plan + weather, assembled by the `companion` agent from a sanitized briefing context the laptop uploaded at 07:25.

## Architecture

```
07:25 (laptop, Task Scheduler)              07:30 (Azure, Timer trigger)
┌────────────────────────────┐              ┌──────────────────────────────┐
│ scripts/local/             │   blob put   │ harness/morning_briefing_timer│
│   briefing_builder.py      │ ───────────► │  - calls Foundry agent        │
│  - reads OneDrive\.me      │   (AES-GCM)  │  - sends reply via Bot API    │
│  - sanitizes to JSON       │              └──────────────┬───────────────┘
│  - encrypts (AES-GCM)      │                             │
│  - uploads to Blob         │                             ▼
└────────────────────────────┘              ┌──────────────────────────────┐
                                            │ Foundry agent: companion v2  │
                                            │  - get_briefing_context()    │
                                            │  - get_weather()             │
                                            └──────────────────────────────┘
```

Telegram inbound is identical: a webhook on `harness/telegram_webhook` replaces the long-poll `scripts/dev/telegram_bridge.py`. Long-poll bridge stays as dev fallback.

## Resources to deploy (`infrastructure/main.bicep`)

| Resource | SKU / tier | Why |
|---|---|---|
| Storage Account `stmindme<suffix>` | Standard_LRS | Blob (briefing-context), Queue (capture-events). LRS is fine — daily overwrite. |
| Key Vault `kv-mindme-<suffix>` | Standard | `telegram-bot-token`, `briefing-encryption-key`. Soft-delete on, purge protection off (cheaper to recover from mistakes). |
| Application Insights `appi-mindme` | classic | Workspace-based, attached to existing LAW if available else create. |
| Log Analytics Workspace `log-mindme` | PerGB2018 | Required by workspace-based App Insights. Retention 30 days. |
| Function App `func-mindme-<suffix>` | Flex Consumption (Python 3.11) | Better cold-start than classic Consumption; only pay for execution. |
| Function App Plan | `FC1` | Free-tier flex with per-instance billing. |
| User-Assigned Managed Identity `id-mindme` | — | Attached to Function App. Granted RBAC on Storage, Key Vault, Foundry. No keys/SAS. |

**Naming:** `<resourceType>-mindme-<region>-<5char-rand>` lowercase. Suffix avoids collisions on storage account global namespace.

**Region:** `swedencentral` (matches `foundrylab-rg`).

**Estimated idle cost:** ~€3/month (Flex Consumption when idle ≈ €0, Storage ≈ €0.50, Key Vault ≈ €0.20, App Insights at 1GB ingestion cap ≈ €1.50, LAW ≈ €1). Budget alert set at €8 (already in tracker).

## Function App endpoints (`harness/`)

| Function | Trigger | Purpose |
|---|---|---|
| `telegram_webhook` | HTTP (POST, anonymous + secret token in URL) | Replaces long-poll. Telegram sends updates here. Verifies `X-Telegram-Bot-Api-Secret-Token` header. |
| `morning_briefing_timer` | Timer (`0 30 7 * * *` Sweden time) | Calls Foundry agent with seed prompt "compose today's briefing"; sends result via Telegram Bot API. |
| `capture_drain` | Queue (`capture-events`) | (Phase 3 — scaffolded but no real work yet). |
| `health` | HTTP GET (anonymous) | `200 OK` for uptime checks. |

All functions use the **user-assigned managed identity** for Foundry, Storage, Key Vault. Bot token comes from Key Vault via Function App app setting `@Microsoft.KeyVault(...)` reference (no plain-text secret in app settings).

## Foundry agent changes (`companion:2`)

Add two function-calling tools to the existing prompt agent:

- `get_briefing_context()` → returns the decrypted JSON snapshot from Blob. Function implemented in `harness/_tools/briefing_context.py` and exposed via HTTP for the agent to call.
- `get_weather(location: str)` → wttr.in JSON, no auth.

**Hosting decision:** because Foundry Agents v2 prompt agents call tools via HTTP function endpoints (OpenAPI-described), the tools live in the Function App. The agent definition gets an OpenAPI spec pointing at `func-mindme-<suffix>.azurewebsites.net/api/tools/*`. The Function App authenticates the agent via the user-assigned managed identity + Easy Auth.

This is scaffolded but **NOT redeployed** yet — `create_agent.py` is updated locally with the new definition but the user must run it after the Function App is live (so the OpenAPI URL exists).

## Local briefing builder (`scripts/local/briefing_builder.py`)

Runs at 07:25 via Windows Task Scheduler. Steps:

1. Read `%USERPROFILE%\OneDrive\.vscode\.me\_dashboard.md` (configurable via `ME_OS_ROOT`), today's journal entry, and current `02_areas/*/README.md` headers.
2. Build a tiered sanitized JSON snapshot:
   ```json
   {
     "schema_version": "2.0.0",
     "date": "2026-05-12",
     "tiers": {
       "core": {"today_focus": "...", "top_goals": ["..."], "urgent_deadlines": ["..."]},
       "extended": {"entries": [{"title": "...", "relevance_score": 8.5}]},
       "deep": {"entries": [{"content_encoding": "zlib+base64", "content_b64": "..."}]}
     }
   }
   ```
   - Entries are ranked by recency + priority (deadlines, unresolved loops, active areas).
   - Hard per-tier size budgets trim low-priority entries instead of failing the run.
   - Blob metadata includes schema version and budget usage.
3. Encrypt with AES-GCM using key from Key Vault (`briefing-encryption-key`).
4. Upload to `briefing-context` container, blob name `today.bin`. Overwrite.
5. Log only sizes and timing. **Never** log content.

Manual install (one-time) registers a Task Scheduler entry running this script daily.

## What's out of scope for Phase 2

- Quick capture (queue drain real work) — Phase 3.
- Foundry batch eval + prompt optimizer — Phase 4.
- Multi-day briefing memory — v2.
- Calendar / email integration — v2.

## Deployment order (when the user is ready)

1. `az login` (verify personal account)
2. `az account set --subscription <personal-subscription-id>` (canonical value lives in Personal OS, not this repo)
3. `az deployment group create -g foundrylab-rg -f infrastructure/main.bicep -p infrastructure/main.bicepparam`
4. Manually upload bot token to Key Vault: `az keyvault secret set ...`
5. Manually upload encryption key (32 random bytes base64) to Key Vault.
6. `func azure functionapp publish func-mindme-<suffix> --python`
7. Run `scripts/dev/create_agent.py` to deploy `companion:2` with tools.
8. Set Telegram webhook to `https://func-mindme-<suffix>.azurewebsites.net/api/telegram_webhook?token=<secret>`.
9. Install Task Scheduler entry for `briefing_builder.py` at 07:25.
10. Wait 24h, verify the 07:30 message arrives. If yes, Phase 2 ships.

Until step 1, only file-level changes have happened. No Azure cost is incurred.
