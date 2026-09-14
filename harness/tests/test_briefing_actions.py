from __future__ import annotations

import json

import httpx
import pytest

from briefing_actions import ActionError, ActionGateway


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


def test_review_envelope_pins_the_canonical_commit_outside_the_exact_receipt():
    bodies = []
    review = {"version": 1, "as_of": "2026-09-14"}
    def send(req):
        bodies.append(json.loads(req.content))
        assert req.extensions["timeout"]["read"] == 30.0
        return httpx.Response(202, json={
            "action_id": "a" * 32, "status": "submitted",
            "pr_url": "https://github.com/example/vault/pull/7",
            "artifact_paths": [
                "reviews/vault-evolve/2026-09-14/review.md",
                "reviews/vault-evolve/2026-09-14/review.json",
            ],
        })
    result = gateway(send).save_review("a" * 32, review, "c" * 40)
    assert bodies == [{
        "version": 1, "vault_id": "mindMe", "chat_id": 7,
        "action_id": "a" * 32, "kind": "save_review",
        "review": review, "source_revision": "c" * 40,
    }]
    assert len(result["artifact_paths"]) == 2


@pytest.mark.parametrize("status,http_status", [("in_progress", 503), ("closed", 409), ("conflict", 409)])
def test_review_gateway_preserves_retryable_and_terminal_writer_outcomes(status, http_status):
    service = gateway(lambda req: httpx.Response(http_status, json={
        "action_id": "a" * 32, "status": status,
    }))
    assert service.save_review("a" * 32, {}, "c" * 40)["status"] == status


def test_review_gateway_refuses_success_with_missing_or_foreign_artifact_link():
    service = gateway(lambda req: httpx.Response(202, json={
        "action_id": "a" * 32, "status": "submitted",
        "pr_url": "https://github.com/example/another-vault/pull/7",
    }))
    with pytest.raises(ActionError, match="invalid_review_result_link"):
        service.save_review("a" * 32, {}, "c" * 40)


def test_review_gateway_enforces_the_writers_utf8_request_limit_before_sending():
    def unexpected(req):
        raise AssertionError("oversized review must not reach the writer")
    with pytest.raises(ActionError, match="review_payload_capacity"):
        gateway(unexpected).save_review("a" * 32, {"text": "\u2603" * 20_000}, "c" * 40)
