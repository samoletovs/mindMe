"""Concise, source-revision-bound continuity in the existing private CAS store."""

from __future__ import annotations

import re
from datetime import date, timedelta
from difflib import SequenceMatcher
from typing import Any

from briefing_plan import fingerprint
from briefing_sources import _SENSITIVE_CONTENT, _kind
from briefing_state import StateError, _SECRET, _tombstone

CAPS = {"memories": 100, "bindings": 200, "requests": 300, "topics": 30}


def empty_knowledge() -> dict[str, Any]:
    return {key: {} for key in CAPS}


def _refs(value: object) -> bool:
    return (
        isinstance(value, dict) and 1 <= len(value) <= 5
        and all(_kind(path) is not None and isinstance(sha, str) and re.fullmatch(r"[a-f0-9]{40}", sha)
                for path, sha in value.items())
    )


def validate_knowledge(value: object) -> None:
    if not isinstance(value, dict) or set(value) != set(CAPS):
        raise StateError("invalid_knowledge_state")
    for key, cap in CAPS.items():
        if not isinstance(value[key], dict) or len(value[key]) > cap:
            raise StateError("knowledge_capacity")
        for identifier, item in value[key].items():
            if not isinstance(identifier, str) or not isinstance(item, dict):
                raise StateError("invalid_knowledge_record")
            if key == "requests":
                if item.get("status") not in {"claimed", "sent", "uncertain"}:
                    raise StateError("invalid_knowledge_request")
            elif not (key == "bindings" and item.get("invalidated") is True and item.get("sources") == {}) and not _refs(item.get("sources")):
                raise StateError("invalid_knowledge_sources")
            for field in ("created_on", "expires_on"):
                if field not in item and field == "expires_on" and key == "memories" and item.get("kind") == "correction":
                    continue
                try:
                    date.fromisoformat(item[field])
                except (KeyError, TypeError, ValueError):
                    raise StateError("invalid_knowledge_date") from None
            if key == "memories":
                if (
                    item.get("kind") not in {"working", "feedback", "correction"}
                    or not isinstance(item.get("text"), str) or not 1 <= len(item["text"]) <= 280
                    or _SECRET.search(item["text"]) or _SENSITIVE_CONTENT.search(item["text"])
                    or type(item.get("use_count")) is not int or item["use_count"] < 0
                    or type(item.get("active")) is not bool
                ):
                    raise StateError("invalid_knowledge_memory")
            if key == "bindings" and not re.fullmatch(r"[1-9]\d*", identifier):
                raise StateError("invalid_knowledge_binding")
            if key == "topics" and (
                not isinstance(item.get("text"), str) or len(item["text"]) > 16000
            ):
                raise StateError("invalid_topic_receipt")


def decide_write(
    state: dict[str, Any], *, kind: str, text: str, sources: dict[str, str], today: date,
    memory_ids: list[str] | None = None,
) -> str | None:
    """Distil, never log. Explicit corrections supersede scoped assumptions."""
    if (
        kind not in {"working", "feedback", "correction"} or not isinstance(text, str)
        or not 1 <= len(text.strip()) <= 280 or "\n" in text or "\r" in text
        or _SECRET.search(text) or _SENSITIVE_CONTENT.search(text) or not _refs(sources)
    ):
        raise StateError("unsafe_knowledge_memory")
    text = text.strip()
    memories = state["knowledge"]["memories"]
    if memory_ids is not None and (
        not isinstance(memory_ids, list) or len(memory_ids) > 6
        or any(not isinstance(identifier, str) or identifier not in memories for identifier in memory_ids)
    ):
        raise StateError("invalid_memory_dependencies")
    for identifier, item in memories.items():
        if item["active"] and item["sources"] == sources and item["kind"] == kind:
            if SequenceMatcher(None, item["text"].casefold(), text.casefold()).ratio() >= .9:
                if memory_ids:
                    item["memory_ids"] = sorted(set(item.get("memory_ids", [])) | (set(memory_ids) - {identifier}))
                return identifier
    if len(memories) >= CAPS["memories"]:
        raise StateError("knowledge_memory_capacity")
    identifier = fingerprint([kind, text, sources, today.isoformat()])[:24]
    record: dict[str, Any] = {
        "kind": kind, "text": text, "sources": sources, "created_on": today.isoformat(),
        "last_used_on": None, "use_count": 0, "active": True,
        "memory_ids": list(memory_ids or []),
    }
    if kind != "correction":
        record["expires_on"] = (today + timedelta(days=14 if kind == "working" else 90)).isoformat()
    predecessors = []
    for previous_id, item in memories.items():
        if item["active"] and item["sources"] == sources and (
            kind == "correction" or item["kind"] == kind == "feedback"
        ):
            item["active"] = False
            item["superseded_on"] = today.isoformat()
            predecessors.append(previous_id)
    if predecessors:
        record["supersedes"] = predecessors
    memories[identifier] = record
    return identifier


def recall(
    state: dict[str, Any], *, query: str, sources: dict[str, str], today: date,
) -> list[dict[str, Any]]:
    terms = set(re.findall(r"\w{3,}", query.casefold()))
    candidates = []
    for identifier, item in state["knowledge"]["memories"].items():
        if (
            not item["active"] or item.get("expires_on", "9999-12-31") <= today.isoformat()
            or any(sources.get(path) != sha for path, sha in item["sources"].items())
        ):
            continue
        relevance = len(terms & set(re.findall(r"\w{3,}", item["text"].casefold())))
        candidates.append((item["kind"] == "correction", relevance, item["created_on"], identifier, item))
    return [
        {"id": identifier, "kind": item["kind"], "text": item["text"]}
        for _, _, _, identifier, item in sorted(candidates, reverse=True)[:6]
    ]


def mark_used(state: dict[str, Any], identifiers: list[str], today: date) -> None:
    for identifier in set(identifiers):
        item = state["knowledge"]["memories"].get(identifier)
        if item and item["active"]:
            item["use_count"] += 1
            item["last_used_on"] = today.isoformat()


def forget_memory(state: dict[str, Any], identifier: str) -> None:
    """Deleting a correction/feedback also removes operational derivatives of it."""
    removed = {identifier}
    memories = state["knowledge"]["memories"]
    while True:
        dependents = {
            key for key, item in memories.items() if removed.intersection(item.get("memory_ids", []))
        }
        if dependents <= removed:
            break
        removed.update(dependents)
    for key in removed:
        memories.pop(key, None)
    for key, topic in list(state["knowledge"]["topics"].items()):
        if removed.intersection(topic.get("memory_ids", [])):
            del state["knowledge"]["topics"][key]


def consolidate(
    state: dict[str, Any], *, revisions: dict[str, str | None], today: date,
) -> None:
    """Only confirmed deletions/changes and expired cache data are removed; receipts remain."""
    knowledge = state["knowledge"]
    for identifier, proposal in list(state["proposals"].items()):
        if any(path in revisions and revisions[path] != sha
               for path, sha in proposal.get("knowledge_sources", {}).items()):
            state["proposals"][identifier] = _tombstone(proposal, today)
    for key in ("memories", "bindings", "topics"):
        for identifier, item in list(knowledge[key].items()):
            if (
                item.get("expires_on", "9999-12-31") <= today.isoformat()
                or any(path in revisions and revisions[path] != sha for path, sha in item["sources"].items())
                or (key == "memories" and not item["active"]
                    and item.get("superseded_on", "9999-12-31") <= (today - timedelta(days=35)).isoformat())
            ):
                if key == "bindings":
                    item["sources"] = {}
                    item["invalidated"] = True
                else:
                    if key == "memories":
                        forget_memory(state, identifier)
                    else:
                        knowledge[key].pop(identifier, None)
    for identifier, item in list(knowledge["requests"].items()):
        # An uncertain send must never be replayed just because time elapsed.
        if item["status"] == "sent" and item["expires_on"] <= today.isoformat():
            del knowledge["requests"][identifier]
