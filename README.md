# mindMe

> *Mind me, so I can mind what matters.*

A personal AI agent that operates on my [Personal OS (mindVault)](https://github.com/samoletovs/mindVault) to reduce daily friction. Reads my dashboard and journal, sends a morning briefing, captures thoughts to my inbox, and stays out of the way the rest of the time.

**Name:** *mindMe* — imperative "mind me" (attend to me, look after me) layered with the camelCase compound "my mind, externalized". A second mind, attentive to one. Renamed from `comes` on 2026-05-11.

---

## What it does (v1 scope)

| Capability | Trigger | Channel |
|---|---|---|
| **Morning briefing** | Timer at 07:30 daily | Telegram DM |
| **Quick capture** | I text the bot | Telegram → `inbox/inbox.md` in Personal OS |
| **Status / help** | `/status`, `/help` | Telegram slash commands |
| **Briefing customization** | `/briefing [sections]` | Telegram slash command → `system/mindme/briefing-prefs.json` in `personal-os/` |

Anything beyond is v2+. See [Out of scope](#out-of-scope-v1).

---

## Architecture

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
│  Storage Account                                 │
│   ├─ personal-os/      ← OS markdown mirror      │
│   ├─ briefing-context/ ← LEGACY, no longer read  │
│   └─ Queue (Phase 3)                             │
│  Key Vault   (bot token; legacy encryption key)  │
│  App Insights                                    │
│                                                  │
│  Function App (Python, Flex Consumption)         │
│   ├─ telegram_webhook        (HTTP)              │
│   ├─ morning_briefing_timer  (07:30 CRON)        │
│   ├─ capture_drain           (Queue trigger)     │
│   ├─ tool_briefing_context   (HTTP, agent tool)  │
│   │     reads personal-os/ blobs, builds         │
│   │     sanitized JSON in-process                │
│   └─ tool_weather            (HTTP, agent tool)  │
│         │                                        │
│         ↓ calls                                  │
│  Foundry project: mindMe                         │
│   └─ Hosted agent: companion                     │
└──────────────────────────────────────────────────┘
           ↑↓
┌─────────────────────┐
│ Telegram bot        │
│ allowlist: [me only]│
└─────────────────────┘
```

**Key principle (revised 2026-05-16):** the morning briefing pipeline runs
entirely in Azure — no laptop required at run-time. The Personal OS markdown is
mirrored to a **private** blob container (`personal-os/`) guarded by
managed-identity RBAC and Microsoft-managed at-rest encryption. The previous
application-layer AES-GCM briefing blob is now legacy only. See
`docs/architecture.md` for the trade-off discussion and the current security
boundary.

---

## Repo layout

```
mindMe/
├── agent/                    # Foundry agent-facing tool schema / metadata
│   └── openapi-tools.json
├── harness/                  # Azure Functions (Telegram receiver + timers)
│   ├── README.md
│   ├── requirements.txt
│   ├── host.json
│   └── function_app.py
├── infrastructure/           # Bicep deployment definitions
│   ├── main.bicep
│   └── main.bicepparam
├── scripts/
│   ├── dev/                  # local bootstrap + smoke-test helpers
│   │   ├── create_agent.py
│   │   ├── smoke_agent.py
│   │   └── telegram_bridge.py
│   └── local/                # runs on laptop on demand
│       ├── briefing_builder.py
│       ├── sync_os_to_blob.py
│       └── test_briefing_snapshot.py
├── docs/
│   ├── architecture.md
│   ├── deploy.md
│   ├── personal-os-azure-plan.md
│   └── phase2-design.md
```

Layout borrows patterns from [`agentMode`](https://github.com/samoletovs/agentMode) and [`foundryLab`](https://github.com/samoletovs/foundryLab), but this repo currently keeps the runnable cloud logic almost entirely in `harness/function_app.py`.

---

## Status

Phase 1 complete (2026-05-11). Phase 2 cloud-native rewrite landed 2026-05-16
(see [docs/architecture.md](docs/architecture.md) for the trade-off decision
that drove it). See planning doc in Personal OS:
`projects/2026-personal-agent-foundation/`.

| Phase | Status | Deliverable |
|---|---|---|
| 1. Foundation | ✅ complete | Repo, Foundry project, Telegram bot, end-to-end smoke test (ping → pong via Foundry) |
| 2. Morning briefing | 🟡 in progress | Function App reads `personal-os/` blob, builds snapshot, calls agent, sends Telegram. Awaiting Function App deploy + first 07:30 observation. |
| 3. Quick capture | ⏳ | Capability #2 working end-to-end |
| 4. Stabilize | ⏳ | Foundry evals, prompt optimizer, foundryLab cross-link |

Target ship date for v1: **2026-06-15**.

---

## Operating constraints

- **Cost cap:** €5–10/month. Azure budget alert set at €8.
- **Model:** `gpt-4o-mini` only.
- **Region:** `swedencentral` (matches foundrylab-rg).
- **Identity:** separate Telegram bot from `agentMode`. Allowlist of one. Never has access to family chat or Microsoft work account.

---

## Out of scope (v1)

Voice, vision, multi-agent orchestration, family-context bridge, long-term memory, calendar integration, web browsing, RPA. All deferred to v2+.

---

## Related

- **[samoletovs/mindVault](https://github.com/samoletovs/mindVault)** — Personal OS this agent operates on (private). Synced zone; sensitive tier stays in the OneDrive `.me` vault.
- **[samoletovs/agentMode](https://github.com/samoletovs/agentMode)** — Family agent. Pattern reference.
- **[samoletovs/foundryLab](https://github.com/samoletovs/foundryLab)** — Foundry research lab. May absorb a port of `mindMe` as agent #6 for comparison.
- **[samoletovs/naurolabs](https://github.com/samoletovs/naurolabs)** — Landing page catalog.
