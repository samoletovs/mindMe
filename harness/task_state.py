"""Bounded task-workspace permissions and receipts in the existing private ledger."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from briefing_state import StateError

HEX24 = re.compile(r"[a-f0-9]{24}\Z")
HEX32 = re.compile(r"[a-f0-9]{32}\Z")
SHA = re.compile(r"[a-f0-9]{40}\Z")
HASH = re.compile(r"[a-f0-9]{64}\Z")
STAGES = ("clarify", "backlog", "ready", "doing", "waiting", "verify")
AREAS = (
    "agents", "learning", "personal-growth", "business", "family", "home",
    "travel", "health", "finances", "career", "legal",
)
PROJECT = re.compile(r"\d{4}-[a-z0-9]+(?:-[a-z0-9]+)*\Z")
CAPS = {"clarifications": 100, "bindings": 300, "requests": 300, "auth_nonces": 40, "auth_sessions": 8}


def empty_workspace() -> dict[str, Any]:
    return {
        "active_projects": {}, "standing": {"enabled": False, "sources": {}},
        **{name: {} for name in CAPS},
        "budget": {"month": "", "used": 0, "day": "", "daily_used": 0},
    }


def workspace(state: dict[str, Any]) -> dict[str, Any]:
    return state.setdefault("task_workspace", empty_workspace())


def _refs(value: object, limit: int) -> bool:
    return (
        isinstance(value, dict) and len(value) <= limit
        and all(isinstance(path, str) and 1 <= len(path) <= 240
                and isinstance(sha, str) and SHA.fullmatch(sha)
                for path, sha in value.items())
    )


def validate_workspace(value: object) -> None:
    if not isinstance(value, dict) or set(value) != set(empty_workspace()):
        raise StateError("invalid_task_workspace")
    if not _refs(value["active_projects"], 8) or any(
        not PROJECT.fullmatch(identifier) for identifier in value["active_projects"]
    ):
        raise StateError("invalid_task_project_selection")
    standing = value["standing"]
    if (
        not isinstance(standing, dict) or set(standing) != {"enabled", "sources"}
        or type(standing["enabled"]) is not bool or not _refs(standing["sources"], 3)
    ):
        raise StateError("invalid_task_standing_permission")
    for name, cap in CAPS.items():
        records = value[name]
        if not isinstance(records, dict) or len(records) > cap:
            raise StateError("task_workspace_capacity")
        if any(not isinstance(key, str) or not key for key in records):
            raise StateError("invalid_task_workspace_key")
    for name in ("auth_nonces", "auth_sessions"):
        if any(
            not HASH.fullmatch(key) or type(expiry) is not int or expiry <= 0
            for key, expiry in value[name].items()
        ):
            raise StateError("invalid_task_auth_receipt")
    for key, record in value["requests"].items():
        if (
            not HASH.fullmatch(key) or not isinstance(record, dict)
            or set(record) - {"digest", "status", "proposal_id", "created_on"}
            or not isinstance(record.get("digest"), str) or not HASH.fullmatch(record["digest"])
            or record.get("status") not in {"claimed", "recorded", "sending", "sent", "uncertain"}
        ):
            raise StateError("invalid_task_request_receipt")
        if "proposal_id" in record and not HEX24.fullmatch(record["proposal_id"]):
            raise StateError("invalid_task_request_receipt")
    for identifier, record in value["clarifications"].items():
        if (
            not HEX24.fullmatch(identifier) or not isinstance(record, dict)
            or type(record.get("turns")) is not int or not 0 <= record["turns"] <= 3
            or not isinstance(record.get("changes"), dict) or len(record["changes"]) > 10
            or not isinstance(record.get("source_revision"), str)
            or not SHA.fullmatch(record["source_revision"])
            or not isinstance(record.get("source_path"), str)
            or record.get("field") not in {None, "outcome", "next_action", "done_when", "area", "execution"}
            or not isinstance(record.get("expires_on"), str)
        ):
            raise StateError("invalid_task_clarification")
        try:
            date.fromisoformat(record["expires_on"])
        except ValueError:
            raise StateError("invalid_task_clarification") from None
        if record.get("question_token") is not None and (
            not isinstance(record["question_token"], str) or not HEX32.fullmatch(record["question_token"])
        ):
            raise StateError("invalid_task_question_binding")
    for key, binding in value["bindings"].items():
        if (
            not re.fullmatch(r"[1-9]\d*", key) or not isinstance(binding, dict)
            or set(binding) not in ({"clarification_id", "field", "turn"}, {"clarification_id", "field", "turn", "question_token"})
            or not isinstance(binding["clarification_id"], str)
            or binding["clarification_id"] not in value["clarifications"]
            or binding["field"] not in {"outcome", "next_action", "done_when", "area", "execution"}
            or type(binding["turn"]) is not int or not 0 <= binding["turn"] < 3
        ):
            raise StateError("invalid_task_message_binding")
        if "question_token" in binding and (
            not isinstance(binding["question_token"], str) or not HEX32.fullmatch(binding["question_token"])
        ):
            raise StateError("invalid_task_question_binding")
    budget = value["budget"]
    if (
        not isinstance(budget, dict) or set(budget) != {"month", "used", "day", "daily_used"}
        or any(type(budget[key]) is not int or budget[key] < 0 for key in ("used", "daily_used"))
        or not isinstance(budget["month"], str) or not isinstance(budget["day"], str)
    ):
        raise StateError("invalid_task_budget")


def prune_auth(state: dict[str, Any], now: int) -> None:
    data = workspace(state)
    for name in ("auth_nonces", "auth_sessions"):
        for key, expiry in list(data[name].items()):
            if expiry <= now:
                del data[name][key]


def prune_workspace(state: dict[str, Any], paths: set[str], today: date) -> None:
    if "task_workspace" not in state:
        return
    data = workspace(state)
    for identifier in list(data["active_projects"]):
        if f"projects/{identifier}/README.md" not in paths:
            del data["active_projects"][identifier]
    for path in list(data["standing"]["sources"]):
        if path not in paths:
            del data["standing"]["sources"][path]
    for identifier, record in data["clarifications"].items():
        if record["source_path"] not in paths or record["expires_on"] <= today.isoformat():
            record["changes"] = {}
            record["field"] = None
            record["question_token"] = None
            record["expired"] = True
            if record["source_path"] not in paths:
                record["source_path"] = ""
