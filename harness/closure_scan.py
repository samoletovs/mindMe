"""Shared, content-free contract for explicitly continued closure checks."""

from __future__ import annotations

import re
from typing import Any

CONTINUATION = re.compile(r"[a-f0-9]{32}\Z")
SCAN_PASS_LIMIT = 6
SCAN_KEYS = {
    "version", "action_id", "status", "path", "phase", "resumable",
    "closure_continuation", "scan_pass", "scan_pass_limit",
}
SCAN_ERRORS = frozenset({
    "task_closure_scan_unconfirmed", "task_closure_checkpoint_unavailable",
    "task_closure_scan_exhausted", "task_closure_source_changed",
    "task_closure_inventory_changed", "task_closure_inventory_limit", "task_closure_inventory_invalid",
    "task_closure_continuation_invalid", "receipt_unavailable", "remote_outcome_unknown",
    "source_changed", "task_links_require_repair", "destination_exists", "action_id_reused",
})
# These fixed 409 diagnostics do not expand the uncertain 503 receipt contract.
CONFLICT_ERRORS = SCAN_ERRORS | frozenset({
    "source_closed", "task_linked_path", "task_source_invalid", "task_source_not_allowed",
    "task_publication_proof_missing", "task_context_limit", "task_closure_conflict",
})


def is_closure(record: dict[str, Any]) -> bool:
    action = record.get("action")
    return (
        record.get("task_workspace") is True and record.get("kind") == "close_task"
        and isinstance(action, dict) and action.get("kind") == "update_task"
        and action.get("change") == {"status": "done"}
        and isinstance(action.get("completion"), dict)
        and action.get("path") == record.get("source_path")
    )


def scan_pending(record: dict[str, Any]) -> bool:
    return (
        record.get("closure_scan_protocol") == 1 and is_closure(record)
        and not record.get("publication_action_id")
        and not (record.get("result") or {}).get("pr_url")
    )


def confirmed_scan(value: object) -> bool:
    return (
        isinstance(value, dict) and set(value) == SCAN_KEYS
        and type(value["version"]) is int and value["version"] == 1
        and isinstance(value["action_id"], str) and bool(re.fullmatch(r"[a-f0-9]{24,64}", value["action_id"]))
        and value["status"] == "in_progress" and value["path"] == ""
        and value["phase"] == "closure_scan" and value["resumable"] is True
        and isinstance(value["closure_continuation"], str)
        and bool(CONTINUATION.fullmatch(value["closure_continuation"]))
        and type(value["scan_pass"]) is int and 1 <= value["scan_pass"] <= SCAN_PASS_LIMIT
        and type(value["scan_pass_limit"]) is int and value["scan_pass_limit"] == SCAN_PASS_LIMIT
    )


def unknown_scan(code: str = "task_closure_scan_unconfirmed") -> dict[str, Any]:
    return {"status": "unknown", "error": code if code in SCAN_ERRORS else "task_closure_scan_unconfirmed"}
