"""Public HTTP URL/method/auth contracts; no service or webhook mutations."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import azure.functions as func
import pytest

import function_app as fa

ROOT_TEMPLATE = "{ignored:maxlength(0)?}"
EXPECTED_HTTP = {
    "task_site_root": ("/", ("GET",), func.AuthLevel.ANONYMOUS),
    "telegram_webhook": ("/api/telegram_webhook", ("POST",), func.AuthLevel.ANONYMOUS),
    "health": ("/api/health", ("GET",), func.AuthLevel.ANONYMOUS),
    "tool_briefing_context": ("/api/tools/briefing_context", ("POST",), func.AuthLevel.FUNCTION),
    "tool_weather": ("/api/tools/weather", ("GET",), func.AuthLevel.FUNCTION),
    "tool_vault_recent": ("/api/tools/vault_recent", ("GET",), func.AuthLevel.FUNCTION),
    "tool_vault_read": ("/api/tools/vault_read", ("GET",), func.AuthLevel.FUNCTION),
    "task_web_root": ("/api/tasks", ("GET",), func.AuthLevel.ANONYMOUS),
    "task_web_route": ("/api/tasks/{*path}", ("GET", "POST"), func.AuthLevel.ANONYMOUS),
}


@pytest.fixture
def http_bindings(monkeypatch):
    monkeypatch.setattr(fa.app, "functions_bindings", {}, raising=False)
    bindings = {}
    for function in fa.app.get_functions():
        for binding in function.get_bindings():
            if binding.type == "httpTrigger":
                value = binding.get_dict_repr()
                value["methods"] = [method.value for method in value["methods"]]
                bindings[function.get_function_name()] = value
    return bindings


def test_emitted_http_bindings_preserve_every_existing_public_url_method_and_auth(http_bindings):
    settings = json.loads((Path(__file__).parents[1] / "host.json").read_text(encoding="utf-8"))
    prefix = settings["extensions"]["http"]["routePrefix"]
    assert prefix == ""
    actual = {}
    for name, binding in http_bindings.items():
        route = binding["route"]
        if name == "task_site_root":
            assert route == ROOT_TEMPLATE
            route = ""
        else:
            assert route.startswith("api/")
        public_path = "/" + "/".join(part for part in (prefix, route) if part)
        actual[name] = public_path, tuple(binding["methods"]), binding["authLevel"]
    assert actual == EXPECTED_HTTP


def test_root_matcher_is_optional_zero_length_not_a_catch_all_or_function_name_fallback(http_bindings):
    binding = http_bindings["task_site_root"]
    assert binding["route"] == ROOT_TEMPLATE
    assert binding["methods"] == ["GET"]
    assert binding["route"] not in {"", "/", "{*path}"}


@pytest.mark.parametrize("url", [
    "https://synthetic.example/",
    "https://synthetic.example/?next=https://untrusted.example/",
])
def test_root_redirect_is_relative_fixed_and_does_not_read_auth_settings_or_set_cookies(monkeypatch, url):
    unavailable = Mock(side_effect=AssertionError("Root redirect must not consult services or configuration"))
    monkeypatch.setattr(fa, "_task_web", unavailable)
    monkeypatch.setattr(fa, "_task_auth", unavailable)
    monkeypatch.setattr(fa, "os", None)
    response = fa.task_site_root(func.HttpRequest(method="GET", url=url, body=b""))
    assert response.status_code == 302
    assert response.headers["Location"] == "/api/tasks"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.get_body() == b""
    assert "Set-Cookie" not in response.headers
    unavailable.assert_not_called()


@pytest.mark.parametrize(("method", "suffix"), [
    ("GET", "auth/login"), ("POST", "auth/callback"), ("POST", "auth/logout"),
    ("GET", "api/session"), ("GET", "api/overview"), ("POST", "api/task"),
    ("POST", "api/decide"), ("GET", "assets/tasks.js"), ("GET", "assets/tasks.css"),
])
@pytest.mark.asyncio
async def test_task_wildcard_preserves_auth_callback_and_api_subroutes(monkeypatch, method, suffix):
    expected = func.HttpResponse("synthetic")
    handler = Mock(return_value=expected)
    monkeypatch.setattr(fa, "_task_web", lambda: Mock(handle=handler))
    req = func.HttpRequest(
        method=method, url=f"https://synthetic.example/api/tasks/{suffix}",
        body=b"", route_params={"path": suffix},
    )
    assert await fa.task_web_route(req) is expected
    handler.assert_called_once_with(req, suffix)
