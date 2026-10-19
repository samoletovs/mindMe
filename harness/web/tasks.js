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
  source_changed: "closureSourceChanged", task_closure_source_changed: "closureSourceChanged",
  source_closed: "closureSourceClosed", task_linked_path: "closureLinkedPath",
  task_source_invalid: "closureSourceInvalid", task_source_not_allowed: "closurePrivacy",
  task_links_require_repair: "closureLinkRepair", task_publication_proof_missing: "closureProof",
  task_context_limit: "closureContext", task_closure_inventory_changed: "closureInventoryChanged",
  task_closure_inventory_invalid: "closureInventoryInvalid", task_closure_inventory_limit: "closureInventoryLimit",
  task_closure_continuation_invalid: "closureContinuation", task_closure_scan_exhausted: "closureExhausted",
  destination_exists: "closureDestination", action_id_reused: "closureRequestConflict",
  task_closure_scan_unconfirmed: "closureUnconfirmed", remote_outcome_unknown: "closureUnconfirmed",
  task_closure_checkpoint_unavailable: "closureCheckpoint", receipt_unavailable: "closureReceipt",
  task_closure_conflict: "closureConflict",
};
const SOURCE_ISSUES = {
  task_metadata_missing: "sourceMetadataMissing", task_metadata_invalid: "sourceMetadataInvalid",
  task_definition_invalid: "sourceDefinitionInvalid", task_date_invalid: "sourceDateInvalid",
  task_title_missing: "sourceTitleMissing", task_review_date_ambiguous: "sourceReviewAmbiguous",
  task_source_too_large: "sourceTooLarge", task_source_policy_unresolved: "sourcePolicyUnresolved",
  task_source_unavailable: "sourceUnavailable",
};
const $ = (id) => document.getElementById(id);
const state = {
  csrf: "", view: "today", layout: "board", items: [], overview: null, selected: null,
  tab: "overview", busy: false, unknown: null, proposal: null, clarification: null,
  loading: false, detailGeneration: 0, detailIntent: 0, sessionGeneration: 0, authExpires: 0, expiryTimer: null,
  captureSource: null, captureReceipt: null, capturePoll: null,
  snapshotCheckedAt: 0, manualDraft: null, manualDraftInvalidated: false,
};
const MANUAL_FORMS = ["definition-form", "date-form", "prepare-form", "completion-form", "clarification-form"];
const focusOrigins = { task: null, capture: null, proposal: null, copilot: null };
const dashboard = new Dashboard({
  state, element, append, button, writeButton, empty, infoTip, api, mutate, showNotice, setBusy,
  renderTaskRow, taskTiming, statusBadge, setView, beginSourceCapture, restoreFocus,
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
  return element("span", label || (assist.language === "ru" && COPY.ru[key]) || LABELS[key] || key.replaceAll("_", " "), `status ${STAGES.includes(key) || Object.hasOwn(LABELS, key) ? key : ""}`);
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
// Assist routes require the canonical hyphenated UUID; legacy task routes keep 32-hex IDs.
function assistRequestId() {
  return crypto.randomUUID().toLowerCase();
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
  const dialogNotice = $("copilot-notice");
  dialogNotice.textContent = $("copilot-dialog").open ? message : "";
  dialogNotice.className = `notice ${kind}`;
  dialogNotice.hidden = !dialogNotice.textContent;
}
function restoreFocus(preferred, fallback) {
  const target = preferred?.isConnected && preferred.getClientRects().length ? preferred : fallback;
  if (target?.isConnected && target.getClientRects().length) target.focus({ preventScroll: true });
}
function setConnection(key) {
  $("connection").dataset.i18n = key;
  $("connection").textContent = t(key);
}
function setAccessMessage(key, message = "") {
  $("access-message").dataset.statusKey = key;
  $("access-message").textContent = key ? t(key) : message;
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
  setConnection(value ? "workingRequest" : state.csrf ? (isMockMode() ? "mock" : "personalWorkspace") : "signedOut");
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
  showNotice(t(attempt.operation === "capture-save" ? "captureSaving" : "workingExactRequest"));
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
      setAccessMessage("sessionExpired");
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
    for (const [value, text, localized] of options.options) {
      const option = element("option", localized ? t(text) : text);
      if (localized) option.dataset.i18n = text;
      option.value = value;
      control.append(option);
    }
  }
  control.value = options.value ?? options.options?.[0]?.[0] ?? "";
  const labelNode = element("label", t(label));
  labelNode.dataset.i18n = label;
  labelNode.htmlFor = id;
  append(wrapper, labelNode, control);
  if (options.hint) {
    const hint = element("p", t(options.hint), "hint");
    hint.dataset.i18n = options.hint;
    wrapper.append(hint);
  }
  parent.append(wrapper);
  return control;
}
let infoTipCount = 0;
function infoTip(text) {
  const node = element("button", null, "info-tip");
  node.type = "button";
  node.setAttribute("aria-label", t("moreInformation"));
  const tip = element("span", text, "tip-text");
  tip.id = `info-tip-${++infoTipCount}`;
  tip.setAttribute("role", "tooltip");
  node.setAttribute("aria-describedby", tip.id);
  const mark = element("span", "i", "info-mark");
  mark.setAttribute("aria-hidden", "true");
  const position = () => {
    const rect = node.getBoundingClientRect();
    const width = Math.min(280, innerWidth - 32);
    tip.style.width = `${width}px`;
    tip.style.left = `${Math.max(16, Math.min(innerWidth - width - 16, rect.left))}px`;
    const height = tip.getBoundingClientRect().height;
    tip.style.top = `${rect.bottom + height + 12 < innerHeight ? rect.bottom + 8 : Math.max(12, rect.top - height - 8)}px`;
  };
  node.addEventListener("focus", position);
  node.addEventListener("pointerenter", position);
  node.addEventListener("click", () => {
    if (node.dataset.open) {
      delete node.dataset.open; node.dataset.dismissed = "true";
    } else {
      delete node.dataset.dismissed; node.dataset.open = "true"; position();
    }
  });
  node.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    event.preventDefault(); event.stopPropagation();
    delete node.dataset.open; node.dataset.dismissed = "true";
  });
  for (const name of ["blur", "pointerleave"]) node.addEventListener(name, () => {
    delete node.dataset.open; delete node.dataset.dismissed;
  });
  return append(node, mark, tip);
}
function empty(parent, title, message) {
  const node = element("div", null, "empty");
  append(node, element("strong", title), element("p", message));
  parent.append(node);
}
function areaOptions() {
  return [["", "chooseArea", true], ...(state.overview?.areas || []).map((area) => [area, prettyId(area)])];
}
function projectOptions() {
  return [["", "noProjectSelected", true], ...(state.overview?.projects || []).map((project) => [project.id, prettyId(project.id)])];
}
function executionOptions() {
  return [["", "notDecided", true], ["human", "executionHuman", true],
    ["assisted", "executionAssisted", true], ["agent", "executionAgent", true]];
}
function isAttention(task) {
  return task.attention_eligible === true;
}
function pendingProposals() {
  return (state.overview?.history || []).filter((record) => record.status === "pending" && record.action && record.approval_digest);
}
function setView(view, { load = true, arrival = false } = {}) {
  if (!["today", "needs", "tasks", "knowledge", "areas", "activity"].includes(view)) view = "today";
  if (["tasks", "needs"].includes(state.view) && !["tasks", "needs"].includes(view)) {
    state.detailIntent += 1;
    if (state.selected && !state.selected.revision) closeDetail({ reason: "navigation", restore: false });
  }
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
  render(!load);
  if (load && state.csrf && view === "today" && !dashboard.today) dashboard.loadToday();
  if (load && state.csrf && view === "knowledge" && !dashboard.inbox) dashboard.loadInbox();
  if (arrival) {
    const target = ["tasks", "needs"].includes(view) && state.selected?.revision ? $("inspector")
      : view === "knowledge" && dashboard.reader ? $("knowledge-reader") : $("view-title");
    restoreFocus(target);
    const position = matchMedia("(max-width: 930px)").matches ? document.querySelector(".navigation") : target;
    position.scrollIntoView({ block: "start", behavior: "instant" });
  }
}
function taskTiming(task) {
  const timing = element("div", null, "row-timing");
  const day = attentionDate();
  const reasons = new Set(task.attention_reasons || []);
  if (task.focus_on) timing.append(element("span", `${t(day && task.focus_on === day ? "selectedToday" : "focusOn")} ${task.focus_on}`, day && task.focus_on === day ? "status ready" : ""));
  if (task.deadline) timing.append(element("span",
    `${t(reasons.has("deadline_due") ? "deadlineDue" : reasons.has("deadline_soon") ? "deadlineApproaching" : "deadlineLabel")} ${task.deadline}`,
    day && task.deadline <= day ? "deadline" : reasons.has("deadline_soon") ? "review-due" : ""));
  if (task.review_on) timing.append(element("span", `${t(reasons.has("review_due") ? "reviewDue" : "reviewLabel")} ${task.review_on}`, day && task.review_on <= day ? "review-due" : ""));
  const datedReasons = { focus_today: task.focus_on, deadline_due: task.deadline, deadline_soon: task.deadline, review_due: task.review_on };
  const reasonLabels = { focus_today: "selectedToday", deadline_due: "deadlineDue", deadline_soon: "deadlineApproaching", review_due: "reviewDue" };
  for (const reason of reasons) {
    if (!datedReasons[reason]) timing.append(element("span", reasonLabels[reason] ? t(reasonLabels[reason]) : reason));
  }
  if (task.waiting_for) timing.append(element("span", `${t("waitingDependency")}: ${task.waiting_for}`));
  if (task.definition?.missing?.length) timing.append(element("span", t("definitionNeedsWork")));
  return timing;
}
function renderTaskRow(task, action = () => openTask(task.path)) {
  const row = button("", action, "task-row");
  row.dataset.path = task.path;
  row.setAttribute("aria-pressed", String(state.selected?.path === task.path));
  const top = element("div", null, "row-top");
  append(top, statusBadge(task.stage), element("span", task.area ? prettyId(task.area) : t("areaUnclear")));
  append(row, top, element("h3", task.title), element("p", task.next_action || t("clarifyNext"), "row-next"));
  const timing = taskTiming(task);
  row.append(timing);
  if (task.estimate_min != null) timing.append(element("span", `${task.estimate_min} ${t("minutes")}`, "status"));
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
    list.setAttribute("aria-label", t("loadingTasks"));
    list.setAttribute("aria-busy", "true");
    for (let i = 0; i < 4; i += 1) list.append(element("div", null, "skeleton"));
    return;
  }
  list.removeAttribute("aria-busy");
  list.removeAttribute("aria-label");
  const tasks = filteredTasks();
  if (!state.overview) {
    empty(list, t("sourcesUnavailable"), t("sourcesUnavailableDetail"));
  } else if (!tasks.length) {
    const incompleteAttention = state.view === "needs" && state.overview.attention_complete === false;
    empty(list, incompleteAttention ? t("attentionIncomplete") :
      (state.view === "needs" ? t("noMatchingAttention") : t("noMatchingTasks")),
    incompleteAttention ? t("attentionIncompleteDetail") : t("noMatchingDetail"));
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
      if (!laneTasks.length) lane.append(element("p", t("noLoadedTasks"), "empty"));
      list.append(lane);
    }
  } else {
    list.removeAttribute("tabindex");
    tasks.forEach((task) => list.append(renderTaskRow(task)));
  }
  const candidateCount = state.overview?.candidate_count;
  if (state.overview) {
    $("page-status").replaceChildren(`${t("loadedTasks")}: ${state.items.length}${Number.isInteger(candidateCount) ? ` / ${candidateCount}` : ""} `,
      infoTip(t("loadedOnly")));
  } else $("page-status").textContent = t("availabilityUnconfirmed");
  $("load-more").hidden = state.overview?.next_offset === null || state.overview?.next_offset === undefined;
  $("load-more").disabled = state.loading;
  for (const row of list.querySelectorAll?.(".task-row") || []) {
    const before = positions.get(row.dataset.path);
    if (!before || !row.animate || matchMedia("(prefers-reduced-motion: reduce)").matches) continue;
    const after = row.getBoundingClientRect();
    const dx = before.left - after.left, dy = before.top - after.top;
    if (before.width && after.width && Math.abs(dx) + Math.abs(dy) > 2) row.animate([
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
  if (pending || unreviewed) target.append(button("Review decisions and results", () => setView("activity", { arrival: true })));
}
function render(preserveForms = false) {
  $("layout-list").setAttribute("aria-pressed", String(state.layout === "list"));
  $("layout-board").setAttribute("aria-pressed", String(state.layout === "board"));
  $("needs-count").textContent = String(state.items.filter(isAttention).length + pendingProposals().length);
  $("tasks-count").textContent = String(state.items.length);
  $("activity-count").textContent = String(pendingProposals().length);
  renderAttention();
  renderTasks();
  if (state.view === "areas") renderAreas(preserveForms);
  if (state.view === "activity") renderActivity();
  if (state.view === "today") dashboard.renderToday();
  if (state.view === "knowledge") {
    $("snapshot").textContent = dashboard.inbox
      ? `${t("reviewSnapshot")} ${dashboard.inbox.canonical_revision.slice(0, 7)}`
      : t("reviewSnapshotSeparate");
  } else if (state.overview) {
    const { calendar_date: day, timezone } = state.overview.attention;
    const checked = state.snapshotCheckedAt ? new Date(state.snapshotCheckedAt).toLocaleTimeString(assist.language, { hour: "2-digit", minute: "2-digit" }) : "";
    $("snapshot").textContent = `${t("taskDay")} ${day} / ${timezone}${checked ? `. ${t("snapshotChecked")} ${checked}` : ""}`;
  } else {
    $("snapshot").textContent = "";
  }
}
function sourceIssueMessage(code) {
  return t(Object.hasOwn(SOURCE_ISSUES, code) ? SOURCE_ISSUES[code] : "sourceIssueUnknown");
}
function renderSourceWarnings() {
  const data = state.overview;
  const issues = $("source-issues");
  const scope = $("source-scope");
  issues.replaceChildren();
  issues.hidden = true;
  for (const [id, count, key] of [
    ["task-source-count", data?.excluded_count, "sourceTaskExclusions"],
    ["project-source-count", data?.project_excluded_count, "sourceProjectExclusions"],
  ]) {
    $(id).hidden = !(count > 0);
    $(id).textContent = count > 0 ? t(key, { count }) : "";
  }
  scope.hidden = $("task-source-count").hidden && $("project-source-count").hidden;
  if (!data) {
    scope.open = false;
    return;
  }
  if (data.attention_complete === false) {
    const assessed = data.attention?.assessed_count;
    issues.append(element("p", Number.isInteger(assessed)
      ? t("sourceAttentionAssessedPartial", { count: assessed }) : t("sourceAttentionPartial")));
  }
  if (data.errors?.length) {
    issues.append(element("p", t("sourceTaskIssues", { count: data.errors.length })));
    for (const error of data.errors) issues.append(element("p", sourceIssueMessage(error.code)));
  }
  if (data.project_errors?.length) {
    issues.append(element("p", t("sourceProjectIssues", { count: data.project_errors.length })));
    for (const error of data.project_errors) issues.append(element("p", sourceIssueMessage(error.code)));
  }
  issues.hidden = !issues.children.length;
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
async function loadOverview(appendPage = false, refreshContext = null) {
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
    )) {
      const permitted = refreshContext && refreshContext.intent === state.detailIntent
        && refreshContext.path === state.selected.path && data.items.some((task) => task.path === refreshContext.path);
      if (permitted) refreshContext.ui = captureDetailContext();
      closeDetail({ reason: "source", restore: false });
      if (permitted) {
        // Drop stale source content immediately; only the explicit selection and UI context survive.
        state.selected = { path: refreshContext.path };
        $("inspector").replaceChildren(button(t("back"), () => closeDetail(), "mobile-back quiet"),
          element("p", t("loadingCanonical"), "empty"));
        renderTasks();
        if (refreshContext.ui.focused) {
          restoreFocus($("inspector"));
          refreshContext.ui.parked = true;
        }
      }
    }
    if (state.captureReceipt) {
      const previous = state.captureReceipt;
      state.captureReceipt = data.history.find((record) => record.id === previous.id) || {
        id: previous.id, kind: "capture_task", status: previous.status,
        approved_by: "owner-capture", source_status: "unavailable",
      };
      if (captureSaved(state.captureReceipt)) state.captureReceipt = null;
    } else {
      state.captureReceipt = data.history.find((record) =>
        record.kind === "capture_task" && record.approved_by === "owner-capture"
        && ["submitted", "executing", "uncertain"].includes(record.status)
      ) || null;
    }
    if (state.proposal) {
      state.proposal = data.history.find((record) => record.id === state.proposal.id)
        || (state.proposal.id === state.captureReceipt?.id ? state.captureReceipt : null);
      $("proposal-preview").replaceChildren(...(state.proposal ? [renderProposal(state.proposal)] : []));
      $("proposal-section").hidden = !state.proposal;
    }
    renderActivity();
    state.snapshotCheckedAt = Date.now();
    const area = $("area-filter").value;
    $("area-filter").replaceChildren();
    for (const [value, text] of [["", t("allAreas")], ...data.areas.map((id) => [id, prettyId(id)])]) {
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
    renderAreas(true);
    const select = $("capture-details").querySelector("[name='project']");
    if (select) {
      const previous = select.value;
      select.replaceChildren();
      for (const [value, text, localized] of projectOptions()) {
        const option = element("option", localized ? t(text) : text);
        if (localized) option.dataset.i18n = text;
        option.value = value; select.append(option);
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
  field(target, "area", "area", { options: areaOptions() });
  field(target, "project", "project", { options: projectOptions() });
  field(target, "execution", "whoActs", { options: executionOptions() });
  field(target, "outcome", "desiredOutcome", { multiline: true, max: 2000, wide: true });
  field(target, "next_action", "nextAction", { multiline: true, max: 500, wide: true });
  field(target, "done_when", "doneWhenField", { multiline: true, max: 2000, wide: true, hint: "doneWhenHint" });
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
async function openTask(path, focus = true) {
  if (!focus && state.selected?.path !== path) return;
  if (focus) { focusOrigins.task = document.activeElement; state.detailIntent += 1; }
  const generation = ++state.detailGeneration;
  const intent = state.detailIntent;
  if (focus) {
    clearManualDraft();
    state.selected = { path };
    state.clarification = null;
    $("inspector").replaceChildren(button(t("back"), () => closeDetail(), "mobile-back quiet"),
      element("p", t("loadingCanonical"), "empty"));
  }
  $("inspector").setAttribute("aria-busy", "true");
  renderTasks();
  if (focus && ["tasks", "needs"].includes(state.view)) restoreFocus($("inspector"));
  try {
    const task = await api("task", { path });
    if (generation !== state.detailGeneration || intent !== state.detailIntent || state.selected?.path !== path) return;
    if (task.path !== path || !task.revision || typeof task.title !== "string") throw new Error("The canonical task response is incomplete.");
    state.selected = task;
    if (focus) state.tab = assist.available ? "chat" : "overview";
    renderDetail();
    renderTasks();
    if (focus && ["tasks", "needs"].includes(state.view)) {
      $("inspector").scrollTop = 0;
      restoreFocus($("inspector"));
      if (matchMedia("(max-width: 1100px)").matches) $("inspector").scrollIntoView({ block: "start" });
    }
  } catch (error) {
    if (generation !== state.detailGeneration) return;
    if (focus) {
      $("inspector").replaceChildren(button(t("back"), () => closeDetail()), element("p", errorMessage(error), "notice error"));
      if (["tasks", "needs"].includes(state.view)) restoreFocus($("inspector"));
    } else {
      if (!state.selected?.revision || [403, 404, 409].includes(error.status)) closeDetail({ reason: "source", restore: false });
      showNotice(`${errorMessage(error)} ${t("refreshFailed")}`, "error");
    }
  } finally {
    if (generation === state.detailGeneration) $("inspector").removeAttribute("aria-busy");
  }
}
function closeDetail({ reason = "user", restore = true } = {}) {
  const path = state.selected?.path;
  if (reason === "user") state.detailIntent += 1;
  if (path && assist.voiceKey === path) { cancelVoice(); renderChats(); }
  state.detailGeneration += 1;
  clearManualDraft(reason === "source");
  state.selected = null;
  state.clarification = null;
  $("inspector").replaceChildren(append(element("div", null, "inspector-empty"),
    element("h2", t("detailEmptyTitle")),
    element("p", t("detailEmptyDetail"))));
  $("inspector").removeAttribute("aria-busy");
  delete $("inspector").dataset.path;
  delete $("inspector").dataset.revision;
  renderTasks();
  if (restore && path && ["tasks", "needs"].includes(state.view)) {
    const row = Array.from($("task-list").querySelectorAll(".task-row")).find((node) => node.dataset.path === path);
    restoreFocus(focusOrigins.task, row || $("view-title"));
    row?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }
  focusOrigins.task = null;
}
function captureDetailContext() {
  const target = $("inspector");
  const active = target.contains?.(document.activeElement) ? document.activeElement : null;
  const chat = active?.closest?.(".chat-form");
  const form = active?.closest?.("form");
  const manual = MANUAL_FORMS.includes(form?.id);
  const messages = target.querySelector(".chat-messages");
  return {
    path: target.dataset.path, revision: target.dataset.revision, manual,
    tab: state.tab, focused: Boolean(active), scroll: target.scrollTop,
    control: manual ? `#${form.id} ${active.name ? `[name="${active.name}"]` : "button[type=submit]"}`
      : chat ? (active.tagName === "TEXTAREA" ? ".chat-form textarea" : active.classList.contains("mic-button") ? ".chat-form .mic-button" : active.type === "submit" ? ".chat-form button[type=submit]" : null)
        : active?.dataset.detailTab ? `[data-detail-tab="${active.dataset.detailTab}"]` : null,
    start: active?.selectionStart, end: active?.selectionEnd, direction: active?.selectionDirection,
    pageX: window.scrollX, pageY: window.scrollY,
    messageScroll: messages?.scrollTop,
    followMessages: messages ? messages.scrollHeight - messages.scrollTop - messages.clientHeight < 48 : true,
  };
}
function restoreDetailContext(context, focus = true) {
  const target = $("inspector");
  const sameSource = context.path === state.selected?.path && context.revision === state.selected?.revision;
  target.scrollTop = context.scroll;
  const messages = target.querySelector(".chat-messages");
  if (messages && context.messageScroll != null) messages.scrollTop = context.followMessages ? messages.scrollHeight : context.messageScroll;
  const control = context.control && (!context.manual || sameSource) && target.querySelector(context.control);
  if (focus && control && context.focused) {
    restoreFocus(control);
    if (typeof context.start === "number") control.setSelectionRange(context.start, context.end, context.direction);
  }
  if (sameSource) window.scrollTo?.({ left: context.pageX, top: context.pageY, behavior: "instant" });
}
function clearManualDraft(invalidated = false) {
  if (invalidated && state.manualDraft) {
    state.manualDraftInvalidated = true;
    showNotice(t("manualDraftInvalidated"), "pending");
  } else if (!invalidated) state.manualDraftInvalidated = false;
  state.manualDraft = null;
}
function bindManualForms(task) {
  if (state.manualDraft && (state.manualDraft.path !== task.path || state.manualDraft.revision !== task.revision)) clearManualDraft(true);
  for (const id of MANUAL_FORMS) {
    const form = $(id);
    if (!form || !$("inspector").contains(form)) continue;
    const binding = form.dataset.binding || "";
    const saved = state.manualDraft?.forms[id];
    if (saved && saved.binding !== binding) delete state.manualDraft.forms[id];
    else if (saved) for (const control of form.elements) {
      if (!control.name || !Object.hasOwn(saved.values, control.name)) continue;
      if (control.type === "checkbox") control.checked = saved.values[control.name];
      else control.value = saved.values[control.name];
    }
    const remember = () => {
      if (!state.csrf || state.selected?.path !== task.path || state.selected?.revision !== task.revision) return;
      state.manualDraft ||= { path: task.path, revision: task.revision, forms: {} };
      state.manualDraft.forms[id] = { binding, values: Object.fromEntries(
        Array.from(form.elements).filter((control) => control.name)
          .map((control) => [control.name, control.type === "checkbox" ? control.checked : control.value])
      ) };
    };
    form.addEventListener("input", remember);
    form.addEventListener("change", remember);
  }
  const dates = $("date-form");
  if (dates) dates.elements.value.disabled = dates.elements.remove_date.checked;
  const preparation = $("prepare-form");
  if (preparation) $("preparation-hint").textContent = t(preparation.elements.kind.value === "research" ? "publicResearchLimits" : "privatePreparationLimits");
}
async function refreshTaskProjection(key) {
  const session = state.sessionGeneration;
  const context = state.selected?.path === key ? { path: key, intent: state.detailIntent, ui: null } : null;
  await loadOverview(false, context);
  if (session !== state.sessionGeneration || !context || context.intent !== state.detailIntent || state.selected?.path !== key) return;
  const restore = context.ui?.parked && document.activeElement === $("inspector");
  await openTask(key, false);
  if (session !== state.sessionGeneration || context.intent !== state.detailIntent || state.selected?.path !== key || !state.selected.revision) return;
  if (context.ui) restoreDetailContext(context.ui, restore && document.activeElement === $("inspector"));
}
function detailBase() {
  const task = state.selected;
  const target = $("inspector");
  target.replaceChildren();
  target.dataset.path = task.path;
  target.dataset.revision = task.revision;
  target.append(button(t("back"), closeDetail, "mobile-back quiet"));
  if (state.manualDraftInvalidated) target.append(element("p", t("manualDraftInvalidated"), "notice pending"));
  const header = element("div", null, "detail-header");
  append(header, statusBadge(task.stage), element("h2", task.title, "detail-title"),
    element("p", `${task.area ? prettyId(task.area) : t("areaUnclear")}${task.project ? ` / ${prettyId(task.project)}` : ""}`, "hint"));
  const links = element("div", null, "meta-line");
  append(links, safeLink(t("openCanonical"), task.url), element("span", `${t("revision")} ${task.revision.slice(0, 7)}`));
  header.append(links);
  target.append(header);
  const outcome = element("section", null, "outcome-block");
  append(outcome, element("span", t("outcome"), "eyebrow"), element("p", task.outcome || t("notDefined")),
    element("span", t("doneWhen"), "eyebrow"), element("p", task.done_when || t("notDefined")));
  target.append(outcome);
  if (assist.available) target.append(buildStepRail(task));
  const tabs = element("nav", null, "detail-tabs");
  tabs.setAttribute("aria-label", t("taskActions"));
  for (const [id, title] of [
    ...(assist.available ? [["chat", t("chatTab")]] : []),
    ["overview", t("overviewTab")], ["edit", t("editTab")], ["ai", t("aiTab")], ["close", t("closeTab")],
  ]) {
    const node = button(title, () => {
      state.tab = id; renderDetail();
      restoreFocus($("inspector").querySelector(`[data-detail-tab="${id}"]`));
    });
    node.dataset.detailTab = id;
    node.setAttribute("aria-pressed", String(state.tab === id));
    tabs.append(node);
  }
  target.append(tabs);
  return append(target, element("div", null, "detail-panel")).lastElementChild;
}
function renderDetail() {
  if (!state.selected?.revision) return;
  if (state.manualDraft && (state.manualDraft.path !== state.selected.path || state.manualDraft.revision !== state.selected.revision)) clearManualDraft(true);
  const context = captureDetailContext();
  const panel = detailBase();
  const task = state.selected;
  if (state.tab === "chat") {
    panel.append(append(element("dl", null, "detail-section"), element("dt", t("nextAction")), element("dd", task.next_action || t("notDefined"))));
  } else if (state.tab === "overview") {
    const description = element("dl", null, "detail-section");
    for (const [label, value] of [[t("nextAction"), task.next_action]]) {
      append(description, element("dt", label), element("dd", value || t("notRecorded")));
    }
    panel.append(description);
    const metadata = element("details", null, "task-metadata");
    metadata.append(element("summary", t("taskDetails")));
    const dates = element("dl", null, "detail-section");
    for (const [label, value] of [["executionMode", task.execution ? t(`execution_${task.execution}`) : ""], ["reviewOn", task.review_on], ["deadlineLabel", task.deadline], ["focusOn", task.focus_on], ["waitingFor", task.waiting_for]]) {
      append(dates, element("dt", t(label)), element("dd", value || t("notRecorded")));
    }
    metadata.append(dates);
    if (task.definition?.missing?.length) {
      metadata.append(element("p", t("needsClarification", { fields: task.definition.missing.join(", ") }), "hint"));
    }
    metadata.append(writeButton(t("clarifyTask"), () => mutate("clarify", { path: task.path, revision: task.revision }, showClarification)));
    panel.append(metadata);
    if (task.text) {
      const details = element("details", null, "section-block");
      append(details, element("summary", t("readFullSource")), element("pre", task.text));
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
  bindManualForms(task);
  setBusy(state.busy);
  if (assist.available) {
    const chat = element("section", null, "task-copilot section-block");
    chat.append(element("h3", t("taskCopilot")));
    const host = element("div"); host.dataset.chatKey = task.path;
    chat.append(host); panel.append(chat);
    renderChat(host, task.path);
  }
  restoreDetailContext(context);
}
function renderEdit(panel, task) {
  const form = element("form"); form.id = "definition-form";
  append(panel, element("h3", t("clarifyRecord")), element("p", t("definitionHint"), "hint"));
  const fields = element("div", null, "form-grid"); fields.id = "edit-fields";
  field(fields, "title", "titleField", { value: task.title, max: 120, wide: true, required: true });
  field(fields, "area", "area", { value: task.area, options: areaOptions() });
  const projects = projectOptions();
  if (task.project && !projects.some(([id]) => id === task.project)) projects.push([task.project, prettyId(task.project)]);
  field(fields, "project", "project", { value: task.project, options: projects });
  field(fields, "stage", "stage", { value: task.stage === "untriaged" ? "" : task.stage, options: [["", "keepUntriaged", true], ...STAGES.filter((id) => id !== "untriaged").map((id) => [id, id, true])] });
  field(fields, "execution", "executionMode", { value: task.execution, options: executionOptions() });
  for (const [name, label, max] of [["outcome", "outcomeField", 2000], ["next_action", "nextAction", 500], ["done_when", "doneWhenField", 2000]]) {
    field(fields, name, label, { value: task[name], max, multiline: true, wide: true });
  }
  field(fields, "waiting_for", "waitingFor", { value: task.waiting_for, max: 300, wide: true, hint: "waitingHint" });
  field(fields, "review_on", "reviewOn", { value: task.review_on, type: "date", hint: "reviewOnHint" });
  form.append(fields);
  const submit = element("button", t("reviewDefinition"), "primary"); submit.type = "submit"; form.append(submit);
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
        showNotice(t("fieldCannotClear", { field: form.elements.namedItem(name)?.labels[0]?.textContent || name }), "error");
        return;
      }
    }
    if (!Object.keys(changes).length) { showNotice(t("noDefinitionChanges")); return; }
    if ((Object.hasOwn(changes, "waiting_for") || Object.hasOwn(changes, "review_on")) && !changes.stage) {
      const stage = String(new FormData(form).get("stage") || "");
      if (!stage) {
        showNotice(t("chooseWaitingStage"), "error");
        return;
      }
      changes.stage = stage;
    }
    mutate("refine", { request_id: requestId(), path: task.path, revision: task.revision, changes }, showProposal);
  });
  panel.append(form);
  const dates = element("form", null, "section-block"); dates.id = "date-form";
  dates.append(element("h3", t("changeOneDate")));
  const kind = field(dates, "field", "dateType", { options: [["review_on", "nextReview", true], ["focus_on", "selectedFocusDate", true], ["deadline", "realDeadline", true]] });
  const date = field(dates, "value", "dateField", { type: "date", value: task.review_on });
  const clearLabel = element("label", null, "checkbox");
  const clear = element("input"); clear.type = "checkbox"; clear.name = "remove_date";
  append(clearLabel, clear, element("span", t("removeDate")));
  dates.append(clearLabel);
  kind.addEventListener("change", () => { date.value = task[kind.value] || ""; clear.checked = false; date.disabled = false; });
  clear.addEventListener("change", () => { date.disabled = clear.checked; });
  dates.append(element("p", t("dateHint"), "hint"));
  const dateSubmit = element("button", t("reviewDateChange")); dateSubmit.type = "submit"; dates.append(dateSubmit);
  dates.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!clear.checked && !date.value) { showNotice(t("chooseDate"), "error"); return; }
    mutate("change", { request_id: requestId(), path: task.path, revision: task.revision, change: { [kind.value]: clear.checked ? null : date.value } }, showProposal);
  });
  panel.append(dates);
}
function renderPreparation(panel, task) {
  append(panel, element("h3", t("preparationTitle")), element("p", t("preparationIntro")));
  if (!task.definition?.complete || !["ready", "doing", "verify"].includes(task.stage)) {
    panel.append(element("p", t("preparationEligibility"), "notice pending"));
    return;
  }
  const form = element("form"); form.id = "prepare-form";
  const kind = field(form, "kind", "workToPrepare", { options: [["prepare_task", "privateDraft", true], ["research", "publicResearch", true]] });
  const scope = field(form, "scope", "exactScope", { multiline: true, max: 500, required: true });
  const limits = element("p", t("privatePreparationLimits"), "hint"); limits.id = "preparation-hint";
  form.append(limits);
  kind.addEventListener("change", () => {
    limits.textContent = t(kind.value === "research" ? "publicResearchLimits" : "privatePreparationLimits");
  });
  const submit = element("button", t("reviewAssignment"), "primary"); submit.type = "submit"; form.append(submit);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    mutate("prepare", { request_id: requestId(), path: task.path, revision: task.revision, kind: kind.value, scope: scope.value.trim() }, showProposal);
  });
  panel.append(form);
}
function renderClose(panel, task) {
  append(panel, element("h3", t("closureTitle")), element("p", t("closureIntro")));
  if (!task.done_when) { panel.append(element("p", t("closureNeedsCondition"), "notice pending")); return; }
  panel.append(element("blockquote", task.done_when));
  const form = element("form"); form.id = "completion-form";
  const result = field(form, "result", "resultAchieved", { multiline: true, max: 1000, required: true });
  const evidence = field(form, "evidence", "evidenceField", { multiline: true, max: 1000, required: true, hint: "evidenceHint" });
  const verifiedOn = field(form, "verified_on", "verifiedOn", { type: "date", value: attentionDate(), required: true, hint: "verifiedOnHint" });
  const learning = field(form, "learning", "learningField", { multiline: true, max: 1000, hint: "learningHint" });
  const attestation = element("label", null, "checkbox");
  const check = element("input"); check.type = "checkbox"; check.required = true; check.name = "checked_done_when";
  append(attestation, check, element("span", t("ownerAttestation")));
  form.append(attestation);
  const submit = element("button", t("reviewClosure"), "primary"); submit.type = "submit"; form.append(submit);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const completion = { result: result.value.trim(), evidence: evidence.value.trim(), verified_on: verifiedOn.value, verification: "owner" };
    if (learning.value.trim()) completion.learning = learning.value.trim();
    mutate("close", { request_id: requestId(), path: task.path, revision: task.revision, checked_done_when: check.checked, completion }, showProposal);
  });
  panel.append(form);
}
function showClarification(record) {
  state.clarification = record;
  state.tab = "overview";
  renderDetail();
  showNotice(t("clarificationRecorded"));
}
function renderClarification(parent) {
  const record = state.clarification;
  const target = element("section", null, "clarification");
  append(target, element("h3", t("oneQuestion")),
    element("p", t("answersUsed", { count: record.turns || 0 }), "hint"));
  if (record.question && (record.turns || 0) < 3) {
    target.append(element("p", record.question));
    if (!/^[a-f0-9]{32}$/.test(record.question_token || "")) {
      target.append(element("p", t("answerBindingMissing"), "notice error"));
      parent.append(target);
      return;
    }
    const source = { path: state.selected.path, revision: state.selected.revision };
    const form = element("form"); form.id = "clarification-form";
    form.dataset.binding = record.question_token;
    const answer = field(form, "answer", "yourAnswer", { multiline: true, max: 2000, required: true });
    const submit = element("button", t("recordAnswer")); submit.type = "submit"; form.append(submit);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      mutate("clarify", {
        ...source, question_token: record.question_token, answer: answer.value.trim(), request_id: requestId(),
      }, showClarification);
    });
    target.append(form);
  } else target.append(element("p", t("clarificationNext")));
  if (record.changes && Object.keys(record.changes).length) {
    append(target, element("h4", t("proposedChanges")), element("pre", JSON.stringify(record.changes, null, 2)),
      writeButton(t("reviewRefinement"), () => mutate("clarify-proposal", { clarification_id: record.id }, showProposal), "primary"));
  }
  parent.append(target);
}
function proposalTitle(record) {
  if (record.kind === "capture_task" && record.source_status === "current" && record.action?.definition?.title) {
    return record.action.definition.title;
  }
  return t(({
    capture_task: "captureOperation", refine_task: "refineOperation", edit_task: "changeOperation",
    close_task: "closeOperation", prepare_task: "prepareOperation", research: "researchOperation",
  })[record.kind] || "taskOperation");
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
  if (result?.status !== "conflict") return null;
  const key = CLOSURE_CONFLICTS[closureConflictCode(result.error)];
  return { label: t(`${key}Label`), text: t(`${key}Text`) };
}
function proposalStatusText(record) {
  if (record.kind === "capture_task") {
    if (captureSaved(record)) return t("captureSavedDetail", { stage: t(record.action?.definition?.stage || "clarify") });
    if (record.status === "submitted") return t("captureSubmitted");
    if (record.status === "executing") return t("captureSaving");
    if (["uncertain", "completed"].includes(record.status)) return t("captureUnconfirmed");
    if (record.approved_by === "owner-capture") return t("captureNotSaved");
  }
  const conflict = closureConflict(record);
  if (conflict) return `${conflict.text} ${t("freshClosureApproval")}`;
  if (confirmedClosureScan(record)) {
    return t("closureCheckpointConfirmed", { count: record.result.scan_pass });
  }
  if (closureScanPending(record)) {
    return t("closureCheckUnconfirmed");
  }
  if (record.status === "completed") {
    if (record.kind === "prepare_task") return t("privateDraftAvailable");
    if (record.kind === "research") return t("researchResultAvailable");
    if (record.kind === "close_task") return t("closureSaved");
    return t("operationSaved");
  }
  if (record.status === "submitted") return t("publicationSubmitted");
  if (["uncertain", "executing"].includes(record.status)) return t("operationUnconfirmed");
  if (record.status === "pending") return t("exactApprovalRequired");
  return t("noNewSuccess");
}
function renderProposal(record) {
  const article = element("article", null, "proposal");
  const contentAvailable = record.action && (!record.source_status || record.source_status === "current");
  const conflict = closureConflict(record);
  const capture = record.kind === "capture_task";
  const status = capture && ((record.status === "completed" && !captureSaved(record))
    || (record.status === "pending" && record.approved_by === "owner-capture")) ? "uncertain" : record.status;
  const label = captureSaved(record) ? t("captureSaved") : capture && status === "submitted" ? t("captureSubmittedLabel")
    : capture && status === "executing" ? t("captureSavingLabel") : conflict?.label;
  append(article, statusBadge(status, label), element("h3", proposalTitle(record)), element("p",
    contentAvailable ? proposalStatusText(record) : (record.text || t("historicalReceipt"))));
  if (capture && !contentAvailable) article.append(element("p", proposalStatusText(record)));
  if (conflict && !contentAvailable) article.append(element("p", proposalStatusText(record)));
  const source = element("p", null, "source");
  append(source, element("span", `${record.created_on || ""}${record.expires_on ? ` / ${t("expiresLabel")} ${record.expires_on}` : ""} `), safeLink(t("sourceLabel"), record.source_url));
  article.append(source);
  if (contentAvailable) {
    if (capture) {
      const request = element("div", null, "outcome-block");
      request.append(element("p", record.action.text));
      article.append(request);
    }
    const action = element(capture ? "details" : "div", null, "exact-action");
    append(action, element(capture ? "summary" : "h4", t(capture ? "captureRecordDetails" : "exactAction")),
      element("pre", JSON.stringify(record.action, null, 2)));
    if (record.kind === "close_task" && record.closure_scan_protocol === 1) {
      action.append(element("p", t("closurePassHint"), "hint"));
    }
    article.append(action);
  }
  if (contentAvailable && record.result?.preparation) {
    const data = record.result.preparation;
    const result = element("section", null, "result");
    append(result, element("h4", t("preparedNotVerified")), element("p", data.summary));
    for (const [heading, items] of [[t("suggestedSteps"), data.steps], [t("uncertainties"), data.uncertainties]]) {
      if (Array.isArray(items) && items.length) {
        result.append(element("h4", heading));
        const list = element("ul"); items.forEach((item) => list.append(element("li", item))); result.append(list);
      }
    }
    append(result, element("h4", t("ownerNextAction")), element("p", data.owner_next_action));
    if (data.source_quote) append(result, element("h4", t("sourceEvidence")), element("blockquote", data.source_quote));
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
    append(details, element("summary", t("operationReceipts")),
      element("pre", JSON.stringify({ result: receipt(record.result), publication: receipt(record.publish_result) }, null, 2)));
    const url = record.publish_result?.pr_url || record.result?.pr_url || record.result?.url;
    append(article, details, safeLink(t("openResultStatus"), url));
  }
  const actions = element("div", null, "actions");
  if (contentAvailable && record.status === "pending" && record.approval_digest && !(capture && record.approved_by === "owner-capture")) {
    actions.append(writeButton(t("approveExact"), () => mutate("decide", {
      proposal_id: record.id, approval_digest: record.approval_digest, decision: "approve",
    }, updateProposal), "primary"));
    actions.append(writeButton(t("declineAction"), () => mutate("decide", {
      proposal_id: record.id, approval_digest: record.approval_digest, decision: "decline",
    }, updateProposal)));
  }
  if (contentAvailable && confirmedClosureScan(record)) {
    actions.append(writeButton(t("continueClosure"), () => mutate("continue-closure", {
      proposal_id: record.id, request_id: requestId(), closure_continuation: record.result.closure_continuation,
    }, updateProposal), "primary"));
    actions.append(element("p", t("closureStepHint"), "hint"));
  } else if (contentAvailable && closureScanPending(record)) {
    actions.append(writeButton(t("recoverClosure"), () => mutate("recover-closure", {
      proposal_id: record.id, request_id: requestId(),
    }, updateProposal)));
  } else if (["submitted", "executing", "uncertain"].includes(record.status) || (capture && record.status === "completed" && !captureSaved(record))) {
    actions.append(writeButton(t(capture ? "checkCapture" : "checkExistingRequest"), () => mutate("reconcile", { proposal_id: record.id }, updateProposal)));
  }
  if (contentAvailable && record.status === "completed" && ["prepare_task", "research"].includes(record.kind) && !record.reviewed) {
    actions.append(writeButton(t("reviewedResult"), () => mutate("review-result", { proposal_id: record.id }, async () => {
      showNotice(t("resultMarkedReviewed"));
      await loadOverview();
    })));
  }
  if (!contentAvailable && record.status === "completed" && ["prepare_task", "research"].includes(record.kind) && !record.reviewed) {
    actions.append(writeButton(t("acknowledgeUnavailable"), () => mutate("review-result", {
      proposal_id: record.id, acknowledge_unavailable: true,
    }, async () => {
      showNotice(t("unavailableAcknowledged"));
      await loadOverview();
    })));
  }
  article.append(actions);
  return article;
}
function showProposal(record, { focus = true } = {}) {
  const redacted = ["changed_or_removed", "unavailable"].includes(record.source_status);
  if (!/^[a-f0-9]{24}$/.test(record.id || "") || !record.status || (!record.action && !redacted)) {
    throw new Error(t("proposalReceiptIncomplete"));
  }
  if (record.kind === "capture_task" && (
    !["pending", "executing", "submitted", "uncertain", "completed", "failed", "invalidated", "expired", "declined"].includes(record.status)
    || (!redacted && (record.source_status !== "current" || record.action?.kind !== "create_task"
      || typeof record.action.text !== "string" || !record.action.text
      || typeof record.action.definition?.title !== "string" || !record.action.definition.title))
  )) throw new Error(t("proposalReceiptIncomplete"));
  if ($("proposal-section").hidden) focusOrigins.proposal = document.activeElement;
  state.proposal = record;
  if (state.captureReceipt?.id === record.id) state.captureReceipt = captureSaved(record) ? null : record;
  const heading = record.kind === "capture_task" && (record.status !== "pending" || record.approved_by === "owner-capture")
    ? "captureResult" : "reviewBefore";
  $("proposal-heading").dataset.i18n = heading;
  $("proposal-heading").textContent = t(heading);
  $("proposal-section").hidden = false;
  $("proposal-preview").replaceChildren(renderProposal(record));
  showNotice(redacted ? (record.text || t("sourceReceiptOnly")) : proposalStatusText(record),
    captureSaved(record) || (record.status === "pending" && !redacted) ? "" : "pending");
  watchCaptureReceipt();
  if (focus) {
    $("proposal-section").scrollIntoView({ block: "start" });
    restoreFocus($("proposal-heading"));
  }
}
function captureSaved(record) {
  return record?.kind === "capture_task" && record.status === "completed" && record.source_status === "current"
    && (record.publish_result || record.result)?.status === "merged";
}
function captureUnresolved(record = state.captureReceipt) {
  return record && (["submitted", "executing", "uncertain"].includes(record.status)
    || (record.status === "completed" && !captureSaved(record)));
}
function watchCaptureReceipt() {
  const record = state.captureReceipt;
  if (!record && state.capturePoll) {
    clearTimeout(state.capturePoll.timer);
    state.capturePoll.timer = null;
  }
  if (!captureUnresolved(record) || state.proposal?.id !== record.id || $("proposal-section").hidden) return;
  if (state.capturePoll?.id !== record.id) {
    clearTimeout(state.capturePoll?.timer);
    state.capturePoll = { id: record.id, generation: state.sessionGeneration, attempt: 0, timer: null };
  }
  const poll = state.capturePoll;
  if (poll.timer || poll.attempt >= 6 || document.hidden || state.unknown) return;
  poll.timer = setTimeout(() => { poll.timer = null; return pollCaptureReceipt(poll); }, Math.min(2000 * 2 ** poll.attempt, 60000));
}
async function pollCaptureReceipt(poll) {
  if (poll !== state.capturePoll || poll.generation !== state.sessionGeneration || !state.csrf
    || !captureUnresolved() || state.proposal?.id !== poll.id || $("proposal-section").hidden || document.hidden) return;
  if (state.busy) { watchCaptureReceipt(); return; }
  poll.attempt += 1;
  await mutate("reconcile", { proposal_id: poll.id }, (record) => {
    if (record.id !== poll.id || record.kind !== "capture_task") throw new Error(t("proposalReceiptIncomplete"));
    if (state.proposal?.id === poll.id) showProposal(record, { focus: false });
  }, loadOverview);
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
  if (!state.overview) empty(target, t("historyUnavailable"), t("historyUnavailableDetail"));
  else if (!history.length) empty(target, t("activityEmpty"), t("activityEmptyDetail"));
  else history.forEach((record) => target.append(renderProposal(record)));
  $("history-more").hidden = state.overview?.history_next_offset === null || state.overview?.history_next_offset === undefined;
}
function renderAreas(preserve = false) {
  const data = state.overview;
  if (!data) return;
  const choices = preserve ? new Map(
    Array.from(document.querySelectorAll("#projects-form input, #standing-form input"))
      .map((input) => [`${input.name || input.id}:${input.value}`, input.checked])
  ) : null;
  $("area-list").replaceChildren();
  for (const area of data.areas) {
    const count = state.items.filter((task) => task.area === area).length;
    $("area-list").append(button(`${prettyId(area)} (${count} ${t("loaded")})`, () => {
      $("area-filter").value = area; setView("tasks", { arrival: true });
    }));
  }
  $("project-options").replaceChildren();
  if (!data.projects.length) empty($("project-options"), t("noProjects"), t("noProjectsDetail"));
  for (const project of data.projects) {
    const label = element("label", null, "checkbox");
    const input = element("input"); input.type = "checkbox"; input.name = "project"; input.value = project.id;
    input.checked = Object.hasOwn(data.active_projects || {}, project.id);
    append(label, input, element("span", prettyId(project.id)));
    $("project-options").append(label);
  }
  const unseen = Object.keys(data.active_projects || {}).filter((id) => !data.projects.some((project) => project.id === id));
  $("project-page-status").textContent = `${t("projectsLoaded")}: ${data.projects.length}.${data.project_excluded_count ? ` ${t("projectsExcluded")}: ${data.project_excluded_count}.` : ""}${data.project_next_offset !== null && data.project_next_offset !== undefined ? ` ${t("moreCandidates")}` : ""}${unseen.length ? ` ${t("unseenProjects")}: ${unseen.length}. ${t("reviewUnseenProjects")}` : ""}`;
  $("projects-more").hidden = data.project_next_offset === null || data.project_next_offset === undefined;
  $("standing-enabled").checked = Boolean(data.standing?.enabled);
  $("standing-options").replaceChildren();
  const eligible = state.items.filter((task) => task.stage === "ready" && task.definition?.complete && Object.hasOwn(data.active_projects || {}, task.project));
  if (!eligible.length) empty($("standing-options"), t("noEligibleTasks"), t("noEligibleTasksDetail"));
  for (const task of eligible) {
    const label = element("label", null, "checkbox");
    const input = element("input"); input.type = "checkbox"; input.name = "standing-task"; input.value = task.path;
    input.checked = data.standing?.sources?.[task.path] === task.revision;
    append(label, input, element("span", task.title));
    $("standing-options").append(label);
  }
  const unloaded = Object.keys(data.standing?.sources || {}).filter((path) => !eligible.some((task) => task.path === path));
  if (unloaded.length) {
    $("standing-options").append(element("p", t("unloadedStanding", { count: unloaded.length }), "notice pending"));
  }
  $("budget-details").textContent = JSON.stringify({ limits: data.limits, usage: data.budget }, null, 2);
  if (choices) for (const input of document.querySelectorAll("#projects-form input, #standing-form input")) {
    const key = `${input.name || input.id}:${input.value}`;
    if (choices.has(key)) input.checked = choices.get(key);
  }
  setBusy(state.busy);
}
function clearData() {
  clearTimeout(state.capturePoll?.timer);
  state.capturePoll = null; state.captureReceipt = null;
  clearManualDraft();
  clearAssist();
  dashboard.clear();
  clearTimeout(state.expiryTimer);
  state.expiryTimer = null;
  state.authExpires = 0;
  state.sessionGeneration += 1;
  state.detailGeneration += 1;
  state.detailIntent += 1;
  state.csrf = ""; state.items = []; state.overview = null; state.selected = null;
  state.proposal = null; state.clarification = null; state.unknown = null;
  state.captureSource = null; state.snapshotCheckedAt = 0;
  for (const key of Object.keys(focusOrigins)) focusOrigins[key] = null;
  $("capture-context").textContent = "";
  $("capture-context").hidden = true;
  renderCanonicalLinks();
  renderSourceWarnings();
  for (const id of ["task-list", "inspector", "activity-list", "proposal-preview", "project-options",
    "standing-options", "area-list", "capture-details", "budget-details", "snapshot", "attention"]) $(id).replaceChildren();
  delete $("inspector").dataset.path;
  delete $("inspector").dataset.revision;
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
  $("access-retry").hidden = true;
  setAccessMessage("sessionExpired");
  for (const id of ["refresh", "capture-open", "sign-out"]) $(id).hidden = true;
  setConnection("signedOut");
  restoreFocus($("sign-in"));
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
  setAccessMessage("checkingWorkspace");
  setConnection("checkingSignIn");
  try {
    const session = await api("session");
    if (session.authenticated !== true || typeof session.csrf !== "string" || !session.csrf
      || !Number.isFinite(session.expires) || session.expires * 1000 <= Date.now()) throw new Error("The owner session could not be verified. Sign in again.");
    state.csrf = session.csrf;
    state.authExpires = session.expires * 1000;
    $("access").hidden = true;
    $("workspace").hidden = false;
    for (const id of ["refresh", "capture-open", "sign-out"]) $(id).hidden = false;
    setConnection(isMockMode() ? "mock" : "personalWorkspace");
    $("quick-bar").hidden = false;
    checkSessionExpiry();
    await loadOverview();
    if (!state.selected) closeDetail();
    setView(location.hash.slice(1) || "today");
    await loadAssist();
  } catch (error) {
    state.csrf = "";
    setAccessMessage(error.status === 401 ? "signInPrompt" : "", errorMessage(error));
    $("sign-in").hidden = ![401, 403].includes(error.status);
    $("access-retry").hidden = [401, 403].includes(error.status);
    setConnection(error.status === 401 ? "signedOut" : "accessUnavailable");
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
$("scope-needs").addEventListener("click", () => setView("needs", { arrival: true }));
$("scope-all").addEventListener("click", () => setView("tasks", { arrival: true }));
window.addEventListener("hashchange", () => setView(location.hash.slice(1), { arrival: true }));
$("load-more").addEventListener("click", () => loadOverview(true));
$("projects-more").addEventListener("click", loadProjects);
$("history-more").addEventListener("click", loadHistory);
$("access-retry").addEventListener("click", start);
document.addEventListener("visibilitychange", () => { if (!document.hidden) checkSessionExpiry(); });
window.addEventListener("focus", checkSessionExpiry);
window.addEventListener("pageshow", checkSessionExpiry);
function beginSourceCapture(source, finding) {
  if (captureEntryBlocked()) return;
  state.captureSource = { id: source.id, revision: source.revision, finding: finding?.id || null };
  $("capture-form").reset();
  const text = finding?.next_step || t("sourceCaptureRequest", { title: source.title });
  $("capture-text").value = text;
  $("capture-title-input").value = Array.from(text).slice(0, 120).join("");
  const prefix = element("span", t("sourceCaptureFrom")); prefix.dataset.i18n = "sourceCaptureFrom";
  const hint = element("span", t("sourceCaptureHint")); hint.dataset.i18n = "sourceCaptureHint";
  $("capture-context").replaceChildren(prefix, `${source.title}${finding ? `, ${finding.id}` : ""}. `, hint);
  $("capture-context").hidden = false;
  openCapture();
}
function openCapture() {
  for (const [id, key] of [
    ["capture-submit", state.captureSource ? "reviewCapture" : "addTask"],
    ["capture-hint", state.captureSource ? "sourceCaptureSaveHint" : "captureHint"],
  ]) {
    $(id).dataset.i18n = key;
    $(id).textContent = t(key);
  }
  if ($("capture-section").hidden) focusOrigins.capture = document.activeElement;
  $("capture-section").hidden = false;
  $("capture-text").focus();
  $("capture-section").scrollIntoView({ block: "start" });
}
$("capture-open").addEventListener("click", () => {
  if (captureEntryBlocked()) return;
  if (state.captureSource) $("capture-form").reset();
  state.captureSource = null;
  $("capture-context").hidden = true;
  openCapture();
});
function captureEntryBlocked() {
  if (state.busy) return true;
  if (state.unknown) {
    showNotice(t("requestUnconfirmedHint"), "pending");
    return true;
  }
  if (captureUnresolved()) {
    showProposal(state.captureReceipt);
    return true;
  }
  return false;
}
function closeCapture() {
  if (!state.busy && !state.unknown) {
    if (state.captureSource) $("capture-form").reset();
    state.captureSource = null;
    $("capture-context").hidden = true;
  }
  $("capture-section").hidden = true;
  restoreFocus(focusOrigins.capture, matchMedia("(max-width: 690px)").matches ? $("quick-capture") : $("capture-open"));
  focusOrigins.capture = null;
}
$("capture-cancel").addEventListener("click", closeCapture);
$("proposal-close").addEventListener("click", () => {
  clearTimeout(state.capturePoll?.timer);
  if (state.capturePoll) state.capturePoll.timer = null;
  $("proposal-section").hidden = true; state.proposal = null;
  restoreFocus(focusOrigins.proposal, $("view-title")); focusOrigins.proposal = null;
});
$("recover-request").addEventListener("click", () => {
  if (state.unknown && !state.busy) performMutation(state.unknown);
});
$("capture-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (captureEntryBlocked()) return;
  const form = new FormData(event.currentTarget);
  const text = String(form.get("text") || "");
  const firstLine = text.split(/\r?\n/)
    .map((line) => line.trim().replace(/^(?:[-*+]\s+|\d+[.)]\s+|#{1,6}\s+|>\s*)/, "").trim())
    .find(Boolean) || "";
  const title = String(form.get("title") || "").trim() || Array.from(firstLine).slice(0, 120).join("");
  if (!text.trim() || !title) { showNotice(t("captureTextRequired"), "error"); return; }
  const definition = { title, stage: "clarify" };
  for (const name of ["area", "project", "execution", "outcome", "next_action", "done_when"]) {
    const value = String(form.get(name) || "").trim();
    if (value) definition[name] = value;
  }
  const source = state.captureSource;
  mutate(source ? "dashboard/capture" : "capture-save", { request_id: requestId(), text, definition, ...(source || {}) }, (record) => {
    if (record.kind !== "capture_task" || (!source && record.source_status === "current" && (
      record.action?.text !== text || record.action?.definition?.title !== title
      || record.approved_by !== "owner-capture"
    ))) throw new Error(t("proposalReceiptIncomplete"));
    showProposal(record);
    if (!source) {
      state.captureReceipt = captureSaved(record) ? null : record;
      watchCaptureReceipt();
    }
    $("capture-form").reset();
    $("capture-section").hidden = true;
    state.captureSource = null;
    $("capture-context").hidden = true;
  }, loadOverview);
});
$("projects-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const unseen = Object.keys(state.overview?.active_projects || {}).filter((id) => !state.overview.projects.some((project) => project.id === id));
  const projects = [...new Set([...new FormData(event.currentTarget).getAll("project"), ...unseen])];
  if (projects.length > 8) { showNotice(t("projectLimit"), "error"); return; }
  mutate("projects", { projects }, async () => { showNotice(t("projectsSaved")); await loadOverview(); });
});
$("projects-clear").addEventListener("click", () => mutate("projects", { projects: [] }, async () => {
  showNotice(t("projectsCleared"));
  await loadOverview();
}));
$("projects-clear").dataset.write = "true";
$("standing-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const enabled = $("standing-enabled").checked;
  const paths = enabled ? new FormData(event.currentTarget).getAll("standing-task") : [];
  if (paths.length > 3 || (enabled && !paths.length)) { showNotice(t("standingLimit"), "error"); return; }
  const sources = {};
  for (const path of paths) {
    const task = state.items.find((item) => item.path === path);
    if (!task) { showNotice(t("standingSourceMissing"), "error"); return; }
    sources[path] = task.revision;
  }
  mutate("standing", { enabled, sources }, async () => { showNotice(t(enabled ? "standingSaved" : "standingOff")); await loadOverview(); });
});
$("standing-stop").addEventListener("click", () => mutate("standing", { enabled: false, sources: {} }, async () => { showNotice(t("standingStopped")); await loadOverview(); }));
$("standing-stop").dataset.write = "true";
$("sign-out").addEventListener("click", async () => {
  if (state.busy) { showNotice(t("signOutBusy"), "pending"); return; }
  const unconfirmed = Boolean(state.unknown);
  setBusy(true);
  try {
    if (isMockMode()) { expireSession(); return; }
    const response = await fetch("/api/tasks/auth/logout", {
      method: "POST", credentials: "same-origin", mode: "same-origin", cache: "no-store",
      headers: { "Content-Type": "application/json", "X-MindMe-CSRF": state.csrf }, body: "{}",
    });
    if (response.status === 401) { expireSession(); return; }
    if (!response.ok) throw new Error(t("signOutUnconfirmed"));
    clearData();
    for (const id of ["capture-open", "refresh", "sign-out"]) $(id).hidden = true;
    await start();
    if (unconfirmed) setAccessMessage("signedOutUnconfirmed");
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
  polls: new Map(), usage: null,
};
const COPY = {
  en: {
    skipWorkspace: "Skip to workspace", vaultLabel: "Personal vault", workspaceLabel: "Workspace", workspaceViews: "Workspace views",
    accessThink: "Room to think.", accessAct: "Space to act.",
    accessIntro: "Your tasks, ideas and evidence. One personal space to move them forward.",
    signIn: "Sign in with Microsoft", tryAgain: "Try again",
    ownerOnly: "For the configured personal owner only. Your tasks stay in their existing vault; this is a view and an authorized way to work with them.",
    artCaption: "From possibility to a next move.", artFoot: "A little structure. More possibility.",
    checkingWorkspace: "Checking your personal workspace. No private content is stored in this browser.",
    checkingSignIn: "Checking sign-in", signInPrompt: "Sign in with your configured personal Microsoft account to see your work.",
    sessionExpired: "Sign in again with your personal account. Inspect Activity before repeating any unconfirmed operation.",
    signedOutUnconfirmed: "Signed out. One earlier request was unconfirmed; inspect Activity after signing in before creating a replacement.",
    signOutBusy: "A request is still returning. Sign out as soon as its response or timeout is reported.",
    signOutUnconfirmed: "Sign-out was not confirmed. Try again before leaving a shared device.",
    personalWorkspace: "Personal workspace", signedOut: "Signed out", accessUnavailable: "Access unavailable", workingRequest: "Working on your request",
    openCopilot: "Open copilot command bar", closeCopilot: "Close copilot",
    useDark: "Use dark theme", useLight: "Use light theme", useRussian: "Switch to Russian", useEnglish: "Switch to English",
    grounded: "Grounded in your vault.", groundedDetail: "AI helps you move. Your canonical record stays the source of truth.",
    footer: "Personal work, grounded in your vault.", footerNote: "Think clearly. Move deliberately.",
    nextMoves: "Next moves", loadedPriorities: "From loaded work", approvedFocus: "Approved focus",
    workToLookAt: "Work to look at", newSince: "New since last visit",
    knowledgeInbox: "Inbox", knowledgeHint: "Findings first. Evidence one step away.",
    knowledgeIntro: "Canonical daily reviews and weekly digests. Their interpretations and possible next steps are not approved work.",
    earlierReviews: "Load earlier reviews", evidenceReader: "Evidence reader", selectedTask: "Selected task",
    captureIntro: "One rough request is enough. Missing details can stay in Clarify. Keep sensitive details in their private record.",
    capturePlaceholder: "Describe the outcome or the question you want to resolve.",
    optionalTitle: "Title (optional)", titlePlaceholder: "Leave blank to use the first line of your request",
    captureHint: "Add task saves this request as one task in Clarify. It does not make purchases, start research or run an agent.",
    sourceCaptureSaveHint: "First review the source-bound record. Capturing it will not start research or an agent.",
    addTask: "Add task", captureResult: "Task capture", captureRecordDetails: "Technical task record",
    captureSubmittedLabel: "Submitted", captureSavingLabel: "Saving",
    captureSaving: "Saving this task. No saved result is confirmed yet.",
    captureSubmitted: "Your request was received. We are checking that the task was saved; no need to add it again.",
    captureUnconfirmed: "Saving is not confirmed. It may already be saved; check this same request, not a new capture.",
    captureNotSaved: "This request did not confirm a saved task. Inspect its receipt before trying again.",
    captureSaved: "Task saved", captureSavedDetail: "Added to your task list in {stage}. No work was started.",
    checkCapture: "Check save status", captureTextRequired: "Describe the task before adding it.",
    reviewBefore: "Review before acting", returnWorkspace: "Return to workspace",
    responsibilities: "Responsibilities and selected projects",
    areasIntro: "Areas are ongoing responsibilities. A project has an outcome; an inventory entry is not automatically an active commitment.",
    selectProjects: "Select projects for planning",
    projectsHint: "Choose up to eight existing projects. Changing this selection turns off standing AI permission until you review it again.",
    moreProjects: "Load more projects", saveProjects: "Save project selection", clearProjects: "Clear project selection",
    standingTitle: "Let AI prepare, within clear limits",
    standingIntro: "Optionally authorize private draft preparation for up to three fully defined Ready tasks in your selected projects. Changes to their source invalidate that scope.",
    standingEnable: "Enable standing preparation for the tasks selected below",
    standingHint: "This does not authorize public research, new commitments, messages, purchases, or verified completion.",
    saveStanding: "Save standing permission", stopStanding: "Stop standing preparation",
    preparationLimits: "Preparation limits and usage",
    limitsHint: "These are operational limits, not a guarantee of a provider's bill. Unconfirmed work is never treated as a refund.",
    decisionsResults: "Decisions and results",
    activityIntro: "Follow the same request from proposal to canonical record. A prepared draft is not a verified outcome.",
    moreActivity: "Load more activity", moreTasks: "Load more tasks", moreInformation: "More information",
    detailEmptyTitle: "A next step, not another list.",
    detailEmptyDetail: "Open a task to clarify its outcome, plan the next action, or review what AI can prepare.",
    areaUnclear: "Area not clarified", clarifyNext: "Clarify the next action.", selectedToday: "Selected for today",
    deadlineLabel: "Deadline", reviewLabel: "Review", deadlineApproaching: "Approaching deadline",
    deadlineDue: "Deadline due", reviewDue: "Review due",
    waitingDependency: "Waiting on a dependency", definitionNeedsWork: "Definition needs work",
    minutes: "min", loadingTasks: "Loading tasks", noLoadedTasks: "No loaded tasks", loadingCanonical: "Loading the canonical task...",
    allStages: "All stages", allAreas: "All areas",
    refreshFailed: "Task refresh was not confirmed. Inspect the source status before continuing.",
    sourcesUnavailable: "Your sources are not available yet.", sourcesUnavailableDetail: "Refresh to load them. Unavailable is not an empty backlog.",
    attentionIncomplete: "The attention check is incomplete.", attentionIncompleteDetail: "More task sources or source errors remain. Load more or resolve the reported issue before concluding that nothing is due.",
    sourceScope: "Some sources are outside this view",
    sourceScopeExplanation: "This is due to scope or privacy rules, not loading failures. Names and contents stay hidden.",
    sourceTaskExclusions: "Excluded task sources in this assessed window: {count}.",
    sourceProjectExclusions: "Excluded project sources checked so far: {count}.",
    sourceAttentionPartial: "Attention coverage is incomplete; unchecked or unresolved sources remain.",
    sourceAttentionAssessedPartial: "Permitted task definitions assessed in this bounded window: {count}. Attention coverage is incomplete; unchecked or unresolved sources remain.",
    sourceTaskIssues: "Task source issues needing attention: {count}. Unavailable sources are not treated as empty or complete.",
    sourceProjectIssues: "Project source issues: {count}. The permitted inventory is incomplete; refresh to retry.",
    sourceMetadataMissing: "A task has no supported metadata block. Its dates could not be assessed.",
    sourceMetadataInvalid: "A source has invalid or conflicting metadata. Correct the source before relying on its dates.",
    sourceDefinitionInvalid: "A task has an invalid definition field. Its attention status is unknown.",
    sourceDateInvalid: "A task has an invalid date. Correct the saved date before relying on this view.",
    sourceTitleMissing: "A task has no supported title. Its definition could not be assessed.",
    sourceReviewAmbiguous: "A task has conflicting review dates. Reconcile them in the source.",
    sourceTooLarge: "A source exceeded the bounded reader's size limit and was not assessed.",
    sourcePolicyUnresolved: "A source's scope or privacy metadata is ambiguous. It remains unavailable until clarified.",
    sourceUnavailable: "A source could not be read or verified. Refresh to retry its current version.",
    sourceIssueUnknown: "A source could not be assessed. Its absence is not evidence that no work is due.",
    noMatchingAttention: "No matching due or selected tasks on this page.", noMatchingTasks: "No matching tasks on this page.",
    noMatchingDetail: "Check the filters, load more tasks, or capture a new request. Nothing has been completed or discarded.",
    loadedTasks: "Loaded tasks", loadedOnly: "Filters and counts cover loaded, permitted tasks only.", availabilityUnconfirmed: "Source availability has not been confirmed.",
    reviewSnapshot: "Canonical review snapshot", reviewSnapshotSeparate: "Review sources checked separately from tasks",
    taskDay: "Task day", snapshotChecked: "Snapshot checked",
    historyUnavailable: "History is unavailable.", historyUnavailableDetail: "Refresh before inferring that no work ran.",
    activityEmpty: "No operations on this loaded page.", activityEmptyDetail: "Capture a task or prepare an action. Its proposals and receipts will appear here.",
    loaded: "loaded", noProjects: "No permitted projects returned.", noProjectsDetail: "This is not permission to create or activate a project automatically.",
    projectsLoaded: "Permitted projects loaded", projectsExcluded: "Checked candidates excluded by scope/privacy policy",
    moreCandidates: "More candidates remain; load more to inspect permitted projects.",
    unseenProjects: "Existing selections outside this page retained", reviewUnseenProjects: "Load more to review them, or explicitly clear the selection.",
    noEligibleTasks: "No eligible loaded tasks.", noEligibleTasksDetail: "Select a project and explicitly ready a fully defined task. Load more tasks if needed.",
    manualDraftInvalidated: "Unsaved manual changes were discarded because the task changed or is no longer available. Review the current source before editing again.",
    titleField: "Title", project: "Project", executionMode: "Execution mode", outcomeField: "Outcome",
    doneWhenField: "Done when", waitingFor: "Waiting for", reviewOn: "Review on", focusOn: "Focus on",
    chooseArea: "Choose an area", noProjectSelected: "No project selected", keepUntriaged: "Keep untriaged",
    notDecided: "Not decided", executionHuman: "Me", executionAssisted: "Me, with AI help", executionAgent: "Agent (requires authorization)",
    execution_human: "Me", execution_assisted: "Me, with AI help", execution_agent: "Agent (requires authorization)",
    whoActs: "Who performs the next action?", desiredOutcome: "Desired outcome",
    doneWhenHint: "An observable result you can actually check.",
    clarifyRecord: "Clarify the record", definitionHint: "Review exact changes before saving. A stage or execution label does not start an agent.",
    waitingHint: "Name the dependency, not sensitive personal details. Clear this field only when the blocker is resolved.",
    reviewOnHint: "Agree a review date when moving started work to Waiting.",
    reviewDefinition: "Review definition changes", noDefinitionChanges: "There are no definition changes to review.",
    fieldCannotClear: "The {field} field cannot be cleared through this form. Choose a replacement or keep it unchanged.",
    chooseWaitingStage: "Choose the task's stage when changing waiting details here, or use the one-date form below.",
    changeOneDate: "Change one date deliberately", dateType: "Date type", dateField: "Date",
    nextReview: "Next review", selectedFocusDate: "Selected focus date", realDeadline: "Real deadline",
    removeDate: "Explicitly remove this date", reviewDateChange: "Review date change",
    dateHint: "A review is not a deadline. Snoozing a review does not change a deadline. Daily focus does not roll over automatically.",
    chooseDate: "Choose a date or explicitly select removal.",
    preparationTitle: "Make the next step easier",
    preparationIntro: "Choose a bounded assignment, then review its exact scope. AI output remains a draft until you verify the outcome.",
    preparationEligibility: "This task needs a complete definition and owner-selected Ready, Doing or Verify stage before preparation.",
    workToPrepare: "Work to prepare", privateDraft: "Private draft from this task", publicResearch: "Bounded public research",
    exactScope: "Exact question or preparation scope", reviewAssignment: "Review AI assignment",
    privatePreparationLimits: "Private preparation: one model call, no external tools, at most 45 seconds. It does not perform the real-world action.",
    publicResearchLimits: "Use one impersonal public question. Never include private context. Research is limited to five sources, one short report and no follow-on jobs; provider costs may be separate.",
    closureTitle: "Close the outcome, not just the run",
    closureIntro: "A draft, sent request or completed agent run is not enough. Record what actually happened and how you know.",
    closureNeedsCondition: "Add a concrete Done when condition before closing this task.",
    resultAchieved: "What was achieved?", evidenceField: "Evidence or verification", verifiedOn: "Date you verified the result",
    evidenceHint: "Describe or reference the permitted evidence. Keep private details in their own record.",
    verifiedOnHint: "This uses your configured task calendar and records verification, not an assumed date the work happened.",
    learningField: "Useful lesson", learningHint: "Optional. What should you or an agent do differently next time?",
    ownerAttestation: "I checked the done condition against the actual result and evidence.", reviewClosure: "Review verified closure",
    openCanonical: "Open canonical source", revision: "Revision", taskActions: "Task actions", notRecorded: "Not recorded",
    taskAttentionFilter: "Task attention filter", taskLayout: "Task layout",
    needsClarification: "Needs clarification: {fields}. These are structural checks, not approval.",
    clarifyTask: "Clarify this task", readFullSource: "Read the full source",
    clarificationRecorded: "Clarification is recorded separately. No task definition has been changed.",
    oneQuestion: "One question at a time", answersUsed: "{count} of 3 answers used for this source version. Unresolved work can stay in Clarify.",
    answerBindingMissing: "This question has no valid answer binding. Refresh the task and start clarification again.",
    yourAnswer: "Your answer", recordAnswer: "Record answer", proposedChanges: "Proposed changes", reviewRefinement: "Review this refinement",
    clarificationNext: "Review the proposed refinement or continue editing the task directly. No additional question is required.",
    sourceCaptureRequest: "Review {title} and choose a concrete next action.", sourceCaptureFrom: "From ",
    sourceCaptureHint: "Canonical provenance and exact evidence will be retained and rechecked before approval.",
    unloadedStanding: "{count} previously authorized task reference(s) are not eligible on the loaded page. Saving replaces the selection with the checked tasks shown here.",
    projectLimit: "Choose at most eight projects.",
    projectsSaved: "Project selection saved. Standing preparation is off until you approve its scope again.",
    projectsCleared: "Project selection cleared. Standing preparation is off; no source project or task was removed.",
    standingLimit: "Choose one to three eligible tasks before enabling preparation.",
    standingSourceMissing: "A selected source is no longer loaded. Refresh before authorizing it.",
    standingSaved: "Standing permission saved for those exact sources. No real-world action is authorized.",
    standingOff: "Standing preparation is off.", standingStopped: "Standing preparation is off. Existing results and receipts are retained.",
    workingExactRequest: "Working on this exact request. Please wait for its receipt.",
    requestUnconfirmed: "This request has not been confirmed.",
    requestUnconfirmedHint: "It may already have reached the server. Keep this page open, inspect Activity, or recover this exact request. Do not create a replacement.",
    recoverSameRequest: "Recover the same request",
    captureOperation: "Capture a task", refineOperation: "Refine the definition", changeOperation: "Change the task",
    closeOperation: "Verify and close", prepareOperation: "Prepare a private draft", researchOperation: "Research a public question", taskOperation: "Task operation",
    freshClosureApproval: "Any new attempt needs a fresh proposal and approval.",
    closureCheckpointConfirmed: "Closure check checkpoint confirmed ({count} of 6 scan passes recorded). The task is still open; no closure has been submitted. Continue only when you want the next bounded step.",
    closureCheckUnconfirmed: "The last closure check is not confirmed. Recover that same request to inspect its progress; do not start another closure or assume the task is saved.",
    privateDraftAvailable: "A private draft is available. The task is still open.",
    researchResultAvailable: "The research operation has a result. Verify the artifact; this does not complete the underlying task.",
    closureSaved: "The approved closure reached the canonical record.", operationSaved: "Saved to the canonical record. Closing the task still needs its evidence.",
    publicationSubmitted: "Submitted for publication. Not yet confirmed on the canonical branch.",
    operationUnconfirmed: "The result is not confirmed. Check this existing receipt; do not submit another operation.",
    exactApprovalRequired: "Nothing will execute until you approve this exact scope and source version.",
    noNewSuccess: "No new success is claimed. Review the recorded state before taking another action.",
    historicalReceipt: "Historical receipt only. Source content is unavailable and cannot authorize a new action.",
    expiresLabel: "expires", sourceLabel: "Source", exactAction: "Exact action to approve",
    closurePassHint: "Closure may require up to six explicitly continued checks of the pinned reference inventory. No task is submitted or closed from a partial check, and another pass never runs automatically.",
    preparedNotVerified: "Prepared, not verified", suggestedSteps: "Suggested steps", uncertainties: "Uncertainties",
    ownerNextAction: "Your next action", sourceEvidence: "Source evidence", operationReceipts: "Operation and publication receipts",
    openResultStatus: "Open result or save status", approveExact: "Approve this exact action", declineAction: "Decline",
    continueClosure: "Continue closure check", recoverClosure: "Recover last closure request",
    closureStepHint: "One explicit step only. It may submit the approved closure once all pinned evidence is checked; publication remains separately verified.",
    checkExistingRequest: "Check this existing request", reviewedResult: "I've reviewed this result",
    resultMarkedReviewed: "Result marked reviewed. The underlying task remains open.",
    acknowledgeUnavailable: "Acknowledge unavailable result", unavailableAcknowledged: "Unavailable result notice acknowledged. No private content was restored or task closed.",
    proposalReceiptIncomplete: "No complete proposal receipt was returned. Inspect Activity before starting another request.",
    sourceReceiptOnly: "Source content is unavailable; only the operation receipt remains.",
    closureSourceChangedLabel: "Source changed", closureSourceChangedText: "The approved source or pinned revision changed. This closure was not completed.",
    closureSourceClosedLabel: "Source already closed", closureSourceClosedText: "The source is already done, closed or cancelled. This receipt does not confirm that this request closed it.",
    closureLinkedPathLabel: "Reference blocked", closureLinkedPathText: "A required path is linked or is not a regular directory. The safety check blocked closure.",
    closureSourceInvalidLabel: "Reference invalid", closureSourceInvalidText: "A required reference has invalid text or metadata. The closure was not completed.",
    closurePrivacyLabel: "Privacy refusal", closurePrivacyText: "A required reference is not permitted by privacy or source-safety rules. It was not skipped; closure was blocked.",
    closureLinkRepairLabel: "Link repair required", closureLinkRepairText: "Required links could not be safely repaired. The closure was not completed.",
    closureProofLabel: "Proof incomplete", closureProofText: "Required closure proof is missing or incomplete. No completed closure is confirmed.",
    closureContextLabel: "Context limit", closureContextText: "A bounded reading or finalization limit blocked closure. The closure was not completed.",
    closureInventoryChangedLabel: "Inventory changed", closureInventoryChangedText: "The pinned reference inventory or ignore rules changed. The closure was not completed.",
    closureInventoryInvalidLabel: "Inventory invalid", closureInventoryInvalidText: "The required reference inventory could not be validated. The closure was not completed.",
    closureInventoryLimitLabel: "Inventory limit", closureInventoryLimitText: "The required reference inventory exceeds the allowed limit. The closure was not completed.",
    closureContinuationLabel: "Checkpoint conflict", closureContinuationText: "The continuation did not match this approved closure request. No completed closure is confirmed.",
    closureExhaustedLabel: "Closure check limit", closureExhaustedText: "The bounded closure checks are exhausted. The closure was not completed.",
    closureDestinationLabel: "Destination exists", closureDestinationText: "The closed-task destination already exists. This request did not overwrite it.",
    closureRequestConflictLabel: "Request conflict", closureRequestConflictText: "This operation ID already belongs to a different approved request. No new closure is confirmed.",
    closureUnconfirmedLabel: "Closure unconfirmed", closureUnconfirmedText: "The closure outcome could not be confirmed. No completed closure is confirmed.",
    closureCheckpointLabel: "Closure unconfirmed", closureCheckpointText: "The saved closure checkpoint could not be confirmed. No completed closure is confirmed.",
    closureReceiptLabel: "Closure unconfirmed", closureReceiptText: "The saved closure receipt could not be confirmed. No completed closure is confirmed.",
    closureConflictLabel: "Closure conflict", closureConflictText: "The saved receipt does not identify a specific supported reason. No completed closure is confirmed.",
    estimatedUsage: "Estimated AI usage", aiLimits: "AI limits", monthlyLimit: "Estimated monthly AI usage and operational cap",
    usageHint: "Operational estimate, not a provider bill.", usageToday: "Today", calls: "calls",
    untriaged: "Untriaged", clarify: "Clarify", backlog: "Backlog",
    today: "Today", flow: "Flow", copilot: "Copilot", capture: "+ Capture",
    refresh: "Refresh", signOut: "Sign out", knowledgeNav: "Knowledge", areasNav: "Areas & projects", activityNav: "Activity",
    captureTitle: "Get it out of your head", closeCapture: "Close capture", captureQuestion: "What do you need to do?",
    captureDetails: "Add the details you already know", reviewCapture: "Review capture",
    needs: "Needs you", allTasks: "All tasks", list: "List", search: "Search loaded tasks",
    searchPlaceholder: "Find an outcome or next action", stage: "Stage", area: "Area",
    back: "Back to tasks", overviewTab: "Overview", editTab: "Edit & plan", aiTab: "AI help", closeTab: "Close",
    chatTab: "Chat",
    knowledgeDescription: "Understand the finding. Check its evidence. Choose what happens next.",
    needsDescription: "Decisions, due follow-ups and the work you selected.",
    areasDescription: "Keep responsibilities visible. Choose which outcomes to advance.",
    activityDescription: "Proposals, preparation and the evidence of follow-through.",
    personal: "PERSONAL INTELLIGENCE", hero: "Make space for what matters.",
    heroDetail: "A clear outcome. A useful next step. A copilot to get you there.",
    briefing: "Plan my day ↗", work: "Your work, in motion", board: "Flow board",
    todayDescription: "Your focus, the next move, and intelligence to move it forward.",
    flowDescription: "From a rough idea to a verified outcome. One flow, with AI alongside.",
    inbox: "Inbox / Clarify", ready: "Ready", doing: "Doing", waiting: "Waiting", verify: "Verify", done: "Done",
    outcome: "OUTCOME", doneWhen: "DONE WHEN", notDefined: "Not defined yet. Let's make it concrete.",
    nextAction: "Next action", taskDetails: "Dates, execution & source details",
    taskCopilot: "Task copilot", welcome: "What shall we move forward?",
    welcomeDetail: "Describe an outcome, untangle a task, or ask for a next step. English or Russian, text or voice.",
    taskWelcome: "Let's make this task easier.",
    taskWelcomeDetail: "Ask me to sharpen the outcome, estimate the work, build a plan, or prepare the next action.",
    placeholder: "Ask, capture, or give an instruction…", send: "Send ↗", you: "YOU", ai: "MINDME",
    thinking: "Working with your canonical sources…", speak: "Read replies",
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
    skipWorkspace: "К рабочему пространству", vaultLabel: "Личное хранилище", workspaceLabel: "Рабочее пространство", workspaceViews: "Разделы рабочего пространства",
    accessThink: "Место для мысли.", accessAct: "Простор для дела.",
    accessIntro: "Ваши задачи, идеи и источники. Одно личное пространство, чтобы двигаться вперёд.",
    signIn: "Войти с Microsoft", tryAgain: "Попробовать снова",
    ownerOnly: "Только для настроенного личного владельца. Задачи остаются в вашем хранилище; это интерфейс для просмотра и разрешённой работы с ними.",
    artCaption: "От возможности к следующему шагу.", artFoot: "Немного структуры. Больше возможностей.",
    checkingWorkspace: "Проверяем ваше личное пространство. Личные данные не сохраняются в браузере.",
    checkingSignIn: "Проверяем вход", signInPrompt: "Войдите с настроенной личной учётной записью Microsoft, чтобы увидеть свои задачи.",
    sessionExpired: "Войдите снова с личной учётной записью. Проверьте историю, прежде чем повторять неподтверждённый запрос.",
    signedOutUnconfirmed: "Вы вышли. Исход одного запроса неизвестен; после входа проверьте историю, прежде чем создавать новый.",
    signOutBusy: "Ответ ещё ожидается. Выйдите после получения ответа или сообщения о тайм-ауте.",
    signOutUnconfirmed: "Выход не подтверждён. Попробуйте снова, прежде чем оставлять общее устройство.",
    personalWorkspace: "Личное пространство", signedOut: "Вход не выполнен", accessUnavailable: "Доступ недоступен", workingRequest: "Обрабатываем ваш запрос",
    openCopilot: "Открыть копилот", closeCopilot: "Закрыть копилот",
    useDark: "Включить тёмную тему", useLight: "Включить светлую тему", useRussian: "Переключить на русский", useEnglish: "Switch to English",
    grounded: "Опора на ваше хранилище.", groundedDetail: "ИИ помогает двигаться. Каноническая запись остаётся источником истины.",
    footer: "Личные дела с опорой на ваше хранилище.", footerNote: "Ясность в мыслях. Осознанность в действиях.",
    nextMoves: "Следующие шаги", loadedPriorities: "Из загруженных задач", approvedFocus: "Утверждённый фокус",
    workToLookAt: "Требует внимания", newSince: "Новое с прошлого визита",
    knowledgeInbox: "Входящие", knowledgeHint: "Сначала вывод. Доказательства рядом.",
    knowledgeIntro: "Канонические ежедневные обзоры и недельные дайджесты. Интерпретации и возможные шаги не являются разрешённой работой.",
    earlierReviews: "Загрузить ранние обзоры", evidenceReader: "Просмотр доказательств", selectedTask: "Выбранная задача",
    captureIntro: "Достаточно чернового запроса. Недостающие детали можно уточнить позже. Чувствительные сведения оставляйте в личной записи.",
    capturePlaceholder: "Опишите желаемый результат или вопрос, который хотите решить.",
    optionalTitle: "Название (необязательно)", titlePlaceholder: "Оставьте пустым, чтобы использовать первую строку запроса",
    captureHint: "«Добавить задачу» сохраняет весь запрос как одну задачу для уточнения. Это не разрешает покупки, исследования или запуск агента.",
    sourceCaptureSaveHint: "Сначала проверьте запись, связанную с источником. Ввод задачи не запускает исследование или агента.",
    addTask: "Добавить задачу", captureResult: "Запись задачи", captureRecordDetails: "Техническая запись задачи",
    captureSubmittedLabel: "Отправлено", captureSavingLabel: "Сохраняется",
    captureSaving: "Сохраняем задачу. Сохранение пока не подтверждено.",
    captureSubmitted: "Запрос получен. Проверяем, что задача сохранилась; добавлять её повторно не нужно.",
    captureUnconfirmed: "Сохранение не подтверждено. Задача уже могла сохраниться; проверьте этот же запрос, не создавайте новый.",
    captureNotSaved: "Этот запрос не подтвердил сохранение задачи. Проверьте его запись, прежде чем пробовать снова.",
    captureSaved: "Задача сохранена", captureSavedDetail: "Добавлено в ваш список задач: «{stage}». Работа не запускалась.",
    checkCapture: "Проверить сохранение", captureTextRequired: "Опишите задачу перед добавлением.",
    reviewBefore: "Проверьте перед действием", returnWorkspace: "Вернуться к работе",
    responsibilities: "Обязанности и выбранные проекты",
    areasIntro: "Сферы — постоянные обязанности. У проекта есть результат; запись в списке не означает активного обязательства.",
    selectProjects: "Выбрать проекты для планирования",
    projectsHint: "Выберите до восьми существующих проектов. Изменение выбора отключает постоянное разрешение ИИ до повторной проверки.",
    moreProjects: "Загрузить ещё проекты", saveProjects: "Сохранить выбор проектов", clearProjects: "Снять выбор проектов",
    standingTitle: "Подготовка с ИИ в ясных границах",
    standingIntro: "Можно разрешить личную подготовку черновиков для трёх полностью определённых задач на этапе «Готово к работе» в выбранных проектах. Изменение источника отменяет этот объём разрешения.",
    standingEnable: "Включить постоянное разрешение на подготовку выбранных ниже задач",
    standingHint: "Это не разрешает публичные исследования, новые обязательства, сообщения, покупки или подтверждённое завершение.",
    saveStanding: "Сохранить разрешение", stopStanding: "Остановить подготовку",
    preparationLimits: "Лимиты и использование подготовки",
    limitsHint: "Это рабочие лимиты, а не гарантия счёта провайдера. Неподтверждённая работа не считается возвратом средств.",
    decisionsResults: "Решения и результаты",
    activityIntro: "Проследите тот же запрос от предложения до канонической записи. Подготовленный черновик не является проверенным результатом.",
    moreActivity: "Загрузить ещё историю", moreTasks: "Загрузить ещё задачи", moreInformation: "Подробнее",
    detailEmptyTitle: "Следующий шаг, а не ещё один список.",
    detailEmptyDetail: "Откройте задачу, чтобы уточнить результат, спланировать следующий шаг или проверить, что может подготовить ИИ.",
    areaUnclear: "Сфера не уточнена", clarifyNext: "Уточните следующий шаг.", selectedToday: "Выбрано на сегодня",
    deadlineLabel: "Крайний срок", reviewLabel: "Пересмотр", deadlineApproaching: "Срок приближается",
    deadlineDue: "Наступил срок", reviewDue: "Пора пересмотреть",
    waitingDependency: "Ожидание зависимости", definitionNeedsWork: "Нужно уточнить описание",
    minutes: "мин", loadingTasks: "Загружаем задачи", noLoadedTasks: "Нет загруженных задач", loadingCanonical: "Загружаем каноническую задачу...",
    allStages: "Все этапы", allAreas: "Все сферы",
    refreshFailed: "Обновление задачи не подтверждено. Проверьте состояние источника, прежде чем продолжать.",
    sourcesUnavailable: "Ваши источники пока недоступны.", sourcesUnavailableDetail: "Обновите их. Недоступность не означает пустой список задач.",
    attentionIncomplete: "Проверка внимания неполная.", attentionIncompleteDetail: "Остались другие источники задач или ошибки чтения. Загрузите ещё или устраните проблему, прежде чем считать, что ничего не требует внимания.",
    sourceScope: "Некоторые источники не входят в этот обзор",
    sourceScopeExplanation: "Это связано с правилами отбора или конфиденциальности, а не с ошибками загрузки. Названия и содержимое скрыты.",
    sourceTaskExclusions: "Исключённые источники задач в проверенной части списка: {count}.",
    sourceProjectExclusions: "Исключённые источники проектов среди уже проверенных: {count}.",
    sourceAttentionPartial: "Проверка внимания неполная; остались непроверенные источники или неразрешённые проблемы.",
    sourceAttentionAssessedPartial: "Оценено разрешённых описаний задач в этой ограниченной части списка: {count}. Проверка внимания неполная; остались непроверенные источники или неразрешённые проблемы.",
    sourceTaskIssues: "Проблемы с источниками задач, требующие внимания: {count}. Недоступные источники не считаются пустыми или полностью проверенными.",
    sourceProjectIssues: "Проблемы с источниками проектов: {count}. Список разрешённых проектов неполный; обновите его, чтобы повторить попытку.",
    sourceMetadataMissing: "У задачи нет поддерживаемого блока метаданных. Не удалось оценить её даты.",
    sourceMetadataInvalid: "Метаданные источника некорректны или противоречивы. Исправьте источник, прежде чем полагаться на его даты.",
    sourceDefinitionInvalid: "Одно из полей описания задачи некорректно. Неизвестно, требует ли она внимания.",
    sourceDateInvalid: "У задачи некорректная дата. Исправьте сохранённую дату, прежде чем полагаться на этот обзор.",
    sourceTitleMissing: "У задачи нет поддерживаемого заголовка. Не удалось оценить её описание.",
    sourceReviewAmbiguous: "У задачи противоречивые даты пересмотра. Согласуйте их в источнике.",
    sourceTooLarge: "Источник превышает допустимый размер для чтения и не был оценён.",
    sourcePolicyUnresolved: "Правила отбора или конфиденциальности в метаданных источника неоднозначны. Он остаётся недоступным до их уточнения.",
    sourceUnavailable: "Не удалось прочитать или проверить источник. Обновите обзор, чтобы повторить попытку с его текущей версией.",
    sourceIssueUnknown: "Не удалось оценить источник. Его отсутствие не означает, что нет дел, требующих внимания.",
    noMatchingAttention: "На этой странице нет подходящих задач со сроком или выбранным фокусом.", noMatchingTasks: "На этой странице нет подходящих задач.",
    noMatchingDetail: "Проверьте фильтры, загрузите ещё задачи или запишите новый запрос. Ничего не завершено и не отброшено.",
    loadedTasks: "Загружено задач", loadedOnly: "Фильтры и счётчики относятся только к загруженным разрешённым задачам.", availabilityUnconfirmed: "Доступность источников не подтверждена.",
    reviewSnapshot: "Канонический снимок обзоров", reviewSnapshotSeparate: "Источники обзоров проверяются отдельно от задач",
    taskDay: "День задач", snapshotChecked: "Снимок проверен",
    historyUnavailable: "История недоступна.", historyUnavailableDetail: "Обновите, прежде чем считать, что работа не выполнялась.",
    activityEmpty: "На загруженной странице нет операций.", activityEmptyDetail: "Запишите задачу или подготовьте действие. Его предложения и квитанции появятся здесь.",
    loaded: "загружено", noProjects: "Нет разрешённых проектов.", noProjectsDetail: "Это не разрешает автоматически создавать или активировать проект.",
    projectsLoaded: "Загружено разрешённых проектов", projectsExcluded: "Проверенных записей исключено правилами доступа и приватности",
    moreCandidates: "Остались другие записи; загрузите ещё, чтобы проверить разрешённые проекты.",
    unseenProjects: "Сохранено выбранных проектов вне этой страницы", reviewUnseenProjects: "Загрузите ещё, чтобы проверить их, или явно снимите выбор.",
    noEligibleTasks: "Нет подходящих загруженных задач.", noEligibleTasksDetail: "Выберите проект и явно переведите полностью определённую задачу в готовые к работе. При необходимости загрузите ещё задачи.",
    manualDraftInvalidated: "Несохранённые ручные правки удалены: задача изменилась или больше недоступна. Проверьте текущий источник перед новыми правками.",
    titleField: "Название", project: "Проект", executionMode: "Режим выполнения", outcomeField: "Результат",
    doneWhenField: "Критерий готовности", waitingFor: "Ожидание", reviewOn: "Дата пересмотра", focusOn: "Дата фокуса",
    chooseArea: "Выберите сферу", noProjectSelected: "Проект не выбран", keepUntriaged: "Оставить неразобранной",
    notDecided: "Не решено", executionHuman: "Я", executionAssisted: "Я, с помощью ИИ", executionAgent: "Агент (нужно разрешение)",
    execution_human: "Я", execution_assisted: "Я, с помощью ИИ", execution_agent: "Агент (нужно разрешение)",
    whoActs: "Кто выполнит следующий шаг?", desiredOutcome: "Желаемый результат",
    doneWhenHint: "Наблюдаемый результат, который можно проверить.",
    clarifyRecord: "Уточнить запись", definitionHint: "Проверьте точные правки перед сохранением. Выбор этапа или исполнителя не запускает агента.",
    waitingHint: "Укажите зависимость, а не чувствительные личные сведения. Очистите поле только после устранения препятствия.",
    reviewOnHint: "Назначьте дату пересмотра, переводя начатую работу в ожидание.",
    reviewDefinition: "Проверить правки описания", noDefinitionChanges: "Нет правок описания для проверки.",
    fieldCannotClear: "Поле «{field}» нельзя очистить через эту форму. Укажите замену или оставьте без изменений.",
    chooseWaitingStage: "Выберите этап задачи при изменении условий ожидания или используйте форму одной даты ниже.",
    changeOneDate: "Осознанно изменить одну дату", dateType: "Тип даты", dateField: "Дата",
    nextReview: "Следующий пересмотр", selectedFocusDate: "Выбранная дата фокуса", realDeadline: "Реальный крайний срок",
    removeDate: "Явно удалить эту дату", reviewDateChange: "Проверить изменение даты",
    dateHint: "Пересмотр не является крайним сроком. Перенос пересмотра не меняет крайний срок. Фокус дня не переносится автоматически.",
    chooseDate: "Выберите дату или явно отметьте её удаление.",
    preparationTitle: "Упростить следующий шаг",
    preparationIntro: "Выберите ограниченное поручение и проверьте его точный объём. Результат ИИ остаётся черновиком до вашей проверки.",
    preparationEligibility: "Для подготовки нужно полное описание и выбранный владельцем этап «Готово к работе», «В работе» или «Проверить».",
    workToPrepare: "Что подготовить", privateDraft: "Личный черновик по этой задаче", publicResearch: "Ограниченное публичное исследование",
    exactScope: "Точный вопрос или объём подготовки", reviewAssignment: "Проверить поручение ИИ",
    privatePreparationLimits: "Личная подготовка: один вызов модели, без внешних инструментов, не более 45 секунд. Действие в реальном мире не выполняется.",
    publicResearchLimits: "Один обезличенный публичный вопрос, без личного контекста. Не более пяти источников, один короткий отчёт, без последующих заданий. Расходы провайдера могут учитываться отдельно.",
    closureTitle: "Завершить результат, а не только запуск",
    closureIntro: "Черновика, отправленного запроса или завершённого запуска агента недостаточно. Запишите, что произошло и чем это подтверждено.",
    closureNeedsCondition: "Перед завершением добавьте конкретный критерий готовности.",
    resultAchieved: "Что удалось сделать?", evidenceField: "Доказательства или проверка", verifiedOn: "Дата проверки результата",
    evidenceHint: "Опишите разрешённые доказательства или сошлитесь на них. Личные сведения оставьте в отдельной записи.",
    verifiedOnHint: "Используется настроенный календарь задач. Это дата проверки, а не предполагаемая дата выполнения работы.",
    learningField: "Полезный урок", learningHint: "Необязательно. Что вам или агенту стоит сделать иначе в следующий раз?",
    ownerAttestation: "Я проверил критерий готовности по фактическому результату и доказательствам.", reviewClosure: "Проверить подтверждённое завершение",
    openCanonical: "Открыть канонический источник", revision: "Версия", taskActions: "Действия с задачей", notRecorded: "Не записано",
    taskAttentionFilter: "Фильтр внимания к задачам", taskLayout: "Вид списка задач",
    needsClarification: "Нужно уточнить: {fields}. Это проверки структуры, а не разрешение действовать.",
    clarifyTask: "Уточнить эту задачу", readFullSource: "Прочитать полный источник",
    clarificationRecorded: "Уточнение записано отдельно. Описание задачи не изменено.",
    oneQuestion: "По одному вопросу", answersUsed: "Для этой версии источника использовано ответов: {count} из 3. Нерешённая работа может остаться на этапе уточнения.",
    answerBindingMissing: "У вопроса нет действительной привязки ответа. Обновите задачу и начните уточнение заново.",
    yourAnswer: "Ваш ответ", recordAnswer: "Записать ответ", proposedChanges: "Предлагаемые правки", reviewRefinement: "Проверить это уточнение",
    clarificationNext: "Проверьте предлагаемое уточнение или продолжите редактировать задачу вручную. Дополнительный вопрос не обязателен.",
    sourceCaptureRequest: "Проверьте «{title}» и выберите конкретный следующий шаг.", sourceCaptureFrom: "Из источника: ",
    sourceCaptureHint: "Каноническое происхождение и точные доказательства сохранятся и будут проверены перед подтверждением.",
    unloadedStanding: "Ранее разрешённых задач, недоступных для выбора на загруженной странице: {count}. Сохранение заменит выбор отмеченными здесь задачами.",
    projectLimit: "Выберите не более восьми проектов.",
    projectsSaved: "Выбор проектов сохранён. Постоянная подготовка отключена до повторного подтверждения её объёма.",
    projectsCleared: "Выбор проектов снят. Постоянная подготовка отключена; исходные проекты и задачи не удалены.",
    standingLimit: "Выберите от одной до трёх подходящих задач перед включением подготовки.",
    standingSourceMissing: "Выбранный источник больше не загружен. Обновите его перед выдачей разрешения.",
    standingSaved: "Разрешение сохранено для этих точных версий источников. Действия в реальном мире не разрешены.",
    standingOff: "Постоянная подготовка отключена.", standingStopped: "Постоянная подготовка отключена. Существующие результаты и квитанции сохранены.",
    workingExactRequest: "Выполняется этот точный запрос. Дождитесь его квитанции.",
    requestUnconfirmed: "Этот запрос не подтверждён.",
    requestUnconfirmedHint: "Он уже мог дойти до сервера. Не закрывайте страницу: проверьте историю или восстановите этот точный запрос. Не создавайте замену.",
    recoverSameRequest: "Восстановить тот же запрос",
    captureOperation: "Записать задачу", refineOperation: "Уточнить описание", changeOperation: "Изменить задачу",
    closeOperation: "Проверить и завершить", prepareOperation: "Подготовить личный черновик", researchOperation: "Исследовать публичный вопрос", taskOperation: "Операция с задачей",
    freshClosureApproval: "Для новой попытки нужны новое предложение и подтверждение.",
    closureCheckpointConfirmed: "Контрольная точка завершения подтверждена (проверок: {count} из 6). Задача открыта; завершение не отправлено. Продолжайте только для следующего ограниченного шага.",
    closureCheckUnconfirmed: "Последняя проверка завершения не подтверждена. Восстановите тот же запрос, чтобы проверить ход работы. Не начинайте новое завершение и не считайте задачу сохранённой.",
    privateDraftAvailable: "Личный черновик доступен. Задача остаётся открытой.",
    researchResultAvailable: "У исследования есть результат. Проверьте материал; сама задача не завершена.",
    closureSaved: "Подтверждённое завершение внесено в каноническую запись.", operationSaved: "Сохранено в канонической записи. Для завершения задачи по-прежнему нужны доказательства.",
    publicationSubmitted: "Отправлено на публикацию. Наличие в канонической ветке ещё не подтверждено.",
    operationUnconfirmed: "Результат не подтверждён. Проверьте эту квитанцию, не отправляйте новую операцию.",
    exactApprovalRequired: "Ничего не будет выполнено без вашего подтверждения точного объёма и версии источника.",
    noNewSuccess: "Новый успех не заявлен. Проверьте записанное состояние перед следующим действием.",
    historicalReceipt: "Только историческая квитанция. Содержимое источника недоступно и не даёт разрешения на новое действие.",
    expiresLabel: "истекает", sourceLabel: "Источник", exactAction: "Точное действие для подтверждения",
    closurePassHint: "Для завершения может потребоваться до шести явно продолженных проверок закреплённых источников. Частичная проверка не отправляет и не закрывает задачу. Следующий проход не запускается автоматически.",
    preparedNotVerified: "Подготовлено, но не проверено", suggestedSteps: "Предлагаемые шаги", uncertainties: "Неопределённости",
    ownerNextAction: "Ваш следующий шаг", sourceEvidence: "Доказательства из источника", operationReceipts: "Квитанции операции и публикации",
    openResultStatus: "Открыть результат или статус сохранения", approveExact: "Подтвердить это точное действие", declineAction: "Отклонить",
    continueClosure: "Продолжить проверку завершения", recoverClosure: "Восстановить последний запрос завершения",
    closureStepHint: "Только один явный шаг. После проверки всех закреплённых доказательств он может отправить подтверждённое завершение. Публикация проверяется отдельно.",
    checkExistingRequest: "Проверить существующий запрос", reviewedResult: "Я проверил этот результат",
    resultMarkedReviewed: "Результат отмечен проверенным. Сама задача остаётся открытой.",
    acknowledgeUnavailable: "Подтвердить недоступность результата", unavailableAcknowledged: "Уведомление о недоступном результате подтверждено. Личные данные не восстановлены, задача не закрыта.",
    proposalReceiptIncomplete: "Полная квитанция предложения не получена. Проверьте историю перед новым запросом.",
    sourceReceiptOnly: "Содержимое источника недоступно; осталась только квитанция операции.",
    closureSourceChangedLabel: "Источник изменился", closureSourceChangedText: "Утверждённый источник или закреплённая версия изменились. Завершение не выполнено.",
    closureSourceClosedLabel: "Источник уже закрыт", closureSourceClosedText: "Источник уже выполнен, закрыт или отменён. Эта квитанция не подтверждает, что его закрыл данный запрос.",
    closureLinkedPathLabel: "Ссылка заблокирована", closureLinkedPathText: "Обязательный путь является ссылкой или не является обычным каталогом. Проверка безопасности заблокировала завершение.",
    closureSourceInvalidLabel: "Источник некорректен", closureSourceInvalidText: "У обязательного источника некорректный текст или метаданные. Завершение не выполнено.",
    closurePrivacyLabel: "Отказ по приватности", closurePrivacyText: "Обязательный источник запрещён правилами приватности или безопасности. Он не пропущен; завершение заблокировано.",
    closureLinkRepairLabel: "Нужно исправить ссылки", closureLinkRepairText: "Обязательные ссылки не удалось безопасно исправить. Завершение не выполнено.",
    closureProofLabel: "Доказательства неполные", closureProofText: "Подтверждение завершения отсутствует или неполное. Успешное завершение не подтверждено.",
    closureContextLabel: "Лимит контекста", closureContextText: "Лимит чтения или финализации заблокировал завершение. Задача не завершена.",
    closureInventoryChangedLabel: "Список источников изменился", closureInventoryChangedText: "Закреплённый список источников или правила исключения изменились. Завершение не выполнено.",
    closureInventoryInvalidLabel: "Список источников некорректен", closureInventoryInvalidText: "Обязательный список источников не прошёл проверку. Завершение не выполнено.",
    closureInventoryLimitLabel: "Лимит источников", closureInventoryLimitText: "Обязательный список источников превышает допустимый лимит. Завершение не выполнено.",
    closureContinuationLabel: "Конфликт контрольной точки", closureContinuationText: "Продолжение не соответствует подтверждённому запросу завершения. Успешное завершение не подтверждено.",
    closureExhaustedLabel: "Лимит проверок завершения", closureExhaustedText: "Лимит ограниченных проверок завершения исчерпан. Завершение не выполнено.",
    closureDestinationLabel: "Назначение уже существует", closureDestinationText: "Запись закрытой задачи уже существует в месте назначения. Этот запрос её не перезаписал.",
    closureRequestConflictLabel: "Конфликт запроса", closureRequestConflictText: "Идентификатор операции уже принадлежит другому подтверждённому запросу. Новое завершение не подтверждено.",
    closureUnconfirmedLabel: "Завершение не подтверждено", closureUnconfirmedText: "Не удалось подтвердить исход завершения. Успешное завершение не подтверждено.",
    closureCheckpointLabel: "Завершение не подтверждено", closureCheckpointText: "Не удалось подтвердить сохранённую контрольную точку. Успешное завершение не подтверждено.",
    closureReceiptLabel: "Завершение не подтверждено", closureReceiptText: "Не удалось подтвердить сохранённую квитанцию. Успешное завершение не подтверждено.",
    closureConflictLabel: "Конфликт завершения", closureConflictText: "Сохранённая квитанция не указывает конкретную поддерживаемую причину. Успешное завершение не подтверждено.",
    estimatedUsage: "Оценка использования ИИ", aiLimits: "Лимиты ИИ", monthlyLimit: "Оценка использования ИИ за месяц и рабочий лимит",
    usageHint: "Рабочая оценка, не счёт провайдера.", usageToday: "Сегодня", calls: "вызовов",
    untriaged: "Не разобрано", clarify: "Уточнить", backlog: "Запас",
    pending: "Нужно подтверждение", submitted: "Ожидает публикации", executing: "В процессе",
    uncertain: "Не подтверждено", completed: "Проверенная операция", invalidated: "Источник изменился",
    failed: "Не выполнено", declined: "Отклонено", expired: "Истекло", merged: "Каноническая запись",
    today: "Сегодня", flow: "Поток", copilot: "Копилот", capture: "+ Задача",
    refresh: "Обновить", signOut: "Выйти", knowledgeNav: "Знания", areasNav: "Сферы и проекты", activityNav: "История",
    captureTitle: "Освободите голову", closeCapture: "Закрыть ввод", captureQuestion: "Что нужно сделать?",
    captureDetails: "Добавить известные детали", reviewCapture: "Проверить запись",
    needs: "Требует вас", allTasks: "Все задачи", list: "Список", search: "Поиск среди загруженных задач",
    searchPlaceholder: "Найти результат или следующий шаг", stage: "Этап", area: "Сфера",
    back: "К задачам", overviewTab: "Обзор", editTab: "Правки и план", aiTab: "Помощь ИИ", closeTab: "Завершение",
    chatTab: "Чат",
    knowledgeDescription: "Разберите вывод. Проверьте доказательства. Выберите следующий шаг.",
    needsDescription: "Решения, сроки пересмотра и выбранная вами работа.",
    areasDescription: "Держите обязанности в поле зрения. Выберите результат для продвижения.",
    activityDescription: "Предложения, подготовка и подтверждения результата.",
    personal: "ЛИЧНЫЙ ИНТЕЛЛЕКТ", hero: "Освободите место для важного.",
    heroDetail: "Ясный результат. Полезный следующий шаг. Копилот, который поможет.",
    briefing: "Спланировать день ↗", work: "Ваши задачи в движении", board: "Доска",
    todayDescription: "Ваш фокус, следующий шаг и помощь ИИ в работе.",
    flowDescription: "От идеи до проверенного результата. Один поток, вместе с ИИ.",
    inbox: "Входящие / Уточнить", ready: "Готово к работе", doing: "В работе", waiting: "Ожидание", verify: "Проверить", done: "Завершено",
    outcome: "РЕЗУЛЬТАТ", doneWhen: "КРИТЕРИЙ ГОТОВНОСТИ", notDefined: "Пока не определено. Давайте уточним.",
    nextAction: "Следующий шаг", taskDetails: "Даты, выполнение и источник",
    taskCopilot: "Копилот задачи", welcome: "Что будем продвигать?",
    welcomeDetail: "Опишите результат, разберите задачу или попросите следующий шаг. Текстом или голосом, по-русски или по-английски.",
    taskWelcome: "Сделаем эту задачу проще.",
    taskWelcomeDetail: "Попросите уточнить результат, оценить работу, составить план или подготовить следующий шаг.",
    placeholder: "Вопрос, идея или поручение…", send: "Отправить ↗", you: "ВЫ", ai: "MINDME",
    thinking: "Работаю с каноническими источниками…", speak: "Читать ответы",
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
function t(key, values = {}) {
  return (COPY[assist.language]?.[key] || COPY.en[key] || key)
    .replace(/\{(\w+)\}/g, (match, name) => Object.hasOwn(values, name) ? String(values[name]) : match);
}
let activeViewTransition = null;
function transitionView(view) {
  activeViewTransition?.skipTransition();
  if (document.startViewTransition && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
    const transition = document.startViewTransition(() => setView(view, { arrival: true }));
    activeViewTransition = transition;
    document.documentElement.dataset.transitioning = "true";
    transition.finished.catch(() => {}).finally(() => {
      if (activeViewTransition !== transition) return;
      activeViewTransition = null;
      delete document.documentElement.dataset.transitioning;
    });
  } else setView(view, { arrival: true });
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
  const header = document.querySelector(".app-header");
  const quickBar = $("quick-bar");
  const measureHeader = () => document.documentElement.style.setProperty("--header-height", `${Math.ceil(header.getBoundingClientRect().height)}px`);
  const measureQuickBar = () => document.documentElement.style.setProperty("--quick-bar-height", `${Math.ceil(quickBar.getBoundingClientRect().height)}px`);
  measureHeader();
  measureQuickBar();
  if (typeof ResizeObserver !== "undefined") {
    new ResizeObserver(measureHeader).observe(header);
    new ResizeObserver(measureQuickBar).observe(quickBar);
  }
  $("command-shortcut").textContent = /Mac|iPhone|iPad/.test(navigator.platform) ? "⌘ K" : "Ctrl K";
  $("demo-banner").hidden = !isMockMode();
  $("demo-banner").dataset.i18n = "mock";
  $("theme-toggle").addEventListener("click", () => {
    const dark = document.documentElement.dataset.theme !== "light";
    document.documentElement.dataset.theme = dark ? "light" : "dark";
    $("theme-toggle").setAttribute("aria-label", t(dark ? "useDark" : "useLight"));
  });
  let languageContext = null;
  $("language-toggle").addEventListener("pointerdown", () => { languageContext = captureDetailContext(); });
  $("language-toggle").addEventListener("click", () => {
    const context = languageContext;
    languageContext = null;
    const page = { left: window.scrollX, top: window.scrollY, behavior: "instant" };
    assist.language = assist.language === "en" ? "ru" : "en";
    translateWorkspace(); setView(state.view, { load: false });
    if (state.selected?.revision) renderDetail();
    if (dashboard.inbox) dashboard.renderInbox();
    if (dashboard.reader) dashboard.renderReader();
    if (assist.usage) renderAssistUsage();
    renderChats();
    if (context?.focused && state.selected?.path === context.path && state.selected?.revision === context.revision
      && ["tasks", "needs"].includes(state.view)) restoreDetailContext(context);
    else window.scrollTo(page);
  });
  for (const id of ["command-open", "nav-copilot", "quick-chat"]) $(id).addEventListener("click", () => openCopilot());
  $("copilot-close").addEventListener("click", () => $("copilot-dialog").close());
  $("quick-capture").addEventListener("click", () => $("capture-open").click());
  $("briefing-open").addEventListener("click", () => { openCopilot(); sendChat("global", t("dayPrompt")); });
  for (const id of ["quick-mic", "header-mic"]) $(id).addEventListener("click", () => {
    const key = assist.voiceKey || (state.selected?.revision && ["tasks", "needs"].includes(state.view) ? state.selected.path : "global");
    if (key === "global") openCopilot();
    toggleVoice(key);
  });
  $("copilot-dialog").addEventListener("close", () => {
    if (assist.voiceKey === "global" || assist.acquiringVoice) { cancelVoice(); renderChats(); }
    $("copilot-notice").textContent = ""; $("copilot-notice").hidden = true;
    restoreFocus(focusOrigins.copilot, matchMedia("(max-width: 1023px)").matches ? $("quick-chat") : $("command-open"));
    focusOrigins.copilot = null;
  });
  document.querySelector(".navigation").addEventListener("keydown", (event) => {
    const keys = ["ArrowDown", "ArrowUp", "ArrowRight", "ArrowLeft", "Home", "End"];
    if (!keys.includes(event.key) || !event.target.matches("[data-view]")) return;
    const views = Array.from(document.querySelectorAll("[data-view]"));
    const index = views.indexOf(event.target);
    const next = event.key === "Home" ? 0 : event.key === "End" ? views.length - 1
      : (index + (["ArrowDown", "ArrowRight"].includes(event.key) ? 1 : -1) + views.length) % views.length;
    event.preventDefault(); views[next].focus();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("copilot-dialog").open) {
      if (!$("capture-section").hidden && $("capture-section").contains(document.activeElement)) closeCapture();
      else if (state.selected && ["tasks", "needs"].includes(state.view)) closeDetail();
      else if (state.view === "knowledge" && $("knowledge-section").classList.contains("knowledge-selected")) dashboard.closeReader(true);
    }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k" && state.csrf && assist.available) {
      event.preventDefault(); openCopilot();
    }
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) watchCaptureReceipt();
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
  document.querySelectorAll("[data-i18n-aria]").forEach((node) => { node.setAttribute("aria-label", t(node.dataset.i18nAria)); });
  $("language-toggle").setAttribute("aria-label", t(assist.language === "en" ? "useRussian" : "useEnglish"));
  $("theme-toggle").setAttribute("aria-label", t(document.documentElement.dataset.theme === "light" ? "useDark" : "useLight"));
  if ($("access-message").dataset.statusKey) setAccessMessage($("access-message").dataset.statusKey);
  for (const option of $("stage-filter").options) option.textContent = option.value ? t(option.value) : t("allStages");
  if ($("area-filter").options[0]) $("area-filter").options[0].textContent = t("allAreas");
  for (const id of ["quick-mic", "header-mic"]) $(id).setAttribute("aria-label", t(assist.recorder ? "stop" : "record"));
  renderSourceWarnings();
  if (state.proposal) {
    $("proposal-preview").replaceChildren(renderProposal(state.proposal));
  }
}
function renderAssistUsage() {
  const usage = assist.usage;
  if (!usage) return;
  delete $("assist-usage").dataset.i18n;
  $("assist-usage").replaceChildren(element("span", `${t("estimatedUsage")} · €${usage.month_eur.toFixed(2)} / €${usage.month_cap_eur.toFixed(2)}`));
  const meter = element("meter", null, "usage-meter");
  meter.min = 0; meter.max = Math.max(usage.month_cap_eur, 0.01); meter.value = usage.month_eur;
  meter.setAttribute("aria-label", t("monthlyLimit"));
  const details = element("details");
  append(details, element("summary", t("aiLimits")),
    element("p", `${t("usageToday")} · €${usage.today_eur} / €${usage.today_cap_eur} · ${usage.calls_today} ${t("calls")}`),
    element("p", Object.entries(usage.models || {}).map(([role, model]) => `${role}: ${model}`).join(" · ")),
    element("p", t("usageHint")));
  $("assist-usage").append(meter, details);
}
async function loadAssist() {
  const generation = state.sessionGeneration;
  try {
    const usage = await api("assist/usage");
    if (generation !== state.sessionGeneration) return;
    if (!Number.isFinite(usage.month_eur) || !Number.isFinite(usage.month_cap_eur)) throw new Error(t("unavailable"));
    assist.available = true;
    assist.usage = usage;
    renderAssistUsage();
  } catch (error) {
    if (generation !== state.sessionGeneration) return;
    assist.available = false; assist.usage = null;
    $("assist-usage").dataset.i18n = "unavailable";
    $("assist-usage").textContent = t("unavailable");
    if (error.status !== 404) showNotice(errorMessage(error), "error");
  }
  document.querySelectorAll(".ai-feature").forEach((node) => { node.hidden = !assist.available; });
  if (state.selected?.revision) renderDetail();
}
function clearAssist() {
  cancelVoice();
  assist.available = false; assist.usage = null; assist.threads.clear(); assist.dragPath = null;
  clearTimeout(assist.toastTimer);
  for (const poll of assist.polls.values()) clearTimeout(poll.timer);
  assist.polls.clear();
  if (typeof window.speechSynthesis !== "undefined") window.speechSynthesis.cancel();
  $("copilot-dialog").close?.();
  $("global-chat").replaceChildren();
  $("today-priorities").replaceChildren();
  $("toast").replaceChildren(); $("toast").hidden = true;
  $("assist-usage").replaceChildren();
  delete $("assist-usage").dataset.i18n;
  $("quick-bar").hidden = true;
  document.querySelectorAll(".ai-feature").forEach((node) => { node.hidden = true; });
}
function openCopilot() {
  if (!assist.available || !state.csrf) return;
  if (!$("copilot-dialog").open) focusOrigins.copilot = document.activeElement;
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
  for (const id of ["quick-mic", "header-mic"]) {
    $(id).setAttribute("aria-pressed", String(Boolean(assist.recorder)));
    $(id).setAttribute("aria-label", t(assist.recorder ? "stop" : "record"));
  }
  if (!assist.available) return;
  document.querySelectorAll("[data-chat-key]").forEach((host) => renderChat(host, host.dataset.chatKey));
}
function renderChat(host, key) {
  const thread = threadFor(key);
  const active = host.querySelector("textarea") === document.activeElement;
  const start = active ? document.activeElement.selectionStart : null;
  const end = active ? document.activeElement.selectionEnd : null;
  const micFocused = host.contains(document.activeElement) && document.activeElement.classList.contains("mic-button");
  const previousMessages = host.querySelector(".chat-messages");
  const followMessages = !previousMessages || previousMessages.scrollHeight - previousMessages.scrollTop - previousMessages.clientHeight < 48;
  const previousScroll = previousMessages?.scrollTop || 0;
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
  if (thread.pending) {
    const pending = element("div", t("thinking"), "thinking"); pending.setAttribute("role", "status");
    messages.append(pending);
  }
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
    const status = element("div", null, "voice-status");
    status.dataset.recording = String(Boolean(assist.recorder));
    status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite");
    shell.append(append(status, waveform, element("span", t(assist.recorder ? "recording" : "transcribing"))));
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
  const mic = button("", () => toggleVoice(key), "mic-button");
  const glyph = element("span", null, "mic-glyph"); glyph.setAttribute("aria-hidden", "true"); mic.append(glyph);
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
  if (active) { input.focus({ preventScroll: true }); input.setSelectionRange(start, end); }
  else if (micFocused) mic.focus({ preventScroll: true });
  messages.scrollTop = followMessages ? messages.scrollHeight : previousScroll;
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
    await refreshTaskProjection(key);
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
  const poll = { key, card, generation: state.sessionGeneration, started: Date.now(), attempt: 0, timer: null, requestId: assistRequestId() };
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
      await refreshTaskProjection(poll.key);
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
    request_id: assistRequestId(), message, thread_id: thread.id, task: taskReference(key),
    language: voice ? "auto" : assist.language, voice,
  }, key);
}
async function sendStep(key, step) {
  if (!taskReference(key) || state.busy || state.unknown) return;
  if (assist.recorder || assist.acquiringVoice) { showNotice(t("finishVoice"), "pending"); return; }
  threadFor(key).messages.push({ role: "user", text: `✦ ${t(`${step}Step`)}` });
  await assistMutation("assist/step", {
    request_id: assistRequestId(), step, task: taskReference(key), input: null, language: assist.language,
  }, key);
}
async function undoAssist(key, receiptId) {
  if (!receiptId) return;
  const generation = state.sessionGeneration;
  await assistMutation("assist/undo", { request_id: assistRequestId(), receipt_id: receiptId }, key, async () => {
    $("toast").hidden = true;
    for (const thread of assist.threads.values()) for (const message of thread.messages) {
      for (const card of message.cards || []) if (card.receipt_id === receiptId) card.undo_available = false;
    }
    if (generation === state.sessionGeneration) await refreshTaskProjection(key);
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
      request_id: assistRequestId(), path: task.path, revision: task.revision, change: { stage },
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
      await mutate("assist/voice", { audio, request_id: assistRequestId() }, async (result) => {
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
