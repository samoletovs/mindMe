# mindMe

> *Mind me, so I can mind what matters.*

A personal AI agent that operates on my [Personal OS](https://github.com/samoletovs/me) to reduce daily friction. Reads my dashboard and journal, sends a morning briefing, captures thoughts to my inbox, and stays out of the way the rest of the time.

**Name:** *mindMe* — imperative "mind me" (attend to me, look after me) layered with the camelCase compound "my mind, externalized". A second mind, attentive to one. Renamed from `comes` on 2026-05-11.

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
application-layer AES-GCM encryption was dropped because (a) the container is
private and (b) the SA's MMK encryption already covers the at-rest threat. See
`docs/architecture.md` for the trade-off discussion and how to re-enable
app-layer encryption if you change your mind.

---

## Repo layout

```
mindMe/
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

Phase 1 complete (2026-05-11). Phase 2 cloud-native rewrite landed 2026-05-16
(see [docs/architecture.md](docs/architecture.md) for the trade-off decision
that drove it). See planning doc in Personal OS:
`01_projects/2026-personal-agent-foundation/`.

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

- **[samoletovs/me](https://github.com/samoletovs/me)** — Personal OS this agent operates on (private)
- **[samoletovs/agentMode](https://github.com/samoletovs/agentMode)** — Family agent. Pattern reference.
- **[samoletovs/foundryLab](https://github.com/samoletovs/foundryLab)** — Foundry research lab. May absorb a port of `mindMe` as agent #6 for comparison.
- **[samoletovs/naurolabs](https://github.com/samoletovs/naurolabs)** — Landing page catalog.
