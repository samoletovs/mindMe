from __future__ import annotations

import base64
import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

import azure.functions as func
import httpx
import pytest
from azure.core.exceptions import ClientAuthenticationError, ResourceNotFoundError

import function_app as fa


def request(payload: object, *, params: dict | None = None) -> func.HttpRequest:
    return func.HttpRequest(
        method="POST",
        url="https://example.invalid/api/telegram_webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-secret"},
        params=params or {},
        body=json.dumps(payload).encode(),
    )


@pytest.fixture
def owner(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "7")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "test-secret")
    sent = Mock()
    monkeypatch.setattr(fa, "_telegram_send", sent)
    return sent


def test_private_tool_bindings_require_function_auth():
    bindings = {
        function.get_function_name(): binding.get_dict_repr()
        for function in fa.app.get_functions()
        for binding in function.get_bindings()
        if binding.type == "httpTrigger"
    }
    for name in ("tool_briefing_context", "tool_weather", "tool_vault_recent", "tool_vault_read"):
        assert bindings[name]["authLevel"] == func.AuthLevel.FUNCTION
    assert bindings["telegram_webhook"]["authLevel"] == func.AuthLevel.ANONYMOUS
    assert bindings["health"]["authLevel"] == func.AuthLevel.ANONYMOUS


@pytest.mark.parametrize("text", ["/note keep this", "/task book a test", "https://example.com"])
def test_failed_captures_remain_retryable(monkeypatch, owner, text):
    monkeypatch.setattr(fa, "_forward_to_memex", lambda update: False)
    response = fa.telegram_webhook(request({"message": {"chat": {"id": 7}, "text": text}}))
    assert response.status_code == 503
    owner.assert_not_called()


@pytest.mark.parametrize("transcript", [None, "synthetic voice note"])
def test_failed_voice_capture_never_echoes_success(monkeypatch, owner, transcript):
    monkeypatch.setattr(fa, "_download_telegram_file", lambda file_id: b"audio")
    monkeypatch.setattr(fa, "_transcribe_voice", lambda data, mime: transcript)
    monkeypatch.setattr(fa, "_forward_to_memex", lambda update: False)
    response = fa.telegram_webhook(request({
        "message": {"chat": {"id": 7}, "voice": {"file_id": "test-file"}},
    }))
    assert response.status_code == 503
    owner.assert_not_called()


@pytest.mark.parametrize("chat_id", [8, None])
def test_callback_allowlist_is_enforced_before_forwarding(monkeypatch, owner, chat_id):
    forward = Mock()
    monkeypatch.setattr(fa, "_forward_to_memex", forward)
    response = fa.telegram_webhook(request({
        "callback_query": {"message": {"chat": {"id": chat_id}}, "data": "test"},
    }))
    assert response.status_code == 200
    forward.assert_not_called()


def test_failed_owner_callback_remains_retryable(monkeypatch, owner):
    monkeypatch.setattr(fa, "_forward_to_memex", lambda update: False)
    response = fa.telegram_webhook(request({
        "callback_query": {"message": {"chat": {"id": 7}}, "data": "test"},
    }))
    assert response.status_code == 503


@pytest.mark.parametrize("payload", [[], None, "text", {"message": ["not a message"]}])
def test_invalid_updates_are_rejected(owner, payload):
    assert fa.telegram_webhook(request(payload)).status_code == 400
    owner.assert_not_called()


@pytest.mark.parametrize("status", [302, 400, 429, 500])
def test_memex_non_success_is_not_acknowledged(monkeypatch, status):
    monkeypatch.setenv("MEMEX_WEBHOOK_URL", "https://example.invalid/capture?code=test")
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(status)))
    monkeypatch.setattr(fa, "_http_client", lambda: client)
    with client:
        assert fa._forward_to_memex({"update_id": 1}) is False


def test_forward_errors_do_not_log_secret_urls(monkeypatch, caplog):
    marker = "SYNTHETIC_SECRET_DO_NOT_LOG"
    monkeypatch.setenv("MEMEX_WEBHOOK_URL", f"https://example.invalid/?code={marker}")
    client = Mock()
    client.post.side_effect = httpx.ConnectError(marker)
    monkeypatch.setattr(fa, "_http_client", lambda: client)
    assert fa._forward_to_memex({}) is False
    assert marker not in caplog.text
    assert "ConnectError" in caplog.text


def test_long_telegram_reply_is_delivered_in_full(monkeypatch):
    text = ("A\U0001f600\n" * 3000) + "final words"
    sent: list[str] = []

    def send(req: httpx.Request) -> httpx.Response:
        sent.append(json.loads(req.content)["text"])
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(send))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic-token")
    monkeypatch.setattr(fa, "_http_client", lambda: client)
    with client:
        fa._telegram_send(7, text)
    assert len(sent) > 1
    assert "".join(sent) == text
    assert all(0 < len(chunk.encode("utf-16-le")) // 2 <= 4096 for chunk in sent)


def test_telegram_delivery_failure_has_no_token_or_response_body(monkeypatch):
    marker = "SYNTHETIC_SECRET_DO_NOT_LOG"
    client = httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(400, json={"ok": False, "description": marker})
    ))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", marker)
    monkeypatch.setattr(fa, "_http_client", lambda: client)
    with client, pytest.raises(fa.TelegramDeliveryError) as error:
        fa._telegram_send(7, "synthetic message")
    assert marker not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.parametrize("failure", [ResourceNotFoundError, ClientAuthenticationError])
def test_blob_read_distinguishes_absent_from_inaccessible(monkeypatch, failure):
    container = Mock()
    container.get_blob_client.return_value.download_blob.side_effect = failure("synthetic")
    monkeypatch.setattr(fa, "_os_container_client", lambda: container)
    if failure is ResourceNotFoundError:
        assert fa._read_os_text("test.md") == ""
    else:
        with pytest.raises(ClientAuthenticationError):
            fa._read_os_text("test.md")


def test_storage_outage_cannot_be_reported_as_empty_vault(monkeypatch):
    container = Mock()
    container.list_blobs.side_effect = ClientAuthenticationError("synthetic")
    container.get_blob_client.return_value.download_blob.side_effect = ClientAuthenticationError("synthetic")
    monkeypatch.setattr(fa, "_os_container_client", lambda: container)
    assert "could not load the vault status" in fa._status_line()
    response = fa.tool_briefing_context(request({}))
    assert response.status_code == 503


def test_yesterday_journal_crosses_year_boundary(monkeypatch):
    class Today(date):
        @classmethod
        def today(cls) -> date:
            return date(2026, 1, 1)

    reads = []

    def read(path: str) -> str:
        reads.append(path)
        return "Mood: 8\nEnergy: 7\n- [ ] synthetic task" if path.startswith("journal/") else ""

    monkeypatch.setattr(fa, "date", Today)
    monkeypatch.setattr(fa, "_read_os_text", read)
    monkeypatch.setattr(fa, "_list_area_h1s", lambda: [])
    monkeypatch.setattr(fa, "_vault_state", lambda today: {})
    monkeypatch.setattr(fa, "_fetch_open_loops", fa._empty_open_loops)
    snapshot = fa._build_briefing_snapshot()
    assert "journal/2025/2025-12-31.md" in reads
    assert snapshot["yesterday"] == {
        "date": "2025-12-31", "mood": "8", "energy": "7", "open_loops_count": 1,
    }


@pytest.mark.parametrize("payload", [{}, [], {"ideas": {"open_count": "invalid"}}])
def test_invalid_open_loop_projection_is_unknown_not_zero(monkeypatch, payload):
    monkeypatch.setenv("MEMEX_STATE_URL", "https://example.invalid/state")
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload)))
    monkeypatch.setattr(fa, "_http_client", lambda: client)
    with client:
        state = fa._fetch_open_loops()
    assert state["status"] == "unavailable"
    assert state["tasks"]["open_count"] is None


def test_missing_open_loop_config_is_visible(monkeypatch):
    monkeypatch.delenv("MEMEX_STATE_URL", raising=False)
    state = fa._fetch_open_loops()
    assert state["status"] == "not_configured"
    assert state["ideas"]["open_count"] is None


def test_all_sections_off_does_not_fetch_weather(monkeypatch):
    monkeypatch.setattr(fa, "_load_briefing", lambda: {"sections": []})
    weather = Mock()
    monkeypatch.setattr(fa, "_weather_summary", weather)
    assert "switched off" in fa._compose_local_briefing()
    weather.assert_not_called()


def test_weather_outage_preserves_personal_fallback(monkeypatch):
    monkeypatch.setattr(fa, "_load_briefing", lambda: {
        "sections": ["focus", "weather"], "today_focus": "Finish synthetic exercise",
    })
    weather = Mock(side_effect=httpx.ConnectError("synthetic"))
    monkeypatch.setattr(fa, "_weather_summary", weather)
    text = fa._compose_local_briefing()
    assert "Finish synthetic exercise" in text
    assert "Weather is unavailable" in text


def test_loop_only_fallback_is_not_reported_as_switched_off(monkeypatch):
    monkeypatch.setattr(fa, "_load_briefing", lambda: {
        "sections": ["loops"],
        "open_loops": {"status": "available", "ideas": {"open_count": 2}, "tasks": {"open_count": 3}},
    })
    text = fa._compose_local_briefing()
    assert "Open ideas: 2" in text and "Open tasks: 3" in text
    assert "switched off" not in text


def test_delivery_failure_fails_timer_without_generating_second_briefing(monkeypatch, owner):
    monkeypatch.setattr(fa, "_briefing_prefs", lambda: ["focus"])
    monkeypatch.setattr(fa, "_mirror_freshness", lambda today: {"status": "current"})
    monkeypatch.setattr(fa, "_ask_companion", lambda text: "synthetic briefing")
    fallback = Mock()
    monkeypatch.setattr(fa, "_compose_local_briefing", fallback)
    owner.side_effect = fa.TelegramDeliveryError("synthetic failure")
    with pytest.raises(fa.TelegramDeliveryError):
        fa.morning_briefing_timer(None)
    fallback.assert_not_called()
    assert owner.call_count == 1


def test_unreadable_preferences_do_not_trigger_weather_fallback(monkeypatch, owner):
    monkeypatch.setattr(fa, "_briefing_prefs", Mock(side_effect=ValueError("invalid")))
    monkeypatch.setattr(fa, "_compose_local_briefing", Mock(side_effect=ValueError("invalid")))
    weather = Mock()
    monkeypatch.setattr(fa, "_weather_summary", weather)
    fa.morning_briefing_timer(None)
    weather.assert_not_called()
    assert "No personal briefing was generated" in owner.call_args.args[1]


def test_stateless_companion_does_not_create_orphan_conversations(monkeypatch):
    client = Mock()
    client.responses.create.return_value = SimpleNamespace(output_text="synthetic reply")
    monkeypatch.setattr(fa, "_foundry", lambda: (None, client))
    assert fa._ask_companion("synthetic input") == "synthetic reply"
    client.conversations.create.assert_not_called()
    assert client.responses.create.call_args.kwargs["store"] is False


@pytest.mark.parametrize("path", [
    "notes/../private.md", "notes/%2e%2e/private.md", "notes/a.md?ref=old",
    "notes/a.md#fragment", "notes/.hidden.md", "notes/a.private.md",
    "notes/a.PRIVATE.md", "/notes/a.md",
])
def test_vault_paths_cannot_escape_the_read_boundary(path):
    assert not fa._vault_path_allowed(path)


def test_vault_read_reports_truncation(monkeypatch):
    text = "a" * (fa._VAULT_READ_MAX_CHARS + 5)
    monkeypatch.setattr(fa, "_mindvault_get", lambda path: {
        "type": "file", "encoding": "base64", "content": base64.b64encode(text.encode()).decode(),
    })
    response = fa.tool_vault_read(request({}, params={"path": "notes/test.md"}))
    payload = json.loads(response.get_body())
    assert response.status_code == 200
    assert payload["truncated"] is True
    assert payload["content"] == text[:fa._VAULT_READ_MAX_CHARS]


def test_legacy_queue_never_discards_a_capture():
    message = SimpleNamespace(id="test", get_body=lambda: b"synthetic")
    with pytest.raises(RuntimeError, match="unsupported"):
        fa.capture_drain(message)


def test_old_mirror_is_reported_as_stale_without_source_paths(monkeypatch):
    monkeypatch.setattr(fa, "_read_os_text", lambda path: json.dumps({
        "synced_at_utc": "2026-07-30T07:24:22+00:00", "source": "synthetic-private-path",
    }))
    state = fa._mirror_freshness(date(2026, 9, 6))
    assert state["status"] == "stale"
    assert state["age_days"] == 38
    assert "synthetic-private-path" not in json.dumps(state)


@pytest.mark.parametrize("manifest", ["", "{}", "[]", "{bad", '{"synced_at_utc": "2027-01-01"}'])
def test_missing_or_invalid_manifest_never_claims_freshness(monkeypatch, manifest):
    monkeypatch.setattr(fa, "_read_os_text", lambda path: manifest)
    assert fa._mirror_freshness(date(2026, 9, 6))["status"] == "unknown"


def test_stale_context_warning_survives_vault_section_being_disabled(monkeypatch):
    monkeypatch.setattr(fa, "_build_briefing_snapshot", lambda: {
        "today_focus": "synthetic focus", "vault_state": {"inbox": {}},
        "source_freshness": {"status": "stale", "age_days": 38},
    })
    monkeypatch.setattr(fa, "_briefing_prefs", lambda: ["focus"])
    text = fa._compose_local_briefing()
    assert "38 days ago" in text
    assert "synthetic focus" in text


@pytest.mark.parametrize("payload", [
    {"message": {"chat": {"id": 7}, "text": "save: synthetic idea"}},
    {"message": {"chat": {"id": 7}, "voice": {"file_id": "synthetic"}}},
])
def test_feedback_failure_does_not_replay_an_accepted_capture(monkeypatch, owner, payload):
    forward = Mock(return_value=True)
    monkeypatch.setattr(fa, "_forward_to_memex", forward)
    monkeypatch.setattr(fa, "_download_telegram_file", lambda file_id: b"audio")
    monkeypatch.setattr(fa, "_transcribe_voice", lambda data, mime: "synthetic transcript")
    owner.side_effect = fa.TelegramDeliveryError("synthetic failure")
    assert fa.telegram_webhook(request(payload)).status_code == 200
    assert forward.call_count == 1


def test_recent_notes_do_not_reveal_private_filenames(monkeypatch):
    monkeypatch.setattr(fa, "_mindvault_get", lambda path: [
        {"type": "file", "name": "2026-09-06-synthetic-secret.private.md",
         "path": "notes/2026-09-06-synthetic-secret.private.md"},
        {"type": "file", "name": "2026-09-06-safe.md", "path": "notes/2026-09-06-safe.md"},
    ])
    items = fa._vault_recent("notes", 10)
    assert len(items) == 1
    assert items[0]["title"] == "safe"
