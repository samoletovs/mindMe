# AGENTS.md — guidance for coding agents working on `mindMe`

> Read this before editing. This repo has strict boundaries that protect personal data.

## What this repo is

`mindMe` is a personal AI agent for a single user. It is **not** a generic assistant template. It assumes:

- One user, one Telegram chat (the ID configured in `.env` as `TELEGRAM_ALLOWED_CHAT_ID`).
- Personal data lives in the developer's Personal OS repo on the laptop, NOT in this repo.
- Cloud is for execution, not storage. The only personal data that touches Azure is an ephemeral encrypted briefing snapshot.

## Hard rules

1. **Never log personal content.** No journal entries, no dashboard text, no family member names, no message bodies. Log only IDs, sizes, durations, error codes.
2. **Never widen the Telegram allowlist** without explicit human confirmation. The check `chat_id == TELEGRAM_ALLOWED_CHAT_ID` is non-negotiable.
3. **Never commit secrets.** Use Key Vault. `.env` is gitignored — never commit a populated `.env`.
4. **Never bypass encryption.** The briefing-context blob must be AES-GCM encrypted with the key from Key Vault. Plaintext briefing context is a security incident.
5. **Never use a corporate / work Microsoft account** for any auth. This is a personal project on the developer's personal Azure subscription only. Verify the signed-in identity with `az account show` before deploying.
6. **Cost discipline.** Default to consumption plans, gpt-4o-mini, no premium SKUs. Budget cap: €10/mo.
7. **Pre-push audit (every push, not just the first).** Before `git push`, run `./scripts/audit-leaks.ps1` (or `./scripts/audit-leaks.ps1 -Staged` for staged-only). Exits non-zero on hits. The scan covers emails, real names, Telegram IDs, subscription GUIDs, addresses. If found, move the value to `.env` (gitignored) or the Personal OS, then re-stage. Same discipline as `samoletovs/me`.
8. **Silence httpx + httpcore loggers before constructing any Telegram client.** `python-telegram-bot` uses `httpx` internally. `httpx` logs full request URLs at INFO level, and Telegram URLs contain the bot token in the path (`/bot<TOKEN>/getMe`). Without silencing, the token leaks to terminal scrollback, log files, and any session capture. Set `logging.getLogger("httpx").setLevel(logging.WARNING)` (and same for `httpcore`) BEFORE `Application.builder().token(...)` is called. If a token ever leaks, rotate it immediately via @BotFather (`/revoke` then `/token`). This rule cost us one token rotation already on 2026-05-10 — don't pay it again.
9. **Never put personal content into OpenTelemetry span attributes.** Trace spans follow the same policy as logs (rule 1): IDs, sizes, durations, status codes, agent names — never prompts, completions, message bodies, briefing text, journal text, or any URL that contains a Telegram bot token. Auto-instrumentation for `httpx`/`requests`/`urllib` is **disabled** via `OTEL_PYTHON_DISABLED_INSTRUMENTATIONS` (set both in code and as an app setting) precisely because those instrumentors capture full URLs. GenAI content capture is **disabled** via `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=false` (same belt-and-suspenders pattern). All HTTP and LLM telemetry uses manual spans declared in `harness/function_app.py` with explicit, audited attribute lists. If you add a span, add it to the architecture.md §8 table; if you can't justify each attribute under rule 1, drop it.

## Code conventions

- **Python 3.11+** with `pyproject.toml`. Type hints required on public functions.
- **Async** for I/O (HTTP, Storage, Telegram, Foundry calls). No blocking calls in Function handlers.
- **Structured logging** via `structlog` or `logging` with JSON formatter. App Insights consumes this.
- **Tests** in `tests/` mirroring `agent/src/` and `harness/`. `pytest` + `pytest-asyncio`.
- **Bicep** for IaC. Modules per resource. No ARM JSON.

## Directory ownership

| Folder | Purpose | Who edits it |
|---|---|---|
| `agent/` | Foundry hosted agent (Python, containerized) | Foundry deploy workflow |
| `harness/` | Azure Functions (Telegram receiver, timers, queue drain) | Functions deploy workflow |
| `infrastructure/` | Bicep modules | Manual `az deployment` or `azd up` |
| `scripts/local/` | Runs on the laptop via Task Scheduler — accesses `%USERPROFILE%\OneDrive\.vscode\.me` directly (override via `ME_OS_ROOT`) | Manual install on laptop |
| `.foundry/` | Foundry agent metadata (per microsoft-foundry skill) | Foundry workflows |

## Workflow shortcuts for Copilot

- "deploy" → run `infrastructure/main.bicep` then `func azure functionapp publish`. Never deploy without verifying budget first.
- "test the bot" → use `scripts/dev/send_test_message.py` (read-only ping, doesn't touch personal data).
- "update the briefing prompt" → edit `agent/src/main.py`, redeploy hosted agent via Foundry deploy workflow.
- "add a tool to the agent" → add Python function under `agent/src/tools/`, register in `agent/src/main.py`, redeploy.

## What this repo is NOT

- Not a template. Don't generalize.
- Not multi-user. Don't add user tables.
- Not a chatbot platform. Don't add session management beyond what Foundry provides.
- Not a productivity SaaS. Don't add a web UI.

If a feature request doesn't fit, push back. The success criterion is "does this make my mornings calmer?" — nothing else.
