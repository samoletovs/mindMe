"""Existing-host task shell and owner-only APIs; no framework or public data endpoint."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import azure.functions as func
import httpx
from azure.core.exceptions import AzureError
from openai import OpenAIError

from briefing_actions import ActionError
from briefing_loop import LoopError
from briefing_plan import PlanError
from briefing_sources import SourceError
from briefing_state import StateError
from execution_budget import BudgetExceeded, execution_budget
from knowledge_plan import KnowledgeError
from task_auth import AuthError, TaskAuth
from task_service import TaskError, TaskService

log = logging.getLogger(__name__)
ASSETS = Path(__file__).parent / "web"
SECURITY_HEADERS = {
    "Cache-Control": "no-store, private", "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY", "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self'; font-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    ),
}
ERROR_TEXT = {
    "authentication_required": "Sign in with the configured personal Microsoft account.",
    "auth_unconfigured": "Owner sign-in is not configured. No task data is available.",
    "auth_configuration_invalid": "Owner sign-in configuration needs attention. Access is disabled.",
    "owner_not_authorized": "This Microsoft account is not the configured personal owner.",
    "secure_origin_required": "Use the configured HTTPS task address.",
    "csrf_rejected": "This action could not be verified. Reload Tasks before trying again.",
    "task_source_changed": "The saved task changed. Reload it and review a fresh action.",
    "task_source_not_canonical": "The task is not yet available on the canonical branch.",
    "task_definition_incomplete": "Resolve the definition and waiting gaps before selecting this stage.",
    "task_not_selected": "Select a fully defined, unblocked task as Ready before preparing work.",
    "task_review_capacity": "Review the existing preparation results before starting more optional work.",
    "task_budget_exhausted": "The configured preparation limit has been reached. No new work was started.",
    "task_clarification_limit": "Three answers is the limit for this file version. Keep unresolved work in Clarify.",
    "task_clarification_stale": "That question has changed. Reload clarification and answer the question now shown; no answer was applied.",
    "task_request_conflict": "This request ID already describes a different action. Reload the saved receipt.",
    "task_closure_requires_verification": "Check the done condition and record the actual result and evidence first.",
    "task_closure_date_invalid": "The verification date cannot be after the owner's current calendar date.",
    "task_timezone_not_configured": "Configure the owner's IANA time zone before using Tasks. No calendar date was assumed.",
    "task_timezone_invalid": "The configured task time zone is invalid or unavailable. Use a valid IANA time zone.",
    "task_clock_invalid": "The task calendar is unavailable. No calendar date was assumed.",
    "task_result_acknowledgment_required": "This result is unavailable. Explicitly acknowledge its content-free notice to clear the review queue; no private content or task outcome is verified.",
    "task_result_available": "This result is available. Review its current output instead of acknowledging an unavailable-result notice.",
}


def response(data: object, status: int = 200, *, headers: dict[str, str] | None = None) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(data, ensure_ascii=False), status_code=status, mimetype="application/json",
        headers={**SECURITY_HEADERS, **(headers or {})},
    )


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise TaskError("task_request_invalid")
        result[key] = value
    return result


def _body(req: func.HttpRequest) -> dict[str, Any]:
    raw = req.get_body()
    if len(raw) > 16384 or req.headers.get("Content-Type", "").split(";")[0].lower() != "application/json":
        raise TaskError("task_request_invalid")
    try:
        value = json.loads(raw, object_pairs_hook=_unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise TaskError("task_request_invalid") from None
    if not isinstance(value, dict):
        raise TaskError("task_request_invalid")
    return value


class TaskWeb:
    def __init__(
        self, *, env: Mapping[str, str], auth: Callable[[], TaskAuth], service: Callable[[], TaskService],
    ) -> None:
        self.env, self.auth_factory, self.service_factory = env, auth, service

    def handle(self, req: func.HttpRequest, path: str = "") -> func.HttpResponse:
        if self.env.get("MINDME_WEB_ENABLED", "").lower() != "true":
            return response({"error": "tasks_disabled", "message": "The personal task web app is not enabled."}, 404)
        try:
            with execution_budget(120):
                return self._handle(req, path.strip("/"))
        except AuthError as error:
            return response({"error": str(error), "message": ERROR_TEXT.get(str(error), "Sign-in could not be verified. Try signing in again.")}, error.status)
        except (TaskError, PlanError, KnowledgeError) as error:
            code = str(error)
            status = (
                503 if code in {"task_timezone_not_configured", "task_timezone_invalid", "task_clock_invalid"}
                else 409 if code in {"task_source_changed", "task_request_conflict", "task_source_not_canonical"}
                else 422
            )
            return response({"error": code if code in ERROR_TEXT else "task_request_rejected",
                             "message": ERROR_TEXT.get(code, "This action was not accepted. Check its fields and scope; no success is claimed.")}, status)
        except (StateError, SourceError, ActionError, LoopError, AzureError, OpenAIError, httpx.HTTPError, BudgetExceeded) as error:
            log.warning("task web unavailable error=%s", type(error).__name__)
            return response({
                "error": "task_service_unavailable",
                "message": "The task service could not confirm this request. Reload its existing receipt; do not submit a replacement.",
            }, 503)

    def _handle(self, req: func.HttpRequest, path: str) -> func.HttpResponse:
        method = req.method.upper()
        if path in {"", "assets/tasks.css", "assets/tasks.js"}:
            if method != "GET":
                return response({"error": "method_not_allowed"}, 405)
            filename, mime = {
                "": ("index.html", "text/html"),
                "assets/tasks.css": ("tasks.css", "text/css"),
                "assets/tasks.js": ("tasks.js", "text/javascript"),
            }[path]
            return func.HttpResponse((ASSETS / filename).read_bytes(), mimetype=mime, headers=SECURITY_HEADERS)
        auth = self.auth_factory()
        auth.check_transport(req.url)
        if path == "auth/login":
            if method != "GET":
                return response({"error": "method_not_allowed"}, 405)
            uri, cookie = auth.login()
            return func.HttpResponse("", status_code=303, headers={**SECURITY_HEADERS, "Location": uri, "Set-Cookie": cookie})
        if path == "auth/callback":
            if method != "POST":
                return response({"error": "method_not_allowed"}, 405)
            if (
                len(req.get_body()) > 20000
                or req.headers.get("Content-Type", "").split(";")[0] != "application/x-www-form-urlencoded"
            ):
                raise AuthError("auth_flow_invalid")
            try:
                form = _unique(parse_qsl(req.get_body().decode("utf-8"), keep_blank_values=True, max_num_fields=10))
            except (ValueError, UnicodeError):
                raise AuthError("auth_flow_invalid") from None
            cookie = auth.callback(req.headers.get("Cookie", ""), form)
            return func.HttpResponse("", status_code=303, headers={
                **SECURITY_HEADERS, "Location": auth.config.origin + "/api/tasks", "Set-Cookie": cookie,
            })
        session = auth.authenticate(req.headers.get("Cookie", ""))
        if path == "api/session" and method == "GET":
            return response({"authenticated": True, "csrf": session["csrf"], "expires": session["exp"]})
        if method == "POST":
            auth.csrf(session, origin=req.headers.get("Origin", ""), token=req.headers.get("X-MindMe-CSRF", ""))
            if path == "auth/logout":
                return response({"signed_out": True}, headers={"Set-Cookie": auth.logout(session)})
        if self.env.get("MINDME_TASKS_ENABLED", "").lower() != "true":
            return response({"error": "tasks_not_configured", "message": "Task integration is not enabled. No task data was loaded."}, 503)
        service = self.service_factory()
        today = datetime.now(timezone.utc).date()
        if method == "GET":
            if path == "api/overview":
                try:
                    offset = int(req.params.get("offset", "0"))
                except ValueError:
                    raise TaskError("task_page_invalid") from None
                return response(service.overview(offset, today))
            if path in {"api/projects", "api/history"}:
                try:
                    offset = int(req.params.get("offset", "0"))
                except ValueError:
                    raise TaskError("task_page_invalid") from None
                return response(service.history(offset, today) if path == "api/history" else service.repository.projects(offset))
        if method != "POST":
            return response({"error": "method_not_allowed"}, 405)
        payload = _body(req)
        if path == "api/task" and set(payload) == {"path"}:
            return response(service._source(payload["path"]))
        operations = {
            "api/capture": service.capture, "api/refine": service.refine, "api/change": service.change,
            "api/prepare": service.prepare, "api/close": service.close, "api/decide": service.decide,
        }
        if path in operations:
            return response(operations[path](payload, today))
        if path == "api/projects":
            return response(service.select_projects(payload))
        if path == "api/standing":
            return response(service.standing(payload))
        if path == "api/review-result":
            return response(service.review_result(payload))
        if path == "api/reconcile" and set(payload) == {"proposal_id"}:
            return response(service.reconcile(payload["proposal_id"], today))
        if path == "api/clarify-proposal" and set(payload) == {"clarification_id"}:
            proposal = service.clarification_proposal(payload["clarification_id"], today)
            return response(proposal if proposal else {"proposal": None, "message": "There are no clarification changes to approve."})
        if path == "api/clarify" and set(payload) in (
            {"path", "revision"}, {"path", "revision", "answer", "request_id"},
            {"path", "revision", "answer", "request_id", "question_token"},
        ):
            return response(service.clarification(
                payload["path"], payload["revision"], today, answer=payload.get("answer"),
                request_id=payload.get("request_id"),
                question_token=payload.get("question_token"),
            ))
        return response({"error": "route_not_found"}, 404)
