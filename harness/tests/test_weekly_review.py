from __future__ import annotations

import copy
import json
from datetime import date, timedelta
from unittest.mock import Mock

import pytest

import function_app as fa
from briefing_loop import BriefingLoop, LoopError
from briefing_plan import PlanError
from briefing_state import _encode, empty_state, prune_state, record_transition, trim_deliveries
from test_briefing_webhook import request
from weekly_review import WeeklyReview, latest_weekly, weekly_activity

TODAY = date(2026, 9, 21)


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


@pytest.fixture
def system():
    store = Store()
    sources = [
        {
            "path": f"ideas/option-{index}.md", "kind": "idea", "title": f"Option {index}",
            "text": f"Consider public learning approach {index}.", "revision": "a" * 40,
            "digest": str(index) * 64,
            "url": f"https://github.com/example/vault/blob/main/ideas/option-{index}.md",
        }
        for index in range(3)
    ]
    raw = {
        "focus": {"path": sources[0]["path"], "text": "Choose a small learning experiment."},
        "changes": [],
        "proposals": [
            {
                "kind": "research", "source_path": source["path"],
                "text": f"Compare public learning approach {index} with spaced practice.",
                "why": "Choose a bounded experiment before making a larger commitment.",
            }
            for index, source in enumerate(sources)
        ],
    }
    sent = []
    generated = []
    executions = []
    previous_reads = []
    message_id = 0

    def read_sources(previous, sections):
        previous_reads.append(copy.deepcopy(previous))
        return {
            "sources": copy.deepcopy(sources), "goals": [],
            "changes": [copy.deepcopy(item) for item in sources if previous and previous.get(item["path"]) != item["digest"]],
            "source_revisions": {item["path"]: item["revision"] for item in sources},
            "fingerprints": {item["path"]: item["digest"] for item in sources},
            "inventory_paths": [item["path"] for item in sources],
            "revision": "a" * 40, "complete": True, "warnings": [],
            "source_status": "available", "initial_baseline": not bool(previous),
        }

    def send(text, keyboard):
        nonlocal message_id
        message_id += 1
        sent.append((text, keyboard))
        return message_id

    def execute(proposal, reconcile):
        executions.append((proposal["id"], reconcile))
        return {"status": "submitted", "issue_url": "https://github.com/example/vault/issues/1"}

    def generate(packet):
        generated.append(copy.deepcopy(packet))
        return copy.deepcopy(raw)

    loop = BriefingLoop(
        store=store, sources=read_sources,
        loops=lambda: {"status": "available", "tasks": {"items": []}},
        generate=lambda packet: {"focus": raw["focus"], "changes": raw["changes"], "proposal": None},
        send=send, revision=lambda path: next(item["revision"] for item in sources if item["path"] == path),
        execute=execute, extras=lambda sections: {"freshness": {"status": "stale", "age_days": 52}},
    )
    review = WeeklyReview(loop=loop, generate=generate, send=send)
    return review, loop, store, sources, raw, sent, generated, executions, previous_reads


def test_review_delivers_one_summary_and_three_individually_bound_actions(system):
    review, loop, store, _, _, sent, generated, executions, _ = system
    assert review.run(TODAY, ["knowledge", "loops", "vault"])
    assert len(sent) == 4
    assert sent[0][1] is None
    assert len([keyboard for _, keyboard in sent if keyboard]) == 3
    assert generated[0]["review_kind"] == "weekly"
    assert executions == []
    identifiers = [loop.target(number) for number in (2, 3, 4)]
    assert len(set(identifiers)) == 3
    loop.reply(identifiers[1], "approve", TODAY)
    loop.reply(identifiers[1], "approve", TODAY)
    assert executions == [(identifiers[1], False)]
    assert store.state["proposals"][identifiers[0]]["status"] == "pending"
    assert store.state["last_delivered"] is None


def test_same_snapshot_does_not_regenerate_or_redeliver(system):
    review, _, _, _, _, sent, generated, _, _ = system
    assert review.run(TODAY, ["knowledge"])
    assert not review.run(TODAY, ["knowledge"])
    assert len(sent) == 4
    assert len(generated) == 1


def test_source_check_does_not_advance_baseline_prune_or_reconcile_decisions(system):
    review, loop, store, sources, _, sent, generated, executions, previous_reads = system
    review.run(TODAY, ["knowledge"])
    before = store.read()
    sources.pop()
    sent.clear()
    generated.clear()
    loop.execute = Mock(side_effect=AssertionError("diagnostics must not reconcile"))
    store.update = Mock(side_effect=AssertionError("diagnostics must not write state"))
    text = "\n".join(review.source_status(TODAY, ["knowledge"]))
    assert "Last delivered weekly baseline: <b>2026-09-21</b>" in text
    assert previous_reads[-1] == latest_weekly(before)["baseline"]
    assert store.read() == before
    assert not sent and not generated and not executions


def test_source_check_does_not_create_a_first_baseline(system):
    review, _, store, _, _, sent, generated, executions, _ = system
    before = store.read()
    assert "No delivered weekly baseline yet" in "\n".join(review.source_status(TODAY, ["knowledge"]))
    assert store.read() == before
    assert not sent and not generated and not executions


def test_unrenderable_action_rejects_the_batch_before_sending_the_summary(system):
    review, _, store, _, raw, sent, _, executions, _ = system
    raw["proposals"][2]["text"] = "&" * 700
    with pytest.raises(PlanError):
        review.run(TODAY, ["knowledge"])
    assert not sent
    assert not executions
    assert not store.state["deliveries"]


def test_changed_source_rejects_old_approval(system):
    review, loop, _, sources, _, _, _, executions, _ = system
    review.run(TODAY, ["knowledge"])
    identifier = loop.target(2)
    sources[0]["revision"] = "c" * 40
    assert "source changed" in loop.reply(identifier, "approve", TODAY).lower()
    assert not executions


def test_selecting_a_task_does_not_execute_or_mark_it_done(system):
    review, loop, store, sources, raw, sent, _, executions, _ = system
    task = {
        **sources[0], "kind": "task", "path": "tasks/compare.md", "title": "Compare approaches",
        "next_action": "Choose comparison criteria.", "deadline": "2026-09-22",
    }
    sources[:] = [task]
    loop.loops = lambda: {"status": "available", "tasks": {"items": [task]}}
    raw.update(
        focus={"path": task["path"], "text": "Choose comparison criteria."},
        proposals=[{"kind": "review_task", "source_path": task["path"], "text": "Choose comparison criteria.", "why": "The decision is due tomorrow."}],
    )
    review.run(TODAY, ["loops"])
    assert sent[1][1][0][0]["text"] == "Select next step"
    identifier = loop.target(2)
    receipt = loop.reply(identifier, "approve", TODAY)
    assert "remains open" in receipt
    assert store.state["proposals"][identifier]["status"] == "accepted"
    assert not executions


def test_task_creation_starts_only_after_its_own_approval_and_is_not_completion(system):
    review, loop, store, _, raw, _, _, executions, _ = system
    raw["proposals"] = [{**raw["proposals"][0], "kind": "create_task", "text": "Draft comparison criteria."}]
    review.run(TODAY, ["knowledge"])
    assert not executions
    identifier = loop.target(2)
    receipt = loop.reply(identifier, "approve", TODAY)
    assert len(executions) == 1
    assert store.state["proposals"][identifier]["status"] == "submitted"
    assert "not yet verified" in receipt


def test_dismissed_and_snoozed_actions_are_not_repeated_next_week(system):
    review, loop, _, _, _, sent, _, executions, _ = system
    review.run(TODAY, ["knowledge"])
    loop.reply(loop.target(2), "decline", TODAY)
    loop.reply(loop.target(3), "snooze 2026-10-15", TODAY)
    sent.clear()
    review.run(TODAY + timedelta(days=7), ["knowledge"])
    assert len(sent) == 2
    assert "approach 2" in sent[1][0]
    assert not executions


def test_weekly_baseline_is_independent_of_daily_fingerprints(system):
    review, _, store, sources, raw, _, _, _, previous_reads = system
    review.run(TODAY, ["knowledge"])
    sources[0]["digest"] = "f" * 64
    store.state["fingerprints"][sources[0]["path"]] = sources[0]["digest"]
    store.state["last_delivered"] = {"date": "2026-09-27", "baseline": {sources[0]["path"]: sources[0]["digest"]}}
    raw["changes"] = [{"path": sources[0]["path"], "why": "The comparison criteria changed."}]
    review.run(TODAY + timedelta(days=7), ["knowledge"])
    assert previous_reads[-1][sources[0]["path"]] == "0" * 64
    assert latest_weekly(store.read())["baseline"][sources[0]["path"]] == "f" * 64
    assert store.state["last_delivered"]["date"] == "2026-09-27"


def test_same_day_reviews_use_the_last_successful_baseline(system):
    review, _, store, sources, raw, _, _, _, _ = system
    review.run(TODAY, ["knowledge"])
    sources[0]["digest"] = "f" * 64
    sources[0]["revision"] = "c" * 40
    raw["changes"] = [{"path": sources[0]["path"], "why": "The criteria changed."}]
    review.run(TODAY, ["knowledge"])
    assert latest_weekly(store.read())["baseline"][sources[0]["path"]] == "f" * 64
    assert latest_weekly(store.read())["completed_order"] == 2


def test_abandon_cannot_downgrade_a_concurrently_completed_review(system):
    review, _, store, _, _, _, _, _, _ = system
    review.run(TODAY, ["knowledge"])
    identifier = next(iter(store.state["deliveries"]))
    assert not review._abandon(identifier, TODAY)
    assert store.state["deliveries"][identifier]["status"] == "sent"
    assert latest_weekly(store.read())


def test_abandoned_attempts_cannot_evict_the_last_successful_comparison(system):
    review, _, store, _, _, _, _, _, _ = system
    review.run(TODAY, ["knowledge"])
    baseline = latest_weekly(store.read())
    review.send = Mock(side_effect=LoopError("synthetic_unknown_send"))
    for days in (1, 2, 3):
        with pytest.raises(LoopError, match="synthetic_unknown_send"):
            review.run(TODAY + timedelta(days=days), ["knowledge"])
    retained = latest_weekly(store.read())
    for field in ("date", "snapshot", "baseline", "completed_order", "status"):
        assert retained[field] == baseline[field]


def test_checkpoint_advanced_during_generation_cannot_be_overwritten(system):
    review, _, store, sources, raw, _, _, _, _ = system
    review.run(TODAY, ["knowledge"])
    concurrent = copy.deepcopy(latest_weekly(store.read()))
    concurrent.update(date="2026-09-22", completed_order=2, snapshot="concurrent-snapshot")
    concurrent["baseline"][sources[1]["path"]] = "f" * 64

    def generate(packet):
        store.state["deliveries"]["concurrent"] = concurrent
        return copy.deepcopy(raw)

    review.generate = generate
    with pytest.raises(LoopError, match="weekly_checkpoint_changed"):
        review.run(TODAY + timedelta(days=1), ["knowledge"])
    assert latest_weekly(store.read())["completed_order"] == 2
    assert latest_weekly(store.read())["baseline"][sources[1]["path"]] == "f" * 64


def test_checkpoint_completed_during_source_loading_cannot_be_overwritten(system):
    review, loop, store, sources, raw, _, _, _, _ = system
    review.run(TODAY, ["knowledge"])
    concurrent = copy.deepcopy(latest_weekly(store.read()))
    concurrent.update(completed_order=2, snapshot="concurrent-snapshot")
    concurrent["baseline"][sources[1]["path"]] = "e" * 64
    original = loop.sources
    sources[1]["digest"] = "f" * 64
    raw["changes"] = [{"path": sources[1]["path"], "why": "Older observed criteria."}]

    def read_sources(previous, sections):
        result = original(previous, sections)
        store.state["deliveries"]["concurrent"] = concurrent
        return result

    loop.sources = read_sources
    with pytest.raises(LoopError, match="weekly_checkpoint_changed"):
        review.run(TODAY, ["knowledge"])
    assert latest_weekly(store.read())["completed_order"] == 2
    assert latest_weekly(store.read())["baseline"][sources[1]["path"]] == "e" * 64


def test_pending_task_revision_has_an_exact_edit_card_and_independent_approval(system):
    review, loop, store, sources, raw, sent, _, executions, _ = system
    task = {
        **sources[0], "kind": "task", "path": "tasks/compare.md",
        "next_action": "Choose comparison criteria.",
    }
    sources[:] = [task]
    loop.loops = lambda: {"status": "available", "tasks": {"items": [task]}}
    raw.update(
        focus={"path": task["path"], "text": "Choose comparison criteria."},
        proposals=[{"kind": "review_task", "source_path": task["path"], "text": "Choose comparison criteria.", "why": "Unblock the decision."}],
    )
    review.run(TODAY, ["loops"])
    loop.reply(loop.target(2), "change: Compare access and total time first.", TODAY)
    revised_id = loop.target(3)
    assert store.state["proposals"][revised_id]["kind"] == "update_task"
    raw["proposals"] = []
    sent.clear()
    review.run(TODAY + timedelta(days=1), ["loops"])
    assert len(sent) == 2
    assert "Compare access and total time first." in sent[1][0]
    assert sent[1][1][0][0]["text"] == "Approve edit"
    assert not executions
    loop.reply(revised_id, "approve", TODAY + timedelta(days=1))
    assert executions == [(revised_id, False)]


def test_another_send_claimed_during_generation_blocks_parallel_delivery(system):
    review, _, store, _, raw, _, _, _, _ = system

    def generate(packet):
        store.state["deliveries"]["concurrent"] = {
            "kind": "weekly", "status": "sending", "date": TODAY.isoformat(), "message_ids": [],
        }
        return copy.deepcopy(raw)

    review.generate = generate
    with pytest.raises(LoopError, match="weekly_delivery_already_claimed"):
        review.run(TODAY, ["knowledge"])
    assert set(store.state["deliveries"]) == {"concurrent"}
    assert not store.state["proposals"]


def test_uncertain_message_requires_explicit_retry_and_keeps_baseline_unadvanced(system):
    review, _, store, _, _, sent, _, executions, _ = system
    working_send = review.send

    def fail(text, keyboard):
        if keyboard:
            raise LoopError("synthetic_unknown_send")
        return working_send(text, keyboard)

    review.send = fail
    with pytest.raises(LoopError, match="synthetic_unknown_send"):
        review.run(TODAY, ["knowledge"])
    assert latest_weekly(store.read()) == {}
    assert len(sent) == 1
    review.send = working_send
    with pytest.raises(LoopError, match="weekly_delivery_uncertain"):
        review.run(TODAY, ["knowledge"])
    assert len(sent) == 1
    assert review.run(TODAY, ["knowledge"], retry_delivery=True)
    assert len(sent) == 4
    assert latest_weekly(store.read())["status"] == "sent"
    assert not executions


def test_review_tracks_observed_verification_not_legacy_completed_counts(system):
    review, loop, store, _, _, _, _, _, _ = system
    review.run(TODAY, ["knowledge"])
    identifiers = [loop.target(number) for number in (2, 3, 4)]
    store.state["proposals"][identifiers[0]]["status"] = "completed"
    record_transition(store.state["proposals"][identifiers[1]], "completed", TODAY - timedelta(days=8))
    loop.reply(identifiers[2], "approve", TODAY)
    loop.execute = lambda proposal, reconcile: {"status": "merged", "pr_url": "https://github.com/example/vault/pull/2"}
    loop.reconcile(TODAY + timedelta(days=1))
    loop.reconcile(TODAY + timedelta(days=2))
    activity = weekly_activity(store.read(), TODAY, TODAY + timedelta(days=2))
    assert len(activity) == 1
    assert activity[0]["status"] == "completed"
    assert activity[0]["date"] == "2026-09-22"
    assert "approach 2" in activity[0]["text"]


def test_verification_receipt_survives_source_removal_without_personal_text(system):
    review, loop, store, _, _, _, _, _, _ = system
    review.run(TODAY, ["knowledge"])
    identifier = loop.target(2)
    record_transition(store.state["proposals"][identifier], "completed", TODAY)
    prune_state(store.state, inventory_paths=set(), today=TODAY)
    activity = weekly_activity(store.read(), TODAY, TODAY)
    assert len(activity) == 1
    assert "approach 0" not in json.dumps(activity)
    assert activity[0]["status"] == "completed"
    _encode(store.read())


def test_delivery_retention_keeps_weekly_comparison_after_daily_sends():
    state = empty_state()
    state["deliveries"]["weekly"] = {"kind": "weekly", "status": "sent", "date": "2026-09-01", "message_ids": []}
    for day in range(2, 15):
        state["deliveries"][str(day)] = {"status": "sent", "date": f"2026-09-{day:02}", "message_ids": []}
    trim_deliveries(state)
    assert "weekly" in state["deliveries"]
    assert len(state["deliveries"]) == 9
    _encode(state)


def test_daily_delivery_does_not_resume_or_abandon_a_weekly_send(system):
    review, loop, store, _, _, _, _, _, _ = system
    review.send = Mock(side_effect=LoopError("synthetic_unknown_send"))
    with pytest.raises(LoopError):
        review.run(TODAY, ["knowledge"])
    weekly_id = next(iter(store.state["deliveries"]))
    loop.deliver(TODAY, ["knowledge"])
    assert store.state["deliveries"][weekly_id]["status"] == "sending"
    assert store.state["last_delivered"]["id"] != weekly_id


def test_transition_history_is_bounded_and_aged_out_without_faking_old_dates(system):
    review, loop, store, sources, _, _, _, _, _ = system
    review.run(TODAY, ["knowledge"])
    item = store.state["proposals"][loop.target(2)]
    assert "activity" not in item
    for number in range(40):
        record_transition(item, "accepted" if number % 2 == 0 else "declined", TODAY)
    assert len(item["activity"]) == 32
    prune_state(store.state, inventory_paths={source["path"] for source in sources}, today=TODAY + timedelta(days=36))
    assert item["activity"] == []


def test_weekly_timer_replaces_both_legacy_messages(monkeypatch):
    knowledge = Mock()
    monkeypatch.setattr(fa, "_knowledge_loop", lambda: knowledge)
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "7")
    monkeypatch.setenv("MINDME_ACTION_BRIEFING_ENABLED", "true")
    review = Mock()
    monkeypatch.setattr(fa, "_weekly_review", lambda: review)
    monkeypatch.setattr(fa, "_briefing_prefs", lambda: ["goals", "loops"])
    legacy = Mock(side_effect=AssertionError("Legacy private counts must not be read"))
    monkeypatch.setattr(fa, "_vault_state", legacy)
    monkeypatch.setattr(fa, "_telegram_send", legacy)
    fa.weekly_review_timer(None)
    review.run.assert_called_once_with(date.today(), ["goals", "loops"])
    knowledge.maintenance.assert_called_once_with(date.today())


@pytest.mark.parametrize("command,retry", [("/review", False), ("/review retry", True)])
def test_review_commands_use_the_same_owner_only_flow(monkeypatch, command, retry):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "7")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "synthetic")
    monkeypatch.setenv("MINDME_ACTION_BRIEFING_ENABLED", "true")
    review = Mock()
    monkeypatch.setattr(fa, "_weekly_review", lambda: review)
    monkeypatch.setattr(fa, "_briefing_prefs", lambda: ["knowledge"])
    assert fa.telegram_webhook(request({"message": {"chat": {"id": 8}, "text": command}})).status_code == 200
    review.run.assert_not_called()
    assert fa.telegram_webhook(request({"message": {"chat": {"id": 7}, "text": command}})).status_code == 200
    review.run.assert_called_once_with(date.today(), ["knowledge"], retry_delivery=retry)


def test_source_command_is_owner_only_and_never_starts_a_review(monkeypatch, caplog):
    caplog.set_level("INFO", logger="mindMe.harness")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "7")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "synthetic")
    monkeypatch.setenv("MINDME_ACTION_BRIEFING_ENABLED", "true")
    review = Mock()
    review.source_status.return_value = ["<b>First part</b>", "<b>Second part</b>"]
    send = Mock()
    monkeypatch.setattr(fa, "_weekly_review", lambda: review)
    monkeypatch.setattr(fa, "_briefing_prefs", lambda: ["knowledge"])
    monkeypatch.setattr(fa, "_telegram_proposal_send", send)
    monkeypatch.setattr(fa, "_telegram_send", Mock(side_effect=AssertionError("must send HTML")))
    command = {"message": {"chat": {"id": 8}, "text": "/review sources"}}
    assert fa.telegram_webhook(request(command)).status_code == 200
    review.source_status.assert_not_called()
    command["message"]["chat"]["id"] = 7
    assert fa.telegram_webhook(request(command)).status_code == 200
    review.source_status.assert_called_once_with(date.today(), ["knowledge"])
    assert [call.args for call in send.call_args_list] == [(7, part) for part in review.source_status.return_value]
    assert all(call.kwargs == {"parse_mode": "HTML"} for call in send.call_args_list)
    assert "parts=2 format=HTML" in caplog.text
    assert "First part" not in caplog.text and "Second part" not in caplog.text
    review.run.assert_not_called()


def test_source_check_partial_html_delivery_reports_failure(monkeypatch, caplog):
    caplog.set_level("INFO", logger="mindMe.harness")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "7")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "synthetic")
    monkeypatch.setenv("MINDME_ACTION_BRIEFING_ENABLED", "true")
    review = Mock()
    review.source_status.return_value = ["<b>First part</b>", "<b>Second part</b>"]
    send = Mock(side_effect=[1, fa.TelegramDeliveryError("synthetic")])
    notice = Mock()
    monkeypatch.setattr(fa, "_weekly_review", lambda: review)
    monkeypatch.setattr(fa, "_briefing_prefs", lambda: ["knowledge"])
    monkeypatch.setattr(fa, "_telegram_proposal_send", send)
    monkeypatch.setattr(fa, "_telegram_send", notice)
    response = fa.telegram_webhook(request({"message": {"chat": {"id": 7}, "text": "/review sources"}}))
    assert response.status_code == 503
    assert send.call_count == 2
    assert "could not finish" in notice.call_args.args[1]
    assert "weekly source check sent" not in caplog.text
    review.run.assert_not_called()


@pytest.mark.parametrize("configuration_failure", [False, True])
def test_failed_source_check_reports_failure_without_claiming_an_empty_vault(monkeypatch, caplog, configuration_failure):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "7")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "synthetic")
    monkeypatch.setenv("MINDME_ACTION_BRIEFING_ENABLED", "true")
    review = Mock()
    review.source_status.side_effect = fa.SourceError("PRIVATE ERROR DETAIL")
    send = Mock()
    factory = Mock(side_effect=fa.ActionError("PRIVATE ERROR DETAIL")) if configuration_failure else lambda: review
    monkeypatch.setattr(fa, "_weekly_review", factory)
    monkeypatch.setattr(fa, "_briefing_prefs", lambda: ["knowledge"])
    monkeypatch.setattr(fa, "_telegram_send", send)
    response = fa.telegram_webhook(request({"message": {"chat": {"id": 7}, "text": "/review sources"}}))
    assert response.status_code == 503
    assert "could not finish" in send.call_args.args[1]
    assert "PRIVATE ERROR DETAIL" not in caplog.text
    review.run.assert_not_called()


def test_weekly_extras_read_only_sync_metadata_not_stale_private_facts(monkeypatch):
    monkeypatch.setattr(fa, "_mirror_freshness", lambda today: {"status": "stale", "age_days": 52})
    monkeypatch.setattr(fa, "_vault_state", Mock(side_effect=AssertionError("no private body reads")))
    assert fa._weekly_extras(["vault", "journal"]) == {"warnings": [], "freshness": {"status": "stale", "age_days": 52}}
    assert fa._weekly_extras(["knowledge"]) == {"warnings": [], "freshness": {"status": "not_requested"}}


def test_weekly_model_is_bounded_and_cannot_execute(monkeypatch):
    client = Mock()
    client.with_options.return_value = client
    client.responses.create.return_value.output = []
    client.responses.create.return_value.output_text = '{"focus":null,"changes":[],"proposals":[]}'
    monkeypatch.setenv("MINDME_BRIEFING_MODEL", "existing-model")
    monkeypatch.setattr(fa, "_foundry", lambda: (None, client))
    monkeypatch.setattr(fa, "_http_client", Mock())
    fa._generate_action_plan({"review_kind": "weekly", "sources": [], "proposal_slots": 3})
    arguments = client.responses.create.call_args.kwargs
    assert arguments["store"] is False
    assert "tools" not in arguments
    assert arguments["max_output_tokens"] == 2400
    assert set(arguments["text"]["format"]["schema"]["properties"]) == {"focus", "changes", "proposals"}


def test_weekly_telegram_html_has_no_preview_and_requires_one_message(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic")
    client = Mock()
    client.post.return_value.json.return_value = {"ok": True, "result": {"message_id": 10}}
    monkeypatch.setattr(fa, "_http_client", lambda: client)
    assert fa._telegram_proposal_send(7, "<b>Weekly review</b>", parse_mode="HTML") == 10
    payload = client.post.call_args.kwargs["json"]
    assert payload["parse_mode"] == "HTML"
    assert payload["link_preview_options"] == {"is_disabled": True}
    with pytest.raises(fa.TelegramDeliveryError):
        fa._telegram_proposal_send(7, "a" * 9000, parse_mode="HTML")
