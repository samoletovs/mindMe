from __future__ import annotations

import json
from unittest.mock import Mock

import httpx
import pytest

import function_app as fa
from briefing_sources import SourceError
from briefing_state import prune_state
from task_context import parse_task_callback, task_context
from task_state import workspace
from task_telegram import TaskTelegram
from test_briefing_webhook import request
from test_task_service import PATH, REVISION, TODAY, refine_payload, service


def pointer():
    return {
        "version": 1, "status": "matched", "publication_status": "submitted",
        "action_id": "a" * 32, "path": PATH, "pr_url": "https://github.com/example/mindVault/pull/7",
        "source_revision": REVISION, "clarification_limit": 3,
    }


def test_task_context_sends_actual_owner_and_message_not_source_text():
    requests = []

    def lookup(req):
        requests.append(json.loads(req.content))
        return httpx.Response(200, json=pointer())

    result = task_context(
        httpx.Client(transport=httpx.MockTransport(lookup)), url="https://synthetic.example/api/dump/mindme",
        repo="example/mindVault", chat_id=7, owner_id=7, message_id=71, task_key="a" * 32,
    )
    assert result == pointer()
    assert requests == [{
        "operation": "task_context", "version": 1, "chat_id": 7, "owner_id": 7,
        "message_id": 71, "task_key": "a" * 32,
    }]
    assert parse_task_callback("task1|clarify|" + "a" * 32) == "a" * 32
    assert parse_task_callback("task1|approve|" + "a" * 32) is None


@pytest.mark.parametrize("change", [
    {"action_id": "b" * 32}, {"source_revision": "commit-not-a-blob"},
    {"path": "tasks/done/closed.md"}, {"pr_url": "https://github.com/example/other/pull/7"},
    {"clarification_limit": 5}, {"source_text": "Caller supplied prose"},
    {"publication_status": "merged"},
])
def test_task_context_rejects_mismatched_or_expanded_binding(change):
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={**pointer(), **change})))
    with pytest.raises(SourceError, match="task_context_invalid"):
        task_context(
            client, url="https://synthetic.example/api/dump/mindme", repo="example/mindVault",
            chat_id=7, owner_id=7, message_id=71, task_key="a" * 32,
        )


def adapter(*, complete=False):
    subject, repository, execute, generate, publish, blob = service(complete=complete, stage="clarify")
    messages = []

    def send(text, keyboard):
        messages.append((text, keyboard))
        return 100 + len(messages)

    lookup = Mock(return_value=pointer())
    telegram = TaskTelegram(subject, lookup=lookup, send=send)
    return telegram, subject, repository, execute, messages, lookup


def test_callback_and_three_bound_answers_prepare_only_one_exact_proposal():
    telegram, subject, _, execute, messages, _ = adapter()
    assert telegram.handle(message_id=71, text="clarify", task_key="a" * 32, event="update:1", today=TODAY)
    assert "What should be different" in messages[-1][0]
    for index, answer in enumerate(["One outline exists.", "Write one heading.", "Read the saved outline."]):
        target = 100 + len(messages)
        telegram.handle(message_id=target, text=answer, event=f"update:{index + 2}", today=TODAY)
    records = list(subject.store.read()["proposals"].values())
    assert len(records) == 1 and records[0]["status"] == "pending"
    assert records[0]["action"]["changes"]["stage"] == "clarify"
    assert records[0]["message_ids"] == [100 + len(messages)]
    assert messages[-1][1][0][0]["callback_data"].startswith("brief1|approve|")
    execute.assert_not_called()


def test_duplicate_callback_and_webhook_do_not_repeat_question_or_answer():
    telegram, subject, _, _, messages, _ = adapter()
    arguments = {"message_id": 71, "text": "clarify", "task_key": "a" * 32, "today": TODAY}
    telegram.handle(**arguments, event="update:1")
    telegram.handle(**arguments, event="update:1")
    telegram.handle(**arguments, event="update:2")
    assert len(messages) == 1
    telegram.handle(message_id=101, text="One outline exists.", event="update:3", today=TODAY)
    telegram.handle(message_id=101, text="One outline exists.", event="update:3", today=TODAY)
    assert len(messages) == 2
    assert next(iter(workspace(subject.store.read())["clarifications"].values()))["turns"] == 1


def test_reply_to_previous_question_cannot_fill_the_next_field():
    telegram, subject, _, _, messages, _ = adapter()
    telegram.handle(message_id=71, text="clarify", task_key="a" * 32, event="update:1", today=TODAY)
    telegram.handle(message_id=101, text="One outline exists.", event="update:2", today=TODAY)
    telegram.handle(message_id=101, text="Different outcome.", event="update:3", today=TODAY)
    record = next(iter(workspace(subject.store.read())["clarifications"].values()))
    assert record["turns"] == 1 and record["changes"] == {"outcome": "One outline exists."}
    assert "no longer current" in messages[-1][0]


def test_pending_publication_or_changed_blob_does_not_fabricate_canonical_task():
    telegram, subject, repository, execute, messages, _ = adapter()
    repository.records[PATH]["revision"] = "f" * 40
    telegram.handle(message_id=71, text="clarify", task_key="a" * 32, event="update:1", today=TODAY)
    assert "awaiting canonical" in messages[0][0]
    assert not subject.store.read()["proposals"]
    execute.assert_not_called()


def test_uncertain_question_delivery_does_not_silently_retry():
    telegram, subject, _, _, _, _ = adapter()
    telegram.send = Mock(side_effect=fa.TelegramDeliveryError("synthetic"))
    args = {"message_id": 71, "text": "clarify", "task_key": "a" * 32, "event": "update:1", "today": TODAY}
    with pytest.raises(fa.TelegramDeliveryError):
        telegram.handle(**args)
    assert telegram.handle(**args)
    assert telegram.send.call_count == 1
    assert workspace(subject.store.read())["bindings"] == {}


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("MINDME_ACTION_BRIEFING_ENABLED", "true")
    monkeypatch.setenv("MINDME_TASKS_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "7")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "synthetic")
    monkeypatch.setattr(fa, "_telegram_send", Mock())


def message(text="/task Write a synthetic outline"):
    return {
        "message_id": 70, "chat": {"id": 7, "type": "private"},
        "from": {"id": 7}, "text": text,
    }


def test_original_task_update_and_owner_ids_survive_forwarding(monkeypatch, enabled):
    forward = Mock(return_value=True)
    monkeypatch.setattr(fa, "_forward_to_memex", forward)
    payload = {"update_id": 700, "message": message()}
    response = fa.telegram_webhook(request(payload))
    assert response.status_code == 200
    assert forward.call_args.args == (payload,)


@pytest.mark.parametrize("sender", [{}, {"id": 8}])
def test_new_task_has_no_owner_fallback(monkeypatch, enabled, sender):
    forward = Mock()
    monkeypatch.setattr(fa, "_forward_to_memex", forward)
    response = fa.telegram_webhook(request({"update_id": 700, "message": {**message(), "from": sender}}))
    assert response.status_code == 400
    forward.assert_not_called()


def test_task_callback_routes_before_generic_memex_capture(monkeypatch, enabled):
    telegram = Mock()
    telegram.handle.return_value = True
    monkeypatch.setattr(fa, "_task_telegram", lambda: telegram)
    forward = Mock()
    monkeypatch.setattr(fa, "_forward_to_memex", forward)
    response = fa.telegram_webhook(request({
        "update_id": 701, "callback_query": {
            "from": {"id": 7}, "message": message("Synthetic saved task"),
            "data": "task1|clarify|" + "a" * 32,
        },
    }))
    assert response.status_code == 200
    telegram.handle.assert_called_once()
    assert telegram.handle.call_args.kwargs["message_id"] == 70
    forward.assert_not_called()


def test_forged_task_callback_sender_cannot_use_owner_message(monkeypatch, enabled):
    telegram = Mock()
    monkeypatch.setattr(fa, "_task_telegram", telegram)
    response = fa.telegram_webhook(request({
        "update_id": 701, "callback_query": {
            "from": {"id": 8}, "message": message("Synthetic saved task"),
            "data": "task1|clarify|" + "a" * 32,
        },
    }))
    assert response.status_code == 400
    telegram.assert_not_called()


def test_callback_without_sender_has_no_message_sender_fallback(monkeypatch, enabled):
    telegram = Mock()
    monkeypatch.setattr(fa, "_task_telegram", telegram)
    response = fa.telegram_webhook(request({
        "update_id": 701, "callback_query": {
            "message": message("Synthetic saved task"), "data": "task1|clarify|" + "a" * 32,
        },
    }))
    assert response.status_code == 400
    telegram.assert_not_called()


def test_unmatched_task_callback_never_falls_through_to_generic_memex(monkeypatch, enabled):
    telegram = Mock()
    telegram.handle.return_value = False
    monkeypatch.setattr(fa, "_task_telegram", lambda: telegram)
    forward = Mock()
    monkeypatch.setattr(fa, "_forward_to_memex", forward)
    response = fa.telegram_webhook(request({
        "update_id": 701, "callback_query": {
            "from": {"id": 7}, "message": message("Synthetic saved task"),
            "data": "task1|clarify|" + "a" * 32,
        },
    }))
    assert response.status_code == 200
    forward.assert_not_called()


def test_task_reply_with_link_stays_bound_instead_of_becoming_capture(monkeypatch, enabled):
    telegram = Mock()
    telegram.handle.return_value = True
    monkeypatch.setattr(fa, "_task_telegram", lambda: telegram)
    forward = Mock()
    monkeypatch.setattr(fa, "_forward_to_memex", forward)
    response = fa.telegram_webhook(request({
        "update_id": 702, "message": {
            **message("Review the example at https://example.org"),
            "reply_to_message": {"message_id": 71},
        },
    }))
    assert response.status_code == 200
    telegram.handle.assert_called_once()
    forward.assert_not_called()


def test_source_removed_task_card_returns_stale_receipt_without_keyerror_or_dispatch():
    telegram, subject, _, execute, messages, _ = adapter(complete=True)
    proposal = subject.refine(refine_payload(), TODAY)

    def bind(state):
        state["messages"]["71"] = proposal["id"]
        state["proposals"][proposal["id"]]["message_ids"] = [71]

    subject.store.update(bind)
    subject.store.update(lambda state: prune_state(state, inventory_paths=set(), today=TODAY))
    assert telegram.proposal(71, proposal["id"], "approve", "update:1", TODAY)
    assert telegram.proposal(71, proposal["id"], "approve", "update:1", TODAY)
    assert len(messages) == 1
    assert "no longer available" in messages[0][0] and "No action was started" in messages[0][0]
    execute.assert_not_called()
