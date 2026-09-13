from __future__ import annotations

import json
from unittest.mock import Mock

import azure.functions as func
import pytest
import httpx

import function_app as fa


def request(payload):
    return func.HttpRequest(
        method="POST", url="https://synthetic.example/api/telegram_webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "synthetic"},
        body=json.dumps(payload).encode(),
    )


@pytest.fixture
def owner(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "7")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "synthetic")
    monkeypatch.setenv("MINDME_ACTION_BRIEFING_ENABLED", "true")
    sent = Mock()
    loop = Mock()
    loop.target.return_value = "a" * 24
    loop.reply.return_value = "Decision saved."
    monkeypatch.setattr(fa, "_telegram_send", sent)
    monkeypatch.setattr(fa, "_briefing_loop", lambda: loop)
    return loop, sent


def test_text_reply_is_bound_before_capture_routing(monkeypatch, owner):
    loop, sent = owner
    forwarded = Mock()
    monkeypatch.setattr(fa, "_forward_to_memex", forwarded)
    response = fa.telegram_webhook(request({
        "message": {"chat": {"id": 7}, "text": "yes", "reply_to_message": {"message_id": 91}},
    }))
    assert response.status_code == 200
    loop.target.assert_called_once_with(91)
    assert loop.reply.call_args.args[:2] == ("a" * 24, "yes")
    forwarded.assert_not_called()
    sent.assert_called_once_with(7, "Decision saved.")


def test_voice_reply_does_not_become_an_unrelated_note(monkeypatch, owner):
    loop, _ = owner
    forwarded = Mock()
    monkeypatch.setattr(fa, "_forward_to_memex", forwarded)
    monkeypatch.setattr(fa, "_download_telegram_file", lambda file_id: b"synthetic-audio")
    monkeypatch.setattr(fa, "_transcribe_voice", lambda data, mime: "snooze 2026-10-01")
    response = fa.telegram_webhook(request({
        "message": {
            "chat": {"id": 7}, "voice": {"file_id": "voice"},
            "reply_to_message": {"message_id": 91},
        },
    }))
    assert response.status_code == 200
    assert loop.reply.call_args.args[1] == "snooze 2026-10-01"
    forwarded.assert_not_called()


def test_unbound_yes_asks_for_a_target_without_calling_the_agent(monkeypatch, owner):
    loop, sent = owner
    companion = Mock()
    monkeypatch.setattr(fa, "_ask_companion", companion)
    response = fa.telegram_webhook(request({"message": {"chat": {"id": 7}, "text": "yes"}}))
    assert response.status_code == 200
    assert "specific proposal" in sent.call_args.args[1]
    loop.reply.assert_not_called()
    companion.assert_not_called()


def test_old_note_review_callbacks_still_go_to_memex(monkeypatch, owner):
    loop, _ = owner
    forwarded = Mock(return_value=True)
    monkeypatch.setattr(fa, "_forward_to_memex", forwarded)
    response = fa.telegram_webhook(request({
        "callback_query": {"message": {"chat": {"id": 7}, "message_id": 91}, "data": "save|mindMe|draft"},
    }))
    assert response.status_code == 200
    forwarded.assert_called_once()
    loop.reply.assert_not_called()


def test_other_chat_cannot_approve_a_personal_proposal(monkeypatch, owner):
    loop, sent = owner
    response = fa.telegram_webhook(request({
        "callback_query": {"message": {"chat": {"id": 8}, "message_id": 91}, "data": "brief1|approve|" + "a" * 24},
    }))
    assert response.status_code == 200
    loop.reply.assert_not_called()
    sent.assert_not_called()


def test_memory_delete_uses_the_private_control_not_companion(monkeypatch, owner):
    loop, sent = owner
    loop.memory_command.return_value = "Memory removed."
    response = fa.telegram_webhook(request({
        "message": {"chat": {"id": 7}, "text": "/memory forget " + "b" * 24},
    }))
    assert response.status_code == 200
    loop.memory_command.assert_called_once_with(" forget " + "b" * 24)
    sent.assert_called_once_with(7, "Memory removed.")


def test_model_has_no_tools_and_has_explicit_cost_and_privacy_limits(monkeypatch):
    client = Mock()
    client.responses.create.return_value.output_text = json.dumps({
        "focus": None, "changes": [], "proposal": None,
    })
    monkeypatch.setenv("MINDME_BRIEFING_MODEL", "existing-model")
    monkeypatch.setattr(fa, "_foundry", lambda: (None, client))
    fa._generate_action_plan({"date": "2026-09-13", "sources": []})
    arguments = client.responses.create.call_args.kwargs
    assert arguments["model"] == "existing-model"
    assert arguments["store"] is False
    assert arguments["max_output_tokens"] == 1600
    assert "tools" not in arguments
    assert "<<<DATA_" in arguments["input"][1]["content"]
    assert arguments["text"]["format"]["strict"] is True


def test_all_due_items_survive_the_memex_five_item_preview(monkeypatch):
    tasks = [
        {"path": f"tasks/task-{number}.md", "title": f"Task {number}", "review_on": "2026-09-13"}
        for number in range(12)
    ]
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(
        200, json={
            "ideas": {"open_count": 0, "oldest_age_days": 0, "items": []},
            "tasks": {"open_count": 12, "items": tasks[:5], "due_items": tasks},
            "complete": True, "metadata_version": 1,
        },
    )))
    monkeypatch.setenv("MEMEX_STATE_URL", "https://synthetic.example/state")
    monkeypatch.setattr(fa, "_http_client", lambda: client)
    with client:
        result = fa._fetch_open_loops()
    assert len(result["tasks"]["items"]) == 12
    assert result["tasks"]["items"] == tasks


def test_action_mode_tool_context_uses_canonical_goals_not_the_legacy_dashboard(monkeypatch, owner):
    loop, _ = owner
    monkeypatch.setattr(fa, "_briefing_prefs", lambda: ["goals"])
    monkeypatch.setattr(fa, "_build_briefing_snapshot", lambda: pytest.fail("Legacy dashboard must not be used"))
    loop.context.return_value = {
        "date": "2026-09-13", "revision": "a" * 40, "tasks": [],
        "goals": [{"text": "A confirmed synthetic goal"}], "warnings": [],
    }
    result = fa._load_briefing()
    assert result["top_goals"] == ["A confirmed synthetic goal"]
    assert "today_focus" not in result
    assert result["source_freshness"]["revision"] == "a" * 40
