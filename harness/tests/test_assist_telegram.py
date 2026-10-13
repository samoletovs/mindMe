from unittest.mock import Mock
import copy

import pytest

import function_app as fa
from task_telegram import TaskAssistTelegram, task_assist_intent
from task_service import TaskError
from test_briefing_webhook import request
from test_task_service import service, PATH, REVISION, refine_payload, TODAY
from test_assist_host import model
from test_briefing_state import FakeBlob


@pytest.mark.parametrize("text", [
    "/tasks", "/next", "/plan Write a synthetic outline", "/task Synthetic task",
    "Create a task to outline one page", "Estimate this task", "Создай задачу написать план",
    "Что делать с задачей дальше?", "Закрой задачу",
    "What should I do now?", "Что мне делать дальше?", "I need to draft an outline", "Мне нужно составить план",
])
def test_task_intent_in_english_and_russian(text):
    assert task_assist_intent(text)


@pytest.mark.parametrize("text", [
    "/note This task was interesting", "/idea A task assistant", "/diary Worked on tasks",
    "/dig task tools", "/review", "note: a task mentioned in an article",
    "How is the weather?", "Я сегодня гулял", "idea: try task planning",
])
def test_other_explicit_commands_and_non_task_chat_keep_their_route(text):
    assert not task_assist_intent(text)


class Store:
    def __init__(self):
        self.state = {}

    def read(self):
        return copy.deepcopy(self.state)

    def update(self, callback):
        draft = copy.deepcopy(self.state)
        result = callback(draft)
        self.state = draft
        return result


def adapter(result=None):
    subject, *_ = service()
    assist = Mock(service=subject, store=Store())
    assist.chat.return_value = result or {
        "thread_id": "4f5f2691-a3ee-4b93-aac1-2dd35ce49431", "reply": "Synthetic reply", "language": "en",
        "cards": [], "suggestions": [], "usage": {},
    }
    messages = []

    def send(text, keyboard):
        messages.append((text, keyboard))
        return 100 + len(messages)

    return TaskAssistTelegram(assist, send=send), assist, messages


def test_task_chat_deduplicates_paid_call_and_send():
    telegram, assist, messages = adapter()
    args = {"text": "Refine this task", "event": "update:7", "today": TODAY}
    assert telegram.handle(**args)
    assert telegram.handle(**args)
    assert assist.chat.call_count == 1 and len(messages) == 1
    assert assist.chat.call_args.args[0]["request_id"].count("-") == 4


@pytest.mark.parametrize("text,voice", [
    ("Create a task to write an outline", False),
    ("Создай задачу написать план", True),
    ("/plan Write a synthetic outline", False),
])
def test_telegram_adapter_calls_real_assist_core(monkeypatch, text, voice):
    subject, *_ = service()
    container = Mock()
    container.get_blob_client.return_value = FakeBlob()
    monkeypatch.setattr(fa, "_os_container_client", lambda: container)
    monkeypatch.setenv("MINDME_ASSIST_ENABLED", "true")
    client = model(monkeypatch, {
        "intent": "next", "reply": "Synthetic next action", "language": "en",
        "suggestions": [], "task_path": None, "changes": None, "definition": None,
        "scope": None, "estimate": None, "plan": None, "knowledge_target": None,
        "completion": None,
    })
    messages = []
    telegram = TaskAssistTelegram(
        fa._task_assist(subject),
        send=lambda text, keyboard: messages.append((text, keyboard)) or 101,
    )
    assert telegram.handle(text=text, voice=voice, event="update:700", today=TODAY)
    assert client.chat.completions.create.call_count == 1
    assert len(messages) == 1 and "Synthetic next action" in messages[0][0]


def test_uncertain_telegram_send_is_never_silently_repeated():
    telegram, assist, _ = adapter()
    telegram.send = Mock(side_effect=fa.TelegramDeliveryError("unconfirmed"))
    args = {"text": "Refine this task", "event": "update:7", "today": TODAY}
    with pytest.raises(fa.TelegramDeliveryError):
        telegram.handle(**args)
    assert telegram.handle(**args)
    assert assist.chat.call_count == 1 and telegram.send.call_count == 1


def test_tasks_includes_definition_and_verification_needing_owner_without_model():
    telegram, assist, messages = adapter()
    assist.service.overview = Mock(return_value={
        "attention_complete": False,
        "items": [
            {"title": "Synthetic clarification", "stage": "clarify", "next_action": "",
             "attention_eligible": False, "definition": {"complete": False}},
            {"title": "Synthetic verification", "stage": "verify", "next_action": "Check the result",
             "attention_eligible": False, "definition": {"complete": True}},
        ],
    })
    assert telegram.handle(text="/tasks", event="update:71", today=TODAY)
    assert "Synthetic clarification" in messages[0][0]
    assert "Synthetic verification" in messages[0][0]
    assert "inventory is incomplete" in messages[0][0]
    assist.chat.assert_not_called()
    assist.step.assert_not_called()


def test_proposal_exact_binding_and_russian_approval_share_existing_decide():
    telegram, assist, messages = adapter()
    proposal = assist.service.refine(refine_payload(), TODAY)
    assist.chat.return_value["cards"] = [{
        "kind": "proposal", "proposal_id": proposal["id"], "action_digest": proposal["approval_digest"],
        "title": "Synthetic edit", "summary": "Review the field", "action": "refine",
        "task": {"path": PATH, "revision": REVISION},
        "diff": [{"field": "next_action", "before": "Old", "after": "New"}],
    }]
    telegram.handle(text="Refine this task", event="update:7", today=TODAY)
    assert messages[0][1][0][0]["callback_data"] == "brief1|approve|" + proposal["id"]
    assert assist.service.store.read()["messages"]["101"] == proposal["id"]
    assist.service.decide = Mock(return_value={"status": "submitted"})
    assist.service.loop._receipt_text = Mock(return_value="Submitted, not saved yet")
    telegram.handle(text="да", event="update:8", today=TODAY)
    assert assist.service.decide.call_args.args[0] == {
        "proposal_id": proposal["id"], "approval_digest": proposal["approval_digest"], "decision": "approve",
    }
    assert "next_action: Old → New" in messages[0][0]


def test_reply_to_other_message_cannot_approve_latest_proposal():
    telegram, assist, _ = adapter()
    assist.store.state = {"telegram": {"bindings": {}, "events": {}, "latest": {
        "proposal_id": "a" * 24, "approval_digest": "b" * 64,
    }}}
    assert not telegram.handle(text="да", event="update:8", today=TODAY, reply_to=999)
    assist.chat.assert_not_called()


def test_explicit_note_reply_does_not_become_a_task_edit():
    telegram, assist, _ = adapter()
    assist.store.state = {"telegram": {"bindings": {"101": {
        "thread_id": "synthetic-thread", "task": {"path": PATH, "revision": REVISION},
    }}, "events": {}, "latest": None}}
    assert not telegram.handle(text="/note a task observation", event="update:8", today=TODAY, reply_to=101)
    assist.chat.assert_not_called()


def test_undo_button_requires_actual_confirmed_message_binding():
    telegram, assist, _ = adapter()
    assist.undo.return_value = assist.chat.return_value
    assist.store.state = {"telegram": {"bindings": {"101": {
        "thread_id": assist.chat.return_value["thread_id"], "receipt_id": "a" * 24,
    }}, "events": {}, "latest": None}}
    with pytest.raises(TaskError, match="task_proposal_message_mismatch"):
        telegram.callback(message_id=102, data="assist1|undo|" + "a" * 24, event="update:9", today=TODAY)
    telegram.callback(message_id=101, data="assist1|undo|" + "a" * 24, event="update:9", today=TODAY)
    assert assist.undo.call_count == 1


def test_pending_telegram_change_can_refresh_before_exposing_undo():
    telegram, assist, messages = adapter()
    assist.chat.return_value["cards"] = [{
        "kind": "applied", "receipt_id": "a" * 24, "summary": "Submitted, not saved yet.",
        "saved": False, "undo_available": False, "status": "submitted",
        "task": {"path": PATH, "revision": REVISION}, "diff": [],
    }]
    assist.receipt.return_value = {
        **assist.chat.return_value, "cards": [{
            "kind": "applied", "receipt_id": "a" * 24, "summary": "Canonical update confirmed.",
            "saved": True, "undo_available": True, "status": "completed",
            "task": {"path": PATH, "revision": "b" * 40}, "diff": [],
        }],
    }
    telegram.handle(text="Refine this task", event="update:7", today=TODAY)
    assert messages[0][1][0][0]["callback_data"] == "assist1|receipt|" + "a" * 24
    telegram.callback(message_id=101, data="assist1|receipt|" + "a" * 24, event="update:8", today=TODAY)
    assert messages[1][1][0][0]["callback_data"] == "assist1|undo|" + "a" * 24


def test_multiple_task_cards_cannot_silently_bind_reply_to_last_task():
    telegram, assist, _ = adapter()
    assist.chat.return_value["cards"] = [
        {"kind": "task", "path": PATH, "revision": REVISION, "title": "Synthetic first task"},
        {"kind": "task", "path": "tasks/2026-10-13-other.md", "revision": "b" * 40,
         "title": "Synthetic second task"},
    ]
    telegram.handle(text="List my tasks", event="update:7", today=TODAY)
    assert "task" not in assist.store.state["telegram"]["bindings"]["101"]


def test_applied_revision_binding_is_not_downgraded_by_old_task_card():
    telegram, assist, _ = adapter()
    assist.chat.return_value["cards"] = [
        {"kind": "applied", "receipt_id": "a" * 24, "summary": "Canonical update confirmed.",
         "saved": True, "undo_available": True, "status": "completed",
         "task": {"path": PATH, "revision": "b" * 40}, "diff": []},
        {"kind": "task", "path": PATH, "revision": REVISION, "title": "Synthetic task"},
    ]
    telegram.handle(text="Refine this task", event="update:7", today=TODAY)
    assert assist.store.state["telegram"]["bindings"]["101"]["task"] == {
        "path": PATH, "revision": "b" * 40,
    }


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("MINDME_ACTION_BRIEFING_ENABLED", "true")
    monkeypatch.setenv("MINDME_TASKS_ENABLED", "true")
    monkeypatch.setenv("MINDME_ASSIST_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "7")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "synthetic")
    monkeypatch.setattr(fa, "_telegram_send", Mock())
    monkeypatch.setattr(fa, "_claim_onboarding", lambda: False)


def message(text="Create a task to write a synthetic outline", sender=7):
    return {"message_id": 70, "chat": {"id": 7, "type": "private"}, "from": {"id": sender}, "text": text}


def test_owner_task_text_routes_to_assist_not_memex(monkeypatch, enabled):
    engine = Mock()
    engine.handle.return_value = True
    monkeypatch.setattr(fa, "_assist_telegram", lambda: engine)
    forward = Mock()
    monkeypatch.setattr(fa, "_forward_to_memex", forward)
    response = fa.telegram_webhook(request({"update_id": 700, "message": message()}))
    assert response.status_code == 200
    assert engine.handle.call_args.kwargs["text"].startswith("Create a task")
    forward.assert_not_called()


def test_nonowner_message_cannot_enter_assist(monkeypatch, enabled):
    factory = Mock()
    monkeypatch.setattr(fa, "_assist_telegram", factory)
    assert not fa._task_assist_message(message(sender=8), "Create a task", "update:1")
    factory.assert_not_called()


def test_forwarded_task_message_cannot_supply_owner_attestation(monkeypatch, enabled):
    factory = Mock()
    monkeypatch.setattr(fa, "_assist_telegram", factory)
    forwarded = {**message("Done with this task"), "forward_origin": {"type": "hidden_user"}}
    assert fa._task_assist_message(forwarded, "Done with this task", "update:700:70:None")
    factory.assert_not_called()
    assert "not your confirmation" in fa._telegram_send.call_args.args[1]


def test_owner_voice_routes_transcript_without_archiving_assist_audio(monkeypatch, enabled):
    assist = Mock()
    assist.voice.return_value = {"text": "Создай задачу написать план", "language": "ru", "duration_s": 1}
    engine = Mock()
    engine.handle.return_value = True
    monkeypatch.setattr(fa, "_task_assist", lambda: assist)
    monkeypatch.setattr(fa, "_assist_telegram", lambda: engine)
    monkeypatch.setattr(fa, "_download_assist_audio", lambda _: b"synthetic")
    forward = Mock()
    monkeypatch.setattr(fa, "_forward_to_memex", forward)
    payload = message("")
    payload["voice"] = {"file_id": "synthetic", "mime_type": "audio/ogg", "duration": 1, "file_size": 100}
    response = fa.telegram_webhook(request({"update_id": 700, "message": payload}))
    assert response.status_code == 200
    assert engine.handle.call_args.kwargs["voice"] is True
    assert engine.handle.call_args.kwargs["text"] == assist.voice.return_value["text"]
    forward.assert_not_called()


def test_assist_audio_error_never_falls_back_to_generic_capture(monkeypatch, enabled):
    assist = Mock()
    assist.voice.side_effect = TaskError("budget_exhausted")
    monkeypatch.setattr(fa, "_task_assist", lambda: assist)
    monkeypatch.setattr(fa, "_download_assist_audio", lambda _: b"synthetic")
    forward = Mock()
    monkeypatch.setattr(fa, "_forward_to_memex", forward)
    payload = message("")
    payload["voice"] = {"file_id": "synthetic", "mime_type": "audio/ogg", "duration": 1}
    response = fa.telegram_webhook(request({"update_id": 700, "message": payload}))
    assert response.status_code == 200 and "spending limit" in fa._telegram_send.call_args.args[1]
    forward.assert_not_called()
