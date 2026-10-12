"""Independent-review reproductions across real adapters, receipts and ETag races."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import httpx
import pytest

import function_app as fa
from briefing_actions import ActionError, ActionGateway
from briefing_loop import BriefingLoop
from briefing_sources import SourceError
from briefing_state import prune_state
from task_service import TaskError
from task_sources import MAX_ATTENTION_READS, TaskRepository
from task_state import workspace
from test_briefing_webhook import request as telegram_request
from test_task_auth import identity
from test_briefing_sources import HEAD, REPO, TOKEN, Vault
from test_task_service import PATH, REVISION, TODAY, approve, prepare_payload, refine_payload, service, source_text
from test_task_telegram import adapter, message
from test_task_web import app, data, logged_in, request


def bind_proposal(subject, identifier, message_id=71):
    def bind(state):
        state["messages"][str(message_id)] = identifier
        state["proposals"][identifier]["message_ids"] = [message_id]
    subject.store.update(bind)


def legacy_loop(subject, repository, execute):
    return BriefingLoop(
        store=subject.store, sources=lambda *_: {}, loops=lambda: {}, generate=lambda _: {},
        send=lambda *_: 1, revision=repository.revision, execute=execute, extras=lambda _: {},
    )


@pytest.mark.parametrize("channel", ["callback", "text", "voice"])
def test_tasks_off_rejects_existing_task_approval_in_real_legacy_webhook_path(monkeypatch, channel):
    subject, repository, execute, generate, publish, _ = service()
    proposal = subject.refine(refine_payload(), TODAY)
    bind_proposal(subject, proposal["id"])
    loop = legacy_loop(subject, repository, execute)
    monkeypatch.setenv("MINDME_ACTION_BRIEFING_ENABLED", "true")
    monkeypatch.setenv("MINDME_TASKS_ENABLED", "false")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "7")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "synthetic")
    monkeypatch.setattr(fa, "_briefing_loop", lambda: loop)
    notices = Mock()
    monkeypatch.setattr(fa, "_telegram_send", notices)
    monkeypatch.setattr(fa, "_task_service", Mock(side_effect=AssertionError("disabled task service")))
    monkeypatch.setattr(fa, "_forward_to_memex", Mock(side_effect=AssertionError("not a capture")))
    if channel == "callback":
        payload = {
            "update_id": 8, "callback_query": {
                "from": {"id": 7}, "message": {**message(), "message_id": 71},
                "data": "brief1|approve|" + proposal["id"],
            },
        }
    else:
        content = {**message("approve"), "reply_to_message": {"message_id": 71}}
        if channel == "voice":
            content.pop("text")
            content["voice"] = {"file_id": "synthetic"}
            monkeypatch.setattr(fa, "_download_telegram_file", lambda _: b"synthetic")
            monkeypatch.setattr(fa, "_transcribe_voice", lambda *_: "approve")
        payload = {"update_id": 8, "message": content}
    response = fa.telegram_webhook(telegram_request(payload))
    assert response.status_code == 200
    assert subject.store.read()["proposals"][proposal["id"]]["status"] == "pending"
    assert "disabled" in notices.call_args.args[1]
    execute.assert_not_called()
    generate.assert_not_called()
    publish.assert_not_called()


def test_bare_briefing_loop_cannot_claim_task_workspace_even_without_webhook_guard():
    subject, repository, execute, _, _, _ = service()
    proposal = subject.refine(refine_payload(), TODAY)
    loop = legacy_loop(subject, repository, execute)
    assert "No action was started" in loop.reply(proposal["id"], "approve", TODAY)
    assert subject.store.read()["proposals"][proposal["id"]]["status"] == "pending"
    execute.assert_not_called()


def test_existing_task_service_honors_live_kill_switch_and_requires_durable_claim():
    subject, _, execute, generate, publish, _ = service()
    proposal = subject.prepare(prepare_payload(), TODAY)
    with pytest.raises(TaskError, match="task_execution_requires_claim"):
        subject.loop.execute(subject.store.read()["proposals"][proposal["id"]], False)
    subject.enabled = lambda: False
    for action in (
        lambda: approve(subject, proposal),
        lambda: subject.loop.reply(proposal["id"], "approve", TODAY),
        lambda: subject.reconcile(proposal["id"], TODAY),
        lambda: subject.publish(proposal["id"], TODAY),
    ):
        with pytest.raises(TaskError, match="tasks_not_configured"):
            action()
    assert subject.proactive(TODAY) is False
    execute.assert_not_called()
    generate.assert_not_called()
    publish.assert_not_called()


@pytest.mark.parametrize("enabled", [None, lambda: False])
def test_generic_gateway_refuses_task_dispatch_and_publication_without_live_authorization(enabled):
    transport = Mock(side_effect=AssertionError("no task network effect permitted"))
    gateway = ActionGateway(
        client=httpx.Client(transport=httpx.MockTransport(transport)), token="synthetic",
        repo="example/mindVault", memex_url="https://synthetic.example/api/personal_action",
        chat_id=7, task_enabled=enabled,
    )
    record = {
        "task_workspace": True, "action_id": "a" * 32, "approved_on": TODAY.isoformat(),
        "action": {"kind": "refine_task", "path": PATH, "changes": {"stage": "ready"}},
    }
    for action in (lambda: gateway(record), lambda: gateway.publish_task(record, "b" * 32)):
        with pytest.raises(ActionError, match="task_workspace_not_authorized"):
            action()
    transport.assert_not_called()


def assert_redacted(record):
    assert record["source_status"] in {"unavailable", "changed_or_removed"}
    assert not record.keys() & {"action", "approval_digest", "source_path", "source_revision", "source_url"}
    assert not record.get("result", {}).keys() & {"preparation", "source_quote", "path"}
    assert "Do not expand beyond one topic" not in json.dumps(record)
    assert "Write a heading" not in json.dumps(record)


@pytest.mark.parametrize("operation", ["decide", "reconcile", "history", "overview", "prepare-replay"])
def test_every_preparation_receipt_return_rechecks_eligibility(logged_in, operation):
    auth, cookie, csrf = logged_in
    subject, repository, execute, generate, _, _ = service()
    payload = prepare_payload()
    proposal = subject.prepare(payload, TODAY)
    assert approve(subject, proposal)["result"]["status"] == "prepared"
    repository.read_receipt_source = Mock(side_effect=SourceError("task_source_not_permitted"))
    endpoints = {
        "decide": ("api/decide", {
            "proposal_id": proposal["id"], "approval_digest": proposal["approval_digest"], "decision": "approve",
        }),
        "reconcile": ("api/reconcile", {"proposal_id": proposal["id"]}),
        "history": ("api/history", None), "overview": ("api/overview", None),
        "prepare-replay": ("api/prepare", payload),
    }
    path, body = endpoints[operation]
    result = app(auth, lambda: subject).handle(request(path, body, cookie=cookie, csrf=csrf), path)
    assert result.status_code == 200
    record = data(result)
    if operation == "history":
        record = record["items"][0]
    if operation == "overview":
        record = record["history"][0]
    assert_redacted(record)
    repository.read_receipt_source.assert_called()
    assert generate.call_count == 1
    execute.assert_not_called()


@pytest.mark.parametrize("operation", ["publish", "decide", "refine-replay", "reconcile"])
def test_every_completed_mutation_receipt_return_is_source_aware(operation):
    subject, repository, execute, _, publish, _ = service()
    publish.return_value = {
        "status": "merged", "path": PATH, "source_revision": REVISION,
        "canonical_revision": "b" * 40, "pr_url": "https://github.com/example/mindVault/pull/7",
    }
    proposal = subject.refine(refine_payload(), TODAY)
    approve(subject, proposal)
    repository.read_receipt_source = Mock(side_effect=SourceError("task_source_not_permitted"))
    result = {
        "publish": lambda: subject.publish(proposal["id"], TODAY),
        "decide": lambda: approve(subject, proposal),
        "refine-replay": lambda: subject.refine(refine_payload(), TODAY),
        "reconcile": lambda: subject.reconcile(proposal["id"], TODAY),
    }[operation]()
    assert_redacted(result)
    repository.read_receipt_source.assert_called()
    assert execute.call_count == publish.call_count == 1


def test_reconciliation_result_is_redacted_if_source_becomes_ineligible_during_check():
    subject, repository, execute, _, _, _ = service()
    proposal = subject.prepare(prepare_payload(
        kind="research", scope="Which public methods support spaced repetition?",
    ), TODAY)
    approve(subject, proposal)

    def finish(record, reconcile):
        assert reconcile
        repository.read_receipt_source = Mock(side_effect=SourceError("task_source_not_permitted"))
        return {"status": "merged", "pr_url": "https://github.com/example/mindVault/pull/7"}

    execute.side_effect = finish
    assert_redacted(subject.reconcile(proposal["id"], TODAY))


def test_telegram_card_and_completed_reply_never_return_ineligible_preparation_or_action():
    telegram, subject, repository, _, messages, _ = adapter(complete=True)
    repository.records[PATH]["stage"] = "ready"
    proposal = subject.prepare(prepare_payload(), TODAY)
    approve(subject, proposal)
    bind_proposal(subject, proposal["id"])
    repository.read_receipt_source = Mock(side_effect=SourceError("task_source_not_permitted"))
    with pytest.raises(TaskError, match="task_source_changed"):
        subject.telegram_card(proposal["id"], telegram.send)
    assert messages == []
    assert telegram.proposal(71, proposal["id"], "approve", "update:blocked", TODAY)
    assert "no longer current or available" in messages[0][0]
    assert "Do not expand" not in messages[0][0]


def test_completed_source_tombstone_overview_and_reconcile_preserve_immutable_receipt():
    subject, repository, _, generate, _, blob = service()
    proposal = subject.prepare(prepare_payload(), TODAY)
    approve(subject, proposal)
    subject.store.update(lambda state: prune_state(state, inventory_paths=set(), today=TODAY))
    before = copy.deepcopy(blob.saved()["proposals"][proposal["id"]])
    assert before["status"] == "completed" and before["invalidation_reason"] == "source_removed"
    repository.page = Mock(return_value={"items": [], "projects": [], "next_offset": None})
    for record in (
        subject.overview(0, TODAY)["history"][0],
        subject.history(0, TODAY)["items"][0],
        subject.reconcile(proposal["id"], TODAY),
    ):
        assert_redacted(record)
        assert record["status"] == "completed"
    assert blob.saved()["proposals"][proposal["id"]] == before
    assert generate.call_count == 1


def test_concurrent_source_pruning_cannot_downgrade_completed_tombstone():
    subject, repository, _, _, _, blob = service()
    proposal = subject.prepare(prepare_payload(), TODAY)
    approve(subject, proposal)
    repository.records.pop(PATH)
    blob.before_upload = lambda _: subject.store.update(
        lambda state: prune_state(state, inventory_paths=set(), today=TODAY)
    )
    subject.review_result({"proposal_id": proposal["id"], "acknowledge_unavailable": True})
    record = subject.history(0, TODAY)["items"][0]
    assert_redacted(record)
    assert record["status"] == "completed"
    assert subject.store.read()["proposals"][proposal["id"]]["status"] == "completed"
    assert subject.store.read()["proposals"][proposal["id"]]["invalidation_reason"] == "source_removed"
    assert record["acknowledged_unavailable"] is True


def test_two_web_tabs_cannot_apply_old_outcome_answer_to_next_action(logged_in):
    auth, cookie, csrf = logged_in
    subject, *_ = service(complete=False, stage="clarify")
    web = app(auth, lambda: subject)
    start = {"path": PATH, "revision": REVISION}
    first = data(web.handle(request("api/clarify", start, cookie=cookie, csrf=csrf), "api/clarify"))
    second = data(web.handle(request("api/clarify", start, cookie=cookie, csrf=csrf), "api/clarify"))
    assert first["question_token"] == second["question_token"]
    payload = {**start, "answer": "One outline exists.", "request_id": "a" * 32, "question_token": first["question_token"]}
    accepted = web.handle(request("api/clarify", payload, cookie=cookie, csrf=csrf), "api/clarify")
    assert accepted.status_code == 200 and data(accepted)["field"] == "next_action"
    assert data(accepted)["question_token"] != first["question_token"]
    stale = web.handle(request(
        "api/clarify", {**payload, "answer": "A different outcome.", "request_id": "b" * 32},
        cookie=cookie, csrf=csrf,
    ), "api/clarify")
    assert stale.status_code == 422 and data(stale)["error"] == "task_clarification_stale"
    duplicate = web.handle(request("api/clarify", payload, cookie=cookie, csrf=csrf), "api/clarify")
    assert duplicate.status_code == 200 and data(duplicate)["turns"] == 1
    saved = workspace(subject.store.read())["clarifications"][first["id"]]
    assert saved["turns"] == 1 and saved["changes"] == {"outcome": "One outline exists."}


def test_question_binding_is_checked_again_on_actual_etag_retry():
    subject, _, _, _, _, blob = service(complete=False, stage="clarify")
    question = subject.clarification(PATH, REVISION, TODAY)

    def competing_answer(_):
        subject.clarification(
            PATH, REVISION, TODAY, answer="Winning outcome.", request_id="a" * 32,
            question_token=question["question_token"],
        )

    blob.before_upload = competing_answer
    with pytest.raises(TaskError, match="task_clarification_stale"):
        subject.clarification(
            PATH, REVISION, TODAY, answer="Losing outcome.", request_id="b" * 32,
            question_token=question["question_token"],
        )
    saved = workspace(subject.store.read())["clarifications"][question["id"]]
    assert saved["turns"] == 1 and saved["changes"] == {"outcome": "Winning outcome."}
    assert saved["field"] == "next_action"


def test_telegram_precheck_race_cannot_rebind_old_answer_during_cas():
    telegram, subject, _, execute, messages, _ = adapter()
    telegram.handle(message_id=71, text="clarify", task_key="a" * 32, event="update:1", today=TODAY)
    binding = workspace(subject.store.read())["bindings"]["101"]
    original = subject.clarification
    blob = subject.store._blob

    def racing(*args, **kwargs):
        blob.before_upload = lambda _: original(
            PATH, REVISION, TODAY, answer="Winning outcome.", request_id="a" * 32,
            question_token=binding["question_token"],
        )
        return original(*args, **kwargs)

    subject.clarification = racing
    assert telegram.handle(message_id=101, text="Losing outcome.", event="update:2", today=TODAY)
    saved = workspace(subject.store.read())["clarifications"][binding["clarification_id"]]
    assert saved["turns"] == 1 and saved["changes"] == {"outcome": "Winning outcome."}
    assert "No answer was applied to another field" in messages[-1][0]
    execute.assert_not_called()


def test_missing_question_token_fails_closed_without_consuming_turn(logged_in):
    auth, cookie, csrf = logged_in
    subject, *_ = service(complete=False, stage="clarify")
    question = subject.clarification(PATH, REVISION, TODAY)
    result = app(auth, lambda: subject).handle(request(
        "api/clarify", {"path": PATH, "revision": REVISION, "answer": "An outcome.", "request_id": "a" * 32},
        cookie=cookie, csrf=csrf,
    ), "api/clarify")
    assert result.status_code == 422 and data(result)["error"] == "task_clarification_stale"
    assert workspace(subject.store.read())["clarifications"][question["id"]]["turns"] == 0


@pytest.mark.parametrize("excluded", [False, True])
def test_first_authenticated_overview_surfaces_new_due_tasks_before_twenty_old_undated_notes(monkeypatch, logged_in, excluded):
    auth, cookie, _ = logged_in
    monkeypatch.setenv("DIG_REPO", REPO)
    monkeypatch.setattr("task_web.datetime", Mock(now=Mock(return_value=datetime(
        TODAY.year, TODAY.month, TODAY.day, tzinfo=timezone.utc,
    ))))
    undated = (
        source_text(stage="backlog")
        .replace("captured: 2026-10-01", "captured: 2020-01-01")
        .replace("deadline: 2026-10-20\n", "")
        .replace("review_on: 2026-10-12\n", "")
        .replace("focus_on: 2026-10-07\n", "")
    )
    files = {f"tasks/2020-01-01-old-{index:02}.md": undated for index in range(20)}
    deadline = "tasks/2026-10-08-new-waiting-deadline.md"
    review = "tasks/2026-10-08-new-review.md"
    focus = "tasks/2026-10-08-selected-focus.md"
    old_focus = "tasks/2026-10-08-past-focus.md"
    files[deadline] = undated.replace("stage: backlog", (
        f"stage: waiting\ndeadline: {TODAY.isoformat()}\nreview_on: {(TODAY + timedelta(days=20)).isoformat()}"
        f"\nwaiting_for: A response.\nstatus: snoozed\nsnoozed_until: {(TODAY + timedelta(days=20)).isoformat()}"
    ))
    files[review] = undated.replace("type: task", f"type: task\nreview_on: {TODAY.isoformat()}")
    files[focus] = undated.replace("type: task", f"type: task\nfocus_on: {TODAY.isoformat()}")
    files[old_focus] = undated.replace("type: task", f"type: task\nfocus_on: {(TODAY - timedelta(days=1)).isoformat()}")
    if excluded:
        files["tasks/2026-10-08-flagged.md"] = files[review].replace("type: task", "type: task\nprivate: true")
        files["tasks/2026-10-08-routed.md"] = files[review].replace("type: task", "type: task\nrouting: aibsVault")
    vault = Vault(files)
    subject, _, execute, generate, publish, _ = service()
    subject.repository = TaskRepository(vault.client, token=TOKEN, repo=REPO)

    result = app(auth, lambda: subject).handle(request("api/overview", cookie=cookie), "api/overview")

    assert result.status_code == 200
    overview = data(result)
    assert [task["path"] for task in overview["items"][:3]] == [deadline, focus, review]
    assert overview["items"][0]["waiting_for"] == "A response."
    assert overview["items"][0]["review_on"] > TODAY.isoformat()
    assert old_focus not in {task["path"] for task in overview["items"]}
    assert "flagged" not in json.dumps(overview) and "routed.md" not in json.dumps(overview)
    assert overview["canonical_revision"] == HEAD
    assert all(task["canonical_revision"] == HEAD for task in overview["items"])
    assert all(item.url.params["ref"] == HEAD for item in vault.reads)
    assert len(vault.reads) == len(files) <= MAX_ATTENTION_READS
    assert overview["attention_complete"] is True
    assert overview["attention"]["attempted_count"] == len(files)
    assert overview["attention"]["assessed_count"] == len(files) - (2 if excluded else 0)
    assert overview["excluded_count"] == (2 if excluded else 0)
    assert overview["errors"] == []
    assert "does not mean nothing is due" not in " ".join(overview["warnings"])
    assert subject.repository.read(old_focus)["focus_on"] == (TODAY - timedelta(days=1)).isoformat()
    execute.assert_not_called()
    generate.assert_not_called()
    publish.assert_not_called()


def test_attention_assessment_stops_at_shared_overview_deadline_instead_of_claiming_empty(logged_in, monkeypatch):
    auth, cookie, _ = logged_in
    monkeypatch.setenv("DIG_REPO", REPO)
    clock = [0.0]
    monkeypatch.setattr("execution_budget.time.monotonic", lambda: clock[0])

    def exceed(request):
        if "/contents/" in request.url.path:
            assert request.extensions["timeout"]["read"] <= 20
            clock[0] = 121.0

    vault = Vault({
        f"tasks/2020-01-01-old-{index:03}.md": source_text() for index in range(MAX_ATTENTION_READS)
    }, override=exceed)
    subject, *_ = service()
    subject.repository = TaskRepository(vault.client, token=TOKEN, repo=REPO)
    result = app(auth, lambda: subject).handle(request("api/overview", cookie=cookie), "api/overview")
    assert result.status_code == 503
    assert data(result)["error"] == "task_service_unavailable"
    assert "items" not in data(result)
    assert len(vault.reads) == 1
