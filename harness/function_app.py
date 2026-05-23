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
content. Rule 8: httpx logger silenced before any Telegram call.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from urllib.parse import quote

import azure.functions as func
import httpx
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Hard Rule 8: silence httpx/httpcore BEFORE constructing any Telegram client.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("azure.identity").setLevel(logging.WARNING)

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
    pre-silenced httpx logger (Hard Rule 8)."""
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    url = f"{TELEGRAM_API}/bot{token}/sendMessage"
    resp = _http_client().post(url, json={"chat_id": chat_id, "text": text})
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


# --- Foundry call -----------------------------------------------------------

def _ask_companion(user_text: str, conversation_id: str | None = None) -> str:
    """Forward to the hosted prompt agent. Returns plain text or '(empty reply)'."""
    _, openai_client = _foundry()
    agent_name = os.environ.get("AZURE_AI_AGENT_NAME", "companion")

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
    return (response.output_text or "").strip() or "(empty reply)"


# --- Briefing encryption ----------------------------------------------------

def _decrypt_briefing(blob_bytes: bytes) -> dict:
    """Format: [12-byte nonce][ciphertext+tag]. Key is 32 bytes base64 in env."""
    key_b64 = os.environ["BRIEFING_ENCRYPTION_KEY"]
    key = base64.b64decode(key_b64)
    if len(key) != 32:
        raise ValueError("BRIEFING_ENCRYPTION_KEY must be 32 bytes (base64-encoded).")
    if len(blob_bytes) < 13:
        raise ValueError("Briefing blob too short.")
    nonce, ciphertext = blob_bytes[:12], blob_bytes[12:]
    aesgcm = AESGCM(key)
    plaintext = aesgcm.decrypt(nonce, ciphertext, associated_data=None)
    return json.loads(plaintext.decode("utf-8"))


def _load_briefing() -> dict:
    container = os.environ["AZURE_STORAGE_BRIEFING_CONTAINER"]
    blob = _blob_client().get_blob_client(container=container, blob="today.bin")
    raw = blob.download_blob().readall()
    return _decrypt_briefing(raw)


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

    message = update.get("message") or update.get("edited_message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    user_text = (message.get("text") or "").strip()

    if not _is_allowed_chat(chat_id):
        log.warning("webhook rejected: unauthorized chat_id=%s", chat_id)
        return func.HttpResponse("ok", status_code=200)  # silent drop

    if not user_text:
        return func.HttpResponse("ok", status_code=200)

    started = time.monotonic()
    try:
        if user_text == "/ping":
            reply = "pong"
        elif user_text == "/status":
            reply = "phase 2 webhook. agent=companion. tools=[briefing_context, weather]."
        elif user_text == "/help":
            reply = "/ping | /status | /reset | /help — anything else goes to mindMe."
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

    Returns today's sanitized JSON snapshot (decrypted from Blob).
    """
    try:
        data = _load_briefing()
    except Exception:
        log.exception("briefing_context load failed")
        return func.HttpResponse(
            json.dumps({"error": "briefing not available"}),
            mimetype="application/json",
            status_code=503,
        )
    return func.HttpResponse(
        json.dumps(data),
        mimetype="application/json",
        status_code=200,
    )


@app.function_name(name="tool_weather")
@app.route(route="tools/weather", methods=["GET"])
def tool_weather(req: func.HttpRequest) -> func.HttpResponse:
    """Foundry agent tool: get_weather(location)."""
    location = req.params.get("location") or "Stockholm"
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
