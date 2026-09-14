from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

import function_app as fa
from test_briefing_webhook import request
from vault_evolve import EvolveError


@pytest.fixture
def owner(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "7")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "synthetic")
    monkeypatch.setenv("MINDME_DAILY_EVOLVE_ENABLED", "true")
    monkeypatch.setenv("MINDME_ACTION_BRIEFING_ENABLED", "false")
    loop = Mock()
    loop.target.return_value = ("2026-09-14", "F1")
    loop.feedback.return_value = "Scoped feedback saved."
    sent = Mock()
    forwarded = Mock(return_value=True)
    monkeypatch.setattr(fa, "_evolve_loop", lambda: loop)
    monkeypatch.setattr(fa, "_telegram_send", sent)
    monkeypatch.setattr(fa, "_forward_to_memex", forwarded)
    return loop, sent, forwarded


def test_only_the_enrolled_owner_can_use_review_buttons(owner):
    loop, sent, forwarded = owner
    response = fa.telegram_webhook(request({
        "callback_query": {"data": "evolve1|known|2026-09-14|F1", "message": {"chat": {"id": 8}, "message_id": 9}},
    }))
    assert response.status_code == 200
    loop.feedback.assert_not_called()
    forwarded.assert_not_called()
    sent.assert_not_called()


def test_review_callback_is_bound_to_its_actual_delivered_message(owner):
    loop, sent, forwarded = owner
    response = fa.telegram_webhook(request({
        "callback_query": {"data": "evolve1|known|2026-09-14|F1", "message": {"chat": {"id": 7}, "message_id": 9}},
    }))
    assert response.status_code == 200
    loop.target.assert_called_once_with(9)
    assert loop.feedback.call_args.args[:3] == ("2026-09-14", "F1", "Already familiar")
    forwarded.assert_not_called()
    sent.assert_called_once_with(7, "Scoped feedback saved.")


def test_rebound_review_button_cannot_record_feedback_for_a_different_day(owner):
    loop, _, _ = owner
    response = fa.telegram_webhook(request({
        "callback_query": {"data": "evolve1|known|2026-09-15|F1", "message": {"chat": {"id": 7}, "message_id": 9}},
    }))
    assert response.status_code == 503
    loop.feedback.assert_not_called()


def test_text_reply_is_saved_before_generic_capture_or_companion(owner):
    loop, _, forwarded = owner
    response = fa.telegram_webhook(request({
        "message": {"chat": {"id": 7}, "text": "This comparison is already familiar", "reply_to_message": {"message_id": 9}},
    }))
    assert response.status_code == 200
    assert loop.feedback.call_args.args[2] == "This comparison is already familiar"
    forwarded.assert_not_called()


def test_voice_review_feedback_uses_the_same_binding_without_forwarding(monkeypatch, owner):
    loop, _, forwarded = owner
    monkeypatch.setattr(fa, "_download_telegram_file", lambda value: b"synthetic")
    monkeypatch.setattr(fa, "_transcribe_voice", lambda content, mime: "Explain the comparison instead")
    response = fa.telegram_webhook(request({
        "message": {"chat": {"id": 7}, "voice": {"file_id": "synthetic"}, "reply_to_message": {"message_id": 9}},
    }))
    assert response.status_code == 200
    assert loop.feedback.call_args.args[2] == "Explain the comparison instead"
    forwarded.assert_not_called()


def test_failed_voice_transcription_of_review_reply_never_becomes_a_capture(monkeypatch, owner):
    loop, sent, forwarded = owner
    monkeypatch.setattr(fa, "_download_telegram_file", lambda value: b"synthetic")
    monkeypatch.setattr(fa, "_transcribe_voice", lambda content, mime: None)
    response = fa.telegram_webhook(request({
        "message": {"chat": {"id": 7}, "voice": {"file_id": "synthetic"}, "reply_to_message": {"message_id": 9}},
    }))
    assert response.status_code == 503
    forwarded.assert_not_called()
    loop.feedback.assert_not_called()
    assert "not saved as a separate capture" in sent.call_args.args[1]


def test_capture_callbacks_still_forward_to_memex(owner):
    loop, _, forwarded = owner
    response = fa.telegram_webhook(request({
        "callback_query": {"data": "save|mindMe|draft", "message": {"chat": {"id": 7}, "message_id": 9}},
    }))
    assert response.status_code == 200
    forwarded.assert_called_once()
    loop.feedback.assert_not_called()


def test_review_model_is_bounded_has_no_tools_and_does_not_store_conversation(monkeypatch):
    client = Mock()
    client.with_options.return_value = client
    http = Mock()
    monkeypatch.setattr(fa, "_http_client", lambda: http)
    client.responses.create.return_value.output_text = json.dumps({"findings": []})
    monkeypatch.setenv("MINDME_BRIEFING_MODEL", "existing-model")
    monkeypatch.setattr(fa, "_foundry", lambda: (None, client))
    fa._generate_evolve_review({"sources": []})
    client.with_options.assert_called_once_with(timeout=45.0, max_retries=0, http_client=http)
    args = client.responses.create.call_args.kwargs
    assert args["model"] == "existing-model"
    assert args["store"] is False and "tools" not in args
    assert args["max_output_tokens"] == 2400
    assert all(message["type"] == "message" for message in args["input"])
    assert args["text"]["format"]["schema"]["properties"]["findings"]["maxItems"] == 0


def test_existing_timer_runs_review_once_without_adding_a_schedule(monkeypatch, owner):
    loop, _, _ = owner
    original = Mock()
    monkeypatch.setattr(fa, "_deliver_morning_briefing", original)
    monkeypatch.setattr(fa, "_briefing_prefs", lambda: ["knowledge"])
    fa.morning_briefing_timer(Mock())
    original.assert_called_once()
    loop.run.assert_called_once()


def test_switching_off_knowledge_preserves_the_users_section_preference(monkeypatch, owner):
    loop, _, _ = owner
    monkeypatch.setattr(fa, "_deliver_morning_briefing", Mock())
    monkeypatch.setattr(fa, "_briefing_prefs", lambda: ["weather"])
    fa.morning_briefing_timer(Mock())
    loop.run.assert_not_called()


def test_failure_of_new_review_does_not_prevent_the_existing_briefing(monkeypatch, owner):
    loop, sent, _ = owner
    loop.run.side_effect = EvolveError("synthetic_failure")
    original = Mock()
    monkeypatch.setattr(fa, "_deliver_morning_briefing", original)
    monkeypatch.setattr(fa, "_briefing_prefs", lambda: ["knowledge"])
    with pytest.raises(RuntimeError, match="Morning delivery incomplete"):
        fa.morning_briefing_timer(Mock())
    original.assert_called_once()
    assert "could not be completed" in sent.call_args.args[1]


def test_existing_briefing_failure_does_not_prevent_independent_review(monkeypatch, owner):
    loop, _, _ = owner
    monkeypatch.setattr(fa, "_deliver_morning_briefing", Mock(side_effect=RuntimeError("synthetic_failure")))
    monkeypatch.setattr(fa, "_briefing_prefs", lambda: ["knowledge"])
    with pytest.raises(RuntimeError, match="Morning delivery incomplete"):
        fa.morning_briefing_timer(Mock())
    loop.run.assert_called_once()
