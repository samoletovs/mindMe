# Personal OS on Azure — channels by sensitivity

> Status: **build-ready plan** for the next mindMe capability.
> Revised 2026-06-01 to match the deployed architecture and the channel-split
> decision (Telegram = capture + notify; GitHub/VS Code = read).
> Supersedes the original generic proposal (Container Apps + PostgreSQL + Redis),
> which never reflected what was actually built. The real stack is documented in
> [architecture.md](architecture.md) and [phase2-design.md](phase2-design.md).

## What this plan is (and is not)

This is **not** a greenfield "stand up a personal OS on Azure" plan — that work
is largely done. mindMe already runs in `foundrylab-rg` (swedencentral), reads
the Personal OS markdown from a private blob container, and answers Telegram
messages through the `companion` Foundry agent.

This plan settles the **interface question** — *which channel touches which
data* — and then defines the next build step that follows from it.

### The decision: split channels by sensitivity

Regular Telegram chats are **not** end-to-end encrypted. Bot conversations are
stored in plaintext on Telegram's servers, and the allowlist trusts a single
Telegram chat ID — so an account takeover would turn the bot into a
data-exfiltration tool the moment it can read the full `.me`. That makes
Telegram the wrong place to *retrieve* personal content.

So we split by sensitivity:

| Channel | Direction | Sensitivity | Use |
|---|---|---|---|
| **Telegram / mindMe** | write + push | low | Quick captures **in**; briefings & notifications **out** |
| **GitHub mobile + VS Code** | read | high | Browse / search the full `.me`, behind your own Microsoft/GitHub identity + 2FA |

In one line: **mindMe is the ears and mouth (capture + notify); GitHub/VS Code
are the eyes (read).** Rich, conversational recall over Telegram is **deferred**
behind explicit privacy controls — built only if the "librarian you talk to"
experience proves worth the exposure.

Why this is the right default:

- **Removes the worst exposure.** Recalled personal content never lands in
  Telegram's plaintext cloud store, because rich reading doesn't go through
  Telegram.
- **Reuses auth you already trust.** GitHub mobile and VS Code sit behind your
  GitHub/Microsoft identity with 2FA — no new front-end to secure, no new
  allowlist.
- **`.me` is already a git repo.** Reading on the phone is just browsing the
  **private** GitHub repo (GitHub's repo search works on markdown today), so
  "make `.me` accessible" on the read side is mostly free.
- **Shrinks the build.** mindMe's job narrows to *capture + notify*, which is
  largely built. The conversational-recall tool (`search_os`) becomes optional
  and deferred rather than the centerpiece.

The trade-off, stated plainly: you lose *conversational* recall on mobile
("summarize my payArc pricing decisions") in exchange for the privacy win. You
keep *manual* recall (browse/search the repo). And `.me` now also lives in a
GitHub cloud copy — acceptable because it's a **private** repo behind your 2FA
(a stronger custodian than Telegram's chat store), but it must stay private with
no collaborators.

## Where we are now (verified 2026-06-01)

### Deployed resources (tag `project=mindMe`, `foundrylab-rg`, swedencentral)

| Resource | Type / SKU | Role |
|---|---|---|
| `func-mindme-ymcpt` | Function App, Flex Consumption (FC1) | Telegram webhook, briefing timer, agent tools |
| `plan-mindme-ymcpt` | Server farm, FC1 FlexConsumption | Hosts the Function App |
| `stmindmeymcpt` | Storage, Standard_LRS (StorageV2) | `personal-os/` markdown mirror, `capture-events` queue |
| `kv-mindme-ymcpt` | Key Vault, Standard | Bot token, webhook secret, (legacy) encryption key |
| `log-mindme` | Log Analytics, PerGB2018, 30-day, 1 GB/day cap | Telemetry store |
| `appi-mindme` | App Insights, workspace-based | Traces / metrics / logs |
| `id-mindme` | User-assigned managed identity | Auth for Storage, Key Vault, Foundry |
| Foundry project `mindMe` | Reuses `foundrylab-aiservices`, `gpt-4o-mini` | The `companion` agent |

> The original plan's Container Apps + PostgreSQL + Redis are **not deployed and
> not needed.** mindMe is a single-user agent reading a few hundred markdown
> files; a relational DB and a cache server would be cost and operational
> overhead with no payoff. If full-text or vector search later proves it needs a
> backing index, see [§ Retrieval options](#retrieval-options) — even then the
> answer is a managed index, not a PostgreSQL server.

### What already works

- **Interactive chat** — `telegram_webhook` forwards free text to `_ask_companion`
  ([harness/function_app.py](../harness/function_app.py)). Allowlist of one
  (Hard Rule 2). `/ping`, `/status`, `/help` handled locally.
- **Morning briefing** — `morning_briefing_timer` at 07:30 Sweden time asks the
  agent to compose a briefing using `get_briefing_context` + `get_weather`.
- **Agent tools** — `tool_briefing_context` (shallow daily snapshot) and
  `tool_weather`.
- **Security baseline** — managed-identity RBAC everywhere, no SAS / account
  keys, Key Vault references for secrets, Telegram webhook secret header,
  OpenTelemetry content suppression (Hard Rules 1, 8, 9).

### The gap

`tool_briefing_context` builds a **fixed, shallow** snapshot in
`_build_briefing_snapshot`: `_dashboard.md` bullets, yesterday's journal
summary, and the H1 of each `02_areas/*/README.md`. There is **no way to query
arbitrary `.me` content**. The agent cannot search projects, past journals,
notes, decisions, or anything outside the four hardcoded slices.

## The next capability: tighten the capture + notify loop

Given the channel split, the **read** side is largely solved for free (browse /
search the private `.me` repo in GitHub mobile or VS Code). So the next build
focuses on the two things that *do* run through mindMe — and are *safe* to —
namely **capture** (quick thoughts in) and **notify** (briefings + nudges out).

**Goal:** from my phone, I can fire a quick capture into `.me` in one message,
and I receive a useful morning briefing plus timely nudges — all without rich
personal content sitting in Telegram's cloud.

### Capture (write path)

A Telegram message that isn't a command becomes a capture: the harness enqueues
it on `capture-events`, and a queue-triggered function appends it to the right
place in `.me` (e.g. `05_journal/YYYY/YYYY-MM-DD.md` or an inbox file). Keep
captures short, and **do not echo the full stored content back** — acknowledge
with a minimal confirmation ("captured ✓") so personal text isn't duplicated
into the chat history.

```
Telegram message ("idea: payArc — tier pricing by merchant volume")
   → telegram_webhook → enqueue capture-events
        → queue-trigger appends to .me inbox/journal (blob mirror)
   → _telegram_send ("captured ✓")  # no content echo
```

> The write path lands in the **blob mirror**. Syncing the blob mirror back to
> the laptop `.me` repo — and from there to the **private GitHub repo** you read
> on mobile — is what closes the loop. See [§ Freshness & the read repo](#freshness--the-read-repo).

### Notify (push path — already mostly built)

- **Morning briefing** at 07:30 (deployed) — keep it low-sensitivity: surface
  *pointers and counts* ("3 open loops in `turgo`, deadline Friday"), not
  verbatim private content, since briefings persist in Telegram's cloud.
- **Nudges** — optional timer/event-driven reminders (deadlines approaching,
  open-loop count rising). Same low-sensitivity rule.

### Freshness & the read repo

The channel split adds a requirement: the data you *read* on GitHub mobile is a
**third copy** (laptop `.me` → blob mirror → GitHub repo). For mobile reading to
be useful, `.me` must be pushed to a **private GitHub repo** on a cadence that
keeps it current. Two ergonomics to build:

- **One-gesture sync** — a VS Code task / keybind (or git hook on `.me`) that
  runs [`sync_os_to_blob.py`](../scripts/local/sync_os_to_blob.py) **and**
  pushes the `.me` git repo, so "sync before I leave the laptop" is one action
  that updates both the agent's mirror and the mobile-read repo.
- **Staleness signal** — surface the last-sync timestamp in `/status` so you
  know how fresh the mobile copy and the agent's mirror are.

## Deferred: conversational recall over Telegram (`search_os`)

This is the capability we are **explicitly not building yet**. It would let you
ask mindMe a free-form question ("summarize my payArc pricing decisions") and
have it read across the full `.me` and answer in chat. It's recorded here so the
design isn't lost — but it stays behind the channel-split decision.

**Build it only if** manual reading via GitHub/VS Code proves too clumsy *and*
you accept the mitigations below (which keep personal content out of Telegram's
cloud as much as a chat channel allows):

- **Pointers, not content** — default replies cite *where* ("found it in
  `02_areas/payArc/pricing.md` — want the detail?"); verbatim excerpts are
  opt-in per query.
- **Sensitivity tiering** — mark some areas/files (e.g. `private:` frontmatter
  or a `99_vault/` prefix) as *never sent to Telegram*; reachable only from the
  laptop/VS Code read path. Caps blast radius on account takeover.
- **Ephemeral replies** — auto-delete the bot's recall messages after N minutes
  to shrink the standing cloud copy.
- **Re-auth gate** — require a time-boxed "unlock" before sensitive recall, so a
  hijacked session can't silently vacuum everything.

If/when it ships, the implementation is a new `search_os` agent tool inside the
existing Function App:

- **Tier 1 (first):** in-process keyword/recency search — `search_os(query,
  k=5, area=None)` lists `personal-os/` blobs (optional prefix), ranks by term
  frequency + recency + path match, returns top-*k* heading-chunked, byte-capped
  excerpts. No new infrastructure, ≈ €0.
- **Tier 2 (only if Tier 1 falls short):** embeddings — Foundry vector store or
  in-blob vectors with `text-embedding-3-small`. **Azure AI Search (~€70+/mo)
  breaks the €5–10 cap and is out of scope** without an explicit decision.

## Security & privacy

### Interface trust boundary (the reason for the split)

- **Telegram is semi-trusted.** Cloud chats are plaintext on Telegram's
  servers; the allowlist trusts a chat ID, so account takeover → bot access.
  Therefore Telegram carries only *low-sensitivity* traffic: short captures in,
  pointer-style briefings out. No full-`.me` retrieval over Telegram (that's the
  deferred `search_os`).
- **Account hardening (do regardless, free):** Telegram 2FA password on, active
  sessions reviewed, carrier SIM-swap PIN set. Highest-impact, lowest-cost fix.
- **Bot token hygiene** — Key Vault only; httpx/httpcore loggers silenced before
  the Telegram client is built (Hard Rule 8); rotate on any suspected leak.
- **GitHub read repo must be private** with no collaborators — it's the
  high-sensitivity read surface and a cloud copy of `.me`.

### Existing guarantees (carried forward)

- **RBAC-only blob access** via `id-mindme`. No SAS, no keys.
- **Allowlist of one** (Hard Rule 2) — never widened without explicit
  confirmation.
- **No content in logs or traces** (Hard Rules 1, 9). The capture path's span
  carries only sizes/counts/durations — **never** the capture text. Any new span
  (capture enqueue, queue drain) gets a row in
  [architecture.md §8.3](architecture.md) with an audited, size-only attribute
  list.
- **Content minimization** — captures aren't echoed back in full; briefings
  prefer pointers over verbatim content.
- **Audit** — export 24 h of traces and grep for a known personal string; zero
  hits is the acceptance criterion ([architecture.md §8.6](architecture.md)).
- **Pre-push leak scan** — `scripts/audit-leaks.ps1` before every push
  (Hard Rule 7).

## Cost — real run-rate (corrected)

The original "$39–165/month" range assumed a stack that was never deployed. The
actual mindMe footprint is far cheaper.

| Service | Driver | Expected / month |
|---|---|---|
| Functions (Flex Consumption) | Very low execution volume; free grant covers it | ~€0 |
| Storage (Standard_LRS) | Tiny capacity, modest transactions | ~€0.50 |
| Key Vault (Standard) | A few hundred ops | ~€0 |
| Log Analytics / App Insights | Light telemetry, 1 GB/day cap, 30-day retention | €0–3 |
| Managed identity | — | €0 |
| Foundry `gpt-4o-mini` | Briefing + occasional capture classification | low single-digit € |
| **Total** | | **~€1–4/month** |

- **Cost cap:** €5–10/month. Budget alert already set at **€8**.
- **Biggest cost risk is telemetry, not compute** — the 1 GB/day Log Analytics
  cap is the guardrail; keep it. The capture + notify loop barely moves token
  cost.
- **Deferred `search_os` Tier 2 with Azure AI Search would break the cap**
  (~€70+/mo) — explicitly gated, not in scope without a decision.
- This is comfortably inside the €150/month Visual Studio Enterprise credit on
  `146099412+samoletovs@users.noreply.github.com`. Verify month-to-date anytime in Azure Portal →
  **Cost Management + Billing** → Cost analysis (scope: mindMe resource group or
  `project=mindMe` tag).

## Build steps (next session)

> Prerequisite: Phase 2 (morning briefing) is deployed and a 07:30 message has
> been observed. If not, finish that first.

1. **Confirm the read repo.** Ensure `.me` is pushed to a **private** GitHub
   repo (no collaborators) and verify browsing/searching it in the GitHub mobile
   app covers your manual-recall needs. This is the read side — mostly config,
   little code.
2. **Harden Telegram (free).** Turn on Telegram 2FA, review active sessions, set
   a carrier SIM-swap PIN.
3. **Capture path.** In [harness/function_app.py](../harness/function_app.py):
   route non-command Telegram text to the `capture-events` queue; add a
   queue-triggered function that appends the capture to the right `.me` file in
   the blob mirror. Acknowledge with `captured ✓` — **no content echo**. Manual
   spans carry sizes/counts only.
4. **Close the sync loop.** One-gesture VS Code task (or `.me` git hook) that
   runs [`sync_os_to_blob.py`](../scripts/local/sync_os_to_blob.py) **and**
   pushes the `.me` repo, so captures land in both the agent mirror and the
   GitHub read repo. Surface `last_sync` in `/status`.
5. **Briefing/nudge polish.** Keep briefings pointer-style (counts + locations,
   not verbatim content). Optionally add deadline/open-loop nudges.
6. **Tests.** Unit-test the capture routing, queue-drain append target, and the
   `no content echo` confirmation in `tests/`. Mock blob + queue — never hit
   Azure in tests.
7. **Telemetry audit.** Deploy, run real captures + a briefing, export 24 h of
   traces, grep for a known personal string → must be zero hits. Add any new
   span rows to architecture.md §8.3.
8. **Update docs.** Architecture.md data-flow + span table, README capability
   table (capture + notify; note recall is manual via GitHub/VS Code), and flip
   this plan's status to "in progress".

## Open questions

- **Read repo cadence** — push `.me` to GitHub on every laptop sync, or on a
  schedule? Tie it to the one-gesture sync so it can't drift.
- **Capture target** — append everything to a single inbox file, or route by
  type (journal vs. project note)? Start with an inbox; refine later.
- **When (if ever) to build `search_os`** — define the trigger: e.g. "manual
  GitHub reading is too slow on >N questions/week." Only then, and only with the
  privacy mitigations above.
- **Sync automation** — VS Code task vs. git hook vs. Storage extension as the
  editor target (carried over from architecture.md §3).

## Decision

mindMe stays on the lean, single-user, serverless footprint it already has, and
**channels are split by sensitivity**: Telegram handles low-sensitivity *capture
in* and *notify out*; reading the full `.me` happens on **GitHub mobile / VS
Code** behind your own identity. The next build tightens the capture + notify
loop and closes the sync→GitHub read loop. **Conversational recall over Telegram
(`search_os`) is deferred** behind explicit privacy controls, built only if
manual reading proves insufficient. This keeps the €5–10/month cap intact and
keeps personal content out of Telegram's plaintext cloud store.
