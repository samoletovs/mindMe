from __future__ import annotations

import function_app as fa


class DummyRequest:
    def __init__(self, payload: dict, secret: str) -> None:
        self._payload = payload
        self.headers = {"X-Telegram-Bot-Api-Secret-Token": secret}

    def get_json(self) -> dict:
        return self._payload


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
