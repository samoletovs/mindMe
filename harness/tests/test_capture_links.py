from __future__ import annotations

from copy import deepcopy
from unittest.mock import Mock

import pytest

import function_app as fa
from capture_links import normalize_message_links
from test_runtime_reliability import request

ARTICLE = "https://example.com/article"
TIKTOK = "https://vm.tiktok.com/ZMpublic/"


@pytest.fixture
def capture(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "7")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setenv("MINDME_ACTION_BRIEFING_ENABLED", "false")
    monkeypatch.setenv("MINDME_DAILY_EVOLVE_ENABLED", "false")
    forward = Mock(return_value=True)
    companion = Mock(side_effect=AssertionError("Links must not reach the companion"))
    monkeypatch.setattr(fa, "_forward_to_memex", forward)
    monkeypatch.setattr(fa, "_ask_companion", companion)
    return forward, companion


@pytest.mark.parametrize("url", [ARTICLE, TIKTOK])
@pytest.mark.parametrize("message_key", ["message", "edited_message"])
@pytest.mark.parametrize("shape", ["bare", "commentary", "caption", "named", "caption_named", "schemeless"])
def test_every_link_shape_reaches_same_capture_pipeline(capture, url, message_key, shape):
    field = "caption" if shape.startswith("caption") else "text"
    entity_field = "caption_entities" if field == "caption" else "entities"
    message = {"chat": {"id": 7}, "message_id": 19}
    prefix = "\U0001f4ce Read: "
    if shape in {"named", "caption_named"}:
        message[field] = prefix + "this"
        message[entity_field] = [{
            "type": "text_link", "offset": 9, "length": 4, "url": url,
        }]
        expected = prefix + url
    elif shape == "schemeless":
        message[field] = url.removeprefix("https://")
        message[entity_field] = [{"type": "url", "offset": 0, "length": len(message[field])}]
        expected = url
    else:
        expected = ("Read this: " if shape == "commentary" else "") + url
        message[field] = expected
    if field == "caption":
        message["photo"] = [{"file_id": "synthetic-photo"}]
    payload = {"update_id": 123, message_key: message}
    original = deepcopy(payload)
    forward, companion = capture

    assert fa.telegram_webhook(request(payload)).status_code == 200

    forward.assert_called_once()
    forwarded = forward.call_args.args[0]
    assert forwarded["update_id"] == 123
    assert set(forwarded) == set(payload)
    assert forwarded[message_key][field] == expected
    assert forwarded[message_key]["chat"] == message["chat"]
    assert forwarded[message_key]["message_id"] == 19
    assert payload == original
    companion.assert_not_called()


def test_named_link_preserves_commentary_and_removes_stale_offsets():
    message = {"text": "Read this please", "entities": [
        {"type": "text_link", "offset": 5, "length": 4, "url": ARTICLE},
        {"type": "bold", "offset": 0, "length": 4},
    ]}
    result = normalize_message_links(message)
    assert result["text"] == f"Read {ARTICLE} please"
    assert "entities" not in result


def test_multiple_links_keep_original_order_and_unmodified_input():
    message = {"text": "one two", "entities": [
        {"type": "text_link", "offset": 4, "length": 3, "url": TIKTOK},
        {"type": "text_link", "offset": 0, "length": 3, "url": ARTICLE},
    ]}
    assert normalize_message_links(message)["text"] == f"{ARTICLE} {TIKTOK}"
    assert message["text"] == "one two"


@pytest.mark.parametrize("separator", [",", "", "\u2014"])
def test_adjacent_named_links_remain_distinct_urls(separator):
    message = {"text": f"Read one{separator}two", "entities": [
        {"type": "text_link", "offset": 5, "length": 3, "url": ARTICLE},
        {"type": "text_link", "offset": 8 + len(separator), "length": 3, "url": TIKTOK},
    ]}
    normalized = normalize_message_links(message)["text"]
    assert fa._URL_RE.findall(normalized) == [ARTICLE, TIKTOK]
    assert separator in normalized


@pytest.mark.parametrize("before,after", [
    ("Read ", "\u2014recommended"),
    ("Read(", ")please"),
    ("prefix", "suffix"),
])
def test_named_link_has_boundaries_without_losing_commentary(before, after):
    message = {"text": before + "this" + after, "entities": [
        {"type": "text_link", "offset": len(before), "length": 4, "url": ARTICLE},
    ]}
    normalized = normalize_message_links(message)["text"]
    assert fa._URL_RE.findall(normalized) == [ARTICLE]
    assert normalized.startswith(before)
    assert normalized.endswith(after)


@pytest.mark.parametrize("entity", [
    {"type": "text_link", "offset": -1, "length": 1, "url": ARTICLE},
    {"type": "text_link", "offset": True, "length": 1, "url": ARTICLE},
    {"type": "text_link", "offset": 0, "length": 100, "url": ARTICLE},
    {"type": "text_link", "offset": 0, "length": 1, "url": None},
    {"type": "text_link", "offset": 0, "length": 1, "url": "https://example.com/\nfoo"},
])
def test_malformed_links_are_rejected_without_forward_or_companion(capture, entity):
    forward, companion = capture
    response = fa.telegram_webhook(request({
        "update_id": 123,
        "message": {"chat": {"id": 7}, "text": "link", "entities": [entity]},
    }))
    assert response.status_code == 400
    forward.assert_not_called()
    companion.assert_not_called()


def test_utf16_surrogate_split_is_rejected():
    with pytest.raises(ValueError):
        normalize_message_links({"text": "\U0001f4ce link", "entities": [
            {"type": "text_link", "offset": 1, "length": 2, "url": ARTICLE},
        ]})


def test_non_web_link_is_not_converted_into_a_capture():
    message = {"text": "mail", "entities": [
        {"type": "text_link", "offset": 0, "length": 4, "url": "mailto:synthetic@example.invalid"},
    ]}
    assert normalize_message_links(message) is message


def test_owner_check_precedes_link_normalization(capture):
    forward, companion = capture
    response = fa.telegram_webhook(request({
        "message": {"chat": {"id": 8}, "caption": ARTICLE, "caption_entities": "invalid"},
    }))
    assert response.status_code == 200
    forward.assert_not_called()
    companion.assert_not_called()


def test_failed_link_forward_remains_retryable(capture):
    forward, companion = capture
    forward.return_value = False
    assert fa.telegram_webhook(request({
        "update_id": 123, "message": {"chat": {"id": 7}, "caption": TIKTOK},
    })).status_code == 503
    companion.assert_not_called()


def test_refresh_without_url_reaches_memex_usage_instead_of_companion(capture):
    forward, _ = capture
    assert fa.telegram_webhook(request({
        "update_id": 123, "message": {"chat": {"id": 7}, "text": "/refresh"},
    })).status_code == 200
    forward.assert_called_once()


def test_dig_with_named_link_preserves_research_command(monkeypatch, capture):
    forward, _ = capture
    dig = Mock(return_value=("https://example.invalid/research/1", "created"))
    monkeypatch.setattr(fa, "_create_dig_issue", dig)
    monkeypatch.setattr(fa, "_telegram_send", Mock())
    assert fa.telegram_webhook(request({
        "message": {"chat": {"id": 7}, "text": "/dig this", "entities": [
            {"type": "text_link", "offset": 5, "length": 4, "url": ARTICLE},
        ]},
    })).status_code == 200
    dig.assert_called_once_with(ARTICLE)
    forward.assert_not_called()
