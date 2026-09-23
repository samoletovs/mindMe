from __future__ import annotations

import copy
from datetime import date
from types import SimpleNamespace

import pytest
from azure.core.exceptions import ResourceNotFoundError

from briefing_loop import BriefingLoop, LoopError
from briefing_plan import PlanError, validate_plan
from briefing_state import BriefingStore, empty_state
from execution_budget import BudgetExceeded, execution_budget
from telegram_format import TelegramHTMLReply

TODAY = date(2026, 9, 13)
REVISION = "a" * 40
SOURCE = {
    "path": "ideas/2026-01-01-small-experiment.md", "revision": REVISION,
    "digest": "b" * 64, "title": "A small experiment", "text": "Try a short learning exercise.",
    "kind": "idea", "url": "https://github.com/example/vault/blob/main/ideas/test.md",
}


class MemoryStore:
    def __init__(self):
        self.state = empty_state()
        self.fail = False

    def read(self):
        return copy.deepcopy(self.state)

    def update(self, mutate):
        next_state = self.read()
        result = mutate(next_state)
        if self.fail:
            raise LoopError("simulated_save_failure")
        self.state = next_state
        return copy.deepcopy(result)


@pytest.fixture
def system():
    store = MemoryStore()
    sent = []
    executed = []
    inputs = []
    source = copy.deepcopy(SOURCE)
    raw = {
        "focus": {"path": source["path"], "text": "Consider one small experiment."},
        "changes": [{"path": source["path"], "why": "This can test the learning goal cheaply."}],
        "proposal": {
            "kind": "research", "source_path": source["path"],
            "text": "Which public learning methods help spaced practice?",
            "why": "Choose a method before creating a larger study plan.",
        },
    }

    def sources(previous, sections):
        return {
            "sources": [source], "changes": [source], "goals": [],
            "fingerprints": {source["path"]: source["digest"]},
            "source_revisions": {source["path"]: source["revision"]},
            "inventory_paths": [source["path"]], "revision": REVISION,
            "complete": True, "warnings": [],
        }

    def generate(context):
        inputs.append(copy.deepcopy(context))
        return copy.deepcopy(raw)

    def send(text, keyboard):
        sent.append((text, keyboard))
        return len(sent)

    def execute(proposal, reconcile):
        executed.append((proposal, reconcile))
        return {"status": "submitted", "issue_url": "https://github.com/example/vault/issues/1"}

    loop = BriefingLoop(
        store=store, sources=sources, loops=lambda: {"status": "available", "tasks": {"items": []}},
        generate=generate, send=send, revision=lambda path: source["revision"],
        execute=execute, extras=lambda sections: {},
    )
    return loop, store, sent, executed, inputs, raw, source


def test_two_briefings_preserve_a_correction_without_creating_work(system):
    loop, store, sent, executed, inputs, raw, _ = system
    loop.deliver(TODAY, ["knowledge", "loops"])
    proposal_id = loop.target(2)
    assert proposal_id
    assert "saved" in loop.reply(proposal_id, "correction: I already use spaced practice", TODAY).lower()
    loop.deliver(date(2026, 9, 14), ["knowledge", "loops"])
    assert inputs[-1]["corrections"][0]["text"] == "I already use spaced practice"
    assert len([keyboard for _, keyboard in sent if keyboard]) == 1
    assert executed == []
    assert store.state["last_delivered"]["date"] == "2026-09-14"


def test_explanation_is_formatted_and_does_not_change_proposal_or_start_work(system):
    loop, store, sent, executed, _, raw, _ = system
    loop.deliver(TODAY, ["knowledge", "loops"])
    before = store.read()
    count = len(sent)

    reply = loop.reply(loop.target(2), "why?", TODAY)

    assert isinstance(reply, TelegramHTMLReply)
    assert raw["proposal"]["why"] in "".join(reply.parts)
    assert "<b>Why now</b>" in "".join(reply.parts)
    assert store.read() == before
    assert len(sent) == count
    assert executed == []


def test_invalid_model_text_is_regenerated_before_any_delivery(system, caplog):
    loop, store, sent, executed, _, raw, _ = system
    packets = []

    def generate(packet):
        packets.append(copy.deepcopy(packet))
        assert not sent
        assert not store.state["deliveries"]
        assert not store.state["proposals"]
        result = copy.deepcopy(raw)
        if len(packets) == 1:
            result["focus"]["text"] = "x" * 501
        return result

    loop.generate = generate
    loop.deliver(TODAY, ["knowledge"])

    assert len(packets) == 2
    assert packets[1] == {**packets[0], "validation_feedback": "invalid_text"}
    assert len(sent) == 2
    assert store.state["last_delivered"]["date"] == TODAY.isoformat()
    assert len(store.state["proposals"]) == 1
    assert not executed
    assert "code=invalid_text" in caplog.text
    assert "x" * 501 not in caplog.text


def test_incomplete_model_json_gets_one_pre_delivery_retry(system):
    loop, store, sent, _, _, raw, _ = system
    calls = []

    def generate(packet):
        calls.append(packet)
        if len(calls) == 1:
            raise PlanError("invalid_model_plan")
        return copy.deepcopy(raw)

    loop.generate = generate
    loop.deliver(TODAY, ["knowledge"])

    assert len(calls) == 2
    assert len(sent) == 2
    assert store.state["last_delivered"]["date"] == TODAY.isoformat()


def test_two_invalid_plans_fail_without_delivery_or_source_checkpoint(system):
    loop, store, sent, executed, inputs, raw, _ = system
    raw["focus"]["text"] = "x" * 501

    with pytest.raises(PlanError, match="invalid_text"):
        loop.deliver(TODAY, ["knowledge"])

    assert len(inputs) == 2
    assert not sent
    assert not executed
    assert not store.state["deliveries"]
    assert not store.state["proposals"]
    assert store.state["last_delivered"] is None
    assert not store.state["fingerprints"]


def test_unsafe_plan_is_not_retried_or_logged(system, caplog):
    loop, store, sent, _, inputs, raw, _ = system
    raw["focus"]["text"] = "token=synthetic-private-value"

    with pytest.raises(PlanError, match="unsafe_text"):
        loop.deliver(TODAY, ["knowledge"])

    assert len(inputs) == 1
    assert not sent
    assert not store.state["deliveries"]
    assert raw["focus"]["text"] not in caplog.text


def test_unknown_plan_error_content_is_never_logged_or_retried(system, caplog):
    loop, _, sent, _, _, _, _ = system
    calls = []

    def generate(packet):
        calls.append(packet)
        raise PlanError("private_source_content")

    loop.generate = generate
    with pytest.raises(PlanError):
        loop.deliver(TODAY, ["knowledge"])

    assert len(calls) == 1
    assert not sent
    assert "private_source_content" not in caplog.text
    assert "code=unclassified_plan_error" in caplog.text


def test_validation_retry_cannot_start_after_the_invocation_deadline(system, monkeypatch):
    loop, store, sent, _, _, _, _ = system
    clock = [0.0]
    calls = []
    monkeypatch.setattr("execution_budget.time.monotonic", lambda: clock[0])

    def generate(packet):
        calls.append(packet)
        clock[0] = 76.0
        raise PlanError("invalid_model_plan")

    loop.generate = generate
    with pytest.raises(BudgetExceeded), execution_budget(75):
        loop.deliver(TODAY, ["knowledge"])

    assert len(calls) == 1
    assert not sent
    assert not store.state["deliveries"]


@pytest.mark.parametrize("code", ["briefing_model_refused", "briefing_model_not_configured"])
def test_terminal_model_errors_do_not_trigger_regeneration(system, code):
    loop, store, sent, _, _, _, _ = system
    calls = []

    def generate(packet):
        calls.append(packet)
        raise PlanError(code)

    loop.generate = generate
    with pytest.raises(PlanError, match=code):
        loop.deliver(TODAY, ["knowledge"])

    assert len(calls) == 1
    assert not sent
    assert not store.state["deliveries"]


@pytest.mark.parametrize(
    ("section", "field", "limit"),
    [("focus", "text", 500), ("changes", "why", 500), ("proposal", "text", 700), ("proposal", "why", 500)],
)
def test_plan_text_limits_are_enforced_without_truncation(system, section, field, limit):
    _, _, _, _, _, raw, source = system
    context = {"sources": [source], "changes": [source]}
    target = raw[section][0] if section == "changes" else raw[section]
    target[field] = "x" * limit
    validate_plan(raw, context, TODAY)

    target[field] += "x"
    with pytest.raises(PlanError, match="invalid_text"):
        validate_plan(raw, context, TODAY)
    assert len(target[field]) == limit + 1


def test_approving_a_proposal_twice_starts_one_operation(system):
    loop, store, _, executed, _, _, _ = system
    loop.deliver(TODAY, ["knowledge"])
    identifier = loop.target(2)
    assert "submitted" in loop.reply(identifier, "approve", TODAY)
    loop.reply(identifier, "yes", TODAY)
    assert len(executed) == 1
    assert store.state["proposals"][identifier]["status"] == "submitted"


def test_declined_idea_does_not_return_unchanged(system):
    loop, _, sent, executed, _, _, _ = system
    loop.deliver(TODAY, ["knowledge"])
    loop.reply(loop.target(2), "decline", TODAY)
    assert "Declined and saved" in loop.reply(loop.target(2), "decline", TODAY)
    loop.deliver(date(2026, 9, 14), ["knowledge"])
    assert len([keyboard for _, keyboard in sent if keyboard]) == 1
    assert not executed


def test_snooze_does_not_guess_a_date_or_change_a_task(system):
    loop, store, _, executed, _, _, _ = system
    loop.deliver(TODAY, ["knowledge"])
    identifier = loop.target(2)
    loop.reply(identifier, "later", TODAY)
    assert store.state["proposals"][identifier]["status"] == "pending"
    loop.reply(identifier, "snooze 2026-10-01", TODAY)
    assert store.state["proposals"][identifier]["review_on"] == "2026-10-01"
    assert executed == []


def test_changed_source_invalidates_old_approval(system):
    loop, store, _, executed, _, _, source = system
    loop.deliver(TODAY, ["knowledge"])
    identifier = loop.target(2)
    source["revision"] = "c" * 40
    assert "source changed" in loop.reply(identifier, "approve", TODAY).lower()
    assert store.state["proposals"][identifier]["status"] == "invalidated"
    assert not executed


def test_failed_second_message_does_not_advance_any_source_checkpoint(system):
    loop, store, _, _, _, _, _ = system
    count = 0

    def send(text, keyboard):
        nonlocal count
        count += 1
        if count == 2:
            raise LoopError("send_failed")
        return count

    loop.send = send
    with pytest.raises(LoopError):
        loop.deliver(TODAY, ["knowledge"])
    assert store.state["fingerprints"] == {}
    assert store.state["last_delivered"] is None
    assert next(iter(store.state["deliveries"].values()))["status"] == "sending"
    loop.deliver(TODAY, ["knowledge"])
    assert loop.target(3) is not None
    assert store.state["last_delivered"]["date"] == TODAY.isoformat()


def test_reissued_proposal_cannot_be_approved_from_an_old_message(system):
    loop, _, _, executed, _, _, source = system
    loop.deliver(TODAY, ["knowledge"])
    old_id = loop.target(2)
    loop.reply(old_id, "snooze 2026-09-14", TODAY)
    source["revision"] = "c" * 40
    loop.deliver(date(2026, 9, 14), ["knowledge"])
    assert loop.target(2) is None
    new_id = loop.target(4)
    assert new_id and new_id != old_id
    loop.reply(old_id, "approve", date(2026, 9, 14))
    assert not executed


def test_revision_cannot_hide_an_action_claimed_concurrently(system):
    loop, store, _, _, _, _, _ = system
    loop.deliver(TODAY, ["knowledge"])
    identifier = loop.target(2)

    def concurrent_approval(path):
        store.state["proposals"][identifier]["status"] = "submitted"
        return REVISION

    loop.revision = concurrent_approval
    with pytest.raises(LoopError, match="already_claimed"):
        loop.reply(identifier, "change: Compare another public method", TODAY)
    assert store.state["proposals"][identifier]["status"] == "submitted"


def test_corrections_remain_idempotent_after_action_submission(system):
    loop, store, _, _, _, _, _ = system
    loop.deliver(TODAY, ["knowledge"])
    identifier = loop.target(2)
    loop.reply(identifier, "approve", TODAY)
    loop.reply(identifier, "correction: Prefer brief experiments", TODAY)
    loop.reply(identifier, "correction: Prefer brief experiments", TODAY)
    loop.reply(identifier, "correction: Prefer guided exercises", TODAY)
    assert store.state["proposals"][identifier]["status"] == "submitted"
    assert len(store.state["memories"]) == 2
    active = [item for item in store.state["memories"].values() if item["active"]]
    assert len(active) == 1 and active[0]["text"] == "Prefer guided exercises"


def test_current_personal_signals_are_available_in_details_not_routine_summary(system):
    loop, _, sent, _, inputs, _, _ = system
    loop.extras = lambda sections: {"signals": ["Inbox: 3 items.", "Yesterday: 2 unfinished checkboxes."]}
    loop.deliver(TODAY, ["knowledge", "vault", "journal"])
    assert inputs[0]["personal_signals"] == ["Inbox: 3 items.", "Yesterday: 2 unfinished checkboxes."]
    assert "Inbox: 3 items." not in sent[0][0]
    assert "Inbox: 3 items." in loop.details(TODAY, ["vault", "journal"])


def test_failed_decision_persistence_cannot_start_work(system):
    loop, store, _, executed, _, _, _ = system
    loop.deliver(TODAY, ["knowledge"])
    identifier = loop.target(2)
    store.fail = True
    with pytest.raises(LoopError):
        loop.reply(identifier, "approve", TODAY)
    assert not executed


def test_only_actually_presented_sources_advance_checkpoint(system):
    loop, store, _, _, _, raw, _ = system
    raw["changes"] = []
    raw["proposal"] = None
    raw["focus"] = None
    loop.deliver(TODAY, ["knowledge"])
    assert store.state["fingerprints"] == {}
    assert store.state["last_delivered"]["baseline"] == {SOURCE["path"]: SOURCE["digest"]}


def test_initial_baseline_is_not_mistaken_for_user_reviewed_material(system):
    loop, store, _, _, _, raw, _ = system
    loader = loop.sources
    previous_inputs = []

    def observe(previous, sections):
        previous_inputs.append(copy.deepcopy(previous))
        context = loader(previous, sections)
        context["changes"] = []
        context["initial_baseline"] = not previous
        return context

    loop.sources = observe
    raw.update(focus=None, changes=[], proposal=None)
    loop.deliver(TODAY, ["knowledge"])
    loop.deliver(date(2026, 9, 14), ["knowledge"])
    assert previous_inputs == [{}, {SOURCE["path"]: SOURCE["digest"]}]
    assert store.state["fingerprints"] == {}


def test_near_deadline_survives_short_briefing_and_model_preference(system):
    loop, _, sent, _, _, raw, _ = system
    tasks = [
        {
            "path": "tasks/2026-09-13-deadline.md", "title": "Near deadline",
            "next_action": "Do the urgent step.", "deadline": "2026-09-14",
            "revision": REVISION,
        },
        *[
            {"path": f"tasks/2026-01-01-old-{i}.md", "title": f"Old item {i}", "revision": REVISION}
            for i in range(20)
        ],
    ]
    loop.loops = lambda: {"status": "available", "tasks": {"items": tasks}}
    raw["proposal"] = None
    loop.deliver(TODAY, ["loops", "knowledge"])
    assert "<b>Morning focus" in sent[0][0]
    assert "<b>One focus</b>\nNear deadline" in sent[0][0]
    assert "Due tomorrow (14 Sep)" in sent[0][0]
    assert sent[0][0].count("Near deadline") == 1


def test_nonexistent_model_source_cannot_authorize_a_proposal():
    context = {"sources": [SOURCE], "changes": [SOURCE]}
    with pytest.raises(PlanError, match="unbacked_proposal"):
        validate_plan({
            "focus": None, "changes": [],
            "proposal": {"kind": "research", "source_path": "../private.md", "text": "Read private data", "why": "Because"},
        }, context, TODAY)


def test_all_due_tasks_remain_accessible_in_details_with_reserved_change_evidence(system):
    loop, _, sent, _, inputs, raw, _ = system
    tasks = [
        {
            "path": f"tasks/task-{n}.md", "title": f"Due task {n}",
            "review_on": TODAY.isoformat(), "revision": REVISION,
        }
        for n in range(24)
    ]
    loop.loops = lambda: {"status": "available", "tasks": {"items": tasks}}
    raw["proposal"] = None

    loop.deliver(TODAY, ["loops", "knowledge"])

    assert SOURCE["path"] in {item["path"] for item in inputs[-1]["sources"]}
    assert len(inputs[-1]["sources"]) == 24
    assert "21 more date-relevant tasks: /briefing details." in sent[0][0]
    assert "partial view" in sent[0][0]
    before = len(sent)
    details = loop.details(TODAY, ["loops", "knowledge"])
    for task in tasks:
        assert task["title"] in details
    assert len(sent) == before
    assert "not included in the model evidence" in details


def test_deadline_displaces_old_waiting_review_without_repeating_focus(system):
    loop, _, sent, _, _, raw, _ = system
    tasks = [
        {"path": "tasks/wait.md", "title": "Waiting for support", "review_on": "2026-08-01",
         "waiting_for": "A reply", "revision": REVISION},
        {"path": "tasks/submit.md", "title": "Submit application", "deadline": "2026-09-14",
         "next_action": "Upload the final draft.", "revision": REVISION},
    ]
    loop.loops = lambda: {"status": "available", "tasks": {"items": tasks}}
    raw["proposal"] = None
    loop.deliver(TODAY, ["loops", "knowledge"])
    summary = sent[0][0]
    assert "<b>One focus</b>\nSubmit application" in summary
    assert summary.count("Submit application") == 1
    assert "Waiting for support" in summary
    assert "Review 43d overdue" in summary


def test_details_do_not_generate_send_execute_or_mutate_private_receipts(system):
    loop, store, sent, executed, inputs, _, _ = system
    before = store.read()
    text = loop.details(TODAY, ["loops", "knowledge"])
    assert SOURCE["title"] in text
    assert "No work is started" in text
    assert store.read() == before
    assert not sent and not executed and not inputs


def test_action_card_names_the_effect_and_approval_executes_only_once(system):
    loop, store, sent, executed, _, _, _ = system
    loop.deliver(TODAY, ["knowledge"])
    card, keyboard = sent[1]
    assert "<b>What approval means</b>" in card
    assert keyboard[0][0]["text"] == "Start research"
    assert not executed
    identifier = loop.target(2)
    loop.reply(identifier, "approve", TODAY)
    loop.reply(identifier, "approve", TODAY)
    assert len(executed) == 1
    assert store.read()["proposals"][identifier]["status"] == "submitted"


def test_unshown_changes_are_not_marked_presented(system):
    loop, store, _, _, _, raw, _ = system
    raw["focus"] = None
    raw["proposal"] = None
    raw["changes"][0]["why"] = "A reason too long to show in the concise overview. " * 8
    loop.deliver(TODAY, ["knowledge"])
    assert not store.read()["fingerprints"]


def test_legacy_plain_delivery_resumes_through_plain_sender(system):
    loop, store, sent, _, _, _, _ = system
    loop.deliver(TODAY, ["knowledge"])
    delivery = next(iter(store.state["deliveries"].values()))
    delivery.update(status="sending", summary_sent=False, text="Legacy <text> & values")
    delivery.pop("format")
    delivery["proposal_id"] = None
    delivery["fingerprints"] = {}
    loop.send_html = lambda *_: pytest.fail("Legacy text must not be parsed as HTML")
    loop._complete_delivery(next(iter(store.state["deliveries"])))
    assert sent[-1][0] == "Legacy <text> & values"


def test_model_cannot_launder_an_existing_omitted_change_into_a_delivery(system):
    loop, store, sent, _, inputs, raw, _ = system
    original_loader = loop.sources
    extra = [
        {**SOURCE, "path": f"notes/extra-{n}.md", "kind": "note"}
        for n in range(30)
    ]

    def sources(previous, sections):
        context = original_loader(previous, sections)
        context["sources"].extend(extra)
        context["changes"].extend(extra)
        return context

    loop.sources = sources
    raw.update(
        focus=None, proposal=None,
        changes=[{"path": extra[-1]["path"], "why": "This source was not supplied."}],
    )

    with pytest.raises(PlanError, match="unbacked_change"):
        loop.deliver(TODAY, ["knowledge"])

    assert extra[-1]["path"] not in {item["path"] for item in inputs[-1]["sources"]}
    assert sent == []
    assert store.state["deliveries"] == {}


def test_memory_deletion_is_idempotent_and_does_not_delete_source(system):
    loop, store, _, _, _, _, _ = system
    loop.deliver(TODAY, ["knowledge"])
    loop.reply(loop.target(2), "correction: Prefer a short experiment", TODAY)
    identifier = next(iter(store.state["memories"]))
    assert "short experiment" in loop.memory_command("")
    loop.memory_command(f"forget {identifier}")
    loop.memory_command(f"forget {identifier}")
    assert store.state["memories"] == {}
    assert len(store.state["proposals"]) == 1


def test_full_loop_round_trips_through_the_real_private_state_schema(system):
    loop, _, _, executed, inputs, _, _ = system

    class Blob:
        data = None
        version = 0

        def download_blob(self, **kwargs):
            if self.data is None:
                raise ResourceNotFoundError("synthetic missing blob")
            data = self.data
            return SimpleNamespace(
                properties=SimpleNamespace(etag=str(self.version), size=len(data)),
                readall=lambda: data,
            )

        def upload_blob(self, payload, **kwargs):
            if self.data is not None:
                assert kwargs["etag"] == str(self.version)
            self.data = payload
            self.version += 1

    blob = Blob()
    container = SimpleNamespace(get_blob_client=lambda name: blob)
    loop.store = BriefingStore(container)
    loop.deliver(TODAY, ["knowledge"])
    identifier = loop.target(2)
    loop.reply(identifier, "approve", TODAY)
    loop.reply(identifier, "correction: Prefer short learning experiments", TODAY)
    loop.reply(identifier, "correction: Prefer hands-on exercises", TODAY)
    loop.store = BriefingStore(container)
    loop.deliver(date(2026, 9, 14), ["knowledge"])
    assert inputs[-1]["corrections"][0]["text"] == "Prefer hands-on exercises"
    assert len([item for item in executed if not item[1]]) == 1
    assert loop.store.read()["last_delivered"]["date"] == "2026-09-14"


def test_source_removed_unresolved_receipt_is_never_reexecuted(system):
    loop, store, _, executed, _, _, _ = system
    store.state["proposals"]["c" * 24] = {
        "id": "c" * 24, "status": "submitted", "action_id": "a" * 32,
        "invalidation_reason": "source_removed",
    }
    loop.reconcile()
    assert executed == []


def test_expiry_date_itself_cannot_authorize_an_action(system):
    loop, store, _, executed, _, _, _ = system
    loop.deliver(TODAY, ["knowledge"])
    identifier = loop.target(2)
    expiry = date.fromisoformat(store.state["proposals"][identifier]["expires_on"])
    assert "expired" in loop.reply(identifier, "approve", expiry)
    assert executed == []


def test_verified_result_is_reported_once_in_a_subsequent_briefing(system):
    loop, _, sent, _, _, _, _ = system
    loop.deliver(TODAY, ["knowledge"])
    loop.reply(loop.target(2), "approve", TODAY)
    loop.execute = lambda proposal, reconcile: {
        "status": "merged", "pr_url": "https://github.com/example/vault/pull/3",
    }
    loop.deliver(date(2026, 9, 14), ["knowledge"])
    loop.deliver(date(2026, 9, 15), ["knowledge"])
    result_messages = [text for text, _ in sent if "<b>Results checked</b>" in text]
    assert len(result_messages) == 1
    assert "https://github.com/example/vault/pull/3" in result_messages[0]
