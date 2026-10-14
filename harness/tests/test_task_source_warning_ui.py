"""Run the actual warning/paging adapter with synthetic DOM nodes, without a browser or network."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is needed for the static UI adapter check")
def test_warning_and_project_pagination_semantics_use_actual_ui_code():
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
    textContent: "", children: [], disabled: false, hidden: false, value: "", dataset: {},
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    get firstElementChild() { return this.children[0]; },
    addEventListener() {},
    setAttribute(name, value) { this[name] = value; },
    classList: { remove() {}, add() {} },
    querySelector() { return null; },
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
const tests = `
(async () => {
  const warningText = () => $("warnings").children.map((item) => item.textContent).join("\\n");
  const calendar = { calendar_date: "2026-10-10", timezone: "UTC", deadline_horizon_days: 7, assessed_count: 14 };
  const current = {
    canonical_revision: "a".repeat(40), attention: calendar, attention_complete: true,
    items: [], history: [], areas: [], projects: [{ id: "2026-one" }, { id: "2026-two" }],
    excluded_count: 1, errors: [], project_excluded_count: 2, project_errors: [],
    project_next_offset: 4, project_candidate_count: 7, next_offset: 12,
  };
  state.overview = structuredClone(current);
  state.overview.canonical_links = {
    dashboard: "https://github.com/example/mindVault/blob/" + "a".repeat(40) + "/home.md",
    knowledge_index: "https://github.com/example/mindVault/blob/" + "a".repeat(40) + "/wiki/index.md",
  };
  renderCanonicalLinks();
  assert.equal($("canonical-links").hidden, false);
  assert.equal($("canonical-links").children[0].textContent, "Open vault dashboard");
  assert.equal($("canonical-links").children[1].textContent, "Knowledge index");
  assert.equal($("canonical-links").children[0].target, "_blank");
  assert.equal($("canonical-links").children[0].rel, "noopener noreferrer");
  assert.match($("canonical-links").children[2].textContent, /GitHub account/);
  state.overview.canonical_links = { dashboard: "javascript:alert(1)", knowledge_index: "http://example.invalid" };
  renderCanonicalLinks();
  assert.equal($("canonical-links").hidden, true);
  assert.equal($("canonical-links").children.length, 0);
  state.captureSource = { id: "a".repeat(64), revision: "b".repeat(40), finding: "F1" };
  $("capture-context").textContent = "Synthetic source context";
  dashboard.today = { items: [{ title: "Synthetic source" }] };
  clearData();
  assert.equal($("canonical-links").hidden, true);
  assert.equal(state.captureSource, null);
  assert.equal($("capture-context").textContent, "");
  assert.equal($("capture-context").hidden, true);
  assert.equal(dashboard.today, null);
  state.overview = structuredClone(current);
  renderSourceWarnings();
  assert.match(warningText(), /1 task source candidate/);
  assert.match(warningText(), /2 checked project source candidate/);
  assert.doesNotMatch(warningText(), /coverage is incomplete|source issue\\(s\\)|inventory is incomplete/);

  const textTree = (item) => [item.textContent, ...item.children.map(textTree)].join("\\n");
  state.overview.next_offset = null;
  state.items = [
    { path: "tasks/selected.md", title: "Server-selected synthetic task", stage: "ready",
      next_action: "Keep the server calendar.", attention_eligible: true, attention_reasons: ["review_due"] },
    { path: "tasks/not-selected.md", title: "Unselected synthetic task", stage: "backlog",
      deadline: "1900-01-01", attention_eligible: false, attention_reasons: [] },
    { path: "tasks/waiting.md", title: "Waiting synthetic task", stage: "waiting",
      waiting_for: "A synthetic dependency.", attention_eligible: false, attention_reasons: [] },
  ];
  dashboard.renderToday();
  const todayText = textTree($("today-work"));
  assert.match(textTree($("today-priorities")), /Server-selected synthetic task/);
  assert.doesNotMatch(todayText, /Server-selected synthetic task/);
  assert.doesNotMatch(todayText, /Unselected synthetic task/);
  assert.match(todayText, /Waiting, not automatically due/);
  assert.match(todayText, /Task day 2026-10-10 \\/ UTC/);
  assert.doesNotMatch(todayText, /More tasks or source issues remain/);
  state.items = [];

  state.overview.attention_complete = false;
  state.overview.errors = [{ code: "task_source_policy_unresolved" }];
  state.overview.project_errors = [{ code: "task_source_unavailable" }];
  renderSourceWarnings();
  assert.match(warningText(), /Attention coverage is incomplete/);
  assert.match(warningText(), /privacy metadata is ambiguous/);
  assert.match(warningText(), /1 project source issue/);
  assert.match(warningText(), /Refresh to retry/);

  state.overview = structuredClone(current);
  renderAreas = () => {};
  renderTasks = () => {};
  renderActivity = () => {};
  renderCaptureFields = () => {};
  render = () => {};
  api = async (operation) => {
    assert.equal(operation, "projects?offset=4");
    return { items: [{ id: "2026-three" }], next_offset: null, excluded_count: 1,
      candidate_count: 7, errors: [{ code: "task_source_policy_unresolved" }], canonical_revision: current.canonical_revision };
  };
  await loadProjects();
  assert.equal(state.overview.projects.length, 3);
  assert.equal(state.overview.project_excluded_count, 3);
  assert.equal(state.overview.project_errors.length, 1);
  assert.equal(state.overview.project_next_offset, null);
  assert.match(warningText(), /3 checked project source candidate/);
  assert.match(warningText(), /1 project source issue/);

  api = async (operation) => {
    assert.equal(operation, "overview?offset=12");
    return { ...structuredClone(current), next_offset: null };
  };
  await loadOverview(true);
  assert.equal(state.overview.projects.length, 3);
  assert.equal(state.overview.project_excluded_count, 3);
  assert.equal(state.overview.project_errors.length, 1);
  assert.equal(state.overview.project_next_offset, null);
  assert.match(warningText(), /1 project source issue/);

  api = async (operation) => {
    assert.equal(operation, "overview?offset=0");
    return structuredClone(current);
  };
  await loadOverview();
  assert.equal(state.overview.project_excluded_count, 2);
  assert.equal(state.overview.project_errors.length, 0);
  assert.equal(state.overview.project_next_offset, 4);
  assert.doesNotMatch(warningText(), /inventory is incomplete/);
})()
`;
const bundledDashboard = "const Dashboard = (() => {\n" + dashboardSource + "\nreturn Dashboard;\n})();\n";
Promise.resolve(vm.runInNewContext(bundledDashboard + source + tests,
  { document, window: { addEventListener() {} }, assert, structuredClone, console, URL, clearTimeout() {} })).catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
    result = subprocess.run(
        [shutil.which("node"), "-", str(Path(__file__).parents[1] / "web" / "tasks.js")],
        input=script, text=True, encoding="utf-8", capture_output=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
