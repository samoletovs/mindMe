"""One owner, one canonical task tree, and the existing approval ledger."""

from __future__ import annotations

import copy
import json
import re
import secrets
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from briefing_loop import BriefingLoop
from briefing_plan import fingerprint, safe_text
from briefing_sources import SourceError, _SENSITIVE_CONTENT
from briefing_state import StateError, _safe_receipt, record_transition
from execution_budget import checkpoint, execution_budget
from knowledge_plan import public_knowledge_question
from task_sources import ATTENTION_DEADLINE_HORIZON_DAYS, TaskRepository, definition_gaps, task_path
from task_state import AREAS, HEX24, HEX32, PROJECT, SHA, STAGES, workspace
from telegram_format import escape, escaped_chunks, pack_html_blocks

DEFINITION_LIMITS = {"title": 120, "outcome": 2000, "next_action": 500, "done_when": 2000}
EDIT_FIELDS = {*DEFINITION_LIMITS, "area", "project", "stage", "execution", "review_on", "waiting_for"}
QUESTIONS = {
    "outcome": "What should be different when this task is finished?",
    "next_action": "What is the one concrete next action?",
    "done_when": "What observable result will you check before calling it done?",
    "area": "Which area owns this task? Use one area ID: " + ", ".join(AREAS) + ".",
    "execution": "Who will do the next action: human, assisted, or agent? This does not authorize a run.",
}
PREPARATION_LIMITS = {
    "model_calls": 1, "input_chars": 9000, "output_tokens": 1200,
    "seconds": 45, "source_reads": 4, "external_tools": 0,
}
PREPARATION_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "steps": {"type": "array", "items": {"type": "string"}},
        "uncertainties": {"type": "array", "items": {"type": "string"}},
        "owner_next_action": {"type": "string"},
        "source_quote": {"type": "string"},
    },
    "required": ["summary", "steps", "uncertainties", "owner_next_action", "source_quote"],
}


class TaskError(RuntimeError):
    """Only stable error codes are safe for the host or browser."""


def task_timezone(env: Mapping[str, str]) -> ZoneInfo:
    value = env.get("MINDME_TASKS_TIMEZONE", "").strip()
    if not value:
        raise TaskError("task_timezone_not_configured")
    if len(value) > 100 or not re.fullmatch(r"[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*", value):
        raise TaskError("task_timezone_invalid")
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise TaskError("task_timezone_invalid") from None


def _text(value: object, limit: int, *, paragraph: bool = True) -> str:
    if isinstance(value, str) and any(ord(char) < 32 and char != "\n" for char in value):
        raise TaskError("task_text_not_permitted")
    result = safe_text(value, limit)
    if (
        _SENSITIVE_CONTENT.search(result)
        or any(ord(char) < 32 and char != "\n" for char in result)
        or (paragraph and (
            "\n\n" in result or any(
                re.match(r"^\s*(?:[-*+] |\d+[.)] |#{1,6} |```|>|\*\*[^:]+:\*\*)", line)
                for line in result.splitlines()
            )
        ))
    ):
        raise TaskError("task_text_not_permitted")
    return result


def _date(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise TaskError("task_date_invalid")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise TaskError("task_date_invalid") from None
    return value


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not HEX32.fullmatch(value):
        raise TaskError("task_request_id_invalid")
    return value


def validate_changes(value: object, *, create: bool = False) -> dict[str, Any]:
    extra = {"deadline", "focus_on", "context", "subtasks", "clarification_question", "benefit", "tags"} if create else set()
    if not isinstance(value, dict) or not value or not value.keys() <= EDIT_FIELDS | extra:
        raise TaskError("task_changes_invalid")
    result = {}
    for field, content in value.items():
        if content is None and not create and field in {"review_on", "waiting_for"}:
            result[field] = None
        elif field in DEFINITION_LIMITS:
            result[field] = _text(content, DEFINITION_LIMITS[field])
            if field == "title" and "\n" in result[field]:
                raise TaskError("task_title_invalid")
        elif field in {"area", "execution", "stage"}:
            choices = {
                "area": AREAS, "execution": ("human", "assisted", "agent"),
                "stage": ("clarify", "backlog") if create else STAGES,
            }[field]
            if not isinstance(content, str) or content not in choices:
                raise TaskError("task_changes_invalid")
            result[field] = content
        elif field == "project":
            if not isinstance(content, str) or not PROJECT.fullmatch(content):
                raise TaskError("task_project_invalid")
            result[field] = content
        elif field in {"review_on", "deadline", "focus_on"}:
            result[field] = _date(content)
        elif field == "subtasks":
            if not isinstance(content, list) or len(content) > 6:
                raise TaskError("task_subtasks_invalid")
            result[field] = [_text(item, 300) for item in content]
        elif field == "tags":
            if not isinstance(content, list) or len(content) > 12 or any(
                not isinstance(item, str) or not 1 <= len(item) <= 40
                or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", item) for item in content
            ):
                raise TaskError("task_tags_invalid")
            result[field] = content
        else:
            result[field] = _text(content, 2000 if field == "context" else 300)
    return result


def _receipt_fields(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(record[key]) for key in (
            "id", "kind", "text", "source_path", "source_revision", "source_url", "status",
            "action", "created_on", "expires_on", "activity", "result", "approved_on",
            "approval_digest", "reviewed", "acknowledged_unavailable", "publish_result",
            "task_workspace",
        ) if key in record
    }


class TaskService:
    def __init__(
        self, *, loop: BriefingLoop, repository: TaskRepository,
        generate: Callable[[dict[str, Any]], dict[str, Any]],
        attention_timezone: ZoneInfo,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        publish: Callable[[dict[str, Any], str], dict[str, Any]] | None = None,
        enabled: Callable[[], bool] = lambda: True,
        daily_limit: int = 3, monthly_limit: int = 40, review_capacity: int = 3,
    ) -> None:
        if (
            type(daily_limit) is not int or not 1 <= daily_limit <= 5
            or type(monthly_limit) is not int or not 1 <= monthly_limit <= 60
            or type(review_capacity) is not int or not 1 <= review_capacity <= 10
        ):
            raise TaskError("task_limits_invalid")
        self.loop, self.store, self.repository = loop, loop.store, repository
        if not isinstance(attention_timezone, ZoneInfo):
            raise TaskError("task_timezone_invalid")
        self.attention_timezone, self.clock = attention_timezone, clock
        self.generate = generate
        self.publish_action = publish
        self.enabled = enabled
        self.daily_limit, self.monthly_limit, self.review_capacity = daily_limit, monthly_limit, review_capacity
        original = loop.execute

        def execute(record: dict[str, Any], reconcile: bool) -> dict[str, Any]:
            if record.get("task_workspace"):
                self._require_enabled()
                saved = self.store.read()["proposals"].get(record["id"])
                if (
                    not saved or not saved.get("approved_on") or not saved.get("action_id")
                    or saved.get("action_id") != record.get("action_id")
                    or saved.get("approval_digest") != record.get("approval_digest")
                    or saved.get("action") != record.get("action")
                    or saved.get("status") not in {"executing", "submitted", "uncertain", "completed"}
                ):
                    raise TaskError("task_execution_requires_claim")
            if record["action"]["kind"] != "prepare_task":
                return original(record, reconcile)
            if reconcile:
                return record.get("result") or {"status": "unknown", "error": "preparation_unconfirmed"}
            try:
                with execution_budget(45):
                    source = self._source(record["source_path"], record["source_revision"])
                    self._standing_recheck(record)
                    raw = self.generate({
                        "source": {key: source[key] for key in ("path", "revision", "text")},
                        "scope": record["action"]["scope"], "limits": PREPARATION_LIMITS,
                    })
                    checkpoint()
                    result = self._preparation_result(raw, source)
                    self._source(record["source_path"], record["source_revision"])
                    self._standing_recheck(record)
            except TaskError:
                return {"status": "failed", "error": "task_preparation_rejected"}
            return {
                "status": "prepared", "preparation": result, "source_revision": source["revision"],
                "limits": PREPARATION_LIMITS, "tools": ["existing_model"],
                "sinks": ["private_proposal_receipt"], "verification": "draft_not_verified",
            }

        loop.execute = execute
        loop.before_claim = self._before_claim

    def _require_enabled(self) -> None:
        if not self.enabled():
            raise TaskError("tasks_not_configured")

    def owner_date(self) -> date:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise TaskError("task_clock_invalid")
        return now.astimezone(self.attention_timezone).date()

    def _standing_recheck(self, record: dict[str, Any]) -> None:
        if not record.get("standing_authorization"):
            return
        data = workspace(self.store.read())
        if (
            not data["standing"]["enabled"]
            or fingerprint([data["standing"], data["active_projects"]]) != record["standing_authorization"]
        ):
            raise TaskError("task_standing_scope_changed")
        project = record["standing_project"]
        self._source(project["path"], project["revision"])

    def _source(self, path: object, revision: object | None = None) -> dict[str, Any]:
        if not isinstance(path, str):
            raise TaskError("task_path_invalid")
        source = self.repository.read(path)
        if source is None:
            raise TaskError("task_source_not_canonical")
        if revision is not None and (not isinstance(revision, str) or source["revision"] != revision):
            raise TaskError("task_source_changed")
        return source

    def _project(self, changes: dict[str, Any]) -> None:
        if changes.get("project"):
            self._source(f"projects/{changes['project']}/README.md")

    def _save(
        self, source: dict[str, Any], action: dict[str, Any], kind: str, request_id: str, today: date,
    ) -> dict[str, Any]:
        request_id = _identifier(request_id)
        request_key = fingerprint(["task-request", request_id])
        digest = fingerprint([source["path"], source["revision"], action, kind])
        identifier = fingerprint([digest, request_id if kind == "capture_task" else "source-bound"])[:24]
        record = {
            "id": identifier, "kind": kind, "text": (
                "Prepare one source-bound draft for review." if kind == "prepare_task"
                else "Research the approved public question." if kind == "research"
                else "Save the exact task changes shown below."
            ),
            "why": "An explicit owner request; no other task or commitment changes.",
            "source_path": source["path"], "source_revision": source["revision"],
            "source_digest": source["digest"], "source_url": source["url"],
            "status": "pending", "created_on": today.isoformat(),
            "expires_on": (today + timedelta(days=14)).isoformat(), "message_ids": [],
            "action": action, "task_workspace": True, "approval_digest": digest,
        }
        if len(json.dumps(action, ensure_ascii=False).encode("utf-8")) > 6800:
            raise TaskError("task_action_too_large")

        def save(state: dict[str, Any]) -> dict[str, Any]:
            data = workspace(state)
            previous = data["requests"].get(request_key)
            if previous:
                if previous["digest"] != digest:
                    raise TaskError("task_request_conflict")
                return copy.deepcopy(state["proposals"][previous["proposal_id"]])
            data["requests"][request_key] = {
                "digest": digest, "status": "recorded", "proposal_id": identifier,
                "created_on": today.isoformat(),
            }
            state["proposals"].setdefault(identifier, record)
            return copy.deepcopy(state["proposals"][identifier])

        return self._project_receipt(self.store.update(save), today)

    def capture(self, payload: dict[str, Any], today: date) -> dict[str, Any]:
        if set(payload) != {"request_id", "text", "definition"}:
            raise TaskError("task_request_invalid")
        _text(payload["text"], 2000, paragraph=False)
        text = payload["text"]
        definition = validate_changes(payload["definition"], create=True)
        if "title" not in definition:
            raise TaskError("task_title_missing")
        definition.setdefault("stage", "clarify")
        if definition["stage"] == "backlog" and (definition_gaps(definition) or definition.get("clarification_question")):
            raise TaskError("task_definition_incomplete")
        self._project(definition)
        return self._save(
            self._source("tasks/README.md"), {"kind": "create_task", "text": text, "definition": definition},
            "capture_task", payload["request_id"], today,
        )

    def refine(self, payload: dict[str, Any], today: date) -> dict[str, Any]:
        if set(payload) != {"request_id", "path", "revision", "changes"}:
            raise TaskError("task_request_invalid")
        if not task_path(payload["path"]):
            raise TaskError("task_path_invalid")
        source = self._source(payload["path"], payload["revision"])
        changes = validate_changes(payload["changes"])
        if {"waiting_for", "review_on"} & changes.keys() and "stage" not in changes:
            raise TaskError("task_waiting_transition_requires_stage")
        merged = {**source, **changes}
        if merged["stage"] not in {"clarify", "untriaged"} and definition_gaps(merged):
            raise TaskError("task_definition_incomplete")
        self._project(changes)
        return self._save(
            source, {"kind": "refine_task", "path": source["path"], "changes": changes},
            "refine_task", payload["request_id"], today,
        )

    def prepare(self, payload: dict[str, Any], today: date) -> dict[str, Any]:
        if set(payload) != {"request_id", "path", "revision", "scope", "kind"}:
            raise TaskError("task_request_invalid")
        source = self._source(payload["path"], payload["revision"])
        if not task_path(source["path"]) or source["stage"] not in {"ready", "doing", "verify"} or definition_gaps(source):
            raise TaskError("task_not_selected")
        kind = payload["kind"]
        if kind == "research":
            question = public_knowledge_question(payload["scope"])
            action = {
                "kind": "research", "text": question,
                "tools": ["public_research"], "sinks": ["reviewed_research_report"],
                "limits": {"sources": 5, "reports": 1, "words": 1200, "follow_on_jobs": 0},
            }
        elif kind == "prepare_task":
            action = {
                "kind": kind, "path": source["path"], "scope": _text(payload["scope"], 500),
                "tools": ["existing_model"], "sinks": ["private_proposal_receipt"],
                "limits": PREPARATION_LIMITS,
            }
        else:
            raise TaskError("task_preparation_kind_invalid")
        return self._save(source, action, kind, payload["request_id"], today)

    def change(self, payload: dict[str, Any], today: date) -> dict[str, Any]:
        if (
            set(payload) != {"request_id", "path", "revision", "change"}
            or not isinstance(payload["change"], dict) or len(payload["change"]) != 1
        ):
            raise TaskError("task_request_invalid")
        source = self._source(payload["path"], payload["revision"])
        if not task_path(source["path"]):
            raise TaskError("task_path_invalid")
        field, value = next(iter(payload["change"].items()))
        if field in {"deadline", "focus_on", "review_on"}:
            change = {field: _date(value) if value is not None else None}
        else:
            change = validate_changes({field: value})
        combined = {**source, **change}
        if combined["stage"] not in {"clarify", "untriaged"} and definition_gaps(combined):
            raise TaskError("task_definition_incomplete")
        return self._save(
            source, {"kind": "update_task", "path": source["path"], "change": change},
            "edit_task", payload["request_id"], today,
        )

    def close(self, payload: dict[str, Any], today: date) -> dict[str, Any]:
        if (
            set(payload) != {"request_id", "path", "revision", "completion", "checked_done_when"}
            or payload["checked_done_when"] is not True
        ):
            raise TaskError("task_closure_requires_verification")
        source = self._source(payload["path"], payload["revision"])
        if not task_path(source["path"]) or not source["done_when"]:
            raise TaskError("task_closure_requires_done_condition")
        completion = payload["completion"]
        if (
            not isinstance(completion, dict)
            or not {"result", "evidence", "verification", "verified_on"} <= completion.keys()
            or not completion.keys() <= {"result", "evidence", "verification", "verified_on", "learning"}
            or completion["verification"] != "owner"
        ):
            raise TaskError("task_closure_evidence_invalid")
        result = {
            "result": _text(completion["result"], 1000),
            "evidence": _text(completion["evidence"], 1000),
            "verification": "owner", "verified_on": _date(completion["verified_on"]),
        }
        if result["verified_on"] > self.owner_date().isoformat():
            raise TaskError("task_closure_date_invalid")
        if completion.get("learning"):
            result["learning"] = _text(completion["learning"], 1000)
        return self._save(source, {
            "kind": "update_task", "path": source["path"], "change": {"status": "done"}, "completion": result,
        }, "close_task", payload["request_id"], today)

    def _before_claim(self, state: dict[str, Any], proposal: dict[str, Any], today: date) -> None:
        if not proposal.get("task_workspace"):
            return
        self._require_enabled()
        if fingerprint([proposal["source_path"], proposal["source_revision"], proposal["action"], proposal["kind"]]) != proposal["approval_digest"]:
            raise TaskError("task_approval_changed")
        standing = proposal.get("standing_authorization")
        if standing:
            data = workspace(state)
            if (
                not data["standing"]["enabled"]
                or standing != fingerprint([data["standing"], data["active_projects"]])
                or data["standing"]["sources"].get(proposal["source_path"]) != proposal["source_revision"]
            ):
                raise TaskError("task_standing_scope_changed")
            if any(
                item.get("standing_authorization") and item.get("approved_on") == today.isoformat()
                for item in state["proposals"].values()
            ):
                raise TaskError("task_budget_exhausted")
        if proposal["action"]["kind"] not in {"prepare_task", "research"}:
            return
        if proposal["action"]["kind"] == "prepare_task" and proposal.get("action_id"):
            raise TaskError("task_preparation_already_attempted")
        unreviewed = sum(
            1 for item in state["proposals"].values()
            if item.get("task_workspace") and item.get("kind") in {"prepare_task", "research"}
            and not item.get("reviewed") and item.get("status") in {"completed", "submitted", "executing", "uncertain"}
        )
        if unreviewed >= self.review_capacity:
            raise TaskError("task_review_capacity")
        data = workspace(state)
        budget = data["budget"]
        month, day = today.isoformat()[:7], today.isoformat()
        if budget["month"] != month:
            budget.update(month=month, used=0)
        if budget["day"] != day:
            budget.update(day=day, daily_used=0)
        if budget["used"] >= self.monthly_limit or budget["daily_used"] >= self.daily_limit:
            raise TaskError("task_budget_exhausted")
        budget["used"] += 1
        budget["daily_used"] += 1

    @staticmethod
    def _preparation_result(raw: object, source: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, dict) or set(raw) != set(PREPARATION_SCHEMA["required"]):
            raise TaskError("task_preparation_invalid")
        result = {}
        for key, limit in (("summary", 700), ("owner_next_action", 500), ("source_quote", 500)):
            result[key] = _text(raw[key], limit, paragraph=key != "source_quote")
        for key, count in (("steps", 5), ("uncertainties", 3)):
            if not isinstance(raw[key], list) or len(raw[key]) > count:
                raise TaskError("task_preparation_invalid")
            result[key] = [_text(item, 400) for item in raw[key]]
        if result["source_quote"] not in source["text"]:
            raise TaskError("task_preparation_evidence_invalid")
        return result

    def decide(self, payload: dict[str, Any], today: date) -> dict[str, Any]:
        self._require_enabled()
        if set(payload) != {"proposal_id", "approval_digest", "decision"}:
            raise TaskError("task_request_invalid")
        identifier = payload["proposal_id"]
        if not isinstance(identifier, str) or not HEX24.fullmatch(identifier):
            raise TaskError("task_proposal_invalid")
        record = self.store.read()["proposals"].get(identifier)
        if (
            not record or not record.get("task_workspace")
            or not isinstance(payload["approval_digest"], str)
            or payload["approval_digest"] != record.get("approval_digest")
            or not isinstance(payload["decision"], str) or payload["decision"] not in {"approve", "decline"}
        ):
            raise TaskError("task_approval_invalid")
        self.loop.reply(identifier, payload["decision"], today)
        saved = self.store.read()["proposals"][identifier]
        if payload["decision"] == "approve" and saved["status"] == "submitted" and saved["kind"] != "research":
            return self.publish(identifier, today)
        return self._project_receipt(saved, today)

    def publish(self, identifier: str, today: date) -> dict[str, Any]:
        self._require_enabled()
        if not isinstance(identifier, str) or not HEX24.fullmatch(identifier) or self.publish_action is None:
            raise TaskError("task_publication_unavailable")
        existing = self.store.read()["proposals"].get(identifier)
        if existing and existing.get("task_workspace") and (
            existing.get("status") == "completed" or existing.get("invalidation_reason") == "source_removed"
        ):
            return self._project_receipt(existing, today)

        def claim(state: dict[str, Any]) -> dict[str, Any]:
            record = state["proposals"].get(identifier)
            if (
                not record or not record.get("task_workspace") or not record.get("approved_on")
                or not record.get("action_id") or not record.get("source_path")
                or record["action"]["kind"] not in {"create_task", "update_task", "refine_task"}
                or record["status"] not in {"submitted", "uncertain", "executing", "completed"}
            ):
                raise TaskError("task_publication_not_authorized")
            record.setdefault("publication_action_id", fingerprint(["publish-task", record["action_id"]])[:32])
            return copy.deepcopy(record)

        record = self.store.update(claim)
        if record["status"] == "completed":
            return self._project_receipt(record, today)
        result = self.publish_action(record, record["publication_action_id"])
        if result.get("status") not in {"submitted", "merged", "conflict", "failed", "in_progress", "unknown"}:
            raise TaskError("task_publication_invalid")

        def save(state: dict[str, Any]) -> dict[str, Any]:
            current = state["proposals"][identifier]
            if current["status"] == "completed":
                return copy.deepcopy(current)
            current["publish_result"] = result
            if result["status"] == "merged":
                current["result"] = {
                    **(current.get("result") or {}), **result, "action_id": current["action_id"],
                }
            record_transition(current, {
                "submitted": "submitted", "merged": "completed", "conflict": "invalidated",
                "failed": "failed", "in_progress": "uncertain", "unknown": "uncertain",
            }[result["status"]], today)
            return copy.deepcopy(current)

        return self._project_receipt(self.store.update(save), today)

    def reconcile(self, identifier: str, today: date) -> dict[str, Any]:
        self._require_enabled()
        if not isinstance(identifier, str) or not HEX24.fullmatch(identifier):
            raise TaskError("task_proposal_invalid")
        record = self.store.read()["proposals"].get(identifier)
        if not record or not record.get("task_workspace"):
            raise TaskError("task_proposal_invalid")
        if record.get("status") == "completed" or record.get("invalidation_reason") == "source_removed":
            return self._project_receipt(record, today)
        if record.get("kind") != "research" and record.get("action_id") and record["kind"] != "prepare_task":
            return self.publish(identifier, today)
        self.loop.reconcile(today, proposal_id=identifier)
        return self._project_receipt(self.store.read()["proposals"][identifier], today)

    def overview(self, offset: int, today: date) -> dict[str, Any]:
        calendar_date = self.owner_date()
        result = self.repository.page(offset, today=calendar_date)
        result.setdefault("attention", {}).update(
            calendar_date=calendar_date.isoformat(), timezone=self.attention_timezone.key,
            deadline_horizon_days=ATTENTION_DEADLINE_HORIZON_DAYS,
        )
        state = self.store.read()
        data = workspace(state)
        history = self.history(0, today)
        result.update(
            date=today.isoformat(),
            active_projects=data["active_projects"], standing=data["standing"],
            limits={"daily": self.daily_limit, "monthly": self.monthly_limit, "review_capacity": self.review_capacity},
            budget=data["budget"],
            history=history["items"], history_next_offset=history["next_offset"],
            warnings=[
                *result.get("warnings", []),
                "Only permitted canonical tasks are shown. Up to 96 task definitions are assessed before each 12-task page.",
                "Area and project inventories are not active commitments. Definition checks are structural, not semantic approval.",
            ],
        )
        return result

    def _project_receipt(
        self, record: dict[str, Any], today: date, *,
        checked: dict[str, dict[str, Any] | None] | None = None, unavailable: set[str] | None = None,
    ) -> dict[str, Any]:
        checked = {} if checked is None else checked
        unavailable = set() if unavailable is None else unavailable
        path, revision = record.get("source_path"), record.get("source_revision")
        result = record.get("result") or {}
        tombstone = record.get("invalidation_reason") == "source_removed"
        if not tombstone and record.get("kind") in {"capture_task", "refine_task", "edit_task", "close_task"} and result.get("status") == "merged":
            path, revision = result.get("path"), result.get("source_revision")
        if not tombstone and path and path not in checked:
            try:
                checked[path] = self.repository.read_receipt_source(path)
            except SourceError:
                checked[path] = None
                unavailable.add(path)
        current = checked.get(path) if path and not tombstone else None
        if current is not None and revision == current["revision"]:
            return {**_receipt_fields(record), "source_status": "current"}
        item = {
            "id": record["id"], "kind": record["kind"], "status": record["status"],
            "source_status": "unavailable" if path in unavailable else "changed_or_removed",
            "text": "Source evidence is no longer current or available. Open a fresh task before acting.",
            "result": _safe_receipt(result),
            "reviewed": record.get("reviewed") is True,
            "acknowledged_unavailable": record.get("acknowledged_unavailable") is True,
        }
        invalidatable = record["status"] in {"pending", "accepted", "snoozed"}
        if not tombstone and invalidatable and path not in unavailable:
            def invalidate(state: dict[str, Any]) -> str:
                saved = state["proposals"][record["id"]]
                if saved.get("invalidation_reason") != "source_removed" and (
                    saved["status"] in {"pending", "accepted", "snoozed"}
                ):
                    record_transition(saved, "invalidated", today)
                return saved["status"]

            item["status"] = self.store.update(invalidate)
        return item

    def history(self, offset: int, today: date) -> dict[str, Any]:
        if type(offset) is not int or not 0 <= offset <= 200 or offset % 10:
            raise TaskError("task_history_page_invalid")
        records = list(reversed([
            item for item in self.store.read()["proposals"].values() if item.get("task_workspace")
        ]))
        checked: dict[str, dict[str, Any] | None] = {}
        unavailable: set[str] = set()
        items = [
            self._project_receipt(record, today, checked=checked, unavailable=unavailable)
            for record in records[offset:offset + 10]
        ]
        return {"items": items, "next_offset": offset + 10 if offset + 10 < len(records) else None}

    def select_projects(self, payload: dict[str, Any]) -> dict[str, Any]:
        if set(payload) != {"projects"} or not isinstance(payload["projects"], list) or len(payload["projects"]) > 8:
            raise TaskError("task_selection_invalid")
        selected = {}
        for identifier in payload["projects"]:
            if not isinstance(identifier, str) or not PROJECT.fullmatch(identifier):
                raise TaskError("task_selection_invalid")
            selected[identifier] = self._source(f"projects/{identifier}/README.md")["revision"]
        self.store.update(lambda state: workspace(state).update(
            active_projects=selected, standing={"enabled": False, "sources": {}},
        ))
        return {"active_projects": selected, "standing_enabled": False}

    def standing(self, payload: dict[str, Any]) -> dict[str, Any]:
        if (
            set(payload) != {"enabled", "sources"} or type(payload["enabled"]) is not bool
            or not isinstance(payload["sources"], dict) or len(payload["sources"]) > 3
        ):
            raise TaskError("task_standing_invalid")
        selected = workspace(self.store.read())["active_projects"]
        for path, revision in payload["sources"].items():
            task = self._source(path, revision)
            project = task.get("project")
            if (
                not task_path(path) or task["stage"] != "ready" or definition_gaps(task)
                or project not in selected
                or self._source(f"projects/{project}/README.md")["revision"] != selected[project]
            ):
                raise TaskError("task_standing_scope_invalid")
        if payload["enabled"] and not payload["sources"]:
            raise TaskError("task_standing_scope_invalid")
        self.store.update(lambda state: workspace(state).update(standing=copy.deepcopy(payload)))
        return payload

    def proactive(self, today: date) -> bool:
        if not self.enabled():
            return False
        data = workspace(self.store.read())
        if not data["standing"]["enabled"]:
            return False
        for path, revision in data["standing"]["sources"].items():
            task = self.repository.read(path)
            if task is None or task["revision"] != revision or task["stage"] != "ready" or definition_gaps(task):
                continue
            project = task.get("project")
            if project not in data["active_projects"]:
                continue
            current = self.repository.read(f"projects/{project}/README.md")
            if current is None or current["revision"] != data["active_projects"][project]:
                continue
            scope = "Prepare a short offline plan for the recorded next action. Do not change scope or do the task."
            digest = fingerprint(["standing-preparation", path, revision])
            if any(
                item.get("task_workspace") and item.get("source_path") == path
                and item.get("source_revision") == revision and item.get("kind") == "prepare_task"
                for item in self.store.read()["proposals"].values()
            ):
                continue
            proposal = self.prepare({
                "request_id": digest[:32], "path": path, "revision": revision,
                "scope": scope, "kind": "prepare_task",
            }, today)
            if "approval_digest" not in proposal:
                return False
            authorization = fingerprint([data["standing"], data["active_projects"]])
            self.store.update(
                lambda state: state["proposals"][proposal["id"]].update(
                    standing_authorization=authorization,
                    standing_project={"path": current["path"], "revision": current["revision"]},
                )
            )
            self.decide({
                "proposal_id": proposal["id"], "approval_digest": proposal["approval_digest"], "decision": "approve",
            }, today)
            return True
        return False

    def review_result(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._require_enabled()
        if (
            set(payload) not in ({"proposal_id"}, {"proposal_id", "acknowledge_unavailable"})
            or ("acknowledge_unavailable" in payload and type(payload["acknowledge_unavailable"]) is not bool)
        ):
            raise TaskError("task_request_invalid")
        identifier = payload["proposal_id"]
        if not isinstance(identifier, str) or not HEX24.fullmatch(identifier):
            raise TaskError("task_proposal_invalid")
        original = self.store.read()["proposals"].get(identifier)
        if (
            not original or not original.get("task_workspace") or original.get("status") != "completed"
            or original.get("kind") not in {"prepare_task", "research"}
        ):
            raise TaskError("task_result_not_ready")
        if original.get("reviewed") is True:
            return {
                "reviewed": True, "acknowledged_unavailable": original.get("acknowledged_unavailable") is True,
                "task_closed": False,
            }
        projection = self._project_receipt(original, self.clock().astimezone(timezone.utc).date())
        unavailable = projection["source_status"] != "current"
        acknowledge = payload.get("acknowledge_unavailable") is True
        if unavailable and not acknowledge:
            raise TaskError("task_result_acknowledgment_required")
        if acknowledge and not unavailable:
            raise TaskError("task_result_available")

        def reviewed(state: dict[str, Any]) -> dict[str, Any]:
            record = state["proposals"].get(identifier)
            if (
                not record or not record.get("task_workspace") or record.get("status") != "completed"
                or record.get("kind") not in {"prepare_task", "research"}
            ):
                raise TaskError("task_result_not_ready")
            if record.get("invalidation_reason") == "source_removed" and not acknowledge:
                raise TaskError("task_result_acknowledgment_required")
            record["reviewed"] = True
            if acknowledge:
                record["acknowledged_unavailable"] = True
            return {
                "reviewed": True, "acknowledged_unavailable": record.get("acknowledged_unavailable") is True,
                "task_closed": False,
            }

        return self.store.update(reviewed)

    def clarification(
        self, path: str, revision: str, today: date, *, answer: str | None = None, request_id: str | None = None,
        question_token: str | None = None,
    ) -> dict[str, Any]:
        source = self._source(path, revision)
        if not task_path(path):
            raise TaskError("task_path_invalid")
        identifier = fingerprint(["task-clarification", path, revision])[:24]
        request_key = fingerprint(["task-answer", _identifier(request_id)]) if answer is not None else None
        answer_digest = fingerprint([identifier, question_token, answer])

        def update(state: dict[str, Any]) -> dict[str, Any]:
            data = workspace(state)
            record = data["clarifications"].setdefault(identifier, {
                "source_path": path, "source_revision": revision, "turns": 0, "changes": {},
                "field": None, "expires_on": (today + timedelta(days=14)).isoformat(),
            })
            if record.get("expired") or record["expires_on"] <= today.isoformat():
                raise TaskError("task_clarification_expired")
            if request_key and request_key in data["requests"]:
                if data["requests"][request_key]["digest"] != answer_digest:
                    raise TaskError("task_request_conflict")
                return copy.deepcopy(record)
            if answer is not None:
                field = record["field"]
                if record["turns"] >= 3:
                    raise TaskError("task_clarification_limit")
                issued = record.get("question_token")
                if (
                    not isinstance(question_token, str) or not HEX32.fullmatch(question_token)
                    or not isinstance(issued, str) or not secrets.compare_digest(question_token, issued)
                ):
                    raise TaskError("task_clarification_stale")
                if field is None:
                    raise TaskError("task_clarification_stale")
                change = validate_changes({field: answer})
                record["changes"].update(change)
                record["turns"] += 1
                data["requests"][request_key] = {
                    "digest": answer_digest, "status": "recorded", "created_on": today.isoformat(),
                }
            merged = {**source, **record["changes"]}
            missing = definition_gaps(merged)
            next_field = next(
                (field for field in QUESTIONS if field in missing), None,
            ) if record["turns"] < 3 else None
            if answer is not None or next_field != record["field"] or not record.get("question_token"):
                record["question_token"] = secrets.token_hex(16) if next_field else None
            record["field"] = next_field
            return copy.deepcopy(record)

        saved = self.store.update(update)
        saved.update(
            id=identifier, question=QUESTIONS.get(saved["field"]),
            question_token=saved.get("question_token"),
            unresolved=definition_gaps({**source, **saved["changes"]}),
        )
        return saved

    def clarification_proposal(self, identifier: str, today: date) -> dict[str, Any] | None:
        if not isinstance(identifier, str) or not HEX24.fullmatch(identifier):
            raise TaskError("task_clarification_invalid")
        record = workspace(self.store.read())["clarifications"].get(identifier)
        if not record or not record["changes"]:
            return None
        if record.get("expired") or record["expires_on"] <= today.isoformat():
            raise TaskError("task_clarification_expired")
        source = self._source(record["source_path"], record["source_revision"])
        changes = {**record["changes"], "stage": "clarify"}
        merged = {**source, **changes}
        if not definition_gaps(merged):
            changes["stage"] = "backlog"
        return self.refine({
            "request_id": fingerprint(["clarification-proposal", identifier, changes])[:32],
            "path": record["source_path"], "revision": record["source_revision"], "changes": changes,
        }, today)

    def telegram_card(self, identifier: str, send: Callable[[str, list | None], int]) -> None:
        record = self._project_receipt(
            self.store.read()["proposals"][identifier], self.clock().astimezone(timezone.utc).date(),
        )
        if "action" not in record:
            raise TaskError("task_source_changed")
        blocks = [
            "<b>Review the exact task action</b>",
            *escaped_chunks(json.dumps(record["action"], ensure_ascii=False, indent=2), 3000),
            "Approval applies only to this action and file version. A submitted change is not saved until verified.",
            escape(record["source_url"]),
        ]
        parts = pack_html_blocks(blocks, limit=3500)

        def claim(state: dict[str, Any]) -> bool:
            current = state["proposals"][identifier]
            if current.get("task_delivery"):
                return False
            current["task_delivery"] = "sending"
            return True

        if not self.store.update(claim):
            return
        last_id = None
        for part in parts:
            last_id = send(part, None)
        keyboard = [[
            {"text": "Approve this action", "callback_data": f"brief1|approve|{identifier}"},
            {"text": "Keep unchanged", "callback_data": f"brief1|decline|{identifier}"},
        ]]
        last_id = send("Approve the exact action above, or keep the task unchanged.", keyboard)

        def bind(state: dict[str, Any]) -> None:
            state["proposals"][identifier]["message_ids"].append(last_id)
            state["messages"][str(last_id)] = identifier
            state["proposals"][identifier]["task_delivery"] = "sent"

        self.store.update(bind)
