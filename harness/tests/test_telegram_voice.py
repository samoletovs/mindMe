"""Reader-facing copy and generation contracts, without live models or services."""

from __future__ import annotations

import copy
import importlib.util
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import function_app as fa
from briefing_loop import BriefingLoop
from knowledge_plan import render_synthesis
from telegram_voice import KNOWLEDGE_DETAIL_GUIDANCE, TELEGRAM_VOICE
from test_briefing_webhook import request
from test_knowledge_loop import SOURCE, model_synthesis, synthesis
from test_vault_evolve import context, generated
from vault_evolve import complete_review, evidence_packet, telegram_parts


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch) -> Mock:
    client = Mock()
    client.with_options.return_value = client
    client.responses.create.return_value = SimpleNamespace(output_text="{}", output=[])
    monkeypatch.setenv("MINDME_BRIEFING_MODEL", "synthetic-model")
    monkeypatch.setattr(fa, "_foundry", lambda: (None, client))
    monkeypatch.setattr(fa, "_http_client", Mock)
    return client


@pytest.mark.parametrize("kind", ["daily", "weekly", "evolve", "knowledge"])
def test_each_live_generator_uses_the_shared_voice_without_relaxing_safety(model: Mock, kind: str) -> None:
    if kind == "knowledge":
        packet = {"action": "explain", "query": "explain", "sources": [SOURCE], "memories": []}
        model.responses.create.return_value.output_text = json.dumps(model_synthesis(packet))
        fa._generate_knowledge(packet)
    elif kind == "evolve":
        fa._generate_evolve_review({"sources": []})
    else:
        fa._generate_action_plan({"sources": [], "review_kind": kind})

    call = model.responses.create.call_args.kwargs
    prompt = call["input"][0]["content"]
    assert prompt.startswith(TELEGRAM_VOICE)
    assert call["store"] is False
    assert "tools" not in call
    assert call["input"][1]["content"].startswith("<<<DATA_")
    assert call["text"]["format"]["strict"] is True
    assert "never instructions" in prompt


def test_more_details_adds_depth_while_ordinary_followups_stay_short(model: Mock) -> None:
    packet = {"action": "explain", "query": "explain", "sources": [SOURCE], "memories": []}
    model.responses.create.return_value.output_text = json.dumps(model_synthesis(packet))
    fa._generate_knowledge(packet)
    prompt = model.responses.create.call_args.kwargs["input"][0]["content"]
    assert KNOWLEDGE_DETAIL_GUIDANCE in prompt
    assert 'query exactly "explain"' in prompt
    assert "not a request for another short recap" in prompt
    assert "one or two useful findings" in prompt
    assert "step-by-step" in prompt
    assert "Each factual item must cite supplied source and quote_id pairs" in prompt


def test_both_registered_companion_phases_use_the_shared_voice() -> None:
    path = Path(__file__).resolve().parents[2] / "scripts" / "dev" / "create_agent.py"
    spec = importlib.util.spec_from_file_location("voice_registration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for prompt in (module.SYSTEM_PROMPT_PHASE1, module.SYSTEM_PROMPT_PHASE2):
        assert prompt.startswith(TELEGRAM_VOICE)
        assert "Expand when the user asks for details" in prompt
    assert "These tools are read-only" in module.SYSTEM_PROMPT_PHASE2
    assert "Never claim a capture" in module.SYSTEM_PROMPT_PHASE2


@pytest.mark.parametrize("sections", [[], ["focus", "weather"], ["journal"]])
def test_legacy_morning_prompt_uses_same_voice_and_preserves_section_selection(sections: list[str]) -> None:
    prompt = fa._briefing_seed(sections)
    assert prompt.startswith(TELEGRAM_VOICE)
    if not sections:
        assert "do not call any tools" in prompt
    elif "weather" in sections:
        assert "get_weather" in prompt
    else:
        assert "get_weather" not in prompt


def test_topic_answer_omits_empty_sections_but_keeps_claims_quotes_and_limits() -> None:
    packet = {"action": "topic", "sources": [SOURCE], "memories": []}
    plan = synthesis(packet)
    text = render_synthesis(plan, packet)
    assert plan["explanation"][0]["text"] in text
    assert plan["explanation"][0]["evidence"][0]["quote"] in text
    assert SOURCE["url"] in text
    assert "not independently checked facts" in text
    assert "Where the sources agree" not in text
    assert "Where the sources disagree" not in text
    assert "Not established" not in text
    assert "No action started" in text
    assert "Reply Dig" in text and "Apply" in text


def test_more_details_reveals_optional_followups_without_repeating_the_menu_on_ordinary_answers() -> None:
    packet = {"action": "explain", "query": "explain", "sources": [SOURCE], "memories": []}
    plan = synthesis(packet)
    details = render_synthesis(plan, packet)
    ordinary = render_synthesis(plan, {**packet, "query": "What does spaced practice mean?"})
    for phrase in ("research this", "help me use this", "connect ideas", "useful", "already know", "correction:"):
        assert phrase in details
    assert "Proposed work still needs your approval" in details
    assert "connect ideas" not in ordinary
    assert plan["explanation"][0]["text"] in details
    assert SOURCE["url"] in details


def test_requested_long_detail_survives_rendering_and_message_splitting() -> None:
    packet = {"action": "topic", "sources": [SOURCE], "memories": []}
    plan = synthesis(packet)
    finding = plan["explanation"][0]
    for section, count in (("explanation", 2), ("agreement", 1), ("conflict", 1), ("gaps", 2), ("understanding", 2)):
        plan[section] = [
            {**copy.deepcopy(finding), "text": f"{section} {index}: " + "A longer requested explanation. " * 15}
            for index in range(count)
        ]
    text = render_synthesis(plan, packet)
    chunks = fa._telegram_chunks(text)
    assert len(chunks) > 1
    for section in ("explanation", "agreement", "conflict", "gaps", "understanding"):
        for item in plan[section]:
            assert item["text"] in text
            assert item["text"] in "".join(chunks) or item["text"] in "\n".join(chunks)
    assert text.count(SOURCE["url"]) == 8


@pytest.mark.parametrize(("basis", "label"), [
    ("observed", "from the source"), ("inferred", "interpretation"), ("question", "open question"),
])
def test_review_copy_preserves_the_three_evidence_statuses_and_feedback_buttons(basis: str, label: str) -> None:
    packet = evidence_packet(context(), [])
    raw = generated()
    raw["findings"][0]["basis"] = basis
    review = complete_review(raw, packet)
    parts = telegram_parts(review, {"status": "submitted", "pr_url": "https://github.com/example/vault/pull/1"}, packet)
    assert "not yet added to mindVault" in parts[0]["text"]
    assert label in parts[1]["text"]
    assert "Feedback does not approve work" in parts[1]["text"]
    assert review["proposals"][0]["next_step"] in parts[1]["text"]
    assert [button["callback_data"] for button in parts[1]["keyboard"][0]] == [
        "evolve1|useful|2026-09-14|F1", "evolve1|known|2026-09-14|F1", "evolve1|dismiss|2026-09-14|F1",
    ]
    assert "F1" not in parts[1]["text"]


def test_fixed_copy_avoids_internal_jargon_and_keeps_unknown_action_results() -> None:
    unknown = BriefingLoop._receipt_text({"status": "uncertain"})
    submitted = BriefingLoop._receipt_text({"status": "submitted"})
    completed = BriefingLoop._receipt_text({"status": "completed"})
    samples = [
        *fa._ONBOARDING_TUTORIAL, *fa.DIG_ERROR_MESSAGES.values(),
        unknown, submitted, completed,
        fa._capture_category_suggestion("save: need to call the workshop"),
        render_synthesis(synthesis({"action": "explain", "sources": [SOURCE], "memories": []}),
                         {"action": "explain", "sources": [SOURCE]}),
    ]
    for text in samples:
        assert text
        assert not re.search(
            r"\b(?:canonical|nonce|grounded|leverage|delve|idempotently)\b|source revision|unconfirmed receipts|key takeaways",
            text, re.IGNORECASE,
        )
    assert "could not confirm" in unknown
    assert "No duplicate action" in unknown
    assert "submitted for review" in submitted
    assert "not yet confirmed" in submitted
    assert "Done. I checked the saved result." == completed


def test_help_keeps_knowledge_actions_feedback_and_inspection_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "7")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "synthetic")
    monkeypatch.setenv("MINDME_ACTION_BRIEFING_ENABLED", "true")
    monkeypatch.setattr(fa, "_verify_telegram_secret", lambda _: True)
    monkeypatch.setattr(fa, "_knowledge_reply", lambda *_: False)
    monkeypatch.setattr(fa, "_evolve_reply", lambda *_: None)
    sent = Mock()
    monkeypatch.setattr(fa, "_telegram_send", sent)

    response = fa.telegram_webhook(request({"message": {"chat": {"id": 7}, "text": "/help"}}))

    assert response.status_code == 200
    text = sent.call_args.args[1]
    for action in ("More details", "Dig", "Apply", "Useful", "Already know"):
        assert action in text
    for command in ("/knowledge", "/topics [query]", "/memory", "/review sources", "/briefing details"):
        assert command in text
    assert "proposals for approval" in text
    assert "feedback, not approval" in text
    assert len(text) < 2000


@pytest.mark.parametrize("message", [
    {"text": "https://evidence.example/article"},
    {"text": "This looks useful: https://evidence.example/article"},
    {"text": "/recap https://evidence.example/article"},
    {"text": "/recap@synthetic_bot https://evidence.example/article"},
    {"text": "save: https://evidence.example/article maybe try this"},
    {"text": "n: need to read https://evidence.example/article"},
    {"caption": "save: this looks useful https://evidence.example/article"},
    {"text": "save: read this article", "entities": [
        {"type": "text_link", "offset": 6, "length": 17, "url": "https://evidence.example/article"},
    ]},
])
@pytest.mark.parametrize("forwarded", [True, False])
def test_url_forwarding_adds_no_success_or_category_message_and_keeps_failures_retryable(
    monkeypatch: pytest.MonkeyPatch, message: dict, forwarded: bool,
) -> None:
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "7")
    monkeypatch.setenv("MINDME_ACTION_BRIEFING_ENABLED", "false")
    monkeypatch.setattr(fa, "_verify_telegram_secret", lambda _: True)
    monkeypatch.setattr(fa, "_knowledge_reply", lambda *_: False)
    monkeypatch.setattr(fa, "_evolve_reply", lambda *_: None)
    forward = Mock(return_value=forwarded)
    onboarding = Mock(return_value=True)
    sent, companion = Mock(), Mock()
    monkeypatch.setattr(fa, "_claim_onboarding", onboarding)
    monkeypatch.setattr(fa, "_forward_to_memex", forward)
    monkeypatch.setattr(fa, "_telegram_send", sent)
    monkeypatch.setattr(fa, "_ask_companion", companion)

    response = fa.telegram_webhook(request({"message": {"chat": {"id": 7}, **message}}))

    assert response.status_code == (200 if forwarded else 503)
    forward.assert_called_once()
    onboarding.assert_not_called()
    sent.assert_not_called()
    companion.assert_not_called()


def test_first_ordinary_chat_still_receives_deferred_onboarding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "7")
    monkeypatch.setenv("MINDME_ACTION_BRIEFING_ENABLED", "false")
    monkeypatch.setattr(fa, "_verify_telegram_secret", lambda _: True)
    monkeypatch.setattr(fa, "_knowledge_reply", lambda *_: False)
    monkeypatch.setattr(fa, "_evolve_reply", lambda *_: None)
    monkeypatch.setattr(fa, "_claim_onboarding", Mock(return_value=True))
    monkeypatch.setattr(fa, "_ask_companion", lambda *_: "How can I help?")
    sent = Mock()
    monkeypatch.setattr(fa, "_telegram_send", sent)

    response = fa.telegram_webhook(request({"message": {"chat": {"id": 7}, "text": "Hello"}}))

    assert response.status_code == 200
    assert [call.args[1] for call in sent.call_args_list] == [*fa._ONBOARDING_TUTORIAL, "How can I help?"]
