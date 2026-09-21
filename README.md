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
| **Weekly review** | `/review`; Sunday **18:00 UTC** | With action briefing enabled: one decision brief, dated follow-through and up to three individually approved actions. Otherwise the original checklist/nudge |
| **Briefing customization** | `/briefing [sections]` | Telegram slash command → `system/mindme/briefing-prefs.json` in `personal-os/` |
| **Vault questions** | Ordinary conversation | Foundry can list recent research/notes/ideas/wiki files and read one allowed markdown file |
| **Deep research** | `/dig <question>` | Creates a research issue in mindVault; downstream Copilot workflows produce the report |
| **Workflow housekeeping** | Every 30 minutes | Dispatches vault reaper workflows when candidate work exists |

Ordinary companion conversation remains **single-turn**: it does not remember the
previous chat message. The action-briefing workflow below has scoped durable
decisions and approval-gated task updates. There is no semantic search, calendar
integration or autonomous task scheduling. `/task` captures an action; it does not schedule it.
Onboarding, `/start`, `/ping`, and `/help` are also available.

## Opt-in action briefing

The [action-briefing implementation](docs/design-action-briefing.md) adds a separate,
feature-gated loop. It is **enabled in the existing deployment and live-verified
on 2026-09-13**; a fresh code checkout alone does not enable it:

- Canonical, revision-pinned goal/knowledge context and date-aware task state.
- Source-linked proposals; owner approval before research, task creation or edits.
- Proposal-specific text/voice replies, corrections, declines and dated snoozes.
- Private decision receipts, retry-safe task handoff and read-only research-result
  reconciliation; no claim that a submitted PR is already a completed task.
- `/briefing now`, `/briefing details`, `/proposals`, `/memory`, and `/memory forget <id>` when enabled.
  Reply directly to the proposal message. Bare unrelated conversation remains
  single-turn, and existing capture/review commands are unchanged.

Proposal cards and their "Why this?" explanations use calm Telegram formatting:
bold section headings, short paragraphs, and named source links without link
previews. Internal IDs stay out of the message body; a single suggestion has no
"1 of 1" counter. Text, voice, and button requests for an explanation share the
same formatting. Long explanations continue in complete sections rather than
dropping the action or rationale. Explanations are read-only: approval, edits,
corrections, and snoozes still target the original proposal message.

See [rollout and acceptance](docs/deploy.md#action-briefing-rollout) for configuration,
privacy controls and verified release evidence. No new schedule or service was added.

The model's 24-source packet reserves evidence for changes and goals before filling with
tasks. Omitted records are disclosed, and model-generated focus, changes, and proposals
must cite a source actually included in that packet. Due-task selection remains
deterministic and is not restricted to the model's source budget.

### A one-minute morning read

The action-enabled morning overview uses Telegram headings, named source links
and no link previews. It is capped at 190 visible words and 3,200 UTF-16 units
including HTML, with one focus and up to two other date-relevant tasks. A hard
deadline within seven days takes priority over an old waiting review. The focus
task is not repeated in the other-dates list; review dates are labelled separately
from deadlines. Useful source updates and optional weather follow only when they fit.

`/briefing details` reads the **current** source view without generating a plan,
changing preferences, advancing delivery state or executing work. It exposes the
full due-task inventory returned by the source, complete next-action wording,
source limitations and current personal-snapshot signals. Omitted task counts are
explicit in the overview; an abbreviated summary is not a claim that other work
does not exist. Changes not shown do not advance presented-source fingerprints.

Out-of-date or undated personal snapshots are labelled and excluded from both
recommendations and routine counts. This does not prevent recommendations based
on current connected notes. No private snapshot is refreshed automatically.

At most one separately bound action card follows. **Start research**, **Draft task**
and **Select next step** say what approval does; selecting a step does not perform
or complete it. The exact proposed action, rationale and scope are on the card.
Research/task results remain submitted until verified. Nothing starts during
briefing generation, and repeated approvals cannot start duplicate work.

### Weekly decisions, not a second status report

With the action briefing enabled, `/review` and the existing Sunday timer use the
same review flow. One formatted message explains the evidence limitations, observed
changes and recommended priority, followed by **at most three** separate action
cards. A card names its exact scope and links its source; selecting a next step
does not complete a task, and opening a research issue or task PR is not a verified
result. Reply to that card to approve, dismiss, revise (`change: ...`) or defer
(`snooze YYYY-MM-DD`). There is no blanket approval.

Use `/review sources` for a read-only source check: candidate/read/selected counts,
all evidence limits, task availability, last delivered weekly baseline, and private
GitHub link help. This check uses no model and does not advance the comparison,
reconcile actions, change decisions, or sync the private snapshot. A bounded read
is reported as incomplete coverage, not missing notes. The private snapshot is
synced manually from the verified laptop folder; see [the sync guide](scripts/local/README.md).
Source links require the GitHub account with access to the private vault, including
inside Telegram's browser; a signed-out browser can return 404 for a valid link.

The weekly comparison has its own baseline, independent of morning briefings.
Follow-through reports dated observations in a maximum seven-day window, never
lifetime status counts presented as this week's accomplishments. Old receipts
without dates are not backfilled. A result first reconciled today is described as
**verified today**, not necessarily completed today. `/proposals all` includes
the recorded transition dates and result links.

Private-mirror freshness is checked separately from current connected sources.
The weekly model never receives private-mirror counts or journal facts; stale
mirror data cannot manufacture urgency or claims of inactivity. No private sync
or additional source upload is performed by the review.

Repeated requests for an unchanged snapshot do not regenerate or resend it.
Confirmed messages are checkpointed individually. If a send's outcome is
uncertain, `/review retry` explicitly permits repeating that message; it does not
repeat approved work. Source versions and owner-only approval bindings still
apply. This release adds no service, schedule or model deployment.

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
