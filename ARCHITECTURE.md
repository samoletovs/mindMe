# mindMe — code architecture

A map of the code for anyone about to change it: what runs where, how a Telegram
message or a timer tick travels through the modules, and the rules a change has to
keep. It describes the repository as it is. The reasons behind the big decisions (the
private mirror, the security boundary, tracing) stay in
[docs/architecture.md](docs/architecture.md).

| You want | Read |
|---|---|
| The rules that protect personal data | [AGENTS.md](AGENTS.md) |
| What the bot does and how to set it up | [README.md](README.md) |
| Cloud data flow, security model, tracing spans | [docs/architecture.md](docs/architecture.md) |
| Deploying, verifying, rolling back, reading telemetry | [docs/deploy.md](docs/deploy.md) |
| One feature's design | [action briefing](docs/design-action-briefing.md), [knowledge loop](docs/design-knowledge-loop.md), [task workspace](docs/task-workspace.md), [vault dashboard](docs/vault-dashboard.md), [daily review](docs/vault-evolve.md) |

## 1. Shape of the system

One Azure Functions app (Python v2 programming model, Flex Consumption plan) is the
whole live system. [harness/function_app.py](harness/function_app.py) is its
**composition root**: it registers the functions, reads configuration and feature flags,
builds the clients and "loop" objects, and injects every external effect (Telegram,
memex, GitHub, models, blob storage) into them. It also decides how an error surfaces:
an HTTP status for Telegram, a short Telegram notice for the owner, or a failed timer run.

The other `harness/*.py` modules hold the rules: state machines, validation and
rendering. They do not reach for the network on their own; effects arrive as constructor
arguments or callables, so the tests run them offline with fakes.

```text
                   ┌──────────────────────────┐
Telegram ──POST──▶ │   mindMe Function App    │ ──▶ memex    captures, note-review buttons
Timers (UTC) ────▶ │  (harness/function_app)  │ ──▶ models   companion, reviews, assist, briefing plan
Owner's browser ─▶ │                          │ ──▶ GitHub   research issues, approved actions, reapers
Foundry agent ───▶ │                          │ ──▶ blob     markdown mirror and private ledgers
                   └──────────────────────────┘
```

mindMe owns the single Telegram webhook (a bot has only one) and forwards captures to
memex, the separate capture engine. State lives in one private blob container
(`personal-os`, in the data storage account): the markdown mirror of the vault plus small
JSON ledgers under `system/mindme/` (briefing, daily-review and task-assist receipts,
briefing preferences, the onboarding marker). Ledgers are updated with ETag
compare-and-swap writes.

## 2. Entry points

| Function | Trigger | Who may call it | Purpose |
|---|---|---|---|
| `telegram_webhook` | `POST /api/telegram_webhook` | Telegram, proven by the `X-Telegram-Bot-Api-Secret-Token` header; then only the allow-listed chat | Every update: commands, captures, voice notes, button taps |
| `morning_briefing_timer` | `0 30 7 * * *` (UTC) | timer | The morning briefing; also the daily knowledge review when enabled |
| `weekly_review_timer` | `0 0 18 * * 0` (Sunday, UTC) | timer | The weekly review (a plain nudge unless the action briefing is on) |
| `reaper_poll_timer` | `0 */30 * * * *` (UTC) | timer | Polls GitHub for finished Copilot-agent PRs and dispatches the existing reaper workflow ([github_reapers.py](harness/github_reapers.py)) |
| `health` | `GET /api/health` | anonymous | Liveness probe only; it checks no dependency |
| `task_site_root` | `GET /` | anonymous | Redirects to `/api/tasks` (on a legacy host after the canonical-domain cutover, to the canonical portal origin); serves no task data |
| `task_web_root`, `task_web_route` | `GET /api/tasks`, `GET`/`POST /api/tasks/{*path}` | anonymous at the HTTP layer; the code enforces the owner's Microsoft sign-in and CSRF | The task workspace shell and its owner-only APIs |
| `tool_briefing_context`, `tool_weather`, `tool_vault_recent`, `tool_vault_read` | `/api/tools/…` | Function key (`x-functions-key`) | Tools the Foundry agent calls; the contract is [agent/openapi-tools.json](agent/openapi-tools.json) |

Timers run in UTC. Never describe them as local time.

## 3. How a Telegram update is routed

`telegram_webhook` is one long, ordered chain. The first handler that claims an update
wins, so the order is part of the behaviour: `/dig`, for example, must run before capture
routing because its question may contain a URL.

1. **Gate.** The secret header must match (`403` otherwise) and the body must be a JSON
   object (`400` otherwise).
2. **Button taps (`callback_query`).** The tapped message's chat must be the allow-listed
   one; any other chat is acknowledged with `200` and ignored. Then, by `callback_data`
   prefix: `assist1|` is AI assistance (owner only, assist flag on); `task1` and the
   task-bound `brief1|` proposals go to the task workspace; `cap1` is a knowledge action on
   a saved source; `evolve1|` is feedback on the daily review; any other `brief1|` is an
   action-briefing approval. Everything else is forwarded to memex (its note-review buttons).
3. **Messages.** The chat allow-list is checked again (others get a silent `200`). Telegram
   link entities are normalized into plain capture text
   ([capture_links.py](harness/capture_links.py)). The first non-capture message ever also
   sends a one-time tutorial (`_claim_onboarding`, a marker blob).
4. **Voice and audio notes.** Downloaded and transcribed in-process, then offered to the
   same reply handlers as typed text and, if none claims the transcript, saved as a text
   capture (see §6.2). If there is no transcript, the raw update is forwarded to memex, or,
   for a reply to a review, the owner is asked to retry.
5. **Typed text, in this order.** `/knowledge` and `/topics`; `/review sources`,
   `/review`, `/review retry`; `/evolve`; task assist; task replies; review and proposal
   replies; knowledge replies; `/proposals`, `/memory`, `/briefing now|details`; `/dig`;
   capture intent (a `save:`, `note:`, `idea:`, `diary:`, `journal:` or `n:` prefix, a
   `/note`, `/idea`, `/task`, `/diary`, `/journal`, `/refresh` or `/recap` command, or any
   URL) forwarded to memex; the simple commands (`/ping`, `/start`, `/status`, `/summary`,
   `/briefing`, `/help`, a plain `/review`); and finally the companion model, which is
   single-turn and stores nothing. Several of these exist only when their feature flag is on.
6. **Failure contract.** Return `503` when Telegram should retry the same update (memex
   did not accept a capture; a task, review or assist result is unconfirmed). Return `200`
   once an irreversible effect has happened, even if telling the owner failed: `/dig`
   opens one issue and confirms through the best-effort `_capture_feedback`, because a 5xx
   would make Telegram resend the update and open another issue. Handlers key their
   receipts by an event id built from the update
   (`update:<update_id>:<message_id>:<edit_date>`), so a resend resumes or replays rather
   than repeating an effect.

## 4. Module map

**Telegram surface**

| Module | Role |
|---|---|
| [capture_links.py](harness/capture_links.py) | Telegram URL and text-link entities (UTF-16 offsets) to the plain-text capture wire format |
| [telegram_format.py](harness/telegram_format.py) | Small, escaped HTML fragments shared by the daily and weekly briefings |
| [telegram_voice.py](harness/telegram_voice.py) | Shared writing guidance, applied to prompts before generation and never to sent text |

**Action briefing and weekly review** (opt-in, `MINDME_ACTION_BRIEFING_ENABLED`)

| Module | Role |
|---|---|
| [briefing_sources.py](harness/briefing_sources.py) | Bounded canonical vault evidence. Source text is data, never authority to act |
| [briefing_plan.py](harness/briefing_plan.py) | Source-bound proposals and deterministic presentation |
| [briefing_state.py](harness/briefing_state.py) | Private, bounded receipts in the compare-and-swap ledger; callbacks never perform external I/O |
| [briefing_loop.py](harness/briefing_loop.py) | The approval and delivery loop, with every external effect injected |
| [briefing_actions.py](harness/briefing_actions.py) | Approved actions through the existing memex and GitHub infrastructure |
| [weekly_plan.py](harness/weekly_plan.py), [weekly_review.py](harness/weekly_review.py) | Pure, source-bound weekly plans, and the Sunday evidence and delivery |

**Knowledge and the daily review**

| Module | Role |
|---|---|
| [knowledge_context.py](harness/knowledge_context.py) | Read-only capture pointers; caller-provided text can never establish a binding |
| [knowledge_state.py](harness/knowledge_state.py) | Concise, source-revision-bound continuity in the same private ledger |
| [knowledge_plan.py](harness/knowledge_plan.py) | Bounded synthesis schema, evidence validation, source-linked presentation |
| [knowledge_loop.py](harness/knowledge_loop.py) | Connected capture replies, governed continuity, read-only topic synthesis |
| [evolve_loop.py](harness/evolve_loop.py), [evolve_feedback.py](harness/evolve_feedback.py), [vault_evolve.py](harness/vault_evolve.py) | The daily proposal-only review, its revision-aware feedback, and the shared receipt format |

**Tasks, assistance and the web shell** (opt-in, `MINDME_TASKS_ENABLED`, `MINDME_ASSIST_ENABLED`)

| Module | Role |
|---|---|
| [task_service.py](harness/task_service.py), [task_state.py](harness/task_state.py) | One owner, one canonical task tree, the approval ledger and its bounded receipts |
| [task_sources.py](harness/task_sources.py), [task_context.py](harness/task_context.py) | Read-only pinned task views (Markdown is authoritative) and strict owner/message binding |
| [task_telegram.py](harness/task_telegram.py) | The task-only Telegram adapter; capture and knowledge namespaces stay separate |
| [task_web.py](harness/task_web.py), [task_auth.py](harness/task_auth.py) | Owner-only shell and APIs on the existing host; Microsoft OIDC BFF with encrypted cookies |
| [task_assist.py](harness/task_assist.py), [task_assist_state.py](harness/task_assist_state.py), [assist_audio.py](harness/assist_audio.py) | Bounded AI assistance with its own ledger and budget, and offline duration checks before paid transcription |
| [dashboard_service.py](harness/dashboard_service.py), [dashboard_sources.py](harness/dashboard_sources.py), [closure_scan.py](harness/closure_scan.py) | Owner dashboard reads and bounded mutations, display-only projections, content-free closure checks |
| [harness/web/](harness/web) | The static shell shipped with the Function package: `index.html`, `tasks.css`, `tasks.js`, `dashboard.js` |

**Platform**

| Module | Role |
|---|---|
| [execution_budget.py](harness/execution_budget.py) | Cooperative deadlines for synchronous network work (see §5) |
| [model_response.py](harness/model_response.py) | Content-free reading of a Responses API result: status, stop reason, refusal flag, token counts |
| [github_reapers.py](harness/github_reapers.py) | Moves the reaper workflows' idle polling off GitHub Actions |
| [vault_layout.py](harness/vault_layout.py), [vault-layout.json](harness/vault-layout.json) | Folder roles per vault: folder names are configuration, not code |

Outside `harness/`: [agent/](agent) (Foundry tool schema), [infrastructure/](infrastructure)
(Bicep), [scripts/dev/](scripts/dev) and [scripts/local/](scripts/local) (helpers, none of
which ship), [scripts/audit-leaks.ps1](scripts/audit-leaks.ps1) (the pre-push personal-data
scan), and [knowledge/wiki/](knowledge/wiki), Markdown pages added by the memex pipeline and
merged by [auto-merge-knowledge.yml](.github/workflows/auto-merge-knowledge.yml) when a PR
only adds them. [PRODUCT.md](PRODUCT.md) and [DESIGN.md](DESIGN.md) brief the web shell.

## 5. Rules every change keeps

These are enforced by tests where they can be. [AGENTS.md](AGENTS.md) has the full hard rules.

1. **Logs and telemetry carry no content.** Log IDs, sizes, durations and fixed codes
   only; never message text, source text, model output, or a URL that holds a token.
   `_failure_code` returns a code only for internal errors that are raised with a literal
   snake_case reason; SDK and HTTP errors are logged by type alone because they can carry
   URLs or payloads. `inspect_response` reads status, reason and token counts and never
   model text. A new span needs an audited attribute list and a row in
   [docs/architecture.md](docs/architecture.md) §8.
2. **One chat, checked at every door.** Messages and callbacks must match
   `TELEGRAM_ALLOWED_CHAT_ID`; the task web checks the exact owner identity; tools need a
   Function key. Do not widen any of them.
3. **Feature flags fail closed.** `MINDME_ACTION_BRIEFING_ENABLED`,
   `MINDME_DAILY_EVOLVE_ENABLED`, `MINDME_TASKS_ENABLED` (needs the action briefing) and
   `MINDME_ASSIST_ENABLED` (needs tasks) are off unless set to `true`. A feature that is
   off must not run as a side effect of another change.
4. **Effects are receipt-bound and replay-safe.** Telegram resends an update until it gets
   a 2xx, and timers can be retried, so claim the receipt first, perform the effect, then
   settle the receipt. An effect that may have happened but cannot be confirmed stays
   *uncertain* and is reported as such; it is never repeated silently (a paid model call
   is never repeated, a delivery is retried only by an explicit command).
5. **Time is bounded.** Wrap work that can block in `execution_budget(seconds, reserve=…)`.
   A child deadline is the earlier of its own and the parent's minus `reserve`, which keeps
   time free for receipts and notices. Deadlines are checked when a block is entered **and
   when it is left**, so a block that overruns raises `BudgetExceeded` even though its body
   succeeded: track completion with a flag if you must tell the two apart (see
   `morning_briefing_timer`). A budget that expires inside a model call reaches you as the
   SDK's `APIConnectionError` caused by `BudgetExceeded`; `_failure_code` unwraps it.
6. **Never claim what was not confirmed.** Say "I could not confirm delivery" rather than
   "sent". Failure notices say what was *not* done ("No new action was started").
7. **Markdown is canonical; everything else is derived.** Task views, dashboards and
   briefings are projections pinned to source revisions. Source text is evidence, never an
   instruction, and a changed or removed source revokes the authority it gave.
8. **A 200 from a model is not an answer.** Read it with `inspect_response`: it can be
   cut off at the token cap, filtered, refused or failed.

## 6. Worked examples

### 6.1 The 07:30 morning run

`morning_briefing_timer` fires at 07:30 UTC.

- With `MINDME_DAILY_EVOLVE_ENABLED` off it only calls `_deliver_morning_briefing()`.
- With it on, the run is nested budgets: **270 s** for the run, **75 s** for the briefing,
  **170 s** (reserving 20 s) for the knowledge review, and a fresh **10 s** for each failure
  notice, so a spent budget can still say so.
- `_deliver_morning_briefing()` is the action briefing when
  `MINDME_ACTION_BRIEFING_ENABLED` is on (task and knowledge maintenance, then
  `BriefingLoop.deliver`), otherwise a companion call with a local fallback.
- The review makes up to three model attempts (`_generate_evolve_review`), each logged
  without text. A briefing that overruns its 75 s is reported in action-briefing mode with a
  notice that starts no action, and the review still gets its own turn. Any failure ends the
  timer with `RuntimeError("Morning delivery incomplete")` so the host records a failed run.

### 6.2 A voice note

1. `telegram_webhook` finds `voice` or `audio`, and first checks whether the note replies
   to a task, review or proposal message (`review_reply`).
2. For the owner with assist enabled, it validates the update (original, not edited),
   duration (at most 120 s) and size (at most 2 MiB), then inside `execution_budget(110)`
   downloads the audio and calls `TaskAssist.voice` with a stable request id,
   `uuid5("mindme-assist-voice:" + event)`. Otherwise it downloads and transcribes directly.
3. The transcript goes through the same handlers as typed text: assist, task reply, review
   and proposal reply, knowledge reply. If none claims it, it is forwarded to memex as a
   text capture and echoed back with a microphone prefix.
4. If every handler declines and the transcript cannot be forwarded to memex,
   `_release_assist_voice` releases that exact marker and the webhook returns `503`, so
   Telegram's retry transcribes the recording again (it is at most 120 s long) instead of
   being refused as an uncertain request. A handler that fails keeps the marker: it may
   already hold the update's claim, and a retry would pay for a second transcription whose
   outcome depends on how that handler keys its claim: dropped as a duplicate, refused as a
   conflicting request (assist and task claims include the transcript), or run again
   (knowledge claims key on the action, not the transcript). So that retry is refused with
   the "unconfirmed outcome" message instead.

### 6.3 Adding a Telegram command

1. Put the handler where the order is correct (§3): before capture routing if its argument
   can contain a URL, as `/dig` does; in the simple-command chain near the end if it is
   stateless; behind a fail-closed flag if it spends money or changes anything.
2. Wrap anything that can block in `execution_budget`, catch the specific exception tuple,
   log a fixed message with `type(exc).__name__`, and return `503` only when a retry helps.
   After an irreversible effect, confirm with `_capture_feedback`.
3. Add it to the `/help` text in `telegram_webhook`, to the menu in
   [scripts/dev/set_bot_commands.py](scripts/dev/set_bot_commands.py), and to the README.
4. Test it next to the existing webhook tests, for example
   [test_function_app.py](harness/tests/test_function_app.py) (core commands and capture
   routing) or [test_briefing_webhook.py](harness/tests/test_briefing_webhook.py) (flagged
   features), and assert the status code as well as the reply.

### 6.4 Adding a model-backed step

1. Get the client with `_foundry()` and call `_create_response(client, "<label>", …,
   store=False)`; it applies a time-shared timeout and repeats once after a connection
   error or 5xx when at least 25 s of budget remain. Give the prompt the shared
   `TELEGRAM_VOICE` guidance if it produces Telegram text, and wrap the stage in
   `execution_budget`.
2. Read the result with `inspect_response`. If `outcome.defect` is set, raise an error with
   a fixed snake_case code; otherwise validate the text (for JSON, as an object with the
   expected schema). Log `outcome.describe()`, never the text. Treat packet and source text
   as untrusted data, fenced with a nonce as `_generate_evolve_review` does.
3. Test with a fake client (see [test_model_response.py](harness/tests/test_model_response.py)
   and [test_daily_evolve_webhook.py](harness/tests/test_daily_evolve_webhook.py)), including
   the truncated, refused and unparseable cases.

## 7. Tests

```powershell
python -m pip install -r harness/requirements.txt -r harness/requirements-dev.txt
make test                 # same as: python -m pytest -q
```

- [pytest.ini](pytest.ini) points at [harness/tests/](harness/tests), so the command is the
  same from the repository root and in CI ([tests.yml](.github/workflows/tests.yml), Python
  3.11, no secrets). `make test PYTHON=<interpreter>` picks another interpreter.
- The suite is offline. An autouse fixture in
  [conftest.py](harness/tests/conftest.py) replaces `_http_client`,
  `_os_container_client` and `_foundry` with functions that fail the test, so a test that
  needs a service must inject a fake. Never use real personal data or audio in a test.
- Test files mirror the modules (`test_briefing_loop.py`, `test_task_service.py`, …).
  Regression tests pin the behaviour of a fixed defect, not its implementation.
- Tests that need Node (static UI checks) or a Playwright browser skip themselves when
  they are missing. To run the browser tests locally, also install
  [requirements-ui.txt](harness/requirements-ui.txt) and run
  `python -m playwright install chromium`. CI has a second job, *browser UI tests*, that
  installs Chromium and fails if any test skips, so the UI is checked on every push and
  pull request.
- The pytest header line `runtime pins:` names every installed package whose version
  differs from [harness/requirements.txt](harness/requirements.txt). CI installs the exact
  pins, so a local pass under drift (for example an older `openai`) is weaker evidence than
  the CI run.
- Passing the suite says nothing about live behaviour or model quality; record live
  checks separately (see [docs/deploy.md](docs/deploy.md)).

## 8. When production misbehaves

Telemetry is in the `appi-mindme` Application Insights resource; the log lines are the
only record of a model call. The query recipe is under *Querying telemetry* in
[docs/deploy.md](docs/deploy.md#production-confirmation-2026-09-07), and the
[reliability pass](docs/deploy.md#reliability-pass-2026-11-15) explains how to read each
review line.

| Symptom | Search `traces` for | Code |
|---|---|---|
| Morning message missing | `morning briefing incomplete`, `action briefing failed` | `morning_briefing_timer`, `_deliver_morning_briefing` |
| Daily review failed | `daily review model call attempt=`, `daily knowledge review failed` | `_generate_evolve_review`, [evolve_loop.py](harness/evolve_loop.py) |
| A model call retried | `model call failed attempt=` | `_create_response` |
| Telegram keeps resending an update | `webhook rejected`, `capture forward rejected`, `capture forward failed`, `request failed error=`, `reply failed error=` | the matching `except` in `telegram_webhook` |
| Reapers not dispatching | `reaper poll complete checked=` | [github_reapers.py](harness/github_reapers.py) |
| A deploy returns 503 | the readiness script in [docs/deploy.md](docs/deploy.md) | `scripts/dev/check-deploy-readiness.ps1` |
