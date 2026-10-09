from __future__ import annotations

import json
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import azure.functions as func
import pytest

import function_app as fa
from task_auth import AuthConfig, AuthError, FLOW_COOKIE, SESSION_COOKIE
from task_web import TaskWeb
from test_task_auth import cookie_pair, identity
from test_task_service import PATH, REVISION, approve, refine_payload, service


def request(path, body=None, *, method=None, cookie="", csrf="", origin="https://synthetic.example", extra=None):
    return func.HttpRequest(
        method=method or ("POST" if body is not None else "GET"),
        url="https://synthetic.example/api/tasks/" + path, params={}, route_params={"path": path},
        headers={
            "Cookie": cookie, "X-MindMe-CSRF": csrf, "Origin": origin,
            "Content-Type": "application/json", **(extra or {}),
        },
        body=json.dumps(body).encode() if body is not None else b"",
    )


@pytest.fixture
def logged_in(identity):
    auth, _, claims, _, _, _ = identity
    uri, flow_cookie = auth.login()
    query = parse_qs(urlsplit(uri).query)
    claims["nonce"] = query["nonce"][0]
    session_cookie = auth.callback(cookie_pair(flow_cookie, FLOW_COOKIE), {"state": query["state"][0], "code": "synthetic"})
    cookie = cookie_pair(session_cookie, SESSION_COOKIE)
    return auth, cookie, auth.authenticate(cookie)["csrf"]


def app(auth, factory):
    return TaskWeb(
        env={"MINDME_WEB_ENABLED": "true", "MINDME_TASKS_ENABLED": "true"},
        auth=lambda: auth, service=factory,
    )


def data(response):
    return json.loads(response.get_body())


def test_no_cookie_or_forged_host_headers_can_reach_task_data(identity):
    auth = identity[0]
    factory = Mock()
    web = app(auth, factory)
    response = web.handle(request("api/overview", extra={
        "X-MS-CLIENT-PRINCIPAL": "forged", "X-Forwarded-Proto": "https",
    }), "api/overview")
    assert response.status_code == 401
    factory.assert_not_called()
    assert response.headers["Cache-Control"] == "no-store, private"


def test_missing_host_config_is_explicit_not_empty_or_fake_live_data():
    factory = Mock()

    def missing():
        AuthConfig.load({})

    web = TaskWeb(env={"MINDME_WEB_ENABLED": "true"}, auth=missing, service=factory)
    response = web.handle(request("api/session"), "api/session")
    assert response.status_code == 503 and data(response)["error"] == "auth_unconfigured"
    factory.assert_not_called()


def test_csrf_or_foreign_origin_cannot_mutate(logged_in):
    auth, cookie, csrf = logged_in
    factory = Mock()
    web = app(auth, factory)
    for origin, token in [("https://attacker.example", csrf), (auth.config.origin, "forged")]:
        response = web.handle(request("api/capture", {}, cookie=cookie, csrf=token, origin=origin), "api/capture")
        assert response.status_code == 403 and data(response)["error"] == "csrf_rejected"
    factory.assert_not_called()


def test_session_bootstrap_returns_only_csrf_and_expiry_not_identity_or_oauth_tokens(logged_in):
    auth, cookie, _ = logged_in
    factory = Mock()
    response = app(auth, factory).handle(request("api/session", cookie=cookie), "api/session")
    assert response.status_code == 200
    assert set(data(response)) == {"authenticated", "csrf", "expires"}
    assert auth.config.owner_id not in response.get_body().decode()
    factory.assert_not_called()


def test_web_task_read_and_exact_approval_use_existing_service_without_query_content(logged_in):
    auth, cookie, csrf = logged_in
    subject, _, execute, _, _, _ = service()
    web = app(auth, lambda: subject)
    source = web.handle(request("api/task", {"path": PATH}, cookie=cookie, csrf=csrf), "api/task")
    assert source.status_code == 200 and data(source)["revision"] == REVISION
    proposal = web.handle(request("api/refine", refine_payload(), cookie=cookie, csrf=csrf), "api/refine")
    assert proposal.status_code == 200
    execute.assert_not_called()
    record = data(proposal)
    result = web.handle(request("api/decide", {
        "proposal_id": record["id"], "approval_digest": record["approval_digest"], "decision": "approve",
    }, cookie=cookie, csrf=csrf), "api/decide")
    assert result.status_code == 200 and data(result)["status"] == "submitted"
    assert execute.call_count == 1
    assert web.handle(request("api/task", cookie=cookie), "api/task").status_code == 405


def test_private_source_state_is_not_returned_after_session_logout(logged_in):
    auth, cookie, csrf = logged_in
    subject, *_ = service()
    web = app(auth, lambda: subject)
    signed_out = web.handle(request("auth/logout", {}, cookie=cookie, csrf=csrf), "auth/logout")
    assert signed_out.status_code == 200
    assert web.handle(request("api/overview", cookie=cookie), "api/overview").status_code == 401


@pytest.mark.parametrize("payload", [
    {"proposal_id": []}, {"proposal_id": {}}, {"proposal_id": "not-an-id"},
])
def test_malformed_receipt_ids_fail_without_500(logged_in, payload):
    auth, cookie, csrf = logged_in
    subject, *_ = service()
    response = app(auth, lambda: subject).handle(
        request("api/reconcile", payload, cookie=cookie, csrf=csrf), "api/reconcile",
    )
    assert response.status_code == 422


def test_duplicate_json_keys_cannot_change_the_approved_action(logged_in):
    auth, cookie, csrf = logged_in
    factory = Mock()
    req = func.HttpRequest(
        method="POST", url=auth.config.origin + "/api/tasks/api/refine",
        headers={"Cookie": cookie, "Origin": auth.config.origin, "X-MindMe-CSRF": csrf,
                 "Content-Type": "application/json"},
        body=b'{"path":"tasks/one.md","path":"tasks/two.md"}',
    )
    response = app(auth, factory).handle(req, "api/refine")
    assert response.status_code == 422


def test_dependency_failures_are_unavailable_not_success_shaped_empty_list(logged_in):
    from briefing_sources import SourceError
    auth, cookie, _ = logged_in
    subject, *_ = service()
    subject.repository.page = Mock(side_effect=SourceError("source_unavailable"))
    response = app(auth, lambda: subject).handle(request("api/overview", cookie=cookie), "api/overview")
    assert response.status_code == 503
    assert "items" not in data(response)


@pytest.mark.asyncio
async def test_functions_routes_use_off_event_loop_adapter(monkeypatch):
    handler = Mock(return_value=func.HttpResponse("synthetic"))
    monkeypatch.setattr(fa, "_task_web", lambda: Mock(handle=handler))
    response = await fa.task_web_route(request("api/session"))
    assert response.status_code == 200
    assert handler.call_args.args[1] == "api/session"


def test_preparation_host_uses_existing_model_no_tools_no_storage_or_retries(monkeypatch):
    from types import SimpleNamespace

    http = Mock()
    model = Mock()
    model.with_options.return_value = model
    content = json.dumps({
        "summary": "A synthetic draft.", "steps": [], "uncertainties": [],
        "owner_next_action": "Review it.", "source_quote": "One topic.",
    })
    model.chat.completions.create.return_value.choices = [
        SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=content, refusal=None)),
    ]
    monkeypatch.setenv("MINDME_BRIEFING_MODEL", "existing-small-model")
    monkeypatch.setattr(fa, "_http_client", lambda: http)
    monkeypatch.setattr(fa, "_foundry", lambda: (None, model))
    fa._generate_task_preparation({"source": {"text": "One topic."}, "scope": "Outline only."})
    arguments = model.chat.completions.create.call_args.kwargs
    assert arguments["model"] == "existing-small-model"
    assert arguments["store"] is False and arguments["max_completion_tokens"] == 1200
    assert "tools" not in arguments
    assert "<<<DATA_" in arguments["messages"][1]["content"]
    assert model.with_options.call_args.kwargs["max_retries"] == 0
    assert model.with_options.call_args.kwargs["timeout"] == 40
    schema = arguments["response_format"]["json_schema"]["schema"]
    assert "maxItems" not in json.dumps(schema)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


def test_preparation_request_rejection_is_content_free_and_not_retried(monkeypatch, caplog):
    import httpx
    from openai import BadRequestError
    from task_service import TaskError

    model = Mock()
    model.with_options.return_value = model
    model.chat.completions.create.side_effect = BadRequestError(
        "synthetic-private-upstream-message",
        response=httpx.Response(400, request=httpx.Request("POST", "https://synthetic.example/chat/completions")),
        body={"code": "invalid_json_schema", "param": "response_format.json_schema.schema", "message": "synthetic-private-upstream-message"},
    )
    monkeypatch.setenv("MINDME_BRIEFING_MODEL", "existing-small-model")
    monkeypatch.setattr(fa, "_http_client", Mock())
    monkeypatch.setattr(fa, "_foundry", lambda: (None, model))
    with pytest.raises(TaskError, match="task_preparation_request_rejected"):
        fa._generate_task_preparation({"source": {"text": "One topic."}, "scope": "Outline only."})
    assert model.chat.completions.create.call_count == 1
    assert "status=400 code=invalid_json_schema parameter=response_format.json_schema.schema" in caplog.text
    assert "synthetic-private" not in caplog.text


@pytest.mark.parametrize("finish_reason,refusal", [("stop", None), ("length", None), ("stop", "Refused.")])
def test_preparation_actual_sdk_wire_and_incomplete_results(monkeypatch, finish_reason, refusal):
    import httpx
    from openai import OpenAI
    from task_service import TaskError

    output = {
        "summary": "A synthetic draft.", "steps": ["Choose a heading."], "uncertainties": [],
        "owner_next_action": "Review it.", "source_quote": "One topic.",
    }
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={
            "id": "synthetic-completion", "object": "chat.completion", "created": 1,
            "model": "existing-small-model",
            "choices": [{"index": 0, "finish_reason": finish_reason, "message": {
                "role": "assistant", "content": json.dumps(output), "refusal": refusal,
            }}],
        })

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        with OpenAI(api_key="synthetic-not-a-credential", base_url="https://synthetic.example/openai/v1/", http_client=http) as model:
            monkeypatch.setenv("MINDME_BRIEFING_MODEL", "existing-small-model")
            monkeypatch.setattr(fa, "_http_client", lambda: http)
            monkeypatch.setattr(fa, "_foundry", lambda: (None, model))
            if finish_reason == "stop" and refusal is None:
                assert fa._generate_task_preparation({"source": {"text": "One topic."}, "scope": "Outline only."}) == output
            else:
                with pytest.raises(TaskError, match="task_preparation_invalid"):
                    fa._generate_task_preparation({"source": {"text": "One topic."}, "scope": "Outline only."})
    assert len(requests) == 1
    assert requests[0].url.path == "/openai/v1/chat/completions"
    body = json.loads(requests[0].content)
    assert body["store"] is False and body["max_completion_tokens"] == 1200
    assert body["response_format"]["json_schema"]["strict"] is True
    assert "tools" not in body and "input" not in body


def test_disabled_web_has_no_auth_or_service_side_effects():
    auth, factory = Mock(), Mock()
    web = TaskWeb(env={}, auth=auth, service=factory)
    assert web.handle(request("api/session"), "api/session").status_code == 404
    auth.assert_not_called()
    factory.assert_not_called()
