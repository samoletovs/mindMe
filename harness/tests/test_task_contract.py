"""These fixtures are captured from real TaskService -> ActionGateway serialization."""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from briefing_actions import ActionError, ActionGateway
from task_service import TaskService
from test_task_service import (
    ORIGINAL, PATH, PROJECT, REVISION, TODAY, approve, refine_payload, service,
)


def serialized_writer_requests():
    packets, receipts = [], {}

    def writer(request):
        packet = json.loads(request.content)
        packets.append(packet)
        identifier = packet["action_id"]
        if packet["kind"] == "publish_task":
            target = receipts[packet["target_action_id"]]
            return httpx.Response(200, json={
                **target, "status": "merged", "action_id": identifier,
                "target_action_id": packet["target_action_id"], "canonical_revision": "f" * 40,
            })
        path = packet.get("path") or f"tasks/2026-10-08-action-{identifier}.md"
        if packet.get("change", {}).get("status") == "done":
            path = "tasks/done/" + path.rsplit("/", 1)[-1]
        result = {
            "version": 1, "action_id": identifier, "status": "submitted", "path": path,
            "source_revision": "b" * 40, "pr_url": "https://github.com/example/mindVault/pull/7",
        }
        receipts[identifier] = result
        return httpx.Response(202, json=result)

    original, repository, _, generate, _, _ = service()
    gateway = ActionGateway(
        client=httpx.Client(transport=httpx.MockTransport(writer)), token="synthetic",
        repo="example/mindVault", memex_url="https://synthetic.example/api/personal_action", chat_id=7,
        task_enabled=lambda: True,
    )
    original.loop.execute = gateway
    subject = TaskService(
        loop=original.loop, repository=repository, generate=generate, publish=gateway.publish_task,
        attention_timezone=original.attention_timezone, clock=original.clock,
    )
    create = subject.capture({
        "request_id": "1" * 32, "text": ORIGINAL,
        "definition": {
            "title": "Draft one synthetic outline", "outcome": "One outline exists.",
            "next_action": "Write one heading.", "done_when": "Read the saved outline.",
            "area": "learning", "project": PROJECT, "stage": "backlog", "execution": "assisted",
        },
    }, TODAY)
    approve(subject, create)
    refine = subject.refine({**refine_payload(next_action="Write a heading.\nKeep one topic."), "request_id": "2" * 32}, TODAY)
    approve(subject, refine)
    waiting = subject.refine({
        **refine_payload(stage="waiting", waiting_for="Owner review.", review_on="2026-10-12"),
        "request_id": "3" * 32,
    }, TODAY)
    approve(subject, waiting)
    focus = subject.change({
        "request_id": "4" * 32, "path": PATH, "revision": REVISION, "change": {"focus_on": "2026-10-09"},
    }, TODAY)
    approve(subject, focus)
    deadline = subject.change({
        "request_id": "5" * 32, "path": PATH, "revision": REVISION, "change": {"deadline": None},
    }, TODAY)
    approve(subject, deadline)
    close = subject.close({
        "request_id": "6" * 32, "path": PATH, "revision": REVISION, "checked_done_when": True,
        "completion": {
            "result": "The synthetic outline has one heading.", "evidence": "Owner read it and checked the topic.",
            "verification": "owner", "verified_on": "2026-10-08", "learning": "One topic kept the scope manageable.",
        },
    }, TODAY)
    approve(subject, close)
    return packets


def test_actual_serialization_exports_cross_repository_contract(tmp_path):
    packets = serialized_writer_requests()
    assert [item["kind"] for item in packets] == [
        "create_task", "publish_task", "refine_task", "publish_task",
        "refine_task", "publish_task", "update_task", "publish_task",
        "update_task", "publish_task", "update_task", "publish_task",
    ]
    for packet in packets:
        assert len(json.dumps(packet, ensure_ascii=False, separators=(",", ":")).encode()) <= 8192
        assert not set(packet) & {"pr_url", "repository", "repo", "branch", "approver"}
        if packet["kind"] == "publish_task":
            assert packet["target_action_id"] != packet["action_id"]
    destination = Path(os.environ.get("MINDME_TEST_CONTRACT_OUTPUT", str(tmp_path / "task-writer-requests.json")))
    destination.write_text(json.dumps(packets, indent=2), encoding="utf-8")


def test_task_202_in_progress_is_not_mistaken_for_submission():
    gateway = ActionGateway(
        client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(202, json={
            "version": 1, "action_id": "a" * 32, "status": "in_progress",
        }))),
        token="synthetic", repo="example/mindVault", memex_url="https://synthetic.example/api/personal_action",
        chat_id=7,
        task_enabled=lambda: True,
    )
    assert gateway({
        "task_workspace": True, "action_id": "a" * 32,
        "action": {"kind": "create_task", "text": "Synthetic task", "definition": {"title": "Synthetic task"}},
    })["status"] == "in_progress"


def test_unknown_task_reconciliation_without_publication_receipt_never_posts_again():
    methods = []
    gateway = ActionGateway(
        client=httpx.Client(transport=httpx.MockTransport(lambda request: methods.append(request.method))),
        token="synthetic", repo="example/mindVault", memex_url="https://synthetic.example/api/personal_action",
        chat_id=7,
        task_enabled=lambda: True,
    )
    result = gateway({
        "task_workspace": True, "action_id": "a" * 32, "action": {"kind": "refine_task", "path": PATH},
    }, True)
    assert result["status"] == "unknown" and methods == []


def test_merge_claim_from_generic_writer_is_not_accepted_as_verified_publication():
    gateway = ActionGateway(
        client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
            "version": 1, "action_id": "a" * 32, "status": "merged",
        }))), token="synthetic", repo="example/mindVault",
        memex_url="https://synthetic.example/api/personal_action", chat_id=7, task_enabled=lambda: True,
    )
    with pytest.raises(ActionError, match="invalid_task_receipt"):
        gateway({
            "task_workspace": True, "action_id": "a" * 32, "source_revision": REVISION,
            "action": {"kind": "refine_task", "path": PATH, "changes": {"stage": "ready"}},
        })


@pytest.mark.parametrize("structured", [True, False])
def test_capture_serialization_preserves_original_structured_text_without_changing_legacy_trim(structured):
    packets = []

    def writer(request):
        packets.append(json.loads(request.content))
        return httpx.Response(202, json={"version": 1, "action_id": "a" * 32, "status": "in_progress"})

    gateway = ActionGateway(
        client=httpx.Client(transport=httpx.MockTransport(writer)), token="synthetic",
        repo="example/mindVault", memex_url="https://synthetic.example/api/personal_action", chat_id=7,
        task_enabled=lambda: True,
    )
    original = "  Draft a synthetic outline.\nKeep one topic.  " if structured else "  Draft a synthetic outline.  "
    action = {"kind": "create_task", "text": original}
    if structured:
        action["definition"] = {"title": "Draft a synthetic outline", "stage": "clarify"}
    gateway({"task_workspace": True, "action_id": "a" * 32, "action": action})
    assert packets[0]["text"] == (original if structured else original.strip())
