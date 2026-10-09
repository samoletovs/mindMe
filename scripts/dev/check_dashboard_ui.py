"""Offline browser regression with synthetic fixtures, never a production identity.

Requires an installed Python Playwright and Chromium. Run with --artifacts pointing
outside the repository. A loopback-only helper is shut down before this exits.
"""

from __future__ import annotations

import argparse
import copy
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "harness" / "web"
QUOTE = "The synthetic pilot changed both the model and the procedure."
STATEMENT = "This synthetic comparison does not isolate the model change."
NOTE = {
    "id": "1" * 64, "revision": "a" * 40, "path": "wiki/sources/synthetic-pilot.md",
    "title": "Synthetic pilot", "kind": "wiki", "source_dates": {"captured": "2026-10-08"},
    "text": "# Synthetic pilot\n\n" + QUOTE, "bounded": False,
}
REVIEW = {
    "id": "2" * 64, "revision": "b" * 40, "path": "reviews/vault-evolve/2026-10-08/review.json",
    "title": "Daily knowledge review", "kind": "daily_review", "as_of": "2026-10-08",
    "findings": [{
        "id": "F1", "kind": "evidence", "basis": "observed", "statement": STATEMENT,
        "next_step": "Plan one synthetic controlled comparison.",
        "evidence": [{**NOTE, "quote": QUOTE}],
    }],
    "feedback": {"available": True, "expires_on": "2026-10-22", "findings": {"F1": {"version": "c" * 64, "value": None}}},
}
TASK = {
    "path": "tasks/synthetic-outline.md", "revision": "d" * 40, "title": "Finish the synthetic outline",
    "stage": "ready", "area": "learning", "project": "", "next_action": "Check one testable claim.",
    "outcome": "A narrow comparison plan.", "done_when": "Read the plan and check the agreed topic.",
    "deadline": "2026-10-10", "review_on": None, "focus_on": "2026-10-08",
    "attention_eligible": True, "attention_reasons": ["deadline_soon", "focus_today"],
    "definition": {"complete": True, "missing": []}, "source_status": "current",
    "text": "Synthetic task only.", "execution": "assisted", "waiting_for": "",
}


class StaticHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        name = {
            "/api/tasks": "index.html", "/api/tasks/assets/tasks.css": "tasks.css",
            "/api/tasks/assets/tasks.js": "tasks.js", "/api/tasks/assets/dashboard.js": "dashboard.js",
        }.get(self.path.split("?", 1)[0])
        if name is None:
            self.send_error(404)
            return
        body = (WEB / name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html" if name.endswith(".html") else "text/css" if name.endswith(".css") else "text/javascript")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        pass


def check(artifacts: Path) -> None:
    artifacts.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), StaticHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    checks = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                for width, height in [(1280, 900), (390, 844)]:
                    page = browser.new_page(viewport={"width": width, "height": height}, reduced_motion="reduce")
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    fixture = {"history": [], "writes": [], "denied": False, "stale": False}
                    review = copy.deepcopy(REVIEW)

                    def route(request_route) -> None:
                        req = request_route.request
                        assert req.url.startswith(base + "/"), "Unexpected external asset or request"
                        if "/api/tasks/api/" not in req.url:
                            request_route.continue_()
                            return
                        operation = req.url.split("/api/tasks/api/")[1].split("?")[0]
                        payload = req.post_data_json if req.method == "POST" else None
                        status = 200
                        if fixture["denied"]:
                            status, body = 401, {"error": "authentication_required", "message": "Synthetic session expired."}
                        elif operation == "session":
                            body = {"authenticated": True, "csrf": "synthetic-csrf", "expires": int(time.time()) + 3600}
                        elif operation == "overview":
                            body = {
                                "items": [TASK], "history": fixture["history"], "areas": ["learning"], "projects": [],
                                "stages": ["ready"], "next_offset": None, "canonical_revision": "e" * 40,
                                "attention_complete": False, "history_next_offset": None, "project_next_offset": None,
                                "attention": {"calendar_date": "2026-10-08", "timezone": "UTC", "deadline_horizon_days": 7},
                                "standing": {"enabled": False, "sources": {}}, "active_projects": {},
                                "limits": {"daily": 3}, "budget": {}, "errors": [],
                            }
                        elif operation == "task":
                            body = TASK
                        elif operation == "dashboard/today":
                            body = {
                                "items": [{**NOTE, "change": "new"}], "first_visit": False,
                                "focus": {
                                    "draft_present": True, "status": "available",
                                    "items": [{"text": "Practise one synthetic controlled comparison.",
                                               "status": "expired", "starts_on": "2026-09-01", "ends_on": "2026-09-30"}],
                                },
                                "since": "2026-10-07T12:00:00+00:00", "observed_at": "2026-10-08T12:00:00+00:00",
                                "partial": True, "issues": ["dashboard_withheld"], "next_offset": None,
                                "visit_token": "synthetic-observation", "canonical_revision": "e" * 40,
                            }
                        elif operation == "dashboard/inbox":
                            body = {"items": [review], "issues": [], "partial": False, "next_offset": None, "canonical_revision": "e" * 40}
                        elif operation == "dashboard/read":
                            if fixture["stale"]:
                                status, body = 409, {"message": "Synthetic source changed. Refresh the inbox."}
                            else:
                                body = review if payload["id"] == REVIEW["id"] else NOTE
                        elif operation == "dashboard/feedback":
                            fixture["writes"].append(operation)
                            assert payload["value"] == "useful" and payload["finding"] == "F1"
                            body = {
                                "value": {"text": "Useful", "recorded_on": "2026-10-08", "review_on": None},
                                "version": "f" * 64, "message": "Synthetic feedback saved. No task was approved.",
                            }
                        elif operation == "dashboard/capture":
                            fixture["writes"].append(operation)
                            assert payload["id"] == REVIEW["id"] and payload["finding"] == "F1"
                            body = {
                                "id": "3" * 24, "kind": "capture_task", "task_workspace": True,
                                "status": "pending", "source_status": "current", "text": "Review the synthetic task.",
                                "approval_digest": "4" * 64, "source_path": REVIEW["path"], "source_revision": REVIEW["revision"],
                                "action": {"kind": "create_task", "text": payload["text"],
                                           "definition": {**payload["definition"], "context": NOTE["path"] + ": " + QUOTE}},
                            }
                            fixture["history"] = [body]
                        elif operation == "decide":
                            fixture["writes"].append(operation)
                            assert payload["proposal_id"] == "3" * 24 and payload["decision"] == "approve"
                            body = {**fixture["history"][0], "status": "submitted"}
                            fixture["history"] = [body]
                        elif operation == "dashboard/visit":
                            fixture["writes"].append(operation)
                            body = {"recorded": True}
                        else:
                            raise AssertionError("Unexpected synthetic operation: " + operation)
                        request_route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

                    page.route("**/*", route)
                    page.goto(base + "/api/tasks", wait_until="networkidle")
                    page.evaluate("""() => {
                      const banner = document.createElement('p');
                      banner.textContent = 'Synthetic UI fixture - no production data or identity';
                      banner.style.cssText = 'margin:0;padding:8px 16px;background:#e5f1eb;color:#20352f';
                      document.body.prepend(banner);
                    }""")
                    page.get_by_text("Expired window", exact=True).wait_for()
                    assert page.get_by_text("The North Star is a draft, not approved focus.", exact=True).is_visible()
                    assert page.locator("html").evaluate("(node) => node.scrollWidth <= innerWidth") is True
                    page.screenshot(path=str(artifacts / f"today-{width}.png"), full_page=True)
                    page.get_by_role("button", name="Knowledge", exact=True).click()
                    page.locator("#knowledge-list .source-row").click()
                    page.get_by_role("blockquote").wait_for()
                    assert page.get_by_role("blockquote").inner_text() == QUOTE
                    assert page.locator("html").evaluate("(node) => node.scrollWidth <= innerWidth") is True
                    page.screenshot(path=str(artifacts / f"knowledge-{width}.png"), full_page=True)
                    page.get_by_role("button", name="Useful", exact=True).click()
                    page.get_by_text("Saved: Useful.", exact=False).wait_for()
                    assert fixture["writes"] == ["dashboard/feedback"]
                    page.get_by_role("button", name="Read source: Synthetic pilot").click()
                    page.locator(".source-prose").get_by_text(QUOTE, exact=True).wait_for()
                    page.get_by_role("button", name="Back to inbox", exact=True).click()
                    page.locator("#knowledge-list .source-row").click()
                    page.get_by_role("button", name="Make a task", exact=True).click()
                    page.get_by_role("button", name="Review capture", exact=True).click()
                    page.get_by_role("button", name="Approve this exact action", exact=True).wait_for()
                    assert "decide" not in fixture["writes"]
                    assert QUOTE in page.locator("#proposal-preview").inner_text()
                    page.get_by_role("button", name="Approve this exact action", exact=True).click()
                    page.get_by_text("Publication pending", exact=True).first.wait_for()
                    assert fixture["writes"].count("dashboard/capture") == 1
                    assert fixture["writes"].count("decide") == 1
                    page.get_by_role("button", name="Return to workspace", exact=True).click()
                    page.get_by_role("button", name="Tasks", exact=False).filter(has=page.locator("#tasks-count")).click()
                    page.locator("#task-list .task-row").click()
                    page.locator("#inspector .detail-title").wait_for()
                    page.screenshot(path=str(artifacts / f"tasks-{width}.png"), full_page=True)
                    assert page.locator("html").evaluate("(node) => node.scrollWidth <= innerWidth") is True
                    fixture["stale"] = True
                    page.get_by_role("button", name="Knowledge", exact=True).click()
                    page.get_by_role("button", name="Back to inbox", exact=True).click()
                    page.locator("#knowledge-list .source-row").click()
                    page.get_by_text("Synthetic source changed. Refresh the inbox.", exact=True).wait_for()
                    assert page.locator("#knowledge-reader").get_by_role("button", name="Make a task").count() == 0
                    fixture["denied"] = True
                    page.get_by_role("button", name="Refresh", exact=True).click()
                    page.get_by_role("link", name="Sign in with Microsoft", exact=True).wait_for()
                    assert QUOTE not in page.locator("body").inner_text()
                    assert page.evaluate("localStorage.length + sessionStorage.length") == 0
                    assert not errors, errors
                    checks.append(f"{width}x{height}: Today, evidence in one interaction, source in two, feedback-only, preview/confirm, task compatibility, stale denial, session clearing, no overflow")
                    page.close()
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    print("\n".join(checks))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", required=True, type=Path)
    check(parser.parse_args().artifacts)
