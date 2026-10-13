from __future__ import annotations

import copy
import json
import sys
import types
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from azure.core.exceptions import ResourceNotFoundError, ResourceModifiedError, ResourceExistsError

from task_assist import (
    ASSIST_MODEL_SCHEMA, TaskAssist, TaskAssistError,
)
from task_assist_state import AssistStoreError
from task_service import TaskError
from briefing_state import _validate as validate_briefing_state, empty_state

TODAY = date(2026, 10, 13)
REQUEST_ID = "c2db9c14-f5cb-40dd-9a7e-3ac07e8a33b3"
THREAD_ID = "52a2a770-97ae-4f1b-bd85-364db1baf0d3"
TASK_PATH = "tasks/2026-10-13-synthetic-task.md"
TASK_REVISION = "a" * 40
TASK_TEXT = (
    "# Synthetic task\n\n"
    "```yaml\n"
    "type: task\ncaptured: 2026-10-13\nstage: ready\narea: learning\nexecution: assisted\n"
    "```\n\n**Outcome:** One synthetic outcome.\n\n"
    "**Next action:** Write one synthetic line.\n\n"
    "**Done when:** Confirm the synthetic line is saved.\n"
)


class FakeDownload:
    def __init__(self, payload: bytes, etag: str):
        self.payload = payload
        self.properties = SimpleNamespace(etag=etag, size=len(payload))

    def readall(self):
        return self.payload


class FakeSidecarBlob:
    def __init__(self):
        self.payload = None
        self.etag = 0

    def download_blob(self, **_):
        if self.payload is None:
            raise ResourceNotFoundError(error_code="BlobNotFound")
        return FakeDownload(self.payload, str(self.etag))

    def upload_blob(self, payload, *, overwrite, etag=None, match_condition=None, **_):
        if self.payload is None:
            if overwrite:
                raise ResourceNotFoundError(error_code="BlobNotFound")
        elif not overwrite:
            raise ResourceExistsError(error_code="BlobAlreadyExists")
        elif str(self.etag) != etag:
            raise ResourceModifiedError(error_code="ConditionNotMet")
        self.payload = bytes(payload)
        self.etag += 1

    def saved(self):
        return json.loads(self.payload) if self.payload else None


class FakeContainer:
    def __init__(self):
        self.blob = FakeSidecarBlob()

    def get_blob_client(self, _name):
        return self.blob


class FakeMainStore:
    def __init__(self, container):
        self._blob = SimpleNamespace(_get_container_client=lambda: container)
        self.state = empty_state()

    def read(self):
        return copy.deepcopy(self.state)

    def update(self, mutate):
        result = mutate(self.state)
        validate_briefing_state(self.state)
        return copy.deepcopy(result)


class FakeTaskService:
    def __init__(self, *, publish_status="merged"):
        self.container = FakeContainer()
        self.store = FakeMainStore(self.container)
        self.publish_status = publish_status
        self.sources = {
            "tasks/README.md": {
                "path": "tasks/README.md", "revision": "b" * 40,
                "text": "Synthetic task schema contract.", "kind": "task_contract",
            },
            TASK_PATH: {
                "path": TASK_PATH, "revision": TASK_REVISION, "text": TASK_TEXT,
                "title": "Synthetic task", "area": "learning", "stage": "ready",
                "execution": "assisted", "outcome": "One synthetic outcome.",
                "next_action": "Write one synthetic line.",
                "done_when": "Confirm the synthetic line is saved.",
                "review_on": None, "waiting_for": None,
            },
        }
        self.reads = []
        self.captured = []
        self.refined = []
        self.decisions = []

    def owner_date(self):
        return TODAY

    def _source(self, path, revision=None):
        self.reads.append(path)
        source = self.sources.get(path)
        if not source:
            raise TaskError("task_source_not_canonical")
        if revision and revision != source["revision"]:
            raise TaskError("task_source_changed")
        return copy.deepcopy(source)

    def _proposal(self, *, request_id, kind, path, action, today):
        proposal_id = request_id[:24]
        digest = "d" * 64
        source = self._source(path)
        record = {
            "id": proposal_id, "kind": kind, "task_workspace": True,
            "source_path": path, "source_revision": source["revision"],
            "source_digest": "c" * 64, "approval_digest": digest,
            "status": "pending", "action": action,
            "text": "Synthetic task proposal.", "created_on": today.isoformat(),
            "expires_on": today.isoformat(), "message_ids": [],
        }
        self.store.state["proposals"][proposal_id] = copy.deepcopy(record)
        return record

    def capture(self, payload, today):
        self.captured.append(copy.deepcopy(payload))
        return self._proposal(
            request_id=payload["request_id"], kind="capture_task",
            path="tasks/README.md", action={"kind": "create_task", "definition": payload["definition"]},
            today=today,
        )

    def refine(self, payload, today):
        self.refined.append(copy.deepcopy(payload))
        return self._proposal(
            request_id=payload["request_id"], kind="refine_task", path=payload["path"],
            action={"kind": "refine_task", "changes": payload["changes"]},
            today=today,
        )

    def change(self, payload, today):
        self.refined.append(copy.deepcopy(payload))
        return self._proposal(
            request_id=payload["request_id"], kind="edit_task", path=payload["path"],
            action={"kind": "update_task", "change": payload["change"]},
            today=today,
        )

    def close(self, payload, today):
        return self._proposal(
            request_id=payload["request_id"], kind="close_task", path=payload["path"],
            action={"kind": "update_task", "change": {"status": "done"}},
            today=today,
        )

    def decide(self, payload, _today):
        self.decisions.append(copy.deepcopy(payload))
        record = self.store.state["proposals"][payload["proposal_id"]]
        if self.publish_status == "merged":
            record.update(
                status="completed",
                publish_result={
                    "status": "merged", "path": TASK_PATH,
                    "source_revision": "e" * 40,
                },
                result={"status": "merged", "summary": "Canonical synthetic task update confirmed."},
                source_status="current",
            )
            if record["kind"] == "refine_task":
                for key, value in record["action"]["changes"].items():
                    self.sources[record["source_path"]][key] = value
                self.sources[record["source_path"]]["revision"] = "e" * 40
        else:
            record.update(
                status="submitted",
                publish_result={"status": self.publish_status},
                result={"status": self.publish_status}, source_status="current",
            )
        change = record["action"].get("changes") or record["action"].get("change") or {}
        if self.publish_status == "merged" and record["source_path"] == TASK_PATH:
            self.sources[TASK_PATH].update(change)
            self.sources[TASK_PATH]["revision"] = "e" * 40
        return copy.deepcopy(record)

    def reconcile(self, proposal_id, _today):
        record = self.store.state["proposals"][proposal_id]
        if self.publish_status == "merged":
            record.update(
                status="completed",
                publish_result={"status": "merged", "path": TASK_PATH, "source_revision": "e" * 40},
                result={"status": "merged", "summary": "Canonical synthetic task update confirmed."},
                source_status="current",
            )
            self.sources[TASK_PATH]["revision"] = "e" * 40
        else:
            record.update(
                status="submitted", publish_result={"status": self.publish_status},
                result={"status": self.publish_status}, source_status="current",
            )
        return copy.deepcopy(record)


def model_output(**overrides):
    result = {
        "intent": "chat", "reply": "A short synthetic answer.", "language": "en",
        "suggestions": [], "task_path": None, "changes": None, "definition": None,
        "scope": None, "estimate": None, "plan": None, "knowledge_target": None, "completion": None,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    result.update(overrides)
    return result


def assistant(generate, *, service=None, **env):
    settings = {"MINDME_ASSIST_ENABLED": "true", **env}
    return TaskAssist(service or FakeTaskService(), settings, generate)


def test_chat_exposes_structured_contract_and_replay_is_idempotent():
    calls = []

    def generate(context):
        calls.append(context)
        return model_output()

    service = FakeTaskService()
    subject = assistant(generate, service=service)
    payload = {"request_id": REQUEST_ID, "message": "What is one next step?", "task": None}
    response = subject.chat(payload, TODAY)

    assert response["reply"] == "A short synthetic answer."
    assert len(response["suggestions"]) <= 4
    assert calls[0]["schema"] == ASSIST_MODEL_SCHEMA
    assert "usage" not in ASSIST_MODEL_SCHEMA["required"]
    assert calls[0]["sources"][0]["path"] == "tasks/README.md"
    assert all(path != TASK_PATH for path in service.reads)
    assert subject.chat(payload, TODAY) == response
    assert len(calls) == 1


@pytest.mark.parametrize("payload", [
    {"request_id": REQUEST_ID, "message": "Hi", "task": None, "unexpected": "not accepted"},
    {"request_id": "not-a-uuid", "message": "Hi"},
])
def test_chat_rejects_extra_keys_and_non_uuid_request_id(payload):
    subject = assistant(lambda _: model_output())
    with pytest.raises(TaskAssistError):
        subject.chat(payload, TODAY)


def test_same_request_id_with_changed_payload_conflicts():
    subject = assistant(lambda _: model_output())
    subject.chat({"request_id": REQUEST_ID, "message": "One", "task": None}, TODAY)
    with pytest.raises(TaskAssistError, match="assist_request_conflict"):
        subject.chat({"request_id": REQUEST_ID, "message": "Two", "task": None}, TODAY)


def test_chat_accepts_strict_boolean_voice_hint():
        subject = assistant(lambda _: model_output())
        response = subject.chat({
            "request_id": REQUEST_ID, "message": "Transcribed owner request",
            "task": None, "voice": True,
        }, TODAY)
        assert response["reply"] == "A short synthetic answer."

        with pytest.raises(TaskAssistError, match="assist_request_invalid"):
            subject.chat({
                "request_id": "2c921d00-2b0a-47fa-99a5-38a2af3d94c5",
                "message": "Not a strict boolean", "task": None, "voice": "true",
            }, TODAY)


def test_stale_cached_detail_is_invalidated_after_published_mutation():
        service = FakeTaskService()
        outputs = iter([
            model_output(intent="detail", task_path=TASK_PATH),
            model_output(
                intent="refine", task_path=TASK_PATH,
                changes={"next_action": "Write a revised synthetic line."},
            ),
        ])
        subject = assistant(lambda _: next(outputs), service=service)
        details_id = REQUEST_ID
        selected = {"path": TASK_PATH, "revision": TASK_REVISION}
        details = subject.chat({
            "request_id": details_id, "message": "Show this task", "task": selected,
        }, TODAY)
        assert details["cards"][0]["next_action"] == "Write one synthetic line."

        subject.step({
            "request_id": "62c73c00-bad0-4d04-a718-2b63de34d15c",
            "thread_id": details["thread_id"], "step": "refine", "task": selected,
            "input": "Change the next action to a revised synthetic line.",
        }, TODAY)

        with pytest.raises(TaskAssistError, match="assist_request_uncertain"):
            subject.chat({
                "request_id": details_id, "message": "Show this task", "task": selected,
            }, TODAY)
        state = subject.store.read()
        from task_assist_state import request_key
        old_request = state["requests"][request_key(details_id)]
        assert old_request["status"] == "uncertain"
        assert old_request["response"] is None


def test_settlement_uses_claimed_month_and_day_without_releasing_new_reservation():
        subject = assistant(lambda _: model_output())
        october = date(2026, 10, 31)
        november = date(2026, 11, 1)
        request_a = "f3ef6340-657f-4124-b6fa-826d54aa98e0"
        request_b = "6c70b835-57e0-4e24-9cf0-49e848443859"
        digest = "a" * 64
        subject._claim(request_a, digest, None, october, voice=False)
        subject._claim(request_b, "b" * 64, None, november, voice=False)
        november_reserved = subject.store.read()["ledger"]["reserved_micro_eur"]
        november_daily_reserved = subject.store.read()["ledger"]["daily_reserved_micro_eur"]

        subject._settle(request_a, october, input_tokens=100, output_tokens=20)

        ledger = subject.store.read()["ledger"]
        assert ledger["month"] == "2026-11"
        assert ledger["reserved_micro_eur"] == november_reserved
        assert ledger["daily_reserved_micro_eur"] == november_daily_reserved
        assert ledger["spent_micro_eur"] == 0
        assert ledger["month_history"]["2026-10"]["reserved_micro_eur"] == 0
        assert ledger["month_history"]["2026-10"]["spent_micro_eur"] > 0
        assert ledger["day_history"]["2026-10-31"]["daily_reserved_micro_eur"] == 0
        assert ledger["day_history"]["2026-10-31"]["daily_spent_micro_eur"] > 0


def test_uncertain_model_call_is_not_retried_and_reservation_is_preserved():
    calls = []

    def fail(_context):
        calls.append(1)
        raise TimeoutError

    subject = assistant(fail)
    payload = {"request_id": REQUEST_ID, "message": "A bounded request", "task": None}
    with pytest.raises(TaskAssistError, match="assist_model_unavailable"):
        subject.chat(payload, TODAY)
    with pytest.raises(TaskAssistError, match="assist_request_uncertain"):
        subject.chat(payload, TODAY)
    assert len(calls) == 1
    assert subject.usage(TODAY)["monthly_reserved"] > 0


def test_monthly_budget_reserves_before_calling_model():
    calls = []
    subject = assistant(lambda _: calls.append(1) or model_output(), MINDME_ASSIST_MONTHLY_EUR="0.00001")
    with pytest.raises(TaskAssistError, match="assist_monthly_budget_exhausted"):
        subject.chat({"request_id": REQUEST_ID, "message": "Budget check", "task": None}, TODAY)
    assert calls == []


def test_only_explicit_canonical_task_path_is_read_and_model_cannot_choose_another():
    service = FakeTaskService()
    calls = []

    def generate(context):
        calls.append(context)
        return model_output(intent="detail", task_path="tasks/2026-10-14-invented.md")

    subject = assistant(generate, service=service)
    with pytest.raises(TaskAssistError, match="assist_model_output_invalid"):
        subject.chat({
            "request_id": REQUEST_ID, "message": "Show this task",
            "task": {"path": TASK_PATH, "revision": TASK_REVISION},
        }, TODAY)
    assert TASK_PATH in service.reads
    assert "tasks/2026-10-14-invented.md" not in service.reads
    assert calls[0]["sources"][-1]["path"] == TASK_PATH


def test_private_or_non_task_path_is_rejected_before_source_read():
    service = FakeTaskService()
    subject = assistant(lambda _: model_output(), service=service)
    with pytest.raises(TaskAssistError, match="assist_task_path_invalid"):
        subject.chat({
            "request_id": REQUEST_ID, "message": "Read another path",
            "task": {"path": "wiki/private.md", "revision": TASK_REVISION},
        }, TODAY)
    assert TASK_PATH not in service.reads


@pytest.mark.parametrize(
    ("publish_status", "card_type"),
    [("merged", "applied"), ("submitted", "applied"), ("unknown", "applied")],
)
def test_auto_apply_only_reports_confirmed_canonical_publication(publish_status, card_type):
    output = model_output(
        intent="capture", definition={"title": "Capture one synthetic task", "stage": "clarify"},
    )
    service = FakeTaskService(publish_status=publish_status)
    subject = assistant(lambda _: copy.deepcopy(output), service=service)
    response = subject.step({
        "request_id": REQUEST_ID, "step": "capture", "input": "Capture this task: one synthetic action",
        "task": None,
    }, TODAY)
    assert response["cards"][0]["type"] == card_type
    serialized_state = json.loads(json.dumps(service.store.state))
    validate_briefing_state(serialized_state)
    saved_proposal = serialized_state["proposals"][REQUEST_ID.replace("-", "")[:24]]
    assert saved_proposal["approved_by"] == "assist-standing"
    if publish_status != "merged":
        assert response["cards"][0]["status"] in {"submitted", "uncertain"}
        assert response["cards"][0]["saved"] is False
    else:
        assert response["cards"][0]["undo_available"] is False
        assert response["cards"][0]["saved"] is True


def test_capture_persists_bounded_estimate_and_plan_as_context_and_subtasks():
        output = model_output(
            intent="capture",
            definition={
                "title": "One synthetic creation",
                "context": "Preserve this owner context.",
                "subtasks": ["Existing owner subtask"],
                "stage": "clarify",
            },
            estimate={
                "minutes_low": 45, "minutes_high": 45,
                "rationale": "A bounded synthetic estimate.", "confidence": "medium",
            },
            plan={
                "steps": [
                    {"text": "Draft one heading", "estimate_min": 15, "who": "owner"},
                    {"text": "Review one heading", "estimate_min": 30, "who": "owner"},
                ],
                "total_estimate_min": 45,
            },
        )
        service = FakeTaskService()
        subject = assistant(lambda _: copy.deepcopy(output), service=service)
        subject.step({
            "request_id": REQUEST_ID, "step": "capture",
            "input": "Capture one synthetic task.",
            "task": None,
        }, TODAY)
        definition = service.captured[0]["definition"]
        assert definition["context"] == "Preserve this owner context.\nEstimate: ~45m"
        assert definition["subtasks"] == [
            "Existing owner subtask", "Draft one heading", "Review one heading",
        ]


def test_closure_requires_explicit_owner_attestation_and_exact_user_evidence():
    output = model_output(
        intent="resolve", task_path=TASK_PATH,
        completion={"result": "The line was written", "evidence": "I saved the line", "learning": None},
    )
    service = FakeTaskService()
    subject = assistant(lambda _: copy.deepcopy(output), service=service)
    no_attestation = subject.step({
        "request_id": REQUEST_ID, "step": "resolve", "input": "I saved the line",
        "task": {"path": TASK_PATH, "revision": TASK_REVISION},
    }, TODAY)
    assert not service.decisions
    assert no_attestation["cards"][0]["type"] == "draft"

    assert subject._explicit_done("Готово, я сделал это.")
    assert not subject._explicit_done("I should do that when done")


def test_voice_duration_is_checked_before_transcription_and_audio_is_not_persisted(monkeypatch):
    calls = []
    audio_module = types.ModuleType("assist_audio")

    def duration(_audio, _mime):
        calls.append("duration")
        return 9.5

    audio_module.audio_duration = duration
    monkeypatch.setitem(sys.modules, "assist_audio", audio_module)

    def transcribe(audio, mime):
        assert audio == b"synthetic-audio"
        assert mime == "audio/ogg"
        calls.append("transcribe")
        return {"text": "A synthetic spoken request", "language": "en", "usage": {
            "input_tokens": 1, "output_tokens": 2,
        }}

    service = FakeTaskService()
    model_calls = []
    subject = TaskAssist(
        service, {"MINDME_ASSIST_ENABLED": "true"},
        lambda context: model_calls.append(context) or model_output(), transcribe,
    )
    response = subject.voice(b"synthetic-audio", "audio/ogg", REQUEST_ID, TODAY)
    assert calls == ["duration", "transcribe"]
    assert model_calls == []
    assert response == {
        "text": "A synthetic spoken request", "language": "en", "duration_s": 9.5,
    }
    assert service.captured == [] and service.refined == [] and service.decisions == []
    assert subject.usage(TODAY)["monthly_used"] == pytest.approx(0.00095)
    assert subject.usage(TODAY)["monthly_reserved"] == 0
    serialized = json.dumps(subject.store.read(), ensure_ascii=False)
    assert "synthetic-audio" not in serialized
    assert "A synthetic spoken request" not in serialized
    with pytest.raises(TaskAssistError, match="assist_request_uncertain"):
        subject.voice(b"synthetic-audio", "audio/ogg", REQUEST_ID, TODAY)
    assert calls == ["duration", "transcribe", "duration"]


def test_voice_rejects_audio_bounds_before_transcription(monkeypatch):
    audio_module = types.ModuleType("assist_audio")
    audio_module.audio_duration = lambda *_: 121
    monkeypatch.setitem(sys.modules, "assist_audio", audio_module)
    calls = []
    subject = TaskAssist(
        FakeTaskService(), {"MINDME_ASSIST_ENABLED": "true"},
        lambda _: model_output(), lambda *_: calls.append("transcribe"),
    )
    with pytest.raises(TaskAssistError, match="assist_audio_too_long"):
        subject.voice(b"x", "audio/ogg", REQUEST_ID, TODAY)
    assert calls == []


def test_voice_returns_literal_sensitive_journal_transcript_without_task_filter(monkeypatch):
    audio_module = types.ModuleType("assist_audio")
    audio_module.audio_duration = lambda *_: 4
    monkeypatch.setitem(sys.modules, "assist_audio", audio_module)
    literal = "Journal: synthetic private reflection."
    subject = TaskAssist(
        FakeTaskService(), {"MINDME_ASSIST_ENABLED": "true"},
        lambda _: pytest.fail("voice must not invoke the task model"),
        lambda *_: {"text": literal, "language": "en"},
    )
    assert subject.voice(b"synthetic-audio", "audio/ogg", REQUEST_ID, TODAY) == {
        "text": literal, "language": "en", "duration_s": 4.0,
    }


def test_usage_baseline_and_model_prices_are_positive_and_conservative():
    subject = assistant(lambda _: model_output())
    usage = subject.usage(TODAY)
    assert {"month_eur", "month_cap_eur", "today_eur", "today_cap_eur", "calls_today", "models"} <= usage.keys()
    assert subject.model_prices["gpt-6-luna"] == (Decimal("10"), Decimal("50"))
    with pytest.raises(TaskAssistError, match="assist_configuration_invalid"):
        assistant(
            lambda _: model_output(),
            MINDME_ASSIST_MODEL_PRICES_JSON='{"gpt-6-luna":{"input_eur_per_1m":0,"output_eur_per_1m":50}}',
        )


def test_telegram_send_claims_are_durable_and_uncertain_events_are_not_retried():
    store = TaskAssist(FakeTaskService(), {"MINDME_ASSIST_ENABLED": "true"}, lambda _: model_output()).store
    assert store.claim_telegram_event("event-42", "e" * 64, TODAY) == "new"
    store.finish_telegram_event("event-42", "uncertain")
    assert store.claim_telegram_event("event-42", "e" * 64, TODAY) == "uncertain"
    assert store.claim_telegram_event("event-42", "f" * 64, TODAY) == "conflict"
    store.bind_telegram_message(420, THREAD_ID, TODAY, proposal_id="a" * 24, receipt_id="b" * 24)
    assert store.thread_for_message(420)["thread_id"] == THREAD_ID


def test_undo_refuses_unconfirmed_or_create_operations():
    subject = assistant(lambda _: model_output())
    with pytest.raises(TaskAssistError, match="assist_request_id_invalid"):
        subject.undo({"request_id": "invalid", "receipt_id": "a" * 24}, TODAY)
    result = subject.undo({"request_id": REQUEST_ID, "receipt_id": "a" * 24}, TODAY)
    assert result["undone"] is False
    assert result["cards"][0]["kind"] == "draft"


def test_auto_apply_off_keeps_a_normal_proposal():
    output = model_output(
        intent="capture", definition={"title": "Capture one synthetic task", "stage": "clarify"},
    )
    service = FakeTaskService()
    subject = assistant(lambda _: copy.deepcopy(output), service=service, MINDME_ASSIST_AUTO_APPLY="false")
    response = subject.step({
        "request_id": REQUEST_ID, "step": "capture", "input": "Capture this task: synthetic",
        "task": None,
    }, TODAY)
    assert response["cards"][0]["type"] == "proposal"
    assert service.decisions == []


def test_deterministic_stage_change_autoapplies_without_model_and_supports_undo():
    service = FakeTaskService()
    calls = []
    subject = assistant(lambda _: calls.append(1) or model_output(), service=service)
    payload = {
        "request_id": REQUEST_ID, "path": TASK_PATH, "revision": TASK_REVISION,
        "change": {"stage": "doing"},
    }
    response = subject.change(payload, TODAY)
    card = response["cards"][0]
    assert calls == []
    assert card["type"] == "applied"
    assert card["action_digest"] == card["approval_digest"] == "d" * 64
    assert card["undo_available"] is True
    assert service.store.state["proposals"][REQUEST_ID.replace("-", "")[:24]]["approved_by"] == "assist-standing"
    assert subject.change(payload, TODAY) == response

    undo_request = "bbd36a90-e4ba-4abe-aa73-9469c567cf09"
    undone = subject.undo({"request_id": undo_request, "receipt_id": card["receipt_id"]}, TODAY)
    assert undone["undone"] is True
    assert service.sources[TASK_PATH]["stage"] == "ready"
    assert subject.undo({"request_id": undo_request, "receipt_id": card["receipt_id"]}, TODAY) == undone


def test_undo_invalidates_cached_same_thread_task_details():
        service = FakeTaskService()
        base_decide = service.decide
        publication_count = 0

        def publish_distinct_revisions(payload, today):
            nonlocal publication_count
            receipt = base_decide(payload, today)
            publication_count += 1
            revision = ("e" if publication_count == 1 else "f") * 40
            receipt["publish_result"]["source_revision"] = revision
            service.sources[TASK_PATH]["revision"] = revision
            service.store.state["proposals"][payload["proposal_id"]]["publish_result"]["source_revision"] = revision
            return receipt

        service.decide = publish_distinct_revisions
        subject = assistant(lambda _: model_output(intent="detail", task_path=TASK_PATH), service=service)
        stage_card = subject.change({
            "request_id": REQUEST_ID, "path": TASK_PATH, "revision": TASK_REVISION,
            "change": {"stage": "doing"},
        }, TODAY)["cards"][0]
        assert service.sources[TASK_PATH]["stage"] == "doing"
        thread_id = subject.store.read()["operations"][stage_card["receipt_id"]]["thread_id"]

        details_id = "3bb120d9-2f75-4b74-bd72-1fcad6a13735"
        selected_doing = {"path": TASK_PATH, "revision": "e" * 40}
        details_payload = {
            "request_id": details_id, "thread_id": thread_id,
            "message": "Show the current task details.", "task": selected_doing,
        }
        details = subject.chat(details_payload, TODAY)
        assert details["cards"][0]["stage"] == "doing"

        subject.undo({
            "request_id": "bbd36a90-e4ba-4abe-aa73-9469c567cf09",
            "receipt_id": stage_card["receipt_id"],
        }, TODAY)
        assert service.sources[TASK_PATH]["stage"] == "ready"

        with pytest.raises(TaskAssistError, match="assist_request_uncertain"):
            subject.chat(details_payload, TODAY)
        from task_assist_state import request_key
        cached_details = subject.store.read()["requests"][request_key(details_id)]
        assert cached_details["status"] == "uncertain"
        assert cached_details["response"] is None


def test_deterministic_change_rejects_other_fields_without_model_call():
    calls = []
    subject = assistant(lambda _: calls.append(1) or model_output())
    with pytest.raises(TaskAssistError, match="assist_change_invalid"):
        subject.change({
            "request_id": REQUEST_ID, "path": TASK_PATH, "revision": TASK_REVISION,
            "change": {"deadline": "2026-10-15"},
        }, TODAY)
    assert calls == []


def test_v1_payloads_require_task_object_and_reject_legacy_aliases():
        subject = assistant(lambda _: model_output())
        with pytest.raises(TaskAssistError, match="assist_request_invalid"):
            subject.chat({"request_id": REQUEST_ID, "message": "Hi"}, TODAY)
        with pytest.raises(TaskAssistError, match="assist_request_invalid"):
            subject.chat({
                "request_id": REQUEST_ID, "message": "Hi", "task": None,
                "task_path": TASK_PATH, "task_revision": TASK_REVISION,
            }, TODAY)
        with pytest.raises(TaskAssistError, match="assist_request_invalid"):
            subject.step({"request_id": REQUEST_ID, "action": "next", "message": "Hi"}, TODAY)


def test_assist_model_schema_uses_json_null_and_host_supplied_usage():
        nullable_stage = ASSIST_MODEL_SCHEMA["properties"]["changes"]["properties"]["stage"]
        assert nullable_stage["type"] == ["string", "null"]
        assert None in nullable_stage["enum"]
        assert "null" not in nullable_stage["enum"]
        assert "usage" not in ASSIST_MODEL_SCHEMA["required"]


def test_model_routing_and_configured_model_prices_are_used_before_call():
        output = model_output(intent="plan")
        seen = []

        def generate(context):
            seen.append(context)
            return copy.deepcopy(output)

        prices = json.dumps({"gpt-6-sol": {"input_eur_per_1m": 1, "output_eur_per_1m": 2}})
        subject = assistant(generate, service=FakeTaskService(), MINDME_ASSIST_MODEL_PRICES_JSON=prices)
        response = subject.step({
            "request_id": REQUEST_ID, "step": "plan", "input": "Plan this task",
            "task": {"path": TASK_PATH, "revision": TASK_REVISION},
        }, TODAY)
        assert seen[0]["model"] == "gpt-6-sol"
        assert seen[0]["schema"] == ASSIST_MODEL_SCHEMA
        assert subject.usage(TODAY)["models"]["assist"] == "gpt-6-luna"
        assert response["usage"]["month_cap_eur"] == 12.0

        chat = assistant(lambda context: seen.append(context) or model_output())
        chat.chat({"request_id": REQUEST_ID, "message": "Plan a careful rollout", "task": None}, TODAY)
        assert seen[-1]["model"] == "gpt-6-sol"


def test_next_gets_three_eligible_ranked_reasons_and_checks_model_path():
        service = FakeTaskService()
        service.repository = SimpleNamespace(page=lambda _offset, today: {
            "items": [{
                "path": TASK_PATH, "revision": TASK_REVISION, "title": "Synthetic task",
                "stage": "ready", "attention_eligible": True,
                "attention_reasons": ["deadline_soon", "review_due"],
            }],
        })
        calls = []
        subject = assistant(lambda context: calls.append(context) or model_output(
            intent="next", task_path=TASK_PATH,
        ), service=service)
        result = subject.step({
            "request_id": REQUEST_ID, "step": "next", "task": None, "input": None,
        }, TODAY)
        assert calls[0]["candidates"] == [{
            "rank": 1, "path": TASK_PATH, "revision": TASK_REVISION, "title": "Synthetic task",
            "stage": "ready", "attention_reasons": ["deadline_soon", "review_due"],
            "attention_eligible": True,
        }]
        assert result["cards"][0]["kind"] == "task"


def test_detail_and_next_apply_only_explicit_supported_source_bound_changes():
        for action in ("detail", "next"):
            service = FakeTaskService()
            subject = assistant(lambda _: model_output(
                intent=action, task_path=TASK_PATH, changes={"next_action": "Write one bounded line."},
            ), service=service)
            response = subject.step({
                "request_id": REQUEST_ID, "step": action,
                "task": {"path": TASK_PATH, "revision": TASK_REVISION},
                "input": "Change the next action to one bounded line.",
            }, TODAY)
            assert len(service.refined) == 1
            assert response["cards"][0]["action"] == "refine"
            assert response["cards"][0]["kind"] == "applied"


def test_plan_and_estimate_cards_meet_v1_contract_and_execution_stays_a_draft():
        estimate = model_output(
            intent="estimate", estimate={
                "minutes_low": 10, "minutes_high": 20, "rationale": "Synthetic bound.",
                "confidence": "medium",
            },
        )
        result = assistant(lambda _: estimate).step({
            "request_id": REQUEST_ID, "step": "estimate", "task": None, "input": "Estimate",
        }, TODAY)
        assert result["cards"][0]["confidence"] == "medium"

        plan = model_output(intent="plan", plan={
            "steps": [{"text": "Write one line", "estimate_min": 5, "who": "owner"}],
            "total_estimate_min": 5,
        })
        result = assistant(lambda _: plan).step({
            "request_id": REQUEST_ID, "step": "plan", "task": None, "input": "Plan",
        }, TODAY)
        assert result["cards"][0]["total_estimate_min"] == 5
        assert result["cards"][0]["steps"][0]["who"] == "owner"

        execute = model_output(intent="execute", reply="Prepare the next synthetic action.")
        result = assistant(lambda _: execute).step({
            "request_id": REQUEST_ID, "step": "execute", "task": None, "input": "Prepare",
        }, TODAY)
        assert result["cards"][0]["kind"] == "draft"
        assert result["cards"][0]["body"] == "Prepare the next synthetic action."


def test_unstated_deadline_suggestion_is_preserved_but_not_autoapproved():
        output = model_output(
            intent="detail", task_path=TASK_PATH,
            changes={"deadline": "2026-10-20"},
        )
        service = FakeTaskService()
        subject = assistant(lambda _: copy.deepcopy(output), service=service)
        response = subject.step({
            "request_id": REQUEST_ID, "step": "detail",
            "task": {"path": TASK_PATH, "revision": TASK_REVISION},
            "input": "The notes mention 2026-10-20. Change task details.",
        }, TODAY)
        assert response["cards"][0]["kind"] == "proposal"
        assert response["cards"][0]["diff"][0]["after"] == "2026-10-20"
        assert service.decisions == []


def test_deadline_authority_is_explicit_or_owner_calendar_relative():
        policy = TaskAssist._date_autoapply_allowed
        assert policy({"deadline": "2026-10-14"}, "Finish by tomorrow.", TODAY)
        assert policy({"deadline": "2026-10-13"}, "Deadline today.", TODAY)
        assert policy({"deadline": "2026-10-20"}, "Set deadline 2026-10-20.", TODAY)
        assert not policy({"deadline": "2026-10-20"}, "The notes mention 2026-10-20.", TODAY)
        assert policy({"review_on": "2026-10-13"}, "Review the task.", TODAY)
        assert not policy({"focus_on": "2026-10-12"}, "Focus on the task.", TODAY)


def test_thread_history_is_invalidated_before_generation_and_cached_replay():
        service = FakeTaskService()
        calls = []
        subject = assistant(lambda context: calls.append(context) or model_output(intent="detail"), service=service)
        selected = {"path": TASK_PATH, "revision": TASK_REVISION}
        response = subject.chat({
            "request_id": REQUEST_ID, "message": "Show this task", "task": selected,
        }, TODAY)
        thread_id = response["thread_id"]
        service.sources[TASK_PATH]["revision"] = "c" * 40
        cached = subject.chat({
            "request_id": REQUEST_ID, "message": "Show this task", "task": selected,
        }, TODAY)
        assert cached["cards"] == []
        assert "earlier context was cleared" in cached["reply"]
        assert len(calls) == 1

        service.sources[TASK_PATH]["revision"] = "a" * 40
        second = subject.chat({
            "request_id": "62c73c00-bad0-4d04-a718-2b63de34d15c",
            "thread_id": thread_id, "message": "Continue", "task": None,
        }, TODAY)
        assert calls[-1]["thread"] == []


def test_async_receipt_refreshes_same_poll_uuid_and_unlocks_only_confirmed_undo():
        service = FakeTaskService(publish_status="submitted")
        output = model_output(
            intent="refine", task_path=TASK_PATH, changes={"next_action": "Write one bounded line."},
        )
        subject = assistant(lambda _: copy.deepcopy(output), service=service)
        initial = subject.step({
            "request_id": REQUEST_ID, "step": "refine", "task": {
                "path": TASK_PATH, "revision": TASK_REVISION,
            }, "input": "Change the next action to one bounded line.",
        }, TODAY)
        card = initial["cards"][0]
        assert card["kind"] == "applied" and card["saved"] is False and not card["undo_available"]
        assert card["source_status"] == "current"
        with pytest.raises(TaskAssistError, match="assist_request_uncertain"):
            subject.step({
                "request_id": REQUEST_ID, "step": "refine", "task": {
                    "path": TASK_PATH, "revision": TASK_REVISION,
                }, "input": "Change the next action to one bounded line.",
            }, TODAY)
        assert len(service.decisions) == 1

        poll_id = "4a4d7d8f-93d1-4b55-bb83-d63f4f597b6e"
        pending = subject.receipt({"request_id": poll_id, "receipt_id": card["receipt_id"]}, TODAY)
        assert pending["cards"][0]["status"] == "submitted"
        service.publish_status = "merged"
        updated = subject.receipt({"request_id": poll_id, "receipt_id": card["receipt_id"]}, TODAY)
        assert updated["cards"][0]["saved"] is True
        assert updated["cards"][0]["source_status"] == "current"
        assert updated["cards"][0]["undo_available"] is True
        assert subject.store.read()["operations"][card["receipt_id"]]["applied_revision"] == "e" * 40


def test_uncertain_request_guard_survives_retention_without_cached_content():
        from task_assist_state import empty_assist_state, prune_assist_state, request_key
        state = empty_assist_state()
        key = request_key(REQUEST_ID)
        state["requests"][key] = {
            "digest": "a" * 64, "status": "uncertain", "created_on": "2026-01-01",
            "thread_id": THREAD_ID, "response": None, "reserved_micro_eur": 0,
        }
        prune_assist_state(state, TODAY)
        assert key in state["requests"]
        assert state["requests"][key]["response"] is None
