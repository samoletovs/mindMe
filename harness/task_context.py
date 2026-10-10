"""Strict owner/message binding for memex's read-only task pointer."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

from briefing_sources import SourceError
from execution_budget import bounded_timeout, checkpoint
from task_sources import task_path


def parse_task_callback(value: object) -> str | None:
    match = re.fullmatch(r"task1\|clarify\|([a-f0-9]{32})", value) if isinstance(value, str) else None
    return match[1] if match else None


def task_context(
    client: httpx.Client, *, url: str, repo: str, chat_id: int, owner_id: int,
    message_id: int, task_key: str | None = None,
) -> dict[str, Any] | None:
    endpoint = urlsplit(url)
    if (
        endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.password
        or any(type(value) is not int or not 0 < value <= 2**53 - 1 for value in (chat_id, owner_id, message_id))
        or owner_id != chat_id or (task_key is not None and not re.fullmatch(r"[a-f0-9]{32}", task_key))
    ):
        raise SourceError("task_binding_invalid")
    packet = {
        "operation": "task_context", "version": 1, "chat_id": chat_id,
        "owner_id": owner_id, "message_id": message_id,
    }
    if task_key is not None:
        packet["task_key"] = task_key
    try:
        with client.stream(
            "POST", url, json=packet, follow_redirects=False, timeout=bounded_timeout(15, stages=4),
        ) as response:
            if response.status_code != 200:
                raise SourceError("task_context_unavailable")
            raw = bytearray()
            for chunk in response.iter_bytes():
                checkpoint()
                raw.extend(chunk)
                if len(raw) > 4096:
                    raise SourceError("task_context_invalid")
        data = json.loads(raw)
    except (ValueError, httpx.HTTPError):
        raise SourceError("task_context_unavailable") from None
    if data == {"version": 1, "status": "unmatched"}:
        return None
    required = {
        "version", "status", "publication_status", "action_id", "path",
        "pr_url", "source_revision", "clarification_limit",
    }
    if (
        not isinstance(data, dict) or not required <= data.keys()
        or not data.keys() <= required | {"canonical_revision"}
        or type(data["version"]) is not int or data["version"] != 1
        or data["status"] != "matched" or not isinstance(data["publication_status"], str)
        or data["publication_status"] not in {"submitted", "merged"}
        or data["clarification_limit"] != 3 or type(data["clarification_limit"]) is not int
        or not isinstance(data["action_id"], str) or not re.fullmatch(r"[a-f0-9]{32}", data["action_id"])
        or (task_key is not None and task_key != data["action_id"])
        or not task_path(data["path"])
        or not isinstance(data["source_revision"], str) or not re.fullmatch(r"[a-f0-9]{40}", data["source_revision"])
        or not isinstance(data["pr_url"], str)
        or not re.fullmatch(rf"https://github\.com/{re.escape(repo)}/pull/[1-9]\d*", data["pr_url"])
        or (data["publication_status"] == "merged" and "canonical_revision" not in data)
        or ("canonical_revision" in data and (
            data["publication_status"] != "merged" or not isinstance(data["canonical_revision"], str)
            or not re.fullmatch(r"[a-f0-9]{40}", data["canonical_revision"])
        ))
    ):
        raise SourceError("task_context_invalid")
    return data
