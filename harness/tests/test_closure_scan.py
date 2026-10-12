"""Caller-side closure checkpoints use synthetic writer responses and real adapters."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from briefing_actions import ActionError, ActionGateway
from briefing_state import StateError, prune_state
from closure_scan import confirmed_scan
from task_service import TaskError, TaskService
from test_task_auth import identity
from test_task_service import PATH, REVISION, TODAY, approve, service
from test_task_web import app, data, logged_in, request

CLOSED_PATH = PATH.replace("tasks/", "tasks/done/", 1)


def closure_payload(request_id="c" * 32):
    return {
        "request_id": request_id, "path": PATH, "revision": REVISION, "checked_done_when": True,
        "completion": {
            "result": "A synthetic outline exists.", "evidence": "Owner checked the agreed topic.",
            "learning": "One topic kept the scope manageable.", "verification": "owner",
            "verified_on": TODAY.isoformat(),
        },
    }


class Writer:
    def __init__(self, passes=3):
        self.max_passes = passes
        self.pass_count = 0
        self.materializations = 0
        self.publications = 0
        self.requests = []
        self.approved = None
        self.result = None
        self.lose_response = False
        self.fail_before_receiving = False
        self.conflict = None
        self.during_call = None
        self.after_checkpoint = None
        self.publication_uncertainty = None
        self.publication_status = "submitted"

    def checkpoint(self, identifier):
        return {
            "version": 1, "action_id": identifier, "status": "in_progress", "path": "",
            "phase": "closure_scan", "resumable": True,
            "closure_continuation": f"{self.pass_count:032x}",
            "scan_pass": self.pass_count, "scan_pass_limit": 6,
        }

    def __call__(self, request):
        assert request.url.host == "synthetic.invalid"
        body = json.loads(request.content)
        self.requests.append(body)
        if self.during_call:
            callback, self.during_call = self.during_call, None
            callback()
        if body["kind"] == "publish_task":
            assert self.result["status"] == "submitted"
            self.publications += 1
            if self.publication_uncertainty == "timeout":
                raise httpx.ReadTimeout("synthetic publication response unavailable")
            if self.publication_uncertainty == "in_progress":
                return httpx.Response(503, json={
                    "version": 1, "action_id": body["action_id"], "target_action_id": body["target_action_id"],
                    "status": "in_progress", "path": "", "error": "remote_outcome_unknown",
                })
            result = {
                **self.result, "action_id": body["action_id"], "target_action_id": body["target_action_id"],
                "status": self.publication_status,
            }
            if self.publication_status == "merged":
                result["canonical_revision"] = "e" * 40
            return httpx.Response(200 if self.publication_status == "merged" else 202, json=result)
        assert body["kind"] == "update_task" and body["change"] == {"status": "done"}
        stable = {key: value for key, value in body.items() if key != "closure_continuation"}
        if self.approved is None:
            self.approved = copy.deepcopy(stable)
        assert stable == self.approved
        if self.fail_before_receiving:
            self.fail_before_receiving = False
            raise httpx.ReadTimeout("synthetic response unavailable")
        if self.conflict:
            return httpx.Response(409, json={
                "version": 1, "action_id": body["action_id"], "status": "conflict",
                "path": "", "error": self.conflict,
            })
        if self.result and self.result["status"] == "submitted":
            return httpx.Response(202, json=self.result)
        token = body.get("closure_continuation")
        if self.pass_count and token != self.result["closure_continuation"]:
            return httpx.Response(202, json=self.result)
        if self.pass_count < self.max_passes:
            self.pass_count += 1
            self.result = self.checkpoint(body["action_id"])
        else:
            self.materializations += 1
            self.result = {
                "version": 1, "action_id": body["action_id"], "status": "submitted", "path": CLOSED_PATH,
                "pr_url": "https://github.com/example/mindVault/pull/7", "source_revision": "b" * 40,
            }
        response_result = copy.deepcopy(self.result)
        if self.after_checkpoint:
            callback, self.after_checkpoint = self.after_checkpoint, None
            callback()
        if self.lose_response:
            self.lose_response = False
            return httpx.Response(503, json={
                "version": 1, "action_id": body["action_id"], "status": "in_progress", "path": "",
                "error": "task_closure_checkpoint_unavailable",
            })
        return httpx.Response(202, json=response_result)


def system(writer=None):
    writer = writer or Writer()
    original, repository, _, generate, _, blob = service()
    gateway = ActionGateway(
        client=httpx.Client(transport=httpx.MockTransport(writer)), token="synthetic",
        repo="example/mindVault", memex_url="https://synthetic.invalid/api/personal_action",
        chat_id=7, task_enabled=lambda: True,
    )
    original.loop.execute = gateway
    subject = TaskService(
        loop=original.loop, repository=repository, generate=generate, publish=gateway.publish_task,
        resume_closure=gateway.continue_closure, attention_timezone=original.attention_timezone, clock=original.clock,
    )
    return subject, writer, repository, generate, blob, gateway


def step_payload(record, request_id):
    return {
        "proposal_id": record["id"], "request_id": request_id,
        "closure_continuation": record["result"]["closure_continuation"],
    }


def test_initial_approval_checkpoints_once_and_only_explicit_steps_advance():
    subject, writer, _, generate, _, _ = system()
    proposal = subject.close(closure_payload(), TODAY)
    assert writer.requests == []
    first = approve(subject, proposal)
    assert confirmed_scan(first["result"]) and first["status"] == "executing"
    assert writer.pass_count == 1 and writer.materializations == writer.publications == 0
    assert first["action"] == proposal["action"]
    original_id = first["result"]["action_id"]
    approve(subject, proposal)
    subject.loop.reconcile(TODAY)
    assert subject.reconcile(first["id"], TODAY)["result"] == first["result"]
    assert subject.publish(first["id"], TODAY)["result"] == first["result"]
    assert len(writer.requests) == 1

    second = subject.continue_closure(step_payload(first, "1" * 32), TODAY)
    assert second["result"]["scan_pass"] == 2 and writer.pass_count == 2
    assert writer.requests[1]["action_id"] == original_id
    assert writer.requests[1]["closure_continuation"] == first["result"]["closure_continuation"]
    third = subject.continue_closure(step_payload(second, "2" * 32), TODAY)
    assert third["result"]["scan_pass"] == 3 and writer.materializations == 0
    submitted = subject.continue_closure(step_payload(third, "3" * 32), TODAY)
    assert submitted["status"] == "submitted"
    assert "closure_continuation" not in submitted["result"]
    assert writer.materializations == writer.publications == 1
    assert all(packet["action_id"] == original_id for packet in writer.requests if packet["kind"] == "update_task")
    assert "task_workspace" not in writer.approved
    assert subject.store.read()["task_workspace"]["budget"]["used"] == 0
    generate.assert_not_called()


def test_sixth_checkpoint_can_finalize_without_inventing_a_seventh_scan():
    subject, writer, *_ = system(Writer(passes=6))
    current = approve(subject, subject.close(closure_payload(), TODAY))
    for index in range(1, 6):
        current = subject.continue_closure(step_payload(current, f"{index:032x}"), TODAY)
    assert current["result"]["scan_pass"] == 6
    submitted = subject.continue_closure(step_payload(current, "a" * 32), TODAY)
    assert submitted["status"] == "submitted"
    assert writer.pass_count == 6 and writer.materializations == 1


def test_duplicate_click_stale_tab_and_duplicate_approval_do_not_spend_another_pass():
    subject, writer, *_ = system()
    first = approve(subject, subject.close(closure_payload(), TODAY))
    payload = step_payload(first, "1" * 32)
    second = subject.continue_closure(payload, TODAY)
    assert subject.continue_closure(payload, TODAY)["result"] == second["result"]
    assert writer.pass_count == 2 and len(writer.requests) == 2
    with pytest.raises(TaskError, match="task_closure_checkpoint_changed"):
        subject.continue_closure({**payload, "request_id": "2" * 32}, TODAY)
    with pytest.raises(TaskError, match="task_request_conflict"):
        subject.continue_closure({**payload, "closure_continuation": second["result"]["closure_continuation"]}, TODAY)
    assert writer.pass_count == 2


def test_lost_checkpoint_is_recovered_with_last_sent_token_not_next_pass():
    subject, writer, *_ = system()
    first = approve(subject, subject.close(closure_payload(), TODAY))
    writer.lose_response = True
    uncertain = subject.continue_closure(step_payload(first, "1" * 32), TODAY)
    assert uncertain["status"] == "uncertain"
    assert uncertain["result"]["error"] == "task_closure_checkpoint_unavailable"
    assert not uncertain["result"].get("resumable")
    assert writer.pass_count == 2
    subject.loop.reconcile(TODAY)
    subject.reconcile(first["id"], TODAY)
    assert len(writer.requests) == 2
    recovered = subject.recover_closure({"proposal_id": first["id"], "request_id": "2" * 32}, TODAY)
    assert writer.requests[-1]["closure_continuation"] == first["result"]["closure_continuation"]
    assert writer.pass_count == 2 and recovered["result"]["scan_pass"] == 2
    assert subject.recover_closure({"proposal_id": first["id"], "request_id": "2" * 32}, TODAY)["result"] == recovered["result"]
    assert len(writer.requests) == 3
    assert writer.materializations == 0


def test_initial_lost_response_uses_original_body_without_new_action_or_token():
    subject, writer, *_ = system()
    writer.lose_response = True
    first = approve(subject, subject.close(closure_payload(), TODAY))
    assert first["status"] == "uncertain" and writer.pass_count == 1
    recovered = subject.recover_closure({"proposal_id": first["id"], "request_id": "1" * 32}, TODAY)
    assert writer.requests[0] == writer.requests[1]
    assert recovered["result"]["scan_pass"] == 1 and writer.pass_count == 1


def test_uncertain_unreceived_initial_call_can_only_retry_the_same_approved_operation():
    subject, writer, *_ = system()
    writer.fail_before_receiving = True
    first = approve(subject, subject.close(closure_payload(), TODAY))
    assert first["status"] == "uncertain" and writer.pass_count == 0
    recovered = subject.recover_closure({"proposal_id": first["id"], "request_id": "1" * 32}, TODAY)
    assert writer.requests[0] == writer.requests[1]
    assert recovered["result"]["scan_pass"] == 1 and writer.materializations == 0


def test_cas_rechecks_checkpoint_before_a_stale_concurrent_step_sends():
    subject, writer, _, _, blob, _ = system()
    first = approve(subject, subject.close(closure_payload(), TODAY))
    payload = step_payload(first, "1" * 32)
    blob.before_upload = lambda _: subject.continue_closure({**payload, "request_id": "2" * 32}, TODAY)
    with pytest.raises(TaskError, match="task_closure_checkpoint_changed"):
        subject.continue_closure(payload, TODAY)
    assert writer.pass_count == 2 and len(writer.requests) == 2


def test_late_result_cannot_replace_a_newer_recovery_result():
    subject, writer, *_ = system()
    first = approve(subject, subject.close(closure_payload(), TODAY))
    writer.during_call = lambda: subject.recover_closure(
        {"proposal_id": first["id"], "request_id": "2" * 32}, TODAY,
    )
    result = subject.continue_closure(step_payload(first, "1" * 32), TODAY)
    assert result["result"]["scan_pass"] == 2
    assert writer.pass_count == 2
    assert subject.store.read()["proposals"][first["id"]]["closure_request_id"] == "2" * 32


def test_late_initial_response_cannot_rewind_a_manually_recovered_checkpoint():
    subject, writer, *_ = system()
    proposal = subject.close(closure_payload(), TODAY)

    def recover_and_continue():
        recovered = subject.recover_closure({"proposal_id": proposal["id"], "request_id": "1" * 32}, TODAY)
        assert recovered["result"]["scan_pass"] == 1
        subject.continue_closure(step_payload(recovered, "2" * 32), TODAY)

    writer.after_checkpoint = recover_and_continue
    result = approve(subject, proposal)
    assert result["result"]["scan_pass"] == 2
    assert writer.pass_count == 2


def test_failed_checkpoint_claim_persistence_cannot_start_a_writer_step():
    subject, writer, _, _, blob, _ = system()
    first = approve(subject, subject.close(closure_payload(), TODAY))
    blob.always_conflict = True
    with pytest.raises(StateError, match="state_conflict_retry_exhausted"):
        subject.continue_closure(step_payload(first, "1" * 32), TODAY)
    assert writer.pass_count == len(writer.requests) == 1


def test_publication_gateway_refuses_scanning_closure_without_submission():
    subject, writer, _, _, _, gateway = system()
    first = approve(subject, subject.close(closure_payload(), TODAY))
    stored = subject.store.read()["proposals"][first["id"]]
    with pytest.raises(ActionError, match="task_closure_scan_not_submitted"):
        gateway.publish_task(stored, "a" * 32)
    assert writer.publications == 0 and len(writer.requests) == 1


@pytest.mark.parametrize("uncertainty", ["in_progress", "timeout"])
def test_timer_publication_uncertainty_preserves_submission_and_recovery_path(uncertainty):
    subject, writer, *_ = system(Writer(passes=0))
    submitted = approve(subject, subject.close(closure_payload(), TODAY))
    assert submitted["status"] == "submitted"
    receipt = copy.deepcopy(submitted["result"])
    assert writer.materializations == writer.publications == 1
    writer.publication_uncertainty = uncertainty
    subject.loop.reconcile(TODAY)
    current = subject.store.read()["proposals"][submitted["id"]]
    assert current["result"] == receipt
    assert current["publish_result"]["status"] in {"in_progress", "unknown"}
    assert current["status"] == "uncertain"
    assert writer.publications == 2
    writer.publication_uncertainty = None
    recovered = subject.reconcile(submitted["id"], TODAY)
    assert recovered["status"] == "submitted"
    assert recovered["result"] == receipt
    assert writer.publications == 3 and writer.materializations == 1
    assert sum(packet["kind"] == "update_task" for packet in writer.requests) == 1
    assert all("closure_continuation" not in packet for packet in writer.requests)


def test_submitted_phase_survives_an_older_ambiguous_result_without_a_pr_url():
    subject, writer, *_ = system(Writer(passes=0))
    submitted = approve(subject, subject.close(closure_payload(), TODAY))
    subject.store.update(lambda state: state["proposals"][submitted["id"]].update(
        result={"status": "in_progress", "error": "remote_outcome_unknown"},
    ))
    recovered = subject.reconcile(submitted["id"], TODAY)
    assert recovered["result"]["pr_url"] == submitted["result"]["pr_url"]
    assert writer.publications == 2 and writer.materializations == 1
    assert sum(packet["kind"] == "update_task" for packet in writer.requests) == 1


def test_timer_recovers_unknown_publication_to_confirmed_merge_without_another_scan():
    subject, writer, *_ = system(Writer(passes=0))
    submitted = approve(subject, subject.close(closure_payload(), TODAY))
    original_action_id = submitted["result"]["action_id"]
    writer.publication_uncertainty = "in_progress"
    subject.loop.reconcile(TODAY)
    writer.publication_uncertainty = None
    writer.publication_status = "merged"
    subject.loop.reconcile(TODAY)
    record = subject.store.read()["proposals"][submitted["id"]]
    assert record["status"] == "completed"
    assert record["result"]["status"] == "merged"
    assert record["result"]["path"] == CLOSED_PATH
    assert record["result"]["action_id"] == original_action_id
    assert record["publish_result"]["action_id"] == record["publication_action_id"]
    assert record["result"]["canonical_revision"] == "e" * 40
    assert writer.publications == 3 and writer.materializations == 1


@pytest.mark.parametrize("code", [
    "task_closure_source_changed", "task_closure_inventory_changed", "task_closure_scan_exhausted",
])
def test_terminal_writer_conflict_stays_terminal_and_needs_new_owner_approval(code):
    subject, writer, *_ = system()
    first = approve(subject, subject.close(closure_payload(), TODAY))
    writer.conflict = code
    failed = subject.continue_closure(step_payload(first, "1" * 32), TODAY)
    assert failed["status"] == "invalidated" and failed["result"]["error"] == code
    saved = copy.deepcopy(subject.store.read()["proposals"][first["id"]])
    subject.recover_closure({"proposal_id": first["id"], "request_id": "2" * 32}, TODAY)
    assert len(writer.requests) == 2
    fresh = subject.close(closure_payload("d" * 32), TODAY)
    assert fresh["id"] != first["id"] and fresh["status"] == "pending"
    assert subject.store.read()["proposals"][first["id"]] == saved


def test_unapproved_forged_disabled_and_source_changed_steps_never_reach_writer():
    subject, writer, repository, *_ = system()
    pending = subject.close(closure_payload(), TODAY)
    with pytest.raises(TaskError, match="task_closure_not_authorized"):
        subject.recover_closure({"proposal_id": pending["id"], "request_id": "1" * 32}, TODAY)
    first = approve(subject, pending)
    with pytest.raises(TaskError, match="task_closure_checkpoint_changed"):
        subject.continue_closure({**step_payload(first, "1" * 32), "closure_continuation": "f" * 32}, TODAY)
    subject.enabled = lambda: False
    with pytest.raises(TaskError, match="tasks_not_configured"):
        subject.continue_closure(step_payload(first, "2" * 32), TODAY)
    subject.enabled = lambda: True
    repository.records[PATH]["revision"] = "e" * 40
    with pytest.raises(TaskError, match="task_source_changed"):
        subject.continue_closure(step_payload(first, "3" * 32), TODAY)
    assert writer.pass_count == len(writer.requests) == 1


def test_old_conflict_receipt_and_removed_source_tombstone_cannot_resume():
    subject, writer, *_ = system()
    first = approve(subject, subject.close(closure_payload(), TODAY))
    subject.store.update(lambda state: prune_state(state, inventory_paths=set(), today=TODAY))
    saved = copy.deepcopy(subject.store.read()["proposals"][first["id"]])
    with pytest.raises(TaskError, match="task_closure_not_authorized"):
        subject.recover_closure({"proposal_id": first["id"], "request_id": "1" * 32}, TODAY)
    assert subject.store.read()["proposals"][first["id"]] == saved
    assert len(writer.requests) == 1


@pytest.mark.parametrize("change", [
    {"version": True}, {"path": PATH}, {"resumable": False}, {"scan_pass": True}, {"scan_pass": 7},
    {"scan_pass_limit": 7}, {"closure_continuation": "A" * 32}, {"phase": "writing"},
    {"evidence": "untrusted source content"},
])
def test_malformed_scan_receipts_never_grant_continuation(change):
    subject, writer, *_ = system()
    valid = writer.checkpoint("a" * 32)
    valid["scan_pass"] = 1
    valid["closure_continuation"] = "1" * 32
    response = httpx.Response(202, json={**valid, **change})
    with pytest.raises(ActionError, match="invalid_closure_receipt"):
        ActionGateway._closure_receipt(response, "a" * 32)


def test_uncertain_checkpoint_with_resume_authority_is_rejected():
    receipt = {
        "version": 1, "action_id": "a" * 32, "status": "in_progress", "path": "",
        "error": "task_closure_checkpoint_unavailable", "resumable": True,
        "closure_continuation": "1" * 32,
    }
    with pytest.raises(ActionError, match="invalid_closure_receipt"):
        ActionGateway._closure_receipt(httpx.Response(503, json=receipt), "a" * 32)


def test_duplicate_protocol_keys_do_not_silently_grant_scan_authority():
    receipt = Writer().checkpoint("a" * 32)
    receipt.update(scan_pass=1, closure_continuation="1" * 32)
    encoded = json.dumps(receipt).replace('"resumable": true', '"resumable": false, "resumable": true')
    with pytest.raises(ActionError, match="invalid_closure_receipt"):
        ActionGateway._closure_receipt(httpx.Response(202, content=encoded.encode()), "a" * 32)


def test_legacy_terminal_conflict_is_not_replaced_by_new_closure_request():
    subject, writer, *_ = system()
    first = subject.close(closure_payload(), TODAY)

    def legacy(state):
        saved = state["proposals"][first["id"]]
        saved.pop("closure_scan_protocol")
        saved.update(
            status="invalidated", action_id="a" * 32,
            result={"status": "conflict", "error": "task_context_limit", "path": ""},
        )

    subject.store.update(legacy)
    old = copy.deepcopy(subject.store.read()["proposals"][first["id"]])
    with pytest.raises(TaskError, match="task_closure_not_authorized"):
        subject.recover_closure({"proposal_id": first["id"], "request_id": "1" * 32}, TODAY)
    new = subject.close(closure_payload("d" * 32), TODAY)
    assert new["id"] != first["id"]
    assert new["status"] == "pending" and writer.requests == []
    assert subject.store.read()["proposals"][first["id"]] == old


def test_closure_step_routes_require_owner_and_csrf(logged_in):
    auth, cookie, csrf = logged_in
    subject, writer, *_ = system()
    first = approve(subject, subject.close(closure_payload(), TODAY))
    web = app(auth, lambda: subject)
    body = step_payload(first, "1" * 32)
    assert web.handle(request("api/continue-closure", body), "api/continue-closure").status_code == 401
    assert web.handle(request("api/continue-closure", body, cookie=cookie), "api/continue-closure").status_code == 403
    result = web.handle(request("api/continue-closure", body, cookie=cookie, csrf=csrf), "api/continue-closure")
    assert result.status_code == 200 and data(result)["result"]["scan_pass"] == 2
    assert writer.pass_count == 2


def test_actual_closure_wire_export(tmp_path):
    subject, writer, *_ = system()
    first = approve(subject, subject.close(closure_payload(), TODAY))
    second = subject.continue_closure(step_payload(first, "1" * 32), TODAY)
    writer.lose_response = True
    subject.continue_closure(step_payload(second, "2" * 32), TODAY)
    recovered = subject.recover_closure({"proposal_id": first["id"], "request_id": "3" * 32}, TODAY)
    subject.continue_closure(step_payload(recovered, "4" * 32), TODAY)
    packets = writer.requests
    original = packets[0]
    for packet in packets:
        assert len(json.dumps(packet, separators=(",", ":"), ensure_ascii=False).encode()) <= 8192
        if packet["kind"] == "update_task":
            assert {key: value for key, value in packet.items() if key != "closure_continuation"} == original
        else:
            assert packet["kind"] == "publish_task" and packet["target_action_id"] == original["action_id"]
    target = Path(os.environ.get("MINDME_TEST_CLOSURE_OUTPUT", str(tmp_path / "closure-writer-requests.json")))
    target.write_text(json.dumps(packets, indent=2), encoding="utf-8")
