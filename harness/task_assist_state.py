"""Private bounded state for task assistance, separate from briefing receipts."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any, TypeVar

from azure.core import MatchConditions
from azure.core.exceptions import AzureError, ResourceExistsError, ResourceModifiedError, ResourceNotFoundError

from execution_budget import checkpoint, sdk_timeouts

ASSIST_STATE_BLOB = "system/mindme/task-assist-state-v1.json"
MAX_STATE_BYTES = 512 * 1024
MAX_THREADS = 40
MAX_REQUESTS = 160
MAX_OPERATIONS = 160
MAX_TELEGRAM_BINDINGS = 200
MAX_TELEGRAM_EVENTS = 300
MAX_TURNS = 12
RETENTION_DAYS = 14
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_TASK_ID = re.compile(r"[0-9a-f]{24}\Z")
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_TASK_PATH = re.compile(r"tasks/(?:[^/]+\.md|README\.md)\Z")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
T = TypeVar("T")


class AssistStoreError(RuntimeError):
    """Content-free error from the private assist state blob."""


def empty_assist_state() -> dict[str, Any]:
    return {
        "version": 1,
        "threads": {},
        "requests": {},
        "operations": {},
        "ledger": {
            "month": "",
            "spent_micro_eur": 0,
            "reserved_micro_eur": 0,
            "day": "",
            "daily_spent_micro_eur": 0,
            "daily_reserved_micro_eur": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "month_history": {},
            "day_history": {},
        },
        "telegram": {"bindings": {}, "events": {}, "latest": None},
    }


def _valid_day(value: object) -> bool:
    if not isinstance(value, str) or not _DAY.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def validate_assist_state(state: object) -> None:
    if not isinstance(state, dict) or set(state) != set(empty_assist_state()):
        raise AssistStoreError("assist_state_invalid")
    if type(state["version"]) is not int or state["version"] != 1:
        raise AssistStoreError("assist_state_invalid")

    threads = state["threads"]
    if not isinstance(threads, dict) or len(threads) > MAX_THREADS:
        raise AssistStoreError("assist_state_capacity")
    for identifier, thread in threads.items():
        if (
            not isinstance(identifier, str) or not _UUID.fullmatch(identifier)
            or             not isinstance(thread, dict)
            or set(thread) - {"created_on", "last_on", "turns", "task_refs"}
            or not {"created_on", "last_on", "turns"} <= thread.keys()
            or not _valid_day(thread["created_on"]) or not _valid_day(thread["last_on"])
            or not isinstance(thread["turns"], list) or len(thread["turns"]) > MAX_TURNS
            or ("task_refs" in thread and (
                not isinstance(thread["task_refs"], dict) or len(thread["task_refs"]) > 12
            ))
        ):
            raise AssistStoreError("assist_state_invalid")
        for path, revision in thread.get("task_refs", {}).items():
            if (
                not isinstance(path, str) or not _TASK_PATH.fullmatch(path)
                or not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision)
            ):
                raise AssistStoreError("assist_state_invalid")
        for turn in thread["turns"]:
            if (
                not isinstance(turn, dict) or set(turn) != {"user", "assistant"}
                or not isinstance(turn["user"], str) or len(turn["user"]) > 4000
                or not isinstance(turn["assistant"], str) or len(turn["assistant"]) > 1200
            ):
                raise AssistStoreError("assist_state_invalid")

    requests = state["requests"]
    if not isinstance(requests, dict) or len(requests) > MAX_REQUESTS:
        raise AssistStoreError("assist_state_capacity")
    for key, item in requests.items():
        if (
            not isinstance(key, str) or not _SHA.fullmatch(key)
            or not isinstance(item, dict)
            or set(item) - {
                "digest", "status", "created_on", "thread_id", "response", "reserved_micro_eur",
                "accounting_month", "accounting_day",
            }
            or not {"digest", "status", "created_on", "thread_id", "response", "reserved_micro_eur"} <= item.keys()
            or not isinstance(item["digest"], str) or not _SHA.fullmatch(item["digest"])
            or item["status"] not in {"claimed", "recorded", "uncertain"}
            or not _valid_day(item["created_on"])
            or not isinstance(item["thread_id"], str) or not _UUID.fullmatch(item["thread_id"])
            or type(item["reserved_micro_eur"]) is not int or item["reserved_micro_eur"] < 0
            or ("accounting_month" in item and (
                not isinstance(item["accounting_month"], str)
                or not re.fullmatch(r"\d{4}-\d{2}", item["accounting_month"])
            ))
            or ("accounting_day" in item and not _valid_day(item["accounting_day"]))
            or (item["response"] is not None and not isinstance(item["response"], dict))
        ):
            raise AssistStoreError("assist_state_invalid")
        if item["response"] is not None and len(_json_bytes(item["response"])) > 8000:
            raise AssistStoreError("assist_state_capacity")

    operations = state["operations"]
    if not isinstance(operations, dict) or len(operations) > MAX_OPERATIONS:
        raise AssistStoreError("assist_state_capacity")
    for identifier, item in operations.items():
        if (
            not isinstance(identifier, str) or not _TASK_ID.fullmatch(identifier)
            or not isinstance(item, dict) or set(item) - {
                "path", "kind", "source_revision", "applied_revision", "changes", "original",
                "created_on", "thread_id", "receipt_id",
            }
            or not {"path", "kind", "source_revision", "created_on", "thread_id", "receipt_id"} <= item.keys()
            or not isinstance(item["path"], str) or not item["path"].startswith("tasks/")
            or item["kind"] not in {"capture_task", "refine_task", "edit_task", "close_task"}
            or not isinstance(item["source_revision"], str) or not re.fullmatch(r"[0-9a-f]{40}", item["source_revision"])
            or ("applied_revision" in item and (
                not isinstance(item["applied_revision"], str)
                or not re.fullmatch(r"[0-9a-f]{40}", item["applied_revision"])
            ))
            or not _valid_day(item["created_on"])
            or not isinstance(item["thread_id"], str) or not _UUID.fullmatch(item["thread_id"])
            or item["receipt_id"] != identifier
            or ("changes" in item and not isinstance(item["changes"], dict))
            or ("original" in item and not isinstance(item["original"], dict))
        ):
            raise AssistStoreError("assist_state_invalid")
        if len(_json_bytes(item)) > 10000:
            raise AssistStoreError("assist_state_capacity")

    ledger = state["ledger"]
    if (
        not isinstance(ledger, dict)
        or set(ledger) - set(empty_assist_state()["ledger"])
        or not set(empty_assist_state()["ledger"]) - {"month_history", "day_history"} <= ledger.keys()
        or not isinstance(ledger["month"], str) or (ledger["month"] and not re.fullmatch(r"\d{4}-\d{2}", ledger["month"]))
        or not isinstance(ledger["day"], str) or (ledger["day"] and not _valid_day(ledger["day"]))
        or any(
            type(ledger[key]) is not int or ledger[key] < 0 for key in (
                "spent_micro_eur", "reserved_micro_eur", "daily_spent_micro_eur", "daily_reserved_micro_eur",
                "input_tokens", "output_tokens",
            )
        )
    ):
        raise AssistStoreError("assist_state_invalid")
    month_history = ledger.get("month_history", {})
    day_history = ledger.get("day_history", {})
    if (
        not isinstance(month_history, dict) or len(month_history) > MAX_REQUESTS
        or any(
            not isinstance(month, str) or not re.fullmatch(r"\d{4}-\d{2}", month)
            or not isinstance(bucket, dict)
            or set(bucket) != {"spent_micro_eur", "reserved_micro_eur", "input_tokens", "output_tokens"}
            or any(type(bucket[key]) is not int or bucket[key] < 0 for key in bucket)
            for month, bucket in month_history.items()
        )
        or not isinstance(day_history, dict) or len(day_history) > MAX_REQUESTS
        or any(
            not _valid_day(day) or not isinstance(bucket, dict)
            or set(bucket) != {"daily_spent_micro_eur", "daily_reserved_micro_eur"}
            or any(type(bucket[key]) is not int or bucket[key] < 0 for key in bucket)
            for day, bucket in day_history.items()
        )
    ):
        raise AssistStoreError("assist_state_invalid")

    telegram = state["telegram"]
    if not isinstance(telegram, dict) or set(telegram) != {"bindings", "events", "latest"}:
        raise AssistStoreError("assist_state_invalid")
    bindings, events = telegram["bindings"], telegram["events"]
    if not isinstance(bindings, dict) or len(bindings) > MAX_TELEGRAM_BINDINGS:
        raise AssistStoreError("assist_state_capacity")
    for message_id, item in bindings.items():
        if (
            not isinstance(message_id, str) or not re.fullmatch(r"[1-9]\d*", message_id)
            or not isinstance(item, dict)
            or set(item) - {"thread_id", "created_on", "task", "proposal_id", "approval_digest", "receipt_id"}
            or not {"thread_id", "created_on"} <= item.keys()
            or not isinstance(item["thread_id"], str) or not _UUID.fullmatch(item["thread_id"])
            or not _valid_day(item["created_on"])
            or ("task" in item and (
                not isinstance(item["task"], dict) or set(item["task"]) != {"path", "revision"}
                or not isinstance(item["task"]["path"], str) or not _TASK_PATH.fullmatch(item["task"]["path"])
                or not isinstance(item["task"]["revision"], str)
                or not re.fullmatch(r"[0-9a-f]{40}", item["task"]["revision"])
            ))
            or ("proposal_id" in item and (
                not isinstance(item["proposal_id"], str) or not _TASK_ID.fullmatch(item["proposal_id"])
            ))
            or ("approval_digest" in item and (
                not isinstance(item["approval_digest"], str) or not _SHA.fullmatch(item["approval_digest"])
            ))
            or ("receipt_id" in item and (
                not isinstance(item["receipt_id"], str) or not _TASK_ID.fullmatch(item["receipt_id"])
            ))
            or ("approval_digest" in item and "proposal_id" not in item)
        ):
            raise AssistStoreError("assist_state_invalid")
    if not isinstance(events, dict) or len(events) > MAX_TELEGRAM_EVENTS:
        raise AssistStoreError("assist_state_capacity")
    for key, item in events.items():
        if (
            not isinstance(key, str) or not _SHA.fullmatch(key)
            or not isinstance(item, dict)
            or set(item) != {"digest", "status", "created_on"}
            or not {"digest", "status", "created_on"} <= item.keys()
            or not isinstance(item["digest"], str) or not _SHA.fullmatch(item["digest"])
            or item["status"] not in {"claimed", "sent", "uncertain"}
            or not _valid_day(item["created_on"])
        ):
            raise AssistStoreError("assist_state_invalid")
    latest = telegram["latest"]
    if latest is not None and (
        not isinstance(latest, dict) or set(latest) - {
            "thread_id", "created_on", "task", "proposal_id", "approval_digest", "receipt_id",
        }
        or not {"thread_id", "created_on"} <= latest.keys()
        or not isinstance(latest["thread_id"], str) or not _UUID.fullmatch(latest["thread_id"])
        or not _valid_day(latest["created_on"])
        or ("task" in latest and (
            not isinstance(latest["task"], dict) or set(latest["task"]) != {"path", "revision"}
            or not isinstance(latest["task"]["path"], str) or not _TASK_PATH.fullmatch(latest["task"]["path"])
            or not isinstance(latest["task"]["revision"], str)
            or not re.fullmatch(r"[0-9a-f]{40}", latest["task"]["revision"])
        ))
        or ("proposal_id" in latest and (
            not isinstance(latest["proposal_id"], str) or not _TASK_ID.fullmatch(latest["proposal_id"])
        ))
        or ("approval_digest" in latest and (
            not isinstance(latest["approval_digest"], str) or not _SHA.fullmatch(latest["approval_digest"])
        ))
        or ("receipt_id" in latest and (
            not isinstance(latest["receipt_id"], str) or not _TASK_ID.fullmatch(latest["receipt_id"])
        ))
    ):
        raise AssistStoreError("assist_state_invalid")

    if len(_json_bytes(state)) > MAX_STATE_BYTES:
        raise AssistStoreError("assist_state_capacity")


def _json_bytes(value: object) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise AssistStoreError("assist_state_invalid") from None


def prune_assist_state(state: dict[str, Any], today: date) -> None:
    cutoff = today - timedelta(days=RETENTION_DAYS)
    for identifier, thread in list(state["threads"].items()):
        if date.fromisoformat(thread["last_on"]) <= cutoff:
            del state["threads"][identifier]
    for key, item in list(state["requests"].items()):
        if item["status"] == "recorded" and date.fromisoformat(item["created_on"]) <= cutoff:
            del state["requests"][key]
    for key, item in list(state["operations"].items()):
        if date.fromisoformat(item["created_on"]) <= cutoff:
            del state["operations"][key]
    telegram = state["telegram"]
    for key, item in list(telegram["bindings"].items()):
        if date.fromisoformat(item["created_on"]) <= cutoff:
            del telegram["bindings"][key]
    for key, item in list(telegram["events"].items()):
        if item["status"] != "claimed" and date.fromisoformat(item["created_on"]) <= cutoff:
            del telegram["events"][key]
    latest = telegram["latest"]
    if latest and date.fromisoformat(latest["created_on"]) <= cutoff:
        telegram["latest"] = None


def request_key(identifier: str) -> str:
    return hashlib.sha256(("assist-request:" + identifier).encode("ascii")).hexdigest()


def telegram_event_key(identifier: str) -> str:
    return hashlib.sha256(("assist-telegram:" + identifier).encode("utf-8")).hexdigest()


class AssistStore:
    """CAS-backed sidecar in the same private container as briefing state."""

    def __init__(self, container_or_blob: Any) -> None:
        if hasattr(container_or_blob, "get_blob_client"):
            self._blob = container_or_blob.get_blob_client(ASSIST_STATE_BLOB)
        else:
            self._blob = container_or_blob

    @classmethod
    def for_task_service(cls, service: Any) -> "AssistStore":
        try:
            container = service.store._blob._get_container_client()
            return cls(container.get_blob_client(ASSIST_STATE_BLOB))
        except Exception:
            raise AssistStoreError("assist_state_client_unavailable") from None

    def _load(self) -> tuple[dict[str, Any], str | None]:
        checkpoint()
        try:
            download = self._blob.download_blob(logging_enable=False, max_concurrency=1, **sdk_timeouts())
        except ResourceNotFoundError as error:
            if getattr(error, "error_code", None) in {None, "BlobNotFound"}:
                return empty_assist_state(), None
            raise AssistStoreError("assist_state_read_unavailable") from None
        except AzureError:
            raise AssistStoreError("assist_state_read_unavailable") from None
        etag = getattr(download.properties, "etag", None)
        size = getattr(download.properties, "size", None)
        if not isinstance(etag, str) or not etag or type(size) is not int or not 0 <= size <= MAX_STATE_BYTES:
            raise AssistStoreError("assist_state_invalid")
        try:
            payload = download.readall()
            if not isinstance(payload, bytes) or len(payload) != size:
                raise AssistStoreError("assist_state_invalid")
            state = json.loads(payload.decode("utf-8"))
            validate_assist_state(state)
            return state, etag
        except AssistStoreError:
            raise
        except (ValueError, UnicodeError, RecursionError):
            raise AssistStoreError("assist_state_invalid") from None
        except AzureError:
            raise AssistStoreError("assist_state_read_unavailable") from None

    def read(self) -> dict[str, Any]:
        return self._load()[0]

    def update(self, mutate: Callable[[dict[str, Any]], T]) -> T:
        for _ in range(4):
            checkpoint()
            state, etag = self._load()
            result = mutate(state)
            validate_assist_state(state)
            payload = _json_bytes(state)
            if len(payload) > MAX_STATE_BYTES:
                raise AssistStoreError("assist_state_capacity")
            try:
                if etag is None:
                    self._blob.upload_blob(payload, overwrite=False, logging_enable=False, **sdk_timeouts())
                else:
                    self._blob.upload_blob(
                        payload, overwrite=True, etag=etag,
                        match_condition=MatchConditions.IfNotModified,
                        logging_enable=False, **sdk_timeouts(),
                    )
                checkpoint()
                return result
            except ResourceModifiedError:
                continue
            except ResourceExistsError:
                if etag is None:
                    continue
                raise AssistStoreError("assist_state_write_unavailable") from None
            except AzureError:
                raise AssistStoreError("assist_state_write_unavailable") from None
        raise AssistStoreError("assist_state_conflict_retry_exhausted")

    def claim_telegram_event(self, event_id: str, digest: str, today: date) -> str:
        key = telegram_event_key(event_id)

        def claim(state: dict[str, Any]) -> str:
            prune_assist_state(state, today)
            previous = state["telegram"]["events"].get(key)
            if previous:
                return "conflict" if previous["digest"] != digest else previous["status"]
            if len(state["telegram"]["events"]) >= MAX_TELEGRAM_EVENTS:
                raise AssistStoreError("assist_telegram_capacity")
            state["telegram"]["events"][key] = {
                "digest": digest, "status": "claimed", "created_on": today.isoformat(),
            }
            return "new"

        return self.update(claim)

    def finish_telegram_event(self, event_id: str, status: str, *, receipt_id: str | None = None) -> None:
        key = telegram_event_key(event_id)
        if status not in {"sent", "uncertain"}:
            raise AssistStoreError("assist_telegram_event_invalid")

        def finish(state: dict[str, Any]) -> None:
            item = state["telegram"]["events"].get(key)
            if not item or item["status"] != "claimed":
                raise AssistStoreError("assist_telegram_event_invalid")
            item["status"] = status

        self.update(finish)

    def bind_telegram_message(
        self, message_id: int, thread_id: str, today: date, *,
        task: dict[str, str] | None = None,
        proposal_id: str | None = None, approval_digest: str | None = None,
        receipt_id: str | None = None,
    ) -> None:
        if type(message_id) is not int or message_id <= 0:
            raise AssistStoreError("assist_telegram_binding_invalid")

        def bind(state: dict[str, Any]) -> None:
            prune_assist_state(state, today)
            key = str(message_id)
            value = {"thread_id": thread_id, "created_on": today.isoformat()}
            if task is not None:
                value["task"] = copy.deepcopy(task)
            if proposal_id is not None:
                value["proposal_id"] = proposal_id
            if approval_digest is not None:
                value["approval_digest"] = approval_digest
            if receipt_id is not None:
                value["receipt_id"] = receipt_id
            previous = state["telegram"]["bindings"].get(key)
            if previous and previous != value:
                raise AssistStoreError("assist_telegram_binding_conflict")
            if not previous and len(state["telegram"]["bindings"]) >= MAX_TELEGRAM_BINDINGS:
                raise AssistStoreError("assist_telegram_capacity")
            state["telegram"]["bindings"][key] = value

        self.update(bind)

    def set_latest_telegram_proposal(
        self, thread_id: str, proposal_id: str, approval_digest: str, today: date,
        *, task: dict[str, str] | None = None, receipt_id: str | None = None,
    ) -> None:
        def save(state: dict[str, Any]) -> None:
            value = {
                "thread_id": thread_id, "proposal_id": proposal_id,
                "approval_digest": approval_digest, "created_on": today.isoformat(),
            }
            if task is not None:
                value["task"] = copy.deepcopy(task)
            if receipt_id is not None:
                value["receipt_id"] = receipt_id
            state["telegram"]["latest"] = value

        self.update(save)

    def thread_for_message(self, message_id: int) -> dict[str, Any] | None:
        return copy.deepcopy(self.read()["telegram"]["bindings"].get(str(message_id)))
