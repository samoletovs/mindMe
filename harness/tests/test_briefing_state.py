"""Synthetic Azure ETag races and privacy/decision boundaries; no services."""

from __future__ import annotations

import copy
import json
import traceback
from collections.abc import Callable
from datetime import date
from typing import Any, cast

import pytest
from azure.core import MatchConditions
from azure.core.exceptions import (
    AzureError,
    ClientAuthenticationError,
    HttpResponseError,
    ResourceExistsError,
    ResourceModifiedError,
    ResourceNotFoundError,
    ServiceRequestError,
    ServiceResponseError,
)
from azure.storage.blob import BlobProperties, ContainerClient

from briefing_state import (
    MAX_CAS_ATTEMPTS,
    MAX_STATE_BYTES,
    RECORD_CAPS,
    STATE_BLOB,
    BriefingStore,
    StateError,
    empty_state,
    is_expired,
    parse_reply,
    prune_state,
)

TODAY = date(2026, 9, 13)
PATH = "ideas/synthetic-experiment.md"
PRIVATE_MARKER = "synthetic-private-content"


def proposal(identifier: str = "proposal-1", **changes: Any) -> dict[str, Any]:
    return {
        "id": identifier, "kind": "create_task", "text": "Try a synthetic exercise.",
        "source_path": PATH, "source_revision": "a" * 40, "source_digest": "b" * 64,
        "status": "pending", "created_on": "2026-09-01", "expires_on": "2026-09-15",
        "message_ids": [], "action": {"kind": "create_task", "text": "Try an exercise."},
        "why": "Test an idea before expanding it.", "parent_extra": {"retained": True},
        **changes,
    }


def memory(identifier: str = "memory-1", **changes: Any) -> dict[str, Any]:
    return {
        "id": identifier, "kind": "correction", "text": "This exercise is already familiar.",
        "source_path": PATH, "proposal_id": "proposal-1", "created_on": "2026-09-01",
        "last_used_on": "2026-09-01", "active": True, **changes,
    }


def initial_state() -> dict[str, Any]:
    state = empty_state()
    state["proposals"]["proposal-1"] = proposal()
    return state


def not_found(code: str = "BlobNotFound") -> ResourceNotFoundError:
    error = ResourceNotFoundError(PRIVATE_MARKER)
    error.error_code = code
    return error


class FakeDownload:
    def __init__(
        self, payload: bytes, etag: str, error: AzureError | None,
        advertised_size: int | None = None,
    ) -> None:
        self.payload = payload
        self.error = error
        self.properties = BlobProperties(**{
            "ETag": etag, "Content-Length": len(payload) if advertised_size is None else advertised_size,
        })
        self.reads = 0

    def readall(self) -> bytes:
        self.reads += 1
        if self.error is not None:
            raise self.error
        return self.payload


class FakeBlob:
    def __init__(self, state: dict[str, Any] | None = None) -> None:
        self.payload = json.dumps(state).encode() if state is not None else None
        self.version = 1
        self.read_error: AzureError | None = None
        self.stream_error: AzureError | None = None
        self.write_error: AzureError | None = None
        self.before_upload: Callable[[FakeBlob], None] | None = None
        self.after_snapshot: Callable[[FakeBlob], None] | None = None
        self.always_conflict = False
        self.advertised_size: int | None = None
        self.reads = 0
        self.writes: list[dict[str, Any]] = []
        self.download: FakeDownload | None = None

    @property
    def etag(self) -> str:
        return f'"version-{self.version}"'

    def replace(self, state: dict[str, Any]) -> None:
        self.payload = json.dumps(state).encode()
        self.version += 1

    def saved(self) -> dict[str, Any]:
        assert self.payload is not None
        return json.loads(self.payload)

    def download_blob(self, **kwargs: Any) -> FakeDownload:
        assert kwargs["logging_enable"] is False
        self.reads += 1
        if self.read_error is not None:
            raise self.read_error
        if self.payload is None:
            raise not_found()
        self.download = FakeDownload(self.payload, self.etag, self.stream_error, self.advertised_size)
        if self.after_snapshot is not None:
            callback, self.after_snapshot = self.after_snapshot, None
            callback(self)
        return self.download

    def upload_blob(self, payload: bytes, **kwargs: Any) -> None:
        assert kwargs["logging_enable"] is False
        self.writes.append(kwargs)
        if self.before_upload is not None:
            callback, self.before_upload = self.before_upload, None
            callback(self)
        if self.write_error is not None:
            raise self.write_error
        if self.always_conflict:
            raise ResourceModifiedError(PRIVATE_MARKER)
        if not kwargs["overwrite"]:
            assert "etag" not in kwargs and "match_condition" not in kwargs
            if self.payload is not None:
                raise ResourceExistsError(PRIVATE_MARKER)
        else:
            assert kwargs["match_condition"] is MatchConditions.IfNotModified
            if kwargs["etag"] != self.etag:
                raise ResourceModifiedError(PRIVATE_MARKER)
        self.payload = payload
        self.version += 1

    def get_blob_properties(self, **kwargs: Any) -> None:
        raise AssertionError("A separate HEAD would not identify the downloaded snapshot")


class FakeContainer:
    def __init__(self, blob: FakeBlob) -> None:
        self.blob = blob

    def get_blob_client(self, name: str) -> FakeBlob:
        assert name == STATE_BLOB
        return self.blob


def store_for(blob: FakeBlob) -> BriefingStore:
    return BriefingStore(cast(ContainerClient, FakeContainer(blob)))


def test_empty_state_has_exact_independent_collections() -> None:
    first, second = empty_state(), empty_state()
    assert set(first) == {
        "version", "proposals", "messages", "memories", "fingerprints", "deliveries", "last_delivered", "knowledge",
    }
    first["proposals"]["one"] = {}
    assert second["proposals"] == {}
    assert second["version"] == 1
    assert second["last_delivered"] is None


def test_missing_blob_reads_empty_without_writing() -> None:
    blob = FakeBlob()
    assert store_for(blob).read() == empty_state()
    assert not blob.writes


def test_missing_blob_is_created_only_after_callback() -> None:
    blob = FakeBlob()

    def mutate(state: dict[str, Any]) -> str:
        assert not blob.writes
        state["proposals"]["proposal-1"] = proposal()
        return "saved"

    assert store_for(blob).update(mutate) == "saved"
    assert blob.writes[0]["overwrite"] is False
    assert blob.saved()["proposals"]["proposal-1"]["parent_extra"] == {"retained": True}


@pytest.mark.parametrize("error", [
    ClientAuthenticationError(PRIVATE_MARKER),
    HttpResponseError(PRIVATE_MARKER),
    ServiceRequestError(PRIVATE_MARKER),
    not_found("ContainerNotFound"),
])
def test_unavailable_state_is_never_treated_as_empty(error: AzureError) -> None:
    blob = FakeBlob()
    blob.read_error = error
    with pytest.raises(StateError, match="state_read_unavailable") as caught:
        store_for(blob).read()
    assert PRIVATE_MARKER not in "".join(traceback.format_exception(caught.value))
    assert not blob.writes


def test_stream_read_failure_does_not_return_empty_or_write() -> None:
    blob = FakeBlob(initial_state())
    blob.stream_error = ServiceResponseError(PRIVATE_MARKER)
    with pytest.raises(StateError, match="state_read_unavailable"):
        store_for(blob).update(lambda state: state["proposals"].clear())
    assert not blob.writes


@pytest.mark.parametrize("error", [
    ClientAuthenticationError(PRIVATE_MARKER),
    ServiceResponseError(PRIVATE_MARKER),
    HttpResponseError(PRIVATE_MARKER),
    not_found(),
    ResourceExistsError(PRIVATE_MARKER),
])
def test_write_failure_never_returns_a_successful_claim(error: AzureError) -> None:
    blob = FakeBlob(initial_state())
    blob.write_error = error
    with pytest.raises(StateError, match="state_write_unavailable") as caught:
        store_for(blob).update(lambda state: state["proposals"]["proposal-1"].update(status="executing"))
    assert blob.saved()["proposals"]["proposal-1"]["status"] == "pending"
    assert len(blob.writes) == 1
    assert PRIVATE_MARKER not in "".join(traceback.format_exception(caught.value))


def test_caller_validation_failure_is_not_caught_or_persisted() -> None:
    blob = FakeBlob(initial_state())

    def reject(state: dict[str, Any]) -> None:
        state["proposals"]["proposal-1"]["status"] = "executing"
        raise ValueError("owner_rejected")

    with pytest.raises(ValueError, match="owner_rejected"):
        store_for(blob).update(reject)
    assert not blob.writes
    assert blob.saved()["proposals"]["proposal-1"]["status"] == "pending"


def test_lost_write_response_never_acknowledges_or_claims_the_action_twice() -> None:
    blob = FakeBlob(initial_state())
    store = store_for(blob)

    def committed_without_response(target: FakeBlob) -> None:
        state = target.saved()
        state["proposals"]["proposal-1"]["status"] = "executing"
        target.replace(state)

    def claim(state: dict[str, Any]) -> bool:
        record = state["proposals"]["proposal-1"]
        if record["status"] != "pending":
            return False
        record["status"] = "executing"
        return True

    blob.before_upload = committed_without_response
    blob.write_error = ServiceResponseError(PRIVATE_MARKER)
    with pytest.raises(StateError, match="state_write_unavailable"):
        store.update(claim)
    blob.write_error = None
    assert store.update(claim) is False
    assert blob.saved()["proposals"]["proposal-1"]["status"] == "executing"


def record_feedback(blob: FakeBlob) -> None:
    state = blob.saved()
    state["proposals"]["proposal-1"]["status"] = "corrected"
    state["proposals"]["proposal-1"]["decision_receipt"] = {"confirmed": True}
    state["memories"]["memory-1"] = memory()
    blob.replace(state)


def test_cas_retries_against_fresh_feedback_without_losing_parent_fields() -> None:
    blob = FakeBlob(initial_state())
    blob.before_upload = record_feedback
    observed: list[str] = []

    def bind(state: dict[str, Any]) -> str:
        observed.append(state["proposals"]["proposal-1"]["status"])
        state["messages"]["41"] = "proposal-1"
        return observed[-1]

    assert store_for(blob).update(bind) == "corrected"
    assert observed == ["pending", "corrected"]
    saved = blob.saved()
    assert saved["memories"]["memory-1"] == memory()
    assert saved["proposals"]["proposal-1"]["decision_receipt"] == {"confirmed": True}
    assert saved["proposals"]["proposal-1"]["parent_extra"] == {"retained": True}
    assert saved["messages"]["41"] == "proposal-1"
    assert len(blob.writes) == 2
    assert blob.writes[0]["etag"] != blob.writes[1]["etag"]


def test_stale_replacement_is_rechecked_after_concurrent_correction() -> None:
    blob = FakeBlob(initial_state())
    store = store_for(blob)
    stale = copy.deepcopy(store.read()["proposals"]["proposal-1"])
    stale["text"] = "A proposed replacement."
    blob.before_upload = record_feedback

    def replace_pending(state: dict[str, Any]) -> bool:
        if state["proposals"]["proposal-1"]["status"] != "pending":
            return False
        state["proposals"]["proposal-1"] = copy.deepcopy(stale)
        return True

    assert store.update(replace_pending) is False
    assert blob.saved()["proposals"]["proposal-1"]["status"] == "corrected"
    assert blob.saved()["memories"]["memory-1"]["text"] == memory()["text"]


def test_etag_belongs_to_same_download_when_remote_changes_before_readall() -> None:
    blob = FakeBlob(initial_state())
    blob.after_snapshot = record_feedback
    store_for(blob).update(lambda state: state["messages"].update({"42": "proposal-1"}))
    assert len(blob.writes) == 2
    assert blob.saved()["memories"]["memory-1"] == memory()


def test_concurrent_create_preserves_winners_feedback() -> None:
    blob = FakeBlob()

    def other_creator(target: FakeBlob) -> None:
        target.replace(initial_state())
        record_feedback(target)

    blob.before_upload = other_creator
    store_for(blob).update(lambda state: state["fingerprints"].update({PATH: "new-digest"}))
    assert blob.writes[0]["overwrite"] is False
    assert blob.writes[1]["overwrite"] is True
    assert blob.saved()["proposals"]["proposal-1"]["status"] == "corrected"
    assert blob.saved()["memories"]["memory-1"] == memory()


def test_contention_has_bounded_retries_and_no_success_fallback() -> None:
    blob = FakeBlob(initial_state())
    blob.always_conflict = True
    with pytest.raises(StateError, match="state_conflict_retry_exhausted"):
        store_for(blob).update(lambda state: state["proposals"]["proposal-1"].update(status="executing"))
    assert len(blob.writes) == MAX_CAS_ATTEMPTS
    assert blob.reads == MAX_CAS_ATTEMPTS
    assert blob.saved()["proposals"]["proposal-1"]["status"] == "pending"


@pytest.mark.parametrize("key", [key for key in empty_state() if key != "knowledge"])
def test_missing_root_key_is_rejected_without_resetting_state(key: str) -> None:
    state = empty_state()
    del state[key]
    blob = FakeBlob(state)
    with pytest.raises(StateError):
        store_for(blob).read()
    assert not blob.writes


@pytest.mark.parametrize("payload", [
    b"", b"not json", b"\xff", b"null", b"[]", b'{"version":1,"version":1}',
    json.dumps({**empty_state(), "version": True}).encode(),
    json.dumps({**empty_state(), "version": 2}).encode(),
    json.dumps({**empty_state(), "last_delivered": []}).encode(),
    json.dumps({**empty_state(), "extra": {}}).encode(),
    json.dumps({**empty_state(), "last_delivered": {"value": float("nan")}}).encode(),
])
def test_malformed_json_or_schema_surfaces_a_safe_state_error(payload: bytes) -> None:
    blob = FakeBlob()
    blob.payload = payload
    with pytest.raises(StateError):
        store_for(blob).read()


@pytest.mark.parametrize("checkpoint", [None, {}, {"date": "2026-09-13", "id": "delivery-1", "revision": None}])
def test_optional_checkpoint_value_accepts_none_or_dict(checkpoint: dict[str, Any] | None) -> None:
    state = {**empty_state(), "last_delivered": checkpoint}
    assert store_for(FakeBlob(state)).read() == state


@pytest.mark.parametrize("field", [
    "id", "kind", "text", "source_path", "source_revision", "source_digest",
    "status", "created_on", "expires_on", "message_ids", "action",
])
def test_every_proposal_required_field_is_checked(field: str) -> None:
    state = initial_state()
    del state["proposals"]["proposal-1"][field]
    with pytest.raises(StateError):
        store_for(FakeBlob(state)).read()


@pytest.mark.parametrize("field,value", [
    ("action", []), ("source_path", None), ("message_ids", [True]),
    ("status", []), ("expires_on", "2026-02-30"), ("created_on", "20260901"),
    ("expires_on", "2026-08-01"),
])
def test_invalid_proposal_values_are_rejected(field: str, value: Any) -> None:
    state = initial_state()
    state["proposals"]["proposal-1"][field] = value
    with pytest.raises(StateError):
        store_for(FakeBlob(state)).read()


@pytest.mark.parametrize("changes", [
    {"active": "true"}, {"id": "different"}, {"text": "x" * 281},
    {"kind": "event"}, {"text": "password=synthetic-value"}, {"last_used_on": "yesterday"},
])
def test_invalid_or_unsafe_memory_is_rejected(changes: dict[str, Any]) -> None:
    state = initial_state()
    state["memories"]["memory-1"] = memory(**changes)
    with pytest.raises(StateError):
        store_for(FakeBlob(state)).read()


def test_shared_memory_object_cannot_be_stored_under_a_second_identifier() -> None:
    blob = FakeBlob(initial_state())

    def alias(current: dict[str, Any]) -> None:
        record = memory()
        current["memories"] = {"memory-1": record, "incorrect-key": record}

    with pytest.raises(StateError, match="invalid_memory_identifier"):
        store_for(blob).update(alias)
    assert not blob.writes


def test_large_json_exponent_is_not_accepted_as_an_infinite_number() -> None:
    blob = FakeBlob()
    blob.payload = json.dumps({**empty_state(), "last_delivered": {"value": "EXPONENT"}}).replace(
        '"EXPONENT"', "1e999",
    ).encode()
    with pytest.raises(StateError, match="invalid_state_encoding"):
        store_for(blob).read()


def populated_collection(key: str, count: int) -> dict[str, Any]:
    if key == "proposals":
        return {f"proposal-{index}": proposal(f"proposal-{index}") for index in range(count)}
    if key == "memories":
        return {f"memory-{index}": memory(f"memory-{index}") for index in range(count)}
    if key == "messages":
        return {str(index + 1): "proposal-1" for index in range(count)}
    if key == "fingerprints":
        return {f"ideas/example-{index}.md": "f" * 64 for index in range(count)}
    return {
        f"delivery-{index}": {"status": "sent", "date": TODAY.isoformat(), "message_ids": []}
        for index in range(count)
    }


@pytest.mark.parametrize("key", list(RECORD_CAPS))
def test_record_cap_is_inclusive_and_exceeding_it_never_evicts(key: str) -> None:
    state = initial_state()
    state[key] = populated_collection(key, RECORD_CAPS[key])
    blob = FakeBlob(state)
    store = store_for(blob)
    assert store.read() == state

    def overflow(current: dict[str, Any]) -> None:
        current[key] = populated_collection(key, RECORD_CAPS[key] + 1)

    with pytest.raises(StateError, match=f"state_{key}_capacity"):
        store.update(overflow)
    assert not blob.writes
    assert blob.saved() == state


def test_large_advertised_blob_is_rejected_before_reading_body() -> None:
    blob = FakeBlob(empty_state())
    blob.advertised_size = MAX_STATE_BYTES + 1
    with pytest.raises(StateError, match="state_payload_capacity"):
        store_for(blob).read()
    assert blob.download is not None
    assert blob.download.reads == 0


def test_actual_payload_size_is_checked_even_if_metadata_understates_it() -> None:
    blob = FakeBlob()
    blob.payload = b" " * (MAX_STATE_BYTES + 1)
    blob.advertised_size = 1
    with pytest.raises(StateError, match="state_payload_capacity"):
        store_for(blob).read()


def test_truncated_body_is_not_accepted_as_complete_state() -> None:
    blob = FakeBlob(empty_state())
    assert blob.payload is not None
    blob.advertised_size = len(blob.payload) + 2
    with pytest.raises(StateError, match="invalid_state_length"):
        store_for(blob).read()


def test_payload_cap_uses_encoded_bytes_not_character_count() -> None:
    blob = FakeBlob(initial_state())
    with pytest.raises(StateError, match="state_payload_capacity"):
        store_for(blob).update(
            lambda state: state["proposals"]["proposal-1"].update(parent_extra="界" * (MAX_STATE_BYTES // 3))
        )
    assert not blob.writes


def test_payload_at_exact_byte_limit_can_be_read_and_conditionally_written() -> None:
    state = {**empty_state(), "last_delivered": {"padding": ""}}
    base = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode()
    state["last_delivered"]["padding"] = "x" * (MAX_STATE_BYTES - len(base))
    blob = FakeBlob()
    blob.payload = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(blob.payload) == MAX_STATE_BYTES
    store = store_for(blob)
    assert store.read() == state
    store.update(lambda current: None)
    assert blob.payload is not None and len(blob.payload) == MAX_STATE_BYTES


def test_encoding_failure_cannot_fall_back_to_success() -> None:
    blob = FakeBlob(initial_state())
    with pytest.raises(StateError, match="invalid_state_encoding"):
        store_for(blob).update(lambda state: state["proposals"]["proposal-1"].update(extra={"not-json"}))
    assert not blob.writes


@pytest.mark.parametrize("text,intent", [
    ("yes", "approve"), (" YES! ", "approve"), ("do it", "approve"), ("go ahead", "approve"),
    ("approve", "approve"), ("no", "decline"), ("skip", "decline"), ("not interested", "decline"),
    ("decline", "decline"), ("done", "done"), ("already done", "done"), ("completed", "done"),
    ("why?", "explain"), ("explain", "explain"), ("explain more", "explain"),
])
def test_explicit_common_replies_are_deterministic_without_binding(text: str, intent: str) -> None:
    assert parse_reply(text, TODAY) == {"intent": intent}


@pytest.mark.parametrize("text", [
    "yes but not now", "no, approve instead", "do it and buy something", "remember my whole transcript",
    "yesterday was productive", "", "I mentioned yes in my notes", "next week or tomorrow",
])
def test_free_form_or_compound_replies_never_guess_authority(text: str) -> None:
    assert parse_reply(text, TODAY)["intent"] == "unknown"


@pytest.mark.parametrize("text,expected", [
    ("snooze 2026-09-20", "2026-09-20"), ("later 2026-10-03", "2026-10-03"),
    ("remind me 2026-09-14", "2026-09-14"), ("tomorrow", "2026-09-14"),
    ("next week", "2026-09-20"), ("snooze tomorrow", "2026-09-14"),
    ("remind me next week", "2026-09-20"), ("later tomorrow!", "2026-09-14"),
    ("snooze 2026-09-13", "2026-09-13"),
])
def test_unambiguous_snoozes_resolve_to_exact_dates(text: str, expected: str) -> None:
    assert parse_reply(text, TODAY) == {"intent": "snooze", "review_on": expected}


@pytest.mark.parametrize("text", [
    "later", "next month", "snooze", "remind me", "later next month",
    "snooze 2026-09-12", "snooze 2026-02-30", "snooze 09/14", "later 2026-09-20 or tomorrow",
])
def test_ambiguous_or_past_snoozes_clarify_without_scheduling(text: str) -> None:
    result = parse_reply(text, TODAY)
    assert result["intent"] == "unknown"
    assert "clarification" in result
    assert "review_on" not in result
    assert "text" not in result


def test_next_week_means_seven_days_across_year_boundary() -> None:
    assert parse_reply("next week", date(2026, 12, 29))["review_on"] == "2027-01-05"


@pytest.mark.parametrize("text,intent,content", [
    ("correction: The goal is paused.", "correct", "The goal is paused."),
    ("actually This is already familiar.", "correct", "This is already familiar."),
    ("Correction:  Choose a smaller step.  ", "correct", "Choose a smaller step."),
    ("change: Read one page.", "change", "Read one page."),
    ("correction:" + "x" * 280, "correct", "x" * 280),
])
def test_scoped_corrections_and_changes_keep_only_concise_content(
    text: str, intent: str, content: str,
) -> None:
    assert parse_reply(text, TODAY) == {"intent": intent, "text": content}


@pytest.mark.parametrize("text", [
    "correction:", "change: ", "actually   ", "correction:" + "x" * 281,
    "change:" + "x" * 281, "correction: First message\nSecond message",
    "remember: Always prioritize this.",
])
def test_unscoped_or_transcript_like_memory_candidates_are_not_saved(text: str) -> None:
    result = parse_reply(text, TODAY)
    assert result["intent"] == "unknown"
    assert "text" not in result


@pytest.mark.parametrize("credential", [
    "password=synthetic-value", "api-key: synthetic-value", "token=synthetic-value",
    "AccountKey=" + "a" * 30, "SharedAccessKey=" + "a" * 30,
    "ghp_" + "a" * 30, "github_pat_" + "a" * 30, "sk-" + "a" * 30,
    "xoxb-" + "a" * 30, "Bearer synthetic-value",
    "123456789:" + "a" * 35, "-----BEGIN PRIVATE KEY-----",
    "https://example.invalid/path?sig=synthetic-value",
    "https://synthetic-user:synthetic-value@example.invalid/",
])
@pytest.mark.parametrize("prefix", ["correction: ", "change: ", "actually "])
def test_credentials_are_rejected_without_echoing_them(prefix: str, credential: str) -> None:
    result = parse_reply(prefix + credential, TODAY)
    assert result["intent"] == "unknown"
    assert "text" not in result
    assert credential not in json.dumps(result)


def test_source_deletion_removes_derived_text_but_keeps_non_replayable_receipt() -> None:
    state = initial_state()
    state["proposals"]["proposal-1"].update(
        status="submitted", text=PRIVATE_MARKER, why=PRIVATE_MARKER,
        action={"kind": "create_task", "text": PRIVATE_MARKER}, action_id="a" * 32,
        parent_extra={"private": PRIVATE_MARKER},
        result={
            "status": "submitted", "operation_id": "operation-1",
            "pr_url": "https://github.com/example/vault/pull/7",
            "private_text": PRIVATE_MARKER, "path": PATH,
        },
    )
    state["memories"]["memory-1"] = memory(text=PRIVATE_MARKER)
    state["messages"]["99"] = "proposal-1"
    state["fingerprints"][PATH] = "b" * 64
    state["deliveries"]["delivery-1"] = {
        "status": "sending", "date": TODAY.isoformat(), "message_ids": [99],
        "text": PRIVATE_MARKER, "proposal_id": "proposal-1", "fingerprints": {PATH: "b" * 64},
    }
    prune_state(state, inventory_paths=set(), today=TODAY)
    tombstone = state["proposals"]["proposal-1"]
    assert tombstone["id"] == "proposal-1"
    assert tombstone["status"] == "submitted"
    assert tombstone["previous_status"] == "submitted"
    assert tombstone["action_id"] == "a" * 32
    assert tombstone["result"] == {
        "status": "submitted", "operation_id": "operation-1", "pr_url": "https://github.com/example/vault/pull/7",
    }
    assert state["messages"]["99"] == "proposal-1"
    assert state["deliveries"]["delivery-1"]["status"] == "abandoned"
    assert state["memories"] == {} and state["fingerprints"] == {}
    assert PRIVATE_MARKER not in json.dumps(state)
    assert PATH not in json.dumps(state)
    assert store_for(FakeBlob(state)).read() == state


@pytest.mark.parametrize("status", [
    "completed", "done", "submitted", "executing", "uncertain", "running", "unknown", "failed",
    "declined", "corrected", "superseded",
])
def test_source_removal_preserves_operation_outcomes_and_decision_history(status: str) -> None:
    state = initial_state()
    receipt_status = "merged" if status in {"completed", "done"} else "unknown"
    state["proposals"]["proposal-1"].update(
        status=status, action_id="a" * 32,
        result={"status": receipt_status, "operation_id": "operation-1"},
    )
    state["memories"]["memory-1"] = memory()
    prune_state(state, inventory_paths={"tasks/done/synthetic-experiment.md"}, today=TODAY)
    record = state["proposals"]["proposal-1"]
    assert record["status"] == status
    assert record["previous_status"] == status
    assert record["action_id"] == "a" * 32
    assert record["result"] == {"status": receipt_status, "operation_id": "operation-1"}
    assert record["invalidation_reason"] == "source_removed"
    assert "source_path" not in record and "text" not in record and "action" not in record
    assert not state["memories"]
    assert store_for(FakeBlob(state)).read() == state


@pytest.mark.parametrize("status", ["pending", "accepted", "approved", "snoozed"])
def test_source_removal_invalidates_unexecuted_approval_without_erasing_its_id(status: str) -> None:
    state = initial_state()
    state["proposals"]["proposal-1"].update(status=status, action_id="a" * 32)
    prune_state(state, inventory_paths=set(), today=TODAY)
    record = state["proposals"]["proposal-1"]
    assert record["status"] == "invalidated"
    assert record["previous_status"] == status
    assert record["action_id"] == "a" * 32
    assert store_for(FakeBlob(state)).read() == state


def test_source_removal_does_not_unverify_an_already_committed_task_snooze() -> None:
    state = initial_state()
    state["proposals"]["proposal-1"].update(
        status="snoozed", action_id="a" * 32, result={"status": "merged", "operation_id": "operation-1"},
    )
    prune_state(state, inventory_paths=set(), today=TODAY)
    record = state["proposals"]["proposal-1"]
    assert record["status"] == "snoozed"
    assert record["result"]["status"] == "merged"
    assert store_for(FakeBlob(state)).read() == state


@pytest.mark.parametrize("status", ["pending", "completed", "executing", "submitted", "uncertain"])
def test_preserved_message_binding_cannot_execute_a_source_removed_proposal(status: str) -> None:
    from briefing_loop import BriefingLoop

    identifier = "c" * 24
    state = empty_state()
    state["proposals"][identifier] = proposal(identifier, status=status, action_id="a" * 32)
    state["messages"]["99"] = identifier
    prune_state(state, inventory_paths=set(), today=TODAY)
    store = store_for(FakeBlob(state))

    def unexpected_action(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("A source-removed proposal must never dispatch work")

    loop = BriefingLoop(
        store=store, sources=unexpected_action, loops=unexpected_action, generate=unexpected_action,
        send=unexpected_action, revision=unexpected_action, execute=unexpected_action, extras=unexpected_action,
    )
    assert loop.target(99) == identifier
    loop.reply(identifier, "approve", TODAY)
    assert store.read() == state


@pytest.mark.parametrize("change", ["status", "action_id", "result_id", "result_status"])
def test_verified_source_removed_receipt_cannot_regress_or_lose_idempotency(change: str) -> None:
    state = initial_state()
    state["proposals"]["proposal-1"].update(
        status="completed", action_id="a" * 32,
        result={"status": "merged", "operation_id": "operation-1"},
    )
    prune_state(state, inventory_paths=set(), today=TODAY)
    blob = FakeBlob(state)

    def regress(current: dict[str, Any]) -> None:
        record = current["proposals"]["proposal-1"]
        if change == "status":
            record["status"] = "uncertain"
        elif change == "action_id":
            record["action_id"] = "b" * 32
        elif change == "result_id":
            record["result"].pop("operation_id")
        else:
            record["result"]["status"] = "unknown"

    with pytest.raises(StateError, match="source_receipt_immutable"):
        store_for(blob).update(regress)
    assert not blob.writes
    assert blob.saved() == state


def test_read_only_verification_can_complete_an_unresolved_source_removed_receipt() -> None:
    state = initial_state()
    state["proposals"]["proposal-1"].update(
        status="submitted", action_id="a" * 32,
        result={"status": "submitted", "operation_id": "operation-1"},
    )
    prune_state(state, inventory_paths=set(), today=TODAY)
    store = store_for(FakeBlob(state))

    def verified(current: dict[str, Any]) -> None:
        record = current["proposals"]["proposal-1"]
        record["status"] = "completed"
        record["result"]["status"] = "merged"

    store.update(verified)
    record = store.read()["proposals"]["proposal-1"]
    assert record["status"] == "completed"
    assert record["previous_status"] == "submitted"
    assert record["action_id"] == "a" * 32
    assert record["result"]["operation_id"] == "operation-1"


def test_source_reappearing_does_not_restore_erased_memory_or_proposal_authority() -> None:
    state = initial_state()
    state["memories"]["memory-1"] = memory()
    prune_state(state, inventory_paths=set(), today=TODAY)
    once_pruned = copy.deepcopy(state)
    prune_state(state, inventory_paths={PATH}, today=TODAY)
    assert state == once_pruned
    state["memories"]["memory-1"] = memory()
    prune_state(state, inventory_paths={PATH}, today=TODAY)
    assert state["memories"] == {}


@pytest.mark.parametrize("revive", ["proposal", "memory", "delete_tombstone"])
def test_stale_writer_cannot_erase_tombstone_or_restore_derived_content(revive: str) -> None:
    state = initial_state()
    prune_state(state, inventory_paths=set(), today=TODAY)
    blob = FakeBlob(state)

    def stale_write(current: dict[str, Any]) -> None:
        if revive == "proposal":
            current["proposals"]["proposal-1"] = proposal()
        elif revive == "memory":
            current["memories"]["memory-1"] = memory()
        else:
            del current["proposals"]["proposal-1"]

    with pytest.raises(StateError):
        store_for(blob).update(stale_write)
    assert not blob.writes
    assert blob.saved() == state


def test_concurrent_source_removal_prevents_correction_from_reviving_memory() -> None:
    blob = FakeBlob(initial_state())

    def source_removed(target: FakeBlob) -> None:
        state = target.saved()
        prune_state(state, inventory_paths=set(), today=TODAY)
        target.replace(state)

    blob.before_upload = source_removed
    with pytest.raises(StateError, match="memory_source_invalidated"):
        store_for(blob).update(lambda state: state["memories"].update({"memory-1": memory()}))
    assert blob.saved()["memories"] == {}
    assert blob.saved()["proposals"]["proposal-1"]["status"] == "invalidated"


def test_pruning_uses_inventory_not_selected_sources() -> None:
    state = initial_state()
    state["memories"]["memory-1"] = memory()
    prune_state(state, inventory_paths={PATH, "goals/another-record.md"}, today=TODAY)
    assert state == initial_state() | {"memories": {"memory-1": memory()}}


@pytest.mark.parametrize("expires,expected", [
    ("2026-09-12", True), ("2026-09-13", True), ("2026-09-14", False),
])
def test_expiry_boundary_is_start_of_expiry_date(expires: str, expected: bool) -> None:
    assert is_expired(expires, TODAY) is expected
    state = initial_state()
    state["proposals"]["proposal-1"]["expires_on"] = expires
    prune_state(state, inventory_paths={PATH}, today=TODAY)
    assert state["proposals"]["proposal-1"]["status"] == ("expired" if expected else "pending")


@pytest.mark.parametrize("status", ["executing", "running", "submitted", "uncertain", "unknown", "completed", "declined"])
def test_expiry_preserves_action_receipts_and_decision_history(status: str) -> None:
    state = initial_state()
    state["proposals"]["proposal-1"].update(
        status=status, expires_on="2026-09-12",
        result={"status": "unknown", "operation_id": "operation-1"},
    )
    before = copy.deepcopy(state)
    prune_state(state, inventory_paths={PATH}, today=TODAY)
    assert state == before


def test_expired_event_is_removed_but_durable_corrections_and_supersession_remain() -> None:
    state = initial_state()
    state["memories"] = {
        "old": memory("old", active=False),
        "new": memory("new", supersedes="old"),
        "event": memory("event", kind="event", expires_on=TODAY.isoformat()),
        "future": memory("future", kind="event", expires_on="2026-09-14"),
    }
    prune_state(state, inventory_paths={PATH}, today=TODAY)
    assert set(state["memories"]) == {"old", "new", "future"}
    assert state["memories"]["old"]["active"] is False
    assert state["memories"]["new"]["supersedes"] == "old"
    assert store_for(FakeBlob(state)).read() == state
