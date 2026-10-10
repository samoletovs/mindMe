const API = "/api/tasks/api/";
const STAGES = ["untriaged", "clarify", "backlog", "ready", "doing", "waiting", "verify"];
const LABELS = {
  untriaged: "Untriaged", clarify: "Clarify", backlog: "Backlog", ready: "Ready",
  doing: "Doing", waiting: "Waiting", verify: "Verify",
  pending: "Needs approval", submitted: "Publication pending", executing: "In progress",
  uncertain: "Not confirmed", completed: "Verified operation", invalidated: "Source changed",
  failed: "Not completed", declined: "Declined", expired: "Expired", merged: "Canonical",
};
const $ = (id) => document.getElementById(id);
const state = {
  csrf: "", view: "needs", layout: "list", items: [], overview: null, selected: null,
  tab: "overview", busy: false, unknown: null, proposal: null, clarification: null,
  loading: false, detailGeneration: 0, sessionGeneration: 0, authExpires: 0, expiryTimer: null,
};

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
function statusBadge(value) {
  const key = typeof value === "string" ? value : "unknown";
  return element("span", LABELS[key] || key.replaceAll("_", " "), `status ${STAGES.includes(key) || Object.hasOwn(LABELS, key) ? key : ""}`);
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
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), payload === undefined ? 25000 : 125000);
  try {
    const response = await fetch(API + operation, {
      method: payload === undefined ? "GET" : "POST",
      credentials: "same-origin", mode: "same-origin", cache: "no-store",
      headers: payload === undefined ? {} : {
        "Content-Type": "application/json", "X-MindMe-CSRF": state.csrf,
      },
      ...(payload === undefined ? {} : { body: JSON.stringify(payload) }),
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
async function mutate(operation, payload, onSuccess) {
  if (state.busy || state.unknown) return;
  const attempt = { operation, payload, onSuccess };
  await performMutation(attempt);
}
async function performMutation(attempt) {
  const generation = state.sessionGeneration;
  setBusy(true);
  showNotice("Working on this exact request. Please wait for its receipt.");
  try {
    const result = await api(attempt.operation, attempt.payload);
    if (generation !== state.sessionGeneration) return;
    state.unknown = null;
    $("uncertain").hidden = true;
    await attempt.onSuccess(result);
  } catch (error) {
    if (generation === state.sessionGeneration && error.status >= 400 && error.status < 500) {
      state.unknown = null;
      $("uncertain").hidden = true;
    }
    if (generation === state.sessionGeneration && (error.uncertain || !error.status)) {
      state.unknown = attempt;
      $("uncertain").hidden = false;
    }
    showNotice(errorMessage(error), "error");
    if (error.status === 401) {
      $("access-message").textContent = "Your sign-in expired. Sign in again, then inspect Activity before repeating a request.";
    }
  } finally {
    setBusy(false);
  }
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
function setView(view) {
  state.view = view;
  const titles = {
    needs: ["Needs you", "Decisions, due follow-ups and the work you selected."],
    tasks: ["All tasks", "An open task is not automatically today's commitment."],
    areas: ["Areas & projects", "Keep responsibilities visible. Choose which outcomes to advance."],
    activity: ["Activity", "Proposals, preparation and the evidence of follow-through."],
  };
  $("view-title").textContent = titles[view][0];
  $("view-description").textContent = titles[view][1];
  document.querySelectorAll("[data-view]").forEach((node) => {
    if (node.dataset.view === view) node.setAttribute("aria-current", "page");
    else node.removeAttribute("aria-current");
  });
  $("tasks-section").hidden = !["needs", "tasks"].includes(view);
  $("areas-section").hidden = view !== "areas";
  $("activity-section").hidden = view !== "activity";
  render();
}
function renderTaskRow(task) {
  const row = button("", () => openTask(task.path), "task-row");
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
    list.setAttribute("aria-label", "Workflow board. Scroll horizontally for more stages; open a task to change its stage.");
    for (const stage of STAGES) {
      const laneTasks = tasks.filter((task) => task.stage === stage);
      const lane = element("section", null, "board-lane");
      lane.append(element("h3", `${LABELS[stage]} (${laneTasks.length})`));
      laneTasks.forEach((task) => lane.append(renderTaskRow(task)));
      if (!laneTasks.length) lane.append(element("p", "No loaded tasks", "empty"));
      list.append(lane);
    }
  } else {
    list.removeAttribute("tabindex");
    tasks.forEach((task) => list.append(renderTaskRow(task)));
  }
  const candidateCount = state.overview?.candidate_count;
  $("page-status").textContent = state.overview
    ? `${state.items.length} permitted tasks loaded${Number.isInteger(candidateCount) ? ` from ${candidateCount} candidates` : ""}. Filters and counts cover loaded tasks only.`
    : "Source availability has not been confirmed.";
  $("load-more").hidden = state.overview?.next_offset === null || state.overview?.next_offset === undefined;
  $("load-more").disabled = state.loading;
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
  $("needs-count").textContent = String(state.items.filter(isAttention).length + pendingProposals().length);
  $("tasks-count").textContent = String(state.items.length);
  $("activity-count").textContent = String(pendingProposals().length);
  renderAttention();
  renderTasks();
  if (state.view === "areas") renderAreas();
  if (state.view === "activity") renderActivity();
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
    state.tab = "overview";
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
  target.append(button("Back to tasks", closeDetail, "mobile-back quiet"));
  const header = element("div", null, "detail-header");
  append(header, statusBadge(task.stage), element("h2", task.title, "detail-title"),
    element("p", `${task.area ? prettyId(task.area) : "Area not clarified"}${task.project ? ` / ${prettyId(task.project)}` : ""}`, "hint"));
  const links = element("div", null, "meta-line");
  append(links, safeLink("Open canonical source", task.url), element("span", `Revision ${task.revision.slice(0, 7)}`));
  header.append(links);
  target.append(header);
  const tabs = element("nav", null, "detail-tabs");
  tabs.setAttribute("aria-label", "Task actions");
  for (const [id, title] of [["overview", "Overview"], ["edit", "Edit & plan"], ["ai", "AI help"], ["close", "Close"]]) {
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
  if (state.tab === "overview") {
    const description = element("dl", null, "detail-section");
    for (const [label, value] of [["Outcome", task.outcome], ["Next action", task.next_action], ["Done when", task.done_when],
      ["Execution", task.execution], ["Review on", task.review_on], ["Deadline", task.deadline], ["Focus on", task.focus_on], ["Waiting for", task.waiting_for]]) {
      append(description, element("dt", label), element("dd", value || "Not recorded"));
    }
    panel.append(description);
    if (task.definition?.missing?.length) {
      panel.append(element("p", `Needs clarification: ${task.definition.missing.join(", ")}. These are structural checks, not approval.`, "hint"));
    }
    panel.append(writeButton("Clarify this task", () => mutate("clarify", { path: task.path, revision: task.revision }, showClarification)));
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
function proposalStatusText(record) {
  if (record.status === "completed") {
    if (record.kind === "prepare_task") return "A private draft is available. The task is still open.";
    if (record.kind === "research") return "The research operation has a result. Verify the artifact; this does not complete the underlying task.";
    if (record.kind === "close_task") return "The approved closure reached the canonical record.";
    return "The approved change reached the canonical record. This is not proof that the task itself is done.";
  }
  if (record.status === "submitted") return "Submitted for publication. Not yet confirmed on the canonical branch.";
  if (["uncertain", "executing"].includes(record.status)) return "The result is not confirmed. Check this existing receipt; do not submit another operation.";
  if (record.status === "pending") return "Nothing will execute until you approve this exact scope and source version.";
  return "No new success is claimed. Review the recorded state before taking another action.";
}
function renderProposal(record) {
  const article = element("article", null, "proposal");
  const contentAvailable = record.action && (!record.source_status || record.source_status === "current");
  append(article, statusBadge(record.status), element("h3", proposalTitle(record)), element("p",
    contentAvailable ? proposalStatusText(record) : (record.text || "Historical receipt only. Source content is unavailable and cannot authorize a new action.")));
  const source = element("p", null, "source");
  append(source, element("span", `${record.created_on || ""}${record.expires_on ? ` / expires ${record.expires_on}` : ""} `), safeLink("Source", record.source_url));
  article.append(source);
  if (contentAvailable) {
    const action = element("div", null, "exact-action");
    append(action, element("h4", "Exact action to approve"), element("pre", JSON.stringify(record.action, null, 2)));
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
    const receipt = (value) => contentAvailable ? value : (value ? {
      status: value.status, action_id: value.action_id, target_action_id: value.target_action_id,
      canonical_revision: value.canonical_revision, error: value.error, pr_url: value.pr_url,
    } : undefined);
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
  if (["submitted", "executing", "uncertain"].includes(record.status)) {
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
  clearTimeout(state.expiryTimer);
  state.expiryTimer = null;
  state.authExpires = 0;
  state.sessionGeneration += 1;
  state.detailGeneration += 1;
  state.csrf = ""; state.items = []; state.overview = null; state.selected = null;
  state.proposal = null; state.clarification = null; state.unknown = null;
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
  clearData();
  $("access").hidden = false;
  $("workspace").hidden = true;
  $("sign-in").hidden = true;
  $("access-retry").hidden = true;
  $("access-message").textContent = "Checking your personal workspace. No task data is stored in this browser.";
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
    checkSessionExpiry();
    await loadOverview();
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
document.querySelectorAll("[data-view]").forEach((node) => node.addEventListener("click", () => setView(node.dataset.view)));
for (const id of ["task-search", "stage-filter", "area-filter"]) $(id).addEventListener(id === "task-search" ? "input" : "change", renderTasks);
$("layout-list").addEventListener("click", () => setLayout("list"));
$("layout-board").addEventListener("click", () => setLayout("board"));
function setLayout(layout) {
  state.layout = layout;
  $("layout-list").setAttribute("aria-pressed", String(layout === "list"));
  $("layout-board").setAttribute("aria-pressed", String(layout === "board"));
  renderTasks();
}
$("refresh").addEventListener("click", () => loadOverview());
$("load-more").addEventListener("click", () => loadOverview(true));
$("projects-more").addEventListener("click", loadProjects);
$("history-more").addEventListener("click", loadHistory);
$("access-retry").addEventListener("click", start);
document.addEventListener("visibilitychange", () => { if (!document.hidden) checkSessionExpiry(); });
window.addEventListener("focus", checkSessionExpiry);
window.addEventListener("pageshow", checkSessionExpiry);
$("capture-open").addEventListener("click", () => { $("capture-section").hidden = false; $("capture-text").focus(); $("capture-section").scrollIntoView({ block: "start" }); });
$("capture-cancel").addEventListener("click", () => { $("capture-section").hidden = true; $("capture-open").focus(); });
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
  mutate("capture", { request_id: requestId(), text, definition }, async (record) => {
    showProposal(record);
    $("capture-form").reset();
    $("capture-section").hidden = true;
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
start();
