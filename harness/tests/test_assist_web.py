from unittest.mock import Mock
from io import BytesIO
from datetime import datetime, timezone
from types import SimpleNamespace
import wave

import azure.functions as func
import httpx
import pytest

from task_service import TaskError
from task_assist import TaskAssistError
from task_assist_state import AssistStoreError
from task_web import TaskWeb
from test_task_auth import identity
from test_task_web import data, logged_in, request
from test_task_service import service
from test_briefing_state import FakeBlob
import function_app as fa
from test_assist_audio import fmp4, webm


def web(auth, assist, **settings):
    return TaskWeb(
        env={"MINDME_WEB_ENABLED": "true", "MINDME_TASKS_ENABLED": "true",
             "MINDME_ASSIST_ENABLED": "true", **settings},
        auth=lambda: auth, service=Mock(), assist=lambda _: assist,
    )


@pytest.mark.parametrize("path", ["chat", "step", "voice", "undo", "change", "receipt"])
def test_assist_mutations_require_owner_cookie_and_csrf(logged_in, path):
    auth, cookie, csrf = logged_in
    assist = Mock()
    subject = web(auth, assist)
    for headers in ({}, {"cookie": cookie, "csrf": "forged"},
                    {"cookie": cookie, "csrf": csrf, "origin": "https://foreign.example"}):
        response = subject.handle(request("api/assist/" + path, {}, **headers), "api/assist/" + path)
        assert response.status_code in {401, 403}
    assert not assist.mock_calls


@pytest.mark.parametrize("operation", ["chat", "step", "undo", "change", "receipt"])
def test_json_assist_routes_preserve_payload_and_no_store(logged_in, operation):
    auth, cookie, csrf = logged_in
    assist = Mock()
    getattr(assist, operation).return_value = {"reply": "Synthetic reply"}
    payload = {"request_id": "2d1ded85-04a3-4ff2-9ff2-7eecae28f146"}
    response = web(auth, assist).handle(
        request("api/assist/" + operation, payload, cookie=cookie, csrf=csrf), "api/assist/" + operation,
    )
    assert response.status_code == 200 and data(response)["reply"] == "Synthetic reply"
    assert response.headers["Cache-Control"] == "no-store, private"
    assert getattr(assist, operation).call_args.args[0] == payload


def test_voice_passes_raw_audio_and_header_request_id_not_json(logged_in):
    auth, cookie, csrf = logged_in
    assist = Mock()
    assist.voice.return_value = {"text": "Synthetic task", "language": "en", "duration_s": 1}
    req = func.HttpRequest(
        method="POST", url=auth.config.origin + "/api/tasks/api/assist/voice",
        headers={"Cookie": cookie, "Origin": auth.config.origin, "X-MindMe-CSRF": csrf,
                 "Content-Type": "audio/webm;codecs=opus", "X-Request-Id": "synthetic-id"},
        body=b"synthetic-audio",
    )
    response = web(auth, assist).handle(req, "api/assist/voice")
    assert response.status_code == 200
    assert assist.voice.call_args.args[:3] == (b"synthetic-audio", "audio/webm", "synthetic-id")


def test_usage_is_authenticated_and_never_requires_a_mutation(logged_in):
    auth, cookie, _ = logged_in
    assist = Mock()
    assist.usage.return_value = {"month_eur": 0}
    assert web(auth, assist).handle(request("api/assist/usage"), "api/assist/usage").status_code == 401
    response = web(auth, assist).handle(request("api/assist/usage", cookie=cookie), "api/assist/usage")
    assert response.status_code == 200 and data(response) == {"month_eur": 0}
    assert assist.usage.call_count == 1


@pytest.mark.parametrize("code,status", [
    ("budget_exhausted", 402), ("rate_limited", 429), ("model_unavailable", 503),
    ("assist_request_conflict", 409), ("assist_audio_invalid", 422),
])
def test_assist_errors_are_stable_and_content_free(logged_in, code, status):
    auth, cookie, csrf = logged_in
    assist = Mock()
    assist.chat.side_effect = TaskError(code)
    response = web(auth, assist).handle(
        request("api/assist/chat", {}, cookie=cookie, csrf=csrf), "api/assist/chat",
    )
    assert response.status_code == status and data(response)["error"] == code
    assert isinstance(data(response)["message"], str)


def test_disabled_assist_never_calls_model(logged_in):
    auth, cookie, csrf = logged_in
    assist = Mock()
    response = web(auth, assist, MINDME_ASSIST_ENABLED="false").handle(
        request("api/assist/chat", {}, cookie=cookie, csrf=csrf), "api/assist/chat",
    )
    assert response.status_code == 503 and data(response)["error"] == "assist_disabled"
    assert not assist.mock_calls


def test_duplicate_keys_remain_rejected_before_assist(logged_in):
    auth, cookie, csrf = logged_in
    assist = Mock()
    req = request("api/assist/chat", {}, cookie=cookie, csrf=csrf)
    req = func.HttpRequest(
        method="POST", url=req.url, headers=dict(req.headers),
        body=b'{"message":"one","message":"two"}',
    )
    response = web(auth, assist).handle(req, "api/assist/chat")
    assert response.status_code == 422 and not assist.mock_calls


@pytest.mark.parametrize("error,code,status", [
    (TaskAssistError("assist_monthly_budget_exhausted"), "budget_exhausted", 402),
    (TaskAssistError("assist_request_id_invalid"), "assist_request_invalid", 422),
    (TaskAssistError("assist_request_uncertain"), "assist_request_uncertain", 503),
    (TaskAssistError("assist_model_output_invalid"), "model_unavailable", 503),
    (TaskAssistError("assist_transcription_unavailable"), "model_unavailable", 503),
    (AssistStoreError("assist_state_capacity"), "rate_limited", 429),
    (AssistStoreError("assist_state_read_unavailable"), "model_unavailable", 503),
])
def test_real_core_and_state_errors_use_safe_http_contract(logged_in, error, code, status):
    auth, cookie, csrf = logged_in
    assist = Mock()
    assist.chat.side_effect = error
    response = web(auth, assist).handle(
        request("api/assist/chat", {}, cookie=cookie, csrf=csrf), "api/assist/chat",
    )
    assert response.status_code == status and data(response)["error"] == code


@pytest.mark.parametrize("failure,code,status", [
    ("provider", "model_unavailable", 503),
    ("empty", "assist_audio_invalid", 422),
])
def test_transcription_provider_failure_is_not_an_invalid_request_id(logged_in, monkeypatch, caplog, failure, code, status):
    auth, cookie, csrf = logged_in
    subject, _, execute, _, publish, _ = service()
    container = Mock()
    container.get_blob_client.return_value = FakeBlob()
    monkeypatch.setattr(fa, "_os_container_client", lambda: container)
    monkeypatch.setenv("MINDME_ASSIST_ENABLED", "true")
    client = Mock()
    client.base_url = httpx.URL("https://synthetic.services.ai.azure.com/api/projects/synthetic/openai/v1/")
    client.with_options.return_value = client
    if failure == "provider":
        client.audio.transcriptions.create.side_effect = RuntimeError("synthetic-private-provider-detail")
    else:
        client.audio.transcriptions.create.return_value = SimpleNamespace(text=" ")
    monkeypatch.setattr(fa, "_foundry", lambda: (None, client))
    monkeypatch.setattr(fa, "_http_client", lambda: Mock())
    assistant = fa._task_assist(subject)
    req = func.HttpRequest(
        method="POST", url=auth.config.origin + "/api/tasks/api/assist/voice",
        headers={"Cookie": cookie, "Origin": auth.config.origin, "X-MindMe-CSRF": csrf,
                 "Content-Type": "audio/webm", "X-Request-Id": "9aeb8104-e499-45dc-bf30-287b704ef92f"},
        body=webm(),
    )
    response = web(auth, assistant).handle(req, "api/assist/voice")
    assert response.status_code == status
    assert data(response)["error"] == code
    assert "request ID" not in data(response)["message"]
    replay = web(auth, assistant).handle(req, "api/assist/voice")
    assert replay.status_code == 503 and data(replay)["error"] == "assist_request_uncertain"
    client.audio.transcriptions.create.assert_called_once()
    client.chat.completions.create.assert_not_called()
    execute.assert_not_called()
    publish.assert_not_called()
    assert not assistant.store.read()["threads"]
    assert "synthetic-private-provider-detail" not in caplog.text
    assert "synthetic-private-provider-detail" not in str(assistant.store.read())


@pytest.mark.parametrize("recorder_mime,raw_audio", [
    ("audio/wav", None),
    ("audio/webm;codecs=opus", webm()),
    ("audio/mp4;codecs=mp4a.40.2", fmp4()),
])
@pytest.mark.parametrize("header_case", ["browser", "lowercase"])
@pytest.mark.parametrize("compact_request_id", [False, True])
def test_real_voice_route_only_transcribes_no_chat_or_task_mutation(
    logged_in, monkeypatch, recorder_mime, raw_audio, header_case, compact_request_id,
):
    auth, cookie, csrf = logged_in
    subject, _, execute, _, publish, _ = service()
    container = Mock()
    container.get_blob_client.return_value = FakeBlob()
    monkeypatch.setattr(fa, "_os_container_client", lambda: container)
    monkeypatch.setenv("MINDME_ASSIST_ENABLED", "true")
    client = Mock()
    client.base_url = httpx.URL("https://synthetic.services.ai.azure.com/api/projects/synthetic/openai/v1/")
    client.with_options.return_value = client
    client.audio.transcriptions.create.return_value = SimpleNamespace(
        text="Create a task to write a synthetic outline", language="en",
        usage=SimpleNamespace(input_tokens=40, output_tokens=10),
    )
    monkeypatch.setattr(fa, "_foundry", lambda: (None, client))
    monkeypatch.setattr(fa, "_http_client", lambda: Mock())
    buffer = BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0" * 8000)
    assistant = fa._task_assist(subject)
    request_id = "9aeb8104-e499-45dc-bf30-287b704ef92f"
    if compact_request_id:
        request_id = request_id.replace("-", "")
    headers = {
        "Cookie": cookie, "Origin": auth.config.origin, "X-MindMe-CSRF": csrf,
        "Content-Type": recorder_mime, "X-Request-Id": request_id,
    }
    if header_case == "lowercase":
        headers = {name.lower(): value for name, value in headers.items()}
    req = func.HttpRequest(
        method="POST", url=auth.config.origin + "/api/tasks/api/assist/voice",
        headers=headers, body=raw_audio or buffer.getvalue(),
    )
    response = web(auth, assistant).handle(req, "api/assist/voice")
    if compact_request_id:
        assert response.status_code == 422
        assert data(response) == {
            "error": "assist_request_invalid",
            "message": "Check the request fields and use a valid request ID.",
        }
        client.audio.transcriptions.create.assert_not_called()
        client.chat.completions.create.assert_not_called()
        execute.assert_not_called()
        publish.assert_not_called()
        assert not subject.store.read()["proposals"]
        assert assistant.usage(datetime.now(timezone.utc).date())["calls_today"] == 0
        return
    assert response.status_code == 200
    result = data(response)
    assert set(result) == {"text", "language", "duration_s"}
    assert result["text"] == "Create a task to write a synthetic outline"
    assert result["language"] == "en" and 0 < result["duration_s"] <= 120
    uploaded = client.audio.transcriptions.create.call_args.kwargs["file"]
    assert uploaded[1:] == (req.get_body(), recorder_mime.split(";")[0])
    client.chat.completions.create.assert_not_called()
    execute.assert_not_called()
    publish.assert_not_called()
    assert not subject.store.read()["proposals"]
    usage = assistant.usage(datetime.now(timezone.utc).date())
    assert {"month_eur", "month_cap_eur", "today_eur", "today_cap_eur", "calls_today", "models"} <= usage.keys()
    assert {key: usage["models"][key] for key in ("assist", "planner", "orchestrator")} == {
        "assist": "gpt-6-luna", "planner": "gpt-6-sol", "orchestrator": "gpt-6-sol",
    }
