"""Focused tests for quick-capture routing and the summary command in ``function_app``."""

from __future__ import annotations

from datetime import date

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
        "That sounds actionable — next time use /task so it can turn into an open loop."
    )


def test_capture_category_suggestion_points_reflection_to_diary():
    assert app._capture_category_suggestion("n: today felt heavier than expected") == (
        "That reads like a journal entry — next time use /diary so it lands with your daily log."
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
        (123, "That sounds actionable — next time use /task so it can turn into an open loop.")
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
