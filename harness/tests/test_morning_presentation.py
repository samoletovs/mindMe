from __future__ import annotations

from datetime import date
from html.parser import HTMLParser
from unittest.mock import Mock

import pytest

import function_app as fa
from briefing_plan import render_briefing, render_briefing_details
from telegram_format import inline_text, units, word_count
from test_briefing_webhook import request
from weekly_plan import render_weekly_proposal

TODAY = date(2026, 9, 21)
SOURCE_URL = "https://github.com/example/vault/blob/main/tasks/example.md"


class TelegramHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[str] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        assert tag in {"b", "i", "a", "code"}
        if tag == "a":
            assert dict(attrs)["href"].startswith("https://")
        self.tags.append(tag)

    def handle_endtag(self, tag: str) -> None:
        assert self.tags.pop() == tag

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def context() -> dict:
    return {
        "tasks": [
            {"path": "tasks/example.md", "title": "Check the application",
             "next_action": "Read the [support reply](https://help.example.com/tickets/123) before replying.",
             "deadline": "2026-09-22", "url": SOURCE_URL},
        ],
        "warnings": ["Some source excerpts are shortened.", "Some source records were not inspected."],
        "extras": {
            "freshness": {"status": "stale", "age_days": 53},
            "weather": "Light rain, 14 C.",
        },
    }


def plan(data: dict) -> dict:
    return {
        "focus": "Check the application", "focus_task": data["tasks"][0],
        "focus_path": "tasks/example.md", "changes": [], "proposal": None,
    }


def test_normal_morning_is_a_short_valid_message_with_named_links_and_no_duplicate_task() -> None:
    data = context()
    text, _ = render_briefing(plan(data), data, TODAY)
    parsed = TelegramHTML()
    parsed.feed(text)
    assert not parsed.tags
    visible = "".join(parsed.text)
    assert len(visible.split()) <= 190
    assert units(text) <= 3200
    assert "https://" not in visible
    assert "[support reply]" not in visible
    assert visible.count("Check the application") == 1
    assert "Due tomorrow (22 Sep)" in visible
    assert "53 days old" in visible
    assert "Source notice:" not in visible
    assert "/briefing details" in visible


def test_summary_bounds_do_not_lose_full_task_wording_or_due_inventory() -> None:
    data = context()
    data["tasks"][0]["next_action"] = "Do not send until permission arrives. " * 60
    data["tasks"].extend([
        {"path": f"tasks/extra-{n}.md", "title": f"Unique task {n}",
         "review_on": "2026-09-20", "next_action": f"Exact step {n}."}
        for n in range(30)
    ])
    summary, _ = render_briefing(plan(data), data, TODAY)
    assert units(summary) <= 3200
    assert word_count(summary) <= 190
    assert "28 more date-relevant tasks" in summary
    assert "Full wording" in summary
    details = render_briefing_details(data, TODAY)
    assert data["tasks"][0]["next_action"] in details
    for task in data["tasks"]:
        assert task["title"] in details
        assert task["next_action"] in details
    for warning in data["warnings"]:
        assert warning in details


def test_untrusted_html_is_text_and_unsafe_markdown_links_cannot_become_clickable() -> None:
    rendered = inline_text(
        '<b>Not bold</b> & [bad](javascript:alert) '
        '[secret](https://example.com/?token=secret) [ok](https://example.com/read)'
    )
    assert "&lt;b&gt;" in rendered
    assert "javascript:" not in rendered
    assert "?token=" not in rendered
    assert rendered.count("<a ") == 1


@pytest.mark.parametrize("value", ["&<> " * 200, "\U0001f642 " * 350, "a " * 350])
def test_extreme_inputs_stay_bounded_without_broken_entities(value: str) -> None:
    data = context()
    data["tasks"][0].update(title=value, next_action=value, waiting_for=value)
    generated = plan(data)
    generated["changes"] = [
        {"source": {"path": f"notes/{n}.md", "title": value, "url": SOURCE_URL},
         "why": "A decision-relevant update, not evidence of completed work."}
        for n in range(2)
    ]
    text, _ = render_briefing(generated, data, TODAY)
    assert units(text) <= 3200
    assert word_count(text) <= 190
    parser = TelegramHTML()
    parser.feed(text)
    assert not parser.tags


@pytest.mark.parametrize("status", ["stale", "unknown", "missing"])
def test_noncurrent_snapshot_is_not_read_or_given_to_the_model(monkeypatch, status: str) -> None:
    monkeypatch.setattr(fa, "_mirror_freshness", lambda _: {"status": status, "age_days": 53})
    readers = ["_vault_state", "_read_os_text", "_stale_areas_state"]
    for name in readers:
        monkeypatch.setattr(fa, name, Mock(side_effect=AssertionError("Must not read old private context")))
    result = fa._action_briefing_extras(["vault", "journal", "areas"])
    assert result["signals"] == []
    assert result["warnings"]
    assert result["freshness"]["status"] == status


@pytest.mark.parametrize("kind,label", [
    ("research", "Start research"), ("create_task", "Draft task"), ("review_task", "Select next step"),
])
def test_daily_cards_show_exact_action_scope_and_only_bound_approval(kind: str, label: str) -> None:
    proposal = {
        "kind": kind, "id": "a" * 24, "text": "Compare <A> & B before deciding.",
        "why": "Choose one small next step.", "source_url": SOURCE_URL,
    }
    card, keyboard = render_weekly_proposal(proposal, 1, 1)
    parser = TelegramHTML()
    parser.feed(card)
    assert proposal["text"] in "".join(parser.text)
    assert "<b>Scope</b>" in card
    assert keyboard[0][0] == {"text": label, "callback_data": "brief1|approve|" + "a" * 24}
    if kind == "review_task":
        assert "does not complete it" in card


def test_details_command_is_owner_only_and_never_changes_preferences_or_starts_work(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "7")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "synthetic")
    monkeypatch.setenv("MINDME_ACTION_BRIEFING_ENABLED", "true")
    loop = Mock()
    loop.details.return_value = "Full current context"
    monkeypatch.setattr(fa, "_briefing_loop", lambda: loop)
    monkeypatch.setattr(fa, "_briefing_prefs", lambda: ["loops"])
    sent, save, companion = Mock(), Mock(), Mock()
    monkeypatch.setattr(fa, "_telegram_send", sent)
    monkeypatch.setattr(fa, "_save_briefing_prefs", save)
    monkeypatch.setattr(fa, "_ask_companion", companion)
    for chat in (8, 7):
        response = fa.telegram_webhook(request({"message": {"chat": {"id": chat}, "text": "/briefing details"}}))
        assert response.status_code == 200
    loop.details.assert_called_once()
    loop.deliver.assert_not_called()
    loop.execute.assert_not_called()
    sent.assert_called_once_with(7, "Full current context")
    save.assert_not_called()
    companion.assert_not_called()


def test_details_feature_off_does_not_become_a_preference_change(monkeypatch) -> None:
    save = Mock()
    monkeypatch.setattr(fa, "_save_briefing_prefs", save)
    assert "preferences are unchanged" in fa._handle_briefing_command("details")
    save.assert_not_called()


def test_real_loop_factory_wires_html_sender_without_changing_plain_legacy_sender(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "7")
    monkeypatch.setenv("DIG_GITHUB_TOKEN", "synthetic")
    monkeypatch.setattr(fa, "_http_client", Mock())
    monkeypatch.setattr(fa, "_os_container_client", Mock())
    sent = Mock(return_value=1)
    monkeypatch.setattr(fa, "_telegram_proposal_send", sent)
    loop = fa._briefing_loop()
    loop.send_html("<b>Morning focus</b>", None)
    sent.assert_called_with(7, "<b>Morning focus</b>", None, parse_mode="HTML")
    loop.send("Legacy <text>", None)
    sent.assert_called_with(7, "Legacy <text>", None)
