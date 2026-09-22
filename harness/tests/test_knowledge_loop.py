"""Behavioral end-to-end tests with synthetic private state and no external services."""

from __future__ import annotations

import copy
import json
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

import function_app as fa
from briefing_loop import BriefingLoop
from briefing_sources import SourceError, load_topic_sources, read_knowledge_source
from briefing_state import BriefingStore, StateError, _encode, empty_state
from knowledge_context import capture_context, parse_capture_callback
from knowledge_loop import KnowledgeLoop
from knowledge_plan import (
    MAX_QUOTE_CHARS, MAX_QUOTES_PER_SOURCE, SECTION_LIMITS, KnowledgeError,
    hydrate_synthesis, knowledge_evidence_packet, knowledge_model_schema, validate_synthesis,
)
from knowledge_state import CAPS, consolidate, decide_write, recall
from test_briefing_sources import HEAD, REPO, TOKEN, Vault, blob
from test_briefing_state import FakeBlob
from test_briefing_webhook import request

TODAY = date(2026, 9, 22)
PATH = "wiki/sources/spaced-learning.md"
TEXT = "Spaced practice improves delayed recall. Immediate performance does not establish durable learning."
SOURCE = {
    "path": PATH, "revision": "a" * 40, "digest": "b" * 64,
    "kind": "wiki", "title": "Spaced learning", "text": TEXT,
    "url": "https://github.com/example/mindVault/blob/" + "a" * 40 + "/" + PATH,
}
POINTER = {
    "version": 1, "status": "ready", "source_id": "c" * 64, "source_path": PATH,
    "source_url": "https://evidence.example/article", "title": "Spaced learning",
}


class Store:
    def __init__(self) -> None:
        self.state = empty_state()

    def read(self) -> dict:
        return copy.deepcopy(self.state)

    def update(self, mutate):
        state = self.read()
        result = mutate(state)
        _encode(state)
        self.state = state
        return copy.deepcopy(result)


def synthesis(packet: dict) -> dict:
    source = packet["sources"][0]
    feedback = any(item["kind"] == "feedback" for item in packet["memories"])
    return {
        "explanation": [{
            "text": "Skip the familiar basics; compare delayed recall." if feedback else "Spaced practice concerns durable rather than immediate learning.",
            "evidence": [{"path": source["path"], "quote": "Spaced practice improves delayed recall."}],
        }],
        "agreement": [], "conflict": [], "gaps": [],
        "understanding": [],
        "continuity": "Explained 1) spacing and 2) delayed recall, with immediate performance as the caveat.",
        "experiment": "Try one short spaced-practice exercise and compare recall the next day.",
        "used_memory_ids": [item["id"] for item in packet["memories"]],
        "proposal": {"text": "Which public studies compare spaced practice and delayed recall?"} if packet["action"] == "dig" else (
            {"text": "Try one short spaced-practice experiment and compare recall the next day."}
            if packet["action"] == "apply" else None
        ),
    }


def model_synthesis(context: dict) -> dict:
    """Actual Responses wire shape: host-issued IDs, never model-written quotes."""
    raw = synthesis(context)
    packet = knowledge_evidence_packet(context)
    first = packet["sources"][0]
    for key in SECTION_LIMITS:
        for item in raw[key]:
            item["evidence"] = [{"source": first["id"], "quote_id": next(iter(first["quotes"]))}]
    return raw


@pytest.fixture
def system():
    store, sent, executed, packets = Store(), [], [], []
    current = {PATH: copy.deepcopy(SOURCE)}

    def send(text, keyboard=None):
        sent.append((text, keyboard))
        return len(sent) + 100

    def execute(record, reconcile):
        executed.append((copy.deepcopy(record), reconcile))
        return {"status": "submitted", "issue_url": "https://github.com/example/mindVault/issues/1"}

    briefing = BriefingLoop(
        store=store, sources=lambda *_: {}, loops=lambda: {}, generate=lambda _: {},
        send=send, revision=lambda path: current.get(path, {}).get("revision"),
        execute=execute, extras=lambda _: {},
    )

    def generate(packet):
        packets.append(copy.deepcopy(packet))
        return synthesis(packet)

    lookup = Mock(return_value=copy.deepcopy(POINTER))
    retrieve = Mock(side_effect=lambda *_: {
        "sources": list(current.values()), "warnings": ["At most 16 reads, five selected sources."],
    })
    loop = KnowledgeLoop(
        store=store, briefing=briefing, lookup=lookup,
        read=lambda path: copy.deepcopy(current.get(path)), retrieve=retrieve,
        generate=generate, send=lambda text: [send(text)],
    )
    return SimpleNamespace(
        loop=loop, store=store, sent=sent, executed=executed,
        packets=packets, current=current, briefing=briefing, lookup=lookup, retrieve=retrieve,
    )


def handle(
    system, text="Explain the second idea", *, action=None, event="one", message_id=50, key=None,
    shown_text="Ideas shown in this message:\n1. Spacing\n2. Delayed recall.",
):
    return system.loop.handle(
        message_id=message_id, text=text, action=action, event=event,
        today=TODAY, capture_key=key, shown_text=shown_text,
    )


def test_followup_injects_current_canonical_source_and_requested_context_not_generic_companion(system):
    assert handle(system)
    packet = system.packets[0]
    assert packet["query"] == "Explain the second idea"
    assert packet["sources"][0]["text"] == TEXT
    assert SOURCE["url"] in system.sent[0][0]
    assert not system.executed


def test_numbered_followup_targets_actual_displayed_idea_not_canonical_note_number(system):
    system.current[PATH]["text"] = (
        "Canonical ideas:\n1. Spaced practice improves delayed recall.\n"
        "2. Immediate performance does not establish durable learning."
    )
    shown = (
        "Displayed takeaways:\n1. Immediate performance is a limited measure.\n"
        "2. Spaced practice improves delayed recall.\nDISPLAY_ONLY_MARKER"
    )

    def generate(packet):
        reference = packet["shown_message_reference"]
        assert reference["text"] == shown
        assert reference["is_evidence"] is False and reference["grants_permission"] is False
        assert packet["sources"][0]["text"].startswith("Canonical ideas:\n1. Spaced practice")
        result = synthesis(packet)
        result["explanation"][0]["text"] = "Your displayed second takeaway concerns spaced practice and delayed recall."
        return result

    system.loop.generate = generate
    handle(system, "Explain the second idea", shown_text=shown)
    assert "displayed second takeaway concerns spaced practice" in system.sent[0][0]
    assert SOURCE["url"] in system.sent[0][0]
    assert "DISPLAY_ONLY_MARKER" not in json.dumps(system.store.state)
    assert "shown_message_reference" not in json.dumps(system.store.state)
    assert not system.executed


@pytest.mark.parametrize("bullet", ["•", "-", "*"])
def test_actual_memex_key_ideas_bullets_disambiguate_displayed_order(system, bullet):
    system.current[PATH]["text"] = (
        "Canonical note:\n1. Spaced practice improves delayed recall.\n"
        "2. Immediate performance does not establish durable learning."
    )
    shown = (
        "Article recap\n\nWhat it says — key ideas\n"
        f"{bullet} Immediate performance is a limited measure.\n"
        f"{bullet} Spaced practice improves delayed recall.\n\n"
        "Caveats\n• These are claims, not independent verification."
    )
    handle(system, "Explain the second idea", shown_text=shown)
    assert len(system.packets) == 1
    packet = system.packets[0]
    assert packet["shown_message_reference"]["text"] == shown
    assert packet["shown_message_reference"]["is_evidence"] is False
    assert packet["sources"][0]["text"].startswith("Canonical note:\n1. Spaced practice")
    assert "Please quote" not in system.sent[0][0]
    assert not system.executed


@pytest.mark.parametrize("shown", [
    "What it says — key ideas\n• Only the first main idea is in this message.",
    "• The second original idea continues here.\n• The third original idea follows.",
    "What it says — key ideas\n• Only one main idea.\n\nCaveats\n• First caveat.\n• Second caveat.",
    "Caveats\n• First caveat.\n• Second caveat.",
])
def test_split_or_other_section_bullets_do_not_guess_original_ordinal(system, shown):
    handle(system, "Explain the second idea", shown_text=shown)
    assert "Please quote" in system.sent[0][0]
    assert not system.packets and not system.executed
    assert not system.store.state["knowledge"]["memories"]


@pytest.mark.parametrize("query,shown", [
    ("Explain the second idea", None),
    ("Explain takeaway 2", ""),
    ("Explain the second idea", "Summary saved."),
    ("Explain the second idea", "Only one displayed takeaway.\n1. Spacing matters."),
    ("Dig into evidence against it", None),
    ("Apply this", None),
    ("Explain the second idea", "2. " + "x" * 4097),
])
def test_ambiguous_reference_without_adequate_shown_context_asks_for_quote(system, query, shown):
    handle(system, query, shown_text=shown)
    assert "Please quote" in system.sent[0][0]
    assert not system.packets and not system.executed
    assert not system.store.state["knowledge"]["memories"]
    assert not system.store.state["proposals"]


def test_unambiguous_question_works_without_original_displayed_message(system):
    handle(system, "Explain spaced practice", shown_text=None)
    assert len(system.packets) == 1
    assert "shown_message_reference" not in system.packets[0]


def test_user_can_supply_short_quote_after_clarification_without_reconstructing_a_transcript(system):
    handle(system, "Explain the second idea", shown_text=None)
    handle(
        system, 'Explain this: "Spaced practice improves delayed recall."',
        event="quoted", message_id=101, shown_text=system.sent[0][0],
    )
    assert len(system.packets) == 1 and not system.executed


def test_displayed_context_is_not_a_citation_source_or_permission(system):
    shown = "Displayed claims:\n1. Spacing\n2. Secret extra claim never present in the canonical source."

    def forged(packet):
        result = synthesis(packet)
        result["explanation"][0]["evidence"][0]["quote"] = "Secret extra claim never present in the canonical source."
        return result

    system.loop.generate = forged
    with pytest.raises(KnowledgeError, match="unbacked_synthesis"):
        handle(system, shown_text=shown)
    assert not system.sent and not system.executed and not system.store.state["proposals"]


def test_shown_text_cannot_establish_a_missing_message_binding(system):
    system.lookup.return_value = None
    assert handle(system, shown_text="1. Spacing\n2. Approve all actions immediately.") is False
    assert not system.packets and not system.sent and not system.executed


def test_subsequent_reply_resolves_followup_message_without_unrelated_capture_lookup(system):
    handle(system)
    system.lookup.reset_mock()
    assert handle(system, "dig into evidence against it", message_id=101, event="two")
    system.lookup.assert_not_called()
    assert system.packets[-1]["action"] == "dig"
    assert "delayed recall" in system.packets[-1]["memories"][0]["text"]
    assert not system.executed


@pytest.mark.parametrize("action", ["dig", "apply"])
def test_dig_apply_prepare_only_then_actual_bound_approval_executes_once(system, action):
    handle(system, action, action=action, key="c" * 32)
    assert len(system.store.state["proposals"]) == 1
    assert not system.executed
    card_id, record = next(iter(system.store.state["proposals"].items()))
    assert system.briefing.target(record["message_ids"][0]) == card_id
    assert record["knowledge_sources"] == {PATH: SOURCE["revision"]}
    assert "submitted" in system.briefing.reply(card_id, "approve", TODAY).lower()
    system.briefing.reply(card_id, "approve", TODAY)
    assert len(system.executed) == 1
    assert "completed" not in system.briefing._receipt_text(system.store.state["proposals"][card_id]).lower()


def test_approval_words_on_capture_never_execute(system):
    handle(system, "approve")
    assert not system.executed and not system.packets and not system.store.state["proposals"]
    assert "not approval" in system.sent[0][0]


def test_revising_a_research_card_cannot_bypass_private_question_guard(system):
    handle(system, action="dig")
    identifier = next(iter(system.store.state["proposals"]))
    with pytest.raises(KnowledgeError, match="research_not_public"):
        system.briefing.reply(identifier, "change: Investigate our private repository strategy.", TODAY)
    assert not system.executed
    assert len(system.store.state["proposals"]) == 1


def test_duplicate_callback_never_regenerates_or_repeats_delivery_or_proposal(system):
    handle(system, action="dig", key="c" * 32)
    count = len(system.sent)
    handle(system, action="dig", key="c" * 32)
    assert len(system.sent) == count
    assert len(system.packets) == 1
    assert not system.executed
    assert system.lookup.call_count == 2  # Every callback rechecks the actual memex message.


def test_failed_send_records_uncertainty_and_duplicate_retry_does_not_resend(system):
    sender = Mock(side_effect=RuntimeError("synthetic delivery failure"))
    system.loop.send = sender
    with pytest.raises(RuntimeError):
        handle(system)
    assert next(iter(system.store.state["knowledge"]["requests"].values()))["status"] == "uncertain"
    assert not system.store.state["knowledge"]["memories"]  # Not falsely recorded as explained.
    handle(system)
    assert sender.call_count == 1


def test_confirmed_early_chunk_remains_bound_when_later_chunk_fails(system):
    attempts = []

    def send_parts(text):
        attempts.append("first")
        yield 801
        assert system.store.state["knowledge"]["bindings"]["801"]["sources"] == {PATH: SOURCE["revision"]}
        attempts.append("second")
        raise RuntimeError("synthetic later-part failure")

    system.loop.send_parts = send_parts
    with pytest.raises(RuntimeError, match="later-part"):
        handle(system)
    receipt = next(iter(system.store.state["knowledge"]["requests"].values()))
    assert receipt["status"] == "uncertain" and receipt["message_ids"] == [801]
    assert system.loop.resolve(801, TODAY)["sources"][0]["path"] == PATH
    handle(system)
    assert attempts == ["first", "second"]
    assert not system.executed and not system.store.state["knowledge"]["memories"]


def test_production_factory_checkpoints_first_telegram_id_before_second_part_failure(monkeypatch, system):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "7")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "synthetic")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic")
    monkeypatch.setenv("MINDME_ACTION_BRIEFING_ENABLED", "true")
    calls = []

    def telegram_transport(request):
        assert request.url.path == "/botsynthetic/sendMessage"
        calls.append(json.loads(request.content))
        if len(calls) == 2:
            return httpx.Response(503, json={"ok": False})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 850 + len(calls)}})

    client = httpx.Client(transport=httpx.MockTransport(telegram_transport))
    lookup = Mock(return_value=copy.deepcopy(POINTER))
    generate = Mock(side_effect=synthesis)
    monkeypatch.setattr(fa, "_http_client", lambda: client)
    monkeypatch.setattr(fa, "_briefing_loop", lambda: system.briefing)
    monkeypatch.setattr(fa, "capture_context", lookup)
    monkeypatch.setattr(fa, "read_knowledge_source", lambda *_, **__: copy.deepcopy(SOURCE))
    monkeypatch.setattr(fa, "_generate_knowledge", generate)
    parts = [
        "Confirmed first source-bound explanation part.",
        "Second source-bound explanation part.",
    ]
    monkeypatch.setattr(fa, "_telegram_chunks", lambda text: [text] if text in parts else parts)
    monkeypatch.setattr(fa, "_evolve_reply", lambda *_: None)
    monkeypatch.setattr(fa, "_proposal_reply", lambda *_: None)
    forwarded, companion = Mock(), Mock()
    monkeypatch.setattr(fa, "_forward_to_memex", forwarded)
    monkeypatch.setattr(fa, "_ask_companion", companion)
    original = {
        "update_id": 100,
        "message": {"message_id": 99, "chat": {"id": 7}, "text": "Explain spaced practice",
                    "reply_to_message": {"message_id": 51}},
    }

    assert fa.telegram_webhook(request(original)).status_code == 503
    assert system.store.state["knowledge"]["bindings"]["851"]["sources"] == {PATH: SOURCE["revision"]}
    receipt = next(iter(system.store.state["knowledge"]["requests"].values()))
    assert receipt["status"] == "uncertain" and receipt["message_ids"] == [851]
    assert fa.telegram_webhook(request(original)).status_code == 200
    assert len(calls) == 2 and generate.call_count == 1  # No automatic resend/regeneration.

    followup = {
        "update_id": 101,
        "message": {"message_id": 100, "chat": {"id": 7}, "text": "Explain delayed recall",
                    "reply_to_message": {"message_id": 851, "text": calls[0]["text"]}},
    }
    assert fa.telegram_webhook(request(followup)).status_code == 200
    assert generate.call_count == 2 and len(calls) == 4
    assert generate.call_args.args[0]["sources"][0]["path"] == PATH
    lookup.assert_called_once()  # The confirmed early part uses its private binding.
    forwarded.assert_not_called()
    companion.assert_not_called()
    assert not system.executed


def test_expired_unexecuted_proposal_can_be_renewed_from_a_fresh_recap(system):
    handle(system, action="apply")
    old_id = next(iter(system.store.state["proposals"]))
    later = TODAY + timedelta(days=15)
    system.loop.handle(
        message_id=51, text="apply", event="fresh-recap", today=later,
        action="apply", capture_key="c" * 32,
    )
    records = system.store.state["proposals"]
    assert len(records) == 2 and records[old_id]["status"] == "expired"
    new_id = next(identifier for identifier in records if identifier != old_id)
    assert records[new_id]["created_on"] == later.isoformat()
    assert records[new_id]["status"] == "pending"
    assert records[new_id]["text"] == records[old_id]["text"]
    assert "expired" in system.briefing.reply(old_id, "approve", later).lower()
    assert not system.executed
    system.briefing.reply(new_id, "approve", later)
    system.briefing.reply(new_id, "approve", later)
    assert len(system.executed) == 1


def test_fresh_recap_cannot_renew_already_submitted_action_even_after_expiry(system):
    handle(system, action="apply")
    identifier = next(iter(system.store.state["proposals"]))
    system.briefing.reply(identifier, "approve", TODAY)
    system.loop.handle(
        message_id=51, text="apply", event="fresh-recap", today=TODAY + timedelta(days=15),
        action="apply", capture_key="c" * 32,
    )
    assert len(system.store.state["proposals"]) == 1
    assert len(system.executed) == 1
    assert system.store.state["proposals"][identifier]["status"] == "submitted"


def test_explicit_familiarity_changes_later_response_and_marks_memory_used(system):
    handle(system, action="known", key="c" * 32)
    identifier = next(iter(system.store.state["knowledge"]["memories"]))
    assert system.store.state["knowledge"]["memories"][identifier]["use_count"] == 0
    handle(system, event="two")
    assert "Skip the familiar basics" in system.sent[-1][0]
    memory = system.store.state["knowledge"]["memories"][identifier]
    assert memory["use_count"] == 1 and memory["last_used_on"] == TODAY.isoformat()


def test_forgotten_feedback_no_longer_influences_responses(system):
    handle(system, action="known")
    identifier = next(iter(system.store.state["knowledge"]["memories"]))
    system.loop.command(f"/knowledge forget {identifier}", TODAY, "forget")
    system.loop.command(f"/knowledge forget {identifier}", TODAY, "forget-again")
    handle(system, event="two")
    assert "Skip the familiar basics" not in system.sent[-1][0]
    assert not system.packets[-1]["memories"]


def test_forget_cascades_to_later_working_memory_derived_from_feedback(system):
    handle(system, action="known")
    identifier = next(iter(system.store.state["knowledge"]["memories"]))
    handle(system, event="explain")
    assert len(system.store.state["knowledge"]["memories"]) == 2
    system.loop.command("/knowledge forget " + identifier, TODAY, "forget")
    assert not system.store.state["knowledge"]["memories"]


def test_correction_supersedes_working_assumption_without_permanent_interest(system):
    handle(system)
    first = next(iter(system.store.state["knowledge"]["memories"]))
    handle(system, "correction: The second idea needs a delayed-recall test.", event="two")
    records = system.store.state["knowledge"]["memories"]
    correction = next(item for item in records.values() if item["kind"] == "correction")
    assert correction["supersedes"] == [first] and not records[first]["active"]
    assert "expires_on" not in correction
    assert all(item["kind"] != "preference" for item in records.values())


def test_correction_on_knowledge_proposal_influences_later_source_followup(system):
    handle(system, action="apply")
    identifier = next(iter(system.store.state["proposals"]))
    system.briefing.reply(identifier, "correction: Compare delayed recall rather than immediate performance.", TODAY)
    handle(system, event="later")
    assert any(item["kind"] == "correction" and "delayed recall" in item["text"] for item in system.packets[-1]["memories"])
    assert not system.executed


def test_changed_source_invalidates_old_bindings_memory_and_pending_approval(system):
    handle(system, action="apply")
    proposal_id = next(iter(system.store.state["proposals"]))
    system.current[PATH]["revision"] = "d" * 40
    with pytest.raises(KnowledgeError, match="source_changed"):
        handle(system, event="two")
    assert not system.store.state["knowledge"]["memories"]
    assert system.store.state["knowledge"]["bindings"]["50"]["invalidated"]
    with pytest.raises(KnowledgeError, match="context_expired"):
        handle(system, event="three", key="c" * 32)
    assert "source was removed" in system.briefing.reply(proposal_id, "approve", TODAY).lower()
    assert not system.executed


def test_source_change_during_generation_prevents_retention_delivery_and_actions(system):
    def generate(packet):
        system.current[PATH]["revision"] = "d" * 40
        return synthesis(packet)

    system.loop.generate = generate
    with pytest.raises(KnowledgeError):
        handle(system, action="apply")
    assert not system.sent and not system.executed and not system.store.state["proposals"]
    assert not system.store.state["knowledge"]["memories"]


def test_deleted_source_erases_topic_and_memory_but_preserves_external_action_receipt(system):
    handle(system, action="topic")
    handle(system, action="apply", event="two")
    proposal_id = next(iter(system.store.state["proposals"]))
    system.briefing.reply(proposal_id, "approve", TODAY)
    system.current.clear()
    system.loop.maintenance(TODAY)
    assert not system.store.state["knowledge"]["topics"]
    assert not system.store.state["knowledge"]["memories"]
    assert system.store.state["proposals"][proposal_id]["status"] == "submitted"


def test_topic_command_retrieves_once_retains_inspectable_revision_receipt(system):
    system.loop.command("/topics spaced learning", TODAY, "topic-one")
    system.retrieve.assert_called_once()
    identifier, topic = next(iter(system.store.state["knowledge"]["topics"].items()))
    assert topic["sources"] == {PATH: SOURCE["revision"]}
    for heading in ("Agreement", "Conflict", "Evidence gaps", "New understanding"):
        assert heading in topic["text"]
    system.loop.command("/topics " + identifier, TODAY, "inspect")
    assert topic["text"] in system.sent[-1][0]
    system.loop.command("/topics forget " + identifier, TODAY, "forget")
    assert not system.store.state["knowledge"]["topics"]


@pytest.mark.parametrize("command,collection", [("/topics", "topics"), ("/knowledge", "memories")])
def test_paginated_five_source_records_remain_inspectable_and_deletable(system, command, collection):
    identifiers = []
    for index in range(5):
        refs = {}
        for number in range(5):
            path = f"notes/topic-{index}-source-{number}.md"
            system.current[path] = {**SOURCE, "path": path}
            refs[path] = SOURCE["revision"]
        if collection == "topics":
            identifier = f"{index + 1:024x}"
            system.store.state["knowledge"]["topics"][identifier] = {
                "sources": refs, "text": f"Topic receipt {index}",
                "created_on": TODAY.isoformat(), "expires_on": (TODAY + timedelta(days=35)).isoformat(),
            }
        else:
            identifier = decide_write(
                system.store.state, kind="working", text=f"Compared source group {index}.",
                sources=refs, today=TODAY,
            )
        identifiers.append(identifier)
    identifiers.sort()
    reader = Mock(side_effect=system.loop.read)
    system.loop.read = reader
    system.loop.command(command, TODAY, "page-one")
    assert reader.call_count == 15
    assert all(identifier in system.sent[-1][0] for identifier in identifiers[2:])
    assert all(identifier not in system.sent[-1][0] for identifier in identifiers[:2])
    reader.reset_mock()
    system.loop.command(command + " 2", TODAY, "page-two")
    assert reader.call_count == 10
    assert all(identifier in system.sent[-1][0] for identifier in identifiers[:2])
    assert all(identifier not in system.sent[-1][0] for identifier in identifiers[2:])
    target = identifiers[0]  # Obtain a deletion ID from the second page, not hidden state.
    assert target in system.sent[-1][0]
    system.loop.command(command + " forget " + target, TODAY, "delete-visible-record")
    system.loop.command(command + " forget " + target, TODAY, "repeat-safe-deletion")
    assert target not in system.store.state["knowledge"][collection]
    assert len(system.store.state["knowledge"][collection]) == 4


def test_two_source_topic_keeps_conflict_quotes_gaps_and_optional_experiment(system):
    other = {
        **SOURCE, "path": "notes/immediate-performance.md", "revision": "d" * 40,
        "text": "Massed practice can improve immediate performance. This does not measure delayed recall.",
    }
    system.current[other["path"]] = other

    def compare(packet):
        result = synthesis(packet)
        result["conflict"] = [{
            "text": "The sources emphasize different test intervals; this is tension, not a causal refutation.",
            "evidence": [
                {"path": PATH, "quote": "Spaced practice improves delayed recall."},
                {"path": other["path"], "quote": "Massed practice can improve immediate performance."},
            ],
        }]
        result["gaps"] = [{
            "text": "What happens when both approaches are tested after the same delay?",
            "evidence": [{"path": other["path"], "quote": "This does not measure delayed recall."}],
        }]
        result["understanding"] = [{
            "text": "Different measurement intervals may explain apparently conflicting recommendations.",
            "evidence": [{"path": PATH, "quote": "Immediate performance does not establish durable learning."}],
        }]
        return result

    system.loop.generate = compare
    handle(system, action="topic")
    topic = next(iter(system.store.state["knowledge"]["topics"].values()))
    assert len(topic["sources"]) == 2
    assert "Massed practice" in topic["text"]
    assert "What happens when" in topic["text"]
    assert "Different measurement intervals" in topic["text"]
    assert "Optional experiment (proposal only)" in topic["text"]
    assert not system.store.state["proposals"] and not system.executed


def test_approval_checks_every_topic_source_not_only_the_primary(system):
    other = {**SOURCE, "path": "notes/other.md", "revision": "d" * 40}
    system.current[other["path"]] = other
    handle(system, action="topic")
    handle(system, "apply this", message_id=101, event="apply-two")
    identifier = next(iter(system.store.state["proposals"]))
    system.current[other["path"]]["revision"] = "e" * 40
    assert "knowledge evidence changed" in system.briefing.reply(identifier, "approve", TODAY).lower()
    assert not system.executed


def test_memory_not_reported_as_used_does_not_reset_decay(system):
    handle(system, action="known")
    identifier = next(iter(system.store.state["knowledge"]["memories"]))

    def unused(packet):
        result = synthesis(packet)
        result["used_memory_ids"] = []
        return result

    system.loop.generate = unused
    handle(system, event="two")
    assert system.store.state["knowledge"]["memories"][identifier]["use_count"] == 0


def test_maintenance_does_not_read_sources_for_an_inactive_owner(system):
    handle(system)
    reader = Mock(side_effect=AssertionError("No source read for inactive owner"))
    system.loop.read = reader
    system.loop.maintenance(TODAY + timedelta(days=15))
    assert not system.store.state["knowledge"]["memories"]
    reader.assert_not_called()


def test_user_can_explicitly_represent_pending_card_but_retry_never_repeats_it(system):
    handle(system, action="apply")
    identifier = next(iter(system.store.state["proposals"]))
    before = len(system.sent)
    system.loop.command(f"/knowledge proposal {identifier}", TODAY, "re-present")
    assert len(system.sent) == before + 1
    system.loop.command(f"/knowledge proposal {identifier}", TODAY, "re-present")
    assert len(system.sent) == before + 1 and not system.executed
    assert len(system.store.state["proposals"][identifier]["message_ids"]) == 2


def test_request_capacity_fails_before_generation_or_delivery_without_eviction(system):
    for index in range(CAPS["requests"]):
        system.store.state["knowledge"]["requests"][str(index)] = {
            "status": "uncertain", "created_on": TODAY.isoformat(), "expires_on": TODAY.isoformat(),
        }
    with pytest.raises(StateError, match="capacity"):
        handle(system)
    assert len(system.store.state["knowledge"]["requests"]) == CAPS["requests"]
    assert not system.packets and not system.sent and not system.executed


def test_expired_topic_is_not_returned_by_exact_id_inspection(system):
    handle(system, action="topic")
    identifier = next(iter(system.store.state["knowledge"]["topics"]))
    system.loop.command("/topics " + identifier, TODAY + timedelta(days=35), "inspect")
    assert not system.store.state["knowledge"]["topics"]
    assert "Spaced practice concerns" not in system.sent[-1][0]


def test_forgetting_feedback_invalidates_topic_derived_from_that_feedback(system):
    handle(system, action="known")
    identifier = next(iter(system.store.state["knowledge"]["memories"]))
    handle(system, action="topic", event="topic")
    assert system.store.state["knowledge"]["topics"]
    system.loop.command("/knowledge forget " + identifier, TODAY, "forget")
    assert not system.store.state["knowledge"]["topics"]


def test_model_cannot_forge_quotes_or_citations(system):
    packet = {"sources": [SOURCE], "memories": [], "action": "explain"}
    raw = synthesis(packet)
    raw["explanation"][0]["evidence"][0]["quote"] = "This entirely fabricated claim never appeared."
    with pytest.raises(KnowledgeError, match="unbacked_synthesis"):
        validate_synthesis(raw, packet)
    raw = synthesis(packet)
    raw["explanation"][0]["evidence"][0]["path"] = "notes/invented.md"
    with pytest.raises(KnowledgeError):
        validate_synthesis(raw, packet)


def test_comparison_cannot_claim_agreement_from_one_source():
    packet = {"sources": [SOURCE], "memories": [], "action": "topic"}
    raw = synthesis(packet)
    raw["agreement"] = raw["explanation"]
    with pytest.raises(KnowledgeError, match="two_sources"):
        validate_synthesis(raw, packet)


@pytest.mark.parametrize("text", [
    "How should our private repository implement this?",
    "Compare my employer's internal service.",
    "Compare salary: 5000 across staff.",
    "What should sample@example.test do?",
])
def test_private_context_never_enters_public_research_proposal(text):
    packet = {"sources": [SOURCE], "memories": [], "action": "dig"}
    raw = synthesis(packet)
    raw["proposal"]["text"] = text
    with pytest.raises((KnowledgeError, ValueError)):
        validate_synthesis(raw, packet)


def test_read_failure_is_not_an_empty_vault_or_unknown_source(system):
    system.loop.read = Mock(side_effect=SourceError("source_unavailable"))
    with pytest.raises(SourceError):
        handle(system)
    assert not system.packets and not system.sent and not system.executed


def test_unknown_or_unpublished_capture_does_not_select_an_unrelated_source(system):
    system.lookup.return_value = None
    assert handle(system) is False
    assert not system.packets and not system.sent and not system.executed


def test_ready_capture_waits_for_canonical_publication_without_claiming_personal_knowledge(system):
    system.current.clear()
    with pytest.raises(KnowledgeError, match="canonical_source_pending_or_unavailable"):
        handle(system, action="dig", key="c" * 32)
    assert not system.packets and not system.sent and not system.executed
    assert not system.store.state["knowledge"]["requests"]
    assert not system.store.state["knowledge"]["memories"]
    assert not system.store.state["knowledge"]["bindings"]
    system.current[PATH] = copy.deepcopy(SOURCE)
    assert handle(system, action="dig", key="c" * 32)
    assert len(system.store.state["proposals"]) == 1
    assert not system.executed


@pytest.mark.parametrize("value", ["cap1|delete|" + "a" * 32, "cap1|apply|A" * 32, "cap1|apply", None, {}, "cap1|dig|" + "a" * 64])
def test_malformed_or_unknown_callback_has_no_action(value):
    assert parse_capture_callback(value) is None


def test_capture_context_contract_sends_exact_actual_message_owner_and_key():
    calls = []
    def transport(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=POINTER)

    client = httpx.Client(transport=httpx.MockTransport(transport))
    assert capture_context(client, url="https://synthetic.test/api", chat_id=7, message_id=51, capture_key="c" * 32) == POINTER
    assert calls == [{
        "operation": "capture_context", "version": 1, "chat_id": 7, "message_id": 51, "capture_key": "c" * 32,
    }]


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_capture_context_accepts_http_source_provenance_without_changing_backend_endpoint(scheme):
    pointer = {**POINTER, "source_url": f"{scheme}://evidence.example/article"}
    backend = "https://synthetic.test/api?code=synthetic-function-key"
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(200, json=pointer)

    client = httpx.Client(transport=httpx.MockTransport(transport))
    assert capture_context(client, url=backend, chat_id=7, message_id=51, capture_key="c" * 32) == pointer
    assert [str(request.url) for request in requests] == [backend]


@pytest.mark.parametrize("response", [
    {"version": 1, "status": "unmatched", "source_path": PATH},
    {**POINTER, "version": True},
    {**POINTER, "source_path": "wiki/sources/../../private.md"},
    {**POINTER, "source_url": "https://user:password@synthetic.test/"},
    {**POINTER, "source_url": "http://user:password@synthetic.test/"},
    {**POINTER, "source_url": "http://@synthetic.test/"},
    {**POINTER, "source_url": "http:///article"},
    {**POINTER, "source_url": "ftp://evidence.example/article"},
    {**POINTER, "source_url": "http://evidence.example/ar\nticle"},
    {**POINTER, "source_url": "https://evidence.example/ar\tticle"},
    {**POINTER, "source_url": "http://evidence.example/article\x7f"},
    {**POINTER, "source_id": "e" * 64},
])
def test_capture_context_rejects_malformed_and_mismatched_pointers(response):
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response)))
    with pytest.raises(SourceError):
        capture_context(client, url="https://synthetic.test/api", chat_id=7, message_id=51, capture_key="c" * 32)


def test_capture_context_unavailable_has_no_fallback():
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(503)))
    with pytest.raises(SourceError, match="unavailable"):
        capture_context(client, url="https://synthetic.test/api", chat_id=7, message_id=51)


def test_memory_duplicate_credential_capacity_expiry_and_supersession():
    state = empty_state()
    refs = {PATH: SOURCE["revision"]}
    identifier = decide_write(state, kind="working", text="Explained spaced learning.", sources=refs, today=TODAY)
    assert decide_write(state, kind="working", text="Explained spaced learning.", sources=refs, today=TODAY) == identifier
    assert len(state["knowledge"]["memories"]) == 1
    with pytest.raises(StateError):
        decide_write(state, kind="working", text="token=synthetic-secret", sources=refs, today=TODAY)
    consolidate(state, revisions={}, today=TODAY + timedelta(days=14))
    assert not state["knowledge"]["memories"]
    for index in range(CAPS["memories"]):
        state["knowledge"]["memories"][str(index)] = {
            "kind": "correction", "text": f"Synthetic assumption {index}", "active": False,
            "sources": refs, "created_on": TODAY.isoformat(),
        }
    with pytest.raises(StateError, match="capacity"):
        decide_write(state, kind="correction", text="Do not silently evict unresolved information.", sources=refs, today=TODAY)


def test_old_state_upgrades_without_resetting_receipts():
    state = empty_state()
    state.pop("knowledge")
    blob = FakeBlob(state)
    store = BriefingStore(SimpleNamespace(get_blob_client=lambda _: blob))
    assert store.read()["knowledge"] == {"memories": {}, "bindings": {}, "requests": {}, "topics": {}}


def test_query_rank_selects_relevant_canonical_notes_before_alphabetical_noise(monkeypatch):
    monkeypatch.setenv("DIG_REPO", REPO)
    files = {f"notes/noise-{index:02}.md": "# Other\nUnrelated gardening evidence." for index in range(30)}
    files[PATH] = "# Spaced learning\n" + TEXT
    vault = Vault(files)
    result = load_topic_sources(vault.client, token=TOKEN, repo=REPO, query="spaced learning")
    assert [item["path"] for item in result["sources"]] == [PATH]
    assert result["coverage"]["read_files"] <= 16
    assert result["complete"] is False


def test_direct_source_citation_uses_pinned_commit_not_file_blob_sha(monkeypatch):
    monkeypatch.setenv("DIG_REPO", REPO)
    raw = "# Spaced learning\n" + TEXT
    vault = Vault({PATH: raw})
    source = read_knowledge_source(vault.client, token=TOKEN, repo=REPO, path=PATH)
    assert source["revision"] == blob(raw)
    assert source["revision"] != HEAD
    assert source["url"] == f"https://github.com/{REPO}/blob/{HEAD}/{PATH}"
    assert all(request.url.params["ref"] == HEAD for request in vault.reads)


@pytest.mark.parametrize("raw", [
    "---\nprivate: true\n---\n# Spaced learning\n" + TEXT,
    "---\nignored: true\n---\n# Spaced learning\n" + TEXT,
    "---\ngenerated: true\n---\n# Spaced learning\n" + TEXT,
    "---\nscope: work\n---\n# Spaced learning\n" + TEXT,
])
def test_reader_reuses_privacy_ignore_and_derived_exclusions(monkeypatch, raw):
    monkeypatch.setenv("DIG_REPO", REPO)
    vault = Vault({PATH: raw})
    assert read_knowledge_source(vault.client, token=TOKEN, repo=REPO, path=PATH) is None


@pytest.fixture
def webhook(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "7")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "synthetic")
    monkeypatch.setenv("MINDME_ACTION_BRIEFING_ENABLED", "true")
    loop = Mock()
    loop.handle.return_value = True
    monkeypatch.setattr(fa, "_knowledge_loop", lambda: loop)
    monkeypatch.setattr(fa, "_evolve_reply", lambda *_: None)
    monkeypatch.setattr(fa, "_proposal_reply", lambda *_: None)
    sent, forward, companion = Mock(), Mock(), Mock()
    monkeypatch.setattr(fa, "_telegram_send", sent)
    monkeypatch.setattr(fa, "_forward_to_memex", forward)
    monkeypatch.setattr(fa, "_ask_companion", companion)
    return loop, sent, forward, companion


@pytest.mark.parametrize("chat_id", [8, None, "7"])
def test_other_chats_cannot_resolve_capture_or_read_memory(webhook, chat_id):
    loop, sent, forward, companion = webhook
    response = fa.telegram_webhook(request({"callback_query": {
        "message": {"chat": {"id": chat_id}, "message_id": 51}, "data": "cap1|dig|" + "c" * 32,
    }}))
    assert response.status_code == 200
    loop.handle.assert_not_called()
    sent.assert_not_called()
    forward.assert_not_called()
    companion.assert_not_called()


@pytest.mark.parametrize("field", ["text", "caption"])
def test_real_webhook_binds_actual_callback_message_and_capture_key(webhook, field):
    loop, _, forward, companion = webhook
    response = fa.telegram_webhook(request({"callback_query": {
        "message": {"chat": {"id": 7}, "message_id": 51, field: "Displayed recap: first spacing, then recall."},
        "data": "cap1|dig|" + "c" * 32,
    }}))
    assert response.status_code == 200
    assert loop.handle.call_args.kwargs["message_id"] == 51
    assert loop.handle.call_args.kwargs["capture_key"] == "c" * 32
    assert loop.handle.call_args.kwargs["shown_text"] == "Displayed recap: first spacing, then recall."
    forward.assert_not_called()
    companion.assert_not_called()


@pytest.mark.parametrize("message_type", ["message", "edited_message"])
@pytest.mark.parametrize("field", ["text", "caption"])
def test_reply_with_link_stays_bound_before_capture_or_companion(webhook, message_type, field):
    loop, _, forward, companion = webhook
    response = fa.telegram_webhook(request({message_type: {
        "chat": {"id": 7}, "message_id": 99, "text": "Compare this https://evidence.example/article",
        "reply_to_message": {"message_id": 51, field: "Actual displayed recap text, not canonical ordering."},
    }}))
    assert response.status_code == 200
    assert loop.handle.call_args.kwargs["message_id"] == 51
    assert loop.handle.call_args.kwargs["shown_text"] == "Actual displayed recap text, not canonical ordering."
    forward.assert_not_called()
    companion.assert_not_called()


def test_lookup_failure_is_retryable_never_unrelated_capture(webhook):
    loop, _, forward, companion = webhook
    loop.handle.side_effect = SourceError("capture_context_unavailable")
    response = fa.telegram_webhook(request({"message": {
        "chat": {"id": 7}, "text": "explain https://evidence.example/a", "reply_to_message": {"message_id": 51},
    }}))
    assert response.status_code == 503
    forward.assert_not_called()
    companion.assert_not_called()


def test_malformed_callback_is_not_forwarded_to_another_action_engine(webhook):
    loop, _, forward, _ = webhook
    response = fa.telegram_webhook(request({"callback_query": {
        "message": {"chat": {"id": 7}, "message_id": 51}, "data": "cap1|unknown|" + "c" * 32,
    }}))
    assert response.status_code == 200
    loop.handle.assert_not_called()
    forward.assert_not_called()


@pytest.mark.parametrize("text", ["/recap", "/recap https://evidence.example/article", "/recap@synthetic_bot"])
def test_recap_url_preserves_existing_memex_forward_path(webhook, text):
    loop, _, forward, companion = webhook
    forward.return_value = True
    response = fa.telegram_webhook(request({"message": {
        "chat": {"id": 7}, "text": text,
    }}))
    assert response.status_code == 200
    forward.assert_called_once()
    loop.handle.assert_not_called()
    companion.assert_not_called()


def test_model_call_is_bounded_stateless_and_memory_is_nonce_fenced(monkeypatch):
    monkeypatch.setenv("MINDME_BRIEFING_MODEL", "existing-model")
    packet = {"action": "explain", "query": "Explain", "sources": [SOURCE], "memories": []}
    client = Mock()
    client.with_options.return_value.responses.create.return_value.output_text = json.dumps(model_synthesis(packet))
    monkeypatch.setattr(fa, "_foundry", lambda: (None, client))
    monkeypatch.setattr(fa, "_http_client", lambda: Mock())
    fa._generate_knowledge(packet)
    call = client.with_options.return_value.responses.create.call_args.kwargs
    assert call["store"] is False and call["max_output_tokens"] == 2800
    assert "tools" not in call
    assert call["input"][1]["content"].startswith("<<<DATA_")
    assert "<<<END_DATA_" in call["input"][1]["content"]


def test_actual_generation_restores_unicode_quotes_and_paths_from_host_ids(monkeypatch):
    monkeypatch.setenv("MINDME_BRIEFING_MODEL", "existing-model")
    first = {
        **SOURCE,
        "text": "DORA’s “four keys” describe delivery performance over time. A score is not a causal explanation.",
    }
    second = {
        **SOURCE, "path": "wiki/sources/short-video.md", "title": "Short video metrics",
        "text": "A short video reports immediate engagement, not delayed comprehension. Viewing is not familiarity.",
    }
    context = {
        "action": "topic", "query": "Compare delivery and short-video measurement.",
        "sources": [first, second], "memories": [], "warnings": [],
    }
    packet = knowledge_evidence_packet(context)
    raw = model_synthesis(context)
    raw["continuity"] = "Compared delivery performance and immediate engagement, distinguishing observed metrics from causal evidence."
    raw["experiment"] = "Compare a leading indicator with one delayed outcome before treating engagement as learning."
    raw["explanation"] = [{
        "text": "The sources describe different observation windows.",
        "evidence": [{"source": "S1", "quote_id": "S1Q1"}, {"source": "S2", "quote_id": "S2Q1"}],
    }]
    raw["gaps"] = [{
        "text": "Neither excerpt establishes that its metric causes the desired outcome.",
        "evidence": [{"source": "S1", "quote_id": "S1Q2"}],
    }]
    client = Mock()
    client.with_options.return_value.responses.create.return_value.output_text = json.dumps(raw)
    monkeypatch.setattr(fa, "_foundry", lambda: (None, client))
    monkeypatch.setattr(fa, "_http_client", lambda: Mock())

    restored = fa._generate_knowledge(context)
    validated = validate_synthesis(restored, context)

    assert validated["explanation"][0]["evidence"] == [
        {"path": first["path"], "quote": packet["sources"][0]["quotes"]["S1Q1"]},
        {"path": second["path"], "quote": packet["sources"][1]["quotes"]["S2Q1"]},
    ]
    assert "DORA’s “four keys”" in validated["explanation"][0]["evidence"][0]["quote"]
    args = client.with_options.return_value.responses.create.call_args.kwargs
    model_packet = json.loads(args["input"][1]["content"].split("\n", 1)[1].rsplit("\n", 1)[0])
    assert all("text" not in source for source in model_packet["sources"])
    assert "verbatim quote" not in args["input"][0]["content"]
    assert "eight findings TOTAL" in args["input"][0]["content"]
    choices = args["text"]["format"]["schema"]["$defs"]["knowledge_citation"]["anyOf"]
    assert choices[0]["properties"]["source"]["enum"] == ["S1"]
    assert all(identifier.startswith("S1Q") for identifier in choices[0]["properties"]["quote_id"]["enum"])


@pytest.mark.parametrize("citation", [
    {"source": "S9", "quote_id": "S1Q1"},
    {"source": "S1", "quote_id": "S1Q999"},
    {"source": "S1", "quote_id": "S2Q1"},
    {"source": "S2", "quote_id": "S1Q1"},
    {"path": PATH, "quote": "Spaced practice improves delayed recall."},
    {"source": "S1", "quote_id": "S1Q1", "quote": "Invented extra text"},
])
def test_unknown_or_cross_source_quote_ids_cannot_be_hydrated(citation):
    context = {
        "action": "topic", "query": "Compare", "memories": [],
        "sources": [SOURCE, {**SOURCE, "path": "notes/other.md"}],
    }
    packet = knowledge_evidence_packet(context)
    raw = model_synthesis(context)
    raw["explanation"][0]["evidence"] = [citation]
    with pytest.raises(KnowledgeError, match="invalid_evidence_reference"):
        hydrate_synthesis(raw, packet)


def test_generation_schema_and_hydrator_enforce_eight_total_findings():
    context = {"action": "explain", "query": "Explain", "sources": [SOURCE], "memories": []}
    packet = knowledge_evidence_packet(context)
    schema = knowledge_model_schema(packet)
    assert sum(schema["properties"][key]["maxItems"] for key in SECTION_LIMITS) == 8
    raw = model_synthesis(context)
    raw["explanation"] *= 3
    with pytest.raises(KnowledgeError, match="invalid_synthesis_sections"):
        hydrate_synthesis(raw, packet)
    # The existing validator still supports legacy/loop fixtures, not only wire-format IDs.
    legacy = synthesis(context)
    legacy["explanation"] *= 3
    assert len(validate_synthesis(legacy, context)["explanation"]) == 3


def test_maximum_evidence_schema_stays_within_structured_outputs_enum_limit():
    text = " ".join(f"Fact {number:02} remains as stated." for number in range(50))
    assert len(text) <= 1500
    context = {
        "action": "topic", "query": "Compare all five sources.",
        "sources": [{**SOURCE, "path": f"wiki/sources/fixture-{index}.md", "text": text} for index in range(5)],
        "memories": [{"id": f"M{index}", "kind": "working", "text": "Synthetic source-bound context."} for index in range(6)],
    }
    packet = knowledge_evidence_packet(context)
    assert len(packet["sources"]) == 5
    assert all(len(source["quotes"]) == 48 for source in packet["sources"])
    schema = knowledge_model_schema(packet)

    def enum_values(value):
        if isinstance(value, dict):
            return len(value.get("enum", [])) + sum(enum_values(child) for child in value.values())
        if isinstance(value, list):
            return sum(enum_values(child) for child in value)
        return 0

    assert enum_values(schema) == 251  # 5 * (one source ID + 48 quote IDs), plus six memories.
    assert enum_values(schema) <= 1000
    citation = schema["$defs"]["knowledge_citation"]
    for key in SECTION_LIMITS:
        assert schema["properties"][key]["items"]["properties"]["evidence"]["items"] == {
            "$ref": "#/$defs/knowledge_citation",
        }
    for source, choice in zip(packet["sources"], citation["anyOf"], strict=True):
        assert choice["properties"]["source"]["enum"] == [source["id"]]
        assert choice["properties"]["quote_id"]["enum"] == list(source["quotes"])
        assert choice["additionalProperties"] is False


def test_evidence_packet_is_bounded_exact_and_does_not_duplicate_full_sources():
    sources = [
        {
            **SOURCE, "path": f"wiki/sources/fixture-{index}.md",
            "text": "\n".join(f"Sentence {number} for fixture {index}: exact evidence with spacing, punctuation and unicode — preserved."
                              for number in range(150)),
        }
        for index in range(5)
    ]
    context = {"action": "topic", "query": "Compare", "sources": sources, "memories": [], "warnings": []}
    packet = knowledge_evidence_packet(context)
    original = {item["path"]: item["text"] for item in sources}
    assert sum(len(quote) for source in packet["sources"] for quote in source["quotes"].values()) <= MAX_QUOTE_CHARS
    for source in packet["sources"]:
        assert len(source["quotes"]) <= MAX_QUOTES_PER_SOURCE
        assert "text" not in source
        assert all(12 <= len(quote) <= 240 and quote in original[source["path"]] for quote in source["quotes"].values())
    assert len(json.dumps(packet, ensure_ascii=False)) <= 36000
    assert any("bounded" in warning for warning in packet["warnings"])


def test_only_canonical_sources_receive_evidence_ids_not_shown_reference_or_memory():
    context = {
        "action": "explain", "query": "Explain", "sources": [SOURCE],
        "memories": [{"id": "memory", "kind": "correction", "text": "MEMORY_ONLY_MARKER"}],
        "shown_message_reference": {"text": "DISPLAY_ONLY_MARKER", "is_evidence": False},
    }
    packet = knowledge_evidence_packet(context)
    evidence = json.dumps(packet["sources"])
    assert "MEMORY_ONLY_MARKER" not in evidence and "DISPLAY_ONLY_MARKER" not in evidence
