from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from openai import APIConnectionError, APITimeoutError, InternalServerError

import function_app as fa
from execution_budget import BudgetExceeded, execution_budget
from test_briefing_webhook import request
from vault_evolve import EvolveError

PRIVATE = "PRIVATE-PACKET-SENTENCE"


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


def answer(text='{"findings": []}', *, status="completed", reason=None, refusal=False):
    return SimpleNamespace(
        status=status, output_text=text,
        incomplete_details=SimpleNamespace(reason=reason) if reason else None,
        output=[SimpleNamespace(type="message", content=[SimpleNamespace(
            type="refusal" if refusal else "output_text", refusal=PRIVATE,
        )])],
        usage=SimpleNamespace(
            input_tokens=900, output_tokens=2400,
            output_tokens_details=SimpleNamespace(reasoning_tokens=2100),
        ),
    )


def cut_off():
    return answer("", status="incomplete", reason="max_output_tokens")


def timeout_error():
    return APITimeoutError(request=httpx.Request("POST", "https://synthetic.example/responses"))


def server_error():
    response = httpx.Response(500, request=httpx.Request("POST", "https://synthetic.example/responses"))
    return InternalServerError("synthetic", response=response, body=None)


@pytest.fixture
def review_model(monkeypatch):
    client = Mock()
    client.with_options.return_value = client
    monkeypatch.setenv("MINDME_BRIEFING_MODEL", "existing-model")
    monkeypatch.setattr(fa, "_foundry", lambda: (None, client))
    monkeypatch.setattr(fa, "_http_client", lambda: Mock())
    return client


def system_texts(client):
    return [call.kwargs["input"][0]["content"] for call in client.responses.create.call_args_list]


def output_caps(client):
    return [call.kwargs["max_output_tokens"] for call in client.responses.create.call_args_list]


def test_a_review_cut_off_at_the_cap_is_retried_with_a_shorter_brief_and_more_room(review_model):
    # 2026-11-11..14: reviews failed as a bare "invalid_model_review" with no cause recorded.
    review_model.responses.create.side_effect = [cut_off(), answer()]
    assert fa._generate_evolve_review({"sources": []}) == {"findings": []}
    first, second = system_texts(review_model)
    assert second.startswith(first) and "Keep this answer short" in second
    assert "Keep this answer short" not in first
    assert output_caps(review_model) == [2400, 4000]


def test_a_review_that_never_completes_fails_with_its_cause_after_three_calls(review_model):
    review_model.responses.create.side_effect = [cut_off(), cut_off(), cut_off()]
    with pytest.raises(EvolveError, match="^review_output_truncated$"):
        fa._generate_evolve_review({"sources": []})
    assert review_model.responses.create.call_count == 3
    assert "Keep this answer very short" in system_texts(review_model)[2]
    assert output_caps(review_model) == [2400, 4000, 6000]


@pytest.mark.parametrize(("reply", "code"), [
    (answer("", status="incomplete", reason="content_filter"), "review_content_filtered"),
    (answer("", refusal=True), "review_model_refused"),
])
def test_a_filtered_or_refused_review_is_reported_once_and_not_repeated(review_model, reply, code):
    review_model.responses.create.side_effect = [reply, answer()]
    with pytest.raises(EvolveError, match=f"^{code}$"):
        fa._generate_evolve_review({"sources": []})
    assert review_model.responses.create.call_count == 1


def test_a_failed_response_is_retried_then_reported_as_a_service_failure(review_model):
    review_model.responses.create.side_effect = [answer("", status="failed")] * 3
    with pytest.raises(EvolveError, match="^review_model_failed$"):
        fa._generate_evolve_review({"sources": []})
    assert review_model.responses.create.call_count == 3


@pytest.mark.parametrize("text", ["{not json", "[]", ""])
def test_an_unusable_text_from_a_completed_response_is_retried(review_model, text):
    review_model.responses.create.side_effect = [answer(text), answer()]
    assert fa._generate_evolve_review({"sources": []}) == {"findings": []}
    assert review_model.responses.create.call_count == 2


def test_unusable_text_keeps_its_original_failure_code(review_model):
    review_model.responses.create.side_effect = [answer("{not json")] * 3
    with pytest.raises(EvolveError, match="^invalid_model_review$"):
        fa._generate_evolve_review({"sources": []})


@pytest.mark.parametrize("error", [timeout_error(), server_error()])
def test_a_connection_failure_is_retried_without_shortening_the_brief(review_model, error):
    review_model.responses.create.side_effect = [error, answer()]
    assert fa._generate_evolve_review({"sources": []}) == {"findings": []}
    first, second = system_texts(review_model)
    assert first == second
    assert output_caps(review_model) == [2400, 2400]


def test_a_review_that_cannot_connect_raises_the_connection_error_after_three_calls(review_model):
    review_model.responses.create.side_effect = [timeout_error(), server_error(), timeout_error()]
    with pytest.raises(APITimeoutError):
        fa._generate_evolve_review({"sources": []})
    assert review_model.responses.create.call_count == 3


def test_review_retries_stop_before_they_could_starve_the_run(review_model):
    review_model.responses.create.side_effect = [cut_off(), answer()]
    with pytest.raises(EvolveError, match="^review_output_truncated$"), execution_budget(20):
        fa._generate_evolve_review({"sources": []})
    assert review_model.responses.create.call_count == 1


def test_each_attempt_logs_what_the_service_said_but_never_the_text(review_model, caplog):
    review_model.responses.create.side_effect = [answer(PRIVATE, status="incomplete", reason="max_output_tokens"), answer()]
    with caplog.at_level("INFO", logger="mindMe.harness"):
        fa._generate_evolve_review({"sources": []})
    assert (
        "attempt=1 status=incomplete reason=max_output_tokens refused=no "
        "input_tokens=900 output_tokens=2400 reasoning_tokens=2100"
    ) in caplog.text
    assert "attempt=2 status=completed" in caplog.text
    assert PRIVATE not in caplog.text


def test_a_spent_budget_is_a_nameable_failure():
    assert fa._failure_code(BudgetExceeded()) == "execution_budget_exhausted"


def test_a_budget_spent_inside_a_model_call_is_still_nameable():
    sdk_request = httpx.Request("POST", "https://example.invalid/responses")
    with pytest.raises(APIConnectionError) as wrapped:
        try:
            raise BudgetExceeded()
        except BudgetExceeded as spent:
            raise APIConnectionError(request=sdk_request) from spent
    assert fa._failure_code(wrapped.value) == "execution_budget_exhausted"
    assert fa._failure_reason(wrapped.value) == " (execution_budget_exhausted)"
    assert fa._failure_code(APIConnectionError(request=sdk_request)) == "-"


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


def test_a_failed_review_logs_which_check_failed(monkeypatch, owner, caplog):
    # Three silent failures (09-30, 10-06, 10-07) logged only "EvolveError", which
    # cannot tell a source outage from a rejected model answer.
    loop, _, _ = owner
    loop.run.side_effect = EvolveError("review_sources_unavailable")
    monkeypatch.setattr(fa, "_deliver_morning_briefing", Mock())
    monkeypatch.setattr(fa, "_briefing_prefs", lambda: ["knowledge"])
    with caplog.at_level("ERROR", logger="mindMe.harness"), pytest.raises(RuntimeError):
        fa.morning_briefing_timer(Mock())
    assert "error=EvolveError reason=review_sources_unavailable" in caplog.text


def test_the_morning_notice_names_the_failed_check(monkeypatch, owner):
    loop, sent, _ = owner
    loop.run.side_effect = EvolveError("review_output_truncated")
    monkeypatch.setattr(fa, "_deliver_morning_briefing", Mock())
    monkeypatch.setattr(fa, "_briefing_prefs", lambda: ["knowledge"])
    with pytest.raises(RuntimeError, match="Morning delivery incomplete"):
        fa.morning_briefing_timer(Mock())
    assert "could not be completed (review_output_truncated). No successful" in sent.call_args.args[1]


def test_a_notice_does_not_guess_a_cause_it_cannot_state_safely(monkeypatch, owner):
    loop, sent, _ = owner
    loop.run.side_effect = httpx.ConnectError("https://api.telegram.org/bot123:secret/sendMessage")
    monkeypatch.setattr(fa, "_deliver_morning_briefing", Mock())
    monkeypatch.setattr(fa, "_briefing_prefs", lambda: ["knowledge"])
    with pytest.raises(RuntimeError, match="Morning delivery incomplete"):
        fa.morning_briefing_timer(Mock())
    assert "could not be completed. No successful" in sent.call_args.args[1]
    assert "secret" not in sent.call_args.args[1]


def test_the_evolve_command_notice_names_the_failed_check(owner):
    loop, sent, _ = owner
    loop.run.side_effect = EvolveError("review_output_truncated")
    response = fa.telegram_webhook(request({"message": {"chat": {"id": 7}, "text": "/evolve now"}}))
    assert response.status_code == 503
    assert "could not finish (review_output_truncated). I cannot confirm" in sent.call_args.args[1]


@pytest.mark.parametrize("exc", [
    EvolveError("Quote: a private sentence"),
    EvolveError("x" * 65),
    RuntimeError("timeout"),
    httpx.HTTPError("https://api.telegram.org/bot123:secret/sendMessage"),
])
def test_failure_codes_never_carry_content_or_foreign_messages(exc):
    assert fa._failure_code(exc) == "-"
