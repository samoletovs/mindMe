"""Read-only capture pointers; no caller-provided text can establish a binding."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

from briefing_sources import SourceError, _kind
from execution_budget import bounded_timeout, checkpoint

CAPTURE_ACTIONS = frozenset({"explain", "dig", "apply", "topic", "known", "useful"})


def parse_capture_callback(value: object) -> tuple[str, str] | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"cap1\|([a-z]+)\|([a-f0-9]{32})", value)
    if not match or match[1] not in CAPTURE_ACTIONS:
        return None
    return match[1], match[2]


def capture_context(
    client: httpx.Client, *, url: str, chat_id: int, message_id: int,
    capture_key: str | None = None,
) -> dict[str, Any] | None:
    if (
        not url or type(chat_id) is not int or type(message_id) is not int or message_id <= 0
        or (capture_key is not None and not re.fullmatch(r"[a-f0-9]{32}", capture_key))
    ):
        raise SourceError("capture_context_invalid")
    packet: dict[str, Any] = {
        "operation": "capture_context", "version": 1, "chat_id": chat_id, "message_id": message_id,
    }
    if capture_key is not None:
        packet["capture_key"] = capture_key
    try:
        checkpoint()
        with client.stream(
            "POST", url, json=packet, follow_redirects=False,
            timeout=bounded_timeout(20.0, stages=4),
        ) as response:
            if response.status_code != 200:
                raise SourceError("capture_context_unavailable")
            content = bytearray()
            for chunk in response.iter_bytes():
                checkpoint()
                content.extend(chunk)
                if len(content) > 4096:
                    raise SourceError("capture_context_invalid")
        data = json.loads(content)
    except (httpx.HTTPError, ValueError, UnicodeError):
        raise SourceError("capture_context_unavailable") from None
    if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
        raise SourceError("capture_context_invalid")
    if data == {"version": 1, "status": "unmatched"}:
        return None
    if set(data) != {"version", "status", "source_id", "source_path", "source_url", "title"}:
        raise SourceError("capture_context_invalid")
    if (
        data["status"] != "ready"
        or not isinstance(data["source_id"], str) or not re.fullmatch(r"[a-f0-9]{64}", data["source_id"])
        or not isinstance(data["source_path"], str) or not data["source_path"].startswith("wiki/sources/")
        or _kind(data["source_path"]) != "wiki"
        or not isinstance(data["title"], str) or not 1 <= len(data["title"]) <= 300
        or not isinstance(data["source_url"], str) or len(data["source_url"]) > 2048
        or (capture_key is not None and data["source_id"][:32] != capture_key)
    ):
        raise SourceError("capture_context_invalid")
    try:
        if any(ord(char) < 32 or ord(char) == 127 for char in data["source_url"]):
            raise ValueError
        parsed = urlsplit(data["source_url"])
        if (
            parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
        ):
            raise ValueError
    except ValueError:
        raise SourceError("capture_context_invalid") from None
    return data
