# AGENTS.md — guidance for coding agents working on `mindMe`

> Read this before editing. This repo has strict boundaries that protect personal data.

## What this repo is

`mindMe` is a personal AI agent for a single user. It is **not** a generic assistant template. It assumes:

- One user, one Telegram chat (the ID configured in `.env` as `TELEGRAM_ALLOWED_CHAT_ID`).
- Personal data lives in the developer's Personal OS repo on the laptop, NOT in this repo.
- Azure stores a private `personal-os/` mirror for run-time access. Treat it as personal data protected by RBAC, private containers, and Microsoft-managed at-rest encryption.

## Hard rules

1. **Never log personal content.** No journal entries, no dashboard text, no family member names, no message bodies. Log only IDs, sizes, durations, error codes.
2. **Never widen the Telegram allowlist** without explicit human confirmation. The check `chat_id == TELEGRAM_ALLOWED_CHAT_ID` is non-negotiable.
3. **Never commit secrets.** Use Key Vault. `.env` is gitignored — never commit a populated `.env`.
4. **Never widen cloud exposure.** Personal OS content may live only in the private `personal-os/` container guarded by managed-identity RBAC and Microsoft-managed at-rest encryption. Do not add public access, SAS-based distribution, or plaintext exports outside that boundary without explicit human approval.
5. **Never use a corporate / work Microsoft account** for any auth. This is a personal project on the developer's personal Azure subscription only. Verify the signed-in identity with `az account show` before deploying.
6. **Cost discipline.** Default to consumption plans, gpt-4o-mini, no premium SKUs. Budget cap: €10/mo.
7. **Pre-push audit (every push, not just the first).** Before `git push`, run `./scripts/audit-leaks.ps1` (or `./scripts/audit-leaks.ps1 -Staged` for staged-only). Exits non-zero on hits. The scan covers emails, real names, Telegram IDs, subscription GUIDs, addresses. If found, move the value to `.env` (gitignored) or the Personal OS, then re-stage. Same discipline as `samoletovs/me`.
8. **Silence httpx + httpcore loggers before constructing any Telegram client.** `python-telegram-bot` uses `httpx` internally. `httpx` logs full request URLs at INFO level, and Telegram URLs contain the bot token in the path (`/bot<TOKEN>/getMe`). Without silencing, the token leaks to terminal scrollback, log files, and any session capture. Set `logging.getLogger("httpx").setLevel(logging.WARNING)` (and same for `httpcore`) BEFORE `Application.builder().token(...)` is called. If a token ever leaks, rotate it immediately via @BotFather (`/revoke` then `/token`). This rule cost us one token rotation already on 2026-05-10 — don't pay it again.
9. **Never put personal content into OpenTelemetry span attributes.** Trace spans follow the same policy as logs (rule 1): IDs, sizes, durations, status codes, agent names — never prompts, completions, message bodies, briefing text, journal text, or any URL that contains a Telegram bot token. Auto-instrumentation for `httpx`/`requests`/`urllib` is **disabled** via `OTEL_PYTHON_DISABLED_INSTRUMENTATIONS` (set both in code and as an app setting) precisely because those instrumentors capture full URLs. GenAI content capture is **disabled** via `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=false` (same belt-and-suspenders pattern). All HTTP and LLM telemetry uses manual spans declared in `harness/function_app.py` with explicit, audited attribute lists. If you add a span, add it to the architecture.md §8 table; if you can't justify each attribute under rule 1, drop it.

## Code conventions

Optional voice transcription uses `AZURE_OPENAI_TRANSCRIPTION_DEPLOYMENT`.
The legacy `AZURE_OPENAI_WHISPER_DEPLOYMENT` setting remains a fallback; its
`whisper` value resolves to `gpt-4o-mini-transcribe` (`2025-12-15`) on the same
account. An unset pair still disables local transcription: do not enable it as
a side effect of a model migration. JSON response format and a 60-second request
timeout preserve the transcript contract. Never use real personal audio in tests.

Telegram copy uses the shared `harness/telegram_voice.py` guidance. Keep a plain,
calm, direct voice for a busy non-native English reader on a phone. Ordinary
answers are short; explicit More details, comparisons and inspection may be long.
Improve prompts and fixed copy at their source, never rewrite outgoing text or
trim evidence at the send boundary. Preserve exact quotes, status, uncertainty
and approval requirements. See README's Telegram voice and deploy.md's rollout.

Azure SDK auto-tracing is disabled too (`azure_sdk`, `AZURE_TRACING_ENABLED=false`,
and the Azure Core tracing setting). SDK HTTP logs must stay suppressed: private
blob filenames are personal data even when bodies are not logged. Keep the offline
SDK telemetry regression alongside any tracing changes.

- **Python 3.11+** with pinned `requirements.txt` files. Type hints required on public functions.
- **Async** for I/O (HTTP, Storage, Telegram, Foundry calls). No blocking calls in Function handlers.
- **Structured logging** via `structlog` or `logging` with JSON formatter. App Insights consumes this.
- **Tests** use `pytest` + `pytest-asyncio` when present; don't document or rely on test paths that are not checked into this repo.
- **Bicep** for IaC. Keep infrastructure definitions in `infrastructure/`. No ARM JSON.

## Directory ownership

| Folder | Purpose | Who edits it |
|---|---|---|
| `agent/` | Foundry agent-facing metadata and tool schema | Foundry workflows / manual updates |
| `harness/` | Azure Functions (Telegram receiver, timers, queue drain) | Functions deploy workflow |
| `infrastructure/` | Bicep deployment definitions | Manual `az deployment` or `azd up` |
| `scripts/local/` | Runs on the laptop on demand — accesses `%USERPROFILE%\OneDrive\.vscode\.me` directly (override via `ME_OS_ROOT`) | Manual local use |
| `scripts/dev/` | Local smoke-test and bootstrap helpers | Manual local use |

## Workflow shortcuts for Copilot

- "deploy" → run `infrastructure/main.bicep` then `func azure functionapp publish`. Never deploy without verifying budget first.
- "test the bot" → use `scripts/dev/smoke_agent.py` for a Foundry round-trip or DM `/ping` to the deployed Telegram bot.
- "update the agent/tool contract" → edit `agent/openapi-tools.json`, then redeploy the hosted agent via the Foundry workflow or local bootstrap flow.
- Agent tools require Function auth and the Foundry `mindme-tools` Custom Keys connection (`x-functions-key`). Never restore anonymous tool access to work around a missing connection.
- Timers run in UTC on the current Linux Flex app: daily 07:30 and Sunday 18:00. Do not describe them as local time.
- Captures are forwarded to memex, not written by the legacy queue handler. A forwarding failure must remain retryable, and callback queries must pass the same chat allowlist.
- `harness/capture_links.py` normalizes Telegram URL/text-link entities (UTF-16
  offsets) before routing text or media captions. Preserve update/message IDs,
  commentary and command/reply precedence; remove stale entity offsets only
  when rewriting their text. Never fetch article/video content in the companion.
- Briefing section preferences and the onboarding marker remain in the private container until explicitly removed. Ordinary companion calls are single-turn and use `store=False`; no durable chat memory is maintained.
- The opt-in action briefing stores scoped proposal/decision receipts and concise
  corrections in `system/mindme/briefing-state-v1.json`, not conversation transcripts.
  `/memory` lists corrections and `/memory forget <id>` deletes them idempotently.
  Corrections remain until deleted/superseded or their source is removed; proposals
  expire after 14 days, and unresolved action receipts must not be silently evicted.
  State has explicit record/size caps. Keep content out of logs and traces.
- `MINDME_ACTION_BRIEFING_ENABLED` defaults off. Enabling requires configured model,
  GitHub and private state access; task execution additionally needs `MEMEX_ACTION_URL`.
  Do not infer its authentication key from another memex function URL.
- New approval callbacks use `brief1|...`; route only that namespace to the briefing
  loop, after the owner allowlist. Other callbacks still belong to memex. Source
  revisions and atomic claims must be checked before side effects.
- `weekly_review.py` orchestrates the action-enabled Sunday timer and `/review`;
  `weekly_plan.py` validates and renders its bounded HTML summary/action cards.
  Weekly baselines are independent of daily checkpoints. At most two completed
  weekly delivery records and eight prior daily records are retained alongside unresolved
  deliveries. Proposal activity holds at most 32 date/status observations, pruned
  after 35 days during source reconciliation; it is inspectable via `/proposals all`.
  Never backfill legacy completion dates or equate a verification date with the
  date work happened. Source removal removes proposal text, preserving only the
  non-replayable operation receipt and date/status observations.
- Weekly models receive current safe canonical sources, not private-mirror facts.
  Only mirror freshness metadata is checked. A stale mirror is not a reason to
  suppress independently current canonical actions or claim the owner was inactive.
  `/review retry` explicitly permits a possibly duplicated unconfirmed message;
  ordinary timers/requests must not silently retry unknown delivery outcomes.
- "change the morning briefing or Telegram behavior" → edit `harness/function_app.py`, then redeploy the Function App.
- Daily `vault-evolve` is a separate default-off cloud adapter (`MINDME_DAILY_EVOLVE_ENABLED`),
  gated by the saved `knowledge` section on the existing morning timer. It reads only
  safe mindVault sources, never a work vault/private mirror. Its `evolve1|` callbacks
  save scoped feedback, not action approval. Preserve original capture callbacks.
  See [the runtime contract](docs/vault-evolve.md); review artifacts are proposal-only,
  and a submitted PR is not canonical publication. Its separate private state expires
  after 14 days; do not add unbounded chat memory or silent delivery retries.
- Contextual knowledge uses the existing action-briefing flag, model, private
  `briefing-state-v1.json` and approval engine. `cap1|action|32hex` resolves the actual
  owner/message through the read-only `capture_context` operation at the existing
  `MEMEX_WEBHOOK_URL`. Never trust caller source text or substitute another note
  after lookup/read failure. `/recap URL` remains memex-owned.
- Only after verifying that message binding, pass the actual Telegram
  `reply_to_message.text`/caption or callback message text/caption (at most 4,096
  characters) as untrusted, reference-only context for pronouns/numbered ideas.
  Recap order may differ from canonical source order. This shown text is not
  evidence, corroboration or permission and must never be persisted. Citations
  still require canonical evidence; missing/ambiguous references require a short
  quote rather than a guess. Durable context remains concise source-bound memory.
- `knowledge_context.py`, `knowledge_loop.py`, `knowledge_plan.py` and
  `knowledge_state.py` implement source-bound follow-up, topic receipts and own-memory
  continuity. This project remains registered as `tier: own` in governance's
  `config/memory-targets.json`; do not install the incompatible TypeScript memory core.
  Every continuity write uses `decide_write`; recall is deterministic, injected in a
  nonce-fenced data packet, and actual model-reported usage updates usage metadata.
  No raw chat archive, inferred permanent interests or extra knowledge vault.
- Retention: working context and live bindings 14 days; explicit feedback 90 days;
  corrections until deletion/source invalidation (superseded memories get a 35-day
  grace); private topic brief/revision receipts 35 days. Source change, deletion,
  privacy/ignore changes or derived-output classification invalidate derivatives.
  Invalidated bindings become source-less replay tombstones, not refreshed authority.
  Memory/topic/source text never enters logs, telemetry or Git.
- `/knowledge [page|id]`, `/knowledge forget <id>`, `/topics [page|id|query]` and
  `/topics forget <id>` inspect/delete this concise memory and complete topic briefs.
  `/knowledge receipts [page]` inspects message/request metadata; `/knowledge forget
  bindings` explicitly removes bindings. `/knowledge proposal <id>` explicitly
  re-presents a pending card after uncertain delivery; duplicate webhook retries
  do not repeat it. Non-content uncertain-request replay guards and existing
  unresolved action receipts are preserved, never silently evicted.
- Caps are 100 knowledge memories, 200 bindings (including revoked tombstones),
  300 request receipts and 30 topic briefs, within the existing 1 MiB state ceiling.
  Capacity fails closed rather than evicting unresolved receipts. Canonical topic
  retrieval ranks paths before at most 16 content reads, then selects at most five
  relevant safe sources. Exact quote/path validation precedes rendering. Agreement
  and conflict need two cited paths; shared origins are not independent corroboration.
  Knowledge-only reads/retrieval/approval rechecks may admit `wiki/sources/*.md`
  captures whose subject filenames contain "report" or "review" when metadata has
  exactly `type: source`, one original HTTP(S) `source:` URL, and a host-shaped
  `Source ID:` footer. This bypasses only the derived-filename heuristic, never
  sensitive/ignored/generated/derived metadata, sensitive content or artifact-path
  restrictions. `allow_captured_sources` defaults false: do not broaden DailyEvolve's
  publication/writer contract, which is separately enforced by memex.
  Direct knowledge reads and captured-source approval rechecks require a regular
  file in the pinned canonical Git tree before fetching Contents, then verify the
  returned blob SHA against that tree entry. Contents `type: file` alone is not a
  symlink check: GitHub can return that shape after dereferencing an in-repo link.
  Generation uses host-issued source/quote IDs, following vault-evolve's evidence
  packet pattern. `knowledge_evidence_packet` replaces source text with bounded
  exact snippets; `knowledge_model_schema` binds each source to its allowed IDs;
  `hydrate_synthesis` restores the original path/quote before the existing validator.
  Citation enums live once in `$defs`, reused with `$ref` in every section; keep the
  maximum five-source/48-quote schema under the Structured Outputs 1,000-enum limit.
  Never regenerate/fuzzy-match quotations. The generation schema's section caps
  sum to eight findings, and hydration enforces the same caps.
- Dig/Apply only prepare cards. Only `brief1` approval against the actual card and
  all current eligible source revisions may execute through ActionGateway. Public
  research remains one impersonal question, at most five sources, one short report,
  no recursive jobs; never include private user context. Apply drafts a task, not
  code edits or completed work. Existing morning/Sunday timers prune bounded
  operational memory for the recently active owner and reconcile approved action
  receipts; no new timer, automatic research or repeated unaccepted nudges.

## What this repo is NOT

- Not a template. Don't generalize.
- Not multi-user. Don't add user tables.
- Not a chatbot platform. Don't add session management beyond what Foundry provides.
- Not a productivity SaaS. The explicitly approved personal-task pilot and its
  first-release Today/Knowledge dashboard share one web surface: one configured
  personal Microsoft owner, the existing Functions host, safe canonical
  mindVault material, canonical task Markdown, and the existing approval ledger.
  Do not generalize it into a public app or add another task database.

The dashboard's generated daily reviews and weekly digests are display-only;
`dashboard_sources.py` must not loosen generation eligibility. Every displayed
field and reference is subject to privacy checks. Evidence reads resolve
server-owned IDs in pinned regular-file trees; no arbitrary URL reader.
Feedback uses `evolve_feedback.py` and the same private fourteen-day evolve state
as Telegram, not browser callback replay. `dashboard_visit` is one content-free
CAS-protected marker, not a reading history. See `docs/vault-dashboard.md`.

If a feature request doesn't fit, push back. The success criterion is "does this make my mornings calmer?" — nothing else.

## Personal task pilot boundaries

`MINDME_TASKS_ENABLED` and `MINDME_WEB_ENABLED` default off. See
[the runtime contract](docs/task-workspace.md) for the exact settings and routes.
Never use a fake development identity, first-visitor owner enrollment, a work
tenant, forwarded identity headers, or browser-held OAuth tokens. Missing auth
configuration must fail closed. The BFF uses MSAL code+PKCE, form-post callback,
signed ID-token validation and exact configured personal-directory owner checks.
Do not enable app-wide EasyAuth over the existing Telegram/Function-key routes.

`task1|clarify|...` is context, not approval. Resolve the actual enrolled DM owner
and confirmed bot message at memex, then re-read the canonical task blob. Keep
the three-answer cap and exact asked-field binding. Source drift, stale questions
and unknown publication/delivery cannot become a fresh operation or generic
capture. New task proposals use the existing `brief1|` approval binding.
An issued question token must be checked in the same CAS update as its answer,
not only before it. Disabling Tasks must block its existing cards in legacy
callback/reply paths too. All cached receipt responses use the shared current-
source projection; completed tombstones stay content-free with immutable
completion/operation proof.
Preserve validated non-content review/acknowledgment flags through source removal.
Only explicit owner acknowledgment of an unavailable completed result may release
its review slot; never infer reading or task verification, restore private content,
or rewrite immutable completion/operation proof.
Assess date metadata before task display pagination, never select the first
twelve capture filenames and then look for due work. The bounded canonical
assessment reports incomplete attention and its cursor when it cannot cover the
full permitted task inventory; an empty partial view is not evidence nothing is due.
Task attention requires configured IANA `MINDME_TASKS_TIMEZONE`. Rank and expose
per-item eligibility/reasons from the same server-derived owner day and seven-day
deadline horizon, not a separate browser clock. Future reviews suppress discretionary
attention, never hard deadlines. Budget, auth and approval-expiry clocks remain UTC;
the owner's attested `verified_on` date uses the owner calendar separately.

Task APIs prepare exact source-version-bound changes. The separate writer-owned
`publish_task` uses a distinct stable operation ID and the approved
`target_action_id`; never accept a browser-selected PR, repository or branch.
Native checks and the exact writer manifest govern publication. Ready is owner
selection, not inferred from a complete form or `execution: agent`.

Private preparation is bounded, uses the existing model with `store=False`, and
cannot execute consequential actions. Standing scope is explicit and revision-
bound, on existing timers only; stop when its source/project changes, work limits
or review capacity are reached. A draft/report is not verified task completion.
Closure requires owner-checked acceptance plus result/evidence/verification date;
learning stays in the closed canonical task before any separately reviewed wiki
promotion. Operational receipts remain in the existing private state, not a
second task store. Never log task content, auth tokens/codes, or identity claims.
