"""One deliberate owner capture, synthetic writer receipts, and no external effects."""

from __future__ import annotations

import copy
import json
import shutil
from unittest.mock import Mock

import httpx
import pytest

from briefing_actions import ActionGateway
from briefing_state import StateError, prune_state
from task_service import TaskError, TaskService
from task_state import workspace
from test_closure_scan_ui import run_ui_test
from test_task_auth import auth_env, identity
from test_task_service import PROJECT, TODAY, service
from test_task_web import app, data, logged_in, request
from test_task_workspace_ui import api_fixture, chromium, screenshot, workspace_url


SHOPPING = "Buy study supplies:\n- a desk mat\n- a storage basket"


def capture_payload(**changes):
    return {
        "request_id": "9" * 32, "text": SHOPPING,
        "definition": {"title": "Synthetic study supplies", "stage": "clarify"},
        **changes,
    }


def test_one_step_capture_keeps_two_items_in_one_rough_task_without_model_or_execution():
    subject, _, execute, generate, publish, _ = service()
    result = subject.save_capture(capture_payload(), TODAY)
    assert result["status"] == "submitted"
    assert result["approved_by"] == "owner-capture"
    assert result["action"] == {
        "kind": "create_task", "text": SHOPPING, "definition": capture_payload()["definition"],
    }
    assert execute.call_count == publish.call_count == 1
    assert execute.call_args.args[1] is False
    generate.assert_not_called()
    saved = subject.store.read()
    assert len(saved["proposals"]) == len(workspace(saved)["requests"]) == 1
    assert not workspace(saved)["standing"]["enabled"]
    assert workspace(saved)["budget"]["used"] == 0


def test_optional_capture_definition_is_preserved_without_authorizing_an_agent():
    subject, _, execute, generate, _, _ = service()
    definition = {
        "title": "Synthetic study supplies", "area": "home", "project": PROJECT,
        "execution": "agent", "outcome": "Two study supplies available.",
        "next_action": "Compare the listed supplies.", "done_when": "Check both supplies.",
    }
    result = subject.save_capture(capture_payload(definition=definition), TODAY)
    assert result["action"]["definition"] == {**definition, "stage": "clarify"}
    assert execute.call_count == 1 and execute.call_args.args[0]["action"]["kind"] == "create_task"
    generate.assert_not_called()


def test_same_capture_request_is_not_reexecuted_and_body_cannot_be_replaced():
    subject, repository, execute, _, publish, _ = service()
    first = subject.save_capture(capture_payload(), TODAY)
    repository.records["tasks/README.md"]["revision"] = "f" * 40
    again = subject.save_capture(capture_payload(), TODAY)
    assert again["id"] == first["id"]
    assert execute.call_count == 1
    assert publish.call_args_list[0].args[1] == publish.call_args_list[1].args[1]
    with pytest.raises(TaskError, match="task_request_conflict"):
        subject.save_capture(capture_payload(text="A different synthetic task."), TODAY)
    assert len(subject.store.read()["proposals"]) == 1


@pytest.mark.parametrize("extra", [
    {"stage": "ready"}, {"stage": "backlog"}, {"deadline": "2026-10-20"},
    {"focus_on": "2026-10-20"}, {"context": "Unverified source evidence."},
    {"subtasks": ["Split the capture."]},
])
def test_one_step_route_cannot_expand_capture_scope(extra):
    subject, _, execute, generate, publish, _ = service()
    with pytest.raises(TaskError):
        subject.save_capture(capture_payload(definition={**capture_payload()["definition"], **extra}), TODAY)
    assert not subject.store.read()["proposals"]
    execute.assert_not_called()
    publish.assert_not_called()
    generate.assert_not_called()


@pytest.mark.parametrize("extra", [
    {"proposal_id": "a" * 24}, {"source": "wiki/sources/synthetic.md"},
    {"revision": "a" * 40}, {"decision": "approve"}, {"action": {"kind": "research"}},
])
def test_save_route_accepts_no_source_pointer_existing_action_or_approval_override(extra):
    subject, _, execute, _, _, _ = service()
    with pytest.raises(TaskError, match="task_request_invalid"):
        subject.save_capture({**capture_payload(), **extra}, TODAY)
    execute.assert_not_called()


def test_legacy_capture_keeps_approval_and_cannot_be_upgraded_by_reusing_its_request_id():
    subject, _, execute, _, publish, _ = service()
    legacy = subject.capture(capture_payload(), TODAY)
    assert legacy["status"] == "pending"
    assert "approved_by" not in legacy
    with pytest.raises(TaskError, match="task_request_conflict"):
        subject.save_capture(capture_payload(), TODAY)
    execute.assert_not_called()
    publish.assert_not_called()


def test_save_recovery_retains_pending_claim_checks_and_privacy_tombstones():
    subject, repository, execute, _, publish, blob = service()
    decide = subject.decide
    subject.decide = Mock(side_effect=StateError("state_save_failed"))
    with pytest.raises(StateError):
        subject.save_capture(capture_payload(), TODAY)
    subject.decide = decide
    pending = next(iter(subject.store.read()["proposals"].values()))
    repository.records["tasks/README.md"]["revision"] = "f" * 40
    result = subject.save_capture(capture_payload(), TODAY)
    assert result["id"] == pending["id"] and result["status"] == "invalidated"
    assert "action" not in result
    execute.assert_not_called()
    publish.assert_not_called()
    subject.store.update(lambda state: prune_state(state, inventory_paths=set(), today=TODAY))
    tombstone = blob.saved()["proposals"][pending["id"]]
    assert tombstone["invalidation_reason"] == "source_removed" and "action" not in tombstone
    assert subject.save_capture(capture_payload(), TODAY)["id"] == pending["id"]
    with pytest.raises(TaskError, match="task_request_conflict"):
        subject.save_capture(capture_payload(text="A replaced synthetic request."), TODAY)


@pytest.mark.parametrize("initial", ["submitted", "in_progress", "lost"])
def test_delayed_writer_completion_recovers_the_same_operation_to_current_canonical(initial, caplog):
    subject, repository, _, generate, _, _ = service()
    packets = []
    publication_ids = []
    created = {}

    def writer(req):
        packet = json.loads(req.content)
        packets.append(packet)
        identifier = packet["action_id"]
        if packet["kind"] == "create_task":
            created.update({
                "version": 1, "action_id": identifier, "status": "submitted",
                "path": f"tasks/{TODAY.isoformat()}-action-{identifier}.md",
                "source_revision": "8" * 40,
                "pr_url": "https://github.com/example/mindVault/pull/7",
            })
            if initial == "lost":
                raise httpx.ReadTimeout("Synthetic lost writer response", request=req)
            return httpx.Response(202, json={**created, "status": initial})
        assert packet["kind"] == "publish_task"
        assert packet["target_action_id"] == created["action_id"]
        assert set(packet) == {"version", "vault_id", "chat_id", "action_id", "kind", "target_action_id"}
        publication_ids.append(identifier)
        if len(publication_ids) == 1:
            return httpx.Response(202, json={
                **created, "action_id": identifier, "target_action_id": created["action_id"],
                "status": "in_progress",
            })
        repository.records[created["path"]] = {"path": created["path"], "revision": "8" * 40}
        return httpx.Response(200, json={
            **created, "action_id": identifier, "target_action_id": created["action_id"],
            "status": "merged", "source_revision": "8" * 40, "canonical_revision": "7" * 40,
        })

    with httpx.Client(transport=httpx.MockTransport(writer)) as client:
        gateway = ActionGateway(
            client=client, token="synthetic", repo="example/mindVault",
            memex_url="https://synthetic.example/api/personal_action", chat_id=7, task_enabled=lambda: True,
        )
        subject.loop.execute = gateway
        subject = TaskService(
            loop=subject.loop, repository=repository, generate=generate, publish=gateway.publish_task,
            attention_timezone=subject.attention_timezone, clock=subject.clock,
        )
        first = subject.save_capture(capture_payload(), TODAY)
        assert first["status"] in {"submitted", "executing", "uncertain"}
        repository.records["tasks/README.md"]["revision"] = "f" * 40
        recovered = subject.save_capture(capture_payload(), TODAY)
        if recovered["status"] != "completed":
            recovered = subject.reconcile(first["id"], TODAY)
        assert recovered["id"] == first["id"]
        assert recovered["status"] == "completed" and recovered["source_status"] == "current"
        assert recovered["publish_result"]["status"] == "merged"
        assert subject.save_capture(capture_payload(), TODAY) == recovered
        assert sum(packet["kind"] == "create_task" for packet in packets) == 1
        assert len(set(publication_ids)) == 1 and publication_ids[0] != created["action_id"]
        assert packets[0]["text"] == SHOPPING
        assert len(subject.store.read()["proposals"]) == 1
        generate.assert_not_called()
        assert SHOPPING not in caplog.text


def test_simultaneous_capture_save_requests_share_the_atomic_claim():
    subject, _, execute, _, publish, blob = service()
    results = []
    blob.before_upload = lambda _: results.append(subject.save_capture(capture_payload(), TODAY))
    results.append(subject.save_capture(capture_payload(), TODAY))
    assert results[0]["id"] == results[1]["id"]
    assert execute.call_count == 1
    assert len({call.args[1] for call in publish.call_args_list}) == 1
    assert len(subject.store.read()["proposals"]) == len(workspace(subject.store.read())["requests"]) == 1


def test_capture_save_still_verifies_action_digest_in_atomic_claim():
    subject, _, execute, _, publish, blob = service()
    decide = subject.decide

    def changed(payload, today):
        state = blob.saved()
        state["proposals"][payload["proposal_id"]]["action"]["definition"]["title"] = "An altered synthetic title"
        blob.replace(state)
        return decide(payload, today)

    subject.decide = changed
    with pytest.raises(TaskError, match="task_approval_changed"):
        subject.save_capture(capture_payload(), TODAY)
    execute.assert_not_called()
    publish.assert_not_called()


def test_web_save_is_one_owner_post_and_legacy_route_remains_prepare_only(logged_in):
    auth, cookie, csrf = logged_in
    subject, _, execute, generate, _, _ = service()
    web = app(auth, lambda: subject)
    result = web.handle(request("api/capture-save", capture_payload(), cookie=cookie, csrf=csrf), "api/capture-save")
    assert result.status_code == 200 and data(result)["status"] == "submitted"
    assert result.headers["Cache-Control"] == "no-store, private"
    assert execute.call_count == 1
    legacy = web.handle(request(
        "api/capture", capture_payload(request_id="8" * 32), cookie=cookie, csrf=csrf,
    ), "api/capture")
    assert legacy.status_code == 200 and data(legacy)["status"] == "pending"
    assert execute.call_count == 1
    generate.assert_not_called()


def test_web_save_cannot_skip_owner_csrf_origin_feature_or_method_boundaries(logged_in):
    auth, cookie, csrf = logged_in
    factory = Mock()
    web = app(auth, factory)
    for credentials, expected in [
        ({}, 401), ({"cookie": cookie, "csrf": "forged"}, 403),
        ({"cookie": cookie, "csrf": csrf, "origin": "https://attacker.example"}, 403),
    ]:
        result = web.handle(request("api/capture-save", capture_payload(), **credentials), "api/capture-save")
        assert result.status_code == expected
    factory.assert_not_called()
    web.env["MINDME_TASKS_ENABLED"] = "false"
    assert web.handle(request(
        "api/capture-save", capture_payload(), cookie=cookie, csrf=csrf,
    ), "api/capture-save").status_code == 503
    factory.assert_not_called()
    web.env["MINDME_TASKS_ENABLED"] = "true"
    assert web.handle(request("api/capture-save", cookie=cookie), "api/capture-save").status_code == 405


def test_post_claim_capture_error_requires_same_request_recovery_not_field_resubmission(logged_in):
    auth, cookie, csrf = logged_in
    subject, _, execute, _, publish, _ = service()
    publish.return_value = {"status": "invalid"}
    web = app(auth, lambda: subject)
    first = web.handle(request("api/capture-save", capture_payload(), cookie=cookie, csrf=csrf), "api/capture-save")
    assert first.status_code == 503 and data(first)["error"] == "task_capture_unconfirmed"
    assert execute.call_count == 1
    publish.return_value = {"status": "submitted"}
    recovered = web.handle(request("api/capture-save", capture_payload(), cookie=cookie, csrf=csrf), "api/capture-save")
    assert recovered.status_code == 200 and data(recovered)["status"] == "submitted"
    assert execute.call_count == 1
    assert publish.call_args_list[0].args[1] == publish.call_args_list[1].args[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required")
def test_capture_status_needs_completed_merged_current_proof_and_never_exposes_main_json():
    run_ui_test(r"""
const flat = (item) => [item, ...item.children.flatMap(flat)];
const record = {
  id: "a".repeat(24), kind: "capture_task", status: "submitted", source_status: "current",
  approved_by: "owner-capture", approval_digest: "b".repeat(64),
  action: { kind: "create_task", text: "For the study:\n- a desk mat\n- a storage basket",
    definition: { title: "Synthetic study supplies", stage: "clarify" } },
};
assert.equal(proposalStatusText(record), "Your request was received. We are checking that the task was saved; no need to add it again.");
for (const unconfirmed of [
  { status: "completed" },
  { status: "completed", publish_result: { status: "submitted" } },
  { status: "completed", publish_result: { status: "merged" }, source_status: "unavailable" },
  { status: "uncertain", publish_result: { status: "merged" } },
]) {
  const value = { ...record, ...unconfirmed };
  assert.match(proposalStatusText(value), /not confirmed/i);
  assert.notEqual(renderProposal(value).firstElementChild.textContent, "Task saved");
}
const saved = { ...record, status: "completed", publish_result: { status: "merged" } };
assert.equal(proposalStatusText(saved), "Added to your task list in Clarify. No work was started.");
assert.equal(renderProposal(saved).firstElementChild.textContent, "Task saved");
const rendered = renderProposal(record);
assert.ok(flat(rendered).some((part) => part.textContent === record.action.definition.title));
assert.ok(flat(rendered).some((part) => part.textContent === record.action.text));
assert.ok(!flat(rendered).some((part) => part.textContent === "Exact action to approve"));
assert.ok(!flat(rendered).some((part) => part.events.click && /approve/i.test(part.textContent)));
const legacy = { ...record, status: "pending" }; delete legacy.approved_by;
assert.ok(flat(renderProposal(legacy)).some((part) => part.events.click && /Approve/.test(part.textContent)));
assist.language = "ru";
assert.equal(proposalStatusText(record), "Запрос получен. Проверяем, что задача сохранилась; добавлять её повторно не нужно.");
assert.equal(proposalStatusText(saved), "Добавлено в ваш список задач: «Уточнить». Работа не запускалась.");
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required")
@pytest.mark.parametrize("completion_after_ms", [76000, None])
def test_capture_polling_covers_delayed_publication_without_another_create_and_stays_bounded(completion_after_ms):
    run_ui_test(r"""
(async () => {
  const timers = new Map();
  let timerId = 0, clock = 0;
  setTimeout = (fn, delay) => { timers.set(++timerId, { fn, at: clock + delay }); return timerId; };
  clearTimeout = (id) => timers.delete(id);
  state.csrf = "synthetic";
  const record = {
    id: "a".repeat(24), kind: "capture_task", status: "submitted", source_status: "current",
    approved_by: "owner-capture", action: { kind: "create_task", text: "Synthetic task",
      definition: { title: "Synthetic task", stage: "clarify" } },
  };
  const calls = [];
  const completionAfter = fixtures[0].completion_after_ms;
  api = async (operation, payload) => {
    calls.push({ operation, payload: { ...payload }, at: clock });
    if (operation === "capture-save") return structuredClone(record);
    assert.equal(operation, "reconcile");
    assert.deepEqual(payload, { proposal_id: record.id });
    return completionAfter !== null && clock >= completionAfter
      ? { ...record, status: "completed", publish_result: { status: "merged" } }
      : structuredClone(record);
  };
  loadOverview = async () => {};
  FormData = class { get(name) { return ["text", "title"].includes(name) ? "Synthetic task" : ""; } };
  const runMutation = mutate;
  let initialRequest;
  mutate = (...args) => { initialRequest = runMutation(...args); return initialRequest; };
  $("capture-form").events.submit({ preventDefault() {}, currentTarget: $("capture-form") });
  await initialRequest;
  assert.equal(calls.length, 1);
  assert.equal(calls[0].operation, "capture-save");
  assert.equal(calls[0].payload.text, "Synthetic task");
  const checkTimes = [2000, 6000, 14000, 30000, 62000, 122000];
  for (const at of checkTimes) {
    assert.equal(timers.size, 1, `Missing automatic check at ${at}ms`);
    const [id, timer] = timers.entries().next().value;
    assert.equal(timer.at, at);
    timers.delete(id); clock = at;
    await timer.fn();
    if (completionAfter === null || at < completionAfter) {
      assert.equal(captureSaved(state.proposal), false);
      assert.notEqual($("proposal-preview").firstElementChild.firstElementChild.textContent, "Task saved");
    }
  }
  assert.equal(timers.size, 0);
  assert.deepEqual(calls.map((call) => call.operation), ["capture-save", ...Array(6).fill("reconcile")]);
  assert.deepEqual(calls.slice(1).map((call) => call.at), checkTimes);
  assert.ok(calls.slice(1).every((call) => call.payload.proposal_id === record.id));
  assert.equal(state.capturePoll.attempt, 6);
  if (completionAfter !== null) {
    assert.equal(captureSaved(state.proposal), true);
    assert.equal($("proposal-preview").firstElementChild.firstElementChild.textContent, "Task saved");
    assert.equal(state.captureReceipt, null);
  } else {
    assert.equal(captureUnresolved(), true);
    $("capture-form").events.submit({ preventDefault() {}, currentTarget: $("capture-form") });
    assert.equal(calls.length, 7, "Unconfirmed capture cannot create a replacement");
    showProposal(record);
    assert.equal(timers.size, 0, "Reentry must not restart the automatic check budget");
    state.sessionGeneration += 1;
    await pollCaptureReceipt(state.capturePoll);
    assert.equal(calls.length, 7);
  }
})()
""", [{"completion_after_ms": completion_after_ms}])


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required")
@pytest.mark.parametrize("stop", ["hidden", "closed", "session", "failed", "invalidated", "declined", "expired"])
def test_capture_polling_keeps_pause_and_terminal_guards(stop):
    run_ui_test(r"""
(async () => {
  const timers = new Map();
  let timerId = 0;
  setTimeout = (fn) => { timers.set(++timerId, fn); return timerId; };
  clearTimeout = (id) => timers.delete(id);
  state.csrf = "synthetic";
  const record = {
    id: "a".repeat(24), kind: "capture_task", status: "submitted", source_status: "current",
    approved_by: "owner-capture", action: { kind: "create_task", text: "Synthetic task",
      definition: { title: "Synthetic task", stage: "clarify" } },
  };
  state.captureReceipt = record;
  const stop = fixtures[0].stop;
  const calls = [];
  api = async (operation, payload) => {
    calls.push({ operation, payload });
    return { ...record, status: stop };
  };
  loadOverview = async () => {};
  showProposal(record);
  const [id, run] = timers.entries().next().value;
  if (stop === "hidden") document.hidden = true;
  if (stop === "closed") $("proposal-close").events.click();
  if (stop === "session") state.sessionGeneration += 1;
  timers.delete(id);
  await run();
  assert.equal(timers.size, 0);
  assert.equal(calls.length, ["hidden", "closed", "session"].includes(stop) ? 0 : 1);
  if (calls.length) assert.deepEqual(calls[0], { operation: "reconcile", payload: { proposal_id: record.id } });
  assert.equal(captureSaved(state.proposal), false);
})()
""", [{"stop": stop}])


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required")
def test_source_capture_form_keeps_review_and_never_calls_the_ordinary_save_route():
    run_ui_test(r"""
const source = { id: "d".repeat(24), revision: "e".repeat(40), title: "Synthetic source" };
const finding = { id: "F1", next_step: "Compare synthetic options." };
$("capture-text").focus = () => {};
FormData = class { get(name) { return { text: finding.next_step, title: "Synthetic comparison" }[name] || ""; } };
const calls = [];
mutate = (operation, payload) => { calls.push({ operation, payload }); };
beginSourceCapture(source, finding);
assert.equal($("capture-submit").textContent, "Review capture");
assert.match($("capture-hint").textContent, /source-bound/);
$("capture-form").events.submit({ preventDefault() {}, currentTarget: $("capture-form") });
assert.equal(calls.length, 1);
assert.equal(calls[0].operation, "dashboard/capture");
assert.equal(calls[0].payload.id, source.id);
assert.equal(calls[0].payload.revision, source.revision);
assert.equal(calls[0].payload.finding, finding.id);
assert.ok(!Object.hasOwn(calls[0].payload, "decision"));
state.unknown = { operation: "dashboard/capture", payload: calls[0].payload };
const heldSource = state.captureSource;
$("capture-open").events.click();
beginSourceCapture({ ...source, id: "f".repeat(24) }, finding);
assert.equal(state.captureSource, heldSource, "Unknown capture reentry cannot replace its source binding");
assert.equal(calls.length, 1);
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required")
@pytest.mark.parametrize("text", ["- a desk mat\n- a storage basket", "1. a desk mat\n2. a storage basket"])
def test_bare_list_capture_derives_a_valid_title_without_rewriting_or_splitting_text(text):
    payload = capture_payload(text=text, definition={"title": "a desk mat", "stage": "clarify"})
    run_ui_test(r"""
FormData = class { get(name) { return name === "text" ? fixtures[0].text : ""; } };
const calls = [];
mutate = (operation, payload) => { calls.push({ operation, payload }); };
$("capture-form").events.submit({ preventDefault() {}, currentTarget: $("capture-form") });
assert.equal(calls.length, 1);
assert.equal(calls[0].operation, "capture-save");
assert.equal(calls[0].payload.text, fixtures[0].text);
assert.deepEqual(calls[0].payload.definition, { ...fixtures[0].definition });
""", [payload])
    subject, _, execute, generate, _, _ = service()
    assert subject.save_capture(payload, TODAY)["status"] == "submitted"
    assert execute.call_count == 1 and execute.call_args.args[0]["action"]["text"] == text
    generate.assert_not_called()


@pytest.mark.parametrize("width,locale", [(1440, "en-US"), (390, "ru-RU")])
@pytest.mark.parametrize("lost_response", [False, True])
def test_browser_one_click_capture_recovers_same_request_and_reads_delayed_publication(
    workspace_url, chromium, width, locale, lost_response,
):
    context = chromium.new_context(
        viewport={"width": width, "height": 1000 if width == 1440 else 844},
        locale=locale, reduced_motion="reduce",
    )
    page = context.new_page()
    calls = []
    receipt = None

    def respond(route):
        nonlocal receipt
        operation = route.request.url.split("/api/tasks/api/")[1]
        payload = route.request.post_data_json if route.request.method == "POST" else None
        calls.append((operation, copy.deepcopy(payload)))
        if operation == "capture-save":
            receipt = {
                "id": "a" * 24, "kind": "capture_task", "status": "submitted",
                "approved_by": "owner-capture", "source_status": "current", "approval_digest": "b" * 64,
                "action": {"kind": "create_task", "text": payload["text"], "definition": payload["definition"]},
            }
            if lost_response and sum(name == "capture-save" for name, _ in calls) == 1:
                route.abort("failed")
                return
            status, body = 200, receipt
        elif operation == "reconcile":
            assert payload == {"proposal_id": "a" * 24}
            completed = sum(name == "reconcile" for name, _ in calls) >= 2
            receipt = {**receipt, "status": "completed" if completed else "uncertain"}
            if completed:
                receipt["publish_result"] = {"status": "merged"}
            status, body = 200, receipt
        else:
            status, body = api_fixture(operation, assist_enabled=False)
            if operation.startswith("overview") and receipt:
                body["history"] = [receipt]
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    page.route("**/api/tasks/api/**", respond)
    page.goto(workspace_url + "#tasks")
    entry = "#quick-capture" if width == 390 else "#capture-open"
    page.locator(entry).click()
    page.locator("#capture-text").fill(SHOPPING)
    label = "Добавить задачу" if locale.startswith("ru") else "Add task"
    assert page.locator("#capture-submit").inner_text() == label
    page.locator("#capture-form summary").click()
    page.locator('#capture-details [name="execution"]').select_option("agent")
    page.locator('#capture-details [name="outcome"]').fill("Both synthetic supplies available.")
    page.locator("#capture-submit").click()
    # A second submit event can occur before a button's disabled state is painted.
    page.locator("#capture-form").evaluate(
        "(form) => form.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true}))"
    )
    if lost_response:
        page.locator("#uncertain").wait_for(state="visible")
        assert page.locator("#capture-text").input_value() == SHOPPING
        assert page.locator("#capture-submit").is_disabled()
        page.locator(entry).click()
        assert page.locator("#capture-text").input_value() == SHOPPING
        assert len([call for call in calls if call[0] == "capture-save"]) == 1
        page.locator("#recover-request").click()
    page.locator("#proposal-section").wait_for(state="visible")
    assert page.locator("#proposal-heading").inner_text() == ("Запись задачи" if locale.startswith("ru") else "Task capture")
    assert page.locator("#proposal-preview h3").inner_text() == "Buy study supplies:"
    assert page.locator("#proposal-preview .outcome-block p").inner_text() == SHOPPING
    assert page.locator("#proposal-preview .exact-action").get_attribute("open") is None
    assert "create_task" not in page.locator("#proposal-preview").inner_text()
    assert not [call for call in calls if call[0] in {"capture", "decide", "assist/chat", "clarify"}]
    page.locator("#proposal-close").click()
    page.locator(entry).click()
    assert page.locator("#capture-section").is_hidden(), "An unresolved save must reopen its receipt, not a new form"
    page.wait_for_function(
        "() => ['Task saved', 'Задача сохранена'].includes(document.querySelector('#proposal-preview .status')?.textContent)",
        timeout=20000,
    )
    captures = [payload for operation, payload in calls if operation == "capture-save"]
    assert len(captures) == (2 if lost_response else 1)
    assert all(payload == captures[0] for payload in captures)
    assert captures[0]["text"] == SHOPPING
    assert captures[0]["definition"]["stage"] == "clarify"
    assert captures[0]["definition"]["execution"] == "agent"
    assert captures[0]["definition"]["outcome"] == "Both synthetic supplies available."
    assert len([call for call in calls if call[0] == "reconcile"]) == 2
    assert page.evaluate("document.activeElement.id") == "proposal-heading", "Polling must not move focus"
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert page.evaluate("localStorage.length === 0 && sessionStorage.length === 0")
    screenshot(page, f"capture-save-{width}-{locale[:2]}-{'recovered' if lost_response else 'submitted'}", reset_scroll=False)
    receipt = {
        "id": receipt["id"], "kind": "capture_task", "status": "completed",
        "approved_by": "owner-capture", "source_status": "changed_or_removed",
    }
    page.locator("#refresh").click()
    page.wait_for_function("!document.querySelector('#proposal-preview .outcome-block')")
    page.locator(entry).click()
    assert page.locator("#capture-section").is_visible(), "Later edits to a confirmed capture must not block new tasks"
    assert page.locator("#capture-text").input_value() == ""
    context.close()


@pytest.mark.parametrize("failure", ["validation", "incomplete", "incomplete_record"])
def test_browser_capture_failure_keeps_input_until_a_durable_receipt(workspace_url, chromium, failure):
    context = chromium.new_context(reduced_motion="reduce")
    page = context.new_page()
    captures = []

    def respond(route):
        operation = route.request.url.split("/api/tasks/api/")[1]
        if operation == "capture-save":
            captures.append(route.request.post_data_json)
            status, body = (422, {"error": "task_request_rejected", "message": "Synthetic field rejected."}) if failure == "validation" else (200, {})
            if failure == "incomplete_record":
                body = {
                    "id": "a" * 24, "kind": "capture_task", "status": "completed", "source_status": "current",
                    "approved_by": "owner-capture", "action": {"kind": "create_task", "text": SHOPPING, "definition": {}},
                }
        else:
            status, body = api_fixture(operation, assist_enabled=False)
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    page.route("**/api/tasks/api/**", respond)
    page.goto(workspace_url + "#tasks")
    page.locator("#capture-open").click()
    page.locator("#capture-text").fill(SHOPPING)
    page.locator("#capture-title-input").fill("Synthetic supplied title")
    page.locator("#capture-form summary").click()
    page.locator('#capture-details [name="done_when"]').fill("Check the two synthetic supplies.")
    page.locator("#capture-submit").click()
    page.wait_for_function("document.querySelector('#notice').classList.contains('error')")
    assert page.locator("#capture-text").input_value() == SHOPPING
    assert page.locator("#capture-title-input").input_value() == "Synthetic supplied title"
    assert page.locator('#capture-details [name="done_when"]').input_value() == "Check the two synthetic supplies."
    assert page.locator("#capture-section").is_visible()
    assert page.locator("#proposal-section").is_hidden()
    if failure == "validation":
        assert page.locator("#capture-submit").is_enabled()
    else:
        assert page.locator("#capture-submit").is_disabled()
        page.locator("#recover-request").click()
        page.wait_for_function("!document.querySelector('#recover-request').disabled")
        assert len(captures) == 2 and captures[0] == captures[1]
    context.close()
