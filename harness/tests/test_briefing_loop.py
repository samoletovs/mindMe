from __future__ import annotations

import copy
from datetime import date
from types import SimpleNamespace

import pytest
from azure.core.exceptions import ResourceNotFoundError

from briefing_loop import BriefingLoop, LoopError
from briefing_plan import PlanError, validate_plan
from briefing_state import BriefingStore, empty_state

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


def test_enabled_personal_signals_reach_input_and_output(system):
    loop, _, sent, _, inputs, _, _ = system
    loop.extras = lambda sections: {"signals": ["Inbox: 3 items.", "Yesterday: 2 unfinished checkboxes."]}
    loop.deliver(TODAY, ["knowledge", "vault", "journal"])
    assert inputs[0]["personal_signals"] == ["Inbox: 3 items.", "Yesterday: 2 unfinished checkboxes."]
    assert "Inbox: 3 items." in sent[0][0]


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
    assert sent[0][0].startswith("Morning focus - 2026-09-13\n\nNear deadline")
    assert "deadline 2026-09-14" in sent[0][0]


def test_nonexistent_model_source_cannot_authorize_a_proposal():
    context = {"sources": [SOURCE], "changes": [SOURCE]}
    with pytest.raises(PlanError, match="unbacked_proposal"):
        validate_plan({
            "focus": None, "changes": [],
            "proposal": {"kind": "research", "source_path": "../private.md", "text": "Read private data", "why": "Because"},
        }, context, TODAY)


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
    result_messages = [text for text, _ in sent if "Verified results from approved work" in text]
    assert len(result_messages) == 1
    assert "https://github.com/example/vault/pull/3" in result_messages[0]
