"""Read-only, pinned task views. Markdown, not this projection, is authoritative."""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any
from urllib.parse import quote

import httpx
import yaml

from briefing_plan import fingerprint
from briefing_sources import (
    MAX_FILE_BYTES, SourceError, _MISSING, _SENSITIVE_CONTENT, _canonical_head,
    _canonical_inventory, _content, _excluded_metadata, _headers, _json, _kind, _metadata, _metadata_pair,
)
from task_state import AREAS, PROJECT, STAGES

PAGE_SIZE = 12
MAX_ATTENTION_READS = 96
ATTENTION_DEADLINE_HORIZON_DAYS = 7
MAX_TASK_CHARS = 16_000
_PLACEHOLDER = re.compile(r"(?:\{\{.*\}\}|<[^>]+>|tbd|todo|unknown|\?)\Z", re.I)


class _TaskLoader(yaml.SafeLoader):
    pass


class _SourceExcluded(SourceError):
    """A proven policy exclusion, not an unavailable permitted source."""


def _mapping(loader: _TaskLoader, node: yaml.MappingNode) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise SourceError("task_metadata_invalid")
        result[key] = loader.construct_object(value_node)
    return result


_TaskLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)
_TaskLoader.add_constructor("tag:yaml.org,2002:timestamp", yaml.SafeLoader.construct_scalar)


def task_path(value: object) -> bool:
    return (
        isinstance(value, str) and _kind(value, tasks=True) == "task"
        and bool(re.fullmatch(r"tasks/[^/]+\.md", value))
    )


def paragraph(raw: str, label: str) -> str:
    prose = re.sub(r"(?ms)^```.*?^```[ \t]*$", "", raw)
    found = re.search(
        rf"(?ms)^\*\*{re.escape(label)}:\*\*[ \t]*(.*?)(?=\n[ \t]*\n|\n\*\*[^:\n]+:\*\*|\n#|\Z)",
        prose,
    )
    return found[1].strip() if found else ""


def _permitted(raw: str) -> bool:
    _, pairs = _metadata(raw)
    if _excluded_metadata(pairs) or _SENSITIVE_CONTENT.search(raw):
        return False
    for key, value in pairs:
        value = value.lower()
        if key in {"ignored", "generated", "derived", "work"} and value not in {"", "false", "no", "0", "none"}:
            return False
        if key in {"scope", "routed_to"} and value not in {"", "personal", "mindme", "mindvault"}:
            return False
    return True


def _require_permitted(raw: str) -> None:
    if _permitted(raw):
        return
    if _SENSITIVE_CONTENT.search(raw):
        raise _SourceExcluded("task_source_not_permitted")
    for line in raw.splitlines():
        pair = _metadata_pair(line)
        if pair is None:
            continue
        key, value = pair[0], pair[1].casefold()
        if key not in {
            "private", "sensitive", "confidential", "ignored", "generated", "derived", "work",
            "classification", "sensitivity", "visibility", "level", "route", "routing", "vault",
            "destination", "owner", "ownership", "scope", "routed_to",
        }:
            continue
        try:
            declaration = yaml.load(line.strip().removeprefix("- "), Loader=_TaskLoader)
        except (yaml.YAMLError, SourceError, RecursionError, TypeError):
            continue
        if not isinstance(declaration, dict) or len(declaration) != 1:
            continue
        declared_key, declared_value = next(iter(declaration.items()))
        if declared_key.casefold().replace("-", "_") != key:
            continue
        if type(declared_value) not in {str, bool, int}:
            continue
        value = str(declared_value).casefold().strip()
        if key in {"private", "sensitive", "confidential", "ignored", "generated", "derived", "work"}:
            if value in {"true", "yes", "1"}:
                raise _SourceExcluded("task_source_not_permitted")
        elif key in {"classification", "sensitivity", "visibility", "level"}:
            if value in {"private", "sensitive", "confidential", "secret", "restricted"}:
                raise _SourceExcluded("task_source_not_permitted")
        elif key in {"route", "routing", "vault", "destination", "owner", "ownership", "scope", "routed_to"}:
            if re.fullmatch(r"[a-z][a-z0-9_-]*", value) and (
                _excluded_metadata([(key, value)])
                or (key in {"scope", "routed_to"} and value not in {"personal", "mindme", "mindvault"})
            ):
                raise _SourceExcluded("task_source_not_permitted")
    raise SourceError("task_source_policy_unresolved")


def _source_error_code(error: SourceError) -> str:
    code = str(error)
    return code if code in {
        "task_metadata_missing", "task_metadata_invalid", "task_definition_invalid", "task_date_invalid",
        "task_title_missing", "task_review_date_ambiguous", "task_source_too_large", "task_source_policy_unresolved",
    } else "task_source_unavailable"


def definition_gaps(task: dict[str, Any]) -> list[str]:
    missing = [
        key for key in ("area", "stage", "execution", "outcome", "next_action", "done_when")
        if not task.get(key) or task[key] == "untriaged" or _PLACEHOLDER.fullmatch(str(task[key]))
    ]
    if task.get("stage") == "waiting" and not task.get("waiting_for"):
        missing.append("waiting_for")
    if (task.get("waiting_for") or task.get("stage") == "waiting") and not task.get("review_on"):
        missing.append("review_on")
    if task.get("waiting_for") and task.get("stage") in {"ready", "doing", "verify"}:
        missing.append("waiting_consistency")
    return missing


def attention_reasons(task: dict[str, Any], today: date) -> list[str]:
    reasons = []
    deadline = task.get("deadline")
    if deadline and date.fromisoformat(deadline) <= today + timedelta(days=ATTENTION_DEADLINE_HORIZON_DAYS):
        reasons.append("deadline_due" if deadline <= today.isoformat() else "deadline_soon")
    review = task.get("review_on")
    if (
        (task.get("snoozed_until") and task["snoozed_until"] > today.isoformat())
        or (review and review > today.isoformat())
    ):
        return reasons
    if review and review <= today.isoformat():
        reasons.append("review_due")
    if (
        task.get("focus_on") == today.isoformat() and not task.get("waiting_for")
        and task.get("stage") != "waiting"
        and task.get("legacy_status", "").lower() not in {"skipped", "snoozed", "deferred", "waiting"}
    ):
        reasons.append("focus_today")
    return reasons


def _attention_priority(task: dict[str, Any]) -> tuple[int, str, str]:
    reasons = task["attention_reasons"]
    if "deadline_due" in reasons or "deadline_soon" in reasons:
        return 0, task["deadline"], task["path"]
    if "focus_today" in reasons:
        return 1, task["focus_on"], task["path"]
    if "review_due" in reasons:
        return 3 if task.get("waiting_for") else 2, task["review_on"], task["path"]
    return 4, "", task["path"]


def parse_task(raw: str, path: str) -> dict[str, Any]:
    if len(raw) > MAX_TASK_CHARS:
        raise SourceError("task_source_too_large")
    _require_permitted(raw)
    block = re.match(r"\ufeff?---[ \t]*\r?\n(.*?)\r?\n---(?:\r?\n|$)", raw, re.S)
    if not block:
        details = re.match(r"\ufeff?# [^\n]+\n\s*(<details>.*?</details>)", raw, re.S)
        block = re.search(r"```ya?ml[ \t]*\n(.*?)\n```", details[1], re.S) if details else None
    if block is None:
        raise SourceError("task_metadata_missing")
    if re.search(r"(?:^|\s)[&*][\w-]+|!!|<<\s*:", block[1]):
        raise SourceError("task_metadata_invalid")
    try:
        values = yaml.load(block[1], Loader=_TaskLoader)
    except (yaml.YAMLError, RecursionError, TypeError):
        raise SourceError("task_metadata_invalid") from None
    if not isinstance(values, dict):
        raise SourceError("task_metadata_invalid")
    if values.get("type", "task") != "task":
        raise SourceError("task_metadata_invalid")
    title = re.search(r"(?m)^# ([^\n]+)", raw)
    if title is None:
        raise SourceError("task_title_missing")
    task: dict[str, Any] = {"path": path, "title": title[1].strip()}
    legacy_status = values.get("status", "")
    if not isinstance(legacy_status, str):
        raise SourceError("task_metadata_invalid")
    task["legacy_status"] = legacy_status
    for field in ("area", "project", "stage", "execution", "waiting_for"):
        value = values.get(field, "")
        if not isinstance(value, str):
            raise SourceError("task_metadata_invalid")
        task[field] = value
    for field in ("captured", "deadline", "review_on", "focus_on", "due", "snoozed_until"):
        value = values.get(field)
        if value is None:
            task[field] = None
            continue
        if type(value) is date:
            value = value.isoformat()
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise SourceError("task_date_invalid")
        try:
            date.fromisoformat(value)
        except ValueError:
            raise SourceError("task_date_invalid") from None
        task[field] = value
    if task["due"] and task["review_on"] and task["due"] != task["review_on"]:
        raise SourceError("task_review_date_ambiguous")
    task["review_on"] = task["review_on"] or task["due"]
    task.pop("due")
    if (
        (task["stage"] and task["stage"] not in STAGES)
        or (task["area"] and task["area"] not in AREAS)
        or (task["execution"] and task["execution"] not in {"human", "assisted", "agent"})
        or (task["project"] and not PROJECT.fullmatch(task["project"]))
    ):
        raise SourceError("task_definition_invalid")
    task["stage"] = task["stage"] or "untriaged"
    for field, label in (("outcome", "Outcome"), ("next_action", "Next action"), ("done_when", "Done when")):
        value = values.get(field, "")
        if not isinstance(value, str):
            raise SourceError("task_definition_invalid")
        task[field] = value.strip() or paragraph(raw, label)
    missing = definition_gaps(task)
    task["definition"] = {"assessment": "structure_only", "complete": not missing, "missing": missing}
    return task


class TaskRepository:
    def __init__(self, client: httpx.Client, *, token: str, repo: str) -> None:
        self.client, self.repo = client, repo
        self.headers = _headers(token, repo)
        self.base = f"/repos/{repo}"

    def snapshot(self) -> tuple[str, dict[str, dict[str, Any]]]:
        head = _canonical_head(self.client, self.base, self.headers)
        return head, _canonical_inventory(self.client, self.base, self.headers, head)

    def _raw(self, path: str, head: str, entry: dict[str, Any]) -> str:
        if type(entry.get("size")) is not int or not 0 <= entry["size"] <= MAX_FILE_BYTES:
            raise SourceError("task_source_too_large")
        data = _json(
            self.client, self.base + "/contents/" + quote(path, safe="/"), self.headers,
            params={"ref": head}, missing_ok=True, limit=MAX_FILE_BYTES * 2 + 8000,
        )
        if data is _MISSING:
            raise SourceError("source_snapshot_unavailable")
        _, raw = _content(data, path, entry["sha"])
        return raw

    def _record(self, path: str, head: str, entry: dict[str, Any]) -> dict[str, Any]:
        raw = self._raw(path, head, entry)
        if task_path(path):
            record = parse_task(raw, path)
        else:
            if path != "tasks/README.md":
                _require_permitted(raw)
            record = {"path": path, "title": path.split("/")[-2], "kind": "task_contract" if path == "tasks/README.md" else "project"}
        return {
            **record, "revision": entry["sha"], "canonical_revision": head,
            "digest": fingerprint(raw), "text": raw, "publication_status": "canonical",
            "url": f"https://github.com/{self.repo}/blob/{head}/{quote(path, safe='/')}",
        }

    def read(self, path: str) -> dict[str, Any] | None:
        if path != "tasks/README.md" and not task_path(path) and _kind(path) != "project":
            raise SourceError("task_source_not_permitted")
        head, entries = self.snapshot()
        entry = entries.get(path)
        return self._record(path, head, entry) if entry else None

    def read_receipt_source(self, path: str) -> dict[str, Any] | None:
        """Closed evidence is reachable only through a host-recorded action receipt."""
        if not path.startswith("tasks/done/"):
            return self.read(path)
        if not task_path("tasks/" + path.removeprefix("tasks/done/")):
            raise SourceError("task_source_not_permitted")
        head, entries = self.snapshot()
        entry = entries.get(path)
        return self._record(path, head, entry) if entry else None

    def revision(self, path: str) -> str | None:
        record = self.read(path)
        return record["revision"] if record else None

    def page(self, offset: int = 0, *, today: date) -> dict[str, Any]:
        if type(offset) is not int or not 0 <= offset <= 5000 or offset % PAGE_SIZE:
            raise SourceError("task_page_invalid")
        head, entries = self.snapshot()
        paths = sorted(path for path in entries if task_path(path))
        start = offset // MAX_ATTENTION_READS * MAX_ATTENTION_READS
        candidates = paths[start:start + MAX_ATTENTION_READS]
        assessed, errors = [], []
        excluded_count = 0
        for path in candidates:
            try:
                task = self._record(path, head, entries[path])
            except _SourceExcluded:
                excluded_count += 1
                continue
            except SourceError as exc:
                errors.append({"code": _source_error_code(exc)})
                continue
            task.pop("text")
            task["attention_reasons"] = attention_reasons(task, today)
            task["attention_eligible"] = bool(task["attention_reasons"])
            assessed.append(task)
        ordered = sorted(assessed, key=_attention_priority)
        within = offset - start
        items = ordered[within:within + PAGE_SIZE]
        next_cursor = start + MAX_ATTENTION_READS if start + MAX_ATTENTION_READS < len(paths) else None
        next_offset = offset + PAGE_SIZE if within + PAGE_SIZE < len(ordered) else next_cursor
        attention_complete = start == 0 and len(paths) <= MAX_ATTENTION_READS and not errors
        warnings = []
        if not attention_complete:
            warnings.append(
                "Attention assessment is incomplete. Tasks outside this assessed window, or with unreadable "
                "metadata, may still have due dates in the permitted scope. "
                "An empty Needs-you view does not mean nothing is due."
            )
        if excluded_count:
            warnings.append(
                f"{excluded_count} task source candidates were excluded by the existing scope/privacy policy. "
                "They are outside the permitted task view, not failed reads."
            )
        projects = self._projects(head, entries, 0, 4)
        return {
            "items": items, "offset": offset, "next_offset": next_offset,
            "candidate_count": len(paths), "excluded_count": excluded_count, "errors": errors, "canonical_revision": head,
            "complete": attention_complete and offset == 0 and len(assessed) <= PAGE_SIZE,
            "attention_complete": attention_complete,
            "attention": {
                "status": "complete" if attention_complete else "incomplete",
                "assessed_from": start, "attempted_count": len(candidates), "assessed_count": len(assessed),
                "excluded_count": excluded_count, "unavailable_count": len(errors), "scope": "permitted",
                "candidate_count": len(paths), "next_cursor": next_cursor,
            },
            "warnings": warnings,
            "projects": projects["items"], "project_next_offset": projects["next_offset"],
            "project_errors": projects["errors"], "project_excluded_count": projects["excluded_count"],
            "project_candidate_count": projects["candidate_count"],
            "areas": list(AREAS), "stages": list(STAGES),
        }

    def _projects(self, head: str, entries: dict[str, dict[str, Any]], offset: int, limit: int) -> dict[str, Any]:
        paths = sorted(
            path for path in entries if _kind(path) == "project" and PROJECT.fullmatch(path.split("/")[1])
        )
        items, errors = [], []
        excluded_count = 0
        for path in paths[offset:offset + limit]:
            try:
                record = self._record(path, head, entries[path])
            except _SourceExcluded:
                excluded_count += 1
                continue
            except SourceError as error:
                errors.append({"code": _source_error_code(error)})
                continue
            items.append({"id": path.split("/")[1], "revision": record["revision"], "assessment": "inventory_only"})
        return {
            "items": items, "next_offset": offset + limit if offset + limit < len(paths) else None,
            "excluded_count": excluded_count, "candidate_count": len(paths),
            "errors": errors, "canonical_revision": head,
        }

    def projects(self, offset: int = 0) -> dict[str, Any]:
        if type(offset) is not int or not 0 <= offset <= 5000:
            raise SourceError("task_page_invalid")
        head, entries = self.snapshot()
        return self._projects(head, entries, offset, PAGE_SIZE)
