import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import function_app as fa
from task_service import TaskError
from task_assist import TaskAssistError
from test_task_service import service
from test_task_service import TODAY, PATH, REVISION
from test_briefing_state import FakeBlob


def model(monkeypatch, output=None):
    client = Mock()
    client.with_options.return_value = client
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
            refusal=None, content=json.dumps(output or {"reply": "Synthetic reply"}),
        ))],
        usage=SimpleNamespace(prompt_tokens=20, completion_tokens=30),
    )
    monkeypatch.setattr(fa, "_foundry", lambda: (None, client))
    monkeypatch.setattr(fa, "_http_client", lambda: Mock())
    return client


def test_assist_model_reuses_project_identity_no_storage_tools_or_retries(monkeypatch):
    client = model(monkeypatch)
    result = fa._generate_task_assist({
        "model": "existing-luna", "schema": {"type": "object"}, "request": "Synthetic task request",
    })
    arguments = client.chat.completions.create.call_args.kwargs
    assert arguments["model"] == "existing-luna" and arguments["store"] is False
    assert arguments["max_completion_tokens"] == 2400 and "tools" not in arguments
    assert "<<<DATA_" in arguments["messages"][1]["content"]
    assert "Synthetic task request" not in arguments["messages"][0]["content"]
    assert client.with_options.call_args.kwargs["max_retries"] == 0
    assert result["usage"] == {"input_tokens": 20, "output_tokens": 30}


@pytest.mark.parametrize("reason,refusal", [("length", None), ("stop", "Refused")])
def test_assist_incomplete_or_refused_model_output_is_explicit(monkeypatch, reason, refusal):
    client = model(monkeypatch)
    choice = client.chat.completions.create.return_value.choices[0]
    choice.finish_reason, choice.message.refusal = reason, refusal
    with pytest.raises(TaskError, match="model_unavailable"):
        fa._generate_task_assist({"model": "existing-luna", "schema": {}})


def test_assist_transcription_preserves_mixed_language_and_usage_without_storage(monkeypatch, caplog):
    client = model(monkeypatch)
    client.audio.transcriptions.create.return_value = SimpleNamespace(
        text=" Создай задачу write an outline ", language=None,
        usage=SimpleNamespace(input_tokens=50, output_tokens=10),
    )
    result = fa._transcribe_task_audio(b"synthetic-audio", "audio/webm")
    arguments = client.audio.transcriptions.create.call_args.kwargs
    assert arguments["model"] == "gpt-4o-mini-transcribe"
    assert arguments["response_format"] == "json"
    assert "Russian" in arguments["prompt"] and "language" not in arguments
    assert arguments["file"] == ("voice.webm", b"synthetic-audio", "audio/webm")
    assert result["language"] == "ru" and result["text"].startswith("Создай")
    assert result["usage"] == {"input_tokens": 50, "output_tokens": 10}
    assert client.with_options.call_args.kwargs["max_retries"] == 0
    assert "Создай" not in caplog.text


def test_empty_transcript_is_not_a_success_shaped_response(monkeypatch):
    client = model(monkeypatch)
    client.audio.transcriptions.create.return_value = SimpleNamespace(text=" ")
    with pytest.raises(TaskError, match="assist_audio_invalid"):
        fa._transcribe_task_audio(b"synthetic", "audio/ogg")


@pytest.mark.parametrize("mime,filename,content_type", [
    ("audio/mp4;codecs=mp4a.40.2", "voice.mp4", "audio/mp4"),
    ("audio/webm;codecs=opus", "voice.webm", "audio/webm"),
    (" Audio/Ogg; codecs=opus ", "voice.ogg", "audio/ogg"),
])
def test_assist_transcription_normalizes_recorder_mime_parameters(monkeypatch, mime, filename, content_type):
    client = model(monkeypatch)
    client.audio.transcriptions.create.return_value = SimpleNamespace(text="Synthetic task", language="en")
    fa._transcribe_task_audio(b"synthetic", mime)
    assert client.audio.transcriptions.create.call_args.kwargs["file"] == (
        filename, b"synthetic", content_type,
    )


def test_assist_transcription_rejects_unsupported_mime_before_provider(monkeypatch):
    factory = Mock()
    monkeypatch.setattr(fa, "_foundry", factory)
    with pytest.raises(TaskError, match="assist_audio_invalid"):
        fa._transcribe_task_audio(b"synthetic", "text/plain")
    factory.assert_not_called()


def test_assist_does_not_enable_legacy_local_transcription(monkeypatch):
    monkeypatch.setenv("MINDME_ASSIST_ENABLED", "true")
    monkeypatch.delenv("AZURE_OPENAI_TRANSCRIPTION_DEPLOYMENT", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_WHISPER_DEPLOYMENT", raising=False)
    factory = Mock()
    monkeypatch.setattr(fa, "_foundry", factory)
    assert fa._transcribe_voice(b"synthetic") is None
    factory.assert_not_called()


def test_host_factory_wires_real_assist_service_and_private_store(monkeypatch):
    subject, *_ = service()
    container = Mock()
    monkeypatch.setattr(fa, "_os_container_client", lambda: container)
    assist = fa._task_assist(subject)
    assert assist.service is subject
    assert assist.generate is fa._generate_task_assist
    assert assist.transcribe is fa._transcribe_task_audio


def test_real_core_supplies_the_host_model_schema_and_selected_deployment(monkeypatch):
    subject, *_ = service()
    container = Mock()
    container.get_blob_client.return_value = FakeBlob()
    monkeypatch.setattr(fa, "_os_container_client", lambda: container)
    monkeypatch.setenv("MINDME_ASSIST_ENABLED", "true")
    client = model(monkeypatch, {
        "intent": "next", "reply": "Synthetic next action", "language": "en", "suggestions": [],
        "task_path": None, "changes": None, "definition": None, "scope": None,
        "estimate": None, "plan": None, "knowledge_target": None, "completion": None,
    })
    assist = fa._task_assist(subject)
    result = assist.step({
        "request_id": "b88e34f3-9657-4546-bdd0-9313f0494acb",
        "step": "next", "task": None, "input": None, "language": "en",
    }, TODAY)
    assert client.chat.completions.create.call_count == 1
    assert client.chat.completions.create.call_args.kwargs["model"] == "gpt-6-luna"
    assert result["reply"] == "Synthetic next action"


def test_board_move_and_fresh_receipt_expose_undo_only_after_canonical_publication(monkeypatch):
    subject, repository, execute, _, publish, _ = service()
    container = Mock()
    container.get_blob_client.return_value = FakeBlob()
    monkeypatch.setattr(fa, "_os_container_client", lambda: container)
    monkeypatch.setenv("MINDME_ASSIST_ENABLED", "true")
    client = model(monkeypatch)
    assist = fa._task_assist(subject)
    result = assist.change({
        "request_id": "a7d8e435-12c5-4ff5-8f83-27b0958c57d8",
        "path": PATH, "revision": REVISION, "change": {"stage": "doing"},
    }, TODAY)
    card = result["cards"][0]
    assert card["kind"] == "applied" and card["saved"] is False and card["undo_available"] is False
    assert card["source_status"] == "current"
    poll = {"request_id": "b88e34f3-9657-4546-bdd0-9313f0494acb", "receipt_id": card["receipt_id"]}
    first = assist.receipt(poll, TODAY)
    assert first["cards"][0]["saved"] is False

    def merged(*_):
        repository.records[PATH].update(revision="b" * 40, stage="doing")
        return {
            "status": "merged", "path": PATH, "source_revision": "b" * 40,
            "pr_url": "https://github.com/example/mindVault/pull/7",
        }

    publish.side_effect = merged
    confirmed = assist.receipt(poll, TODAY)["cards"][0]
    assert confirmed["status"] == "completed" and confirmed["publication_status"] == "merged"
    assert confirmed["source_status"] == "current" and confirmed["saved"] is True
    assert confirmed["undo_available"] is True and confirmed["task"]["revision"] == "b" * 40
    client.chat.completions.create.assert_not_called()
    assert execute.call_count == 1
