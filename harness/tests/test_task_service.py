from __future__ import annotations

import copy
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from unittest.mock import Mock

import pytest

from briefing_loop import BriefingLoop
from briefing_plan import fingerprint
from briefing_state import StateError, empty_state, prune_state
from task_service import TaskError, TaskService, validate_changes
from task_sources import parse_task
from task_state import workspace
from test_briefing_state import FakeBlob, proposal as legacy_proposal, store_for

TODAY = date(2026, 10, 8)
PATH = "tasks/2026-10-08-synthetic-outline.md"
REVISION = "a" * 40
PROJECT = "2026-synthetic-project"
ORIGINAL = "Draft one synthetic outline.\nDo not expand beyond one topic."
SOURCE = (
    "# Draft a synthetic outline\n\n"
    "```yaml\n"
    "```\n"
)


def source_text(stage="ready", *, complete=True):
    return (
        "# Draft a synthetic outline\n\n<details>\n<summary>Task</summary>\n\n```yaml\n"
        "type: task\ncaptured: 2026-10-01\n"
        f"stage: {stage}\nproject: {PROJECT}\n"
        + ("area: learning\nexecution: assisted\n" if complete else "")
        + "deadline: 2026-10-20\nreview_on: 2026-10-12\nfocus_on: 2026-10-07\n"
        "```\n\n</details>\n\n"
        + ("**Outcome:** One outline exists.\n\n**Next action:** Write a heading.\nKeep one topic.\n\n"
           "**Done when:** Read the saved outline and check the agreed topic.\n\n" if complete else "")
        + "## Capture\n\n> Draft one synthetic outline.\n> Do not expand beyond one topic.\n"
    )


class Repository:
    def __init__(self, complete=True, stage="ready"):
        text = source_text(stage, complete=complete)
        self.records = {
            PATH: {
                **parse_task(text, PATH), "text": text, "revision": REVISION,
                "canonical_revision": "b" * 40, "digest": fingerprint(text),
                "url": "https://github.com/example/mindVault/blob/" + "b" * 40 + "/" + PATH,
            },
            "tasks/README.md": {
                "path": "tasks/README.md", "revision": "c" * 40,
                "digest": "d" * 64, "text": "Synthetic task contract.",
                "url": "https://github.com/example/mindVault/blob/main/tasks/README.md",
            },
            f"projects/{PROJECT}/README.md": {
                "path": f"projects/{PROJECT}/README.md", "revision": "e" * 40,
                "digest": "f" * 64, "text": "Synthetic project.",
                "url": f"https://github.com/example/mindVault/blob/main/projects/{PROJECT}/README.md",
            },
        }

    def read(self, path):
        return copy.deepcopy(self.records.get(path))

    def revision(self, path):
        return (self.records.get(path) or {}).get("revision")

    def read_receipt_source(self, path):
        return self.read(path)

    def page(self, offset=0, *, today=None):
        return {
            "items": [copy.deepcopy(self.records[PATH])], "projects": [{"id": PROJECT, "revision": "e" * 40}],
            "canonical_revision": "b" * 40, "next_offset": None,
        }


def service(*, complete=True, stage="ready", result="submitted", **limits):
    blob = FakeBlob()
    repository = Repository(complete=complete, stage=stage)
    execute = Mock(return_value={"status": result, "path": PATH, "pr_url": "https://github.com/example/mindVault/pull/7"})
    publish = Mock(return_value={
        "status": "submitted", "path": PATH, "pr_url": "https://github.com/example/mindVault/pull/7",
    })
    generate = Mock(return_value={
        "summary": "A proposed outline is ready to review.",
        "steps": ["Write one heading.", "Check the scope."],
        "uncertainties": ["The source does not specify a length."],
        "owner_next_action": "Review this draft before doing the task.",
        "source_quote": "Do not expand beyond one topic.",
    })
    loop = BriefingLoop(
        store=store_for(blob), sources=lambda *_: {}, loops=lambda: {}, generate=lambda _: {},
        send=lambda *_: 1, revision=repository.revision, execute=execute, extras=lambda _: {},
    )
    subject = TaskService(
        loop=loop, repository=repository, generate=generate, publish=publish, **limits,
        attention_timezone=ZoneInfo("UTC"),
        clock=lambda: datetime(TODAY.year, TODAY.month, TODAY.day, tzinfo=timezone.utc),
    )
    return subject, repository, execute, generate, publish, blob


def refine_payload(**changes):
    return {
        "request_id": "1" * 32, "path": PATH, "revision": REVISION,
        "changes": changes or {"next_action": "Write two synthetic headings."},
    }


def prepare_payload(**changes):
    return {
        "request_id": "2" * 32, "path": PATH, "revision": REVISION,
        "scope": "Prepare a short outline draft without doing the task.",
        "kind": "prepare_task", **changes,
    }


def approve(subject, proposal, *, day=TODAY):
    return subject.decide({
        "proposal_id": proposal["id"], "approval_digest": proposal["approval_digest"], "decision": "approve",
    }, day)


def test_refinement_has_no_effect_until_exact_approval_and_duplicate_does_not_rewrite():
    subject, _, execute, _, publish, _ = service()
    proposal = subject.refine(refine_payload(), TODAY)
    assert proposal["status"] == "pending"
    execute.assert_not_called()
    first = approve(subject, proposal)
    assert first["status"] == "submitted"
    assert execute.call_count == 1
    approve(subject, proposal)
    assert execute.call_count == 1
    assert publish.call_count == 2
    assert publish.call_args_list[0].args[1] == publish.call_args_list[1].args[1]


def test_stale_source_and_forged_digest_cannot_approve():
    subject, repository, execute, _, _, _ = service()
    proposal = subject.refine(refine_payload(), TODAY)
    with pytest.raises(TaskError, match="task_approval_invalid"):
        approve(subject, {**proposal, "approval_digest": "forged"})
    repository.records[PATH]["revision"] = "f" * 40
    assert approve(subject, proposal)["status"] == "invalidated"
    execute.assert_not_called()


def test_changed_action_after_preview_is_rejected_in_atomic_claim():
    subject, _, execute, _, _, blob = service()
    proposal = subject.refine(refine_payload(), TODAY)
    state = blob.saved()
    state["proposals"][proposal["id"]]["action"]["changes"]["next_action"] = "Unexpected different action."
    blob.replace(state)
    with pytest.raises(TaskError, match="task_approval_changed"):
        approve(subject, proposal)
    execute.assert_not_called()


def test_same_request_id_different_action_is_conflict():
    subject, *_ = service()
    subject.refine(refine_payload(), TODAY)
    with pytest.raises(TaskError, match="task_request_conflict"):
        subject.refine(refine_payload(next_action="Different request."), TODAY)


def test_creation_retains_exact_multiline_request_and_agent_metadata_does_not_run():
    subject, _, execute, generate, _, _ = service()
    proposal = subject.capture({
        "request_id": "3" * 32, "text": ORIGINAL,
        "definition": {"title": "Draft one synthetic outline", "stage": "clarify", "execution": "agent"},
    }, TODAY)
    assert proposal["action"]["text"] == ORIGINAL
    assert proposal["action"]["definition"]["stage"] == "clarify"
    execute.assert_not_called()
    generate.assert_not_called()


@pytest.mark.parametrize("status", ["unknown", "in_progress"])
def test_unknown_action_outcome_is_never_reexecuted(status):
    subject, _, execute, _, publish, _ = service(result=status)
    proposal = subject.refine(refine_payload(), TODAY)
    first = approve(subject, proposal)
    assert first["status"] in {"uncertain", "executing"}
    approve(subject, proposal)
    assert execute.call_count == 1
    publish.assert_not_called()


def test_publication_unknown_remains_uncertain_with_same_publication_id():
    subject, _, execute, _, publish, _ = service()
    publish.return_value = {"status": "in_progress"}
    proposal = subject.refine(refine_payload(), TODAY)
    assert approve(subject, proposal)["status"] == "uncertain"
    subject.reconcile(proposal["id"], TODAY)
    assert execute.call_count == 1
    assert publish.call_args_list[0].args[1] == publish.call_args_list[1].args[1]


def test_rejected_preparation_retains_failed_receipt_and_cannot_replay():
    subject, _, _, generate, _, _ = service()
    generate.side_effect = TaskError("task_preparation_request_rejected")
    proposal = subject.prepare(prepare_payload(), TODAY)
    result = approve(subject, proposal)
    assert result["status"] == "failed"
    assert result["result"] == {"status": "failed", "error": "task_preparation_request_rejected"}
    assert approve(subject, proposal)["result"] == result["result"]
    assert generate.call_count == 1
    assert subject.store.read()["task_workspace"]["budget"]["used"] == 1
    subject.reconcile(proposal["id"], TODAY)
    assert generate.call_count == 1


def test_concurrent_failed_preparation_cannot_be_claimed_again():
    subject, _, _, generate, _, _ = service()
    generate.side_effect = TaskError("task_preparation_request_rejected")
    proposal = subject.prepare(prepare_payload(), TODAY)
    original_revision = subject.loop.revision

    def fail_concurrently(path):
        subject.loop.revision = original_revision
        assert approve(subject, proposal)["status"] == "failed"
        return original_revision(path)

    subject.loop.revision = fail_concurrently
    with pytest.raises(TaskError, match="task_preparation_already_attempted"):
        approve(subject, proposal)
    assert generate.call_count == 1
    assert subject.store.read()["task_workspace"]["budget"]["used"] == 1


@pytest.mark.parametrize("field,count", [("steps", 6), ("uncertainties", 4)])
def test_preparation_output_caps_remain_enforced_locally(field, count):
    subject, _, _, generate, _, _ = service()
    generate.return_value[field] = ["A synthetic item."] * count
    proposal = subject.prepare(prepare_payload(), TODAY)
    assert approve(subject, proposal)["status"] == "failed"
    assert generate.call_count == 1


def test_preparation_validation_records_safe_reason_without_output(caplog):
    subject, _, _, generate, _, _ = service()
    generate.return_value["source_quote"] = "synthetic-private-unmatched-quote"
    proposal = subject.prepare(prepare_payload(), TODAY)
    result = approve(subject, proposal)
    assert result["result"] == {"status": "failed", "error": "task_preparation_evidence_invalid"}
    assert "phase=validation code=task_preparation_evidence_invalid" in caplog.text
    assert "synthetic-private" not in caplog.text


@pytest.mark.parametrize(("field", "value", "code"), [
    ("summary", "x" * 701, "task_preparation_invalid"),
    ("summary", "", "task_preparation_invalid"),
    ("owner_next_action", None, "task_preparation_invalid"),
    ("steps", [""], "task_preparation_invalid"),
    ("uncertainties", ["x" * 401], "task_preparation_invalid"),
    ("source_quote", "", "task_preparation_invalid"),
    ("summary", "token: synthetic-private-marker", "task_text_not_permitted"),
], ids=["overlong-summary", "empty-summary", "null-owner-action", "empty-step",
        "overlong-uncertainty", "empty-quote", "secret-shaped-summary"])
def test_invalid_model_text_retains_failed_receipt_through_actual_sdk(monkeypatch, caplog, field, value, code):
    import json

    import httpx
    from openai import OpenAI

    import function_app as fa

    subject, _, execute, generate, publish, _ = service()
    output = {**generate.return_value, field: value}
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={
            "id": "synthetic", "object": "chat.completion", "created": 1,
            "model": "existing-small-model",
            "choices": [{
                "index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(output), "refusal": None},
            }],
        })

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        with OpenAI(
            api_key="synthetic-not-a-credential", base_url="https://synthetic.invalid/openai/v1/",
            http_client=http,
        ) as client:
            monkeypatch.setenv("MINDME_BRIEFING_MODEL", "existing-small-model")
            monkeypatch.setattr(fa, "_http_client", lambda: http)
            monkeypatch.setattr(fa, "_foundry", lambda: (None, client))
            subject.generate = fa._generate_task_preparation
            proposal = subject.prepare(prepare_payload(), TODAY)
            result = approve(subject, proposal)
            assert result["status"] == "failed"
            assert result["result"] == {"status": "failed", "error": code}
            assert "preparation" not in result["result"]
            assert approve(subject, proposal)["result"] == result["result"]

    assert len(requests) == 1
    assert subject.store.read()["task_workspace"]["budget"] == {
        "month": TODAY.isoformat()[:7], "day": TODAY.isoformat(), "used": 1, "daily_used": 1,
    }
    assert f"phase=validation code={code}" in caplog.text
    assert "synthetic-private-marker" not in caplog.text
    assert "x" * 401 not in caplog.text
    execute.assert_not_called()
    publish.assert_not_called()


def test_unrelated_plan_error_is_not_swallowed_as_a_known_model_text_rejection(monkeypatch):
    from briefing_plan import PlanError

    subject, _, execute, generate, publish, _ = service()
    proposal = subject.prepare(prepare_payload(), TODAY)
    monkeypatch.setattr("task_service._text", Mock(side_effect=PlanError("unbacked_focus")))
    with pytest.raises(PlanError, match="unbacked_focus"):
        approve(subject, proposal)
    record = subject.store.read()["proposals"][proposal["id"]]
    assert record["status"] == "executing"
    assert "result" not in record
    assert subject.store.read()["task_workspace"]["budget"]["used"] == 1
    assert approve(subject, proposal)["status"] == "executing"
    generate.assert_called_once()
    execute.assert_not_called()
    publish.assert_not_called()


def test_preparation_unrecognized_failure_never_exposes_exception_content(caplog):
    subject, _, _, generate, _, _ = service()
    generate.side_effect = TaskError("synthetic-private-failure-detail")
    proposal = subject.prepare(prepare_payload(), TODAY)
    result = approve(subject, proposal)
    assert result["result"] == {"status": "failed", "error": "task_preparation_rejected"}
    assert "phase=generation code=task_preparation_rejected" in caplog.text
    assert "synthetic-private" not in caplog.text


def test_private_preparation_has_real_model_path_only_after_approval_and_exact_evidence():
    subject, _, execute, generate, _, _ = service()
    proposal = subject.prepare(prepare_payload(), TODAY)
    generate.assert_not_called()
    result = approve(subject, proposal)
    assert result["status"] == "completed"
    assert result["result"]["status"] == "prepared"
    assert result["result"]["verification"] == "draft_not_verified"
    assert result["result"]["preparation"]["source_quote"] == "Do not expand beyond one topic."
    assert generate.call_count == 1
    execute.assert_not_called()
    assert subject.store.read()["task_workspace"]["budget"]["used"] == 1
    approve(subject, proposal)
    assert generate.call_count == 1
    assert subject.review_result({"proposal_id": proposal["id"]})["task_closed"] is False


def test_budget_is_reserved_before_model_failure_and_unknown_does_not_retry():
    subject, _, _, generate, _, _ = service(daily_limit=1)
    generate.side_effect = TaskError("task_preparation_invalid")
    proposal = subject.prepare(prepare_payload(), TODAY)
    assert approve(subject, proposal)["status"] == "failed"
    assert subject.store.read()["task_workspace"]["budget"]["used"] == 1
    assert approve(subject, proposal)["status"] == "failed"
    assert generate.call_count == 1
    other = subject.prepare(prepare_payload(scope="A different bounded draft.", request_id="4" * 32), TODAY)
    with pytest.raises(TaskError, match="task_budget_exhausted"):
        approve(subject, other)
    assert generate.call_count == 1


def test_review_capacity_stops_optional_preparation_until_explicit_owner_review():
    subject, _, _, generate, _, _ = service(review_capacity=1)
    first = subject.prepare(prepare_payload(), TODAY)
    approve(subject, first)
    second = subject.prepare(prepare_payload(scope="Another bounded draft.", request_id="4" * 32), TODAY)
    with pytest.raises(TaskError, match="task_review_capacity"):
        approve(subject, second)
    assert generate.call_count == 1
    subject.review_result({"proposal_id": first["id"]})
    assert approve(subject, second)["status"] == "completed"
    assert generate.call_count == 2


def test_invented_model_quote_fails_without_success_shaped_fallback():
    subject, _, _, generate, _, _ = service()
    generate.return_value["source_quote"] = "Evidence that is not present."
    proposal = subject.prepare(prepare_payload(), TODAY)
    assert approve(subject, proposal)["status"] == "failed"
    assert subject.store.read()["proposals"][proposal["id"]]["status"] == "failed"


def test_research_uses_existing_executor_but_never_includes_private_task_source():
    subject, _, execute, _, _, _ = service()
    proposal = subject.prepare(prepare_payload(
        kind="research", scope="What public evidence compares spaced repetition methods?",
    ), TODAY)
    approve(subject, proposal)
    action = execute.call_args.args[0]["action"]
    assert action["limits"]["sources"] == 5
    assert "source" not in action and ORIGINAL not in str(action)
    with pytest.raises(Exception, match="research"):
        subject.prepare(prepare_payload(kind="research", scope="Research my private repository and employer."), TODAY)


def test_proactive_requires_explicit_active_project_and_standing_scope_and_never_repeats():
    subject, _, execute, generate, _, _ = service()
    assert subject.proactive(TODAY) is False
    with pytest.raises(TaskError, match="task_standing_scope_invalid"):
        subject.standing({"enabled": True, "sources": {PATH: REVISION}})
    subject.select_projects({"projects": [PROJECT]})
    subject.standing({"enabled": True, "sources": {PATH: REVISION}})
    assert subject.proactive(TODAY) is True
    assert subject.proactive(TODAY + timedelta(days=1)) is False
    assert generate.call_count == 1
    execute.assert_not_called()


def test_changed_project_or_task_revokes_standing_work():
    subject, repository, _, generate, _, _ = service()
    subject.select_projects({"projects": [PROJECT]})
    subject.standing({"enabled": True, "sources": {PATH: REVISION}})
    repository.records[f"projects/{PROJECT}/README.md"]["revision"] = "1" * 40
    assert subject.proactive(TODAY) is False
    generate.assert_not_called()


def test_clarification_is_exact_source_bound_three_answers_and_never_executes():
    subject, _, execute, generate, _, _ = service(complete=False, stage="clarify")
    draft = subject.clarification(PATH, REVISION, TODAY)
    assert draft["field"] == "outcome"
    for index, answer in enumerate(["One outline exists.", "Write one heading.", "Read the saved outline."]):
        token = draft["question_token"]
        draft = subject.clarification(
            PATH, REVISION, TODAY, answer=answer, request_id=str(index + 1) * 32, question_token=token,
        )
        again = subject.clarification(
            PATH, REVISION, TODAY, answer=answer, request_id=str(index + 1) * 32, question_token=token,
        )
        assert again["turns"] == draft["turns"] == index + 1
    assert draft["question"] is None
    with pytest.raises(TaskError, match="task_clarification_limit"):
        subject.clarification(PATH, REVISION, TODAY, answer="learning", request_id="4" * 32)
    proposal = subject.clarification_proposal(draft["id"], TODAY)
    assert proposal["action"]["changes"]["stage"] == "clarify"
    assert "text" not in proposal["action"]["changes"]
    execute.assert_not_called()
    generate.assert_not_called()


def test_closure_requires_owner_evidence_and_preserves_date_and_capture_scope():
    subject, _, execute, _, _, _ = service()
    payload = {
        "request_id": "6" * 32, "path": PATH, "revision": REVISION, "checked_done_when": True,
        "completion": {
            "result": "The synthetic outline has one heading.", "evidence": "Owner read it and checked the topic.",
            "verification": "owner", "verified_on": TODAY.isoformat(), "learning": "One topic kept the scope manageable.",
        },
    }
    with pytest.raises(TaskError, match="task_closure_requires_verification"):
        subject.close({**payload, "checked_done_when": False}, TODAY)
    proposal = subject.close(payload, TODAY)
    assert proposal["action"]["change"] == {"status": "done"}
    assert proposal["action"]["completion"] == payload["completion"]
    assert "deadline" not in proposal["action"]
    execute.assert_not_called()


def test_waiting_and_focus_are_canonical_mutations_not_private_schedule():
    subject, _, execute, _, _, _ = service()
    proposal = subject.refine(refine_payload(stage="waiting", waiting_for="Owner review.", review_on="2026-10-12"), TODAY)
    assert set(proposal["action"]["changes"]) == {"stage", "waiting_for", "review_on"}
    focus = subject.change({
        "request_id": "7" * 32, "path": PATH, "revision": REVISION, "change": {"focus_on": "2026-10-09"},
    }, TODAY)
    assert focus["action"]["change"] == {"focus_on": "2026-10-09"}
    assert "focus_on" not in workspace(subject.store.read())
    execute.assert_not_called()


@pytest.mark.parametrize("field", ["review_on", "waiting_for"])
def test_only_allowlisted_nulls_clear_fields(field):
    assert validate_changes({field: None}) == {field: None}
    with pytest.raises(Exception):
        validate_changes({"outcome": None})


def test_cas_failure_never_returns_a_saved_proposal_or_executes():
    subject, _, execute, _, _, blob = service()
    blob.always_conflict = True
    with pytest.raises(StateError, match="state_conflict_retry_exhausted"):
        subject.refine(refine_payload(), TODAY)
    execute.assert_not_called()


def test_old_ledger_remains_valid_without_task_workspace():
    subject, *_ = service()
    assert "task_workspace" not in empty_state()
    assert subject.store.read() == empty_state()


def test_preparation_capacity_accepts_real_legacy_proposals_in_same_ledger():
    subject, _, _, generate, _, _ = service()
    subject.store.update(lambda state: state["proposals"].update({"legacy": legacy_proposal("legacy")}))
    proposal = subject.prepare(prepare_payload(), TODAY)
    assert approve(subject, proposal)["status"] == "completed"
    generate.assert_called_once()


def test_monthly_limit_is_not_reset_by_the_next_day():
    subject, _, _, generate, _, _ = service(monthly_limit=1)
    proposal = subject.prepare(prepare_payload(), TODAY)
    approve(subject, proposal)
    second = subject.prepare(prepare_payload(request_id="a" * 32, scope="Another small draft."), TODAY)
    with pytest.raises(TaskError, match="task_budget_exhausted"):
        approve(subject, second, day=TODAY + timedelta(days=1))
    generate.assert_called_once()


def test_standing_revocation_between_preparation_and_claim_blocks_inference():
    subject, _, _, generate, _, _ = service()
    subject.select_projects({"projects": [PROJECT]})
    subject.standing({"enabled": True, "sources": {PATH: REVISION}})
    original = subject.prepare

    def revoke(payload, today):
        proposal = original(payload, today)
        subject.standing({"enabled": False, "sources": {}})
        return proposal

    subject.prepare = revoke
    with pytest.raises(TaskError, match="task_standing_scope_changed"):
        subject.proactive(TODAY)
    generate.assert_not_called()


def test_history_never_reexposes_preparation_after_source_policy_failure():
    from briefing_sources import SourceError
    subject, repository, _, _, _, _ = service()
    proposal = subject.prepare(prepare_payload(), TODAY)
    approve(subject, proposal)
    repository.read_receipt_source = Mock(side_effect=SourceError("task_source_not_permitted"))
    result = subject.history(0, TODAY)["items"][0]
    assert result["source_status"] == "unavailable"
    assert "action" not in result and "source_path" not in result and "approval_digest" not in result
    assert "preparation" not in result["result"]
    assert "Do not expand" not in str(result)


def test_changed_source_invalidates_draft_history_and_removes_approval_surface():
    subject, repository, _, _, _, _ = service()
    proposal = subject.refine(refine_payload(), TODAY)
    repository.records[PATH]["revision"] = "b" * 40
    result = subject.history(0, TODAY)["items"][0]
    assert result["source_status"] == "changed_or_removed" and result["status"] == "invalidated"
    assert "action" not in result
    assert subject.store.read()["proposals"][proposal["id"]]["status"] == "invalidated"


def test_removed_source_keeps_content_free_task_receipt_visible_after_pruning():
    subject, _, _, _, _, _ = service()
    proposal = subject.refine(refine_payload(), TODAY)
    approve(subject, proposal)
    subject.store.update(lambda state: prune_state(state, inventory_paths=set(), today=TODAY))
    record = subject.store.read()["proposals"][proposal["id"]]
    assert record["task_workspace"] is True and record["kind"] == "refine_task"
    assert "action" not in record and "source_path" not in record
    assert subject.history(0, TODAY)["items"][0]["id"] == proposal["id"]


def test_source_change_during_preparation_does_not_store_stale_generated_evidence():
    subject, repository, _, generate, _, _ = service()
    output = generate.return_value

    def change(_):
        repository.records[PATH]["revision"] = "b" * 40
        return output

    generate.side_effect = change
    proposal = subject.prepare(prepare_payload(), TODAY)
    result = approve(subject, proposal)
    assert result["status"] == "failed"
    assert "preparation" not in result["result"]


def test_original_capture_whitespace_is_not_rewritten_at_intake():
    subject, *_ = service()
    original = "  Draft one outline.\nKeep its boundaries.  "
    record = subject.capture({
        "request_id": "b" * 32, "text": original, "definition": {"title": "Draft one outline"},
    }, TODAY)
    assert record["action"]["text"] == original


def test_refine_waiting_metadata_requires_explicit_stage():
    subject, *_ = service()
    with pytest.raises(TaskError, match="task_waiting_transition_requires_stage"):
        subject.refine(refine_payload(review_on="2026-10-12"), TODAY)


def test_closed_task_history_uses_recorded_closed_blob_and_survives_open_source_removal():
    subject, repository, _, _, publish, _ = service()
    closed_path = PATH.replace("tasks/", "tasks/done/", 1)
    publish.return_value = {
        "status": "merged", "path": closed_path, "pr_url": "https://github.com/example/mindVault/pull/7",
        "source_revision": "9" * 40, "canonical_revision": "8" * 40,
    }
    record = subject.close({
        "request_id": "c" * 32, "path": PATH, "revision": REVISION, "checked_done_when": True,
        "completion": {"result": "An outline exists.", "evidence": "Owner checked the topic.",
                       "verification": "owner", "verified_on": TODAY.isoformat(), "learning": "Keep one topic."},
    }, TODAY)
    approve(subject, record)
    repository.records[closed_path] = {**repository.records.pop(PATH), "revision": "9" * 40, "path": closed_path}
    subject.store.update(lambda state: prune_state(state, inventory_paths={closed_path}, today=TODAY))
    history = subject.history(0, TODAY)["items"][0]
    assert history["source_status"] == "current"
    assert history["action"]["completion"]["learning"] == "Keep one topic."
    assert history["result"]["path"] == closed_path
