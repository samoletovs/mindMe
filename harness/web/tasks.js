import { Dashboard } from "./dashboard.js";

const API = "/api/tasks/api/";
const STAGES = ["untriaged", "clarify", "backlog", "ready", "doing", "waiting", "verify"];
const LABELS = {
  untriaged: "Untriaged", clarify: "Clarify", backlog: "Backlog", ready: "Ready",
  doing: "Doing", waiting: "Waiting", verify: "Verify",
  pending: "Needs approval", submitted: "Publication pending", executing: "In progress",
  uncertain: "Not confirmed", completed: "Verified operation", invalidated: "Source changed",
  failed: "Not completed", declined: "Declined", expired: "Expired", merged: "Canonical",
};
const CLOSURE_CONFLICTS = {
  source_changed: {
    label: "Source changed", text: "The approved source or pinned revision changed. This closure was not completed.",
  },
  task_closure_source_changed: {
    label: "Source changed", text: "The approved source or pinned revision changed. This closure was not completed.",
  },
  source_closed: {
    label: "Source already closed", text: "The source is already done, closed or cancelled. This receipt does not confirm that this request closed it.",
  },
  task_linked_path: {
    label: "Reference blocked", text: "A required path is linked or is not a regular directory. The safety check blocked closure.",
  },
  task_source_invalid: {
    label: "Reference invalid", text: "A required reference has invalid text or metadata. The closure was not completed.",
  },
  task_source_not_allowed: {
    label: "Privacy refusal", text: "A required reference is not permitted by privacy or source-safety rules. It was not skipped; closure was blocked.",
  },
  task_links_require_repair: {
    label: "Link repair required", text: "Required links could not be safely repaired. The closure was not completed.",
  },
  task_publication_proof_missing: {
    label: "Proof incomplete", text: "Required closure proof is missing or incomplete. No completed closure is confirmed.",
  },
  task_context_limit: {
    label: "Context limit", text: "A bounded reading or finalization limit blocked closure. The closure was not completed.",
  },
  task_closure_inventory_changed: {
    label: "Inventory changed", text: "The pinned reference inventory or ignore rules changed. The closure was not completed.",
  },
  task_closure_inventory_invalid: {
    label: "Inventory invalid", text: "The required reference inventory could not be validated. The closure was not completed.",
  },
  task_closure_inventory_limit: {
    label: "Inventory limit", text: "The required reference inventory exceeds the allowed limit. The closure was not completed.",
  },
  task_closure_continuation_invalid: {
    label: "Checkpoint conflict", text: "The continuation did not match this approved closure request. No completed closure is confirmed.",
  },
  task_closure_scan_exhausted: {
    label: "Closure check limit", text: "The bounded closure checks are exhausted. The closure was not completed.",
  },
  destination_exists: {
    label: "Destination exists", text: "The closed-task destination already exists. This request did not overwrite it.",
  },
  action_id_reused: {
    label: "Request conflict", text: "This operation ID already belongs to a different approved request. No new closure is confirmed.",
  },
  task_closure_scan_unconfirmed: {
    label: "Closure unconfirmed", text: "The closure outcome could not be confirmed. No completed closure is confirmed.",
  },
  task_closure_checkpoint_unavailable: {
    label: "Closure unconfirmed", text: "The saved closure checkpoint could not be confirmed. No completed closure is confirmed.",
  },
  receipt_unavailable: {
    label: "Closure unconfirmed", text: "The saved closure receipt could not be confirmed. No completed closure is confirmed.",
  },
  remote_outcome_unknown: {
    label: "Closure unconfirmed", text: "The closure outcome could not be confirmed. No completed closure is confirmed.",
  },
  task_closure_conflict: {
    label: "Closure conflict", text: "The saved receipt does not identify a specific supported reason. No completed closure is confirmed.",
  },
};
const $ = (id) => document.getElementById(id);
const state = {
  csrf: "", view: "today", layout: "board", items: [], overview: null, selected: null,
  tab: "overview", busy: false, unknown: null, proposal: null, clarification: null,
  loading: false, detailGeneration: 0, sessionGeneration: 0, authExpires: 0, expiryTimer: null,
  captureSource: null,
};
const dashboard = new Dashboard({
  state, element, append, button, writeButton, empty, infoTip, api, mutate, showNotice, setBusy,
  renderTaskRow, setView, beginSourceCapture,
  openTask: (path) => { setView("tasks"); openTask(path); },
});

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}
function append(parent, ...children) {
  children.filter(Boolean).forEach((child) => parent.append(child));
  return parent;
}
function button(text, action, className = "") {
  const node = element("button", text, className);
  node.type = "button";
  node.addEventListener("click", action);
  return node;
}
function statusBadge(value, label) {
  const key = typeof value === "string" ? value : "unknown";
  return element("span", label || LABELS[key] || key.replaceAll("_", " "), `status ${STAGES.includes(key) || Object.hasOwn(LABELS, key) ? key : ""}`);
}
function prettyId(value) {
  return String(value || "").replace(/^\d{4}-/, "").replaceAll("-", " ");
}
function attentionDate() {
  return state.overview?.attention?.calendar_date || "";
}
function requestId() {
  return crypto.randomUUID().replaceAll("-", "");
}
function safeLink(text, value) {
  try {
    const url = new URL(value);
    if (url.protocol !== "https:") return null;
    const link = element("a", text);
    link.href = url.href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    return link;
  } catch {
    return null;
  }
}
function showNotice(message, kind = "") {
  $("notice").textContent = message;
  $("notice").className = `notice ${kind}`;
  $("notice").hidden = !message;
}
function setBusy(value) {
  state.busy = value;
  document.querySelectorAll("button[data-write], form button[type='submit']").forEach((node) => {
    node.disabled = value || Boolean(state.unknown);
  });
  document.querySelectorAll("form input, form textarea, form select").forEach((node) => {
    if ((value || state.unknown) && !node.disabled) {
      node.dataset.requestLock = "true";
      node.disabled = true;
    } else if (!value && !state.unknown && node.dataset.requestLock === "true") {
      node.disabled = false;
      delete node.dataset.requestLock;
    }
  });
  $("recover-request").disabled = value;
  $("connection").textContent = value ? "Working on your request" : (state.csrf ? "Personal workspace" : "Signed out");
}
function writeButton(text, action, className = "") {
  const node = button(text, action, className);
  node.dataset.write = "true";
  node.disabled = state.busy || Boolean(state.unknown);
  return node;
}
function errorMessage(error) {
  return error.message || "The service could not confirm this request.";
}
async function api(operation, payload) {
  if (isMockMode()) return mockApi(operation, payload);
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), payload === undefined ? 25000 : 125000);
  try {
    const response = await fetch(API + operation, {
      method: payload === undefined ? "GET" : "POST",
      credentials: "same-origin", mode: "same-origin", cache: "no-store",
      headers: payload === undefined ? {} : {
        "Content-Type": payload.audio ? payload.audio.type : "application/json", "X-MindMe-CSRF": state.csrf,
        ...(payload.audio ? { "X-Request-Id": payload.request_id } : {}),
      },
      ...(payload === undefined ? {} : { body: payload.audio || JSON.stringify(payload) }),
      signal: controller.signal,
    });
    let body;
    try {
      body = await response.json();
    } catch {
      const error = new Error("The service returned an unreadable response. No success is claimed.");
      error.uncertain = payload !== undefined;
      throw error;
    }
    if (!response.ok) {
      const error = new Error(typeof body.message === "string" ? body.message : "This request was not accepted.");
      error.status = response.status;
      error.code = body.error;
      error.uncertain = payload !== undefined && response.status >= 500;
      if (response.status === 401 && operation !== "session") expireSession();
      throw error;
    }
    if (!body || typeof body !== "object") {
      const error = new Error("The service response was incomplete. No success is claimed.");
      error.uncertain = payload !== undefined;
      throw error;
    }
    return body;
  } catch (error) {
    if (payload !== undefined && !error.status) error.uncertain = true;
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}
async function mutate(operation, payload, onSuccess, afterSuccess) {
  if (state.busy || state.unknown) return;
  if (assist.recorder || assist.acquiringVoice) { showNotice(t("finishVoice"), "pending"); return; }
  const attempt = { operation, payload, onSuccess, afterSuccess };
  await performMutation(attempt);
}
async function performMutation(attempt) {
  const generation = state.sessionGeneration;
  let confirmed = false;
  setBusy(true);
  showNotice("Working on this exact request. Please wait for its receipt.");
  try {
    const result = await api(attempt.operation, attempt.payload);
    if (generation !== state.sessionGeneration) return;
    state.unknown = null;
    $("uncertain").hidden = true;
    await attempt.onSuccess(result);
    confirmed = true;
  } catch (error) {
    if (generation === state.sessionGeneration && error.status >= 400 && error.status < 500) {
      state.unknown = null;
      $("uncertain").hidden = true;
    }
    if (generation === state.sessionGeneration && (error.uncertain || !error.status)) {
      state.unknown = attempt;
      $("uncertain").hidden = false;
    }
    const voiceFallback = attempt.operation === "assist/voice" && error.status >= 400 && error.status < 500 ? ` ${t("voiceTypeFallback")}` : "";
    showNotice((attempt.operation.startsWith("assist/") ? assistError(error) : errorMessage(error)) + voiceFallback, "error");
    if (attempt.operation.startsWith("assist/") && error.status === 404) {
      assist.available = false;
      document.querySelectorAll(".ai-feature").forEach((node) => { node.hidden = true; });
      $("copilot-dialog").close();
      if (state.selected?.revision) renderDetail();
    }
    if (error.status === 401) {
      $("access-message").textContent = "Your sign-in expired. Sign in again, then inspect Activity before repeating a request.";
    }
  } finally {
    setBusy(false);
    renderChats();
  }
  if (confirmed && generation === state.sessionGeneration && attempt.afterSuccess) await attempt.afterSuccess();
}
function field(parent, name, label, options = {}) {
  const wrapper = element("div", null, options.wide ? "wide" : "");
  const control = options.options ? element("select") : element(options.multiline ? "textarea" : "input");
  const id = `${parent.id || "field"}-${name}-${Math.random().toString(36).slice(2, 7)}`;
  control.id = id;
  control.name = name;
  if (control.tagName === "INPUT") control.type = options.type || "text";
  if (control.tagName === "TEXTAREA") control.rows = options.rows || 3;
  if (options.max) control.maxLength = options.max;
  control.required = Boolean(options.required);
  if (options.options) {
    for (const [value, text] of options.options) {
      const option = element("option", text);
      option.value = value;
      control.append(option);
    }
  }
  control.value = options.value ?? options.options?.[0]?.[0] ?? "";
  const labelNode = element("label", label);
  labelNode.htmlFor = id;
  append(wrapper, labelNode, control);
  if (options.hint) wrapper.append(element("p", options.hint, "hint"));
  parent.append(wrapper);
  return control;
}
let infoTipCount = 0;
function infoTip(text) {
  const node = element("span", null, "info-tip");
  node.tabIndex = 0;
  node.setAttribute("role", "note");
  node.setAttribute("aria-label", "More information");
  const tip = element("span", text, "tip-text");
  tip.id = `info-tip-${++infoTipCount}`;
  tip.setAttribute("role", "tooltip");
  node.setAttribute("aria-describedby", tip.id);
  const mark = element("span", "i", "info-mark");
  mark.setAttribute("aria-hidden", "true");
  return append(node, mark, tip);
}
function empty(parent, title, message) {
  const node = element("div", null, "empty");
  append(node, element("strong", title), element("p", message));
  parent.append(node);
}
function areaOptions() {
  return [["", "Choose an area"], ...(state.overview?.areas || []).map((area) => [area, prettyId(area)])];
}
function projectOptions() {
  return [["", "No project selected"], ...(state.overview?.projects || []).map((project) => [project.id, prettyId(project.id)])];
}
function isAttention(task) {
  return task.attention_eligible === true;
}
function pendingProposals() {
  return (state.overview?.history || []).filter((record) => record.status === "pending" && record.action && record.approval_digest);
}
function setView(view, load = true) {
  if (!["today", "needs", "tasks", "knowledge", "areas", "activity"].includes(view)) view = "today";
  state.view = view;
  const titles = {
    today: [t("today"), t("todayDescription")],
    knowledge: [t("knowledgeNav"), t("knowledgeDescription")],
    needs: [t("needs"), t("needsDescription")],
    tasks: [t("flow"), t("flowDescription")],
    areas: [t("areasNav"), t("areasDescription")],
    activity: [t("activityNav"), t("activityDescription")],
  };
  $("view-title").textContent = titles[view][0];
  $("view-description").textContent = titles[view][1];
  if (location.hash !== `#${view}`) history.replaceState(null, "", `#${view}`);
  document.querySelectorAll("[data-view]").forEach((node) => {
    if (node.dataset.view === view || (view === "needs" && node.dataset.view === "tasks")) node.setAttribute("aria-current", "page");
    else node.removeAttribute("aria-current");
  });
  $("tasks-section").hidden = !["needs", "tasks"].includes(view);
  $("areas-section").hidden = view !== "areas";
  $("activity-section").hidden = view !== "activity";
  $("today-section").hidden = view !== "today";
  $("knowledge-section").hidden = view !== "knowledge";
  $("warnings").hidden = view === "knowledge";
  $("scope-needs").setAttribute("aria-pressed", String(view === "needs"));
  $("scope-all").setAttribute("aria-pressed", String(view !== "needs"));
  render();
  if (load && state.csrf && view === "today" && !dashboard.today) dashboard.loadToday();
  if (load && state.csrf && view === "knowledge" && !dashboard.inbox) dashboard.loadInbox();
}
function renderTaskRow(task, action = () => openTask(task.path)) {
  const row = button("", action, "task-row");
  row.dataset.path = task.path;
  row.setAttribute("aria-pressed", String(state.selected?.path === task.path));
  const top = element("div", null, "row-top");
  append(top, statusBadge(task.stage), element("span", task.area ? prettyId(task.area) : "Area not clarified"));
  append(row, top, element("h3", task.title), element("p", task.next_action || "Clarify the next action.", "row-next"));
  const timing = element("div", null, "row-timing");
  const day = attentionDate();
  if (day && task.focus_on === day) timing.append(element("span", "Selected for today", "status ready"));
  if (task.deadline) timing.append(element("span", `Deadline ${task.deadline}`, day && task.deadline <= day ? "deadline" : ""));
  if (task.review_on) timing.append(element("span", `Review ${task.review_on}`, day && task.review_on <= day ? "review-due" : ""));
  if (task.attention_reasons?.includes("deadline_soon")) timing.append(element("span", "Approaching deadline", "review-due"));
  if (task.waiting_for) timing.append(element("span", "Waiting on a dependency"));
  if (task.definition?.missing?.length) timing.append(element("span", "Definition needs work"));
  row.append(timing);
  if (task.estimate_min != null) timing.append(element("span", `${task.estimate_min} min`, "status"));
  if (task.ai_hint) row.append(element("p", `✦ ${task.ai_hint}`, "row-ai"));
  if (state.layout === "board") {
    row.draggable = true;
    row.addEventListener("dragstart", (event) => {
      assist.dragPath = task.path;
      event.dataTransfer.setData("text/plain", task.path);
      event.dataTransfer.effectAllowed = "move";
      row.classList.add("dragging");
    });
    row.addEventListener("dragend", () => { assist.dragPath = null; row.classList.remove("dragging"); });
  }
  return row;
}
function filteredTasks() {
  const search = $("task-search").value.trim().toLocaleLowerCase();
  return state.items.filter((task) =>
    (state.view !== "needs" || isAttention(task))
    && (!$("stage-filter").value || task.stage === $("stage-filter").value)
    && (!$("area-filter").value || task.area === $("area-filter").value)
    && (!search || [task.title, task.next_action, task.outcome, task.project].filter(Boolean).join(" ").toLocaleLowerCase().includes(search))
  );
}
function renderTasks() {
  const list = $("task-list");
  const positions = new Map(Array.from(list.querySelectorAll?.(".task-row") || []).map((row) => [row.dataset.path, row.getBoundingClientRect()]));
  list.replaceChildren();
  list.className = `task-list ${state.layout === "board" ? "board" : ""}`;
  $("tasks-section").classList.toggle("layout-board", state.layout === "board");
  $("tasks-section").classList.toggle("task-selected", Boolean(state.selected));
  if (state.loading && !state.items.length) {
    list.setAttribute("aria-label", "Loading tasks");
    for (let i = 0; i < 4; i += 1) list.append(element("div", null, "skeleton"));
    return;
  }
  list.removeAttribute("aria-label");
  const tasks = filteredTasks();
  if (!state.overview) {
    empty(list, "Your sources are not available yet.", "Refresh to load them. Unavailable is not an empty backlog.");
  } else if (!tasks.length) {
    const incompleteAttention = state.view === "needs" && state.overview.attention_complete === false;
    empty(list, incompleteAttention ? "The attention check is incomplete." :
      (state.view === "needs" ? "No matching due or selected tasks on this page." : "No matching tasks on this page."),
    incompleteAttention ? "More task sources or source errors remain. Load more or resolve the reported issue before concluding that nothing is due." :
      "Check the filters, load more tasks, or capture a new request. Nothing has been completed or discarded.");
  } else if (state.layout === "board") {
    list.tabIndex = 0;
    list.setAttribute("aria-label", t("boardHelp"));
    for (const [stage, stages, label] of [
      ["clarify", ["untriaged", "clarify", "backlog"], "inbox"],
      ["ready", ["ready"], "ready"], ["doing", ["doing"], "doing"],
      ["waiting", ["waiting"], "waiting"], ["verify", ["verify"], "verify"], ["done", ["done"], "done"],
    ]) {
      const laneTasks = tasks.filter((task) => stages.includes(task.stage));
      const lane = element("section", null, "board-lane");
      lane.dataset.stage = stage;
      lane.append(append(element("h3"), element("span", null, "lane-dot"), element("span", t(label)), element("span", laneTasks.length, "lane-count")));
      lane.addEventListener("dragover", (event) => {
        if (!assist.dragPath || state.busy || state.unknown) return;
        event.preventDefault(); lane.classList.add("drag-over");
      });
      lane.addEventListener("dragleave", () => lane.classList.remove("drag-over"));
      lane.addEventListener("drop", (event) => {
        event.preventDefault(); lane.classList.remove("drag-over");
        const task = state.items.find((item) => item.path === assist.dragPath);
        assist.dragPath = null;
        if (task) moveTask(task, stage);
      });
      laneTasks.forEach((task) => lane.append(renderTaskRow(task)));
      if (!laneTasks.length) lane.append(element("p", "No loaded tasks", "empty"));
      list.append(lane);
    }
  } else {
    list.removeAttribute("tabindex");
    tasks.forEach((task) => list.append(renderTaskRow(task)));
  }
  const candidateCount = state.overview?.candidate_count;
  if (state.overview) {
    $("page-status").replaceChildren(`${state.items.length} tasks${Number.isInteger(candidateCount) ? ` of ${candidateCount}` : ""} `,
      infoTip("Filters and counts cover loaded, permitted tasks only."));
  } else $("page-status").textContent = "Source availability has not been confirmed.";
  $("load-more").hidden = state.overview?.next_offset === null || state.overview?.next_offset === undefined;
  $("load-more").disabled = state.loading;
  for (const row of list.querySelectorAll?.(".task-row") || []) {
    const before = positions.get(row.dataset.path);
    if (!before || !row.animate || matchMedia("(prefers-reduced-motion: reduce)").matches) continue;
    const after = row.getBoundingClientRect();
    const dx = before.left - after.left, dy = before.top - after.top;
    if (Math.abs(dx) + Math.abs(dy) > 2) row.animate([
      { transform: `translate(${dx}px, ${dy}px)`, opacity: .6 }, { transform: "translate(0, 0)", opacity: 1 },
    ], { duration: 400, easing: "cubic-bezier(.2,.85,.25,1)" });
  }
}
function renderAttention() {
  const target = $("attention");
  target.replaceChildren();
  target.hidden = state.view !== "needs";
  if (target.hidden || !state.overview) return;
  const pending = pendingProposals().length;
  const unreviewed = (state.overview.history || []).filter((item) =>
    item.status === "completed" && ["prepare_task", "research"].includes(item.kind) && !item.reviewed
  ).length;
  const focused = attentionDate() ? state.items.filter((item) => item.focus_on === attentionDate()).length : 0;
  append(target,
    element("strong", pending ? `${pending} proposal${pending === 1 ? "" : "s"} need your decision.` : "Make room for one useful next action."),
    element("p", `${focused} loaded task${focused === 1 ? "" : "s"} selected for today. ${unreviewed} result${unreviewed === 1 ? "" : "s"} await review. Deadlines stay visible, even when work is waiting.`),
  );
  if (state.overview.attention_complete === false) {
    target.append(element("p", "Attention coverage is incomplete. Unchecked or unavailable tasks may still have deadlines; load more and review source warnings.", "review-due"));
  }
  if (pending || unreviewed) target.append(button("Review decisions and results", () => setView("activity")));
}
function render() {
  $("layout-list").setAttribute("aria-pressed", String(state.layout === "list"));
  $("layout-board").setAttribute("aria-pressed", String(state.layout === "board"));
  $("needs-count").textContent = String(state.items.filter(isAttention).length + pendingProposals().length);
  $("tasks-count").textContent = String(state.items.length);
  $("activity-count").textContent = String(pendingProposals().length);
  renderAttention();
  renderTasks();
  if (state.view === "areas") renderAreas();
  if (state.view === "activity") renderActivity();
  if (state.view === "today") dashboard.renderToday();
  if (state.view === "knowledge") {
    $("snapshot").textContent = dashboard.inbox
      ? `Canonical review snapshot ${dashboard.inbox.canonical_revision.slice(0, 7)}`
      : "Review sources checked separately from tasks";
  }
}
function sourceIssueMessage(code) {
  return {
    task_metadata_missing: "A task has no supported metadata block. Its dates could not be assessed.",
    task_metadata_invalid: "A source has invalid or conflicting metadata. Correct the source before relying on its dates.",
    task_definition_invalid: "A task has an invalid definition field. Its attention status is unknown.",
    task_date_invalid: "A task has an invalid date. Correct the saved date before relying on this view.",
    task_title_missing: "A task has no supported title. Its definition could not be assessed.",
    task_review_date_ambiguous: "A task has conflicting review dates. Reconcile them in the source.",
    task_source_too_large: "A source exceeded the bounded reader's size limit and was not assessed.",
    task_source_policy_unresolved: "A source's scope or privacy metadata is ambiguous. It remains unavailable until clarified.",
    task_source_unavailable: "A source could not be read or verified. Refresh to retry its current version.",
  }[code] || "A source could not be assessed. Its absence is not evidence that no work is due.";
}
function renderSourceWarnings() {
  const data = state.overview;
  $("warnings").replaceChildren();
  if (!data) return;
  if (data.excluded_count > 0) {
    $("warnings").append(element("p", `${data.excluded_count} task source candidate(s) in this assessed window are outside the permitted scope/privacy policy. They remain excluded, not failed reads.`));
  }
  if (data.project_excluded_count > 0) {
    $("warnings").append(element("p", `${data.project_excluded_count} checked project source candidate(s) are outside the permitted scope/privacy policy. Their content is not shown.`));
  }
  if (data.attention_complete === false) {
    const assessed = data.attention?.assessed_count;
    $("warnings").append(element("p", `${Number.isInteger(assessed) ? `${assessed} permitted task definitions assessed in this bounded window. ` : ""}Attention coverage is incomplete; unchecked or unresolved sources remain.`));
  }
  if (data.errors?.length) {
    $("warnings").append(element("p", `${data.errors.length} task source issue(s) need attention. Unavailable sources are not treated as empty or complete.`));
    for (const error of data.errors) $("warnings").append(element("p", sourceIssueMessage(error.code)));
  }
  if (data.project_errors?.length) {
    $("warnings").append(element("p", `${data.project_errors.length} project source issue(s) remain. The permitted inventory is incomplete; refresh to retry.`));
    for (const error of data.project_errors) $("warnings").append(element("p", sourceIssueMessage(error.code)));
  }
}
function renderCanonicalLinks() {
  const target = $("canonical-links");
  target.replaceChildren();
  for (const [key, label] of [["dashboard", "Open vault dashboard"], ["knowledge_index", "Knowledge index"]]) {
    const link = safeLink(label, state.overview?.canonical_links?.[key]);
    if (link) {
      link.className = "button";
      target.append(link);
    }
  }
  target.hidden = !target.children.length;
  if (!target.hidden) {
    target.append(element("p", "Opens canonical pages on GitHub; vault access requires your GitHub account.", "hint"));
  }
}
async function loadOverview(appendPage = false) {
  if (state.loading) return;
  state.loading = true;
  const generation = state.sessionGeneration;
  renderTasks();
  try {
    const offset = appendPage ? state.overview?.next_offset : 0;
    if (offset === null || offset === undefined) return;
    const data = await api(`overview?offset=${offset}`);
    if (generation !== state.sessionGeneration) return;
    if (!Array.isArray(data.items) || !Array.isArray(data.history) || !Array.isArray(data.areas) || !Array.isArray(data.projects)
      || !/^\d{4}-\d{2}-\d{2}$/.test(data.attention?.calendar_date || "")
      || typeof data.attention?.timezone !== "string"
      || data.items.some((task) => typeof task.attention_eligible !== "boolean")) {
      throw new Error("The workspace response is incomplete. Its counts cannot be trusted.");
    }
    if (appendPage && state.overview?.canonical_revision && state.overview.canonical_revision !== data.canonical_revision) {
      throw new Error("The canonical source changed while loading another page. Refresh to avoid mixing task versions.");
    }
    if (appendPage && (state.overview?.attention?.calendar_date !== data.attention.calendar_date
      || state.overview?.attention?.timezone !== data.attention.timezone
      || state.overview?.attention?.deadline_horizon_days !== data.attention.deadline_horizon_days)) {
      throw new Error("Your task calendar changed while paging. Refresh from the first page to see today's priorities.");
    }
    const items = appendPage ? [...state.items, ...data.items] : data.items;
    if (appendPage && state.overview?.canonical_revision === data.canonical_revision) {
      for (const key of ["projects", "project_next_offset", "project_errors", "project_excluded_count", "project_candidate_count"]) {
        data[key] = state.overview[key];
      }
    }
    state.items = [...new Map(items.map((task) => [task.path, task])).values()];
    state.overview = data;
    renderCanonicalLinks();
    if (!appendPage && state.selected?.revision && !data.items.some((task) =>
      task.path === state.selected.path && task.revision === state.selected.revision
    )) closeDetail();
    if (state.proposal) {
      state.proposal = data.history.find((record) => record.id === state.proposal.id) || null;
      $("proposal-preview").replaceChildren(...(state.proposal ? [renderProposal(state.proposal)] : []));
      $("proposal-section").hidden = !state.proposal;
    }
    renderActivity();
    $("snapshot").textContent = `Task day ${data.attention.calendar_date} / ${data.attention.timezone}. Snapshot checked ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
    const area = $("area-filter").value;
    $("area-filter").replaceChildren();
    for (const [value, text] of [["", "All areas"], ...data.areas.map((id) => [id, prettyId(id)])]) {
      const option = element("option", text); option.value = value; $("area-filter").append(option);
    }
    $("area-filter").value = area;
    renderSourceWarnings();
    renderCaptureFields();
    render();
  } catch (error) {
    if (generation === state.sessionGeneration) showNotice(`${errorMessage(error)}${state.overview ? " Previously loaded data may be stale." : ""}`, "error");
  } finally {
    state.loading = false;
    renderTasks();
  }
}
async function loadProjects() {
  const offset = state.overview?.project_next_offset;
  if (offset === null || offset === undefined) return;
  $("projects-more").disabled = true;
  const generation = state.sessionGeneration;
  try {
    const data = await api(`projects?offset=${offset}`);
    if (generation !== state.sessionGeneration) return;
    if (!Array.isArray(data.items) || !Object.hasOwn(data, "next_offset")) throw new Error("The project page is incomplete.");
    if (state.overview.canonical_revision && data.canonical_revision !== state.overview.canonical_revision) {
      throw new Error("The canonical source changed while paging. Refresh before changing project selection.");
    }
    if (state.overview.project_next_offset !== offset) {
      throw new Error("The project page changed while loading. Refresh before continuing the inventory.");
    }
    state.overview.projects = [...new Map([...state.overview.projects, ...data.items].map((project) => [project.id, project])).values()];
    state.overview.project_next_offset = data.next_offset;
    state.overview.project_errors = [...(state.overview.project_errors || []), ...(data.errors || [])];
    state.overview.project_excluded_count = (state.overview.project_excluded_count || 0) + (data.excluded_count || 0);
    state.overview.project_candidate_count = data.candidate_count;
    renderSourceWarnings();
    renderAreas();
    const select = $("capture-details").querySelector("[name='project']");
    if (select) {
      const previous = select.value;
      select.replaceChildren();
      for (const [value, text] of projectOptions()) {
        const option = element("option", text); option.value = value; select.append(option);
      }
      select.value = previous;
    }
  } catch (error) {
    if (generation === state.sessionGeneration) showNotice(errorMessage(error), "error");
  } finally {
    $("projects-more").disabled = false;
  }
}
function renderCaptureFields() {
  const target = $("capture-details");
  if (target.children.length) return;
  field(target, "area", "Area", { options: areaOptions() });
  field(target, "project", "Project", { options: projectOptions() });
  field(target, "execution", "Who performs the next action?", { options: [["", "Not decided"], ["human", "Me"], ["assisted", "Me, with AI help"], ["agent", "Agent (requires authorization)"]] });
  field(target, "outcome", "Desired outcome", { multiline: true, max: 2000, wide: true });
  field(target, "next_action", "Next action", { multiline: true, max: 500, wide: true });
  field(target, "done_when", "Done when", { multiline: true, max: 2000, wide: true, hint: "An observable result you can actually check." });
}
async function loadHistory() {
  const offset = state.overview?.history_next_offset;
  if (offset === null || offset === undefined) return;
  const generation = state.sessionGeneration;
  $("history-more").disabled = true;
  try {
    const data = await api(`history?offset=${offset}`);
    if (generation !== state.sessionGeneration) return;
    if (!Array.isArray(data.items) || !Object.hasOwn(data, "next_offset")) throw new Error("The activity page is incomplete.");
    state.overview.history = [...new Map([...state.overview.history, ...data.items].map((item) => [item.id, item])).values()];
    state.overview.history_next_offset = data.next_offset;
    renderActivity();
  } catch (error) {
    if (generation === state.sessionGeneration) showNotice(errorMessage(error), "error");
  } finally {
    $("history-more").disabled = false;
  }
}
async function openTask(path) {
  const generation = ++state.detailGeneration;
  state.selected = { path };
  state.clarification = null;
  $("inspector").replaceChildren(element("p", "Loading the canonical task...", "empty"));
  renderTasks();
  try {
    const task = await api("task", { path });
    if (generation !== state.detailGeneration) return;
    if (!task.path || !task.revision || typeof task.title !== "string") throw new Error("The canonical task response is incomplete.");
    state.selected = task;
    state.tab = assist.available ? "chat" : "overview";
    renderDetail();
    renderTasks();
    if (matchMedia("(max-width: 690px)").matches) $("inspector").scrollIntoView({ block: "start" });
  } catch (error) {
    if (generation !== state.detailGeneration) return;
    $("inspector").replaceChildren(button("Back to tasks", closeDetail), element("p", errorMessage(error), "empty"));
  }
}
function closeDetail() {
  state.detailGeneration += 1;
  state.selected = null;
  state.clarification = null;
  $("inspector").replaceChildren(append(element("div", null, "inspector-empty"),
    element("h2", "A next step, not another list."),
    element("p", "Open a task to clarify its outcome, plan the next action, or review what AI can prepare.")));
  renderTasks();
}
function detailBase() {
  const task = state.selected;
  const target = $("inspector");
  target.replaceChildren();
  target.append(button(t("back"), closeDetail, "mobile-back quiet"));
  const header = element("div", null, "detail-header");
  append(header, statusBadge(task.stage), element("h2", task.title, "detail-title"),
    element("p", `${task.area ? prettyId(task.area) : "Area not clarified"}${task.project ? ` / ${prettyId(task.project)}` : ""}`, "hint"));
  const links = element("div", null, "meta-line");
  append(links, safeLink("Open canonical source", task.url), element("span", `Revision ${task.revision.slice(0, 7)}`));
  header.append(links);
  target.append(header);
  const outcome = element("section", null, "outcome-block");
  append(outcome, element("span", t("outcome"), "eyebrow"), element("p", task.outcome || t("notDefined")),
    element("span", t("doneWhen"), "eyebrow"), element("p", task.done_when || t("notDefined")));
  target.append(outcome);
  if (assist.available) target.append(buildStepRail(task));
  const tabs = element("nav", null, "detail-tabs");
  tabs.setAttribute("aria-label", "Task actions");
  for (const [id, title] of [
    ...(assist.available ? [["chat", t("chatTab")]] : []),
    ["overview", t("overviewTab")], ["edit", t("editTab")], ["ai", t("aiTab")], ["close", t("closeTab")],
  ]) {
    const node = button(title, () => { state.tab = id; renderDetail(); });
    node.setAttribute("aria-pressed", String(state.tab === id));
    tabs.append(node);
  }
  target.append(tabs);
  return append(target, element("div", null, "detail-panel")).lastElementChild;
}
function renderDetail() {
  if (!state.selected?.revision) return;
  const panel = detailBase();
  const task = state.selected;
  if (state.tab === "chat") {
    panel.append(append(element("dl", null, "detail-section"), element("dt", t("nextAction")), element("dd", task.next_action || t("notDefined"))));
  } else if (state.tab === "overview") {
    const description = element("dl", null, "detail-section");
    for (const [label, value] of [[t("nextAction"), task.next_action]]) {
      append(description, element("dt", label), element("dd", value || "Not recorded"));
    }
    panel.append(description);
    const metadata = element("details", null, "task-metadata");
    metadata.append(element("summary", t("taskDetails")));
    const dates = element("dl", null, "detail-section");
    for (const [label, value] of [["Execution", task.execution], ["Review on", task.review_on], ["Deadline", task.deadline], ["Focus on", task.focus_on], ["Waiting for", task.waiting_for]]) {
      append(dates, element("dt", label), element("dd", value || "Not recorded"));
    }
    metadata.append(dates);
    if (task.definition?.missing?.length) {
      metadata.append(element("p", `Needs clarification: ${task.definition.missing.join(", ")}. These are structural checks, not approval.`, "hint"));
    }
    metadata.append(writeButton("Clarify this task", () => mutate("clarify", { path: task.path, revision: task.revision }, showClarification)));
    panel.append(metadata);
    if (task.text) {
      const details = element("details", null, "section-block");
      append(details, element("summary", "Read the full source"), element("pre", task.text));
      panel.append(details);
    }
    if (state.clarification) renderClarification(panel);
  } else if (state.tab === "edit") {
    renderEdit(panel, task);
  } else if (state.tab === "ai") {
    renderPreparation(panel, task);
  } else {
    renderClose(panel, task);
  }
  if (assist.available) {
    const chat = element("section", null, "task-copilot section-block");
    chat.append(element("h3", t("taskCopilot")));
    const host = element("div"); host.dataset.chatKey = task.path;
    chat.append(host); panel.append(chat);
    renderChat(host, task.path);
  }
}
function renderEdit(panel, task) {
  const form = element("form"); form.id = "definition-form";
  append(panel, element("h3", "Clarify the record"), element("p", "Review exact changes before saving. A stage or execution label does not start an agent.", "hint"));
  const fields = element("div", null, "form-grid"); fields.id = "edit-fields";
  field(fields, "title", "Title", { value: task.title, max: 120, wide: true, required: true });
  field(fields, "area", "Area", { value: task.area, options: areaOptions() });
  const projects = projectOptions();
  if (task.project && !projects.some(([id]) => id === task.project)) projects.push([task.project, prettyId(task.project)]);
  field(fields, "project", "Project", { value: task.project, options: projects });
  field(fields, "stage", "Stage", { value: task.stage === "untriaged" ? "" : task.stage, options: [["", "Keep untriaged"], ...STAGES.filter((id) => id !== "untriaged").map((id) => [id, LABELS[id]])] });
  field(fields, "execution", "Execution mode", { value: task.execution, options: [["", "Not decided"], ["human", "Me"], ["assisted", "Me, with AI help"], ["agent", "Agent (requires authorization)"]] });
  for (const [name, label, max] of [["outcome", "Outcome", 2000], ["next_action", "Next action", 500], ["done_when", "Done when", 2000]]) {
    field(fields, name, label, { value: task[name], max, multiline: true, wide: true });
  }
  field(fields, "waiting_for", "Waiting for", { value: task.waiting_for, max: 300, wide: true, hint: "Name the dependency, not sensitive personal details. Clear this field only when the blocker is resolved." });
  field(fields, "review_on", "Review on", { value: task.review_on, type: "date", hint: "Agree a review date when moving started work to Waiting." });
  form.append(fields);
  const submit = element("button", "Review definition changes", "primary"); submit.type = "submit"; form.append(submit);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const changes = {};
    for (const [name, raw] of new FormData(form)) {
      const value = String(raw).trim();
      const before = task[name] === "untriaged" ? "" : (task[name] || "");
      if (value === before) continue;
      if (!value && ["waiting_for", "review_on"].includes(name)) changes[name] = null;
      else if (value) changes[name] = value;
      else if (before) {
        showNotice(`The ${name.replaceAll("_", " ")} field cannot be cleared through this form. Choose a replacement or keep it unchanged.`, "error");
        return;
      }
    }
    if (!Object.keys(changes).length) { showNotice("There are no definition changes to review."); return; }
    if ((Object.hasOwn(changes, "waiting_for") || Object.hasOwn(changes, "review_on")) && !changes.stage) {
      const stage = String(new FormData(form).get("stage") || "");
      if (!stage) {
        showNotice("Choose the task's stage when changing waiting details here, or use the one-date form below.", "error");
        return;
      }
      changes.stage = stage;
    }
    mutate("refine", { request_id: requestId(), path: task.path, revision: task.revision, changes }, showProposal);
  });
  panel.append(form);
  const dates = element("form", null, "section-block"); dates.id = "date-form";
  dates.append(element("h3", "Change one date deliberately"));
  const kind = field(dates, "field", "Date type", { options: [["review_on", "Next review"], ["focus_on", "Selected focus date"], ["deadline", "Real deadline"]] });
  const date = field(dates, "value", "Date", { type: "date", value: task.review_on });
  const clearLabel = element("label", null, "checkbox");
  const clear = element("input"); clear.type = "checkbox";
  append(clearLabel, clear, element("span", "Explicitly remove this date"));
  dates.append(clearLabel);
  kind.addEventListener("change", () => { date.value = task[kind.value] || ""; clear.checked = false; date.disabled = false; });
  clear.addEventListener("change", () => { date.disabled = clear.checked; });
  dates.append(element("p", "A review is not a deadline. Snoozing a review does not change a deadline. Daily focus does not roll over automatically.", "hint"));
  const dateSubmit = element("button", "Review date change"); dateSubmit.type = "submit"; dates.append(dateSubmit);
  dates.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!clear.checked && !date.value) { showNotice("Choose a date or explicitly select removal.", "error"); return; }
    mutate("change", { request_id: requestId(), path: task.path, revision: task.revision, change: { [kind.value]: clear.checked ? null : date.value } }, showProposal);
  });
  panel.append(dates);
  setBusy(state.busy);
}
function renderPreparation(panel, task) {
  append(panel, element("h3", "Make the next step easier"), element("p", "Choose a bounded assignment, then review its exact scope. AI output remains a draft until you verify the outcome."));
  if (!task.definition?.complete || !["ready", "doing", "verify"].includes(task.stage)) {
    panel.append(element("p", "This task needs a complete definition and owner-selected Ready, Doing or Verify stage before preparation.", "notice pending"));
    return;
  }
  const form = element("form"); form.id = "prepare-form";
  const kind = field(form, "kind", "Work to prepare", { options: [["prepare_task", "Private draft from this task"], ["research", "Bounded public research"]] });
  const scope = field(form, "scope", "Exact question or preparation scope", { multiline: true, max: 500, required: true });
  const limits = element("p", "Private preparation: one model call, no external tools, at most 45 seconds. It does not perform the real-world action.", "hint");
  form.append(limits);
  kind.addEventListener("change", () => {
    limits.textContent = kind.value === "research"
      ? "Use one impersonal public question. Never include private context. Research is limited to five sources, one short report and no follow-on jobs; provider costs may be separate."
      : "Private preparation: one model call, no external tools, at most 45 seconds. It does not perform the real-world action.";
  });
  const submit = element("button", "Review AI assignment", "primary"); submit.type = "submit"; form.append(submit);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    mutate("prepare", { request_id: requestId(), path: task.path, revision: task.revision, kind: kind.value, scope: scope.value.trim() }, showProposal);
  });
  panel.append(form);
  setBusy(state.busy);
}
function renderClose(panel, task) {
  append(panel, element("h3", "Close the outcome, not just the run"),
    element("p", "A draft, sent request or completed agent run is not enough. Record what actually happened and how you know."));
  if (!task.done_when) { panel.append(element("p", "Add a concrete Done when condition before closing this task.", "notice pending")); return; }
  panel.append(element("blockquote", task.done_when));
  const form = element("form"); form.id = "completion-form";
  const result = field(form, "result", "What was achieved?", { multiline: true, max: 1000, required: true });
  const evidence = field(form, "evidence", "Evidence or verification", { multiline: true, max: 1000, required: true, hint: "Describe or reference the permitted evidence. Keep private details in their own record." });
  const verifiedOn = field(form, "verified_on", "Date you verified the result", { type: "date", value: attentionDate(), required: true, hint: "This uses your configured task calendar and records verification, not an assumed date the work happened." });
  const learning = field(form, "learning", "Useful lesson", { multiline: true, max: 1000, hint: "Optional. What should you or an agent do differently next time?" });
  const attestation = element("label", null, "checkbox");
  const check = element("input"); check.type = "checkbox"; check.required = true;
  append(attestation, check, element("span", "I checked the done condition against the actual result and evidence."));
  form.append(attestation);
  const submit = element("button", "Review verified closure", "primary"); submit.type = "submit"; form.append(submit);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const completion = { result: result.value.trim(), evidence: evidence.value.trim(), verified_on: verifiedOn.value, verification: "owner" };
    if (learning.value.trim()) completion.learning = learning.value.trim();
    mutate("close", { request_id: requestId(), path: task.path, revision: task.revision, checked_done_when: check.checked, completion }, showProposal);
  });
  panel.append(form);
  setBusy(state.busy);
}
function showClarification(record) {
  state.clarification = record;
  state.tab = "overview";
  renderDetail();
  showNotice("Clarification is recorded separately. No task definition has been changed.");
}
function renderClarification(parent) {
  const record = state.clarification;
  const target = element("section", null, "clarification");
  append(target, element("h3", "One question at a time"),
    element("p", `${record.turns || 0} of 3 answers used for this source version. Unresolved work can stay in Clarify.`, "hint"));
  if (record.question && (record.turns || 0) < 3) {
    target.append(element("p", record.question));
    if (!/^[a-f0-9]{32}$/.test(record.question_token || "")) {
      target.append(element("p", "This question has no valid answer binding. Refresh the task and start clarification again.", "notice error"));
      parent.append(target);
      return;
    }
    const source = { path: state.selected.path, revision: state.selected.revision };
    const form = element("form"); form.id = "clarification-form";
    const answer = field(form, "answer", "Your answer", { multiline: true, max: 2000, required: true });
    const submit = element("button", "Record answer"); submit.type = "submit"; form.append(submit);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      mutate("clarify", {
        ...source, question_token: record.question_token, answer: answer.value.trim(), request_id: requestId(),
      }, showClarification);
    });
    target.append(form);
  } else target.append(element("p", "Review the proposed refinement or continue editing the task directly. No additional question is required."));
  if (record.changes && Object.keys(record.changes).length) {
    append(target, element("h4", "Proposed changes"), element("pre", JSON.stringify(record.changes, null, 2)),
      writeButton("Review this refinement", () => mutate("clarify-proposal", { clarification_id: record.id }, showProposal), "primary"));
  }
  parent.append(target);
}
function proposalTitle(record) {
  return ({
    capture_task: "Capture a task", refine_task: "Refine the definition", edit_task: "Change the task",
    close_task: "Verify and close", prepare_task: "Prepare a private draft", research: "Research a public question",
  })[record.kind] || "Task operation";
}
function closureScanPending(record) {
  return record.kind === "close_task" && record.closure_scan_protocol === 1
    && ["executing", "uncertain"].includes(record.status)
    && !record.publication_action_id && !record.result?.pr_url;
}
function confirmedClosureScan(record) {
  const result = record.result;
  return closureScanPending(record) && result?.status === "in_progress"
    && result.phase === "closure_scan" && result.resumable === true
    && /^[a-f0-9]{32}$/.test(result.closure_continuation || "")
    && Number.isInteger(result.scan_pass) && result.scan_pass >= 1 && result.scan_pass <= 6
    && result.scan_pass_limit === 6;
}
function closureConflictCode(value) {
  return typeof value === "string" && Object.hasOwn(CLOSURE_CONFLICTS, value) ? value : "task_closure_conflict";
}
function closureConflict(record) {
  if (record.kind !== "close_task" || record.status !== "invalidated") return null;
  const result = record.publish_result?.status === "conflict" ? record.publish_result : record.result;
  return result?.status === "conflict" ? CLOSURE_CONFLICTS[closureConflictCode(result.error)] : null;
}
function proposalStatusText(record) {
  const conflict = closureConflict(record);
  if (conflict) return `${conflict.text} Any new attempt needs a fresh proposal and approval.`;
  if (confirmedClosureScan(record)) {
    return `Closure check checkpoint confirmed (${record.result.scan_pass} of 6 scan passes recorded). The task is still open; no closure has been submitted. Continue only when you want the next bounded step.`;
  }
  if (closureScanPending(record)) {
    return "The last closure check is not confirmed. Recover that same request to inspect its progress; do not start another closure or assume the task is saved.";
  }
  if (record.status === "completed") {
    if (record.kind === "prepare_task") return "A private draft is available. The task is still open.";
    if (record.kind === "research") return "The research operation has a result. Verify the artifact; this does not complete the underlying task.";
    if (record.kind === "close_task") return "The approved closure reached the canonical record.";
    return "Saved to the canonical record. Closing the task still needs its evidence.";
  }
  if (record.status === "submitted") return "Submitted for publication. Not yet confirmed on the canonical branch.";
  if (["uncertain", "executing"].includes(record.status)) return "The result is not confirmed. Check this existing receipt; do not submit another operation.";
  if (record.status === "pending") return "Nothing will execute until you approve this exact scope and source version.";
  return "No new success is claimed. Review the recorded state before taking another action.";
}
function renderProposal(record) {
  const article = element("article", null, "proposal");
  const contentAvailable = record.action && (!record.source_status || record.source_status === "current");
  const conflict = closureConflict(record);
  append(article, statusBadge(record.status, conflict?.label), element("h3", proposalTitle(record)), element("p",
    contentAvailable ? proposalStatusText(record) : (record.text || "Historical receipt only. Source content is unavailable and cannot authorize a new action.")));
  if (conflict && !contentAvailable) article.append(element("p", proposalStatusText(record)));
  const source = element("p", null, "source");
  append(source, element("span", `${record.created_on || ""}${record.expires_on ? ` / expires ${record.expires_on}` : ""} `), safeLink("Source", record.source_url));
  article.append(source);
  if (contentAvailable) {
    const action = element("div", null, "exact-action");
    append(action, element("h4", "Exact action to approve"), element("pre", JSON.stringify(record.action, null, 2)));
    if (record.kind === "close_task" && record.closure_scan_protocol === 1) {
      action.append(element("p", "Closure may require up to six explicitly continued checks of the pinned reference inventory. No task is submitted or closed from a partial check, and another pass never runs automatically.", "hint"));
    }
    article.append(action);
  }
  if (contentAvailable && record.result?.preparation) {
    const data = record.result.preparation;
    const result = element("section", null, "result");
    append(result, element("h4", "Prepared, not verified"), element("p", data.summary));
    for (const [heading, items] of [["Suggested steps", data.steps], ["Uncertainties", data.uncertainties]]) {
      if (Array.isArray(items) && items.length) {
        result.append(element("h4", heading));
        const list = element("ul"); items.forEach((item) => list.append(element("li", item))); result.append(list);
      }
    }
    append(result, element("h4", "Your next action"), element("p", data.owner_next_action));
    if (data.source_quote) append(result, element("h4", "Source evidence"), element("blockquote", data.source_quote));
    article.append(result);
  }
  if (record.result || record.publish_result) {
    const receipt = (value) => {
      if (!value) return undefined;
      const projected = contentAvailable ? { ...value } : {
        status: value.status, action_id: value.action_id, target_action_id: value.target_action_id,
        canonical_revision: value.canonical_revision, error: value.error, pr_url: value.pr_url,
      };
      if (record.kind === "close_task" && projected.status === "conflict") {
        projected.error = closureConflictCode(projected.error);
      }
      return projected;
    };
    const details = element("details");
    append(details, element("summary", "Operation and publication receipts"),
      element("pre", JSON.stringify({ result: receipt(record.result), publication: receipt(record.publish_result) }, null, 2)));
    const url = record.publish_result?.pr_url || record.result?.pr_url || record.result?.url;
    append(article, details, safeLink("Open result or save status", url));
  }
  const actions = element("div", null, "actions");
  if (contentAvailable && record.status === "pending" && record.approval_digest) {
    actions.append(writeButton("Approve this exact action", () => mutate("decide", {
      proposal_id: record.id, approval_digest: record.approval_digest, decision: "approve",
    }, updateProposal), "primary"));
    actions.append(writeButton("Decline", () => mutate("decide", {
      proposal_id: record.id, approval_digest: record.approval_digest, decision: "decline",
    }, updateProposal)));
  }
  if (contentAvailable && confirmedClosureScan(record)) {
    actions.append(writeButton("Continue closure check", () => mutate("continue-closure", {
      proposal_id: record.id, request_id: requestId(), closure_continuation: record.result.closure_continuation,
    }, updateProposal), "primary"));
    actions.append(element("p", "One explicit step only. It may submit the approved closure once all pinned evidence is checked; publication remains separately verified.", "hint"));
  } else if (contentAvailable && closureScanPending(record)) {
    actions.append(writeButton("Recover last closure request", () => mutate("recover-closure", {
      proposal_id: record.id, request_id: requestId(),
    }, updateProposal)));
  } else if (["submitted", "executing", "uncertain"].includes(record.status)) {
    actions.append(writeButton("Check this existing request", () => mutate("reconcile", { proposal_id: record.id }, updateProposal)));
  }
  if (contentAvailable && record.status === "completed" && ["prepare_task", "research"].includes(record.kind) && !record.reviewed) {
    actions.append(writeButton("I've reviewed this result", () => mutate("review-result", { proposal_id: record.id }, async () => {
      showNotice("Result marked reviewed. The underlying task remains open.");
      await loadOverview();
    })));
  }
  if (!contentAvailable && record.status === "completed" && ["prepare_task", "research"].includes(record.kind) && !record.reviewed) {
    actions.append(writeButton("Acknowledge unavailable result", () => mutate("review-result", {
      proposal_id: record.id, acknowledge_unavailable: true,
    }, async () => {
      showNotice("Unavailable result notice acknowledged. No private content was restored or task closed.");
      await loadOverview();
    })));
  }
  article.append(actions);
  return article;
}
function showProposal(record) {
  const redacted = ["changed_or_removed", "unavailable"].includes(record.source_status);
  if (!/^[a-f0-9]{24}$/.test(record.id || "") || !record.status || (!record.action && !redacted)) {
    throw new Error("No complete proposal receipt was returned. Inspect Activity before starting another request.");
  }
  state.proposal = record;
  $("proposal-section").hidden = false;
  $("proposal-preview").replaceChildren(renderProposal(record));
  showNotice(redacted ? (record.text || "Source content is unavailable; only the operation receipt remains.") : proposalStatusText(record),
    record.status === "pending" && !redacted ? "" : "pending");
  $("proposal-section").scrollIntoView({ block: "start" });
}
async function updateProposal(record) {
  showProposal(record);
  await loadOverview();
  if (record.status === "completed" && record.kind === "close_task") closeDetail();
}
function renderActivity() {
  const target = $("activity-list");
  target.replaceChildren();
  const history = state.overview?.history || [];
  if (!state.overview) empty(target, "History is unavailable.", "Refresh before inferring that no work ran.");
  else if (!history.length) empty(target, "No task-workspace operations yet.", "Capture a task or prepare an action. Its proposals and receipts will appear here.");
  else history.forEach((record) => target.append(renderProposal(record)));
  $("history-more").hidden = state.overview?.history_next_offset === null || state.overview?.history_next_offset === undefined;
}
function renderAreas() {
  const data = state.overview;
  if (!data) return;
  $("area-list").replaceChildren();
  for (const area of data.areas) {
    const count = state.items.filter((task) => task.area === area).length;
    $("area-list").append(button(`${prettyId(area)} (${count} loaded)`, () => {
      $("area-filter").value = area; setView("tasks");
    }));
  }
  $("project-options").replaceChildren();
  if (!data.projects.length) empty($("project-options"), "No permitted projects returned.", "This is not permission to create or activate a project automatically.");
  for (const project of data.projects) {
    const label = element("label", null, "checkbox");
    const input = element("input"); input.type = "checkbox"; input.name = "project"; input.value = project.id;
    input.checked = Object.hasOwn(data.active_projects || {}, project.id);
    append(label, input, element("span", prettyId(project.id)));
    $("project-options").append(label);
  }
  const unseen = Object.keys(data.active_projects || {}).filter((id) => !data.projects.some((project) => project.id === id));
  $("project-page-status").textContent = `${data.projects.length} permitted projects loaded.${data.project_excluded_count ? ` ${data.project_excluded_count} checked candidates excluded by scope/privacy policy.` : ""}${data.project_next_offset !== null && data.project_next_offset !== undefined ? " More candidates remain; load more to inspect permitted projects." : ""}${unseen.length ? ` ${unseen.length} existing selection(s) outside this page will be retained. Load more to review them, or explicitly clear the selection.` : ""}`;
  $("projects-more").hidden = data.project_next_offset === null || data.project_next_offset === undefined;
  $("standing-enabled").checked = Boolean(data.standing?.enabled);
  $("standing-options").replaceChildren();
  const eligible = state.items.filter((task) => task.stage === "ready" && task.definition?.complete && Object.hasOwn(data.active_projects || {}, task.project));
  if (!eligible.length) empty($("standing-options"), "No eligible loaded tasks.", "Select a project and explicitly ready a fully defined task. Load more tasks if needed.");
  for (const task of eligible) {
    const label = element("label", null, "checkbox");
    const input = element("input"); input.type = "checkbox"; input.name = "standing-task"; input.value = task.path;
    input.checked = data.standing?.sources?.[task.path] === task.revision;
    append(label, input, element("span", task.title));
    $("standing-options").append(label);
  }
  const unloaded = Object.keys(data.standing?.sources || {}).filter((path) => !eligible.some((task) => task.path === path));
  if (unloaded.length) {
    $("standing-options").append(element("p", `${unloaded.length} previously authorized task reference(s) are not eligible on the loaded page. Saving replaces the selection with the checked tasks shown here.`, "notice pending"));
  }
  $("budget-details").textContent = JSON.stringify({ limits: data.limits, usage: data.budget }, null, 2);
  setBusy(state.busy);
}
function clearData() {
  clearAssist();
  dashboard.clear();
  clearTimeout(state.expiryTimer);
  state.expiryTimer = null;
  state.authExpires = 0;
  state.sessionGeneration += 1;
  state.detailGeneration += 1;
  state.csrf = ""; state.items = []; state.overview = null; state.selected = null;
  state.proposal = null; state.clarification = null; state.unknown = null;
  state.captureSource = null;
  $("capture-context").textContent = "";
  $("capture-context").hidden = true;
  renderCanonicalLinks();
  for (const id of ["task-list", "inspector", "activity-list", "proposal-preview", "project-options",
    "standing-options", "area-list", "capture-details", "warnings", "budget-details", "snapshot", "attention"]) $(id).replaceChildren();
  $("capture-form").reset();
  $("task-search").value = "";
  $("stage-filter").value = "";
  $("area-filter").value = "";
  $("capture-section").hidden = true;
  $("proposal-section").hidden = true;
  $("uncertain").hidden = true;
  showNotice("");
}
function expireSession() {
  clearData();
  $("workspace").hidden = true;
  $("access").hidden = false;
  $("sign-in").hidden = false;
  $("access-retry").hidden = false;
  $("access-message").textContent = "Sign in again with your personal account. Inspect Activity before repeating any unconfirmed operation.";
  for (const id of ["refresh", "capture-open", "sign-out"]) $(id).hidden = true;
  $("connection").textContent = "Signed out";
}
function checkSessionExpiry() {
  clearTimeout(state.expiryTimer);
  if (!state.authExpires) return;
  const remaining = state.authExpires - Date.now();
  if (remaining <= 0) {
    expireSession();
    return;
  }
  state.expiryTimer = setTimeout(checkSessionExpiry, Math.min(remaining, 2147483647));
}
async function start() {
  initWorkspace();
  clearData();
  $("access").hidden = false;
  $("workspace").hidden = true;
  $("sign-in").hidden = true;
  $("access-retry").hidden = true;
  $("access-message").textContent = "Checking your personal workspace. No private content is stored in this browser.";
  try {
    const session = await api("session");
    if (session.authenticated !== true || typeof session.csrf !== "string" || !session.csrf
      || !Number.isFinite(session.expires) || session.expires * 1000 <= Date.now()) throw new Error("The owner session could not be verified. Sign in again.");
    state.csrf = session.csrf;
    state.authExpires = session.expires * 1000;
    $("access").hidden = true;
    $("workspace").hidden = false;
    for (const id of ["refresh", "capture-open", "sign-out"]) $(id).hidden = false;
    $("connection").textContent = "Personal workspace";
    $("quick-bar").hidden = false;
    if (isMockMode()) $("connection").textContent = t("mock");
    checkSessionExpiry();
    await loadOverview();
    if (!state.selected) closeDetail();
    setView(location.hash.slice(1) || "today");
    await loadAssist();
  } catch (error) {
    state.csrf = "";
    $("access-message").textContent = error.status === 401
      ? "Sign in with your configured personal Microsoft account to see your work."
      : errorMessage(error);
    $("sign-in").hidden = ![401, 403].includes(error.status);
    $("access-retry").hidden = false;
    $("connection").textContent = error.status === 401 ? "Signed out" : "Access unavailable";
  }
}

for (const stage of STAGES) {
  const option = element("option", LABELS[stage]); option.value = stage; $("stage-filter").append(option);
}
document.querySelectorAll("[data-view]").forEach((node) => node.addEventListener("click", () => transitionView(node.dataset.view)));
for (const id of ["task-search", "stage-filter", "area-filter"]) $(id).addEventListener(id === "task-search" ? "input" : "change", renderTasks);
$("layout-list").addEventListener("click", () => setLayout("list"));
$("layout-board").addEventListener("click", () => setLayout("board"));
function setLayout(layout) {
  state.layout = layout;
  $("layout-list").setAttribute("aria-pressed", String(layout === "list"));
  $("layout-board").setAttribute("aria-pressed", String(layout === "board"));
  renderTasks();
}
$("refresh").addEventListener("click", () => {
  loadOverview();
  if (state.view === "today") dashboard.loadToday();
  if (state.view === "knowledge") dashboard.loadInbox();
});
$("scope-needs").addEventListener("click", () => setView("needs"));
$("scope-all").addEventListener("click", () => setView("tasks"));
window.addEventListener("hashchange", () => setView(location.hash.slice(1)));
$("load-more").addEventListener("click", () => loadOverview(true));
$("projects-more").addEventListener("click", loadProjects);
$("history-more").addEventListener("click", loadHistory);
$("access-retry").addEventListener("click", start);
document.addEventListener("visibilitychange", () => { if (!document.hidden) checkSessionExpiry(); });
window.addEventListener("focus", checkSessionExpiry);
window.addEventListener("pageshow", checkSessionExpiry);
function beginSourceCapture(source, finding) {
  state.captureSource = { id: source.id, revision: source.revision, finding: finding?.id || null };
  $("capture-form").reset();
  const text = finding?.next_step || `Review ${source.title} and choose a concrete next action.`;
  $("capture-text").value = text;
  $("capture-title-input").value = Array.from(text).slice(0, 120).join("");
  $("capture-context").textContent = `From ${source.title}${finding ? `, ${finding.id}` : ""}. Canonical provenance and exact evidence will be retained and rechecked before approval.`;
  $("capture-context").hidden = false;
  openCapture();
}
function openCapture() {
  $("capture-section").hidden = false;
  $("capture-text").focus();
  $("capture-section").scrollIntoView({ block: "start" });
}
$("capture-open").addEventListener("click", () => {
  if (state.captureSource) $("capture-form").reset();
  state.captureSource = null;
  $("capture-context").hidden = true;
  openCapture();
});
$("capture-cancel").addEventListener("click", () => {
  if (state.captureSource) $("capture-form").reset();
  state.captureSource = null;
  $("capture-context").hidden = true;
  $("capture-section").hidden = true; $("capture-open").focus();
});
$("proposal-close").addEventListener("click", () => { $("proposal-section").hidden = true; state.proposal = null; });
$("recover-request").addEventListener("click", () => {
  if (state.unknown && !state.busy) performMutation(state.unknown);
});
$("capture-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const text = String(form.get("text") || "");
  const firstLine = text.split(/\r?\n/).find((line) => line.trim())?.trim() || "";
  const title = String(form.get("title") || "").trim() || Array.from(firstLine).slice(0, 120).join("");
  if (!text.trim() || !title) return;
  const definition = { title, stage: "clarify" };
  for (const name of ["area", "project", "execution", "outcome", "next_action", "done_when"]) {
    const value = String(form.get(name) || "").trim();
    if (value) definition[name] = value;
  }
  const source = state.captureSource;
  mutate(source ? "dashboard/capture" : "capture", { request_id: requestId(), text, definition, ...(source || {}) }, async (record) => {
    showProposal(record);
    $("capture-form").reset();
    $("capture-section").hidden = true;
    state.captureSource = null;
    $("capture-context").hidden = true;
    await loadOverview();
  });
});
$("projects-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const unseen = Object.keys(state.overview?.active_projects || {}).filter((id) => !state.overview.projects.some((project) => project.id === id));
  const projects = [...new Set([...new FormData(event.currentTarget).getAll("project"), ...unseen])];
  if (projects.length > 8) { showNotice("Choose at most eight projects.", "error"); return; }
  mutate("projects", { projects }, async () => { showNotice("Project selection saved. Standing preparation is off until you approve its scope again."); await loadOverview(); });
});
$("projects-clear").addEventListener("click", () => mutate("projects", { projects: [] }, async () => {
  showNotice("Project selection cleared. Standing preparation is off; no source project or task was removed.");
  await loadOverview();
}));
$("projects-clear").dataset.write = "true";
$("standing-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const enabled = $("standing-enabled").checked;
  const paths = enabled ? new FormData(event.currentTarget).getAll("standing-task") : [];
  if (paths.length > 3 || (enabled && !paths.length)) { showNotice("Choose one to three eligible tasks before enabling preparation.", "error"); return; }
  const sources = {};
  for (const path of paths) {
    const task = state.items.find((item) => item.path === path);
    if (!task) { showNotice("A selected source is no longer loaded. Refresh before authorizing it.", "error"); return; }
    sources[path] = task.revision;
  }
  mutate("standing", { enabled, sources }, async () => { showNotice(enabled ? "Standing permission saved for those exact sources. No real-world action is authorized." : "Standing preparation is off."); await loadOverview(); });
});
$("standing-stop").addEventListener("click", () => mutate("standing", { enabled: false, sources: {} }, async () => { showNotice("Standing preparation is off. Existing results and receipts are retained."); await loadOverview(); }));
$("standing-stop").dataset.write = "true";
$("sign-out").addEventListener("click", async () => {
  if (state.busy) { showNotice("A request is still returning. Sign out as soon as its response or timeout is reported.", "pending"); return; }
  const unconfirmed = Boolean(state.unknown);
  setBusy(true);
  try {
    if (isMockMode()) { expireSession(); return; }
    const response = await fetch("/api/tasks/auth/logout", {
      method: "POST", credentials: "same-origin", mode: "same-origin", cache: "no-store",
      headers: { "Content-Type": "application/json", "X-MindMe-CSRF": state.csrf }, body: "{}",
    });
    if (response.status === 401) { expireSession(); return; }
    if (!response.ok) throw new Error("Sign-out was not confirmed. Try again before leaving a shared device.");
    clearData();
    for (const id of ["capture-open", "refresh", "sign-out"]) $(id).hidden = true;
    await start();
    if (unconfirmed) $("access-message").textContent = "Signed out. One earlier request was unconfirmed; inspect Activity after signing in before creating a replacement.";
  } catch (error) {
    showNotice(errorMessage(error), "error");
  } finally {
    setBusy(false);
  }
});

const assist = {
  available: false, initialized: false, language: "en", threads: new Map(),
  dragPath: null, recorder: null, stream: null, voiceKey: null, voiceTimer: null,
  voiceGeneration: 0, processingVoice: false, acquiringVoice: false, speak: false, toastTimer: null,
  polls: new Map(),
};
const COPY = {
  en: {
    today: "Today", flow: "Flow", copilot: "Copilot", capture: "+ Capture",
    refresh: "Refresh", signOut: "Sign out", knowledgeNav: "Knowledge", areasNav: "Areas & projects", activityNav: "Activity",
    captureTitle: "Get it out of your head", closeCapture: "Close capture", captureQuestion: "What do you need to do?",
    captureDetails: "Add the details you already know", reviewCapture: "Review capture",
    needs: "Needs you", allTasks: "All tasks", list: "List", search: "Search loaded tasks",
    searchPlaceholder: "Find an outcome or next action", stage: "Stage", area: "Area",
    back: "Back to tasks", overviewTab: "Overview", editTab: "Edit & plan", aiTab: "AI help", closeTab: "Close",
    chatTab: "✦ Chat",
    knowledgeDescription: "Understand the finding. Check its evidence. Choose what happens next.",
    needsDescription: "Decisions, due follow-ups and the work you selected.",
    areasDescription: "Keep responsibilities visible. Choose which outcomes to advance.",
    activityDescription: "Proposals, preparation and the evidence of follow-through.",
    personal: "PERSONAL INTELLIGENCE", hero: "Make space for what matters.",
    heroDetail: "A clear outcome. A useful next step. A copilot to get you there.",
    briefing: "✦ Plan my day", work: "Your work, in motion", board: "Flow board",
    todayDescription: "Your focus, the next move, and intelligence to move it forward.",
    flowDescription: "From a rough idea to a verified outcome. One flow, with AI alongside.",
    inbox: "Inbox / Clarify", ready: "Ready", doing: "Doing", waiting: "Waiting", verify: "Verify", done: "Done",
    outcome: "OUTCOME", doneWhen: "DONE WHEN", notDefined: "Not defined yet. Let's make it concrete.",
    nextAction: "Next action", taskDetails: "Dates, execution & source details",
    taskCopilot: "✦ Task copilot", welcome: "What shall we move forward?",
    welcomeDetail: "Describe an outcome, untangle a task, or ask for a next step. English or Russian, text or voice.",
    taskWelcome: "Let's make this task easier.",
    taskWelcomeDetail: "Ask me to sharpen the outcome, estimate the work, build a plan, or prepare the next action.",
    placeholder: "Ask, capture, or give an instruction…", send: "Send ↗", you: "YOU", ai: "MINDME",
    thinking: "✦ Working with your canonical sources…", speak: "Read replies",
    globalContext: "Across your tasks · Changes follow the server's approval and publication rules.",
    taskContext: "This task only · Exact source version · Conversation stays in memory.",
    refine: "Sharpen the outcome", refinePrompt: "Make the outcome and done condition concrete.",
    plan: "Build a plan", planPrompt: "Propose a practical plan with estimates and who does each step.",
    next: "Find the next move", nextPrompt: "Suggest the best next action and explain why.",
    captureStep: "Capture", detailStep: "Detail", estimateStep: "Estimate", planStep: "Plan",
    executeStep: "Execute", resolveStep: "Resolve", learnStep: "Learn",
    stepHelp: "Prepare this AI step. A completed definition is not proof of completed work.",
    record: "Record voice instruction", stop: "Stop recording", recording: "Listening · tap to finish",
    finishVoice: "Finish the voice recording before starting another request.",
    voiceSourceChanged: "The task changed while recording or recovering. Review the transcript draft against the current source before sending it.",
    voiceTypeFallback: "You can type your instruction instead. No voice instruction was sent.",
    transcribing: "Transcribing English / Russian…", voiceUnsupported: "Voice recording is unavailable in this browser. Type your instruction instead.",
    voiceDenied: "Microphone access was not granted. No audio was sent. You can type instead.",
    voiceEmpty: "No usable speech was returned. No instruction was sent.",
    voiceError: "The recording could not be completed. No instruction was sent.",
    voiceTooLarge: "The recording exceeded 2 MB. Record a shorter instruction.",
    approve: "Approve", edit: "Edit", skip: "Skip", undo: "Undo",
    applied: "Change applied", proposal: "Your decision", draft: "Draft · not executed",
    applying: "Applying · publication pending", saved: "Saved to canonical source", appliedUnknown: "Change receipt · check Activity",
    inspect: "Inspect Activity", failedChange: "Change not saved · inspect Activity",
    knowledge: "Learning · not published", estimated: "Estimated effort", planLabel: "Suggested plan",
    before: "Before", after: "After", receipt: "Receipt", preview: "Exact server action",
    unavailable: "AI is unavailable. Manual capture, task editing and Activity still work.",
    unknown: "This request is unconfirmed. Recover the same request above; do not send a replacement.",
    budget: "AI budget reached. Manual task tools remain available.",
    rate: "AI is busy. Wait before making a new request.",
    model: "The AI model is unavailable. No change is claimed.",
    mock: "LOCAL DEMO · synthetic data", boardHelp: "Flow board. Drag a task to prepare a stage change. Open the task for keyboard-accessible stage editing.",
    closeHint: "Done needs checked evidence. Use verified closure; a drag cannot close the task.",
    stageHint: "Stage change prepared for review. The card stays at its saved stage until canonical publication.",
    dayPrompt: "Help plan today. Suggest up to three tasks with reasons and a next action, using current canonical task sources. Keep deadlines and unknowns explicit.",
    showTask: "Open task", confidence: "Confidence", permission: "This proposal has no usable approval binding. Refresh Activity.",
  },
  ru: {
    today: "Сегодня", flow: "Поток", copilot: "Копилот", capture: "+ Задача",
    refresh: "Обновить", signOut: "Выйти", knowledgeNav: "Знания", areasNav: "Сферы и проекты", activityNav: "История",
    captureTitle: "Освободите голову", closeCapture: "Закрыть ввод", captureQuestion: "Что нужно сделать?",
    captureDetails: "Добавить известные детали", reviewCapture: "Проверить запись",
    needs: "Требует вас", allTasks: "Все задачи", list: "Список", search: "Поиск среди загруженных задач",
    searchPlaceholder: "Найти результат или следующий шаг", stage: "Этап", area: "Сфера",
    back: "К задачам", overviewTab: "Обзор", editTab: "Правки и план", aiTab: "Помощь ИИ", closeTab: "Завершение",
    chatTab: "✦ Чат",
    knowledgeDescription: "Разберите вывод. Проверьте доказательства. Выберите следующий шаг.",
    needsDescription: "Решения, сроки пересмотра и выбранная вами работа.",
    areasDescription: "Держите обязанности в поле зрения. Выберите результат для продвижения.",
    activityDescription: "Предложения, подготовка и подтверждения результата.",
    personal: "ЛИЧНЫЙ ИНТЕЛЛЕКТ", hero: "Освободите место для важного.",
    heroDetail: "Ясный результат. Полезный следующий шаг. Копилот, который поможет.",
    briefing: "✦ Спланировать день", work: "Ваши задачи в движении", board: "Доска",
    todayDescription: "Ваш фокус, следующий шаг и помощь ИИ в работе.",
    flowDescription: "От идеи до проверенного результата. Один поток, вместе с ИИ.",
    inbox: "Входящие / Уточнить", ready: "Готово к работе", doing: "В работе", waiting: "Ожидание", verify: "Проверить", done: "Завершено",
    outcome: "РЕЗУЛЬТАТ", doneWhen: "КРИТЕРИЙ ГОТОВНОСТИ", notDefined: "Пока не определено. Давайте уточним.",
    nextAction: "Следующий шаг", taskDetails: "Даты, выполнение и источник",
    taskCopilot: "✦ Копилот задачи", welcome: "Что будем продвигать?",
    welcomeDetail: "Опишите результат, разберите задачу или попросите следующий шаг. Текстом или голосом, по-русски или по-английски.",
    taskWelcome: "Сделаем эту задачу проще.",
    taskWelcomeDetail: "Попросите уточнить результат, оценить работу, составить план или подготовить следующий шаг.",
    placeholder: "Вопрос, идея или поручение…", send: "Отправить ↗", you: "ВЫ", ai: "MINDME",
    thinking: "✦ Работаю с каноническими источниками…", speak: "Читать ответы",
    globalContext: "Все задачи · Изменения подчиняются правилам подтверждения и публикации сервера.",
    taskContext: "Только эта задача · Точная версия источника · Чат хранится только в памяти.",
    refine: "Уточнить результат", refinePrompt: "Сделай результат и критерий готовности конкретными.",
    plan: "Составить план", planPrompt: "Предложи практический план с оценкой времени и исполнителем каждого шага.",
    next: "Найти следующий шаг", nextPrompt: "Предложи лучший следующий шаг и объясни почему.",
    captureStep: "Записать", detailStep: "Уточнить", estimateStep: "Оценить", planStep: "План",
    executeStep: "Выполнить", resolveStep: "Завершить", learnStep: "Усвоить",
    stepHelp: "Подготовить этот шаг с ИИ. Полное описание не означает выполненную работу.",
    record: "Записать голосовое поручение", stop: "Остановить запись", recording: "Слушаю · нажмите для завершения",
    finishVoice: "Завершите запись голоса перед новым запросом.",
    voiceSourceChanged: "Задача изменилась во время записи или восстановления. Проверьте черновик расшифровки по текущему источнику перед отправкой.",
    voiceTypeFallback: "Можно ввести поручение текстом. Голосовое поручение не отправлено.",
    transcribing: "Распознаю русский / английский…", voiceUnsupported: "В этом браузере нет записи голоса. Введите поручение текстом.",
    voiceDenied: "Доступ к микрофону не предоставлен. Аудио не отправлено. Можно написать текстом.",
    voiceEmpty: "Речь не распознана. Поручение не отправлено.",
    voiceError: "Не удалось завершить запись. Поручение не отправлено.",
    voiceTooLarge: "Запись больше 2 МБ. Запишите более короткое поручение.",
    approve: "Подтвердить", edit: "Изменить", skip: "Пропустить", undo: "Отменить",
    applied: "Изменение применено", proposal: "Ваше решение", draft: "Черновик · не выполнено",
    applying: "Применяется · ожидает публикации", saved: "Сохранено в каноническом источнике", appliedUnknown: "Квитанция изменения · проверьте историю",
    inspect: "Проверить историю", failedChange: "Не сохранено · проверьте историю",
    knowledge: "Вывод · не опубликован", estimated: "Оценка работы", planLabel: "Предложенный план",
    before: "Было", after: "Стало", receipt: "Квитанция", preview: "Точное действие сервера",
    unavailable: "ИИ недоступен. Ручной ввод, редактирование задач и история работают.",
    unknown: "Исход запроса неизвестен. Восстановите тот же запрос выше, не отправляйте новый.",
    budget: "Лимит ИИ исчерпан. Ручные инструменты доступны.",
    rate: "ИИ занят. Подождите перед новым запросом.",
    model: "Модель ИИ недоступна. Изменение не подтверждено.",
    mock: "ЛОКАЛЬНОЕ ДЕМО · вымышленные данные", boardHelp: "Доска задач. Перетащите задачу для подготовки смены этапа. Этап можно изменить с клавиатуры в задаче.",
    closeHint: "Завершение требует проверки. Перетаскивание не закрывает задачу.",
    stageHint: "Смена этапа подготовлена для проверки. Карточка остается на сохранённом этапе до канонической публикации.",
    dayPrompt: "Помоги спланировать сегодня. Предложи до трёх задач с причинами и следующим шагом на основе текущих канонических источников. Явно укажи сроки и неизвестное.",
    showTask: "Открыть задачу", confidence: "Уверенность", permission: "Нет привязки подтверждения. Обновите историю.",
  },
};
function t(key) { return COPY[assist.language]?.[key] || COPY.en[key] || key; }
let activeViewTransition = null;
function transitionView(view) {
  activeViewTransition?.skipTransition();
  if (document.startViewTransition && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
    const transition = document.startViewTransition(() => setView(view));
    activeViewTransition = transition;
    document.documentElement.dataset.transitioning = "true";
    transition.finished.catch(() => {}).finally(() => {
      if (activeViewTransition !== transition) return;
      activeViewTransition = null;
      delete document.documentElement.dataset.transitioning;
    });
  } else setView(view);
}
function threadFor(key) {
  if (!assist.threads.has(key)) assist.threads.set(key, { id: null, messages: [], suggestions: [], draft: "", pending: false });
  return assist.threads.get(key);
}
function taskReference(key) {
  if (key === "global") return null;
  const task = state.selected?.path === key && state.selected.revision ? state.selected : state.items.find((item) => item.path === key);
  return task?.revision ? { path: task.path, revision: task.revision } : null;
}
function initWorkspace() {
  if (assist.initialized) return;
  assist.initialized = true;
  assist.language = navigator.language?.toLowerCase().startsWith("ru") ? "ru" : "en";
  $("theme-toggle").addEventListener("click", () => {
    const dark = document.documentElement.dataset.theme
      ? document.documentElement.dataset.theme === "dark" : !matchMedia("(prefers-color-scheme: light)").matches;
    document.documentElement.dataset.theme = dark ? "light" : "dark";
    $("theme-toggle").setAttribute("aria-label", dark ? "Use dark theme" : "Use light theme");
  });
  $("language-toggle").addEventListener("click", () => {
    assist.language = assist.language === "en" ? "ru" : "en";
    translateWorkspace(); setView(state.view, false);
    if (state.selected?.revision) renderDetail();
    renderChats();
  });
  for (const id of ["command-open", "nav-copilot", "quick-chat"]) $(id).addEventListener("click", () => openCopilot());
  $("copilot-close").addEventListener("click", () => $("copilot-dialog").close());
  $("quick-capture").addEventListener("click", () => $("capture-open").click());
  $("briefing-open").addEventListener("click", () => { openCopilot(); sendChat("global", t("dayPrompt")); });
  for (const id of ["quick-mic", "header-mic"]) $(id).addEventListener("click", () => {
    const key = state.selected?.revision && ["tasks", "needs"].includes(state.view) ? state.selected.path : "global";
    if (key === "global") openCopilot();
    toggleVoice(key);
  });
  $("copilot-dialog").addEventListener("close", () => { if (assist.voiceKey === "global") cancelVoice(); });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && state.selected && !$("copilot-dialog").open
      && $("tasks-section").classList.contains("layout-board") && matchMedia("(min-width: 1101px) and (max-width: 1599px)").matches) {
      closeDetail();
    }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k" && state.csrf && assist.available) {
      event.preventDefault(); openCopilot();
    }
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) for (const poll of assist.polls.values()) {
      if (!poll.timer) scheduleReceiptPoll(poll);
    }
  });
  translateWorkspace();
}
function translateWorkspace() {
  document.documentElement.lang = assist.language;
  $("language-toggle").textContent = assist.language.toUpperCase();
  document.querySelectorAll("[data-i18n]").forEach((node) => { node.textContent = t(node.dataset.i18n); });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((node) => { node.placeholder = t(node.dataset.i18nPlaceholder); });
  for (const id of ["quick-mic", "header-mic"]) $(id).setAttribute("aria-label", t("record"));
}
async function loadAssist() {
  const generation = state.sessionGeneration;
  try {
    const usage = await api("assist/usage");
    if (generation !== state.sessionGeneration) return;
    if (!Number.isFinite(usage.month_eur) || !Number.isFinite(usage.month_cap_eur)) throw new Error(t("unavailable"));
    assist.available = true;
    $("assist-usage").replaceChildren(element("span", `AI · €${usage.month_eur.toFixed(2)} / €${usage.month_cap_eur.toFixed(2)}`));
    const meter = element("meter", null, "usage-meter");
    meter.min = 0; meter.max = Math.max(usage.month_cap_eur, 0.01); meter.value = usage.month_eur;
    meter.setAttribute("aria-label", "Monthly AI budget");
    $("assist-usage").append(meter);
    const details = element("details");
    append(details, element("summary", assist.language === "ru" ? "Лимиты ИИ" : "AI limits"),
      element("p", `Today · €${usage.today_eur} / €${usage.today_cap_eur} · ${usage.calls_today} calls`),
      element("p", Object.entries(usage.models || {}).map(([role, model]) => `${role}: ${model}`).join(" · ")));
    $("assist-usage").append(details);
  } catch (error) {
    if (generation !== state.sessionGeneration) return;
    assist.available = false;
    $("assist-usage").textContent = t("unavailable");
    if (error.status !== 404) showNotice(errorMessage(error), "error");
  }
  document.querySelectorAll(".ai-feature").forEach((node) => { node.hidden = !assist.available; });
  if (state.selected?.revision) renderDetail();
}
function clearAssist() {
  cancelVoice();
  assist.available = false; assist.threads.clear(); assist.dragPath = null;
  clearTimeout(assist.toastTimer);
  for (const poll of assist.polls.values()) clearTimeout(poll.timer);
  assist.polls.clear();
  if (typeof window.speechSynthesis !== "undefined") window.speechSynthesis.cancel();
  $("copilot-dialog").close?.();
  $("global-chat").replaceChildren();
  $("today-priorities").replaceChildren();
  $("toast").replaceChildren(); $("toast").hidden = true;
  $("assist-usage").replaceChildren();
  $("quick-bar").hidden = true;
  document.querySelectorAll(".ai-feature").forEach((node) => { node.hidden = true; });
}
function openCopilot() {
  if (!assist.available || !state.csrf) return;
  $("global-chat").dataset.chatKey = "global";
  renderChat($("global-chat"), "global");
  if (!$("copilot-dialog").open) $("copilot-dialog").showModal();
  $("global-chat").querySelector("textarea")?.focus();
}
function buildStepRail(task) {
  const rail = element("nav", null, "step-rail");
  rail.setAttribute("aria-label", t("stepHelp"));
  const cards = threadFor(task.path).messages.flatMap((message) => message.cards || []);
  const complete = {
    capture: true, detail: Boolean(task.definition?.complete),
    estimate: cards.some((card) => card.kind === "estimate"), plan: cards.some((card) => card.kind === "plan"),
    execute: false, resolve: task.stage === "done", learn: Boolean(task.completion?.learning),
  };
  for (const step of ["capture", "detail", "estimate", "plan", "execute", "resolve", "learn"]) {
    const node = writeButton("", () => sendStep(task.path, step));
    node.title = t("stepHelp");
    node.dataset.complete = String(complete[step]);
    append(node, element("span", complete[step] ? "✓" : "✦", "step-marker"), element("span", t(`${step}Step`)));
    rail.append(node);
  }
  return rail;
}
function renderChats() {
  for (const id of ["quick-mic", "header-mic"]) $(id).setAttribute("aria-pressed", String(Boolean(assist.recorder)));
  if (!assist.available) return;
  document.querySelectorAll("[data-chat-key]").forEach((host) => renderChat(host, host.dataset.chatKey));
}
function renderChat(host, key) {
  const thread = threadFor(key);
  const active = host.querySelector("textarea") === document.activeElement;
  const start = active ? document.activeElement.selectionStart : null;
  const shell = element("div", null, "chat-shell");
  shell.append(element("p", t(key === "global" ? "globalContext" : "taskContext"), "chat-context"));
  const messages = element("div", null, "chat-messages");
  messages.setAttribute("role", "log"); messages.setAttribute("aria-live", "polite");
  if (!thread.messages.length) {
    messages.append(append(element("div", null, "chat-welcome"),
      element("h3", t(key === "global" ? "welcome" : "taskWelcome")),
      element("p", t(key === "global" ? "welcomeDetail" : "taskWelcomeDetail"))));
  }
  for (const message of thread.messages) {
    if (message.text) {
      const node = element("div", null, `chat-message ${message.role}`);
      append(node, element("span", t(message.role === "user" ? "you" : "ai"), "message-role"), element("span", message.text));
      messages.append(node);
    }
    for (const card of message.cards || []) messages.append(renderAssistCard(card, key));
  }
  if (thread.pending) messages.append(element("div", t("thinking"), "thinking"));
  if (state.unknown) messages.append(element("p", t("unknown"), "notice pending"));
  shell.append(messages);
  const suggestions = element("div", null, "chat-suggestions");
  const ideas = thread.suggestions.length ? thread.suggestions : [
    { label: t("refine"), message: t("refinePrompt") }, { label: t("plan"), message: t("planPrompt") }, { label: t("next"), message: t("nextPrompt") },
  ];
  for (const suggestion of ideas) suggestions.append(writeButton(suggestion.label, () => sendChat(key, suggestion.message)));
  shell.append(suggestions);
  if (assist.voiceKey === key || (assist.processingVoice && assist.processingVoice === key)) {
    const waveform = element("span", null, "waveform"); waveform.setAttribute("aria-hidden", "true");
    for (let i = 0; i < 7; i += 1) waveform.append(element("span"));
    shell.append(append(element("div", null, "voice-status"), waveform, element("span", t(assist.recorder ? "recording" : "transcribing"))));
  }
  const form = element("form", null, "chat-form");
  const input = element("textarea"); input.rows = 2; input.maxLength = 4000;
  input.placeholder = t("placeholder"); input.value = thread.draft;
  input.setAttribute("aria-label", t("placeholder"));
  input.disabled = state.busy || Boolean(state.unknown) || Boolean(assist.processingVoice);
  input.addEventListener("input", () => { thread.draft = input.value; });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing && !matchMedia("(pointer: coarse)").matches) {
      event.preventDefault(); form.requestSubmit();
    }
  });
  const actions = element("div", null, "chat-compose-actions");
  const mic = button(assist.voiceKey === key ? "■" : "●", () => toggleVoice(key), "mic-button");
  mic.disabled = (state.busy || Boolean(state.unknown) || Boolean(assist.processingVoice)) && assist.voiceKey !== key;
  mic.setAttribute("aria-label", t(assist.voiceKey === key ? "stop" : "record"));
  mic.setAttribute("aria-pressed", String(assist.voiceKey === key));
  const speech = element("label", null, "checkbox speech-toggle");
  const toggle = element("input"); toggle.type = "checkbox"; toggle.checked = assist.speak;
  toggle.addEventListener("change", () => { assist.speak = toggle.checked; if (!assist.speak) window.speechSynthesis?.cancel(); });
  append(speech, toggle, element("span", t("speak")));
  const submit = element("button", t("send"), "primary"); submit.type = "submit";
  submit.disabled = state.busy || Boolean(state.unknown) || Boolean(assist.processingVoice) || Boolean(assist.recorder);
  append(actions, mic, speech, submit); append(form, input, actions);
  form.addEventListener("submit", (event) => { event.preventDefault(); sendChat(key, input.value); });
  shell.append(form); host.replaceChildren(shell);
  if (active) { input.focus({ preventScroll: true }); input.setSelectionRange(start, start); }
  messages.scrollTop = messages.scrollHeight;
}
function safeCardValue(value) {
  if (value == null) return "—";
  return typeof value === "object" ? JSON.stringify(value, null, 2) : String(value);
}
function renderAssistCard(card, key) {
  const supported = ["applied", "proposal", "task", "plan", "estimate", "knowledge", "draft"];
  const kind = supported.includes(card.kind) ? card.kind : "draft";
  const target = element("article", null, `ai-card ${kind}`);
  const badge = { applied: "applied", proposal: "proposal", plan: "planLabel", estimate: "estimated", knowledge: "knowledge", draft: "draft" }[kind];
  if (badge) target.append(element("span", kind === "applied" ? appliedCardStatus(card) : t(badge), "status"));
  if (card.title) target.append(element("h4", card.title));
  if (card.summary) target.append(element("p", card.summary));
  if (card.body) target.append(element("p", card.body));
  if (Array.isArray(card.diff)) {
    const diff = element("dl", null, "ai-diff");
    for (const change of card.diff) append(diff, element("dt", change.field),
      element("dd", `${t("before")}: ${safeCardValue(change.before)}`, "before"),
      element("dd", `${t("after")}: ${safeCardValue(change.after)}`));
    target.append(diff);
  }
  if (kind === "plan") {
    for (const step of card.steps || []) {
      const label = element("label", null, "checkbox plan-check");
      const check = element("input"); check.type = "checkbox"; check.checked = Boolean(step.uiChecked);
      check.addEventListener("change", () => { step.uiChecked = check.checked; });
      append(label, check, element("span", `${step.text} · ${step.estimate_min ?? "?"} min · ${step.who}`));
      target.append(label);
    }
    target.append(element("p", `${card.total_estimate_min ?? "?"} min · ${t("draft")}`, "hint"));
  }
  if (kind === "estimate") {
    append(target, element("h4", `${card.minutes_low}–${card.minutes_high} min`),
      element("p", `${t("confidence")}: ${safeCardValue(card.confidence)}`, "hint"), element("p", card.rationale));
  }
  const actions = element("div", null, "actions");
  if (kind === "applied" && card.receipt_id) {
    if (isCardSaved(card) && card.undo_available) actions.append(writeButton(t("undo"), () => undoAssist(key, card.receipt_id)));
    if (!isCardSaved(card)) actions.append(button(t("inspect"), () => {
      $("copilot-dialog").close(); transitionView("activity");
    }));
    target.append(element("p", `${t("receipt")}: ${card.receipt_id}`, "hint"));
  }
  if (kind === "proposal") {
    if (card.action) {
      const details = element("details");
      append(details, element("summary", t("preview")), element("pre", safeCardValue(card.action)));
      target.append(details);
    }
    if (!card.decided) {
      actions.append(writeButton(t("approve"), () => decideAssist(key, card, "approve"), "primary"),
        writeButton(t("edit"), () => {
          threadFor(key).draft = `${t("edit")}: ${card.title || card.summary || ""}\n`;
          renderChats();
          document.querySelector(`[data-chat-key="${CSS.escape(key)}"] textarea`)?.focus();
        }), writeButton(t("skip"), () => decideAssist(key, card, "decline")));
    } else target.append(element("p", card.decisionStatus || card.decided, "hint"));
  }
  if (kind === "task" && typeof card.path === "string") {
    actions.append(button(t("showTask"), () => {
      $("copilot-dialog").close(); setView("tasks"); openTask(card.path);
    }));
  }
  target.append(actions);
  return target;
}
function assistError(error) {
  return ({ budget_exhausted: t("budget"), rate_limited: t("rate"), model_unavailable: t("model") })[error.code] || errorMessage(error);
}
async function receiveAssist(key, response, operation = "assist/chat") {
  const generation = state.sessionGeneration;
  const cardsOnly = operation === "assist/change";
  if (!Array.isArray(response.cards) || response.cards.some((card) => !card || typeof card.kind !== "string")
    || (!cardsOnly && (typeof response.reply !== "string" || typeof response.thread_id !== "string"))) {
    throw new Error("The AI response is incomplete. Inspect Activity before repeating any instruction.");
  }
  const thread = threadFor(key);
  showNotice("");
  if (typeof response.thread_id === "string") thread.id = response.thread_id;
  thread.messages.push({ role: "assistant", text: typeof response.reply === "string" ? response.reply : "", cards: response.cards });
  thread.suggestions = Array.isArray(response.suggestions) ? response.suggestions : [];
  renderChats();
  for (const card of response.cards) {
    if (card.kind === "applied") {
      showToast(`${card.title || t("applied")} · ${appliedCardStatus(card)}`, isCardSaved(card) && card.undo_available ? () => undoAssist(key, card.receipt_id) : null);
      watchReceipt(key, card);
    }
  }
  if (response.cards.some((card) => ["applied", "task"].includes(card.kind))) {
    const wasSelected = state.selected?.path === key;
    await loadOverview();
    if (generation !== state.sessionGeneration) return;
    if (key !== "global" && wasSelected && (!state.selected || state.selected.path === key)) await openTask(key);
  } else if (key !== "global" && state.selected?.path === key && state.selected?.revision) renderDetail();
  if (generation !== state.sessionGeneration) return;
  if (response.reply && assist.speak && window.speechSynthesis && typeof SpeechSynthesisUtterance !== "undefined") {
    const utterance = new SpeechSynthesisUtterance(response.reply);
    utterance.lang = response.language === "ru" ? "ru-RU" : "en-US";
    window.speechSynthesis.speak(utterance);
  }
}
function appliedCardStatus(card) {
  if (isCardSaved(card)) return t("saved");
  if (["submitted", "executing"].includes(card.status)) return t("applying");
  if (["failed", "invalidated", "declined", "expired"].includes(card.status)) return t("failedChange");
  return t("appliedUnknown");
}
function isCardSaved(card) {
  return card.saved === true && card.status === "completed" && card.publication_status === "merged"
    && card.source_status === "current";
}
function watchReceipt(key, card) {
  if (isMockMode() || !/^[a-f0-9]{24}$/.test(card.receipt_id || "")
    || !["submitted", "executing"].includes(card.status) || assist.polls.has(card.receipt_id)) return;
  card.undo_available = false;
  const poll = { key, card, generation: state.sessionGeneration, started: Date.now(), attempt: 0, timer: null, requestId: requestId() };
  assist.polls.set(card.receipt_id, poll);
  scheduleReceiptPoll(poll);
}
function scheduleReceiptPoll(poll) {
  if (poll.timer || document.hidden || poll.generation !== state.sessionGeneration) return;
  const delay = Math.min(2000 * 2 ** poll.attempt, 32000);
  if (Date.now() + delay - poll.started > 120000 || poll.attempt >= 7) {
    assist.polls.delete(poll.card.receipt_id); return;
  }
  poll.timer = setTimeout(() => { poll.timer = null; pollReceipt(poll); }, delay);
}
async function pollReceipt(poll) {
  if (poll.generation !== state.sessionGeneration || !state.csrf || document.hidden) return;
  if (state.busy || state.unknown) { scheduleReceiptPoll(poll); return; }
  poll.attempt += 1;
  try {
    const response = await api("assist/receipt", { request_id: poll.requestId, receipt_id: poll.card.receipt_id });
    if (poll.generation !== state.sessionGeneration) return;
    const refreshed = Array.isArray(response?.cards)
      ? response.cards.find((item) => item && item.kind === "applied" && item.receipt_id === poll.card.receipt_id) : null;
    if (!refreshed || typeof refreshed.status !== "string") throw new Error("The publication receipt did not match this operation.");
    const card = poll.card;
    card.status = refreshed.status;
    card.publication_status = typeof refreshed.publication_status === "string" ? refreshed.publication_status : null;
    card.source_status = typeof refreshed.source_status === "string" ? refreshed.source_status : null;
    card.saved = refreshed.saved === true;
    card.undo_available = refreshed.undo_available === true;
    if (card.source_status && card.source_status !== "current") {
      card.title = t("appliedUnknown"); card.summary = ""; card.diff = []; card.action = null; card.task = null;
      card.undo_available = false;
    } else {
      for (const field of ["title", "summary", "action"]) if (typeof refreshed[field] === "string") card[field] = refreshed[field];
      if (Array.isArray(refreshed.diff)) card.diff = refreshed.diff;
      if (refreshed.task && typeof refreshed.task === "object") card.task = refreshed.task;
    }
    if (isCardSaved(card)) {
      assist.polls.delete(card.receipt_id);
      const wasSelected = state.selected?.path === poll.key;
      await loadOverview();
      if (poll.generation !== state.sessionGeneration) return;
      if (wasSelected && (!state.selected || state.selected.path === poll.key)) await openTask(poll.key);
      if (poll.generation !== state.sessionGeneration) return;
      showToast(`${card.title || t("applied")} · ${t("saved")}`, card.undo_available ? () => undoAssist(poll.key, card.receipt_id) : null);
    } else if (["submitted", "executing"].includes(card.status)) scheduleReceiptPoll(poll);
    else assist.polls.delete(card.receipt_id);
    renderChats();
  } catch (error) {
    if (poll.generation !== state.sessionGeneration) return;
    poll.card.status = "uncertain"; poll.card.saved = false;
    assist.polls.delete(poll.card.receipt_id);
    showNotice(`${t("appliedUnknown")}. ${assistError(error)}`, "pending");
    renderChats();
  }
}
async function assistMutation(operation, payload, key, onSuccess) {
  if (!assist.available || !state.csrf || state.busy || state.unknown || assist.recorder) return;
  const thread = threadFor(key);
  const generation = state.sessionGeneration;
  thread.pending = true;
  renderChats();
  await mutate(operation, payload, async (response) => {
    await receiveAssist(key, response, operation);
    if (onSuccess && generation === state.sessionGeneration) await onSuccess(response);
  });
  thread.pending = false;
  if (state.unknown) {
    showNotice(t("unknown"), "pending");
  }
  renderChats();
}
async function sendChat(key, raw, voice = false) {
  const message = String(raw || "").trim();
  if (!message || state.busy || state.unknown || !assist.available) return;
  if (assist.recorder || assist.acquiringVoice) { showNotice(t("finishVoice"), "pending"); return; }
  if (key !== "global" && !taskReference(key)) { showNotice(t("unavailable"), "error"); return; }
  const thread = threadFor(key);
  const proposals = thread.messages.flatMap((entry) => entry.cards || []).filter((card) => card.kind === "proposal" && !card.decided);
  if (voice && /^(yes|approve|да|подтверждаю|одобряю)[.!]?$/i.test(message) && proposals.length === 1) {
    await decideAssist(key, proposals[0], "approve"); return;
  }
  thread.messages.push({ role: "user", text: message });
  thread.draft = "";
  await assistMutation("assist/chat", {
    request_id: requestId(), message, thread_id: thread.id, task: taskReference(key),
    language: voice ? "auto" : assist.language, voice,
  }, key);
}
async function sendStep(key, step) {
  if (!taskReference(key) || state.busy || state.unknown) return;
  if (assist.recorder || assist.acquiringVoice) { showNotice(t("finishVoice"), "pending"); return; }
  threadFor(key).messages.push({ role: "user", text: `✦ ${t(`${step}Step`)}` });
  await assistMutation("assist/step", {
    request_id: requestId(), step, task: taskReference(key), input: null, language: assist.language,
  }, key);
}
async function undoAssist(key, receiptId) {
  if (!receiptId) return;
  const generation = state.sessionGeneration;
  await assistMutation("assist/undo", { request_id: requestId(), receipt_id: receiptId }, key, async () => {
    $("toast").hidden = true;
    for (const thread of assist.threads.values()) for (const message of thread.messages) {
      for (const card of message.cards || []) if (card.receipt_id === receiptId) card.undo_available = false;
    }
    const wasSelected = state.selected?.path === key;
    await loadOverview();
    if (generation !== state.sessionGeneration) return;
    if (wasSelected && (!state.selected || state.selected.path === key)) await openTask(key);
  });
}
async function decideAssist(key, card, decision) {
  if (!card.proposal_id || !card.action_digest) { showNotice(t("permission"), "error"); return; }
  await mutate("decide", {
    proposal_id: card.proposal_id, approval_digest: card.action_digest, decision,
  }, async (record) => {
    card.decided = decision; card.decisionStatus = proposalStatusText(record);
    threadFor(key).messages.push({ role: "assistant", text: card.decisionStatus });
    showToast(card.decisionStatus);
    await loadOverview();
    renderChats();
  });
}
function moveTask(task, stage) {
  if (task.stage === stage || state.busy || state.unknown) return;
  if (stage === "done") { openTask(task.path).then(() => { state.tab = "close"; renderDetail(); showNotice(t("closeHint")); }); return; }
  if (assist.available) {
    assistMutation("assist/change", {
      request_id: requestId(), path: task.path, revision: task.revision, change: { stage },
    }, task.path);
    return;
  }
  mutate("refine", { request_id: requestId(), path: task.path, revision: task.revision, changes: { stage } }, (record) => {
    showProposal(record); showNotice(t("stageHint"));
  });
}
function showToast(text, undo) {
  clearTimeout(assist.toastTimer);
  $("toast").replaceChildren(element("span", text));
  if (undo) $("toast").append(writeButton(t("undo"), undo));
  $("toast").hidden = false;
  assist.toastTimer = setTimeout(() => { $("toast").hidden = true; }, undo ? 12000 : 7000);
}
function cancelVoice() {
  assist.voiceGeneration += 1;
  clearTimeout(assist.voiceTimer);
  if (assist.recorder && assist.recorder.state !== "inactive") assist.recorder.stop();
  assist.stream?.getTracks().forEach((track) => track.stop());
  assist.stream = null; assist.recorder = null; assist.voiceKey = null; assist.processingVoice = false; assist.acquiringVoice = false;
}
async function toggleVoice(key) {
  if (assist.recorder) {
    if (assist.voiceKey === key && assist.recorder.state !== "inactive") assist.recorder.stop();
    return;
  }
  if (state.busy || state.unknown || assist.processingVoice || assist.acquiringVoice || !assist.available) return;
  if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") { showNotice(t("voiceUnsupported"), "error"); return; }
  const generation = ++assist.voiceGeneration;
  const session = state.sessionGeneration;
  const sourceAtCapture = taskReference(key);
  assist.acquiringVoice = true;
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    if (generation !== assist.voiceGeneration || session !== state.sessionGeneration) { stream.getTracks().forEach((track) => track.stop()); return; }
    assist.acquiringVoice = false;
    assist.voiceKey = key;
    assist.stream = stream;
    const mime = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/ogg", "audio/mp4", "audio/webm"].find((type) => MediaRecorder.isTypeSupported(type));
    const recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : {});
    assist.recorder = recorder;
    const chunks = []; let size = 0; let failed = false;
    recorder.addEventListener("dataavailable", (event) => {
      if (event.data.size) { chunks.push(event.data); size += event.data.size; }
      if (size > 2 * 1024 * 1024 && recorder.state === "recording") recorder.stop();
    });
    recorder.addEventListener("error", () => { failed = true; showNotice(t("voiceError"), "error"); cancelVoice(); renderChats(); });
    recorder.addEventListener("stop", async () => {
      clearTimeout(assist.voiceTimer);
      stream.getTracks().forEach((track) => track.stop());
      if (generation !== assist.voiceGeneration || session !== state.sessionGeneration || failed) return;
      assist.recorder = null; assist.stream = null; assist.voiceKey = null; assist.processingVoice = key;
      const audio = new Blob(chunks, { type: recorder.mimeType || mime || "audio/webm" });
      if (!size || size > 2 * 1024 * 1024) {
        assist.processingVoice = false; showNotice(t(size ? "voiceTooLarge" : "voiceEmpty"), "error"); renderChats(); return;
      }
      renderChats();
      let transcription = null;
      await mutate("assist/voice", { audio, request_id: requestId() }, async (result) => {
        if (typeof result.text !== "string" || !result.text.trim()) throw new Error(t("voiceEmpty"));
        threadFor(key).draft = result.text;
        transcription = result.text;
      }, async () => {
        assist.processingVoice = false;
        renderChats();
        if (session !== state.sessionGeneration) return;
        const sourceNow = taskReference(key);
        if (sourceAtCapture && (sourceNow?.path !== sourceAtCapture.path || sourceNow?.revision !== sourceAtCapture.revision)) {
          showNotice(t("voiceSourceChanged"), "pending"); return;
        }
        await sendChat(key, transcription, true);
      });
      assist.processingVoice = false;
      renderChats();
    });
    recorder.start(1000);
    assist.voiceTimer = setTimeout(() => { if (recorder.state === "recording") recorder.stop(); }, 60000);
    renderChats();
  } catch (error) {
    if (generation !== assist.voiceGeneration) return;
    cancelVoice();
    showNotice(error.name === "NotAllowedError" ? t("voiceDenied") : t("voiceError"), "error");
    renderChats();
  }
}

function isMockMode() {
  return typeof location !== "undefined" && ["localhost", "127.0.0.1", "[::1]"].includes(location.hostname)
    && new URLSearchParams(location.search).get("mock") === "1";
}
const mockState = { items: null, receipts: new Map(), proposals: new Map() };
function mockItems() {
  if (mockState.items) return mockState.items;
  const samples = [
    ["Design a better morning", "ready", "personal", "Create a 20-minute routine that starts the day with focus.", "Try the routine three mornings and record what worked.", "Choose one small change for tomorrow.", "Start small: one repeatable experiment.", 20],
    ["Ship the reading companion", "doing", "building", "Turn saved sources into a useful reading workspace.", "Keyboard, mobile and evidence checks pass.", "Review the mobile prototype.", "I can prepare the accessibility checklist.", 45],
    ["Разобрать идеи для статьи", "clarify", "learning", "Выбрать одну идею и сформулировать главный тезис.", "Есть тезис, три источника и план статьи.", "Сравнить две идеи.", "Давайте сначала уточним аудиторию.", null],
    ["Prepare the weekend trail", "waiting", "personal", "Choose a short public walking route.", "Route and weather checked before leaving.", "Check the updated forecast.", "Waiting is not the same as falling behind.", 15],
    ["Verify the export workflow", "verify", "building", "Confirm a synthetic export is complete and reproducible.", "A second run produces matching checksums.", "Run the synthetic fixture again.", "Bring evidence before marking this done.", 10],
    ["Learn from the prototype", "ready", "learning", "Capture one reusable lesson from the experiment.", "A short source-linked learning note is reviewed.", "Compare the expected and actual result.", "Keep the lesson concrete, not a transcript.", 25],
    ["Voice-first task capture", "untriaged", "building", "", "", "Define the smallest useful outcome.", "A rough thought is enough to start.", null],
  ];
  mockState.items = samples.map(([title, stage, area, outcome, done_when, next_action, ai_hint, estimate_min], i) => ({
    path: `tasks/synthetic-${i + 1}.md`, revision: "a".repeat(40), title, stage, area, outcome, done_when,
    next_action, ai_hint, estimate_min, project: "synthetic-workspace", execution: "assisted",
    review_on: i < 2 ? "2026-10-13" : "", deadline: "", focus_on: i === 0 ? "2026-10-13" : "",
    waiting_for: stage === "waiting" ? "An updated public forecast" : "", definition: { complete: Boolean(outcome), missing: outcome ? [] : ["outcome", "done_when"] },
    attention_eligible: i < 3, attention_reasons: i === 0 ? ["focus_today"] : i < 3 ? ["review_due"] : [],
    text: `# ${title}\n\nSynthetic local demo. No personal content or live action.`,
  }));
  return mockState.items;
}
async function mockApi(operation, payload) {
  await new Promise((resolve) => setTimeout(resolve, operation.startsWith("assist/") ? 650 : 120));
  const items = mockItems();
  if (operation === "session") return { authenticated: true, csrf: "synthetic-local-only", expires: Date.now() / 1000 + 3600 };
  if (operation.startsWith("overview")) return structuredClone({
    canonical_revision: "a".repeat(40), items, history: [...mockState.proposals.values()], areas: ["personal", "building", "learning"],
    projects: [{ id: "synthetic-workspace", title: "Build a thoughtful workspace" }], active_projects: {},
    next_offset: null, history_next_offset: null, project_next_offset: null, attention_complete: true,
    attention: { calendar_date: "2026-10-13", timezone: "Europe/Riga", deadline_horizon_days: 7, assessed_count: items.length },
    excluded_count: 0, errors: [], limits: {}, budget: {},
  });
  if (operation === "task") {
    const item = items.find((task) => task.path === payload.path);
    if (!item) throw Object.assign(new Error("Synthetic task not found."), { status: 404 });
    return structuredClone(item);
  }
  if (operation.startsWith("dashboard/today")) return {
    items: [], focus: { status: "available", items: [{ status: "current", text: "Build less friction between an idea and a useful outcome.", starts_on: "2026-10-01", ends_on: "2026-10-31" }] },
    visit_token: "synthetic", first_visit: true, observed_at: "2026-10-13T15:00:00+00:00", next_offset: null, canonical_revision: "a".repeat(40),
  };
  if (operation.startsWith("dashboard/inbox")) return {
    items: [{ id: "synthetic-review", revision: "a".repeat(40), kind: "daily_review", as_of: "2026-10-13",
      findings: [{ id: "F1", basis: "inferred", statement: "A smaller next action made the prototype easier to start." }] }],
    next_offset: null, canonical_revision: "a".repeat(40),
  };
  if (operation === "dashboard/read") return {
    id: payload.id, revision: payload.revision, kind: "daily_review", as_of: "2026-10-13", title: "A small experiment",
    path: "wiki/synthetic-review.md", findings: [{ id: "F1", basis: "inferred", statement: "A smaller next action made the prototype easier to start.", next_step: "Try a ten-minute first step.", evidence: [] }],
  };
  if (operation === "assist/usage") return { month_eur: 1.24, month_cap_eur: 10, today_eur: .08, today_cap_eur: .5, calls_today: 4, models: { assist: "small-model", planner: "planner", orchestrator: "orchestrator" } };
  if (operation === "assist/voice") return { text: assist.language === "ru" ? "Составь план для этой задачи" : "Build a plan for this task", language: assist.language, duration_s: 3 };
  if (operation === "assist/chat" || operation === "assist/step") {
    const task = items.find((item) => item.path === payload.task?.path);
    const message = payload.message || payload.step;
    const plan = /plan|план/i.test(message);
    const cards = plan ? [{ kind: "plan", title: task?.title || "Make today lighter", steps: [
      { text: "Clarify the observable outcome", estimate_min: 5, who: "me" },
      { text: "Prepare a small first draft", estimate_min: 15, who: "ai" },
      { text: "Check the result against the done condition", estimate_min: 10, who: "me" },
    ], total_estimate_min: 30 }] : payload.step === "estimate"
      ? [{ kind: "estimate", minutes_low: 20, minutes_high: 40, confidence: "medium", rationale: "One bounded draft and a human evidence check." }]
      : [{ kind: "draft", title: "A useful next move", body: "Start with one observable result. Prepare the first small step, then check it against the done condition." }];
    if (task && /sharpen|concrete|уточн|конкретн|detail/i.test(message)) {
      const before = task.next_action;
      task.next_action = "Draft one concrete option and check it against the desired outcome.";
      const receipt = requestId();
      mockState.receipts.set(receipt, { path: task.path, before });
      task.revision = requestId().padEnd(40, "b");
      cards.splice(0, cards.length, { kind: "applied", status: "completed", publication_status: "merged", source_status: "current", saved: true, action: "refine_task", title: "Next action sharpened", summary: "Synthetic change applied in local demo memory only.",
        diff: [{ field: "next_action", before, after: task.next_action }], receipt_id: receipt, undo_available: true });
    }
    return { thread_id: payload.thread_id || requestId(), reply: assist.language === "ru" ? "Сделаем следующий шаг маленьким и конкретным. Вот план на ~30 минут." : "Let's make the next move small and concrete. Here's a ~30 min plan.",
      language: assist.language, cards, suggestions: [{ label: t("next"), message: t("nextPrompt") }], usage: {} };
  }
  if (operation === "assist/change") {
    const task = items.find((item) => item.path === payload.path);
    if (!task || task.revision !== payload.revision) throw Object.assign(new Error("Synthetic source changed."), { status: 409 });
    const before = task.stage; task.stage = payload.change.stage;
    const receipt = requestId();
    mockState.receipts.set(receipt, { path: task.path, before, field: "stage" });
    task.revision = requestId().padEnd(40, "b");
    return { cards: [
      { kind: "applied", status: "completed", publication_status: "merged", source_status: "current", saved: true, title: "Stage updated", diff: [{ field: "stage", before, after: task.stage }], receipt_id: receipt, undo_available: true },
    ], suggestions: [], usage: {} };
  }
  if (operation === "assist/undo") {
    const receipt = mockState.receipts.get(payload.receipt_id);
    if (!receipt) throw Object.assign(new Error("Synthetic undo receipt is unavailable."), { status: 409 });
    const task = items.find((item) => item.path === receipt.path);
    task[receipt.field || "next_action"] = receipt.before; task.revision = requestId().padEnd(40, "c");
    mockState.receipts.delete(payload.receipt_id);
    return { thread_id: requestId(), reply: "Synthetic change undone. The saved next action has been restored.", cards: [], suggestions: [] };
  }
  if (operation === "refine" || operation === "change") {
    const record = { id: requestId(), kind: "refine_task", status: "pending", approval_digest: "b".repeat(64), source_status: "current", action: { task: payload.path, revision: payload.revision, changes: payload.changes || payload.change } };
    mockState.proposals.set(record.id, record); return structuredClone(record);
  }
  if (operation === "decide") {
    const record = mockState.proposals.get(payload.proposal_id);
    if (!record) throw Object.assign(new Error("Synthetic proposal not found."), { status: 404 });
    record.status = payload.decision === "approve" ? "completed" : "declined";
    if (record.status === "completed") {
      const task = items.find((item) => item.path === record.action.task);
      Object.assign(task, record.action.changes);
      task.revision = requestId().padEnd(40, "d");
    }
    return structuredClone(record);
  }
  throw Object.assign(new Error("This operation is not part of the synthetic demo. No live API was called."), { status: 422 });
}
start();
