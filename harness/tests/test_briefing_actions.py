from __future__ import annotations

import json

import httpx

from briefing_actions import ActionGateway


def gateway(handler, memex_url="https://synthetic.example/api/personal_action?code=test"):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return ActionGateway(client=client, token="synthetic", repo="example/vault", memex_url=memex_url, chat_id=7)


def proposal(kind="create_task"):
    return {
        "action_id": "a" * 32, "source_revision": "b" * 40,
        "action": {"kind": kind, "text": "Compare public learning techniques"},
    }


def test_task_action_requires_matching_persisted_receipt():
    action = gateway(lambda req: httpx.Response(200, json={
        "action_id": "a" * 32, "status": "submitted",
        "pr_url": "https://github.com/example/vault/pull/2",
    }))
    result = action(proposal())
    assert result["status"] == "submitted"
    assert result["pr_url"].endswith("/2")


def test_uncertain_task_network_result_is_not_completed():
    def fail(req):
        raise httpx.ReadTimeout("synthetic-secret")
    result = gateway(fail)(proposal())
    assert result["status"] == "unknown"
    assert "synthetic-secret" not in json.dumps(result)


def test_missing_action_configuration_does_not_create_task():
    def unexpected(req):
        raise AssertionError("No external request expected")
    assert gateway(unexpected, None)(proposal())["status"] == "failed"


def test_research_scope_is_explicit_and_bounded():
    bodies = []
    def send(req):
        bodies.append(json.loads(req.content))
        return httpx.Response(201, json={"html_url": "https://github.com/example/vault/issues/4"})
    result = gateway(send)(proposal("research"))
    assert result["status"] == "submitted"
    assert "at most five" in bodies[0]["body"]
    assert "Do not create follow-on" in bodies[0]["body"]
    assert "mindme-action:" + "a" * 32 in bodies[0]["body"]


def test_uncertain_research_reconciliation_never_posts_another_issue():
    methods = []
    def inspect(req):
        methods.append(req.method)
        return httpx.Response(200, json={"items": []})
    result = gateway(inspect)(proposal("research"), True)
    assert result["status"] == "unknown"
    assert methods == ["GET"]


def test_merged_task_is_verified_against_the_canonical_blob_without_resubmission():
    record = proposal()
    path = "tasks/2026-09-13-action-" + "a" * 32 + ".md"
    record["result"] = {
        "status": "submitted", "pr_url": "https://github.com/example/vault/pull/3", "path": path,
    }
    methods = []

    def inspect(req):
        methods.append(req.method)
        if req.url.path.endswith("/files"):
            data = [{"filename": path, "status": "added", "sha": "b" * 40}]
        elif "/contents/" in req.url.path:
            data = {"path": path, "sha": "b" * 40, "type": "file"}
        else:
            data = {"merged": True}
        return httpx.Response(200, json=data)

    result = gateway(inspect)(record, True)
    assert result["status"] == "merged"
    assert methods == ["GET", "GET", "GET"]


def test_merged_pr_does_not_claim_a_changed_canonical_task_is_the_approved_result():
    record = proposal()
    path = "tasks/2026-09-13-action-" + "a" * 32 + ".md"
    record["result"] = {
        "status": "submitted", "pr_url": "https://github.com/example/vault/pull/3", "path": path,
    }

    def inspect(req):
        if req.url.path.endswith("/files"):
            data = [{"filename": path, "status": "added", "sha": "b" * 40}]
        elif "/contents/" in req.url.path:
            data = {"path": path, "sha": "c" * 40, "type": "file"}
        else:
            data = {"merged": True}
        return httpx.Response(200, json=data)

    assert gateway(inspect)(record, True)["status"] == "conflict"
