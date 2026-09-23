"""Focused tests for quick-capture routing and the summary command in ``function_app``."""

from __future__ import annotations

from datetime import date

import pytest

import function_app as app
import function_app as fa


class FakeRequest:
    def __init__(self, payload: dict) -> None:
        self.headers = {"X-Telegram-Bot-Api-Secret-Token": "secret"}
        self._payload = payload

    def get_json(self) -> dict:
        return self._payload


class DummyRequest:
    def __init__(self, payload: dict, secret: str) -> None:
        self._payload = payload
        self.headers = {"X-Telegram-Bot-Api-Secret-Token": secret}

    def get_json(self) -> dict:
        return self._payload


def test_capture_category_suggestion_points_actionable_notes_to_task():
    assert app._capture_category_suggestion("save: need to call the dentist") == (
        "For something you need to do, use /task next time."
    )


def test_capture_category_suggestion_points_reflection_to_diary():
    assert app._capture_category_suggestion("n: today felt heavier than expected") == (
        "For a journal entry, use /diary next time."
    )


def test_capture_category_suggestion_skips_explicit_categories():
    assert app._capture_category_suggestion("/idea maybe build a weather digest") is None
    assert app._capture_category_suggestion("https://example.com/article") is None


def test_webhook_sends_category_suggestion_for_ambiguous_capture(monkeypatch):
    sent_messages: list[tuple[int, str]] = []

    monkeypatch.setattr(app, "_verify_telegram_secret", lambda req: True)
    monkeypatch.setattr(app, "_is_allowed_chat", lambda chat_id: True)
    monkeypatch.setattr(app, "_forward_to_memex", lambda update: True)
    monkeypatch.setattr(app, "_telegram_send", lambda chat_id, text: sent_messages.append((chat_id, text)))
    monkeypatch.setattr(app, "_ask_companion", lambda text: (_ for _ in ()).throw(AssertionError("unexpected companion call")))

    req = FakeRequest(
        {
            "message": {
                "chat": {"id": 123},
                "text": "save: need to book the dentist",
            }
        }
    )

    resp = app.telegram_webhook(req)

    assert resp.status_code == 200
    assert sent_messages == [
        (123, "For something you need to do, use /task next time.")
    ]


def test_webhook_does_not_send_suggestion_when_capture_already_explicit(monkeypatch):
    sent_messages: list[tuple[int, str]] = []

    monkeypatch.setattr(app, "_verify_telegram_secret", lambda req: True)
    monkeypatch.setattr(app, "_is_allowed_chat", lambda chat_id: True)
    monkeypatch.setattr(app, "_forward_to_memex", lambda update: True)
    monkeypatch.setattr(app, "_telegram_send", lambda chat_id, text: sent_messages.append((chat_id, text)))

    req = FakeRequest(
        {
            "message": {
                "chat": {"id": 123},
                "text": "/task book the dentist",
            }
        }
    )

    resp = app.telegram_webhook(req)

    assert resp.status_code == 200
    assert sent_messages == []


def _webhook_payload(text: str, chat_id: int = 7) -> dict:
    return {"message": {"chat": {"id": chat_id}, "text": text}}


def test_first_interaction_sends_onboarding_tutorial(monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "sec")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "7")
    monkeypatch.setattr(fa, "_claim_onboarding", lambda: True)

    sent: list[str] = []
    monkeypatch.setattr(fa, "_telegram_send", lambda _chat_id, text: sent.append(text))

    resp = fa.telegram_webhook(DummyRequest(_webhook_payload("/start"), "sec"))

    assert resp.status_code == 200
    assert sent == [
        *fa._ONBOARDING_TUTORIAL,
        "You're all set. Send a note, task, idea, or message whenever you like.",
    ]


def test_later_interaction_skips_onboarding_tutorial(monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "sec")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "7")
    monkeypatch.setattr(fa, "_claim_onboarding", lambda: False)

    sent: list[str] = []
    monkeypatch.setattr(fa, "_telegram_send", lambda _chat_id, text: sent.append(text))

    resp = fa.telegram_webhook(DummyRequest(_webhook_payload("/ping"), "sec"))

    assert resp.status_code == 200
    assert sent == ["pong"]


def test_help_includes_summary_command(monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "sec")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "7")
    monkeypatch.setattr(fa, "_is_capture_intent", lambda _text: False)

    sent: list[str] = []
    monkeypatch.setattr(fa, "_telegram_send", lambda _chat_id, text: sent.append(text))

    resp = fa.telegram_webhook(DummyRequest(_webhook_payload("/help"), "sec"))

    assert resp.status_code == 200
    assert sent
    assert "/summary" in sent[0]


def test_summary_command_sends_daily_summary(monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "sec")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "7")
    monkeypatch.setattr(fa, "_is_capture_intent", lambda _text: False)
    monkeypatch.setattr(fa, "_daily_summary", lambda: "summary text")

    sent: list[str] = []
    monkeypatch.setattr(fa, "_telegram_send", lambda _chat_id, text: sent.append(text))

    resp = fa.telegram_webhook(DummyRequest(_webhook_payload("/summary"), "sec"))

    assert resp.status_code == 200
    assert sent == ["summary text"]


def test_review_command_sends_weekly_review_prompt(monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "sec")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "7")
    monkeypatch.setattr(fa, "_is_capture_intent", lambda _text: False)
    monkeypatch.setattr(fa, "_review_prompt", lambda: "review text")

    sent: list[str] = []
    monkeypatch.setattr(fa, "_telegram_send", lambda _chat_id, text: sent.append(text))

    resp = fa.telegram_webhook(DummyRequest(_webhook_payload("/review"), "sec"))

    assert resp.status_code == 200
    assert sent == ["review text"]


def test_reviews_state_uses_latest_iso_week(monkeypatch):
    monkeypatch.setattr(
        fa,
        "_os_blob_props",
        lambda _prefix: [
            ("reviews/2026-w03-weekly.md", None),
            ("reviews/2026-w05-weekly.md", None),
            ("reviews/not-a-review.md", None),
        ],
    )

    state = fa._reviews_state(date(2026, 2, 3))

    assert state == {"last_weekly": "2026-01-26", "days_since": 8}


def test_weekly_review_timer_sends_nudge(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "7")
    monkeypatch.setattr(
        fa,
        "_vault_state",
        lambda: {
            "inbox": {"count": 2, "oldest_age_days": 4},
            "projects": {"open_count": 1, "nearest_deadline": "2026-08-01", "nearest_project": "x"},
            "reviews": {"last_weekly": "2026-07-20", "days_since": 9},
            "stale_areas": [],
        },
    )
    monkeypatch.setattr(fa, "_compose_review_nudge", lambda _state: "nudge text")

    sent: list[tuple[int, str]] = []
    monkeypatch.setattr(fa, "_telegram_send", lambda chat_id, text: sent.append((chat_id, text)))

    fa.weekly_review_timer(None)

    assert sent == [(7, "nudge text")]


# ---------------------------------------------------------------------------
# Voice capture tests
# ---------------------------------------------------------------------------

def _voice_payload(file_id: str = "abc123", mime: str = "audio/ogg", chat_id: int = 7) -> dict:
    return {
        "message": {
            "chat": {"id": chat_id},
            "voice": {"file_id": file_id, "mime_type": mime, "duration": 3, "file_size": 4096},
        }
    }


def test_voice_note_is_transcribed_and_forwarded_as_text(monkeypatch):
    """When Whisper is configured and download/transcription succeed, the webhook
    forwards an update with the transcript injected as ``message.text`` and echoes
    it back to the user prefixed with 🎤."""
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "sec")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "7")

    monkeypatch.setattr(fa, "_download_telegram_file", lambda fid: b"AUDIO")
    monkeypatch.setattr(fa, "_transcribe_voice", lambda _bytes, _mime: "book the dentist")

    forwarded: list[dict] = []
    monkeypatch.setattr(fa, "_forward_to_memex", lambda upd: forwarded.append(upd) or True)

    sent: list[tuple[int, str]] = []
    monkeypatch.setattr(fa, "_telegram_send", lambda cid, text: sent.append((cid, text)))

    resp = fa.telegram_webhook(DummyRequest(_voice_payload(), "sec"))

    assert resp.status_code == 200
    # One update forwarded, with the transcript as message.text.
    assert len(forwarded) == 1
    assert forwarded[0]["message"]["text"] == "book the dentist"
    # Original voice field still present so memex can archive it.
    assert "voice" in forwarded[0]["message"]
    # Confirmation echoed back to user.
    assert sent == [(7, "\U0001f3a4 book the dentist")]


def test_voice_note_falls_back_to_raw_forward_when_no_deployment(monkeypatch):
    """When AZURE_OPENAI_WHISPER_DEPLOYMENT is not set, _transcribe_voice returns
    None and the original update is forwarded unchanged (no confirmation message)."""
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "sec")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "7")
    monkeypatch.delenv("AZURE_OPENAI_WHISPER_DEPLOYMENT", raising=False)

    monkeypatch.setattr(fa, "_download_telegram_file", lambda fid: b"AUDIO")
    # _transcribe_voice returns None when deployment is unset — mirror the real impl.
    monkeypatch.setattr(fa, "_transcribe_voice", lambda _bytes, _mime: None)

    forwarded: list[dict] = []
    monkeypatch.setattr(fa, "_forward_to_memex", lambda upd: forwarded.append(upd) or True)

    sent: list[tuple[int, str]] = []
    monkeypatch.setattr(fa, "_telegram_send", lambda cid, text: sent.append((cid, text)))

    payload = _voice_payload()
    resp = fa.telegram_webhook(DummyRequest(payload, "sec"))

    assert resp.status_code == 200
    # Raw update forwarded without modification.
    assert len(forwarded) == 1
    assert forwarded[0] == payload
    # No confirmation sent.
    assert sent == []


def test_voice_note_falls_back_to_raw_forward_on_download_error(monkeypatch):
    """If the file download fails (network error), the raw update is forwarded and
    no confirmation is sent."""
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "sec")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "7")

    import httpx as _httpx

    def _failing_download(_fid: str) -> bytes:
        raise _httpx.HTTPError("network failure")

    monkeypatch.setattr(fa, "_download_telegram_file", _failing_download)

    forwarded: list[dict] = []
    monkeypatch.setattr(fa, "_forward_to_memex", lambda upd: forwarded.append(upd) or True)

    sent: list[tuple[int, str]] = []
    monkeypatch.setattr(fa, "_telegram_send", lambda cid, text: sent.append((cid, text)))

    payload = _voice_payload()
    resp = fa.telegram_webhook(DummyRequest(payload, "sec"))

    assert resp.status_code == 200
    assert len(forwarded) == 1
    assert forwarded[0] == payload
    assert sent == []


def test_transcribe_voice_returns_none_when_deployment_unset(monkeypatch):
    """_transcribe_voice returns None immediately when the deployment env var is absent."""
    monkeypatch.delenv("AZURE_OPENAI_WHISPER_DEPLOYMENT", raising=False)
    assert fa._transcribe_voice(b"audio", "audio/ogg") is None


def test_download_telegram_file_calls_getfile_then_download(monkeypatch):
    """_download_telegram_file resolves the file_path via getFile and returns blob bytes."""
    import httpx as _httpx

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")

    calls: list[str] = []

    def _fake_get(url: str, **_kwargs):
        calls.append(url)
        req = _httpx.Request("GET", url)
        if "getFile" in url:
            return _httpx.Response(
                200, json={"result": {"file_path": "voice/x.ogg"}}, request=req
            )
        return _httpx.Response(200, content=b"BYTES", request=req)

    class _FakeClient:
        def get(self, url: str, **kwargs):
            return _fake_get(url, **kwargs)

    monkeypatch.setattr(fa, "_http_client", lambda: _FakeClient())

    result = fa._download_telegram_file("fid42")

    assert result == b"BYTES"
    assert any("getFile" in c for c in calls)
    assert any("voice/x.ogg" in c for c in calls)


# ---------------------------------------------------------------------------
# Morning briefing customization (/briefing)
# ---------------------------------------------------------------------------

def test_briefing_prefs_default_to_every_section(monkeypatch):
    monkeypatch.setattr(fa, "_read_os_text", lambda _path: "")

    assert fa._briefing_prefs() == list(fa.BRIEFING_SECTION_NAMES)


def test_briefing_prefs_keep_known_sections_in_canonical_order(monkeypatch):
    monkeypatch.setattr(
        fa,
        "_read_os_text",
        lambda _path: '{"sections": ["weather", "bogus", "focus", "focus"]}',
    )

    assert fa._briefing_prefs() == ["focus", "weather"]


def test_briefing_prefs_do_not_reenable_sections_on_invalid_json(monkeypatch):
    monkeypatch.setattr(fa, "_read_os_text", lambda _path: "{not json")

    with pytest.raises(ValueError, match="preferences"):
        fa._briefing_prefs()


def test_load_briefing_drops_disabled_sections(monkeypatch):
    monkeypatch.setattr(
        fa,
        "_build_briefing_snapshot",
        lambda: {
            "date": "2026-08-11",
            "today_focus": "ship it",
            "top_goals": ["a"],
            "this_week": ["b"],
            "yesterday": {"open_loops_count": 1},
            "areas": ["health"],
            "vault_state": {"inbox": {"count": 2}},
            "open_loops": {"ideas": {}, "tasks": {}},
        },
    )
    monkeypatch.setattr(fa, "_briefing_prefs", lambda: ["focus", "vault"])

    data = fa._load_briefing()

    assert data["sections"] == ["focus", "vault"]
    assert data["today_focus"] == "ship it"
    assert data["vault_state"] == {"inbox": {"count": 2}}
    assert data["date"] == "2026-08-11"
    for dropped in ("top_goals", "this_week", "yesterday", "areas", "open_loops"):
        assert dropped not in data


def test_briefing_command_without_argument_lists_sections(monkeypatch):
    monkeypatch.setattr(fa, "_briefing_prefs", lambda: ["focus", "weather"])

    reply = fa._handle_briefing_command("")

    assert "✅ focus" in reply
    assert "✅ weather" in reply
    assert "⬜ goals" in reply


def test_briefing_command_saves_selected_sections(monkeypatch):
    saved: list[list[str]] = []
    monkeypatch.setattr(fa, "_save_briefing_prefs", lambda sections: saved.append(sections) or True)

    reply = fa._handle_briefing_command(" weather, focus ")

    assert saved == [["focus", "weather"]]
    assert reply.startswith("🌅 Briefing updated.")


def test_briefing_command_reset_restores_all_sections(monkeypatch):
    saved: list[list[str]] = []
    monkeypatch.setattr(fa, "_save_briefing_prefs", lambda sections: saved.append(sections) or True)

    fa._handle_briefing_command("reset")

    assert saved == [list(fa.BRIEFING_SECTION_NAMES)]


def test_briefing_command_rejects_unknown_section(monkeypatch):
    monkeypatch.setattr(
        fa,
        "_save_briefing_prefs",
        lambda _sections: (_ for _ in ()).throw(AssertionError("must not save")),
    )

    reply = fa._handle_briefing_command("focus nonsense")

    assert "unknown section: nonsense" in reply


def test_briefing_command_excludes_one_section_from_current_selection(monkeypatch):
    monkeypatch.setattr(fa, "_briefing_prefs", lambda: list(fa.BRIEFING_SECTION_NAMES))
    saved: list[list[str]] = []
    monkeypatch.setattr(fa, "_save_briefing_prefs", lambda sections: saved.append(sections) or True)

    fa._handle_briefing_command("-weather")

    assert saved == [[name for name in fa.BRIEFING_SECTION_NAMES if name != "weather"]]


def test_briefing_command_includes_one_section_back(monkeypatch):
    monkeypatch.setattr(fa, "_briefing_prefs", lambda: ["focus"])
    saved: list[list[str]] = []
    monkeypatch.setattr(fa, "_save_briefing_prefs", lambda sections: saved.append(sections) or True)

    fa._handle_briefing_command("+weather")

    assert saved == [["focus", "weather"]]


def test_briefing_command_mixes_include_and_exclude_tokens(monkeypatch):
    monkeypatch.setattr(fa, "_briefing_prefs", lambda: ["focus", "goals"])
    saved: list[list[str]] = []
    monkeypatch.setattr(fa, "_save_briefing_prefs", lambda sections: saved.append(sections) or True)

    fa._handle_briefing_command("-goals +journal")

    assert saved == [["focus", "journal"]]


def test_briefing_command_treats_bare_token_as_implicit_add_in_incremental_mode(monkeypatch):
    monkeypatch.setattr(fa, "_briefing_prefs", lambda: ["goals"])
    saved: list[list[str]] = []
    monkeypatch.setattr(fa, "_save_briefing_prefs", lambda sections: saved.append(sections) or True)

    fa._handle_briefing_command("focus -weather")

    assert saved == [["focus", "goals"]]


def test_briefing_command_incremental_rejects_unknown_section(monkeypatch):
    monkeypatch.setattr(
        fa,
        "_save_briefing_prefs",
        lambda _sections: (_ for _ in ()).throw(AssertionError("must not save")),
    )

    reply = fa._handle_briefing_command("-nonsense")

    assert "unknown section: nonsense" in reply


def test_briefing_command_reports_save_failure(monkeypatch):
    monkeypatch.setattr(fa, "_save_briefing_prefs", lambda _sections: False)

    assert fa._handle_briefing_command("focus") == (
        "I could not save your briefing preferences. Please try again later."
    )


def test_webhook_routes_briefing_command(monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "sec")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "7")
    monkeypatch.setattr(fa, "_handle_briefing_command", lambda arg: f"sections:{arg.strip()}")

    sent: list[str] = []
    monkeypatch.setattr(fa, "_telegram_send", lambda _chat_id, text: sent.append(text))

    resp = fa.telegram_webhook(DummyRequest(_webhook_payload("/briefing focus"), "sec"))

    assert resp.status_code == 200
    assert sent == ["sections:focus"]


def test_local_briefing_skips_weather_when_disabled(monkeypatch):
    monkeypatch.setattr(
        fa,
        "_load_briefing",
        lambda: {
            "date": "2026-08-11",
            "today_focus": "ship it",
            "vault_state": {"inbox": {"count": 1, "oldest_age_days": 2}},
            "sections": ["focus", "vault"],
        },
    )
    monkeypatch.setattr(
        fa,
        "_weather_summary",
        lambda _location: (_ for _ in ()).throw(AssertionError("weather must not be fetched")),
    )

    text = fa._compose_local_briefing()

    assert "ship it" in text
    assert "Weather" not in text


def test_briefing_seed_mentions_only_enabled_sections():
    seed = fa._briefing_seed(["focus", "weather"])

    assert "get_weather" in seed
    assert "up to 120 words" in seed
    assert "vault_state" not in seed
    assert "open_loops" not in seed


def test_briefing_seed_without_sections_asks_for_a_plain_note():
    seed = fa._briefing_seed([])

    assert "do not call any tools" in seed
