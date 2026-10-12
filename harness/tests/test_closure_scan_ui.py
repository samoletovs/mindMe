"""Exercise real closure controls offline; rendering/recovery never schedules a next pass."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required for the static UI control test")
def test_explicit_closure_controls_and_same_request_recovery():
    script = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const source = fs.readFileSync(process.argv[2], "utf8");
const boundary = source.indexOf("\nfor (const stage of STAGES)");
assert.ok(boundary > 0);
function node() {
  return {
    textContent: "", children: [], disabled: false, hidden: false, value: "", dataset: {}, events: {},
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    addEventListener(name, callback) { this.events[name] = callback; },
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
};
const tests = `
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
`;
Promise.resolve(vm.runInNewContext(source.slice(0, boundary) + tests, {
  document, assert, structuredClone, console, URL, clearTimeout() {},
  crypto: { randomUUID() { return "01234567-89ab-cdef-0123-456789abcdef"; } },
})).catch((error) => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run(
        [shutil.which("node"), "-", str(Path(__file__).parents[1] / "web" / "tasks.js")],
        input=script, text=True, encoding="utf-8", capture_output=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
