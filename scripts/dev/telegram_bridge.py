"""Local bridge: Telegram long-poll <-> Foundry hosted agent ``companion``.

Single-user. Enforces the allowlist (``TELEGRAM_ALLOWED_CHAT_ID``) from the
first line of every handler. Other users are silently dropped.

This is dev/smoke-test scaffolding. It runs on your laptop with ``python``
and disappears when you Ctrl-C. Phase 2 of the project replaces this with an
Azure Function App. The Foundry agent (created by ``create_agent.py``) stays.

Logging policy (AGENTS.md rule 1): IDs, sizes, durations only. Never message
content.

Run from the comes/ repo root::

    .\\.venv\\Scripts\\python.exe scripts\\dev\\telegram_bridge.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = REPO_ROOT / ".env"
CACHE_DIR = REPO_ROOT / ".cache"
CONV_CACHE_PATH = CACHE_DIR / "conversation.json"

log = logging.getLogger("comes.bridge")


def _load_env() -> dict[str, str]:
    load_dotenv(ENV_PATH)
    required = [
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_ALLOWED_CHAT_ID",
        "AZURE_AI_PROJECT_ENDPOINT",
        "AZURE_AI_AGENT_NAME",
    ]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        log.error("Missing required .env vars: %s", ", ".join(missing))
        sys.exit(2)
    return {k: os.environ[k] for k in required}


def _load_conversation_id() -> str | None:
    if not CONV_CACHE_PATH.exists():
        return None
    try:
        return json.loads(CONV_CACHE_PATH.read_text(encoding="utf-8")).get("id")
    except Exception:
        return None


def _save_conversation_id(conv_id: str) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    CONV_CACHE_PATH.write_text(
        json.dumps({"id": conv_id}, indent=2), encoding="utf-8"
    )


def _clear_conversation_id() -> None:
    if CONV_CACHE_PATH.exists():
        CONV_CACHE_PATH.unlink()


class FoundryAgent:
    """Thin wrapper around the Foundry responses API for ``companion``."""

    def __init__(self, endpoint: str, agent_name: str) -> None:
        self.agent_name = agent_name
        self._project = AIProjectClient(
            endpoint=endpoint, credential=DefaultAzureCredential()
        )
        self._openai = self._project.get_openai_client()
        self._conv_id = _load_conversation_id()

    def _ensure_conversation(self) -> str:
        if self._conv_id:
            return self._conv_id
        conv = self._openai.conversations.create()
        self._conv_id = conv.id
        _save_conversation_id(conv.id)
        log.info("conversation created id=%s", conv.id)
        return conv.id

    def reset(self) -> None:
        self._conv_id = None
        _clear_conversation_id()
        log.info("conversation reset")

    def ask(self, user_text: str) -> str:
        """Forward ``user_text`` to the agent and return ``output_text``.

        Synchronous SDK call - the caller should run this via ``to_thread``.
        """
        conv_id = self._ensure_conversation()
        response = self._openai.responses.create(
            conversation=conv_id,
            input=user_text,
            extra_body={
                "agent_reference": {
                    "name": self.agent_name,
                    "type": "agent_reference",
                }
            },
        )
        return (response.output_text or "").strip() or "(empty reply)"


# ---------- Telegram handlers ----------

async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "comes is here. This is a smoke test - no tools yet, just chat. "
        "Try /ping, /reset, or just write something."
    )


async def cmd_ping(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("pong")


async def cmd_help(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "/ping - health check\n"
        "/reset - start a new conversation\n"
        "/status - show what comes can do right now\n"
        "/help - this message"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    agent: FoundryAgent = context.application.bot_data["agent"]
    await update.message.reply_text(
        f"phase 1 smoke test. agent={agent.agent_name}. "
        "no tools wired yet (briefing, capture, weather all come in phase 2)."
    )


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    agent: FoundryAgent = context.application.bot_data["agent"]
    agent.reset()
    await update.message.reply_text("conversation reset. starting fresh.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    agent: FoundryAgent = context.application.bot_data["agent"]
    msg = update.message
    text = msg.text or ""
    chat_id = msg.chat_id
    in_len = len(text)
    started = time.monotonic()

    try:
        reply = await asyncio.to_thread(agent.ask, text)
    except Exception:
        log.exception("agent error chat=%s in_len=%d", chat_id, in_len)
        await msg.reply_text("comes hit an error. check the bridge log.")
        return

    duration = time.monotonic() - started
    out_len = len(reply)
    log.info(
        "round-trip chat=%s msg=%s in_len=%d out_len=%d duration=%.2fs",
        chat_id, msg.message_id, in_len, out_len, duration,
    )
    await msg.reply_text(reply)


async def handle_unauthorized(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.effective_user:
        log.warning(
            "unauthorized access attempt user_id=%s",
            update.effective_user.id,
        )


# ---------- Entry point ----------

def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    cfg = _load_env()
    allowed_id = int(cfg["TELEGRAM_ALLOWED_CHAT_ID"])

    agent = FoundryAgent(
        endpoint=cfg["AZURE_AI_PROJECT_ENDPOINT"],
        agent_name=cfg["AZURE_AI_AGENT_NAME"],
    )
    log.info(
        "bridge starting agent=%s allowed_user=%s",
        agent.agent_name, allowed_id,
    )

    app = Application.builder().token(cfg["TELEGRAM_BOT_TOKEN"]).build()
    app.bot_data["agent"] = agent

    allow = filters.User(user_id=allowed_id)
    app.add_handler(CommandHandler("start", cmd_start, filters=allow))
    app.add_handler(CommandHandler("ping", cmd_ping, filters=allow))
    app.add_handler(CommandHandler("help", cmd_help, filters=allow))
    app.add_handler(CommandHandler("status", cmd_status, filters=allow))
    app.add_handler(CommandHandler("reset", cmd_reset, filters=allow))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & allow, handle_text)
    )
    # Catch-all for anyone NOT on the allowlist
    app.add_handler(MessageHandler(~allow, handle_unauthorized))

    log.info("polling Telegram - Ctrl-C to stop")
    app.run_polling(stop_signals=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
