"""Exercise real closure controls offline; rendering/recovery never schedules a next pass."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from test_closure_scan import KNOWN_CONFLICT_CODES, UNKNOWN_CONFLICT_CODES, closure_payload, system
from test_task_service import PATH, TODAY, approve


pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node is required for the static UI control test")


def run_ui_test(tests: str, fixtures: list[dict[str, object]] | None = None) -> None:
    script = r"""
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const source = fs.readFileSync(process.argv[2], "utf8").replace(
  'import { Dashboard } from "./dashboard.js";', ""
).replace(/\nstart\(\);\s*$/, "\n");
const dashboardSource = fs.readFileSync(path.join(path.dirname(process.argv[2]), "dashboard.js"), "utf8")
  .replace("export class Dashboard", "class Dashboard");
assert.ok(!source.endsWith("\nstart();\n"));
function node() {
  return {
    textContent: "", children: [], disabled: false, hidden: false, value: "", dataset: {}, events: {},
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    get firstElementChild() { return this.children[0]; },
    classList: { remove() {}, add() {} },
    addEventListener(name, callback) { this.events[name] = callback; },
    setAttribute(name, value) { this[name] = value; },
    querySelector() { return null; },
    scrollIntoView() {},
    reset() {},
  };
}
const nodes = new Map();
const document = {
  getElementById(id) { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); },
  createElement() { return node(); },
  querySelectorAll() { return []; },
  addEventListener() {},
};
"""
    script += "\nconst tests = " + json.dumps(tests) + ";\n"
    script += "\nconst fixtures = " + json.dumps(fixtures or []) + ";\n"
    script += r"""
const bundledDashboard = "const Dashboard = (() => {\n" + dashboardSource + "\nreturn Dashboard;\n})();\n";
Promise.resolve(vm.runInNewContext(bundledDashboard + source + tests, {
  document, window: { addEventListener() {} }, assert, structuredClone, console, URL, clearTimeout() {}, fixtures,
  crypto: { randomUUID() { return "01234567-89ab-cdef-0123-456789abcdef"; } },
})).catch((error) => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run(
        [shutil.which("node"), "-", str(Path(__file__).parents[1] / "web" / "tasks.js")],
        input=script, text=True, encoding="utf-8", capture_output=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_explicit_closure_controls_and_same_request_recovery():
    run_ui_test(r"""
(async () => {
  const id = "a".repeat(24);
  const firstToken = "1".repeat(32);
  const record = {
    id, kind: "close_task", closure_scan_protocol: 1, task_workspace: true,
    status: "executing", source_status: "current", approved_on: "2026-10-12",
    action: { kind: "update_task", change: { status: "done" } },
    result: { version: 1, action_id: "b".repeat(32), status: "in_progress", path: "",
      phase: "closure_scan", resumable: true, closure_continuation: firstToken, scan_pass: 1, scan_pass_limit: 6 },
  };
  const flat = (item) => [item, ...item.children.flatMap(flat)];
  const controls = (item) => flat(item).filter((part) => part.events.click);
  const calls = [];
  let returned;
  updateProposal = async (value) => { returned = value; };
  api = async (operation, payload) => {
    calls.push({ operation, payload: structuredClone(payload) });
    return { ...record, result: { ...record.result, closure_continuation: "2".repeat(32), scan_pass: 2 } };
  };
  let rendered = renderProposal(record);
  assert.equal(calls.length, 0);
  assert.match(proposalStatusText(record), /task is still open/);
  assert.match(proposalStatusText(record), /no closure has been submitted/);
  const continueButton = controls(rendered).find((part) => part.textContent === "Continue closure check");
  assert.ok(continueButton);
  await continueButton.events.click();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].operation, "continue-closure");
  assert.equal(calls[0].payload.proposal_id, id);
  assert.equal(calls[0].payload.closure_continuation, firstToken);
  assert.match(calls[0].payload.request_id, /^[a-f0-9]{32}$/);
  assert.equal(returned.result.scan_pass, 2);
  renderProposal(returned);
  assert.equal(calls.length, 1);

  api = async (operation, payload) => {
    calls.push({ operation, payload: structuredClone(payload) });
    const error = new Error("Synthetic unconfirmed response");
    error.uncertain = true;
    throw error;
  };
  await continueButton.events.click();
  const uncertain = state.unknown;
  assert.ok(uncertain);
  const originalPayload = structuredClone(uncertain.payload);
  api = async (operation, payload) => {
    calls.push({ operation, payload: structuredClone(payload) });
    return record;
  };
  await performMutation(uncertain);
  assert.deepEqual(calls.at(-1).payload, originalPayload);
  assert.equal(calls.at(-1).payload.closure_continuation, firstToken);
  assert.equal(state.unknown, null);

  const unknownRecord = { ...record, status: "uncertain", result: { status: "unknown" } };
  rendered = renderProposal(unknownRecord);
  assert.ok(!controls(rendered).some((part) => part.textContent === "Continue closure check"));
  const recover = controls(rendered).find((part) => part.textContent === "Recover last closure request");
  assert.ok(recover);
  await recover.events.click();
  assert.equal(calls.at(-1).operation, "recover-closure");
  assert.equal(calls.at(-1).payload.proposal_id, id);
  assert.equal(Object.hasOwn(calls.at(-1).payload, "closure_continuation"), false);
  assert.match(calls.at(-1).payload.request_id, /^[a-f0-9]{32}$/);

  const publishing = { ...unknownRecord, publication_action_id: "c".repeat(32) };
  rendered = renderProposal(publishing);
  assert.ok(!controls(rendered).some((part) => /Continue closure|Recover last closure/.test(part.textContent)));
  assert.ok(controls(rendered).some((part) => part.textContent === "Check this existing request"));

  const redacted = { id, kind: "close_task", status: "executing", source_status: "unavailable",
    text: "Source unavailable.", result: { status: "in_progress" } };
  rendered = renderProposal(redacted);
  assert.ok(!controls(rendered).some((part) => /Continue closure|Recover last closure/.test(part.textContent)));
  assert.ok(!flat(rendered).some((part) => part.textContent.includes(firstToken)));
})()
""")


def test_real_ui_classifies_saved_closure_conflicts_without_claiming_source_drift():
    expected = {
        "source_changed": ("Source changed", "source or pinned revision changed"),
        "task_closure_source_changed": ("Source changed", "source or pinned revision changed"),
        "source_closed": ("Source already closed", "already done, closed or cancelled"),
        "task_linked_path": ("Reference blocked", "regular directory"),
        "task_source_invalid": ("Reference invalid", "invalid text or metadata"),
        "task_source_not_allowed": ("Privacy refusal", "privacy or source-safety rules"),
        "task_links_require_repair": ("Link repair required", "safely repaired"),
        "task_publication_proof_missing": ("Proof incomplete", "proof is missing or incomplete"),
        "task_context_limit": ("Context limit", "bounded reading or finalization limit"),
        "task_closure_inventory_changed": ("Inventory changed", "inventory or ignore rules changed"),
        "task_closure_inventory_invalid": ("Inventory invalid", "inventory could not be validated"),
        "task_closure_inventory_limit": ("Inventory limit", "inventory exceeds the allowed limit"),
        "task_closure_continuation_invalid": ("Checkpoint conflict", "continuation did not match"),
        "task_closure_scan_exhausted": ("Closure check limit", "checks are exhausted"),
        "destination_exists": ("Destination exists", "destination already exists"),
        "action_id_reused": ("Request conflict", "different approved request"),
        "task_closure_scan_unconfirmed": ("Closure unconfirmed", "could not be confirmed"),
        "task_closure_checkpoint_unavailable": ("Closure unconfirmed", "could not be confirmed"),
        "receipt_unavailable": ("Closure unconfirmed", "could not be confirmed"),
        "remote_outcome_unknown": ("Closure unconfirmed", "could not be confirmed"),
        "task_closure_conflict": ("Closure conflict", "does not identify a specific supported reason"),
    }
    assert set(expected) == {*KNOWN_CONFLICT_CODES, "task_closure_conflict"}
    fixtures = []
    for code in ["task_closure_conflict", *KNOWN_CONFLICT_CODES, *UNKNOWN_CONFLICT_CODES]:
        subject, writer, repository, *_ = system()
        writer.conflict = code
        record = approve(subject, subject.close(closure_payload(), TODAY))
        safe_code = code if code in expected else "task_closure_conflict"
        label, explanation = expected[safe_code]
        case = {
            "label": label, "explanation": explanation, "safeCode": safe_code,
            "unsafeCode": code if code in UNKNOWN_CONFLICT_CODES else None,
        }
        fixtures.append({**case, "record": record})
        repository.records[PATH]["revision"] = "e" * 40
        fixtures.append({**case, "record": subject.history(0, TODAY)["items"][0]})

    run_ui_test(r"""
const flat = (item) => [item, ...item.children.flatMap(flat)];
for (const fixture of fixtures) {
  const { record, label, explanation, safeCode, unsafeCode } = fixture;
  const rendered = renderProposal(record);
  assert.equal(rendered.firstElementChild.textContent, label, safeCode);
  assert.ok(proposalStatusText(record).includes(explanation), safeCode);
  assert.ok(proposalStatusText(record).includes("fresh proposal and approval"), safeCode);
  const parts = flat(rendered);
  assert.ok(parts.some((part) => part.textContent.includes(explanation)), safeCode);
  const details = parts.find((part) => part.children.some((child) =>
    child.textContent === "Operation and publication receipts"));
  assert.ok(details, safeCode);
  const inspected = JSON.parse(details.children[1].textContent);
  assert.equal(inspected.result.error, safeCode);
  assert.equal(inspected.result.status, "conflict");
  assert.ok(!parts.some((part) => part.events.click), safeCode);
  if (unsafeCode) assert.ok(parts.every((part) => !part.textContent.includes(unsafeCode)));
  if (record.source_status !== "current") {
    assert.ok(!parts.some((part) => part.textContent === "Exact action to approve"));
  }
  showProposal(record);
  if (record.source_status === "current") assert.equal($("notice").textContent, proposalStatusText(record));
  state.overview = { history: [record] };
  renderActivity();
  assert.equal($("activity-list").firstElementChild.firstElementChild.textContent, label);
}
const current = fixtures.find((fixture) => fixture.record.source_status === "current").record;
for (const code of ["__proto__", "constructor", "<script>synthetic unsafe error</script>"]) {
  const record = { ...current, result: { ...current.result, error: code } };
  const rendered = renderProposal(record);
  assert.equal(rendered.firstElementChild.textContent, "Closure conflict");
  assert.ok(flat(rendered).every((part) => !part.textContent.includes(code)));
}
""", fixtures)
