"""Synthetic, offline UI regressions for the AI-first owner workspace."""

from __future__ import annotations

import os
import json
import re
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from test_closure_scan_ui import run_ui_test


WEB = Path(__file__).parents[1] / "web"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required")
def test_studio_copy_covers_both_locales_and_partial_work_is_not_a_quiet_day():
    keys = sorted(
        set(re.findall(r'data-i18n(?:-aria|-placeholder)?="([^"]+)"', (WEB / "index.html").read_text(encoding="utf-8")))
        | set(re.findall(r'\bt\("([^"]+)"\)', (WEB / "tasks.js").read_text(encoding="utf-8")))
    )
    run_ui_test(r"""
for (const key of %s) for (const language of ["en", "ru"]) {
  assert.ok(COPY[language][key], `${language} missing ${key}`);
}
for (const key of Object.values(CLOSURE_CONFLICTS)) for (const suffix of ["Label", "Text"]) {
  for (const language of ["en", "ru"]) assert.ok(COPY[language][key + suffix], `${language} missing ${key + suffix}`);
}
state.items = [];
state.overview = null;
dashboard.renderPriorities();
assert.match($("hero-line").textContent, /No task snapshot/);
assert.match($("hero-detail").textContent, /Unavailable does not mean/);
state.overview = { attention_complete: false, next_offset: null };
dashboard.renderPriorities();
assert.match($("hero-line").textContent, /more to check/);
assert.match($("hero-detail").textContent, /Unchecked/);
state.overview = { attention_complete: true, next_offset: 12 };
dashboard.renderPriorities();
assert.match($("hero-line").textContent, /more to check/);
state.overview.next_offset = null;
dashboard.renderPriorities();
assert.match($("hero-detail").textContent, /loaded snapshot/);
state.items = [
  { title: "Synthetic estimated task", attention_eligible: true, estimate_min: 20 },
  { title: "Synthetic unestimated task", attention_eligible: true },
];
dashboard.renderPriorities();
assert.match($("hero-detail").textContent, /~20m recorded across 1\/2 tasks/);
""" % json.dumps(keys))


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
def test_assist_routes_send_canonical_uuid_while_legacy_routes_keep_hex32():
    from task_assist import _UUID_RE

    # Live voice failed because assist IDs had their dashes stripped; bind the JS check to the real host regex.
    run_ui_test(r"""
(async () => {
  const canonical = new RegExp("^" + %s.replace(/\\Z$/, "$"));
  let counter = 0;
  crypto.randomUUID = () => `3f2a${String(++counter).padStart(4, "0")}-9c1d-4e5f-8a7b-0123456789ab`;
  assist.available = true;
  state.csrf = "synthetic";
  const path = "tasks/synthetic.md";
  state.items = [{ path, revision: "a".repeat(40), stage: "ready" }];
  state.selected = { path, revision: "a".repeat(40) };
  const ids = {};
  assistMutation = async (operation, payload) => { ids[operation] = payload.request_id; };
  await sendChat(path, "Plan this.");
  await sendStep(path, "plan");
  await undoAssist(path, "c".repeat(24));
  moveTask(state.items[0], "doing");
  const receiptCalls = [];
  api = async (operation, payload) => { receiptCalls.push(payload.request_id); return { cards: [{ kind: "applied", receipt_id: "d".repeat(24), status: "executing" }] }; };
  setTimeout = () => 0;
  const card = { kind: "applied", receipt_id: "d".repeat(24), status: "submitted" };
  watchReceipt(path, card);
  const poll = assist.polls.get(card.receipt_id);
  assert.ok(poll);
  await pollReceipt(poll);
  poll.timer = null;
  await pollReceipt(poll);
  for (const operation of ["assist/chat", "assist/step", "assist/undo", "assist/change"]) {
    assert.match(ids[operation], canonical, operation);
  }
  assert.equal(receiptCalls.length, 2);
  assert.match(receiptCalls[0], canonical);
  assert.equal(receiptCalls[0], receiptCalls[1]);
  assert.match(requestId(), /^[0-9a-f]{32}$/);
})()
""" % json.dumps(_UUID_RE.pattern))


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
  assert.match(poll.requestId, /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/);
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


def screenshot(page, name, *, reset_scroll=True):
    folder = os.environ.get("MINDME_UI_SCREENSHOTS")
    if folder:
        target = Path(folder)
        target.mkdir(parents=True, exist_ok=True)
        settle(page)
        if reset_scroll:
            page.evaluate("window.scrollTo(0, 0)")
        page.screenshot(path=str(target / f"{name}.png"), full_page=False)


def settle(page):
    page.wait_for_function("!document.documentElement.dataset.transitioning")
    page.evaluate("""Promise.all(document.getAnimations()
      .filter((a) => Number.isFinite(a.effect?.getComputedTiming?.().endTime))
      .map((a) => a.finished.catch(() => {})))""")


def click_visible_header(page, selector):
    # Locator.click auto-scrolls sticky header nodes to their original document
    # position. A real pointer click on the visible header must not do that.
    box = page.locator(selector).bounding_box()
    assert box and box["y"] >= 0
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)


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
    if scheme == "light":
        page.locator("#theme-toggle").click()
    assert page.locator(".priority-card").count() == 3
    assert page.evaluate("""() => {
      const priorities = new Set([...document.querySelectorAll('.priority-card')].map(n => n.dataset.path));
      return [...document.querySelectorAll('#today-work .task-row')].every(n => !priorities.has(n.dataset.path));
    }"""), "Additional attention must not repeat the priority cards"
    for selector in ["#snapshot", ".priority-reason", ".status", ".focus-line .hint"]:
        assert page.locator(selector).first.evaluate("(node) => parseFloat(getComputedStyle(node).fontSize)") >= 12
    assert visible_panels(page) == ["today"]
    hero = page.locator("#hero-line").inner_text()
    assert "today" in hero, hero
    assert "~1h 5m recorded across 2/3 tasks" in page.locator("#hero-detail").inner_text()
    tip = page.locator(".info-tip").first
    described = tip.get_attribute("aria-describedby")
    assert described and page.locator(f"#{described}").count() == 1
    tip.focus()
    assert page.locator(f"#{described}").is_visible()
    tip.blur()
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
    assert page.locator("#inspector").evaluate("(node) => node === document.activeElement")
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
        header_bottom = page.locator(".app-header").bounding_box()["y"] + page.locator(".app-header").bounding_box()["height"]
        assert page.locator("#inspector").bounding_box()["y"] >= header_bottom - 1
    screenshot(page, f"{width}-{scheme}-task-detail")
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


def source_notice_overview(**changes):
    _, body = api_fixture("overview?offset=0")
    body.update(excluded_count=1, project_excluded_count=2, errors=[], project_errors=[])
    body["attention"]["assessed_count"] = 14
    body.update(changes)
    return body


def test_real_browser_ai_controls_do_not_wait_for_task_overview(workspace_url, chromium):
    context = chromium.new_context(viewport={"width": 1440, "height": 1000})
    page = context.new_page()
    pending, calls = [], []

    def respond(route):
        operation = route.request.url.split("/api/tasks/api/")[1]
        calls.append(operation)
        if operation.startswith("overview"):
            pending.append(route)
            return
        status, body = api_fixture(operation)
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    try:
        page.route("**/api/tasks/api/**", respond)
        page.goto(workspace_url + "#tasks", wait_until="domcontentloaded")
        page.locator("#header-mic").wait_for(state="visible", timeout=3000)
        assert page.locator("#command-open").is_visible()
        assert pending and "assist/usage" in calls
        assert page.locator("#capture-section").is_hidden()
        status, body = api_fixture("overview")
        for route in pending:
            route.fulfill(status=status, content_type="application/json", body=json.dumps(body))
        page.locator("#task-list .task-row").first.wait_for()
    finally:
        context.close()


@pytest.mark.parametrize("width,locale", [(1440, "en-US"), (390, "ru-RU")])
def test_real_browser_capture_voice_only_fills_reviewable_draft(workspace_url, chromium, width, locale):
    context = chromium.new_context(viewport={"width": width, "height": 1000}, locale=locale)
    page = context.new_page()
    calls = []
    transcript = "Write a synthetic outline." if locale == "en-US" else "Написать учебный план."

    def respond(route):
        operation = route.request.url.split("/api/tasks/api/")[1]
        calls.append((operation, route.request))
        status, body = api_fixture(operation)
        if operation == "assist/voice":
            body = {"text": transcript, "language": "en" if locale == "en-US" else "ru", "duration_s": 1}
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    try:
        page.route("**/api/tasks/api/**", respond)
        page.goto(workspace_url + "#tasks")
        page.locator("#assist-usage meter").wait_for(state="attached")
        page.locator("#capture-open" if width == 1440 else "#quick-capture").click()
        page.locator("#capture-text").fill("Existing draft")
        page.locator("#capture-title-input").fill("Existing title")
        record_capture_voice(page)
        page.wait_for_function(
            "text => document.getElementById('capture-text').value === 'Existing draft\\n' + text", arg=transcript,
        )
        assert page.locator("#capture-title-input").input_value() == "Existing title"
        assert page.locator("#capture-section").is_visible()
        assert not page.locator("#copilot-dialog").is_visible()
        assert page.locator("#capture-form button[type=submit]").is_enabled()
        action = "Add task" if locale == "en-US" else "Добавить задачу"
        assert page.locator("#capture-submit").inner_text() == action
        assert action in page.locator("#notice").inner_text()
        assert [operation for operation, _ in calls if operation in {"assist/voice", "assist/chat", "capture", "capture-save", "decide"}] == ["assist/voice"]
        voice = next(request for operation, request in calls if operation == "assist/voice")
        from task_assist import _uuid
        assert _uuid(voice.headers["x-request-id"]) == voice.headers["x-request-id"]
        page.locator("#capture-text").fill("Edited synthetic draft")
        assert page.locator("#capture-text").input_value() == "Edited synthetic draft"
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        screenshot(page, f"capture-voice-{width}-draft")
    finally:
        context.close()


def record_capture_voice(page):
    page.locator("#capture-mic").click()
    page.wait_for_function("document.getElementById('capture-mic').getAttribute('aria-pressed') === 'true'")
    assert page.locator("#capture-form button[type=submit]").is_disabled()
    page.wait_for_timeout(250)
    page.locator("#capture-mic").click()


def test_real_browser_capture_retains_long_transcript_until_explicit_edited_review(workspace_url, chromium):
    context = chromium.new_context(viewport={"width": 1440, "height": 1000})
    page = context.new_page()
    calls = []
    transcript = "Synthetic " + "x" * 3990

    def respond(route):
        operation = route.request.url.split("/api/tasks/api/")[1]
        calls.append((operation, route.request))
        status, body = api_fixture(operation)
        if operation == "assist/voice":
            body = {"text": transcript, "language": "en", "duration_s": 1}
        elif operation == "capture-save":
            payload = route.request.post_data_json
            status, body = 200, {
                "id": "e" * 24, "kind": "capture_task", "status": "submitted",
                "approval_digest": "f" * 64, "source_status": "current",
                "approved_by": "owner-capture",
                "action": {"kind": "create_task", "text": payload["text"], "definition": payload["definition"]},
            }
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    try:
        page.route("**/api/tasks/api/**", respond)
        page.goto(workspace_url + "#tasks")
        page.locator("#header-mic").wait_for()
        page.locator("#capture-open").click()
        page.locator("#capture-text").fill("Existing draft")
        page.locator("#capture-title-input").fill("Existing title")
        record_capture_voice(page)
        page.wait_for_function(
            "text => document.getElementById('capture-text').value === 'Existing draft\\n' + text", arg=transcript,
        )
        assert "2,000" in page.locator("#capture-voice-status").inner_text()
        page.locator("#capture-form").evaluate("(form) => form.requestSubmit()")
        assert not any(operation in {"capture", "capture-save", "assist/chat", "decide"} for operation, _ in calls)
        assert page.locator("#capture-text").input_value() == "Existing draft\n" + transcript
        page.locator("#capture-text").fill("Short edited task")
        with page.expect_request("**/api/tasks/api/capture-save") as sent:
            page.locator("#capture-form button[type=submit]").click()
        assert sent.value.post_data_json["text"] == "Short edited task"
        assert sent.value.post_data_json["definition"]["title"] == "Existing title"
        assert re.fullmatch(r"[a-f0-9]{32}", sent.value.post_data_json["request_id"])
        assert not any(operation in {"assist/chat", "decide"} for operation, _ in calls)
    finally:
        context.close()


@pytest.mark.parametrize("outcome", ["rejected", "recover", "closed"])
def test_real_browser_capture_voice_failure_recovery_and_late_result(workspace_url, chromium, outcome):
    context = chromium.new_context(viewport={"width": 1440, "height": 1000})
    page = context.new_page()
    calls, pending = [], []

    def respond(route):
        operation = route.request.url.split("/api/tasks/api/")[1]
        calls.append((operation, route.request))
        status, body = api_fixture(operation)
        if operation == "assist/voice":
            if outcome == "closed":
                pending.append(route)
                return
            if outcome == "recover" and sum(name == "assist/voice" for name, _ in calls) == 1:
                route.abort("failed")
                return
            if outcome == "rejected":
                status, body = 422, {"error": "assist_audio_invalid", "message": "Synthetic recording rejected."}
            else:
                body = {"text": "Synthetic recovered draft", "language": "en", "duration_s": 1}
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    try:
        page.route("**/api/tasks/api/**", respond)
        page.goto(workspace_url + "#tasks")
        page.locator("#header-mic").wait_for()
        page.locator("#capture-open").click()
        page.locator("#capture-text").fill("Existing draft")
        page.locator("#capture-title-input").fill("Existing title")
        with page.expect_request("**/api/tasks/api/assist/voice"):
            record_capture_voice(page)
        if outcome == "closed":
            page.wait_for_function("document.getElementById('capture-mic').disabled")
            page.locator("#capture-cancel").click()
            page.locator("#capture-open").click()
            assert page.locator("#capture-section").is_hidden()
            assert len(pending) == 1
            pending[0].fulfill(status=200, content_type="application/json", body=json.dumps({
                "text": "Must not enter a new draft", "language": "en", "duration_s": 1,
            }))
            page.wait_for_function("!document.getElementById('capture-mic').disabled")
            page.locator("#capture-open").click()
            assert page.locator("#capture-text").input_value() == "Existing draft"
        elif outcome == "recover":
            page.locator("#uncertain").wait_for()
            assert page.locator("#capture-text").input_value() == "Existing draft"
            page.locator("#recover-request").click()
            page.wait_for_function(
                "document.getElementById('capture-text').value === 'Existing draft\\nSynthetic recovered draft'",
            )
            requests = [request for operation, request in calls if operation == "assist/voice"]
            assert len(requests) == 2
            assert requests[0].headers["x-request-id"] == requests[1].headers["x-request-id"]
            assert requests[0].post_data_buffer == requests[1].post_data_buffer
        else:
            page.wait_for_function("document.getElementById('notice').textContent.includes('Synthetic recording rejected')")
            assert page.locator("#capture-text").input_value() == "Existing draft"
        assert page.locator("#capture-title-input").input_value() == "Existing title"
        assert page.locator("#capture-form button[type=submit]").is_enabled()
        assert not any(operation in {"assist/chat", "capture", "capture-save", "decide"} for operation, _ in calls)
    finally:
        context.close()


def test_real_browser_closing_capture_cancels_pending_microphone_permission(workspace_url, chromium):
    context = chromium.new_context(viewport={"width": 1440, "height": 1000})
    page = context.new_page()
    calls = []
    page.add_init_script("""navigator.mediaDevices.getUserMedia = () => new Promise((resolve) => {
      window.finishCapturePermission = () => resolve({
        getTracks: () => [{ stop: () => { window.captureStreamStopped = true; } }],
      });
    });""")

    def respond(route):
        operation = route.request.url.split("/api/tasks/api/")[1]
        calls.append(operation)
        status, body = api_fixture(operation)
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    try:
        page.route("**/api/tasks/api/**", respond)
        page.goto(workspace_url + "#tasks")
        page.locator("#header-mic").wait_for()
        page.locator("#capture-open").click()
        page.locator("#capture-text").fill("Existing draft")
        page.locator("#capture-mic").click()
        assert page.locator("#capture-mic").is_disabled()
        page.locator("#capture-cancel").click()
        page.evaluate("window.finishCapturePermission()")
        page.wait_for_function("window.captureStreamStopped === true")
        assert "assist/voice" not in calls and "assist/chat" not in calls
        assert page.locator("#capture-text").input_value() == "Existing draft"
    finally:
        context.close()


@pytest.mark.parametrize("width", [390, 1440])
@pytest.mark.parametrize("scheme", ["dark", "light"])
def test_real_browser_source_notice_disclosure_locale_and_text_reflow(workspace_url, chromium, width, scheme):
    context = chromium.new_context(viewport={"width": width, "height": 1000 if width == 1440 else 844},
                                   locale="en-US", reduced_motion="reduce", has_touch=width == 390)
    page = context.new_page()
    calls, errors = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def respond(route):
        operation = route.request.url.split("/api/tasks/api/")[1]
        calls.append((operation, route.request.method))
        status, body = api_fixture(operation, assist_enabled=False)
        if operation.startswith("overview"):
            body = source_notice_overview()
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    page.route("**/api/tasks/api/**", respond)
    page.goto(workspace_url + "#tasks")
    page.locator("#assist-usage[data-i18n='unavailable']").wait_for(state="attached")
    label_fixture(page)
    if scheme == "light":
        page.locator("#theme-toggle").click()
    scope = page.locator("#source-scope")
    summary = scope.locator("summary")
    original = summary.element_handle()
    assert scope.evaluate("(node) => node.tagName") == "DETAILS"
    assert scope.is_visible() and scope.get_attribute("open") is None
    assert page.locator("#source-issues").is_hidden()
    assert summary.inner_text() == "Some sources are outside this view"
    assert "assessed window: 1." in page.locator("#task-source-count").text_content()
    assert "checked so far: 2." in page.locator("#project-source-count").text_content()
    assert not page.locator("#task-source-count").is_visible()
    assert scope.evaluate("(node) => !node.closest('[role=alert], [aria-live]')")
    assert scope.evaluate("(node) => getComputedStyle(node).color") == (
        "rgb(176, 180, 167)" if scheme == "dark" else "rgb(90, 98, 80)")
    screenshot(page, f"source-scope-{width}-{scheme}-en-closed")

    page.keyboard.press("Tab")
    summary.focus()
    assert summary.evaluate("(node) => getComputedStyle(node).outlineStyle") == "solid"
    assert summary.evaluate("(node) => parseFloat(getComputedStyle(node).outlineWidth)") >= 2
    summary.press("Enter")
    assert scope.get_attribute("open") is not None
    summary.press("Space")
    assert scope.get_attribute("open") is None
    summary.press("Enter")
    assert page.locator("#task-source-count").is_visible()
    assert page.locator("#project-source-count").is_visible()
    task_text = page.locator("#task-list").text_content()

    for language in ["en", "ru"]:
        if language == "ru":
            summary.focus()
            before_calls, before_scroll = list(calls), page.evaluate("scrollY")
            page.locator("#language-toggle").evaluate("(node) => node.click()")
            assert summary.evaluate("(node, original) => node === original && node === document.activeElement", original)
            assert abs(page.evaluate("scrollY") - before_scroll) <= 1
            assert calls == before_calls, "Changing local notice copy must not refetch or mutate"
            assert scope.get_attribute("open") is not None
            assert summary.inner_text() == "Некоторые источники не входят в этот обзор"
            assert "проверенной части списка: 1." in page.locator("#task-source-count").inner_text()
            assert "среди уже проверенных: 2." in page.locator("#project-source-count").inner_text()
            assert "ошибками загрузки" in scope.inner_text()
            assert page.locator("#capture-section").is_hidden()
            assert page.locator("#copilot-dialog").is_hidden()
        for text_size in [16, 32]:
            page.evaluate("(size) => document.documentElement.style.fontSize = size + 'px'", text_size)
            summary.scroll_into_view_if_needed()
            metrics = scope.evaluate("""node => {
              const summary = node.querySelector('summary').getBoundingClientRect();
              return {
                height: summary.height, width: summary.width,
                overflow: document.documentElement.scrollWidth - innerWidth,
                clipped: [...node.children].some(child => {
                  const rect = child.getBoundingClientRect();
                  return rect.left < 0 || rect.right > innerWidth + 1 || child.scrollWidth > child.clientWidth + 1;
                }),
              };
            }""")
            assert metrics["height"] >= 44 and metrics["width"] >= 44
            assert metrics["overflow"] <= 0 and not metrics["clipped"], metrics
            assert summary.evaluate("(node) => parseFloat(getComputedStyle(node).fontSize)") >= text_size * .8
            screenshot(page, f"source-scope-{width}-{scheme}-{language}-text{text_size}", reset_scroll=False)
        page.evaluate("document.documentElement.style.fontSize = '16px'")
    assert page.locator("#task-list").text_content() != "", "Locale rendering must retain the task"
    assert "<img src=x onerror=alert(1)> Synthetic task" in task_text
    assert "<img src=x onerror=alert(1)> Synthetic task" in page.locator("#task-list").text_content()
    if width == 390:
        summary.tap()
        assert scope.get_attribute("open") is None
        summary.tap()
        assert scope.get_attribute("open") is not None
    assert all(method == "GET" for _, method in calls)
    assert page.evaluate("localStorage.length + sessionStorage.length") == 0
    assert not errors
    context.close()


@pytest.mark.parametrize("width", [390, 1440])
def test_real_browser_source_notice_keeps_errors_visible_and_paging_state(workspace_url, chromium, width):
    context = chromium.new_context(viewport={"width": width, "height": 1000 if width == 1440 else 844},
                                   locale="en-US", reduced_motion="reduce")
    page = context.new_page()
    calls, errors = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    fixture = {"overview": source_notice_overview(
        attention_complete=False, project_excluded_count=1, project_next_offset=4, next_offset=12,
        errors=[
            {"code": "task_source_policy_unresolved", "path": "SYNTHETIC_HIDDEN_PATH"},
            {"code": "SYNTHETIC_HIDDEN_CODE", "message": "SYNTHETIC_HIDDEN_MESSAGE"},
        ],
        project_errors=[{"code": "task_source_unavailable", "title": "SYNTHETIC_HIDDEN_TITLE"}],
    )}

    def respond(route):
        operation = route.request.url.split("/api/tasks/api/")[1]
        calls.append((operation, route.request.method))
        status, body = api_fixture(operation, assist_enabled=False)
        if operation == "overview?offset=0":
            body = fixture["overview"]
        elif operation == "projects?offset=4":
            status, body = 200, {
                "items": [{"id": "synthetic-project"}], "next_offset": None, "excluded_count": 2,
                "candidate_count": 4, "errors": [{"code": "task_source_unavailable"}],
                "canonical_revision": "a" * 40,
            }
        elif operation == "overview?offset=12":
            _, task = api_fixture("task")
            task.update(path="tasks/synthetic-next.md", title="Synthetic next page")
            body = source_notice_overview(items=[task])
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    page.route("**/api/tasks/api/**", respond)
    page.goto(workspace_url + "#tasks")
    page.locator("#assist-usage[data-i18n='unavailable']").wait_for(state="attached")
    label_fixture(page)
    scope, issues = page.locator("#source-scope"), page.locator("#source-issues")
    summary = scope.locator("summary")
    original = summary.element_handle()
    assert scope.get_attribute("open") is None
    assert issues.is_visible()
    assert "Attention coverage is incomplete" in issues.inner_text()
    assert "privacy metadata is ambiguous" in issues.inner_text()
    assert "Project source issues: 1" in issues.inner_text()
    assert "Refresh to retry its current version" in issues.inner_text()
    assert "absence is not evidence that no work is due" in issues.inner_text()
    assert "SYNTHETIC_HIDDEN" not in page.locator("#warnings").text_content()
    assert issues.evaluate("(node) => !node.closest('details')")
    assert all(node.is_visible() for node in issues.locator("p").all())
    assert issues.evaluate("(node) => getComputedStyle(node).color") == "rgb(241, 199, 141)"
    screenshot(page, f"source-warnings-{width}-mixed-closed")

    before_calls = list(calls)
    click_visible_header(page, "#language-toggle")
    assert calls == before_calls
    assert "Проверка внимания неполная" in issues.inner_text()
    assert "остаётся недоступным до их уточнения" in issues.inner_text()
    assert "повторить попытку с его текущей версией" in issues.inner_text()
    assert "Его отсутствие не означает" in issues.inner_text()
    assert scope.get_attribute("open") is None
    assert "SYNTHETIC_HIDDEN" not in page.locator("#warnings").text_content()
    click_visible_header(page, "#language-toggle")

    fixture["overview"] = source_notice_overview(
        attention_complete=False, project_excluded_count=1, project_next_offset=4, next_offset=12)
    page.locator("#refresh").click()
    page.wait_for_function("document.getElementById('source-issues').children.length === 1")
    assert "unchecked or unresolved sources remain" in issues.inner_text()
    assert issues.is_visible() and scope.get_attribute("open") is None
    page.locator('[data-view="areas"]').click()
    summary.focus()
    summary.press("Enter")
    page.locator("#projects-more").evaluate("(node) => node.click()")
    page.wait_for_function("document.getElementById('project-source-count').textContent.endsWith(': 3.')")
    assert scope.get_attribute("open") is not None
    assert summary.evaluate("(node, original) => node === original && node === document.activeElement", original)
    assert "Project source issues: 1" in issues.inner_text()
    assert page.locator("#projects-more").is_hidden()

    page.locator('[data-view="tasks"]').click()
    summary.focus()
    page.locator("#load-more").evaluate("(node) => node.click()")
    page.wait_for_function("document.querySelectorAll('#task-list .task-row').length === 2")
    assert "checked so far: 3." in page.locator("#project-source-count").inner_text()
    assert "assessed window: 1." in page.locator("#task-source-count").inner_text()
    assert "Project source issues: 1" in issues.inner_text()
    assert scope.get_attribute("open") is not None
    assert summary.evaluate("(node, original) => node === original && node === document.activeElement", original)
    fixture["overview"] = source_notice_overview()
    before_scroll = page.evaluate("scrollY")
    page.locator("#refresh").evaluate("(node) => node.click()")
    page.wait_for_function("document.getElementById('source-issues').hidden")
    assert "checked so far: 2." in page.locator("#project-source-count").inner_text()
    assert scope.get_attribute("open") is not None
    assert summary.evaluate("(node, original) => node === original && node === document.activeElement", original)
    assert abs(page.evaluate("scrollY") - before_scroll) <= 1
    assert page.locator("#task-list .task-row").count() == 1
    page.locator('[data-view="knowledge"]').click()
    assert page.locator("#warnings").is_hidden()
    page.locator('[data-view="tasks"]').click()
    assert scope.is_visible() and scope.get_attribute("open") is not None
    assert all(method == "GET" for _, method in calls)
    assert page.evaluate("localStorage.length + sessionStorage.length") == 0
    assert not errors
    context.close()


@pytest.mark.parametrize("scenario", ["zero", "missing-overview", "expiry", "logout"])
def test_real_browser_source_notice_clears_without_data_or_session(workspace_url, chromium, scenario):
    context = chromium.new_context(locale="en-US", reduced_motion="reduce")
    page = context.new_page()
    fixture = {"expired": False}
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def respond(route):
        operation = route.request.url.split("/api/tasks/api/")[1]
        status, body = api_fixture(operation, assist_enabled=False)
        if fixture["expired"]:
            status, body = 401, {"error": "authentication_required", "message": "Synthetic session expired."}
        elif operation.startswith("overview"):
            if scenario == "missing-overview":
                status, body = 503, {"error": "unavailable", "message": "Synthetic overview unavailable."}
            else:
                count = 0 if scenario == "zero" else 1
                body = source_notice_overview(excluded_count=count, project_excluded_count=count)
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    def logout(route):
        fixture["expired"] = True
        route.fulfill(status=200, content_type="application/json", body="{}")

    page.route("**/api/tasks/api/**", respond)
    page.route("**/api/tasks/auth/logout", logout)
    page.goto(workspace_url + "#tasks")
    page.locator("#assist-usage[data-i18n='unavailable']").wait_for(state="attached")
    scope = page.locator("#source-scope")
    if scenario in {"expiry", "logout"}:
        scope.locator("summary").click()
        assert scope.get_attribute("open") is not None
        if scenario == "expiry":
            fixture["expired"] = True
            page.locator("#refresh").click()
        else:
            page.locator("#sign-out").click()
        page.locator("#sign-in").wait_for()
    assert scope.get_attribute("hidden") is not None
    assert scope.get_attribute("open") is None
    assert page.locator("#task-source-count").text_content() == ""
    assert page.locator("#project-source-count").text_content() == ""
    assert page.locator("#source-issues").text_content() == ""
    assert page.locator("#source-issues").is_hidden()
    if scenario == "missing-overview":
        assert page.locator("#notice.error").is_visible()
        assert "Synthetic overview unavailable" in page.locator("#notice").inner_text()
    if scenario == "zero":
        assert page.locator("#warnings").evaluate("(node) => node.getBoundingClientRect().height") == 0
    assert page.evaluate("localStorage.length + sessionStorage.length") == 0
    assert not errors
    context.close()


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
    from task_assist import _uuid

    assert _uuid(voice.headers["x-request-id"]) == voice.headers["x-request-id"]
    assert voice.headers["content-type"].startswith("audio/")
    assert voice.post_data_buffer and len(voice.post_data_buffer) <= 2 * 1024 * 1024
    if recover_voice:
        recovered = [request for operation, request in calls if operation == "assist/voice"]
        assert len(recovered) == 2
        assert recovered[0].headers["x-request-id"] == recovered[1].headers["x-request-id"]
        assert recovered[0].post_data_buffer == recovered[1].post_data_buffer
    chat = next(request for operation, request in calls if operation == "assist/chat")
    assert chat.post_data_json["task"] == {"path": "tasks/synthetic.md", "revision": "a" * 40}
    assert _uuid(chat.post_data_json["request_id"]) == chat.post_data_json["request_id"]
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
    assert page.locator(".studio-mark").evaluate("(node) => getComputedStyle(node).animationName") == "none"
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


def label_fixture(page):
    page.evaluate("""() => {
      const banner = document.getElementById('demo-banner');
      banner.hidden = false;
      banner.textContent = 'SYNTHETIC UI FIXTURE · no production data or identity';
    }""")


@pytest.mark.parametrize("width,height", [(1440, 1000), (390, 844)])
@pytest.mark.parametrize("locale,scheme,motion", [
    ("en-US", "dark", "no-preference"), ("en-US", "light", "reduce"),
    ("ru-RU", "dark", "reduce"), ("ru-RU", "light", "no-preference"),
])
def test_real_browser_studio_sign_in(workspace_url, chromium, width, height, locale, scheme, motion):
    context = chromium.new_context(viewport={"width": width, "height": height}, locale=locale, reduced_motion=motion)
    page = context.new_page()
    operations, requests, errors = [], [], []
    page.on("request", lambda request: requests.append(request.url))
    page.on("pageerror", lambda error: errors.append(str(error)))

    def deny(route):
        operations.append(route.request.url.split("/api/tasks/api/")[1])
        route.fulfill(status=401, content_type="application/json", body=json.dumps({
            "error": "authentication_required", "message": "Synthetic signed-out fixture.",
        }))

    page.route("**/api/tasks/api/**", deny)
    page.goto(workspace_url)
    page.locator("#sign-in").wait_for(state="visible")
    label_fixture(page)
    assert page.locator("html").get_attribute("data-theme") == "dark"
    if scheme == "light":
        page.locator("#theme-toggle").click()
    assert page.locator("#sign-in").get_attribute("href") == "/api/tasks/auth/login"
    assert page.locator("#access-retry").is_hidden(), "Normal signed-out state needs sign-in, not a competing retry"
    assert page.locator("#workspace").is_hidden()
    assert page.locator("html").get_attribute("lang") == locale[:2]
    assert ("Место для мысли." if locale.startswith("ru") else "Room to think.") in page.locator("#access-title").inner_text()
    assert page.locator("#sign-in").bounding_box()["height"] >= 44
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert operations == ["session"], "Sign-in must not load tasks or fake a live session"
    assert all(url.startswith(workspace_url.split("/api/tasks")[0]) for url in requests), requests
    settle(page)
    assert page.evaluate("document.getAnimations().every(a => Number.isFinite(a.effect.getComputedTiming().endTime))")
    if motion == "reduce":
        assert page.locator(".fold-signal").evaluate("(node) => getComputedStyle(node).animationName") == "none"
    screenshot(page, f"signin-{width}-{scheme}-{locale[:2]}-{motion}")
    page.locator("#language-toggle").click()
    assert page.locator("html").get_attribute("lang") != locale[:2]
    assert page.evaluate("localStorage.length + sessionStorage.length") == 0
    assert not errors
    context.close()


@pytest.mark.parametrize("width", [320, 390, 1024, 1101, 1440, 1599, 1600])
@pytest.mark.parametrize("locale,scheme", [
    ("en-US", "dark"), ("en-US", "light"), ("ru-RU", "dark"), ("ru-RU", "light"),
])
def test_real_browser_keyboard_focus_and_responsive_frame(workspace_url, chromium, width, locale, scheme):
    context = chromium.new_context(viewport={"width": width, "height": 1000 if width >= 1024 else 844},
                                   locale=locale, reduced_motion="reduce")
    page = context.new_page()
    page.goto(workspace_url + "?mock=1#tasks")
    page.locator("#briefing-open").wait_for(state="attached")
    page.wait_for_function("!document.getElementById('command-open').hidden")
    if scheme == "light":
        page.locator("#theme-toggle").click()
    assert page.locator(".app-header").bounding_box()["height"] <= 100, "Normal text must not create an oversized header"
    assert page.locator("#refresh").is_visible(), "Recovery must remain reachable on phones"
    if width >= 1024:
        for control in ["command-open", "header-mic", "capture-open", "refresh", "theme-toggle", "language-toggle", "sign-out"]:
            box = page.locator(f"#{control}").bounding_box()
            assert box and box["height"] >= 44 and box["x"] + box["width"] <= width + 1, control
    nav = page.locator('[data-view="tasks"]')
    nav.focus()
    page.keyboard.press("End")
    assert page.evaluate("document.activeElement.dataset.view") == "activity"
    page.keyboard.press("Home")
    assert page.evaluate("document.activeElement.dataset.view") == "today"
    row = page.locator("#task-list .task-row").first
    path = row.get_attribute("data-path")
    row.press("Enter")
    page.locator(".task-copilot textarea").wait_for()
    assert page.locator("#inspector").evaluate("(node) => node === document.activeElement")
    assert page.locator(".step-rail button").count() == 7
    if 1101 <= width <= 1599:
        header = page.locator(".app-header").bounding_box()
        assert page.locator("#inspector").bounding_box()["y"] >= header["y"] + header["height"] - 1
    last_step = page.locator(".step-rail button").last
    last_step.focus()
    assert last_step.evaluate("(node) => node.getBoundingClientRect().right <= innerWidth + 1")
    page.keyboard.press("Escape")
    assert page.evaluate("document.activeElement.dataset.path") == path
    capture = "quick-capture" if width < 690 else "capture-open"
    page.locator(f"#{capture}").click()
    assert page.locator("#capture-text").evaluate("(node) => node === document.activeElement")
    page.locator("#capture-text").fill("Synthetic unsaved capture / тестовый черновик")
    if width in [390, 1440]:
        screenshot(page, f"capture-{width}-{locale[:2]}-{scheme}-reduced")
    page.keyboard.press("Escape")
    assert page.locator("#capture-section").is_hidden()
    assert page.evaluate("document.activeElement.id") == capture
    page.keyboard.press("Control+k")
    assert page.locator("#global-chat textarea").evaluate("(node) => node === document.activeElement")
    page.keyboard.press("Escape")
    assert not page.locator("#copilot-dialog").is_visible()
    assert page.evaluate("document.activeElement.id") == capture
    for view in ["areas", "activity", "today", "knowledge", "tasks"]:
        page.locator(f'[data-view="{view}"]').click()
        assert visible_panels(page) == [view]
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), view
        if width in [390, 1440] and view in ["areas", "activity"]:
            screenshot(page, f"{view}-{width}-{locale[:2]}-{scheme}-reduced")
    if width == 1440:
        samples = page.evaluate("""async () => {
          const samples = [];
          for (const view of ['today','knowledge','areas','activity','today','tasks'])
            document.querySelector(`[data-view="${view}"]`).click();
          for (let i = 0; i < 20; i++) {
            await new Promise(requestAnimationFrame);
            samples.push([...document.querySelectorAll('[data-view-panel]')].filter(n => n.getClientRects().length).length);
          }
          return samples;
        }""")
        assert set(samples) == {1}
    page.locator("#sign-out").click()
    page.locator("#sign-in").wait_for()
    assert not page.locator("#capture-text").input_value()
    assert not page.locator("#global-chat").inner_text()
    assert page.locator("#task-list .task-row").count() == 0
    assert page.locator("#today-priorities").inner_text() == ""
    assert page.evaluate("localStorage.length + sessionStorage.length") == 0
    context.close()


@pytest.mark.parametrize("failure", ["permission", "rejected", "empty"])
def test_real_browser_voice_failure_is_visible_inside_copilot(workspace_url, chromium, failure):
    context = chromium.new_context(viewport={"width": 1440, "height": 1000})
    page = context.new_page()
    operations = []
    if failure == "permission":
        page.add_init_script("""Object.defineProperty(navigator.mediaDevices, 'getUserMedia', {
          value: () => Promise.reject(new DOMException('Synthetic denial', 'NotAllowedError'))
        });""")

    def respond(route):
        operation = route.request.url.split("/api/tasks/api/")[1]
        operations.append(operation)
        if operation == "assist/voice":
            status, body = (422, {"error": "audio_invalid", "message": "Synthetic audio rejected."}) if failure == "rejected" else (200, {"text": "", "language": "en", "duration_s": 0})
        else:
            status, body = api_fixture(operation)
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    page.route("**/api/tasks/api/**", respond)
    page.goto(workspace_url + "#tasks")
    page.wait_for_function("!document.getElementById('command-open').hidden")
    label_fixture(page)
    page.locator("#command-open").click()
    page.locator("#global-chat .mic-button").click()
    if failure != "permission":
        page.locator('#global-chat .mic-button[aria-pressed="true"]').wait_for()
        page.wait_for_timeout(1100)  # Synthetic Chromium media, never a real microphone.
        page.locator("#global-chat .mic-button").click()
    page.locator("#copilot-notice").wait_for(state="visible")
    message = page.locator("#copilot-notice").inner_text()
    assert ("not granted" if failure == "permission" else "rejected" if failure == "rejected" else "No usable speech") in message
    assert "assist/chat" not in operations
    assert operations.count("assist/voice") == (0 if failure == "permission" else 1)
    if failure == "empty":
        assert page.locator("#uncertain").is_visible()
        assert page.locator("#global-chat button[type=submit]").is_disabled()
    else:
        assert not page.locator("#uncertain").is_visible()
        assert not page.locator("#global-chat textarea").is_disabled()
    screenshot(page, f"voice-{failure}-synthetic")
    context.close()


@pytest.mark.parametrize("invalidated", [False, True])
def test_real_browser_receipt_projects_saved_proof_and_undo(workspace_url, chromium, invalidated):
    context = chromium.new_context(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
    page = context.new_page()
    polls = []
    card = {
        "kind": "applied", "receipt_id": "d" * 24, "title": "Synthetic next action",
        "status": "submitted", "saved": False, "source_status": "current", "undo_available": True,
        "diff": [{"field": "next_action", "before": "Old synthetic step", "after": "New synthetic step"}],
    }

    def respond(route):
        operation = route.request.url.split("/api/tasks/api/")[1]
        if operation == "assist/chat":
            status, body = 200, {"thread_id": "synthetic", "reply": "Synthetic fixture only.", "cards": [card], "suggestions": []}
        elif operation == "assist/receipt":
            polls.append(route.request.post_data_json)
            refreshed = {**card, "status": "executing"} if len(polls) == 1 else {
                **card, "status": "invalidated" if invalidated else "completed",
                "publication_status": "merged", "source_status": "changed" if invalidated else "current",
                "saved": not invalidated,
            }
            status, body = 200, {"cards": [refreshed]}
        else:
            status, body = api_fixture(operation)
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    page.route("**/api/tasks/api/**", respond)
    page.goto(workspace_url + "#tasks")
    page.locator("#command-open").wait_for()
    label_fixture(page)
    page.locator("#command-open").click()
    page.locator("#global-chat textarea").fill("Synthetic source-bound edit.")
    page.locator("#global-chat button[type=submit]").click()
    page.locator(".ai-card.applied").wait_for()
    assert "publication pending" in page.locator(".ai-card .status").inner_text()
    assert page.locator(".ai-card").get_by_role("button", name="Undo", exact=True).count() == 0
    page.wait_for_function("""(invalidated) => {
      const card = document.querySelector('.ai-card.applied');
      return invalidated ? card.textContent.includes('not saved') : card.textContent.includes('Saved to canonical');
    }""", arg=invalidated, timeout=15000)
    assert len(polls) == 2
    assert polls[0] == polls[1], "Polling reuses one UUID and the existing receipt, not a new operation"
    assert polls[0]["receipt_id"] == "d" * 24
    if invalidated:
        assert page.locator(".ai-card .ai-diff dd").count() == 0
        assert page.locator(".ai-card").get_by_role("button", name="Undo", exact=True).count() == 0
    else:
        assert page.locator(".ai-card").get_by_role("button", name="Undo", exact=True).is_visible()
    screenshot(page, f"receipt-{'invalidated' if invalidated else 'saved'}-synthetic")
    context.close()


@pytest.mark.parametrize("scenario", ["same", "changed", "close", "navigate", "withheld", "read_denied", "focus_elsewhere"])
@pytest.mark.parametrize("manual", [False, True])
def test_real_browser_receipt_refresh_preserves_intent_and_composer(workspace_url, chromium, scenario, manual):
    context = chromium.new_context(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce")
    page = context.new_page()
    fixture = {"refresh": False, "overview": None, "task": None, "task_reads": 0}
    card = {
        "kind": "applied", "receipt_id": "f" * 24, "title": "Synthetic edit",
        "status": "submitted", "saved": False, "source_status": "current",
    }

    def respond(route):
        operation = route.request.url.split("/api/tasks/api/")[1]
        if operation == "assist/chat":
            status, body = 200, {"thread_id": "synthetic", "reply": "Synthetic receipt pending.", "cards": [card], "suggestions": []}
        elif operation == "assist/receipt":
            fixture["refresh"] = True
            status, body = 200, {"cards": [{**card, "status": "completed", "saved": True, "publication_status": "merged"}]}
        elif operation.startswith("overview") and fixture["refresh"]:
            fixture["overview"] = route
            return
        elif operation == "task" and fixture["refresh"]:
            fixture["task"] = route
            fixture["task_reads"] += 1
            return
        else:
            if operation == "task":
                fixture["task_reads"] += 1
            status, body = api_fixture(operation)
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    page.route("**/api/tasks/api/**", respond)
    page.goto(workspace_url + "#tasks")
    page.locator("#command-open").wait_for()
    page.locator("#task-list .task-row").click()
    composer = page.locator(".task-copilot textarea")
    composer.fill("A synthetic edit.")
    page.locator(".task-copilot button[type=submit]").click()
    page.locator(".ai-card.applied").wait_for()
    page.wait_for_function("!document.querySelector('.task-copilot button[type=submit]').disabled")
    tab = "edit" if manual else "overview"
    page.locator(f'[data-detail-tab="{tab}"]').click()
    if manual:
        composer = page.locator('#definition-form [name="title"]')
    draft = "My next synthetic instruction is still a draft."
    composer.fill(draft)
    composer.evaluate("(node) => node.setSelectionRange(3, 9)")
    page.locator("#inspector").evaluate("(node) => { node.scrollTop = 120; }")
    scroll = page.locator("#inspector").evaluate("(node) => node.scrollTop")
    if not fixture["overview"]:
        with page.expect_request("**/api/tasks/api/overview?offset=0", timeout=10000):
            pass
        page.wait_for_timeout(30)  # Dispatch the held synthetic route, without releasing its response.
    assert fixture["overview"] is not None
    assert composer.input_value() == draft
    assert composer.evaluate("(node) => node === document.activeElement")
    assert page.locator(f'[data-detail-tab="{tab}"]').get_attribute("aria-pressed") == "true"

    task_reads = fixture["task_reads"]
    _, overview = api_fixture("overview?offset=0")
    if scenario == "close":
        page.keyboard.press("Escape")
    elif scenario == "navigate":
        page.locator('[data-view="activity"]').click()
    elif scenario == "withheld":
        overview["items"] = []
    if scenario not in {"same", "withheld"}:
        overview["items"][0]["revision"] = "b" * 40
    if scenario in {"close", "navigate", "withheld"}:
        fixture["overview"].fulfill(status=200, content_type="application/json", body=json.dumps(overview))
        page.locator("#toast").get_by_text("Synthetic edit · Saved to canonical source", exact=True).wait_for()
        assert fixture["task_reads"] == task_reads, "A closed, hidden or withheld detail must not be reopened by receipt refresh"
        assert page.locator("#inspector .detail-title").count() == 0
        context.close()


        return
    with page.expect_request("**/api/tasks/api/task", timeout=5000):
        fixture["overview"].fulfill(status=200, content_type="application/json", body=json.dumps(overview))
    page.wait_for_timeout(30)
    assert fixture["task"] is not None
    if scenario == "same":
        # A same-source background read must not unmount or blur the live composer while it waits.
        assert composer.evaluate("(node) => node === document.activeElement")
        assert composer.evaluate("(node) => [node.selectionStart, node.selectionEnd]") == [3, 9]
    else:
        assert page.locator("#inspector .detail-title").count() == 0, "Revision drift must clear stale source content before a re-read"
    if scenario == "focus_elsewhere":
        page.locator("#theme-toggle").focus()
    if scenario == "read_denied":
        fixture["task"].fulfill(status=403, content_type="application/json", body=json.dumps({
            "error": "source_not_allowed", "message": "Synthetic source is no longer permitted.",
        }))
        page.locator("#notice").get_by_text("Synthetic source is no longer permitted.", exact=False).wait_for()
        assert page.locator("#inspector .detail-title").count() == 0
        assert page.locator(".task-copilot textarea").count() == 0
    else:
        _, task = api_fixture("task")
        task["revision"] = overview["items"][0]["revision"]
        fixture["task"].fulfill(status=200, content_type="application/json", body=json.dumps(task))
        page.locator("#toast").get_by_text("Synthetic edit · Saved to canonical source", exact=True).wait_for()
        assert composer.input_value() == (task["title"] if manual and scenario != "same" else draft)
        assert page.locator(f'[data-detail-tab="{tab}"]').get_attribute("aria-pressed") == "true"
        if manual and scenario != "same":
            assert "Unsaved manual changes were discarded" in page.locator("#notice").inner_text()
        if scenario == "focus_elsewhere":
            assert page.locator("#theme-toggle").evaluate("(node) => node === document.activeElement")
        elif manual and scenario != "same":
            assert page.locator("#inspector").evaluate("(node) => node === document.activeElement")
        else:
            assert composer.evaluate("(node) => node === document.activeElement")
            assert composer.evaluate("(node) => [node.selectionStart, node.selectionEnd]") == [3, 9]
        assert abs(page.locator("#inspector").evaluate("(node) => node.scrollTop") - scroll) <= 1
    context.close()


@pytest.mark.parametrize("scheme", ["dark", "light"])
def test_real_browser_studio_contrast(workspace_url, chromium, scheme):
    context = chromium.new_context()
    page = context.new_page()
    page.goto(workspace_url + "?mock=1")
    page.locator("#command-open").wait_for()
    if scheme == "light":
        page.locator("#theme-toggle").click()
    colors = page.evaluate("""() => Object.fromEntries(
      ['ink','muted','page','paper','accent','soft','danger','danger-bg','warning','warning-bg','control-line']
        .map(key => [key, getComputedStyle(document.documentElement).getPropertyValue('--' + key).trim()])
    )""")

    def luminance(hex_color):
        channels = [int(hex_color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4 for value in channels]
        return sum(value * weight for value, weight in zip(linear, [.2126, .7152, .0722]))

    for foreground, background, minimum in [
        ("ink", "page", 4.5), ("muted", "page", 4.5), ("muted", "paper", 4.5),
        ("accent", "soft", 4.5), ("danger", "danger-bg", 4.5), ("warning", "warning-bg", 4.5),
        ("control-line", "paper", 3),
    ]:
        light, dark = sorted([luminance(colors[foreground]), luminance(colors[background])], reverse=True)
        contrast = (light + .05) / (dark + .05)
        assert contrast >= minimum, (scheme, foreground, background, contrast)
    context.close()


@pytest.mark.parametrize("width,locale,scheme,motion", [
    (1440, "en-US", "dark", "no-preference"), (1440, "ru-RU", "light", "reduce"),
    (390, "ru-RU", "dark", "reduce"), (390, "en-US", "light", "no-preference"),
])
def test_real_browser_studio_reader_focus_source_text_and_failure(workspace_url, chromium, width, locale, scheme, motion):
    context = chromium.new_context(viewport={"width": width, "height": 1000 if width > 1000 else 844},
                                   locale=locale, reduced_motion=motion)
    page = context.new_page()
    quote = "Synthetic exact evidence: <strong>not executable markup</strong> / Точная тестовая цитата."
    source = {
        "id": "1" * 64, "revision": "a" * 40, "kind": "wiki",
        "path": "wiki/sources/synthetic-evidence.md", "title": "Synthetic evidence",
        "source_dates": {"captured": "2026-10-13"}, "bounded": False,
        "text": "# Synthetic evidence\n\n" + quote + "\n\n" + "\n\n".join(
            f"Synthetic paragraph {i}. This is labelled, public-style test content, not a personal source. "
            "The reader must preserve the original text and expose its source status." for i in range(20)
        ),
    }
    review = {
        "id": "2" * 64, "revision": "b" * 40, "kind": "daily_review",
        "path": "reviews/synthetic.json", "title": "Synthetic daily review", "as_of": "2026-10-13",
        "findings": [{"id": "F1", "basis": "observed", "statement": "Synthetic finding / Тестовый вывод",
                      "next_step": "Inspect the synthetic original.", "evidence": [{**source, "quote": quote}]}],
        "feedback": {"available": False},
    }
    fixture = {"deny": False}

    def respond(route):
        operation = route.request.url.split("/api/tasks/api/")[1]
        if operation.startswith("dashboard/inbox"):
            status, body = 200, {"items": [review], "next_offset": None, "canonical_revision": "c" * 40, "issues": []}
        elif operation == "dashboard/read":
            if fixture["deny"]:
                status, body = 409, {"error": "source_changed", "message": "Synthetic source changed; refresh before continuing."}
            else:
                status, body = 200, source if route.request.post_data_json["id"] == source["id"] else review
        else:
            status, body = api_fixture(operation)
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    page.route("**/api/tasks/api/**", respond)
    page.goto(workspace_url + "#knowledge")
    page.locator("#knowledge-list .source-row").wait_for()
    label_fixture(page)
    if scheme == "light":
        page.locator("#theme-toggle").click()
    row = page.locator("#knowledge-list .source-row")
    row.click()
    page.locator("#knowledge-reader blockquote").wait_for()
    assert page.locator("#knowledge-reader blockquote").inner_text() == quote
    assert page.locator("#knowledge-reader blockquote strong").count() == 0
    assert page.locator("#knowledge-reader").evaluate("(node) => node === document.activeElement")
    assert page.locator("#knowledge-reader").bounding_box()["y"] >= page.locator(".app-header").bounding_box()["height"] - 1
    assert page.locator("#knowledge-reader").bounding_box()["y"] < 160
    page.locator("#knowledge-reader .evidence-block button").click()
    page.locator(".source-prose").wait_for()
    assert quote in page.locator(".source-prose").inner_text()
    assert page.locator(".source-prose strong").count() == 0
    if width == 1440 and motion != "reduce" and page.evaluate("CSS.supports('animation-timeline: scroll()')"):
        page.wait_for_function("new DOMMatrixReadOnly(getComputedStyle(document.querySelector('.reader-progress')).transform).a < .01")
        page.locator("#knowledge-reader").evaluate("(node) => { node.scrollTop = node.scrollHeight; }")
        page.wait_for_function("new DOMMatrixReadOnly(getComputedStyle(document.querySelector('.reader-progress')).transform).a > .99")
        page.locator("#knowledge-reader").evaluate("(node) => { node.scrollTop = 0; }")
    if motion == "reduce":
        assert page.locator(".reader-progress").is_hidden()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    screenshot(page, f"reader-{width}-{scheme}-{locale[:2]}-{motion}")
    page.locator(".reader-back").click()
    assert row.evaluate("(node) => node === document.activeElement")
    fixture["deny"] = True
    row.click()
    page.locator("#knowledge-reader [role=alert]").wait_for()
    assert page.locator("#knowledge-reader .source-prose").count() == 0
    page.keyboard.press("Escape")
    assert row.evaluate("(node) => node === document.activeElement")
    page.locator('[data-view="tasks"]').click()
    settle(page)
    assert ("День задач" if locale.startswith("ru") else "Task day") in page.locator("#snapshot").inner_text()
    context.close()


@pytest.mark.parametrize("width", [390, 1440])
@pytest.mark.parametrize("dismiss", ["knowledge", "today", "back", "escape"])
def test_real_browser_initial_task_load_cannot_outlive_selection(workspace_url, chromium, width, dismiss):
    context = chromium.new_context(viewport={"width": width, "height": 844 if width == 390 else 1000}, reduced_motion="reduce")
    page = context.new_page()
    fixture = {"held": None, "hold": True}

    def respond(route):
        operation = route.request.url.split("/api/tasks/api/")[1]
        if operation == "task" and fixture["hold"]:
            fixture["held"] = route
            return
        status, body = api_fixture(operation)
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    page.route("**/api/tasks/api/**", respond)
    page.goto(workspace_url + "#tasks")
    page.wait_for_function("!document.getElementById('command-open').hidden")
    with page.expect_request("**/api/tasks/api/task"):
        page.locator("#task-list .task-row").click()
    page.wait_for_timeout(30)
    assert fixture["held"] is not None
    assert page.locator("#inspector").get_attribute("aria-busy") == "true"
    assert page.locator("#inspector .mobile-back").is_visible()
    assert page.locator("#inspector").evaluate("(node) => node === document.activeElement")
    if dismiss in {"knowledge", "today"}:
        page.locator(f'[data-view="{dismiss}"]').click()
        page.locator('[data-view="tasks"]').click()
    elif dismiss == "back":
        page.locator("#inspector .mobile-back").click()
    else:
        page.keyboard.press("Escape")
    assert "task-selected" not in (page.locator("#tasks-section").get_attribute("class") or "")
    status, body = api_fixture("task")
    with page.expect_response("**/api/tasks/api/task"):
        fixture["held"].fulfill(status=status, content_type="application/json", body=json.dumps(body))
    page.wait_for_load_state("networkidle")
    assert visible_panels(page) == ["tasks"]
    assert page.locator("#inspector .detail-title").count() == 0
    assert page.locator("#inspector").get_attribute("aria-busy") is None
    fixture["hold"] = False
    page.locator("#task-list .task-row").click()
    page.locator(".task-copilot textarea").wait_for()
    assert page.locator("#inspector .detail-title").inner_text() == body["title"]
    page.locator('[data-view="knowledge"]').click()
    page.locator('[data-view="tasks"]').click()
    assert page.locator("#inspector .detail-title").inner_text() == body["title"], "A fully loaded selection remains available on return"
    context.close()


@pytest.mark.parametrize("width", [390, 1440])
def test_real_browser_voice_stop_keeps_recording_binding_after_navigation(workspace_url, chromium, width):
    context = chromium.new_context(viewport={"width": width, "height": 844 if width == 390 else 1000}, reduced_motion="reduce")
    page = context.new_page()
    calls = []

    def respond(route):
        operation = route.request.url.split("/api/tasks/api/")[1]
        calls.append((operation, route.request))
        status, body = api_fixture(operation)
        if operation == "assist/voice":
            body = {"text": "A synthetic instruction for the original task.", "language": "en", "duration_s": 1}
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    page.route("**/api/tasks/api/**", respond)
    page.goto(workspace_url + "#tasks")
    page.wait_for_function("!document.getElementById('command-open').hidden")
    page.locator("#task-list .task-row").click()
    page.locator(".task-copilot .mic-button").click()
    page.locator('.task-copilot .mic-button[aria-pressed="true"]').wait_for()
    page.locator('[data-view="today"]').click()
    page.wait_for_timeout(1100)  # One fake Chromium media timeslice; no real microphone/audio.
    stop = page.locator("#quick-mic" if width == 390 else "#header-mic")
    assert stop.get_attribute("aria-label") == "Stop recording"
    with page.expect_request("**/api/tasks/api/assist/chat"):
        stop.click()
    page.wait_for_function("!document.querySelector('.task-copilot button[type=submit]').disabled")
    assert sum(operation == "assist/voice" for operation, _ in calls) == 1
    chat = [request for operation, request in calls if operation == "assist/chat"]
    assert len(chat) == 1
    assert chat[0].post_data_json["task"] == {"path": "tasks/synthetic.md", "revision": "a" * 40}
    assert chat[0].post_data_json["voice"] is True
    assert stop.get_attribute("aria-pressed") == "false"
    assert not page.locator("#copilot-dialog").is_visible(), "Stopping a task recording must not switch it into global chat"
    context.close()


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required")
def test_today_consolidation_keeps_every_loaded_attention_cue():
    run_ui_test(r"""
const textTree = (node) => [node.textContent, ...node.children.map(textTree)].join("\n");
const flat = (node) => [node, ...node.children.flatMap(flat)];
state.overview = {
  attention: { calendar_date: "2026-10-13", timezone: "UTC" },
  attention_complete: false, next_offset: 12,
  history: [{ status: "pending", action: {} }, { status: "completed", kind: "prepare_task" }],
};
state.items = Array.from({ length: 11 }, (_, i) => ({
  path: `tasks/synthetic-${i}.md`, title: `Unique synthetic task ${i}.`,
  stage: i === 2 || i === 10 ? "waiting" : "ready",
  attention_eligible: i < 10, attention_reasons: i === 2 ? ["deadline_due", "review_due"] : ["review_due"],
  deadline: i === 2 ? "2026-10-12" : "", review_on: "2026-10-13",
  focus_on: i === 2 ? "2026-10-13" : "",
  waiting_for: i === 2 || i === 10 ? `Exact synthetic dependency ${i}` : "",
  next_action: "A synthetic next step.", definition: { missing: i === 2 ? ["outcome"] : [] },
}));
dashboard.renderToday();
const priorities = flat($("today-priorities")).filter(n => n.dataset.path);
const secondary = flat($("today-work")).filter(n => n.dataset.path);
assert.equal(priorities.length, 3);
assert.equal(priorities[0].dataset.path, "tasks/synthetic-2.md");
const paths = [...priorities, ...secondary].map(n => n.dataset.path);
assert.equal(new Set(paths).size, paths.length, "A task should only be rendered once on Today");
assert.deepEqual([...paths].sort(), state.items.map(task => task.path).sort());
const priorityText = textTree(priorities[0]);
for (const cue of ["2026-10-12", "2026-10-13", "Selected for today", "Waiting", "Exact synthetic dependency 2", "Definition needs work"]) {
  assert.ok(priorityText.includes(cue), cue);
}
const extraText = textTree($("today-work"));
assert.match(extraText, /1 loaded proposal\(s\).*1 prepared result/);
assert.match(extraText, /More tasks or source issues remain/);
assert.match(extraText, /Waiting, not automatically due/);
assert.match(extraText, /Task day 2026-10-13 \/ UTC/);
""")


@pytest.mark.parametrize("width", [390, 1440])
def test_real_browser_manual_forms_survive_locale_and_keep_exact_payloads(workspace_url, chromium, width):
    context = chromium.new_context(viewport={"width": width, "height": 1000 if width == 1440 else 844},
                                   locale="en-US", reduced_motion="reduce")
    page = context.new_page()
    calls, errors = [], []
    fixture = {"expired": False}
    page.on("pageerror", lambda error: errors.append(str(error)))
    _, task = api_fixture("task")
    task.update(area="learning", project="synthetic-project", execution="assisted", review_on="2026-10-13")

    def respond(route):
        operation = route.request.url.split("/api/tasks/api/")[1]
        status, body = api_fixture(operation)
        if fixture["expired"]:
            status, body = 401, {"error": "authentication_required", "message": "Synthetic session expired."}
        elif operation == "task":
            body = task
        elif operation.startswith("overview"):
            body.update(items=[task], areas=["learning"],
                        projects=[{"id": "synthetic-project"}, {"id": "synthetic-alternative"}],
                        active_projects={"synthetic-project": "a" * 40},
                        standing={"enabled": False, "sources": {}})
        elif operation in {"refine", "change", "prepare", "close"}:
            payload = route.request.post_data_json
            calls.append((operation, payload))
            status, body = 200, {
                "id": "b" * 24, "status": "pending", "source_status": "current",
                "kind": {"refine": "refine_task", "change": "edit_task", "prepare": "research", "close": "close_task"}[operation],
                "approval_digest": "c" * 64, "action": payload,
            }
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    page.route("**/api/tasks/api/**", respond)
    page.goto(workspace_url + "#tasks")
    page.wait_for_function("!document.getElementById('command-open').hidden")
    label_fixture(page)
    page.locator("#task-list .task-row").click()
    page.locator('[data-detail-tab="edit"]').click()
    title = page.locator('#definition-form [name="title"]')
    title.fill("UNSAVED SYNTHETIC POLISH DRAFT")
    title.evaluate("(node) => node.setSelectionRange(2, 8, 'backward')")
    page.locator("#language-toggle").click()
    assert title.input_value() == "UNSAVED SYNTHETIC POLISH DRAFT"
    assert title.evaluate("(node) => node === document.activeElement")
    assert title.evaluate("(node) => [node.selectionStart, node.selectionEnd, node.selectionDirection]") == [2, 8, "backward"]
    assert page.locator('[data-detail-tab="edit"]').get_attribute("aria-pressed") == "true"
    assert page.locator("#definition-form label").all_text_contents() == [
        "Название", "Сфера", "Проект", "Этап", "Режим выполнения", "Результат",
        "Следующий шаг", "Критерий готовности", "Ожидание", "Дата пересмотра",
    ]
    page.locator('#definition-form [name="stage"]').select_option("waiting")
    page.locator('#definition-form [name="execution"]').select_option("agent")
    page.locator('#definition-form [name="waiting_for"]').fill("SYNTHETIC DEPENDENCY")
    page.locator('#definition-form [name="review_on"]').fill("2026-10-20")
    page.locator('#date-form [name="field"]').select_option("deadline")
    page.locator('#date-form [name="value"]').fill("2026-10-31")
    page.locator('#date-form input[type="checkbox"]').check()
    screenshot(page, f"polish-manual-{width}-ru", reset_scroll=False)

    page.locator('[data-detail-tab="ai"]').click()
    page.locator('#prepare-form [name="kind"]').select_option("research")
    page.locator('#prepare-form [name="scope"]').fill("Exact synthetic public question / точный вопрос")
    page.locator("#language-toggle").click()
    assert page.locator('#prepare-form [name="kind"]').input_value() == "research"
    assert page.locator('#prepare-form [name="scope"]').input_value() == "Exact synthetic public question / точный вопрос"
    assert "five sources" in page.locator("#prepare-form").inner_text()

    page.locator('[data-detail-tab="close"]').click()
    for name in ["result", "evidence", "learning"]:
        page.locator(f'#completion-form [name="{name}"]').fill(f"Exact synthetic {name}")
    page.locator('#completion-form [name="verified_on"]').fill("2026-10-12")
    page.locator('#completion-form input[type="checkbox"]').check()
    page.locator("#language-toggle").click()
    assert page.locator('#completion-form [name="verified_on"]').input_value() == "2026-10-12"
    assert page.locator('#completion-form input[type="checkbox"]').is_checked()
    assert page.locator("#inspector blockquote").inner_text() == task["done_when"], "Canonical evidence is not translated"
    page.locator("#completion-form button[type=submit]").click()
    page.locator("#proposal-section").wait_for(state="visible")
    assert calls[-1][1]["completion"] == {
        "result": "Exact synthetic result", "evidence": "Exact synthetic evidence",
        "learning": "Exact synthetic learning", "verified_on": "2026-10-12", "verification": "owner",
    }
    assert calls[-1][1]["checked_done_when"] is True
    page.locator("#proposal-close").click()

    page.locator('[data-detail-tab="edit"]').click()
    assert title.input_value() == "UNSAVED SYNTHETIC POLISH DRAFT"
    assert page.locator('#date-form [name="field"]').input_value() == "deadline"
    assert page.locator('#date-form [name="value"]').input_value() == "2026-10-31"
    assert page.locator('#date-form [name="value"]').is_disabled()
    page.locator("#definition-form button[type=submit]").click()
    page.locator("#proposal-section").wait_for(state="visible")
    assert calls[-1][1]["changes"] == {
        "title": "UNSAVED SYNTHETIC POLISH DRAFT", "stage": "waiting", "execution": "agent",
        "waiting_for": "SYNTHETIC DEPENDENCY", "review_on": "2026-10-20",
    }
    page.locator("#proposal-close").click()
    page.locator("#date-form button[type=submit]").click()
    page.locator("#proposal-section").wait_for(state="visible")
    assert calls[-1][1]["change"] == {"deadline": None}
    page.locator("#proposal-close").click()
    page.locator('[data-detail-tab="ai"]').click()
    page.locator("#prepare-form button[type=submit]").click()
    page.locator("#proposal-section").wait_for(state="visible")
    assert calls[-1][1]["kind"] == "research"
    assert calls[-1][1]["scope"] == "Exact synthetic public question / точный вопрос"
    assert all(payload["path"] == task["path"] and payload["revision"] == task["revision"] for _, payload in calls)
    assert all(re.fullmatch(r"[a-f0-9]{32}", payload["request_id"]) for _, payload in calls)
    page.locator("#proposal-close").click()

    page.locator('[data-view="areas"]').click()
    project = page.locator('#project-options input[value="synthetic-alternative"]')
    project.check()
    page.locator("#standing-enabled").check()
    standing_task = page.locator('#standing-options input[name="standing-task"]')
    standing_task.check()
    page.locator("#language-toggle").click()
    assert project.is_checked() and standing_task.is_checked() and page.locator("#standing-enabled").is_checked()
    page.locator("#quick-capture" if width == 390 else "#capture-open").click()
    page.locator("#capture-form summary").click()
    page.locator('#capture-details [name="execution"]').select_option("agent")
    page.locator('#capture-details [name="outcome"]').fill("Exact synthetic capture outcome")
    page.locator("#language-toggle").click()
    assert page.locator('#capture-details [name="execution"]').input_value() == "agent"
    assert page.locator('#capture-details [name="outcome"]').input_value() == "Exact synthetic capture outcome"
    assert "Кто выполнит следующий шаг?" in page.locator("#capture-details").inner_text()
    assert "Агент (нужно разрешение)" in page.locator('#capture-details [name="execution"]').inner_text()
    page.locator("#capture-cancel").click()
    page.locator('[data-view="tasks"]').click()
    page.locator("#inspector .mobile-back").click()
    page.locator("#task-list .task-row").click()
    page.locator('[data-detail-tab="edit"]').click()
    assert title.input_value() == task["title"], "Explicit close keeps cancellation semantics"
    title.fill("SYNTHETIC PRIVATE DRAFT CLEARED ON EXPIRY")
    fixture["expired"] = True
    page.locator("#refresh").click()
    page.locator("#sign-in").wait_for()
    assert page.locator("#access-retry").is_hidden()
    assert "Проверьте историю" in page.locator("#access-message").inner_text()
    assert not page.locator("#inspector").inner_text()
    assert not page.locator("#capture-text").input_value()
    assert page.locator("#capture-details").inner_text() == ""
    assert page.evaluate("localStorage.length + sessionStorage.length") == 0
    assert not errors
    context.close()


def test_real_browser_navigation_arrival_is_not_inherited_scroll(workspace_url, chromium):
    context = chromium.new_context(viewport={"width": 1440, "height": 900}, reduced_motion="reduce")
    page = context.new_page()
    page.goto(workspace_url + "?mock=1")
    page.locator(".priority-card").first.wait_for()
    page.locator("#command-open").wait_for()
    page.evaluate("window.scrollTo(0, 600)")
    assert page.evaluate("scrollY") > 100
    page.locator('[data-view="tasks"]').click()
    settle(page)
    heading, header = page.locator("#view-title").bounding_box(), page.locator(".app-header").bounding_box()
    assert heading["y"] >= header["y"] + header["height"]
    assert heading["y"] + heading["height"] < 900
    assert page.locator("#view-title").evaluate("(node) => node === document.activeElement")
    screenshot(page, "polish-navigation-arrival", reset_scroll=False)
    page.evaluate("window.scrollTo(0, 200)")
    before = page.evaluate("scrollY")
    click_visible_header(page, "#language-toggle")
    settle(page)
    assert abs(page.evaluate("scrollY") - before) <= 1, "Locale-only rendering is not navigation"
    assert page.locator("#language-toggle").evaluate("(node) => node === document.activeElement")
    click_visible_header(page, "#refresh")
    page.wait_for_function("!document.getElementById('load-more').disabled")
    assert abs(page.evaluate("scrollY") - before) <= 1, "A snapshot refresh must not navigate"
    context.close()


@pytest.mark.parametrize("width", [390, 1440])
@pytest.mark.parametrize("locale,scheme", [
    ("en-US", "dark"), ("en-US", "light"), ("ru-RU", "dark"), ("ru-RU", "light"),
])
def test_real_browser_200_percent_text_reflows_controls(workspace_url, chromium, width, locale, scheme):
    context = chromium.new_context(viewport={"width": width, "height": 1000 if width == 1440 else 844},
                                   locale=locale, reduced_motion="reduce")
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(workspace_url + "?mock=1#tasks")
    page.locator("#command-open").wait_for(state="attached")
    page.wait_for_function("!document.getElementById('command-open').hidden")
    if scheme == "light":
        page.locator("#theme-toggle").click()
    page.evaluate("document.documentElement.style.fontSize = '32px'")

    def check_frame():
        page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
        metrics = page.evaluate("""() => ({
          overflow: Math.max(0, document.documentElement.scrollWidth - innerWidth),
          rootSize: getComputedStyle(document.documentElement).fontSize,
          clipped: [...document.querySelectorAll('.header-actions button, .quick-bar button')]
            .filter(n => n.getClientRects().length)
            .filter(n => { const r = n.getBoundingClientRect(); return r.left < 0 || r.right > innerWidth + 1 || r.height < 44 || r.width < 44; })
            .map(n => n.id)
        })""")
        assert metrics == {"overflow": 0, "rootSize": "32px", "clipped": []}, metrics
        if width == 390:
            assert page.locator("#workspace").evaluate("(n) => parseFloat(getComputedStyle(n).paddingBottom)") >= page.locator("#quick-bar").bounding_box()["height"]

    check_frame()
    assert page.locator("#task-list").evaluate("(n) => n.scrollWidth > n.clientWidth"), "Only the board scrolls horizontally"
    screenshot(page, f"polish-text200-{width}-{locale[:2]}-{scheme}", reset_scroll=False)
    page.locator("#task-list .task-row").first.click()
    for tab in ["edit", "ai", "close"]:
        page.locator(f'[data-detail-tab="{tab}"]').click()
        check_frame()
        for control in page.locator("#inspector form input:not([type=checkbox]), #inspector form textarea, #inspector form select").all():
            assert control.evaluate("(n) => parseFloat(getComputedStyle(n).fontSize)") >= 28, "Do not clamp enlarged form text"
            assert control.evaluate("(n) => n.getBoundingClientRect().width <= n.closest('form').getBoundingClientRect().width + 1")
        assert page.locator(".step-rail button").count() == 7
        page.locator(".step-rail button").last.focus()
        assert page.locator(".step-rail button").last.evaluate("(n) => n.getBoundingClientRect().right <= innerWidth + 1")
    screenshot(page, f"polish-text200-detail-{width}-{locale[:2]}-{scheme}", reset_scroll=False)
    page.locator("#inspector .mobile-back").click()
    for view in ["today", "areas", "activity"]:
        page.locator(f'[data-view="{view}"]').click()
        check_frame()
    page.locator("#quick-capture" if width == 390 else "#capture-open").click()
    page.locator("#capture-form summary").click()
    check_frame()
    assert not errors
    context.close()


@pytest.mark.parametrize("failure", ["server", "network"])
def test_real_browser_access_retry_only_for_recoverable_failure(workspace_url, chromium, failure):
    context = chromium.new_context(reduced_motion="reduce")
    page = context.new_page()
    operations = []

    def fail(route):
        operations.append(route.request.url.split("/api/tasks/api/")[1])
        if failure == "network":
            route.abort("failed")
        else:
            route.fulfill(status=503, content_type="application/json", body=json.dumps({
                "error": "unavailable", "message": "Synthetic service unavailable.",
            }))

    page.route("**/api/tasks/api/**", fail)
    page.goto(workspace_url)
    page.locator("#access-retry").wait_for()
    label_fixture(page)
    assert page.locator("#sign-in").is_hidden()
    assert page.locator("#workspace").is_hidden()
    page.locator("#access-retry").click()
    page.locator("#access-retry").wait_for()
    assert operations == ["session", "session"]
    assert page.locator("#task-list .task-row").count() == 0
    context.close()


@pytest.mark.parametrize("locale", ["en-US", "ru-RU"])
@pytest.mark.parametrize("motion", ["reduce", "no-preference"])
def test_real_browser_phone_arrival_keeps_navigation_and_context(workspace_url, chromium, locale, motion):
    context = chromium.new_context(viewport={"width": 390, "height": 844}, locale=locale, reduced_motion=motion)
    page = context.new_page()
    page.goto(workspace_url + "?mock=1")
    page.wait_for_function("!document.getElementById('command-open').hidden")
    settle(page)
    screenshot(page, f"arrival-cold-390-{locale[:2]}-{motion}", reset_scroll=False)

    def assert_navigation_context():
        header = page.locator(".app-header").bounding_box()
        nav = page.locator(".navigation").bounding_box()
        heading = page.locator("#view-title").bounding_box()
        assert nav["y"] >= header["y"] + header["height"], (header, nav)
        assert nav["y"] + nav["height"] <= heading["y"], (nav, heading)
        assert heading["y"] + heading["height"] <= page.locator("#quick-bar").bounding_box()["y"]

    assert_navigation_context()
    assert page.evaluate("scrollY") == 0, "Bootstrap must not scroll to an arrival target"
    assert page.evaluate("document.activeElement.tagName") == "BODY", "Cold loading must not focus the heading"
    for view in ["tasks", "today", "knowledge"]:
        page.locator(f'[data-view="{view}"]').click()
        settle(page)
        assert visible_panels(page) == [view]
        assert_navigation_context()
        assert page.locator("#view-title").evaluate("(node) => node === document.activeElement")
        screenshot(page, f"arrival-{view}-390-{locale[:2]}-{motion}", reset_scroll=False)

    page.locator('[data-view="tasks"]').click()
    settle(page)
    page.locator("#task-list .task-row").first.click()
    page.locator('[data-detail-tab="edit"]').click()
    title = page.locator('#definition-form [name="title"]')
    title.fill("SYNTHETIC RETAINED ARRIVAL DRAFT")
    page.locator('[data-view="today"]').click()
    settle(page)
    page.locator('[data-view="tasks"]').click()
    settle(page)
    assert_navigation_context()
    assert page.locator('[data-detail-tab="edit"]').get_attribute("aria-pressed") == "true"
    assert title.input_value() == "SYNTHETIC RETAINED ARRIVAL DRAFT"
    assert page.locator("#inspector").evaluate("(node) => node === document.activeElement")
    context.close()


@pytest.mark.parametrize("width", [390, 1440])
@pytest.mark.parametrize("delayed_operation", ["session", "overview?offset=0"])
def test_real_browser_delayed_bootstrap_does_not_steal_header_focus(workspace_url, chromium, width, delayed_operation):
    context = chromium.new_context(viewport={"width": width, "height": 844 if width == 390 else 1000},
                                   reduced_motion="reduce")
    page = context.new_page()
    fixture = {"held": None}

    def respond(route):
        operation = route.request.url.split("/api/tasks/api/")[1]
        if operation == delayed_operation:
            fixture["held"] = route
            return
        status, body = api_fixture(operation)
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    page.route("**/api/tasks/api/**", respond)
    with page.expect_request("**/api/tasks/api/" + delayed_operation):
        page.goto(workspace_url + "#tasks")
    label_fixture(page)
    control = page.locator("#theme-toggle")
    control.focus()
    assert fixture["held"] is not None
    before = page.evaluate("scrollY")
    status, body = api_fixture(delayed_operation)
    fixture["held"].fulfill(status=status, content_type="application/json", body=json.dumps(body))
    page.wait_for_function("!document.getElementById('command-open').hidden")
    settle(page)
    assert control.evaluate("(node) => node === document.activeElement"), "An async initial read stole header focus"
    assert control.evaluate("(node) => node.matches(':focus-visible')"), "Keyboard focus must stay visible"
    assert page.evaluate("scrollY") == before, "An async initial read scrolled the page"
    assert visible_panels(page) == ["tasks"]
    context.close()
