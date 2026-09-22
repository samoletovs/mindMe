"""Private, bounded briefing receipts; callbacks must never perform external I/O."""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any, TypeVar

from azure.core import MatchConditions
from azure.core.exceptions import (
    AzureError,
    ResourceExistsError,
    ResourceModifiedError,
    ResourceNotFoundError,
)
from azure.storage.blob import ContainerClient

from execution_budget import checkpoint, sdk_timeouts

STATE_BLOB = "system/mindme/briefing-state-v1.json"
MAX_STATE_BYTES = 1024 * 1024
MAX_CAS_ATTEMPTS = 4
RECORD_CAPS = {
    "proposals": 200, "messages": 400, "memories": 100,
    "fingerprints": 1000, "deliveries": 14,
}
_ROOT_KEYS = {"version", *RECORD_CAPS, "last_delivered", "knowledge"}
_PROPOSAL_KEYS = {
    "id", "kind", "text", "source_path", "source_revision", "source_digest",
    "status", "created_on", "expires_on", "message_ids", "action",
}
_MEMORY_KEYS = {
    "id", "kind", "text", "source_path", "proposal_id", "created_on",
    "last_used_on", "active",
}
_PROPOSAL_STATUSES = {
    "pending", "accepted", "approved", "declined", "snoozed", "corrected",
    "expired", "superseded", "invalidated", "failed", "completed", "done",
    "executing", "running", "submitted", "uncertain", "unknown", "in_progress",
}
_RECEIPT_STATUSES = _PROPOSAL_STATUSES | {"merged", "conflict"}
_UNEXECUTED_STATUSES = {"pending", "accepted", "approved", "snoozed"}
_TOMBSTONE_STATUSES = _PROPOSAL_STATUSES - _UNEXECUTED_STATUSES
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_RECEIPT_URL = re.compile(
    r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(?:issues|pull)/[1-9]\d*\Z"
)
_SECRET = re.compile(
    r"(?i)(?:"
    r"\bgh[pousr]_[A-Za-z0-9]{16,}|\bgithub_pat_[A-Za-z0-9_]{16,}|"
    r"\bsk-[A-Za-z0-9_-]{16,}|\bxox[baprs]-[A-Za-z0-9-]{10,}|"
    r"\bBearer\s+\S+|\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.|"
    r"\b(?:password|passwd|api[_ -]?key|secret|client[_ -]?secret|token|"
    r"AccountKey|SharedAccessKey|SharedAccessSignature)\b\s*[:=]\s*\S+|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b|"
    r"https?://[^\s/]+:[^\s@]+@|[?&](?:sig|token|code|key|api_key)=\S+|"
    r"\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b)"
)
_TOMBSTONE_KEYS = {
    "id", "status", "invalidation_reason", "invalidated_on", "previous_status",
    "action_id", "result", "activity",
}
T = TypeVar("T")


class StateError(RuntimeError):
    """A content-free state error code, never an Azure exception or private value."""


def empty_state() -> dict[str, Any]:
    return {
        "version": 1, "proposals": {}, "messages": {}, "memories": {},
        "fingerprints": {}, "deliveries": {}, "last_delivered": None,
        "knowledge": {"memories": {}, "bindings": {}, "requests": {}, "topics": {}},
    }


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _day(value: object) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise StateError("invalid_state_date")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise StateError("invalid_state_date") from None


def is_expired(expires_on: str, today: date) -> bool:
    """An expiry date is exclusive: the record expires at its start, not its end."""
    return _day(expires_on) <= today


def record_transition(record: dict[str, Any], status: str, today: date) -> None:
    """Date observations, never reconstruct historical completion dates."""
    if status not in _PROPOSAL_STATUSES:
        raise StateError("invalid_activity_status")
    if record["status"] == status:
        return
    cutoff = (today - timedelta(days=35)).isoformat()
    activity = [item for item in record.get("activity", []) if item["date"] >= cutoff]
    activity.append({"date": today.isoformat(), "status": status})
    record["activity"] = activity[-32:]
    record["status"] = status


def trim_deliveries(state: dict[str, Any]) -> None:
    """Keep weekly baselines independent of the daily delivery checkpoint."""
    for kind, keep in (("daily", 8), ("weekly", 2)):
        if kind == "weekly":
            for key, item in list(state["deliveries"].items()):
                if item.get("kind") == "weekly" and item["status"] == "abandoned":
                    del state["deliveries"][key]
        completed = sorted(
            (
                (item["date"], item.get("completed_order", 0), key)
                for key, item in state["deliveries"].items()
                if item.get("kind", "daily") == kind and item["status"] in {"sent", "abandoned"}
            ),
        )
        for _, _, key in completed[:-keep]:
            del state["deliveries"][key]


def _message_ids(value: object) -> bool:
    return isinstance(value, list) and all(type(item) is int and item > 0 for item in value)


def _safe_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    kept: dict[str, Any] = {}
    for key, value in receipt.items():
        if key == "status" and isinstance(value, str) and value in _RECEIPT_STATUSES:
            kept[key] = value
        elif key in {"action_id", "operation_id", "request_id", "job_id"}:
            if isinstance(value, str) and _IDENTIFIER.fullmatch(value):
                kept[key] = value
        elif key in {"issue_number", "pr_number"} and type(value) is int and value > 0:
            kept[key] = value
        elif key in {"issue_url", "pr_url"} and isinstance(value, str) and _RECEIPT_URL.fullmatch(value):
            kept[key] = value
    return kept


def _tombstone(record: dict[str, Any], today: date) -> dict[str, Any]:
    status = record["status"]
    receipt = _safe_receipt(record.get("result") or {})
    verified_snooze = status == "snoozed" and receipt.get("status") == "merged"
    result: dict[str, Any] = {
        "id": record["id"],
        "status": "invalidated" if status in _UNEXECUTED_STATUSES and not verified_snooze else status,
        "invalidation_reason": "source_removed",
        "invalidated_on": today.isoformat(), "previous_status": status,
    }
    if isinstance(record.get("action_id"), str) and _IDENTIFIER.fullmatch(record["action_id"]):
        result["action_id"] = record["action_id"]
    if isinstance(record.get("result"), dict):
        result["result"] = receipt
    if record.get("activity"):
        result["activity"] = copy.deepcopy(record["activity"])
    return result


def _validate_proposal(identifier: str, record: object) -> None:
    if not isinstance(record, dict) or record.get("id") != identifier:
        raise StateError("invalid_proposal")
    if "activity" in record:
        activity = record["activity"]
        if not isinstance(activity, list) or len(activity) > 32:
            raise StateError("invalid_proposal_activity")
        for item in activity:
            if (
                not isinstance(item, dict) or set(item) != {"date", "status"}
                or not isinstance(item["status"], str) or item["status"] not in _PROPOSAL_STATUSES
            ):
                raise StateError("invalid_proposal_activity")
            _day(item["date"])
    if record.get("invalidation_reason") == "source_removed":
        required = {"id", "status", "invalidation_reason", "invalidated_on", "previous_status"}
        if not required <= record.keys() or not record.keys() <= _TOMBSTONE_KEYS:
            raise StateError("invalid_source_tombstone")
        if (
            not isinstance(record["status"], str) or not isinstance(record["previous_status"], str)
            or record["previous_status"] not in _PROPOSAL_STATUSES
        ):
            raise StateError("invalid_source_tombstone")
        verified_snooze = (
            record["status"] == record["previous_status"] == "snoozed"
            and isinstance(record.get("result"), dict) and record["result"].get("status") == "merged"
        )
        if not verified_snooze and (
            record["status"] not in _TOMBSTONE_STATUSES
            or (record["previous_status"] in _UNEXECUTED_STATUSES and record["status"] != "invalidated")
        ):
            raise StateError("invalid_source_tombstone")
        _day(record["invalidated_on"])
        if "action_id" in record and (
            not isinstance(record["action_id"], str) or not _IDENTIFIER.fullmatch(record["action_id"])
        ):
            raise StateError("invalid_source_tombstone")
        if "result" in record and (
            not isinstance(record["result"], dict) or _safe_receipt(record["result"]) != record["result"]
        ):
            raise StateError("invalid_source_tombstone")
        return
    if not _PROPOSAL_KEYS <= record.keys():
        raise StateError("invalid_proposal")
    if "knowledge_sources" in record:
        from knowledge_state import _refs

        if not _refs(record["knowledge_sources"]):
            raise StateError("invalid_proposal_evidence")
    for key in ("kind", "text", "source_path", "source_revision", "source_digest", "status"):
        if not _nonempty(record[key]):
            raise StateError("invalid_proposal")
    if record["status"] not in _PROPOSAL_STATUSES:
        raise StateError("invalid_proposal")
    if not isinstance(record["action"], dict) or not _message_ids(record["message_ids"]):
        raise StateError("invalid_proposal")
    if _day(record["expires_on"]) < _day(record["created_on"]):
        raise StateError("invalid_proposal_dates")
    if "review_on" in record:
        _day(record["review_on"])
    if "result" in record and not isinstance(record["result"], dict):
        raise StateError("invalid_action_receipt")
    if "action_id" in record and (
        not isinstance(record["action_id"], str) or not _IDENTIFIER.fullmatch(record["action_id"])
    ):
        raise StateError("invalid_action_identifier")


def _check_tombstones(before: dict[str, dict[str, Any]], after: dict[str, Any]) -> None:
    for identifier, previous in before.items():
        current = after.get(identifier, {})
        if current.get("invalidation_reason") != "source_removed":
            raise StateError("source_tombstone_immutable")
        for key in ("action_id", "previous_status", "invalidated_on"):
            if key in previous and current.get(key) != previous[key]:
                raise StateError("source_receipt_immutable")
        if previous["status"] in {"completed", "done"}:
            if current["status"] != previous["status"]:
                raise StateError("source_receipt_immutable")
            for key, value in previous.get("result", {}).items():
                if current.get("result", {}).get(key) != value:
                    raise StateError("source_receipt_immutable")
        for key in ("action_id", "operation_id", "request_id", "job_id"):
            if key in previous.get("result", {}) and (
                current.get("result", {}).get(key) != previous["result"][key]
            ):
                raise StateError("source_receipt_immutable")


def _validate(state: object) -> None:
    if not isinstance(state, dict) or set(state) not in (_ROOT_KEYS, _ROOT_KEYS - {"knowledge"}) or type(state["version"]) is not int:
        raise StateError("invalid_state_schema")
    if "knowledge" in state:
        from knowledge_state import validate_knowledge

        validate_knowledge(state["knowledge"])
    if state["version"] != 1:
        raise StateError("unsupported_state_version")
    if state["last_delivered"] is not None and not isinstance(state["last_delivered"], dict):
        raise StateError("invalid_delivery_checkpoint")
    for key, limit in RECORD_CAPS.items():
        if not isinstance(state[key], dict):
            raise StateError("invalid_state_collection")
        if len(state[key]) > limit:
            raise StateError(f"state_{key}_capacity")
        if not all(_nonempty(identifier) for identifier in state[key]):
            raise StateError("invalid_state_identifier")
    for identifier, record in state["proposals"].items():
        _validate_proposal(identifier, record)
    for message_id, proposal_id in state["messages"].items():
        if (
            not re.fullmatch(r"[1-9]\d*", message_id)
            or not isinstance(proposal_id, str) or proposal_id not in state["proposals"]
        ):
            raise StateError("invalid_message_binding")
    for identifier, record in state["memories"].items():
        if not isinstance(record, dict) or not _MEMORY_KEYS <= record.keys():
            raise StateError("invalid_memory")
        if any(not _nonempty(record[key]) for key in ("id", "kind", "text", "source_path", "proposal_id")):
            raise StateError("invalid_memory")
        if record["kind"] not in {"correction", "preference", "fact", "event"} or type(record["active"]) is not bool:
            raise StateError("invalid_memory")
        if len(record["text"]) > 280 or _SECRET.search(record["text"]):
            raise StateError("unsafe_memory_text")
        _day(record["created_on"])
        _day(record["last_used_on"])
        if record.get("expires_on") is not None:
            _day(record["expires_on"])
        elif record["kind"] == "event":
            raise StateError("event_requires_expiry")
        if "supersedes" in record and not _nonempty(record["supersedes"]):
            raise StateError("invalid_memory")
        if record["id"] != identifier:
            raise StateError("invalid_memory_identifier")
        proposal = state["proposals"].get(record["proposal_id"])
        if proposal and (
            proposal.get("invalidation_reason") == "source_removed"
            or proposal.get("source_path") != record["source_path"]
        ):
            raise StateError("memory_source_invalidated")
    if any(not _nonempty(value) for value in state["fingerprints"].values()):
        raise StateError("invalid_source_fingerprint")
    for delivery in state["deliveries"].values():
        if not isinstance(delivery, dict) or not {"status", "date", "message_ids"} <= delivery.keys():
            raise StateError("invalid_delivery")
        if not isinstance(delivery["status"], str) or delivery["status"] not in {
            "sending", "sent", "abandoned", "uncertain", "failed",
        }:
            raise StateError("invalid_delivery")
        _day(delivery["date"])
        if not _message_ids(delivery["message_ids"]):
            raise StateError("invalid_delivery")
        if "fingerprints" in delivery and (
            not isinstance(delivery["fingerprints"], dict)
            or any(not _nonempty(key) or not _nonempty(value) for key, value in delivery["fingerprints"].items())
        ):
            raise StateError("invalid_delivery")


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StateError("duplicate_state_key")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise StateError("invalid_state_number")


def _encode(state: dict[str, Any]) -> bytes:
    _validate(state)
    try:
        payload = json.dumps(state, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise StateError("invalid_state_encoding") from None
    if len(payload) > MAX_STATE_BYTES:
        raise StateError("state_payload_capacity")
    return payload


class BriefingStore:
    """Synchronous SDK adapter; async handlers must call it off the event loop.

    ``mutate`` runs on the freshly read state before each conditional write. It
    may run repeatedly, so external effects belong strictly after ``update``
    returns. Failed writes never return the callback's result.
    """

    def __init__(self, container: ContainerClient, *, blob_name: str = STATE_BLOB) -> None:
        try:
            self._blob = container.get_blob_client(blob_name)
        except AzureError:
            raise StateError("state_client_unavailable") from None

    def _load(self) -> tuple[dict[str, Any], str | None]:
        checkpoint()
        try:
            download = self._blob.download_blob(
                logging_enable=False, max_concurrency=1, **sdk_timeouts(),
            )
            checkpoint()
        except ResourceNotFoundError as error:
            if getattr(error, "error_code", None) in {None, "BlobNotFound"}:
                return empty_state(), None
            raise StateError("state_read_unavailable") from None
        except AzureError:
            raise StateError("state_read_unavailable") from None
        # These properties belong to this download, not a later HEAD request.
        etag = getattr(download.properties, "etag", None)
        size = getattr(download.properties, "size", None)
        if not _nonempty(etag) or type(size) is not int or size < 0:
            raise StateError("invalid_state_metadata")
        if size > MAX_STATE_BYTES:
            raise StateError("state_payload_capacity")
        try:
            checkpoint()
            payload = download.readall()
            checkpoint()
        except AzureError:
            raise StateError("state_read_unavailable") from None
        if not isinstance(payload, bytes) or len(payload) > MAX_STATE_BYTES:
            raise StateError("state_payload_capacity")
        if len(payload) != size:
            raise StateError("invalid_state_length")
        try:
            state = json.loads(payload.decode("utf-8"), object_pairs_hook=_object_pairs, parse_constant=_invalid_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise StateError("invalid_state_json") from None
        _encode(state)
        state.setdefault("knowledge", {"memories": {}, "bindings": {}, "requests": {}, "topics": {}})
        return state, etag

    def read(self) -> dict[str, Any]:
        state, _ = self._load()
        return state

    def update(self, mutate: Callable[[dict[str, Any]], T]) -> T:
        for _ in range(MAX_CAS_ATTEMPTS):
            checkpoint()
            state, etag = self._load()
            removed = {
                key: copy.deepcopy(record) for key, record in state["proposals"].items()
                if record.get("invalidation_reason") == "source_removed"
            }
            result = mutate(state)
            payload = _encode(state)
            _check_tombstones(removed, state["proposals"])
            try:
                if etag is None:
                    self._blob.upload_blob(
                        payload, overwrite=False, logging_enable=False, **sdk_timeouts(),
                    )
                else:
                    self._blob.upload_blob(
                        payload, overwrite=True, etag=etag,
                        match_condition=MatchConditions.IfNotModified,
                        logging_enable=False, **sdk_timeouts(),
                    )
                checkpoint()
            except ResourceModifiedError:
                continue
            except ResourceExistsError:
                if etag is None:
                    continue
                raise StateError("state_write_unavailable") from None
            except AzureError:
                raise StateError("state_write_unavailable") from None
            return result
        raise StateError("state_conflict_retry_exhausted")


def parse_reply(text: str, today: date) -> dict[str, Any]:
    """Classify only explicit replies; target binding never happens here."""
    unknown = {"intent": "unknown"}
    if not isinstance(text, str):
        return unknown
    text = text.strip()
    if _SECRET.search(text):
        return {"intent": "unknown", "clarification": "Do not include credentials. Send a non-sensitive decision."}
    correction = re.match(r"(?is)^(?:correction:|actually\s+)(.*)$", text)
    change = re.match(r"(?is)^change:(.*)$", text)
    if correction or change:
        match = correction if correction is not None else change
        assert match is not None
        value = match.group(1).strip()
        if not value or len(value) > 280 or "\n" in value or "\r" in value:
            return {"intent": "unknown", "clarification": "Use one concise correction or change, at most 280 characters."}
        return {"intent": "correct" if correction else "change", "text": value}
    normalized = " ".join(text.casefold().split()).rstrip(".!?")
    intents = {
        "approve": {"yes", "do it", "go ahead", "approve"},
        "decline": {"no", "skip", "not interested", "decline"},
        "done": {"done", "already done", "completed"},
        "explain": {"why", "explain", "explain more"},
    }
    for intent, phrases in intents.items():
        if normalized in phrases:
            return {"intent": intent}
    scheduled = re.fullmatch(
        r"(?:(?:snooze|later|remind me)\s+)?(tomorrow|next week)", normalized,
    )
    dated = re.fullmatch(r"(?:snooze|later|remind me)\s+(\d{4}-\d{2}-\d{2})", normalized)
    if scheduled or dated:
        try:
            if scheduled is not None:
                review = today + timedelta(days=1 if scheduled.group(1) == "tomorrow" else 7)
            else:
                assert dated is not None
                review = date.fromisoformat(dated.group(1))
        except (ValueError, OverflowError):
            return {"intent": "unknown", "clarification": "Use a valid date: snooze YYYY-MM-DD."}
        if review < today:
            return {"intent": "unknown", "clarification": "That date is in the past. Use snooze YYYY-MM-DD."}
        return {"intent": "snooze", "review_on": review.isoformat()}
    if normalized.startswith(("snooze", "later", "remind me", "next month")):
        return {"intent": "unknown", "clarification": "Which date? Reply snooze YYYY-MM-DD; no reminder was scheduled."}
    if normalized.startswith("remember:"):
        return {"intent": "unknown", "clarification": "Clarify the scope first, or use correction: for this proposal."}
    return unknown


def prune_state(state: dict[str, Any], *, inventory_paths: set[str], today: date) -> None:
    """Prune only against a complete pinned inventory, never a bounded read list.

    Pending proposals expire on ``expires_on``. Unresolved action receipts never
    expire implicitly. Source removal invalidates approval, not action history:
    completed/unresolved outcomes survive in non-replayable, source-less receipts.
    """
    cutoff = (today - timedelta(days=35)).isoformat()
    for record in state["proposals"].values():
        if "activity" in record:
            record["activity"] = [item for item in record["activity"] if item["date"] >= cutoff]
    removed = {
        key for key, record in state["proposals"].items()
        if record.get("invalidation_reason") == "source_removed"
    }
    for identifier, record in state["proposals"].items():
        path = record.get("source_path")
        if path and path not in inventory_paths:
            state["proposals"][identifier] = _tombstone(record, today)
            removed.add(identifier)
        elif record["status"] == "pending" and is_expired(record["expires_on"], today):
            record["status"] = "expired"
    for identifier, record in list(state["memories"].items()):
        if (
            record["source_path"] not in inventory_paths
            or record["proposal_id"] in removed
            or (record.get("expires_on") is not None and is_expired(record["expires_on"], today))
        ):
            del state["memories"][identifier]
    for path in list(state["fingerprints"]):
        if path not in inventory_paths:
            del state["fingerprints"][path]
    for delivery in state["deliveries"].values():
        fingerprints = delivery.get("fingerprints", {})
        missing = set(fingerprints) - inventory_paths
        missing_baseline = set(delivery.get("baseline", {})) - inventory_paths
        if (
            missing or missing_baseline or delivery.get("proposal_id") in removed
            or bool(set(delivery.get("proposal_ids", [])) & removed)
        ):
            delivery.pop("text", None)
            if delivery["status"] == "sending":
                delivery["status"] = "abandoned"
        for path in missing:
            del fingerprints[path]
