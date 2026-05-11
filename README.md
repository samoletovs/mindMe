# comes

> *"Can a quiet companion run a life?"*

A personal AI agent that operates on my [Personal OS](https://github.com/samoletovs/me) to reduce daily friction. Reads my dashboard and journal, sends a morning briefing, captures thoughts to my inbox, and stays out of the way the rest of the time.

**Latin:** *comes* — companion at the table. A trusted figure who travels with you.

---

## What it does (v1 scope)

| Capability | Trigger | Channel |
|---|---|---|
| **Morning briefing** | Timer at 07:30 daily | Telegram DM |
| **Quick capture** | I text the bot | Telegram → `00_inbox/inbox.md` in Personal OS |
| **Status / help** | `/status`, `/help` | Telegram slash commands |

Anything beyond is v2+. See [Out of scope](#out-of-scope-v1).

---

## Architecture

```
┌─────────────────────┐
│ Laptop (c:\vsCode)  │
│  - .me (Personal OS)│  07:25 cron → builds briefing-context.json
│  - briefing_builder │           → encrypts (AES-GCM)
│  - capture_sync     │           → uploads to Blob
└──────────┬──────────┘
           │
           ↓ (encrypted, overwritten daily)
┌─────────────────────────────────────────────┐
│ Azure (foundrylab-rg, swedencentral)        │
│                                             │
│  Storage Account (Blob + Queue + Table)     │
│  Key Vault (bot token, encryption key)      │
│  App Insights                               │
│                                             │
│  Function App (Python, Consumption)         │
│   ├─ telegram_webhook (HTTP)                │
│   ├─ morning_briefing_timer (07:30 CRON)    │
│   └─ capture_drain (Queue trigger)          │
│       │                                     │
│       ↓ calls                               │
│  Foundry project: comes-personal            │
│   └─ Hosted agent: companion                │
│       ├─ get_briefing_context()             │
│       ├─ get_weather()                      │
│       └─ ack_capture()                      │
└─────────────────────────────────────────────┘
           ↑↓
┌─────────────────────┐
│ Telegram bot        │
│ allowlist: [me only]│
└─────────────────────┘
```

**Key principle:** personal markdown NEVER lives unencrypted in the cloud. The briefing-context blob is the only personal data that touches Azure, and it's:
- Sanitized (only what the agent needs)
- AES-GCM encrypted with key in Key Vault
- Overwritten daily (no history)

---

## Repo layout

```
comes/
├── .foundry/                 # Foundry agent metadata (per microsoft-foundry skill)
│   └── agent-metadata.yaml
├── agent/                    # Foundry hosted agent (Python)
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── src/
│   │   ├── main.py
│   │   └── tools/
│   └── tests/
├── harness/                  # Azure Functions (Telegram receiver + timers)
│   ├── host.json
│   ├── requirements.txt
│   ├── telegram_webhook/
│   ├── morning_briefing_timer/
│   └── capture_drain/
├── infrastructure/           # Bicep
│   ├── main.bicep
│   ├── main.bicepparam
│   └── modules/
├── scripts/
│   ├── local/                # runs on laptop (Task Scheduler)
│   │   ├── briefing_builder.py
│   │   └── capture_sync.py
│   └── deploy.ps1
├── docs/
│   ├── architecture.md
│   ├── foundry-learnings.md
│   └── operations.md
└── tests/
```

Layout mirrors [`agentMode`](https://github.com/samoletovs/agentMode) and [`foundryLab`](https://github.com/samoletovs/foundryLab).

---

## Status

Phase 1 in progress (2026-05-10). See planning doc in Personal OS: `01_projects/2026-personal-agent-foundation/`.

| Phase | Status | Deliverable |
|---|---|---|
| 1. Foundation | 🟡 in progress | Repo, infra, Foundry project, Telegram bot, smoke test (ping → pong) |
| 2. Morning briefing | ⏳ | Capability #1 working end-to-end |
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

- **[samoletovs/me](https://github.com/samoletovs/me)** — Personal OS this agent operates on (private)
- **[samoletovs/agentMode](https://github.com/samoletovs/agentMode)** — Family agent. Pattern reference.
- **[samoletovs/foundryLab](https://github.com/samoletovs/foundryLab)** — Foundry research lab. May absorb a port of `comes` as agent #6 for comparison.
- **[samoletovs/naurolabs](https://github.com/samoletovs/naurolabs)** — Landing page catalog.
