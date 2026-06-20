"""mindMe Function App — Phase 2 entry points.

Programming model: Azure Functions Python v2 (decorator-based, single file).
All handlers share the module-level Foundry client and Telegram HTTP client to
avoid cold-start per request.

Endpoints
---------
- POST  /api/telegram_webhook    Telegram update receiver (replaces long-poll)
- TIMER 0 30 7 * * *             morning_briefing_timer (07:30 Sweden time)
- QUEUE capture-events           capture_drain (Phase 3 placeholder)
- GET   /api/health              uptime probe
- POST  /api/tools/briefing_context  Foundry agent tool: get_briefing_context()
- GET   /api/tools/weather       Foundry agent tool: get_weather()

Logging policy (AGENTS.md rule 1): IDs, sizes, durations only. Never message
content. Rule 8: httpx logger silenced before any Telegram call. Rule 9 (added
2026-05-17 with agentFlow Phase 1): OpenTelemetry span attributes follow the
same policy as logs — never set attributes containing prompts, completions,
message bodies, briefing text, or Telegram URLs. Manual spans only; httpx /
requests auto-instrumentation is explicitly disabled.
"""

from __future__ import annotations

# --- Tracing safety env (Hard Rule 9) ---------------------------------------
# MUST be set BEFORE importing any OTel exporter or instrumentation. GenAI
# semantic conventions capture prompts/completions by default — we don't ship
# that to App Insights. Done belt-and-suspenders via env so any nested
# OTel-aware library also picks it up.
import os as _os_for_otel_env

_os_for_otel_env.environ.setdefault(
    "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "false"
)
# httpx / requests / urllib auto-instrumentation would capture full URLs.
# Telegram URLs contain the bot token in the path. Disable them outright; we
# emit manual spans for the few HTTP calls we make.
_os_for_otel_env.environ.setdefault(
    "OTEL_PYTHON_DISABLED_INSTRUMENTATIONS",
    "httpx,requests,urllib,urllib3,aiohttp-client",
)
del _os_for_otel_env

import json
import logging
import os
import re
import time
from datetime import date
from urllib.parse import quote

import azure.functions as func
import httpx
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

# Hard Rule 8: silence httpx/httpcore BEFORE constructing any Telegram client.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("azure.identity").setLevel(logging.WARNING)

# --- Azure Monitor OpenTelemetry (Application Insights) ---------------------
# Wires traces, metrics, and logs to App Insights via the connection string
# in APPLICATIONINSIGHTS_CONNECTION_STRING. Safe no-op locally if either the
# env var or the package is missing.
try:
    from azure.monitor.opentelemetry import configure_azure_monitor

    if os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING"):
        configure_azure_monitor()
except ImportError:  # pragma: no cover — local dev without the package installed
    pass

from opentelemetry import trace

tracer = trace.get_tracer("mindMe.harness")

log = logging.getLogger("mindMe.harness")

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

# --- Module-level singletons ------------------------------------------------

_credential = DefaultAzureCredential()
_project: AIProjectClient | None = None
_openai = None
_blob: BlobServiceClient | None = None
_http: httpx.Client | None = None


def _foundry() -> tuple[AIProjectClient, object]:
    global _project, _openai
    if _project is None:
        endpoint = os.environ["AZURE_AI_PROJECT_ENDPOINT"]
        _project = AIProjectClient(endpoint=endpoint, credential=_credential)
        _openai = _project.get_openai_client()
    return _project, _openai


def _blob_client() -> BlobServiceClient:
    global _blob
    if _blob is None:
        account = os.environ["AZURE_STORAGE_ACCOUNT"]
        _blob = BlobServiceClient(
            account_url=f"https://{account}.blob.core.windows.net",
            credential=_credential,
        )
    return _blob


def _http_client() -> httpx.Client:
    global _http
    if _http is None:
        _http = httpx.Client(timeout=15.0)
    return _http


# --- Telegram helpers -------------------------------------------------------

TELEGRAM_API = "https://api.telegram.org"


def _telegram_send(chat_id: int, text: str) -> None:
    """Send a Telegram message. URL contains the token — caller must trust the
    pre-silenced httpx logger (Hard Rule 8). Span attributes carry size/status
    only (Hard Rule 9) — NEVER the URL or message text."""
    with tracer.start_as_current_span("telegram.send") as span:
        span.set_attribute("chat_id", chat_id)
        span.set_attribute("message.length", len(text))
        token = os.environ["TELEGRAM_BOT_TOKEN"]
        url = f"{TELEGRAM_API}/bot{token}/sendMessage"
        resp = _http_client().post(url, json={"chat_id": chat_id, "text": text})
        span.set_attribute("http.status_code", resp.status_code)
        resp.raise_for_status()


def _verify_telegram_secret(req: func.HttpRequest) -> bool:
    expected = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    if not expected:
        return False
    return req.headers.get("X-Telegram-Bot-Api-Secret-Token") == expected


def _is_allowed_chat(chat_id: int | None) -> bool:
    """Hard Rule 2: never widen the allowlist."""
    if chat_id is None:
        return False
    allowed_raw = os.environ.get("TELEGRAM_ALLOWED_CHAT_ID")
    if not allowed_raw:
        return False
    try:
        return int(allowed_raw) == chat_id
    except ValueError:
        return False


# --- Capture front door -----------------------------------------------------
# mindMe and memex share one Telegram bot (a bot has one webhook), so mindMe owns
# the webhook and forwards capture-intent updates to memex's capture engine. The
# companion stays the default for ordinary conversation.

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_CAPTURE_PREFIX_RE = re.compile(r"^\s*(save|note|idea|n)\s*[:\-]", re.IGNORECASE)


def _is_capture_intent(text: str) -> bool:
    """A message is a capture (not a chat) if it is prefixed save:/note:/idea:/n:
    or contains a URL."""
    if not text:
        return False
    return bool(_CAPTURE_PREFIX_RE.match(text) or _URL_RE.search(text))


def _forward_to_memex(update: dict) -> bool:
    """Forward a raw Telegram update to memex's mindMe capture webhook.

    Returns True if forwarded. The URL (incl. the function key as ?code=) is held
    in MEMEX_WEBHOOK_URL. Token-bearing URLs are never logged (Hard Rule 8)."""
    target = os.environ.get("MEMEX_WEBHOOK_URL")
    if not target:
        log.warning("capture forward skipped: MEMEX_WEBHOOK_URL not set")
        return False
    with tracer.start_as_current_span("capture.forward") as span:
        try:
            resp = _http_client().post(target, json=update)
            span.set_attribute("http.status_code", resp.status_code)
            return resp.status_code < 400
        except httpx.HTTPError:
            log.exception("capture forward failed")
            return False


# --- dig: deep-research front door (Mode B) ---------------------------------
# `/dig <question>` opens a labelled 'dig' issue in the mindVault repo. A workflow
# there (dig-assign.yml) assigns the Copilot coding agent, which runs the research
# and opens a PR with the report. Reasoning runs on Copilot, not Azure.

GITHUB_API = "https://api.github.com"
DIG_REPO_DEFAULT = "samoletovs/mindVault"


def _create_dig_issue(question: str) -> str | None:
    """Create a labelled 'dig' research issue and return its URL (or None).
    Hard Rule 1: never log the question text — only lengths/status."""
    token = os.environ.get("DIG_GITHUB_TOKEN")
    if not token:
        log.warning("dig issue skipped: DIG_GITHUB_TOKEN not set")
        return None
    repo = os.environ.get("DIG_REPO", DIG_REPO_DEFAULT)
    title = "[dig] " + (question[:60].strip() or "research request")
    body = (
        "Deep-research request fired from Telegram (Mode B).\n\n"
        f"## Question\n{question}\n\n"
        "## Execution method (lead research agent — orchestrator/worker)\n"
        "1. PLAN: restate the question; default to standard effort (3–4 subagents); decompose into non-overlapping sub-questions.\n"
        "2. RESEARCH each sub-question via web search/fetch + relevant MCP tools; 4–8 sources each; start broad then narrow.\n"
        "3. Capture a SOURCE URL for every key claim; prefer primary/official sources.\n"
        "4. SYNTHESIZE: merge, dedupe, resolve contradictions explicitly.\n"
        "5. SAVE a markdown report to `02_areas/agents/research/YYYY-MM-DD-<slug>.md` with TL;DR, themed sections with inline citations, a 'So what (for me)' section, and a 'confidence + gaps' note.\n"
        "GUARDRAILS: markdown only; citations required; no invented sources/numbers; if anything sensitive surfaces, leave a reference-note (system.md §7). Open a PR titled 'dig: <question>'."
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    with tracer.start_as_current_span("dig.create_issue") as span:
        span.set_attribute("question.length", len(question))
        try:
            resp = _http_client().post(
                f"{GITHUB_API}/repos/{repo}/issues",
                json={"title": title, "body": body, "labels": ["dig"]},
                headers=headers,
            )
            span.set_attribute("http.status_code", resp.status_code)
        except httpx.HTTPError:
            log.exception("dig issue create failed (network)")
            return None
        if resp.status_code >= 400:
            log.error("dig issue create failed status=%d", resp.status_code)
            return None
        return resp.json().get("html_url")


# --- Foundry call -----------------------------------------------------------

def _ask_companion(user_text: str, conversation_id: str | None = None) -> str:
    """Forward to the hosted prompt agent. Returns plain text or '(empty reply)'.

    Span attributes carry agent name, input/output **lengths**, and conversation
    presence flag only (Hard Rule 9) — NEVER prompts or completions."""
    with tracer.start_as_current_span("ask_companion") as span:
        span.set_attribute("input.length", len(user_text))
        span.set_attribute("has_conversation_id", conversation_id is not None)

        _, openai_client = _foundry()
        agent_name = os.environ.get("AZURE_AI_AGENT_NAME", "companion")
        span.set_attribute("agent.name", agent_name)

        if conversation_id is None:
            conv = openai_client.conversations.create()
            conversation_id = conv.id

        response = openai_client.responses.create(
            conversation=conversation_id,
            input=user_text,
            extra_body={
                "agent_reference": {
                    "name": agent_name,
                    "type": "agent_reference",
                }
            },
        )
        text = (response.output_text or "").strip() or "(empty reply)"
        span.set_attribute("output.length", len(text))
        return text


# --- Briefing snapshot (built from personal-os blob container) -------------
#
# Source of truth lives in the `personal-os` container of the SA. The Function
# reads the markdown files directly with managed identity and builds a
# sanitized snapshot in-process. No application-layer encryption — the
# container is private, RBAC-gated, and Microsoft-managed at-rest encryption
# applies. See docs/architecture.md for the rationale and how to re-add an
# AES-GCM layer if you change your mind.

PERSONAL_OS_CONTAINER_DEFAULT = "personal-os"
_personal_os_container = None


def _os_container_client():
    global _personal_os_container
    if _personal_os_container is None:
        name = os.environ.get(
            "AZURE_STORAGE_PERSONAL_OS_CONTAINER", PERSONAL_OS_CONTAINER_DEFAULT
        )
        _personal_os_container = _blob_client().get_container_client(name)
    return _personal_os_container


def _read_os_text(rel_path: str) -> str:
    """Read a markdown blob by relative path; return '' if it doesn't exist."""
    blob = _os_container_client().get_blob_client(rel_path)
    try:
        data = blob.download_blob().readall()
    except Exception:
        return ""
    return data.decode("utf-8", errors="replace")


def _extract_dashboard_sections(text: str) -> dict:
    """Parse `_dashboard.md` into a few well-known slices."""
    sections: dict[str, list[str]] = {}
    current_key: str | None = None
    current_lines: list[str] = []

    for line in text.splitlines():
        header = re.match(r"^##\s+(.+?)\s*$", line)
        if header:
            if current_key is not None:
                sections[current_key] = current_lines
            current_key = header.group(1).strip().lower()
            current_lines = []
        else:
            if current_key is not None:
                current_lines.append(line)
    if current_key is not None:
        sections[current_key] = current_lines

    def first_bullets(key_substrings: list[str], limit: int = 5) -> list[str]:
        for k, lines in sections.items():
            if any(s in k for s in key_substrings):
                bullets = [
                    re.sub(r"^[-*]\s+", "", ln).strip()
                    for ln in lines
                    if re.match(r"^\s*[-*]\s+", ln)
                ]
                return [b for b in bullets if b][:limit]
        return []

    return {
        "top_goals": first_bullets(["top goal", "goal"]),
        "this_week": first_bullets(["this week", "week"]),
        "today_focus": " ".join(first_bullets(["today", "focus"])) or "",
    }


def _extract_journal_summary(text: str, journal_date: str) -> dict:
    if not text:
        return {"date": None, "open_loops_count": 0, "mood": "", "energy": ""}
    open_loops = len(re.findall(r"^\s*-\s*\[\s*\]", text, flags=re.MULTILINE))
    mood = ""
    energy = ""
    mood_match = re.search(r"Mood\s*:\s*([0-9]{1,2})", text)
    energy_match = re.search(r"Energy\s*:\s*([0-9]{1,2})", text)
    if mood_match:
        mood = mood_match.group(1)
    if energy_match:
        energy = energy_match.group(1)
    return {
        "date": journal_date,
        "open_loops_count": open_loops,
        "mood": mood,
        "energy": energy,
    }


def _list_area_h1s(limit: int = 8) -> list[str]:
    """H1 of each `02_areas/<area>/README.md`, in alphabetical order."""
    headlines: list[str] = []
    container = _os_container_client()
    blobs = container.list_blobs(name_starts_with="02_areas/")
    readmes = sorted(
        b.name for b in blobs
        if b.name.endswith("/README.md") and b.name.count("/") == 2
    )
    for name in readmes:
        text = _read_os_text(name)
        first_line = next((ln for ln in text.splitlines() if ln.strip()), "")
        if first_line.startswith("# "):
            headlines.append(first_line[2:].strip())
        if len(headlines) >= limit:
            break
    return headlines


def _build_briefing_snapshot() -> dict:
    with tracer.start_as_current_span("build_briefing_snapshot") as span:
        today = date.today()
        snapshot: dict = {"date": today.isoformat()}

        dashboard_text = _read_os_text("_dashboard.md")
        span.set_attribute("dashboard.length", len(dashboard_text))
        if dashboard_text:
            snapshot.update(_extract_dashboard_sections(dashboard_text))
        else:
            snapshot.update({"top_goals": [], "this_week": [], "today_focus": ""})

        journal_rel = (
            f"05_journal/{today.year}/{today.year}-{today.month:02d}-{today.day:02d}.md"
        )
        journal_text = _read_os_text(journal_rel)
        span.set_attribute("journal.length", len(journal_text))
        snapshot["yesterday"] = _extract_journal_summary(
            journal_text,
            journal_date=journal_rel.split("/")[-1].removesuffix(".md"),
        )

        snapshot["areas"] = _list_area_h1s()
        span.set_attribute("areas.count", len(snapshot["areas"]))
        return snapshot


def _is_tiered_briefing(data: dict) -> bool:
    return isinstance(data.get("tiers"), dict)


def _normalize_tier_name(tier: str | None) -> str:
    value = (tier or "core").strip().lower()
    return value if value in {"core", "extended", "deep"} else "core"


def _parse_bool_param(value: object, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ValueError("invalid boolean value")


def _select_briefing_view(data: dict, tier: str, include_meta: bool) -> dict:
    """Return the requested briefing tier while preserving legacy compatibility."""
    if not _is_tiered_briefing(data):
        if tier == "core":
            return data
        empty = {"entries": []}
        if include_meta:
            empty["_meta"] = {
                "schema_version": data.get("schema_version", "1.x"),
                "legacy_format": True,
            }
        return empty

    fallback = {} if tier == "core" else {"entries": []}
    selected = data["tiers"].get(tier, fallback)
    if not isinstance(selected, dict):
        selected = dict(fallback)
    result = dict(selected)

    if include_meta:
        result["_meta"] = {
            "schema_version": data.get("schema_version", "2.x"),
            "date": data.get("date"),
            "generated_at": data.get("generated_at"),
            "tier": tier,
            "meta": data.get("meta", {}),
        }
    return result


def _weather_summary(location: str) -> dict:
    """Fetch and normalize weather summary data for a location.

    Raises:
        httpx.HTTPError: If wttr.in is unreachable or returns non-2xx.
        ValueError: If response parsing fails unexpectedly.
    """
    safe_location = quote(location, safe="")
    resp = _http_client().get(f"https://wttr.in/{safe_location}?format=j1")
    resp.raise_for_status()
    full = resp.json()
    current = (full.get("current_condition") or [{}])[0]
    description = (current.get("weatherDesc") or [{}])[0].get("value") or "N/A"
    return {
        "location": location,
        "temp_c": current.get("temp_C") or "N/A",
        "feels_like_c": current.get("FeelsLikeC") or "N/A",
        "description": description,
        "wind_kph": current.get("windspeedKmph") or "N/A",
        "humidity_pct": current.get("humidity") or "N/A",
    }


# --- Function: telegram_webhook --------------------------------------------

@app.function_name(name="telegram_webhook")
@app.route(route="telegram_webhook", methods=["POST"])
def telegram_webhook(req: func.HttpRequest) -> func.HttpResponse:
    if not _verify_telegram_secret(req):
        log.warning("webhook rejected: bad secret")
        return func.HttpResponse("forbidden", status_code=403)

    try:
        update = req.get_json()
    except ValueError:
        return func.HttpResponse("bad request", status_code=400)

    # Inline-keyboard button taps (note review) belong to the memex capture
    # engine — forward and return before any companion handling.
    if "callback_query" in update:
        _forward_to_memex(update)
        return func.HttpResponse("ok", status_code=200)

    message = update.get("message") or update.get("edited_message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    user_text = (message.get("text") or "").strip()

    if not _is_allowed_chat(chat_id):
        log.warning("webhook rejected: unauthorized chat_id=%s", chat_id)
        return func.HttpResponse("ok", status_code=200)  # silent drop

    # Voice / audio notes are always captures → memex (transcribe + draft).
    if message.get("voice") or message.get("audio"):
        _forward_to_memex(update)
        return func.HttpResponse("ok", status_code=200)

    if not user_text:
        return func.HttpResponse("ok", status_code=200)

    # Command: /dig <question> → open a Mode B deep-research issue. Handled BEFORE
    # capture routing, since the question may contain a URL that would otherwise
    # look like a capture and get forwarded to memex.
    if user_text == "/dig" or user_text.startswith("/dig "):
        question = user_text[4:].strip()
        if not question:
            _telegram_send(chat_id, "usage: /dig <research question>")
        else:
            issue_url = _create_dig_issue(question)
            _telegram_send(
                chat_id,
                f"\U0001f50e dig started: {issue_url}\nCopilot is researching — report will land in mindVault."
                if issue_url
                else "couldn't start dig — DIG_GITHUB_TOKEN may be missing. check the function logs.",
            )
        return func.HttpResponse("ok", status_code=200)

    # Capture intent (save:/note:/idea:/n: or a URL) → memex capture engine.
    # Everything else is a conversation with the companion.
    if _is_capture_intent(user_text):
        _forward_to_memex(update)
        return func.HttpResponse("ok", status_code=200)

    started = time.monotonic()
    try:
        if user_text == "/ping":
            reply = "pong"
        elif user_text == "/status":
            reply = "phase 2 webhook. agent=companion. tools=[briefing_context, weather]. commands=[dig]."
        elif user_text == "/help":
            reply = "/ping | /status | /reset | /dig <question> | /help — anything else goes to mindMe."
        else:
            reply = _ask_companion(user_text)
    except Exception:
        log.exception("agent error chat=%s in_len=%d", chat_id, len(user_text))
        _telegram_send(chat_id, "mindMe hit an error. check the function logs.")
        return func.HttpResponse("ok", status_code=200)

    _telegram_send(chat_id, reply)
    log.info(
        "round-trip chat=%s in_len=%d out_len=%d duration=%.2fs",
        chat_id, len(user_text), len(reply), time.monotonic() - started,
    )
    return func.HttpResponse("ok", status_code=200)


# --- Function: morning_briefing_timer --------------------------------------

@app.function_name(name="morning_briefing_timer")
@app.timer_trigger(
    schedule="0 30 7 * * *",
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
def morning_briefing_timer(timer: func.TimerRequest) -> None:
    started = time.monotonic()
    chat_id = int(os.environ["TELEGRAM_ALLOWED_CHAT_ID"])

    try:
        seed = (
            "Compose my morning briefing. Call get_briefing_context for today's "
            "data and get_weather for the weather. Keep it 3 short paragraphs "
            "max: today's focus, what's open, the weather."
        )
        reply = _ask_companion(seed)
        _telegram_send(chat_id, reply)
        log.info(
            "briefing sent chat=%s out_len=%d duration=%.2fs",
            chat_id, len(reply), time.monotonic() - started,
        )
    except Exception:
        log.exception("briefing failed")
        try:
            fallback = (
                "mindMe could not load your personal briefing context today. "
                "I can still send weather: "
            )
            try:
                weather = _weather_summary("Stockholm")
                fallback += (
                    f"{weather.get('location')} {weather.get('temp_c')}°C "
                    f"(feels {weather.get('feels_like_c')}°C), {weather.get('description')}."
                )
            except Exception:
                log.exception("briefing fallback weather failed")
                fallback = (
                    "mindMe could not assemble today's briefing or weather. "
                    "please try again later."
                )
            _telegram_send(chat_id, fallback)
        except Exception:
            log.exception("briefing fallback notify failed")


# --- Function: capture_drain (Phase 3 placeholder) -------------------------

@app.function_name(name="capture_drain")
@app.queue_trigger(
    arg_name="msg",
    queue_name="capture-events",
    connection="AzureWebJobsStorage",
)
def capture_drain(msg: func.QueueMessage) -> None:
    log.info("capture event received id=%s size=%d", msg.id, len(msg.get_body()))
    # Phase 3: forward to laptop sync daemon via separate queue / signed URL.


# --- Function: health ------------------------------------------------------

@app.function_name(name="health")
@app.route(route="health", methods=["GET"])
def health(req: func.HttpRequest) -> func.HttpResponse:
    del req
    return func.HttpResponse(
        json.dumps({"status": "ok", "agent": os.environ.get("AZURE_AI_AGENT_NAME")}),
        mimetype="application/json",
        status_code=200,
    )


# --- Foundry agent tools (HTTP endpoints) ----------------------------------

@app.function_name(name="tool_briefing_context")
@app.route(route="tools/briefing_context", methods=["POST"])
def tool_briefing_context(req: func.HttpRequest) -> func.HttpResponse:
    """Foundry agent tool: get_briefing_context().

    Returns today's sanitized JSON snapshot, built in-process from the
    `personal-os` blob container. No application-layer encryption.
    """
    req_json: dict = {}
    try:
        req_json = req.get_json()
    except ValueError:
        if req.get_body():
            return func.HttpResponse(
                json.dumps({"error": "invalid JSON body"}),
                mimetype="application/json",
                status_code=400,
            )
        req_json = {}

    tier = _normalize_tier_name(req.params.get("tier") or req_json.get("tier"))
    include_meta_raw = req.params.get("include_meta")
    if include_meta_raw is None:
        include_meta_source = req_json.get("include_meta", False)
    else:
        include_meta_source = include_meta_raw

    try:
        include_meta = _parse_bool_param(include_meta_source, default=False)
    except ValueError:
        return func.HttpResponse(
            json.dumps(
                {
                    "error": "invalid include_meta value",
                    "accepted": ["true", "false", "1", "0", "yes", "no", "on", "off"],
                }
            ),
            mimetype="application/json",
            status_code=400,
        )

    try:
        data = _load_briefing()
        view = _select_briefing_view(data, tier=tier, include_meta=include_meta)
    except Exception:
        log.exception("briefing_context build failed")
        return func.HttpResponse(
            json.dumps({"error": "briefing not available"}),
            mimetype="application/json",
            status_code=503,
        )
    return func.HttpResponse(
        json.dumps(view),
        mimetype="application/json",
        status_code=200,
    )


@app.function_name(name="tool_weather")
@app.route(route="tools/weather", methods=["GET"])
def tool_weather(req: func.HttpRequest) -> func.HttpResponse:
    """Foundry agent tool: get_weather(location)."""
    location = req.params.get("location") or "Riga"
    try:
        summary = _weather_summary(location)
        return func.HttpResponse(
            json.dumps(summary), mimetype="application/json", status_code=200
        )
    except Exception:
        log.exception("weather lookup failed location=%s", location)
        return func.HttpResponse(
            json.dumps({"error": "weather unavailable"}),
            mimetype="application/json",
            status_code=503,
        )
