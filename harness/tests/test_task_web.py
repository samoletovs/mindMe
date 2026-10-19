from __future__ import annotations

import json
from unittest.mock import Mock
from urllib.parse import parse_qs, urlencode, urlsplit

import azure.functions as func
import pytest

import function_app as fa
from task_auth import AuthConfig, AuthError, FLOW_COOKIE, SESSION_COOKIE
from task_web import TaskWeb
from test_task_auth import auth_env, cookie_pair, identity
from test_task_service import PATH, REVISION, approve, refine_payload, service


def request(path, body=None, *, method=None, cookie="", csrf="", origin="https://synthetic.example", extra=None, url=None):
    return func.HttpRequest(
        method=method or ("POST" if body is not None else "GET"),
        url=url or "https://synthetic.example/api/tasks/" + path, params={}, route_params={"path": path},
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


@pytest.fixture
def redirect_web(identity, auth_env):
    auth_env.update(
        MINDME_WEB_ORIGIN="https://mindme.synthetic.example",
        MINDME_WEB_REDIRECT_ORIGINS="https://vault.synthetic.example",
    )
    auth = identity[0]
    auth.config = AuthConfig.load(auth_env)
    auth_factory, service_factory = Mock(return_value=auth), Mock()
    web = TaskWeb(env=auth_env, auth=auth_factory, service=service_factory)
    return web, auth, auth_factory, service_factory


@pytest.mark.parametrize(("path", "suffix", "target"), [
    ("", "", "/api/tasks"),
    ("", "/", "/api/tasks"),
    ("auth/login", "/auth/login", "/api/tasks/auth/login"),
    ("auth/login/", "/auth/login/", "/api/tasks/auth/login"),
])
def test_legacy_navigation_uses_only_fixed_canonical_destinations(redirect_web, identity, path, suffix, target, caplog):
    web, auth, auth_factory, service_factory = redirect_web
    query = "?next=https://attacker.example/&redirect_uri=//attacker.example&code=synthetic-code&state=synthetic-state&access_token=synthetic-token"
    result = web.handle(request(
        path, url="https://vault.synthetic.example/api/tasks" + suffix + query,
        cookie="synthetic-cookie", extra={"X-Forwarded-Host": "attacker.example"},
    ), path)
    assert result.status_code == 302
    assert result.headers["Location"] == auth.config.origin + target
    assert result.headers["Cache-Control"] == "no-store, private"
    assert result.headers["Referrer-Policy"] == "no-referrer"
    assert result.get_body() == b"" and "Set-Cookie" not in result.headers
    assert not urlsplit(result.headers["Location"]).fragment
    assert all(value not in caplog.text for value in ("synthetic-code", "synthetic-token", "synthetic-cookie"))
    auth_factory.assert_not_called()
    service_factory.assert_not_called()
    assert identity[3] == []


@pytest.mark.parametrize("url", [
    "http://vault.synthetic.example/api/tasks",
    "https://unlisted.synthetic.example/api/tasks",
    "https://vault.synthetic.example.evil.example/api/tasks",
    "https://vault.synthetic.example@evil.example/api/tasks",
    "https://evil.example@vault.synthetic.example/api/tasks",
    "https://vault.synthetic.example:443/api/tasks",
    "https://vault.synthetic.example./api/tasks",
    "https://VAULT.synthetic.example/api/tasks",
    "https://vault.synthetic.example\\@evil.example/api/tasks",
    "https://vault.synthetic.example\t/api/tasks",
    " https://vault.synthetic.example/api/tasks",
    "//vault.synthetic.example/api/tasks",
    "https://[invalid/api/tasks",
])
def test_navigation_requires_the_exact_actual_https_url_not_spoofed_headers(redirect_web, url):
    web, auth, auth_factory, service_factory = redirect_web
    result = web.handle(request("", url=url, origin=auth.config.origin, extra={
        "Host": "vault.synthetic.example",
        "X-Forwarded-Host": "vault.synthetic.example", "X-Forwarded-Proto": "https",
        "Forwarded": "proto=https;host=vault.synthetic.example",
        "X-Original-Host": "mindme.synthetic.example",
    }))
    assert result.status_code == 400 and data(result)["error"] == "secure_origin_required"
    assert "Location" not in result.headers and "Set-Cookie" not in result.headers
    auth_factory.assert_not_called()
    service_factory.assert_not_called()


@pytest.mark.parametrize(("method", "path", "status"), [
    ("POST", "", 405), ("POST", "auth/login", 400),
    ("GET", "auth/callback", 400), ("POST", "auth/callback", 400),
    ("POST", "auth/logout", 400),
    ("GET", "api/session", 400), ("GET", "api/overview", 400),
    ("GET", "api/dashboard/today", 400), ("GET", "api/assist/usage", 400),
    ("POST", "api/task", 400), ("POST", "api/decide", 400),
    ("POST", "api/assist/chat", 400), ("GET", "auth/login/extra", 400),
])
def test_legacy_non_navigation_keeps_method_and_secure_origin_rejection(
    redirect_web, identity, monkeypatch, method, path, status,
):
    web, auth, _, service_factory = redirect_web
    forbidden = Mock(side_effect=AssertionError("Legacy requests must not authenticate or execute"))
    for name in ("login", "callback", "authenticate", "logout"):
        monkeypatch.setattr(auth, name, forbidden)
    result = web.handle(request(
        path, method=method, url="https://vault.synthetic.example/api/tasks/" + path,
        cookie="synthetic-cookie", csrf="synthetic-csrf", origin=auth.config.origin,
    ), path)
    assert result.status_code == status
    assert data(result)["error"] == ("method_not_allowed" if status == 405 else "secure_origin_required")
    assert "Location" not in result.headers and "Set-Cookie" not in result.headers
    forbidden.assert_not_called()
    service_factory.assert_not_called()
    assert identity[3] == []


@pytest.mark.parametrize("path", ["assets/tasks.js", "assets/tasks.css", "assets/dashboard.js"])
def test_legacy_static_assets_are_not_redirected_or_authenticated(redirect_web, path):
    web, _, auth_factory, service_factory = redirect_web
    result = web.handle(request(path, url="https://vault.synthetic.example/api/tasks/" + path), path)
    assert result.status_code == 200 and "Location" not in result.headers
    auth_factory.assert_not_called()
    service_factory.assert_not_called()


@pytest.mark.parametrize("path", ["", "auth/login"])
def test_bad_redirect_configuration_has_an_explicit_content_free_error(redirect_web, path, caplog):
    web, _, auth_factory, service_factory = redirect_web
    web.env["MINDME_WEB_REDIRECT_ORIGINS"] = "https://synthetic-secret@vault.synthetic.example"
    result = web.handle(request(path, url="https://vault.synthetic.example/api/tasks/" + path), path)
    assert result.status_code == 503 and data(result)["error"] == "auth_configuration_invalid"
    assert "Location" not in result.headers
    assert "synthetic-secret" not in result.get_body().decode() + caplog.text
    auth_factory.assert_not_called()
    service_factory.assert_not_called()


@pytest.mark.parametrize("origin", ["https://mindme.synthetic.example", "http://legacy.synthetic.example"])
def test_unset_redirect_configuration_preserves_the_public_static_shell(origin):
    forbidden = Mock(side_effect=AssertionError("Static shell must not load auth or services without the opt-in"))
    web = TaskWeb(env={"MINDME_WEB_ENABLED": "true"}, auth=forbidden, service=forbidden)
    result = web.handle(request("", url=origin + "/api/tasks"))
    assert result.status_code == 200 and "Location" not in result.headers
    forbidden.assert_not_called()


@pytest.mark.parametrize("wrong_owner", [False, True])
def test_new_canonical_host_keeps_real_owner_flow_host_only_cookies_and_exact_csrf(
    redirect_web, identity, wrong_owner,
):
    web, auth, _, service_factory = redirect_web
    origin = auth.config.origin
    shell = web.handle(request("", url=origin + "/api/tasks"))
    assert shell.status_code == 200 and "Set-Cookie" not in shell.headers
    login = web.handle(request("auth/login", url=origin + "/api/tasks/auth/login"), "auth/login")
    assert login.status_code == 303
    query = parse_qs(urlsplit(login.headers["Location"]).query)
    assert query["redirect_uri"] == [origin + "/api/tasks/auth/callback"]
    assert query["response_mode"] == ["form_post"] and query["code_challenge_method"] == ["S256"]
    claims = identity[2]
    claims["nonce"] = query["nonce"][0]
    if wrong_owner:
        claims["oid"] = "44444444-4444-4444-8444-444444444444"
    callback = web.handle(func.HttpRequest(
        method="POST", url=origin + "/api/tasks/auth/callback",
        headers={"Cookie": cookie_pair(login.headers["Set-Cookie"], FLOW_COOKIE),
                 "Content-Type": "application/x-www-form-urlencoded"},
        body=urlencode({"state": query["state"][0], "code": "synthetic-code"}).encode(),
    ), "auth/callback")
    if wrong_owner:
        assert callback.status_code == 403 and data(callback)["error"] == "owner_not_authorized"
        assert "Set-Cookie" not in callback.headers and "Location" not in callback.headers
    else:
        assert callback.status_code == 303 and callback.headers["Location"] == origin + "/api/tasks"
        for result, name, same_site in ((login, FLOW_COOKIE, "None"), (callback, SESSION_COOKIE, "Lax")):
            cookie = result.headers["Set-Cookie"]
            assert cookie.startswith(name + "=") and name.startswith("__Host-")
            assert "Secure" in cookie and "HttpOnly" in cookie and "Path=/" in cookie
            assert "Domain=" not in cookie and f"SameSite={same_site}" in cookie
        cookie = cookie_pair(callback.headers["Set-Cookie"], SESSION_COOKIE)
        session = web.handle(request("api/session", url=origin + "/api/tasks/api/session", cookie=cookie), "api/session")
        assert session.status_code == 200 and set(data(session)) == {"authenticated", "csrf", "expires"}
        csrf = data(session)["csrf"]
        for request_origin, token in (("https://vault.synthetic.example", csrf), (origin, "forged")):
            result = web.handle(request(
                "auth/logout", {}, url=origin + "/api/tasks/auth/logout",
                cookie=cookie, csrf=token, origin=request_origin,
            ), "auth/logout")
            assert result.status_code == 403 and data(result)["error"] == "csrf_rejected"
        logout = web.handle(request(
            "auth/logout", {}, url=origin + "/api/tasks/auth/logout", cookie=cookie, csrf=csrf, origin=origin,
        ), "auth/logout")
        assert logout.status_code == 200
    service_factory.assert_not_called()


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
        "owner_next_action": "Review it.", "source_quote": "One topic only.",
    })
    model.chat.completions.create.return_value.choices = [
        SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=content, refusal=None)),
    ]
    monkeypatch.setenv("MINDME_BRIEFING_MODEL", "existing-small-model")
    monkeypatch.setattr(fa, "_http_client", lambda: http)
    monkeypatch.setattr(fa, "_foundry", lambda: (None, model))
    fa._generate_task_preparation({"source": {"text": "One topic only."}, "scope": "Outline only."})
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
        fa._generate_task_preparation({"source": {"text": "One topic only."}, "scope": "Outline only."})
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
        "owner_next_action": "Review it.", "source_quote": "One topic only.",
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
                assert fa._generate_task_preparation({"source": {"text": "One topic only."}, "scope": "Outline only."}) == output
            else:
                with pytest.raises(TaskError, match="task_preparation_invalid"):
                    fa._generate_task_preparation({"source": {"text": "One topic only."}, "scope": "Outline only."})
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


def test_preparation_schema_offers_exact_canonical_quotes_not_free_text(monkeypatch):
    from types import SimpleNamespace

    source = '# Synthetic\n\nA "quoted" constraint stays exact.\r\nKeep the \\ character unchanged.\r\n'
    output = {
        "summary": "A synthetic draft.", "steps": [], "uncertainties": [],
        "owner_next_action": "Review the draft.", "source_quote": 'A "quoted" constraint stays exact.',
    }
    model = Mock()
    model.with_options.return_value = model
    model.chat.completions.create.return_value.choices = [
        SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(output), refusal=None)),
    ]
    monkeypatch.setenv("MINDME_BRIEFING_MODEL", "existing-small-model")
    monkeypatch.setattr(fa, "_foundry", lambda: (None, model))
    monkeypatch.setattr(fa, "_http_client", Mock())
    assert fa._generate_task_preparation({"source": {"text": source}, "scope": "Draft only."}) == output
    schema = model.chat.completions.create.call_args.kwargs["response_format"]["json_schema"]["schema"]
    quotes = schema["properties"]["source_quote"]["enum"]
    assert output["source_quote"] in quotes
    assert "Keep the \\ character unchanged." in quotes
    assert 1 <= len(quotes) <= 12
    assert all(12 <= len(quote) <= 500 and quote in source for quote in quotes)
    assert sum(map(len, quotes)) <= 1600


def test_preparation_without_citable_source_fails_before_model_client(monkeypatch):
    from task_service import TaskError

    model = Mock(side_effect=AssertionError("No model client without canonical evidence"))
    monkeypatch.setenv("MINDME_BRIEFING_MODEL", "existing-small-model")
    monkeypatch.setattr(fa, "_foundry", model)
    with pytest.raises(TaskError, match="task_preparation_evidence_invalid"):
        fa._generate_task_preparation({"source": {"text": "# Header only\nTiny."}, "scope": "Draft only."})
    model.assert_not_called()
