"""Synthetic, offline UI regressions for the AI-first owner workspace."""

from __future__ import annotations

import os
import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from test_closure_scan_ui import run_ui_test


WEB = Path(__file__).parents[1] / "web"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required")
def test_mock_is_local_only_and_never_falls_through_to_live_api():
    run_ui_test(r"""
(async () => {
  location = { hostname: "vault.synthetic.example", search: "?mock=1" };
  URLSearchParams = URL.searchParams?.constructor || requireParams;
  assert.equal(isMockMode(), false);
  location.hostname = "localhost";
  location.search = "?mock=0";
  assert.equal(isMockMode(), false);
  location.search = "?mock=1";
  assert.equal(isMockMode(), true);
  setTimeout = (fn) => { fn(); return 1; };
  const response = await mockApi("overview?offset=0");
  assert.ok(response.items.every((item) => item.path.startsWith("tasks/synthetic-")));
  assert.ok(response.items.some((item) => /[а-я]/i.test(item.title)));
  await assert.rejects(mockApi("unsupported", {}), /No live API was called/);
})()
""".replace(
        "URLSearchParams = URL.searchParams?.constructor || requireParams;",
        'URLSearchParams = class { constructor(value) { this.value = value; } get() { return this.value === "?mock=1" ? "1" : "0"; } };',
    ))


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required")
def test_applied_status_never_equates_submission_or_missing_proof_with_saved():
    run_ui_test(r"""
assert.match(appliedCardStatus({ status: "submitted" }), /pending/);
assert.match(appliedCardStatus({ status: "executing" }), /pending/);
assert.match(appliedCardStatus({}), /check Activity/);
assert.match(appliedCardStatus({ status: "completed" }), /check Activity/);
assert.match(appliedCardStatus({ status: "completed", publication_status: "merged", saved: true }), /check Activity/);
assert.match(appliedCardStatus({ status: "completed", publication_status: "merged", saved: true, source_status: "current" }), /canonical/);
assert.match(appliedCardStatus({ status: "completed", publication_status: "merged", saved: false }), /check Activity/);
assert.match(appliedCardStatus({ status: "completed", publication_status: "merged", saved: true, source_status: "removed" }), /check Activity/);
assert.match(appliedCardStatus({ status: "uncertain" }), /check Activity/);
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required")
def test_ai_requests_bind_actual_task_revision_and_use_exact_decision_digest():
    run_ui_test(r"""
(async () => {
  assist.available = true;
  state.csrf = "synthetic";
  const path = "tasks/synthetic.md";
  state.items = [{ path, revision: "a".repeat(40) }];
  state.selected = { path, revision: "b".repeat(40) };
  assert.deepEqual(taskReference(path), { path, revision: "b".repeat(40) });
  assert.equal(taskReference("global"), null);
  assert.equal(taskReference("tasks/missing.md"), null);
  let call;
  assistMutation = async (operation, payload, key) => { call = { operation, payload, key }; };
  await sendChat(path, "Make the next step concrete.");
  assert.equal(call.operation, "assist/chat");
  assert.equal(call.payload.task.revision, "b".repeat(40));
  assert.equal(call.payload.voice, false);
  assert.equal(call.payload.language, "en");
  assert.equal(threadFor(path).messages.length, 1);
  mutate = async (operation, payload) => { call = { operation, payload }; };
  const proposal = { kind: "proposal", proposal_id: "c".repeat(24), action_digest: "d".repeat(64) };
  threadFor(path).messages.push({ role: "assistant", text: "Review", cards: [proposal] });
  await sendChat(path, "да", true);
  assert.equal(call.operation, "decide");
  assert.equal(call.payload.approval_digest, proposal.action_digest);
  assert.equal(call.payload.decision, "approve");
  state.unknown = { operation: "assist/chat" };
  call = null;
  await sendChat(path, "A replacement instruction");
  assert.equal(call, null);
})()
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required")
def test_board_uses_deterministic_change_and_never_optimistically_moves():
    run_ui_test(r"""
const task = { path: "tasks/synthetic.md", revision: "a".repeat(40), stage: "ready" };
assist.available = true;
let call;
assistMutation = (operation, payload, key) => { call = { operation, payload, key }; };
moveTask(task, "doing");
assert.equal(call.operation, "assist/change");
assert.deepEqual(call.payload.change, { stage: "doing" });
assert.equal(call.payload.revision, task.revision);
assert.equal(task.stage, "ready");
assist.available = false;
mutate = (operation, payload) => { call = { operation, payload }; };
moveTask(task, "verify");
assert.equal(call.operation, "refine");
assert.deepEqual(call.payload.changes, { stage: "verify" });
assert.equal(task.stage, "ready");
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required")
def test_receipt_poll_uses_existing_id_and_never_continues_closure():
    run_ui_test(r"""
(async () => {
  state.csrf = "synthetic";
  assist.available = false;
  const card = { kind: "applied", receipt_id: "b".repeat(24), status: "submitted", saved: false, undo_available: true };
  const scheduled = [];
  scheduleReceiptPoll = (poll) => scheduled.push(poll);
  watchReceipt("global", card);
  assert.equal(card.undo_available, false);
  const poll = scheduled[0];
  assert.match(poll.requestId, /^[a-f0-9]{32}$/);
  const refreshed = (fields) => ({ thread_id: "t", reply: "", language: "en", suggestions: [], usage: {},
    cards: [{ kind: "applied", receipt_id: card.receipt_id, title: "Synthetic", summary: "", diff: [], ...fields }] });
  const calls = [];
  api = async (operation, payload) => {
    calls.push({ operation, payload });
    return refreshed({ status: "executing", publication_status: null, source_status: "current", saved: false, undo_available: true });
  };
  loadOverview = async () => {};
  showToast = () => {};
  await pollReceipt(poll);
  assert.equal(card.saved, false);
  assert.equal(isCardSaved(card), false);
  assert.equal(scheduled.length, 2);
  api = async (operation, payload) => {
    calls.push({ operation, payload });
    return refreshed({ status: "completed", publication_status: "merged", source_status: "current", saved: true, undo_available: true });
  };
  await pollReceipt(poll);
  assert.deepEqual(calls.map((c) => c.operation), ["assist/receipt", "assist/receipt"]);
  assert.ok(calls.every((c) => c.payload.request_id === poll.requestId && c.payload.receipt_id === "b".repeat(24)));
  assert.equal(isCardSaved(card), true);
  assert.equal(card.undo_available, true);
  api = async () => refreshed({ status: "completed", publication_status: "merged", source_status: "current", saved: true });
  await pollReceipt(poll);
  assert.equal(card.undo_available, false, "Undo comes only from the refreshed card");
  api = async () => ({ cards: [{ kind: "applied", receipt_id: "e".repeat(24), status: "completed" }] });
  await pollReceipt(poll);
  assert.equal(card.status, "uncertain");
  api = async () => refreshed({ status: "uncertain", source_status: "removed", saved: false, undo_available: true,
    result: { phase: "closure_scan", closure_continuation: "c".repeat(32) } });
  card.diff = [{ field: "title", before: "Old synthetic text", after: "Changed synthetic text" }];
  card.title = "Old synthetic title";
  await pollReceipt(poll);
  assert.equal(card.saved, false);
  assert.equal(card.diff.length, 0);
  assert.equal(card.undo_available, false);
  assert.match(card.title, /check Activity/);
  assert.equal(scheduled.length, 2, "uncertain receipts never continue polling or closure");

  api = async () => refreshed({ status: "completed", publication_status: "merged", source_status: "current", saved: true });
  loadOverview = async () => { state.sessionGeneration += 1; };
  let toastAfterExpiry = false;
  showToast = () => { toastAfterExpiry = true; };
  await pollReceipt(poll);
  assert.equal(toastAfterExpiry, false);
})()
""")


@pytest.fixture(scope="module")
def workspace_url():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            name = self.path.split("?")[0]
            asset = name.removeprefix("/api/tasks/assets/")
            if name in {"/api/tasks", "/api/tasks/"}:
                asset = "index.html"
            if asset not in {"index.html", "tasks.js", "tasks.css", "dashboard.js"}:
                self.send_error(404)
                return
            body = (WEB / asset).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", {
                ".html": "text/html", ".css": "text/css", ".js": "text/javascript",
            }[Path(asset).suffix])
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/api/tasks"
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.fixture(scope="module")
def chromium():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runtime:
        try:
            browser = runtime.chromium.launch(args=[
                "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
            ])
        except playwright.Error as error:
            if "Executable doesn't exist" in str(error):
                pytest.skip("Optional Playwright browser is not installed")
            raise
        yield browser
        browser.close()


def screenshot(page, name):
    folder = os.environ.get("MINDME_UI_SCREENSHOTS")
    if folder:
        target = Path(folder)
        target.mkdir(parents=True, exist_ok=True)
        settle(page)
        page.evaluate("window.scrollTo(0, 0)")
        # Full-page capture misplaces fixed overlays, so capture the real viewport when one is showing.
        overlay = page.evaluate("""[...document.querySelectorAll('body *')].some((node) =>
          getComputedStyle(node).position === 'fixed' && node.getClientRects().length && node.offsetWidth > 0)""")
        page.screenshot(path=str(target / f"{name}.png"), full_page=not overlay)


def settle(page):
    page.wait_for_function("!document.documentElement.dataset.transitioning")
    page.evaluate("""Promise.all(document.getAnimations()
      .filter((a) => Number.isFinite(a.effect?.getComputedTiming?.().endTime))
      .map((a) => a.finished.catch(() => {})))""")


def visible_panels(page):
    return page.evaluate("""[...document.querySelectorAll('[data-view-panel]')]
      .filter((node) => node.getClientRects().length && getComputedStyle(node).visibility !== 'hidden')
      .map((node) => node.dataset.viewPanel)""")


@pytest.mark.parametrize("width,height,scheme", [
    (1440, 1000, "dark"), (1440, 1000, "light"),
    (390, 844, "dark"), (390, 844, "light"),
])
def test_real_browser_layouts_and_task_chat(workspace_url, chromium, width, height, scheme):
    context = chromium.new_context(viewport={"width": width, "height": height}, color_scheme=scheme)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(workspace_url + "?mock=1")
    page.locator("#briefing-open").wait_for(state="visible")
    page.locator(".priority-card").first.wait_for()
    assert page.locator(".priority-card").count() == 3
    assert visible_panels(page) == ["today"]
    hero = page.locator("#hero-line").inner_text()
    assert "today" in hero and "~" in hero, hero
    desktop = width >= 1024
    assert page.locator("#quick-bar").is_visible() is not desktop
    assert page.locator("#header-mic").is_visible() is desktop
    if not desktop:
        bar = page.locator("#quick-bar").bounding_box()
        padding = page.evaluate("parseFloat(getComputedStyle(document.getElementById('workspace')).paddingBottom)")
        assert padding >= bar["height"], "Mobile content can hide behind the action bar"
    screenshot(page, f"{width}-{scheme}-today")
    page.locator('[data-view="tasks"]').click()
    settle(page)
    assert visible_panels(page) == ["tasks"], "Only one workspace view may remain visible"
    assert page.locator(".board-lane").count() == 6
    screenshot(page, f"{width}-{scheme}-flow")
    page.locator('[data-view="today"]').click()
    page.locator('[data-view="tasks"]').click()
    settle(page)
    assert visible_panels(page) == ["tasks"], "Rapid navigation left a stale view visible"
    page.locator("#task-list .task-row").first.click()
    page.locator(".task-copilot textarea").wait_for()
    assert page.locator(".step-rail button").count() == 7
    rail = page.evaluate("""(() => { const rail = document.querySelector('.step-rail').getBoundingClientRect();
      return { fits: [...document.querySelectorAll('.step-rail button')].every((b) => b.getBoundingClientRect().right <= rail.right + 1),
        scrolls: getComputedStyle(document.querySelector('.step-rail')).overflowX }; })()""")
    assert rail["fits"] or (not desktop and rail["scrolls"] == "auto"), rail
    if width == 1440:
        assert page.evaluate("getComputedStyle(document.getElementById('inspector')).position") == "fixed"
        lanes = page.evaluate("""(() => { const board = document.querySelector('.board').getBoundingClientRect();
          return board.right <= innerWidth + 1; })()""")
        assert lanes, "Board must stay within the viewport while the task drawer is open"
    page.locator(".task-copilot textarea").fill("Build a plan for this task.")
    page.locator(".task-copilot button[type=submit]").click()
    page.locator(".ai-card.plan").wait_for()
    assert page.locator(".plan-check").count() == 3
    screenshot(page, f"{width}-{scheme}-task-chat")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "Viewport overflows"
    assert page.evaluate("localStorage.length === 0 && sessionStorage.length === 0")
    page.locator("#theme-toggle").click()
    assert page.locator("html").get_attribute("data-theme") == ("light" if scheme == "dark" else "dark")
    assert not errors
    context.close()


def api_fixture(operation, *, assist_enabled=True):
    task = {
        "path": "tasks/synthetic.md", "revision": "a" * 40,
        "title": "<img src=x onerror=alert(1)> Synthetic task",
        "stage": "ready", "outcome": "A synthetic outcome.", "done_when": "Evidence checked.",
        "next_action": "Prepare a bounded draft.", "attention_eligible": True,
        "attention_reasons": ["focus_today"], "definition": {"complete": True, "missing": []},
    }
    if operation == "session":
        import time
        return 200, {"authenticated": True, "csrf": "synthetic-csrf", "expires": time.time() + 3600}
    if operation.startswith("overview"):
        return 200, {
            "items": [task], "history": [], "areas": [], "projects": [],
            "next_offset": None, "canonical_revision": "a" * 40, "attention_complete": True,
            "attention": {"calendar_date": "2026-10-13", "timezone": "Europe/Riga", "deadline_horizon_days": 7},
        }
    if operation == "task":
        return 200, task
    if operation == "assist/usage":
        return (200, {"month_eur": 0, "month_cap_eur": 10, "today_eur": 0, "today_cap_eur": .5, "calls_today": 0, "models": {}}) if assist_enabled else (404, {"error": "disabled", "message": "AI disabled."})
    if operation == "assist/voice":
        return 200, {"text": "да", "language": "ru", "duration_s": 1}
    if operation == "assist/chat":
        return 200, {"thread_id": "synthetic-thread", "reply": "Review this exact scope.", "language": "en", "cards": [{
            "kind": "proposal", "proposal_id": "b" * 24, "action_digest": "c" * 64,
            "title": "<script>alert(1)</script>", "summary": "A synthetic proposal.",
            "action": {"kind": "prepare_task", "scope": "A synthetic outline."}, "task": task,
        }], "suggestions": [], "usage": {}}
    if operation == "decide":
        return 200, {"id": "b" * 24, "kind": "prepare_task", "status": "submitted"}
    return 404, {"error": "not_found", "message": "Synthetic API operation unavailable."}


@pytest.mark.parametrize("recover_voice", [False, True])
def test_real_browser_owner_boundary_voice_and_text_only_proposal(workspace_url, chromium, recover_voice):
    context = chromium.new_context(viewport={"width": 1440, "height": 1000})
    page = context.new_page()
    calls = []

    def respond(route):
        operation = route.request.url.split("/api/tasks/api/")[1]
        calls.append((operation, route.request))
        if recover_voice and operation == "assist/voice" and sum(name == "assist/voice" for name, _ in calls) == 1:
            route.abort("failed")
            return
        status, body = api_fixture(operation)
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    page.route("**/api/tasks/api/**", respond)
    page.goto(workspace_url + "#tasks")
    page.locator("#command-open").wait_for(state="visible")
    page.locator("#task-list .task-row").click()
    page.locator(".task-copilot textarea").fill("Prepare a synthetic outline.")
    page.locator(".task-copilot button[type=submit]").click()
    page.locator(".ai-card.proposal").wait_for()
    assert page.locator("#inspector img").count() == 0
    assert page.locator("#inspector script").count() == 0
    assert page.locator(".ai-card h4").inner_text() == "<script>alert(1)</script>"
    page.locator(".task-copilot .mic-button").click()
    page.locator('.task-copilot .mic-button[aria-pressed="true"]').wait_for()
    page.wait_for_timeout(1100)  # One synthetic MediaRecorder timeslice, no real audio.
    screenshot(page, "desktop-voice-waveform")
    page.locator(".task-copilot .mic-button").click()
    if recover_voice:
        page.locator("#uncertain").wait_for(state="visible")
        page.locator("#recover-request").click()
    page.wait_for_function("document.querySelector('.ai-card.proposal').textContent.includes('Submitted')")
    voice = next(request for operation, request in calls if operation == "assist/voice")
    assert voice.headers["x-mindme-csrf"] == "synthetic-csrf"
    assert len(voice.headers["x-request-id"]) == 32
    assert voice.headers["content-type"].startswith("audio/")
    assert voice.post_data_buffer and len(voice.post_data_buffer) <= 2 * 1024 * 1024
    if recover_voice:
        recovered = [request for operation, request in calls if operation == "assist/voice"]
        assert len(recovered) == 2
        assert recovered[0].headers["x-request-id"] == recovered[1].headers["x-request-id"]
        assert recovered[0].post_data_buffer == recovered[1].post_data_buffer
    chat = next(request for operation, request in calls if operation == "assist/chat")
    assert chat.post_data_json["task"] == {"path": "tasks/synthetic.md", "revision": "a" * 40}
    decision = next(request for operation, request in calls if operation == "decide")
    assert decision.post_data_json == {"proposal_id": "b" * 24, "approval_digest": "c" * 64, "decision": "approve"}
    assert decision.headers["x-mindme-csrf"] == "synthetic-csrf"
    assert decision.headers["origin"] == workspace_url.split("/api/tasks")[0]
    context.close()


def test_real_browser_ai_disabled_preserves_manual_controls(workspace_url, chromium):
    context = chromium.new_context()
    page = context.new_page()

    def respond(route):
        operation = route.request.url.split("/api/tasks/api/")[1]
        status, body = api_fixture(operation, assist_enabled=False)
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    page.route("**/api/tasks/api/**", respond)
    page.goto(workspace_url + "#tasks")
    page.get_by_text("AI is unavailable.", exact=False).wait_for()
    assert not page.locator("#quick-mic").is_visible()
    assert not page.locator("#quick-chat").is_visible()
    page.locator("#task-list .task-row").click()
    page.get_by_role("button", name="Edit & plan", exact=True).click()
    page.locator("#definition-form").wait_for()
    assert page.locator(".task-copilot").count() == 0
    page.locator("#capture-open").click()
    assert page.locator("#capture-text").is_visible()
    context.close()


def test_real_browser_uncertain_ai_request_locks_replacement(workspace_url, chromium):
    context = chromium.new_context()
    page = context.new_page()
    chat_calls = []

    def respond(route):
        operation = route.request.url.split("/api/tasks/api/")[1]
        if operation == "assist/chat":
            chat_calls.append(route.request.post_data_json)
            route.abort("failed")
            return
        status, body = api_fixture(operation)
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    page.route("**/api/tasks/api/**", respond)
    page.goto(workspace_url + "#tasks")
    page.locator("#command-open").wait_for(state="visible")
    page.locator("#command-open").click()
    page.locator("#global-chat textarea").fill("A synthetic instruction.")
    page.locator("#global-chat button[type=submit]").click()
    page.locator("#uncertain").wait_for(state="visible")
    assert page.locator("#global-chat button[type=submit]").is_disabled()
    assert len(chat_calls) == 1
    page.locator("#copilot-close").click()
    page.locator("#recover-request").click()
    page.wait_for_function("document.querySelector('#recover-request').disabled === false")
    assert len(chat_calls) == 2
    assert chat_calls[0]["request_id"] == chat_calls[1]["request_id"]
    context.close()


def test_real_browser_drag_undo_and_reduced_motion(workspace_url, chromium):
    context = chromium.new_context(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
    page = context.new_page()
    page.goto(workspace_url + "?mock=1#tasks")
    page.locator("#command-open").wait_for(state="visible")
    card = page.locator('.board-lane[data-stage="ready"] .task-row').first
    title = card.locator("h3").inner_text()
    card.drag_to(page.locator('.board-lane[data-stage="doing"]'))
    page.locator('.board-lane[data-stage="doing"] h3').filter(has_text=title).wait_for()
    page.locator("#toast button").click()
    page.locator('.board-lane[data-stage="ready"] h3').filter(has_text=title).wait_for()
    assert page.locator(".intelligence-orb span").first.evaluate("(node) => getComputedStyle(node).animationName") == "none"
    context.close()


def test_real_browser_undo_language_keyboard_and_mock_isolation(workspace_url, chromium):
    context = chromium.new_context(viewport={"width": 1440, "height": 1000}, locale="ru-RU")
    page = context.new_page()
    live_requests = []
    page.on("request", lambda request: live_requests.append(request.url) if "/api/tasks/api/" in request.url else None)
    page.goto(workspace_url + "?mock=1#tasks")
    page.locator("#command-open").wait_for(state="visible")
    assert page.locator("html").get_attribute("lang") == "ru"
    page.locator("#language-toggle").click()
    page.locator("#task-list .task-row").first.click()
    page.locator(".task-copilot textarea").wait_for()
    original = page.locator(".detail-section dd").first.inner_text()
    page.locator(".task-copilot .chat-suggestions button").first.click()
    page.locator(".ai-card.applied").wait_for()
    page.locator("#toast button").click()
    page.get_by_text("Synthetic change undone.", exact=False).wait_for()
    assert page.locator(".detail-section dd").first.inner_text() == original
    page.keyboard.press("Control+k")
    assert page.locator("#copilot-dialog").is_visible()
    assert page.locator("#global-chat textarea").evaluate("(node) => node === document.activeElement")
    screenshot(page, "desktop-global-copilot")
    page.keyboard.press("Escape")
    assert not page.locator("#copilot-dialog").is_visible()
    assert live_requests == []
    context.close()
