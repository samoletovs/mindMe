# mindMe — architecture

> Current architecture as of **2026-05-16**. Supersedes the original Phase 2
> design (see [phase2-design.md](phase2-design.md) for the historical record
> and the trade-off discussion that produced this pivot).

## 1. The decision that drove this rev

Original design (2026-05-11) had a laptop-side scheduled task at 07:25 building
an encrypted briefing-context blob, then the Function read it at 07:30. That
satisfied a hard rule: *personal markdown NEVER lives unencrypted in the cloud.*

In practice the laptop dependency was the wrong trade:

- The whole point of mindMe is to **not depend on a laptop being on at 07:25**
  for a 07:30 Telegram briefing. Travel, OneDrive sync delay, laptop reboot —
  any of these silently break the day.
- "Encrypted at the app layer with a key in the same Azure tenant" is a thin
  protection against the threats it was meant to cover. The realistic threat
  (someone with read access to the storage account) is gated by RBAC and
  managed identity, not by the AES-GCM layer.
- Microsoft-managed at-rest encryption already covers the at-rest leak case
  for a private blob container.

So the rule was relaxed (consciously, with this note as the record): **personal
markdown lives in a private blob container in Azure, MMK-encrypted at rest,
gated by RBAC and managed identity**. No app-layer encryption.

If that trade ever stops being acceptable, see §5 for how to re-introduce
app-layer encryption without redesigning the data flow.

## 2. Data flow

```
┌────────────────────────────────────────┐
│ Laptop (occasional, on-demand only)    │
│  - .me (Personal OS, markdown)         │  When you edit, run:
│  - scripts/local/sync_os_to_blob.py    │  → pushes markdown to personal-os/
└──────────────────┬─────────────────────┘
                   │ (on-demand, never scheduled)
                   ↓
┌──────────────────────────────────────────────────┐
│ Azure (foundrylab-rg, swedencentral)             │
│                                                  │
│  Storage Account stmindmeymcpt                   │
│   └─ personal-os/      ← markdown mirror         │
│                                                  │
│  Function App func-mindme-ymcpt (Flex Consumption)│
│   ├─ morning_briefing_timer (07:30 CRON)         │
│   │   → asks companion agent for the briefing    │
│   ├─ tool_briefing_context                       │
│   │   → reads personal-os/ blobs                 │
│   │   → builds sanitized JSON                    │
│   │   → returns to companion                     │
│   └─ tool_weather → wttr.in                      │
│                                                  │
│  Foundry agent: companion                        │
│   composes the user-facing message               │
└──────────────────────────────────────────────────┘
                   ↓ Telegram Bot API
              user (allowlisted chat)
```

## 3. Source-of-truth for the OS

The canonical Personal OS markdown lives in **two** places:

| Location | Role |
|---|---|
| `%USERPROFILE%\OneDrive\.vscode\.me` on the laptop | Your editor's working copy. This is where you read, edit, and grep. |
| `personal-os/` container in `stmindmeymcpt` | What the Function App reads at briefing time. Cloud-side authoritative copy. |

The two are kept in sync by `scripts/local/sync_os_to_blob.py`, which you run
**when** you've made edits worth getting into tomorrow's briefing. Not on a
timer. The Function App does not care whether you ran it today, yesterday, or
last week — it always reads whatever is currently in the container.

Open question (not solved today): edit workflow as you become more nomadic.
The cleanest evolution is probably one of:
- Wire `sync_os_to_blob.py` into a VS Code `tasks.json` and a keyboard shortcut.
- Hang it off a git pre-push hook once `.me` becomes a private GitHub repo.
- Replace it entirely with VS Code's "Azure Storage" extension as the editor target.

Pick when motivated; doesn't affect the runtime architecture.

## 4. Security model

- **Container is private.** No public access. No anonymous read.
- **Access is RBAC-only.** Two principals have data-plane roles on the storage
  account:
  - The Function App's user-assigned MI `id-mindme` —
    `Storage Blob Data Contributor` + `Storage Blob Data Owner`.
  - The signed-in human (you) — `Storage Blob Data Contributor`, for running
    `sync_os_to_blob.py` from your laptop.
- **Microsoft-managed keys** encrypt the container contents at rest (default
  for Azure Storage). No customer-managed key configured.
- **No SAS, no account keys.** All access goes through Entra-issued tokens.
- **Bot token + Telegram webhook secret** are still in Key Vault; the Function
  App reads them via `@Microsoft.KeyVault(...)` app-setting references.

## 5. Re-enabling app-layer encryption (if the trade stops being acceptable)

Restore the original guarantee without abandoning the new flow:

1. Pick a content-key scheme:
   - **Per-file**: encrypt each `.md` at `sync_os_to_blob.py` upload time.
     Function decrypts on read. Highest blast-radius reduction.
   - **Bulk**: encrypt the entire snapshot JSON only at the Function's response
     boundary (after `_build_briefing_snapshot()`). Cheap but the OS is still
     plaintext in blob — only the agent-facing payload is encrypted.
2. Store the key as `personal-os-encryption-key` in `kv-mindme-ymcpt`.
3. Re-add `azure-keyvault-secrets` and `cryptography` to
   `harness/requirements.txt` and the local venv.
4. Update `sync_os_to_blob.py` and `harness/function_app.py` to encrypt /
   decrypt around the AES-GCM boundary.

The original `scripts/local/briefing_builder.py` (kept in-repo, marked
deprecated) is a working reference for the AES-GCM pieces.

## 6. What's still on the laptop

Honest list:

- Editing the OS markdown (inherent — you have to edit somewhere).
- Running `sync_os_to_blob.py` after edits.
- `az login` cache for the signed-in user (only used for the sync script —
  the Function App uses its own MI, not your CLI creds).

What's **not** on the laptop anymore:

- ~~Daily Task Scheduler entry~~
- ~~`briefing_builder.py` running at 07:25~~
- ~~Any timing-sensitive dependency between laptop state and the morning briefing~~

## 7. Status

| Phase | Status | Notes |
|---|---|---|
| 1. Foundation | ✅ complete (2026-05-11) | Repo, Foundry project, Telegram bot, end-to-end ping |
| 2. Morning briefing | 🟡 in progress | Cloud-native rewrite complete locally (2026-05-16). Awaiting Function App deploy + 07:30 observation. |
| 3. Quick capture | ⏳ | |
| 4. Stabilize | ⏳ | Foundry evals, prompt optimizer |

## 8. Tracing (added 2026-05-17, agentFlow Phase 1)

mindMe ships OpenTelemetry traces to Application Insights via the
`azure-monitor-opentelemetry` distro. The Foundry portal Tracing tab and AI
Toolkit local viewer both consume the same data. This is the visualization
substrate for the `agentFlow` web app (`agentflow.naurolabs.com`).

### 8.1 Wiring

- `mindMe/harness/function_app.py` calls `configure_azure_monitor()` at module
  load when `APPLICATIONINSIGHTS_CONNECTION_STRING` is present (always true in
  Azure; never set locally unless you want to ship local traces to App
  Insights).
- The connection string comes from the App Insights component declared in
  `infrastructure/main.bicep` and is injected as an app setting.
- The Function App's user-assigned managed identity already has
  `Monitoring Metrics Publisher` on the App Insights resource — no extra RBAC.
- The Foundry project is linked to the same App Insights resource via the
  portal Tracing tab. Both prompt-agent invocations and our Function App spans
  appear in the same end-to-end timeline.

### 8.2 Span tree

A complete morning briefing produces:

```
morning_briefing_timer            (Functions runtime root span)
└─ ask_companion                  (manual span)
   └─ openai.responses.create     (azure-core auto-span)
      └─ tool_briefing_context    (Functions runtime root span — separate trace)
         └─ build_briefing_snapshot
            └─ BlobClient.download_blob × N  (azure-core auto-span)
   └─ tool_weather                (Functions runtime root span — separate trace)
      └─ weather.fetch            (manual span)
└─ telegram.send                  (manual span)
```

Tool calls are root spans on their own traces because Foundry invokes them as
separate HTTP requests; the Tracing UI stitches them by `conversation.id` and
timestamp.

### 8.3 Span attribute inventory

| Span | Safe attributes | NEVER include |
|---|---|---|
| `ask_companion` | `input.length`, `output.length`, `has_conversation_id`, `agent.name` | prompt text, completion text, conversation history |
| `build_briefing_snapshot` | `dashboard.length`, `journal.length`, `areas.count` | dashboard text, journal text, area titles, goal titles |
| `telegram.send` | `chat_id`, `message.length`, `http.status_code` | URL (contains bot token), message body |
| `weather.fetch` | `location`, `http.status_code` | full response body |

If you add a span, add a row here. If you can't justify each attribute under
[AGENTS.md Hard Rule 1](../AGENTS.md), drop it.

### 8.4 Content suppression (defense in depth)

Three independent mechanisms keep personal content out of telemetry:

1. **Disabled auto-instrumentation.** `OTEL_PYTHON_DISABLED_INSTRUMENTATIONS=httpx,requests,urllib,urllib3,aiohttp-client` is set in code at module-top AND as an app setting in `main.bicep`. HTTP auto-instrumentors capture full URLs; Telegram URLs contain the bot token; therefore no HTTP auto-instrumentation. We emit manual spans instead.
2. **Disabled GenAI content capture.** `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=false` is set the same two ways. OpenAI/Foundry semantic-convention auto-instrumentation captures prompts/completions by default — we don't.
3. **Manual spans only.** Every span we emit is declared in `function_app.py` with an explicit attribute list (table §8.3). No `record_exception(exc)` calls that might serialize an LLM payload into an error message.

### 8.5 Viewing traces

- **Production (Foundry portal):** Foundry project → **Tracing** tab. Filter by `cloud_RoleName == "func-mindme-…"` for harness spans or by agent name for prompt-agent spans.
- **Production (App Insights):** End-to-end transaction details show the full span tree. KQL: `traces | where cloud_RoleName == "func-mindme-…"`.
- **Local dev (AI Toolkit):** Set `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318` in `local.settings.json`, run AI Toolkit's local OTLP viewer, then `func start`. Spans appear in the Tracing webview without round-tripping to Azure.

### 8.6 Audit

Before declaring a tracing change shipped: export 24h of trace JSON from App
Insights, grep for known personal strings (a family member name, a journal
phrase from yesterday). Zero hits is the acceptance criterion. Re-run after
every span addition.
