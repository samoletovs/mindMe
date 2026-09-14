from __future__ import annotations

import copy
import hashlib
from datetime import date, datetime, timezone

import pytest

from briefing_state import _encode
from evolve_loop import DailyEvolve
from test_briefing_loop import MemoryStore
from vault_evolve import EvolveError, complete_review, evidence_packet, review_schema, telegram_parts

TODAY = date(2026, 9, 14)
TEXT = "Both the model and the operating procedure changed."
SOURCE = {
    "kind": "wiki", "path": "wiki/insights/pilot.md", "revision": "a" * 40,
    "sha256": hashlib.sha256(TEXT.encode()).hexdigest(),
    "title": "Pilot evidence", "evidence_text": TEXT,
    "url": "https://github.com/example/mindVault/blob/" + "a" * 40 + "/wiki/insights/pilot.md",
}


def context():
    return {
        "date": TODAY.isoformat(), "sources": [copy.deepcopy(SOURCE)], "goals": [],
        "source_status": "available", "warnings": [],
        "processed_revisions": {SOURCE["path"]: SOURCE["revision"]},
        "scan_cursor": SOURCE["path"],
    }


def generated():
    return {"findings": [{
        "kind": "evidence", "basis": "observed",
        "statement": "The pilot changed two variables, so it does not isolate either effect.",
        "relationship": "none", "evidence": [{"source": "S1", "quote_id": "Q1"}],
        "action": "curate", "next_step": "Propose a caveat on the comparison before attributing the gain.",
    }]}


def test_receipt_resolves_literal_evidence_and_never_approves_proposed_edits():
    packet = evidence_packet(context(), [])
    review = complete_review(generated(), packet)
    assert review["sources"][0]["sha256"] == hashlib.sha256(TEXT.encode()).hexdigest()
    assert review["findings"][0]["evidence"] == [{"source": "S1", "quote": TEXT}]
    assert review["scope"]["personal_knowledge"] == "not_assessed"
    assert review["proposals"][0]["status"] == "proposed"
    assert set(review) == {"version", "as_of", "scope", "sources", "findings", "proposals"}


@pytest.mark.parametrize("field,value", [
    ("kind", "execute"), ("basis", "proven"), ("action", "create_task"),
    ("relationship", "supports"), ("evidence", []),
    ("evidence", [{"source": "S9", "quote_id": "Q1"}]),
    ("evidence", [{"source": "S1", "quote_id": "Q999"}]),
    ("evidence", [{"source": "S1", "quote": TEXT + "!"}]),
])
def test_invalid_or_unsupplied_evidence_is_rejected(field, value):
    raw = generated()
    raw["findings"][0][field] = value
    with pytest.raises(EvolveError):
        complete_review(raw, evidence_packet(context(), []))


def test_connection_needs_two_distinct_sources_not_two_quotes_from_one_page():
    raw = generated()
    raw["findings"][0].update(kind="connection", relationship="supports")
    with pytest.raises(EvolveError, match="ungrounded_connection"):
        complete_review(raw, evidence_packet(context(), []))


def test_model_schema_cannot_choose_an_unseen_source_or_rewrite_a_quote():
    packet = evidence_packet(context(), [])
    choices = review_schema(packet)["properties"]["findings"]["items"]["anyOf"][0]["properties"]["evidence"]["items"]["anyOf"]
    assert len(choices) == 1
    assert choices[0]["properties"]["source"]["enum"] == ["S1"]
    assert choices[0]["properties"]["quote_id"]["enum"] == ["Q1"]
    assert "quote" not in choices[0]["properties"]


def test_model_schema_prevents_the_real_non_connection_relationship_failure():
    variants = review_schema(evidence_packet(context(), []))["properties"]["findings"]["items"]["anyOf"]
    for variant in variants:
        props = variant["properties"]
        if props["kind"]["enum"] == ["connection"]:
            assert "none" not in props["relationship"]["enum"]
            assert props["evidence"]["minItems"] == 2
        else:
            assert "connection" not in props["kind"]["enum"]
            assert props["relationship"]["enum"] == ["none"]


def test_goal_private_mirror_and_generated_review_are_not_quote_sources():
    data = context()
    data["sources"] = [{**SOURCE, "kind": kind} for kind in ("goal", "private", "review")]
    assert evidence_packet(data, [])["sources"] == []


def test_paraphrasing_does_not_repeat_an_unchanged_delivered_finding():
    packet = evidence_packet(context(), [])
    review = complete_review(generated(), packet)
    next_packet = evidence_packet(context(), [{"review": review, "feedback": {"F1": {"text": "Already familiar"}}}])
    raw = generated()
    raw["findings"][0]["statement"] = "The experiment varies more than one factor."
    assert complete_review(raw, next_packet)["findings"] == []


def test_changed_source_version_can_produce_a_new_finding():
    review = complete_review(generated(), evidence_packet(context(), []))
    changed = context()
    changed["sources"][0]["sha256"] = "b" * 64
    packet = evidence_packet(changed, [{"review": review}])
    assert complete_review(generated(), packet)["findings"]


def test_telegram_keeps_evidence_labels_next_steps_and_truthful_publication_status():
    packet = evidence_packet(context(), [])
    review = complete_review(generated(), packet)
    parts = telegram_parts(review, {"status": "submitted", "pr_url": "https://github.com/example/mindVault/pull/2"}, packet)
    assert "not yet canonical" in parts[0]["text"]
    assert "observed" in parts[1]["text"]
    assert review["proposals"][0]["next_step"] in parts[1]["text"]
    assert all(len(part["text"]) <= 4000 for part in parts)


@pytest.fixture
def system():
    store = MemoryStore()
    sends = []
    publishes = []
    generations = []
    data = context()

    def generate(packet):
        generations.append(packet)
        return generated()

    def publish(identifier, review):
        publishes.append((identifier, copy.deepcopy(review)))
        return {"action_id": identifier, "status": "submitted", "pr_url": "https://github.com/example/mindVault/pull/2"}

    def send(text, keyboard):
        sends.append((text, keyboard))
        return len(sends) + 100

    loop = DailyEvolve(
        store=store, sources=lambda metadata: copy.deepcopy(data),
        generate=generate, publish=publish, send=send,
        revision=lambda path: data["sources"][0]["revision"],
        clock=lambda: datetime(2026, 9, 14, 7, 30, tzinfo=timezone.utc),
    )
    return loop, store, sends, publishes, generations, data


def test_daily_invocations_generate_publish_and_deliver_only_once(system):
    loop, store, sends, publishes, generations, _ = system
    loop.run(TODAY)
    loop.run(TODAY)
    assert len(generations) == len(publishes) == 1
    assert len(sends) == 2
    assert store.read()["last_delivered"]["date"] == TODAY.isoformat()
    _encode(store.read())


def test_feedback_is_bound_persistent_and_used_next_day_without_executing_work(system):
    loop, store, sends, publishes, generations, _ = system
    loop.run(TODAY)
    assert loop.target(102) == ("2026-09-14", "F1")
    loop.feedback("2026-09-14", "F1", "Already use a controlled comparison", TODAY)
    loop.run(date(2026, 9, 15))
    assert generations[-1]["previous_findings"][0]["feedback"]["text"] == "Already use a controlled comparison"
    assert len(publishes) == 1
    assert store.read()["deliveries"]["2026-09-15"]["review"]["findings"] == []
    assert "no new vault PR" in sends[-1][0]


def test_approval_like_feedback_does_not_execute_or_save_a_decision(system):
    loop, store, _, publishes, _, _ = system
    loop.run(TODAY)
    assert "has not authorized work" in loop.feedback("2026-09-14", "F1", "approve", TODAY)
    assert store.read()["deliveries"]["2026-09-14"]["feedback"] == {}
    assert len(publishes) == 1


def test_forgotten_feedback_is_not_left_in_cached_model_packets(system):
    loop, store, _, _, _, _ = system
    loop.run(TODAY)
    loop.feedback("2026-09-14", "F1", "Unique scoped correction", TODAY)
    loop.run(date(2026, 9, 15))
    loop.feedback_command("forget 2026-09-14")
    assert b"Unique scoped correction" not in _encode(store.read())


def test_changed_source_removes_derived_feedback_and_never_accepts_stale_buttons(system):
    loop, store, _, _, _, data = system
    loop.run(TODAY)
    loop.feedback("2026-09-14", "F1", "Unique scoped correction", TODAY)
    data["sources"][0]["revision"] = None
    assert "No feedback" in loop.feedback("2026-09-14", "F1", "useful", TODAY)
    assert b"Unique scoped correction" not in _encode(store.read())


def test_model_failure_keeps_checkpoint_unadvanced_and_attempts_bounded(system):
    loop, store, _, _, _, _ = system
    def fail(packet):
        raise EvolveError("synthetic_failure")
    loop.generate = fail
    for _ in range(2):
        with pytest.raises(EvolveError, match="synthetic_failure"):
            loop.run(TODAY)
    with pytest.raises(EvolveError, match="daily_model_attempt_limit"):
        loop.run(TODAY)
    assert store.read()["last_delivered"] is None


def test_uncertain_publication_reuses_prepared_review_and_same_action_id(system):
    loop, store, _, publishes, generations, _ = system
    original = loop.publish
    calls = []
    def publish(identifier, review):
        calls.append((identifier, copy.deepcopy(review)))
        if len(calls) == 1:
            raise EvolveError("unconfirmed_write")
        return original(identifier, review)
    loop.publish = publish
    with pytest.raises(EvolveError, match="unconfirmed_write"):
        loop.run(TODAY)
    assert store.read()["last_delivered"] is None
    loop.run(TODAY)
    assert calls[0] == calls[1]
    assert len(generations) == len(publishes) == 1


def test_partial_telegram_delivery_needs_explicit_retry_and_resumes_only_remaining_part(system):
    loop, store, sends, publishes, generations, _ = system
    original = loop.send
    def send(text, keyboard):
        if keyboard:
            raise EvolveError("unconfirmed_delivery")
        return original(text, keyboard)
    loop.send = send
    with pytest.raises(EvolveError, match="unconfirmed_delivery"):
        loop.run(TODAY)
    with pytest.raises(EvolveError, match="use_explicit_retry"):
        loop.run(TODAY)
    assert store.read()["last_delivered"] is None
    assert len(sends) == 1
    loop.send = original
    loop.run(TODAY, retry_delivery=True)
    assert len(sends) == 2
    assert len(publishes) == len(generations) == 1
    assert store.read()["last_delivered"]["date"] == TODAY.isoformat()


def test_live_lease_prevents_concurrent_generation_or_delivery(system):
    loop, store, sends, publishes, generations, _ = system
    store.state["deliveries"]["2026-09-14"] = {
        "date": "2026-09-14", "status": "sending", "message_ids": [],
        "lease_until": loop.clock().timestamp() + 600,
    }
    with pytest.raises(EvolveError, match="daily_review_busy"):
        loop.run(TODAY)
    assert not sends and not publishes and not generations
