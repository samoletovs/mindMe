"""Retained-review regressions: owner calendar and durable review acknowledgments."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

import function_app as fa
from briefing_state import StateError, prune_state
from task_service import TaskError, task_timezone
from task_sources import TaskRepository, attention_reasons
from test_briefing_sources import HEAD, REPO, TOKEN, Vault
from test_task_auth import identity
from test_task_service import PATH, REVISION, TODAY, approve, prepare_payload, service, source_text
from test_task_web import app, data, logged_in, request

UTC_MOMENT = datetime(2026, 10, 8, 22, 30, tzinfo=timezone.utc)
OWNER_ZONE = "Etc/GMT-3"  # Synthetic non-geographic UTC+3; not a deployment default.
OWNER_DAY = (TODAY + timedelta(days=1)).isoformat()


def undated_source(stage="ready"):
    return (
        source_text(stage=stage)
        .replace("deadline: 2026-10-20\n", "")
        .replace("review_on: 2026-10-12\n", "")
        .replace("focus_on: 2026-10-07\n", "")
    )


def calendar_service(monkeypatch, files):
    monkeypatch.setenv("DIG_REPO", REPO)
    monkeypatch.setattr("task_web.datetime", Mock(now=Mock(return_value=UTC_MOMENT)))
    subject, _, execute, generate, publish, _ = service()
    subject.attention_timezone = task_timezone({"MINDME_TASKS_TIMEZONE": OWNER_ZONE})
    subject.clock = lambda: UTC_MOMENT
    vault = Vault(files)
    subject.repository = TaskRepository(vault.client, token=TOKEN, repo=REPO)
    return subject, vault, execute, generate, publish


def test_owner_midnight_focus_reaches_first_actual_overview_with_server_eligibility(monkeypatch, logged_in):
    auth, cookie, _ = logged_in
    files = {f"tasks/2020-01-01-old-{index:02}.md": undated_source("backlog") for index in range(12)}
    selected = "tasks/2026-10-09-selected.md"
    files[selected] = undated_source().replace("type: task", "type: task\nfocus_on: " + OWNER_DAY)
    subject, vault, execute, generate, publish = calendar_service(monkeypatch, files)
    result = app(auth, lambda: subject).handle(request("api/overview", cookie=cookie), "api/overview")
    assert result.status_code == 200
    overview = data(result)
    assert overview["date"] == TODAY.isoformat()
    assert overview["attention"]["calendar_date"] == OWNER_DAY
    assert overview["attention"]["timezone"] == OWNER_ZONE
    assert overview["attention"]["deadline_horizon_days"] == 7
    assert overview["items"][0]["path"] == selected
    assert overview["items"][0]["attention_eligible"] is True
    assert overview["items"][0]["attention_reasons"] == ["focus_today"]
    assert all(item["attention_eligible"] is False for item in overview["items"][1:])
    assert all(item.url.params["ref"] == HEAD for item in vault.reads)
    execute.assert_not_called()
    generate.assert_not_called()
    publish.assert_not_called()


def test_twelve_tomorrow_deadlines_remain_visible_through_the_actual_ui_contract(monkeypatch, logged_in):
    auth, cookie, _ = logged_in
    tomorrow = (TODAY + timedelta(days=2)).isoformat()
    files = {
        f"tasks/2026-10-08-deadline-{index:02}.md":
        undated_source().replace("type: task", "type: task\ndeadline: " + tomorrow)
        for index in range(12)
    }
    files["tasks/2026-10-09-review.md"] = undated_source().replace(
        "type: task", "type: task\nreview_on: " + OWNER_DAY,
    )
    subject, *_ = calendar_service(monkeypatch, files)
    result = app(auth, lambda: subject).handle(request("api/overview", cookie=cookie), "api/overview")
    overview = data(result)
    assert result.status_code == 200
    assert len(overview["items"]) == 12
    assert all(item["attention_eligible"] is True for item in overview["items"])
    assert all(item["attention_reasons"] == ["deadline_soon"] for item in overview["items"])
    assert overview["next_offset"] == 12
    second = subject.overview(12, TODAY)
    assert second["items"][0]["attention_reasons"] == ["review_due"]
    assert second["items"][0]["attention_eligible"] is True


@pytest.mark.parametrize(("days", "reason"), [
    (-1, "deadline_due"), (0, "deadline_due"), (1, "deadline_soon"),
    (7, "deadline_soon"), (8, None),
])
def test_one_deadline_horizon_drives_ranking_eligibility_and_reason(days, reason):
    task = {
        "deadline": (TODAY + timedelta(days=days)).isoformat(),
        "stage": "waiting", "review_on": (TODAY + timedelta(days=20)).isoformat(),
        "snoozed_until": (TODAY + timedelta(days=20)).isoformat(),
    }
    assert attention_reasons(task, TODAY) == ([reason] if reason else [])


def test_future_review_suppresses_discretionary_focus_but_never_hard_deadline():
    task = {"focus_on": TODAY.isoformat(), "review_on": (TODAY + timedelta(days=1)).isoformat()}
    assert attention_reasons(task, TODAY) == []
    assert attention_reasons({**task, "deadline": TODAY.isoformat()}, TODAY) == ["deadline_due"]
    assert attention_reasons({"stage": "clarify", "definition": {"complete": False}}, TODAY) == []
    assert attention_reasons({"stage": "waiting", "waiting_for": "A response."}, TODAY) == []
    assert attention_reasons({"focus_on": (TODAY - timedelta(days=1)).isoformat()}, TODAY) == []


@pytest.mark.parametrize("value", ["", "Missing/Zone", "../UTC", "UTC+03:00"])
def test_enabled_tasks_refuse_missing_or_invalid_owner_calendar_before_service_io(monkeypatch, logged_in, value):
    auth, cookie, _ = logged_in
    monkeypatch.setenv("MINDME_ACTION_BRIEFING_ENABLED", "true")
    monkeypatch.setenv("MINDME_TASKS_ENABLED", "true")
    monkeypatch.setenv("MINDME_TASKS_TIMEZONE", value)
    io = Mock(side_effect=AssertionError("configuration must fail before service I/O"))
    monkeypatch.setattr(fa, "_http_client", io)
    result = app(auth, fa._task_service).handle(request("api/overview", cookie=cookie), "api/overview")
    assert result.status_code == 503
    assert data(result)["error"] == ("task_timezone_not_configured" if not value else "task_timezone_invalid")
    assert "items" not in data(result)
    io.assert_not_called()


def test_explicit_utc_is_allowed_but_never_silently_assumed():
    assert task_timezone({"MINDME_TASKS_TIMEZONE": "UTC"}).key == "UTC"
    with pytest.raises(TaskError, match="task_timezone_not_configured"):
        task_timezone({})


def test_owner_verification_date_and_utc_budget_approval_expiry_are_separate(monkeypatch, logged_in):
    auth, cookie, csrf = logged_in
    monkeypatch.setattr("task_web.datetime", Mock(now=Mock(return_value=UTC_MOMENT)))
    subject, _, _, _, _, _ = service()
    subject.attention_timezone = ZoneInfo(OWNER_ZONE)
    subject.clock = lambda: UTC_MOMENT
    web = app(auth, lambda: subject)
    prepared = web.handle(request("api/prepare", prepare_payload(), cookie=cookie, csrf=csrf), "api/prepare")
    proposal = data(prepared)
    assert proposal["created_on"] == TODAY.isoformat()
    assert proposal["expires_on"] == (TODAY + timedelta(days=14)).isoformat()
    approved = web.handle(request("api/decide", {
        "proposal_id": proposal["id"], "approval_digest": proposal["approval_digest"], "decision": "approve",
    }, cookie=cookie, csrf=csrf), "api/decide")
    assert approved.status_code == 200
    assert subject.store.read()["task_workspace"]["budget"]["day"] == TODAY.isoformat()
    assert subject.store.read()["task_workspace"]["budget"]["used"] == 1

    closure = {
        "request_id": "c" * 32, "path": PATH, "revision": REVISION, "checked_done_when": True,
        "completion": {
            "result": "An outline exists.", "evidence": "Owner checked the agreed topic.",
            "verification": "owner", "verified_on": OWNER_DAY,
        },
    }
    closed = web.handle(request("api/close", closure, cookie=cookie, csrf=csrf), "api/close")
    assert closed.status_code == 200
    assert data(closed)["action"]["completion"]["verified_on"] == OWNER_DAY
    assert data(closed)["created_on"] == TODAY.isoformat()
    closure["completion"]["verified_on"] = (TODAY + timedelta(days=2)).isoformat()
    assert web.handle(request("api/close", closure, cookie=cookie, csrf=csrf), "api/close").status_code == 422


def next_selected_task(repository):
    other = "tasks/2026-10-08-another-selected-task.md"
    repository.records[other] = {
        **copy.deepcopy(repository.records[PATH]), "path": other, "revision": "e" * 40,
    }
    return {**prepare_payload(path=other, revision="e" * 40), "request_id": "b" * 32}


@pytest.mark.parametrize("kind", ["prepare_task", "research"])
def test_reviewed_result_does_not_reconsume_capacity_after_source_closure(kind):
    subject, repository, execute, _, _, _ = service(review_capacity=1)
    if kind == "research":
        execute.return_value = {"status": "merged", "pr_url": "https://github.com/example/mindVault/pull/7"}
    scope = "Which public methods support spaced repetition?" if kind == "research" else prepare_payload()["scope"]
    proposal = subject.prepare(prepare_payload(kind=kind, scope=scope), TODAY)
    approve(subject, proposal)
    subject.review_result({"proposal_id": proposal["id"]})
    next_payload = next_selected_task(repository)
    repository.records["tasks/done/synthetic.md"] = repository.records.pop(PATH)
    subject.store.update(lambda state: prune_state(
        state, inventory_paths=set(repository.records), today=TODAY,
    ))
    receipt = subject.store.read()["proposals"][proposal["id"]]
    assert receipt["status"] == "completed"
    assert receipt["invalidation_reason"] == "source_removed"
    assert receipt["reviewed"] is True
    following = subject.prepare(next_payload, TODAY)
    assert approve(subject, following)["status"] == "completed"


def test_unreviewed_unavailable_result_needs_explicit_ack_to_release_capacity(logged_in):
    auth, cookie, csrf = logged_in
    subject, repository, execute, generate, _, _ = service(review_capacity=1)
    proposal = subject.prepare(prepare_payload(), TODAY)
    approve(subject, proposal)
    next_payload = next_selected_task(repository)
    repository.records.pop(PATH)
    subject.store.update(lambda state: prune_state(state, inventory_paths=set(repository.records), today=TODAY))
    before = copy.deepcopy(subject.store.read()["proposals"][proposal["id"]])
    assert before.get("reviewed") is not True
    following = subject.prepare(next_payload, TODAY)
    with pytest.raises(TaskError, match="task_review_capacity"):
        approve(subject, following)
    web = app(auth, lambda: subject)
    normal = web.handle(request(
        "api/review-result", {"proposal_id": proposal["id"]}, cookie=cookie, csrf=csrf,
    ), "api/review-result")
    assert normal.status_code == 422
    assert data(normal)["error"] == "task_result_acknowledgment_required"
    ack = web.handle(request(
        "api/review-result", {"proposal_id": proposal["id"], "acknowledge_unavailable": True},
        cookie=cookie, csrf=csrf,
    ), "api/review-result")
    assert data(ack) == {"reviewed": True, "acknowledged_unavailable": True, "task_closed": False}
    after = subject.store.read()["proposals"][proposal["id"]]
    assert {key: value for key, value in after.items() if key not in {"reviewed", "acknowledged_unavailable"}} == before
    assert "action" not in after and "source_path" not in after
    assert subject.history(0, TODAY)["items"][-1]["reviewed"] is True
    assert approve(subject, following)["status"] == "completed"
    assert generate.call_count == 2
    execute.assert_not_called()


@pytest.mark.parametrize("value", ["true", 1, None, []])
def test_reviewed_tombstone_flags_require_actual_booleans(value):
    subject, *_ = service()
    proposal = subject.prepare(prepare_payload(), TODAY)
    approve(subject, proposal)
    subject.review_result({"proposal_id": proposal["id"]})
    subject.store.update(lambda state: prune_state(state, inventory_paths=set(), today=TODAY))
    with pytest.raises(StateError, match="invalid_task_review_receipt"):
        subject.store.update(lambda state: state["proposals"][proposal["id"]].update(reviewed=value))


def test_reviewed_tombstone_cannot_be_downgraded_or_rewritten_as_unreviewed():
    subject, *_ = service()
    proposal = subject.prepare(prepare_payload(), TODAY)
    approve(subject, proposal)
    subject.review_result({"proposal_id": proposal["id"]})
    subject.store.update(lambda state: prune_state(state, inventory_paths=set(), today=TODAY))
    with pytest.raises(StateError, match="source_receipt_immutable"):
        subject.store.update(lambda state: state["proposals"][proposal["id"]].update(reviewed=False))
