# mindMe

> *Mind me, so I can mind what matters.*

A single-user Telegram companion for a [Personal OS (mindVault)](https://github.com/samoletovs/mindVault). It sends briefings, hands captures to memex, reads selected vault material, and starts research requests. Its success criterion is calmer mornings, not more messages.

**Name:** *mindMe* — imperative "mind me" (attend to me, look after me) layered with the camelCase compound "my mind, externalized". A second mind, attentive to one. Renamed from `comes` on 2026-05-11.

---

## What it does now

| Capability | Trigger | Channel |
|---|---|---|
| **Morning briefing** | Daily at **07:30 UTC** | Selected dashboard text, previous day's journal summary, vault counts, open ideas/tasks, optional weather |
| **Quick capture** | `/note`, `/idea`, `/task`, `/diary`, capture prefixes, or a URL | Forwarded to **memex**, which owns storage and review; ordinary text is conversation, not capture |
| **Voice capture** | Voice/audio message | Forwarded to memex; local transcription requires `AZURE_OPENAI_WHISPER_DEPLOYMENT` |
| **Vault status / daily summary** | `/status`, `/summary` | Counts, focus, journal summary, and mirror-freshness warnings |
| **Weekly review** | `/review`; Sunday **18:00 UTC** nudge | A checklist and current counts, not an interactive review or task completion |
| **Briefing customization** | `/briefing [sections]` | Telegram slash command → `system/mindme/briefing-prefs.json` in `personal-os/` |
| **Vault questions** | Ordinary conversation | Foundry can list recent research/notes/ideas/wiki files and read one allowed markdown file |
| **Deep research** | `/dig <question>` | Creates a research issue in mindVault; downstream Copilot workflows produce the report |
| **Workflow housekeeping** | Every 30 minutes | Dispatches vault reaper workflows when candidate work exists |

The companion is currently **single-turn**: it does not remember the previous chat
message. It has no semantic search, calendar integration, autonomous reminders, or
complete/do-later task workflow. `/task` captures an action; it does not schedule it.
Onboarding, `/start`, `/ping`, and `/help` are also available.

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
│   └─ legacy capture queue (not the active flow)  │
│  Key Vault   (bot token; legacy encryption key)  │
│  App Insights                                    │
│                                                  │
│  Function App (Python, Flex Consumption)         │
│   ├─ telegram_webhook        (HTTP)              │
│   ├─ morning_briefing_timer  (07:30 UTC)         │
│   ├─ weekly_review_timer    (Sun 18:00 UTC)      │
│   ├─ reaper_poll_timer      (every 30 minutes)   │
│   ├─ capture forwarding → memex                │
│   ├─ tool_briefing_context   (HTTP, agent tool)  │
│   │     reads personal-os/ blobs, builds         │
│   │     selected context JSON in-process         │
│   ├─ tool_weather            (HTTP, agent tool)  │
│   └─ vault_recent / vault_read → mindVault      │
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
boundary. Agent tools require a Function key supplied through a Foundry project
connection; only the health probe and secret-verified Telegram webhook are anonymous.
The snapshot selects fields; it is **not a general-purpose personal-data sanitizer**.
Dashboard bullets and area/project titles can reach the companion and Telegram.

---

## Repo layout

```
mindMe/
├── agent/                    # Foundry agent-facing tool schema / metadata
│   └── openapi-tools.json
├── harness/                  # Azure Functions (Telegram receiver + timers)
│   ├── README.md
│   ├── requirements.txt
│   ├── requirements-dev.txt  # pytest deps for the unit tests
│   ├── host.json
│   ├── function_app.py
│   └── tests/                # `python -m pytest` (see harness/README.md)
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

The Function App is deployed; see [the deployment record](docs/deploy.md).
The September 2026 review found that healthy HTTP responses did not establish
end-to-end usefulness. The private mirror's sync manifest was last modified
**2026-07-30**, and the queried seven-day telemetry contained no recorded briefing
sends. That is missing evidence of delivery, not proof that every briefing failed.

Reliability guards now cover authenticated tools, retryable capture failures,
complete delivery of long Telegram replies, yesterday's journal, explicit
unavailable states, section-safe fallbacks, and mirror freshness. A mirror two
calendar days old is labelled stale. Local sync compares content hashes and refuses
to publish a successful manifest after file/transfer errors.

No personal data was refreshed as part of that review. The mirror remains
**upload-only**: deleting a local file does not delete its cloud copy. After the
next successful sync, the source inventory excludes retained, deleted files from
runtime reads. Legacy manifests without an inventory cannot certify freshness.

---

## Operating constraints

- **Cost cap:** €5–10/month. Azure budget alert set at €8.
- **Model:** `gpt-4o-mini` only.
- **Region:** `swedencentral` (matches foundrylab-rg).
- **Identity:** separate Telegram bot from `agentMode`. Allowlist of one. Never has access to family chat or Microsoft work account.

---

## Out of scope (v1)

Vision, multi-agent orchestration, family-context bridge, long-term memory,
calendar integration, and RPA remain outside the current scope. Voice forwarding
and research requests are already implemented.

## What would make it valuable

1. **Fresh, approved inputs.** Decide which non-sensitive mindVault files should
   feed the briefing; do not automatically expand uploads from the sensitive vault.
   Add reliable event-driven refresh and an explicit deletion policy.
2. **Close a loop, not just capture it.** Surface at most three actionable items,
   with Done / Later / Drop actions persisted by memex. Show overdue work, not
   just the nearest future deadline.
3. **A genuinely conversational review.** Keep short-lived chat context with an
   explicit reset and retention policy, then guide one weekly-review step at a time.
4. **Make delivery observable.** Record last attempted/delivered briefing,
   source age, capture acceptance, and downstream completion separately.
   Choose the owner's local timezone explicitly; current timers use UTC.
5. **Prove value before adding RAG or more agents.** Run a two-week trial:
   useful briefing on at least 10 of 14 days, zero silently lost captures,
   at least five tasks closed from Telegram, and less than one minute of
   daily inbox maintenance. If that fails, simplify the loop rather than
   adding more generated text.

---

## Related

- **[samoletovs/mindVault](https://github.com/samoletovs/mindVault)** — Personal OS this agent operates on (private). Synced zone; sensitive tier stays in the OneDrive `.me` vault.
- **[samoletovs/agentMode](https://github.com/samoletovs/agentMode)** — Family agent. Pattern reference.
- **[samoletovs/foundryLab](https://github.com/samoletovs/foundryLab)** — Foundry research lab. May absorb a port of `mindMe` as agent #6 for comparison.
- **[samoletovs/naurolabs](https://github.com/samoletovs/naurolabs)** — Landing page catalog.
