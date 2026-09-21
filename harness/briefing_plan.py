"""Source-bound proposals and deterministic presentation for action briefings."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from datetime import date, timedelta
from typing import Any

MAX_TEXT = 700
MAX_PROPOSALS = 1
MAX_MODEL_SOURCES = 24
MAX_MODEL_CHANGES = 2
MAX_MODEL_GOALS = 5
MAX_FOCUS_TEXT = 500
MAX_REASON_TEXT = 500
RETRYABLE_PLAN_ERRORS = frozenset({
    "invalid_text", "invalid_model_plan", "invalid_plan", "unbacked_focus",
    "invalid_changes", "invalid_change", "unbacked_change", "invalid_proposal",
    "unsupported_action", "unbacked_proposal", "not_a_task",
    "task_creation_requires_idea", "research_requires_knowledge_source",
    "task_requires_single_line",
})
_PLAN_ERROR_CODES = RETRYABLE_PLAN_ERRORS | {
    "unsafe_text", "research_requires_non_sensitive_question",
    "briefing_model_not_configured", "briefing_context_limit",
    "briefing_model_refused",
}
_SECRET = re.compile(
    r"(?i)(?:\b(?:password|api[_ -]?key|token|secret)\s*[:=]\s*\S+|"
    r"\bBearer\s+\S+|https?://\S+[?&](?:code|key|token)=\S+|"
    r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]){11,30}\b)"
)
_PRIVATE_RESEARCH = re.compile(
    r"(?i)(?:\b(?:salary|paycheck|balance|account number|medical|diagnosis|"
    r"passport|government id|legal case|tax return|compensation)\b|"
    r"[\w.+-]+@[\w.-]+\.[a-z]{2,}|\b\d{12,}\b)"
)


class PlanError(ValueError):
    """An invalid plan; messages must never contain source content."""

    @property
    def code(self) -> str:
        value = self.args[0] if len(self.args) == 1 else None
        return value if isinstance(value, str) and value in _PLAN_ERROR_CODES else "unclassified_plan_error"


def safe_text(value: object, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise PlanError("invalid_text")
    if _SECRET.search(value):
        raise PlanError("unsafe_text")
    return value.strip()


def fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=True).encode()
    ).hexdigest()


def public_research_question(value: object) -> str:
    text = safe_text(value)
    if _PRIVATE_RESEARCH.search(text):
        raise PlanError("research_requires_non_sensitive_question")
    return text


def task_due(task: dict[str, Any], today: date) -> bool:
    for key, horizon in (("deadline", today + timedelta(days=7)), ("review_on", today)):
        value = task.get(key)
        if value and date.fromisoformat(value) <= horizon:
            return True
    return task.get("focus_on") == today.isoformat()


def source_map(context: dict[str, Any]) -> dict[str, dict[str, Any]]:
    sources = {
        source["path"]: source
        for source in context.get("sources", [])
        if isinstance(source.get("path"), str)
    }
    for task in context.get("tasks", []):
        if not task.get("path") or not task.get("revision"):
            continue
        sources[task["path"]] = {
            **task,
            "kind": "task",
            "digest": fingerprint(
                {key: task.get(key) for key in (
                    "title", "next_action", "review_on", "deadline", "waiting_for",
                )}
            ),
        }
    return sources


def _select_model_sources(
    context: dict[str, Any], sources: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Reserve change/goal evidence before task-first filling; due tasks render separately."""
    changes = [
        sources[item["path"]] for item in context.get("changes", [])
        if item.get("path") in sources
    ][:MAX_MODEL_CHANGES]
    goals = [item for item in sources.values() if item["kind"] == "goal"][:MAX_MODEL_GOALS]
    remaining = sorted(
        sources.values(), key=lambda item: (item["kind"] != "task", item["kind"] != "goal"),
    )
    selected: dict[str, dict[str, Any]] = {}
    for item in [*changes, *goals, *remaining]:
        selected.setdefault(item["path"], item)
        if len(selected) == MAX_MODEL_SOURCES:
            break
    return list(selected.values())


def model_input(context: dict[str, Any], memories: list[dict[str, Any]]) -> dict[str, Any]:
    """Keep operational permissions out of model-controlled data."""
    sources = source_map(context)
    selected = _select_model_sources(context, sources)
    selected_paths = {item["path"] for item in selected}
    changes = [
        item for item in context.get("changes", [])
        if item.get("path") in selected_paths
    ]
    deferred = len(sources) - len(selected)
    warnings = list(context.get("warnings", []))
    if deferred:
        warnings.append(
            f"{deferred} source records were not included in the model evidence packet; "
            "due tasks are still listed independently."
        )
    return {
        "date": context["date"],
        "initial_baseline": context.get("initial_baseline", False),
        "goals": [
            {key: item.get(key, "")[:700] for key in ("path", "title", "text")}
            for item in context.get("goals", [])[:MAX_MODEL_GOALS]
        ],
        "sources": [
            {key: (item.get(key, "")[:700] if isinstance(item.get(key), str) else item.get(key)) for key in (
                "path", "kind", "title", "text", "next_action", "review_on",
                "deadline", "waiting_for",
            )}
            for item in selected
        ],
        "changed_paths": [item["path"] for item in changes],
        "change_kinds": {item["path"]: item.get("change_kind", "newly_available") for item in changes},
        "deferred_source_count": deferred,
        "corrections": [
            {"source_path": item["source_path"], "text": item["text"]}
            for item in memories[:12] if item.get("active", True)
        ],
        "warnings": warnings,
        "decisions": context.get("decisions", [])[:40],
        "personal_signals": (context.get("extras") or {}).get("signals", []),
    }


PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "focus": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "path": {"type": "string"},
                        "text": {"type": "string", "description": f"Nonempty focus, at most {MAX_FOCUS_TEXT} characters."},
                    },
                    "required": ["path", "text"],
                },
            ],
        },
        "changes": {
            "type": "array", "maxItems": 2,
            "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "path": {"type": "string"},
                    "why": {"type": "string", "description": f"Nonempty reason, at most {MAX_REASON_TEXT} characters."},
                },
                "required": ["path", "why"],
            },
        },
        "proposal": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "kind": {"type": "string", "enum": ["review_task", "create_task", "research"]},
                        "source_path": {"type": "string"},
                        "text": {"type": "string", "description": f"Nonempty proposal, at most {MAX_TEXT} characters; create_task must be one line."},
                        "why": {"type": "string", "description": f"Nonempty reason, at most {MAX_REASON_TEXT} characters."},
                    },
                    "required": ["kind", "source_path", "text", "why"],
                },
            ],
        },
    },
    "required": ["focus", "changes", "proposal"],
}

def plan_schema(context: dict[str, Any]) -> dict[str, Any]:
    """Constrain the model to evidence it was actually given, including empty deltas."""
    schema = deepcopy(PLAN_SCHEMA)
    sources = {
        item["path"]: item.get("kind")
        for item in context.get("sources", [])
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }
    focus = schema["properties"]["focus"]
    if sources:
        focus["anyOf"][1]["properties"]["path"]["enum"] = sorted(sources)
    else:
        schema["properties"]["focus"] = {"type": "null"}
    changed = sorted(set(context.get("changed_paths", [])) & sources.keys())
    changes = schema["properties"]["changes"]
    if changed:
        changes["items"]["properties"]["path"]["enum"] = changed
    else:
        changes["maxItems"] = 0
    template = schema["properties"]["proposal"]["anyOf"][1]
    proposals: list[dict[str, Any]] = [{"type": "null"}]
    for kind, allowed in (
        ("review_task", {"task"}),
        ("create_task", {"idea"}),
        ("research", {"idea", "note", "research", "wiki"}),
    ):
        paths = sorted(path for path, source_kind in sources.items() if source_kind in allowed)
        if not paths:
            continue
        proposal = deepcopy(template)
        proposal["properties"]["kind"]["enum"] = [kind]
        proposal["properties"]["source_path"]["enum"] = paths
        proposals.append(proposal)
    schema["properties"]["proposal"] = (
        {"anyOf": proposals} if len(proposals) > 1 else {"type": "null"}
    )
    return schema


def validate_plan(
    raw: dict[str, Any], context: dict[str, Any], today: date,
    *, evidence_paths: set[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != {"focus", "changes", "proposal"}:
        raise PlanError("invalid_plan")
    sources = source_map(context)
    if evidence_paths is None:
        evidence_paths = {item["path"] for item in _select_model_sources(context, sources)}
    sources = {path: item for path, item in sources.items() if path in evidence_paths}
    focus = raw["focus"]
    if focus is None:
        focus = "No evidence-backed focus recommendation today."
    elif (
        not isinstance(focus, dict) or set(focus) != {"path", "text"}
        or not isinstance(focus["path"], str) or focus["path"] not in sources
    ):
        raise PlanError("unbacked_focus")
    else:
        focus = safe_text(focus["text"], MAX_FOCUS_TEXT) + "\nSource: " + (
            sources[focus["path"]].get("url") or focus["path"]
        )
    changes = raw["changes"]
    if not isinstance(changes, list) or len(changes) > 2:
        raise PlanError("invalid_changes")
    changed = {source["path"] for source in context.get("changes", [])}
    change_kinds = {source["path"]: source.get("change_kind") for source in context.get("changes", [])}
    normalized = []
    for change in changes:
        if not isinstance(change, dict) or set(change) != {"path", "why"}:
            raise PlanError("invalid_change")
        if not isinstance(change["path"], str) or change["path"] not in changed or change["path"] not in sources:
            raise PlanError("unbacked_change")
        normalized.append({
            "source": {**sources[change["path"]], "change_kind": change_kinds[change["path"]]},
            "why": safe_text(change["why"], MAX_REASON_TEXT),
        })
    proposal = raw["proposal"]
    if proposal is not None:
        if not isinstance(proposal, dict) or set(proposal) != {"kind", "source_path", "text", "why"}:
            raise PlanError("invalid_proposal")
        kind = proposal["kind"]
        if not isinstance(kind, str) or kind not in {"review_task", "create_task", "research"}:
            raise PlanError("unsupported_action")
        if not isinstance(proposal["source_path"], str):
            raise PlanError("unbacked_proposal")
        source = sources.get(proposal["source_path"])
        if source is None or not source.get("revision") or not source.get("digest"):
            raise PlanError("unbacked_proposal")
        if kind == "review_task" and source["kind"] != "task":
            raise PlanError("not_a_task")
        if kind == "create_task" and source["kind"] != "idea":
            raise PlanError("task_creation_requires_idea")
        if kind == "research" and source["kind"] not in {"idea", "note", "research", "wiki"}:
            raise PlanError("research_requires_knowledge_source")
        text = public_research_question(proposal["text"]) if kind == "research" else safe_text(proposal["text"])
        if kind == "create_task" and any(ord(char) < 32 for char in text):
            raise PlanError("task_requires_single_line")
        why = safe_text(proposal["why"], MAX_REASON_TEXT)
        action = {"kind": kind, "text": text}
        if kind == "review_task":
            action["path"] = source["path"]
        identity = fingerprint([source["path"], source["revision"], source["digest"], action, today.isoformat()])[:24]
        proposal = {
            "id": identity,
            "kind": kind,
            "text": text,
            "why": why,
            "source_path": source["path"],
            "source_revision": source["revision"],
            "source_digest": source["digest"],
            "source_url": source.get("url", ""),
            "status": "pending",
            "created_on": today.isoformat(),
            "expires_on": (today + timedelta(days=14)).isoformat(),
            "action": action,
            "message_ids": [],
        }
    return {"focus": focus, "changes": normalized, "proposal": proposal}


def proposal_allowed(proposal: dict[str, Any], state: dict[str, Any], today: date) -> bool:
    for previous in state["proposals"].values():
        if (
            previous.get("source_path") != proposal["source_path"]
            or previous.get("source_digest") != proposal["source_digest"]
        ):
            continue
        status = previous.get("status")
        if status == "snoozed":
            if date.fromisoformat(previous["review_on"]) > today:
                return False
        elif status not in {"expired", "invalidated", "failed"}:
            return False
    return True


def render_briefing(
    plan: dict[str, Any], context: dict[str, Any], today: date,
) -> tuple[str, list[str]]:
    lines = [f"Morning focus - {today.isoformat()}", "", plan["focus"]]
    delivered_paths = []
    if context.get("results"):
        lines.extend(["", "Verified results from approved work"])
        lines.extend(f"- {item['id']}: canonical result verified. {item['url']}" for item in context["results"])
    for change in plan["changes"]:
        source = change["source"]
        heading = "What changed" if source.get("change_kind") == "modified" else "New to the briefing"
        lines.extend(["", f"{heading}: {source['title']}", change["why"], source.get("url", "")])
        delivered_paths.append(source["path"])
    due = [task for task in context.get("tasks", []) if task_due(task, today)]
    if due:
        lines.extend(["", "Due reviews / approaching deadlines"])
        for task in due:
            timing = ", ".join(
                f"{key.replace('_', ' ')} {task[key]}"
                for key in ("review_on", "deadline", "waiting_for") if task.get(key)
            )
            lines.append(f"- {task['title']}: {timing}")
    for warning in context.get("warnings", []):
        lines.extend(["", f"Source notice: {warning}"])
    extras = context.get("extras") or {}
    if extras.get("weather"):
        lines.extend(["", str(extras["weather"])])
    if extras.get("signals"):
        lines.extend(["", "Personal context", *extras["signals"]])
    if not context.get("goals") and any(
        section in context.get("sections", []) for section in ("goals", "focus", "week")
    ):
        lines.extend(["", "No confirmed goals were available from the canonical source."])
    return "\n".join(line for line in lines if line is not None), delivered_paths


def render_proposal(proposal: dict[str, Any]) -> str:
    operation = {
        "research": "Public research: one question, at most 5 primary sources, one short report; no further jobs.",
        "create_task": "Create one draft task through the existing reviewable vault workflow.",
        "review_task": "Select this next action; the existing task stays open until you report completion.",
        "update_task": "Propose the displayed task edit through the existing reviewable vault workflow.",
    }[proposal["kind"]]
    return (
        f"Proposal {proposal['id']}\n\n{proposal['text']}\n\n"
        f"Why now: {proposal['why']}\n\n{operation}\n"
        "Research consumes existing agent capacity; no work starts before approval.\n\n"
        f"Source: {proposal.get('source_url') or proposal['source_path']}\n\n"
        "Approve, decline, ask why, or reply with a correction. "
        "To defer, reply 'snooze YYYY-MM-DD'. Task completion: reply 'done'."
    )
