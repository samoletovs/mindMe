"""Register the mindMe bot's slash-command menu via Telegram ``setMyCommands``.

Run this AFTER deploying so typing ``/`` in the chat shows the command list.

Security: the bot token is a secret. This script reads it from the
``TELEGRAM_BOT_TOKEN`` environment variable (or load it from Key Vault into the
env first) — it is never hard-coded and never printed. Run it yourself; do not
paste the token into any shared tool.

    # PowerShell, from the mindMe/ repo root:
    #   $env:TELEGRAM_BOT_TOKEN = (az keyvault secret show --vault-name <kv> `
    #       --name telegram-bot-token --query value -o tsv)
    .\\.venv\\Scripts\\python.exe scripts\\dev\\set_bot_commands.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

# Order here is the order shown in the Telegram menu. Capture verbs first.
COMMANDS = [
    {"command": "note", "description": "Save a note"},
    {"command": "idea", "description": "Save an idea to revisit"},
    {"command": "task", "description": "Create a task"},
    {"command": "dig", "description": "Request research on a question"},
    {"command": "status", "description": "Check your vault status"},
    {"command": "briefing", "description": "Choose morning briefing sections"},
    {"command": "review", "description": "Review weekly progress and priorities"},
    {"command": "help", "description": "What mindMe can do"},
]


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print(
            "TELEGRAM_BOT_TOKEN not set — export it (or load it from Key Vault) "
            "and re-run. The token is a secret; never commit or paste it.",
            file=sys.stderr,
        )
        return 2

    payload = json.dumps({"commands": COMMANDS}).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/setMyCommands",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # pragma: no cover - network/CLI helper
        # Never echo the URL (it contains the token) — just the error type.
        print(f"setMyCommands request failed: {type(exc).__name__}", file=sys.stderr)
        return 1

    if body.get("ok"):
        print(f"registered {len(COMMANDS)} commands")
        return 0
    print(f"setMyCommands rejected: {body.get('description')}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
