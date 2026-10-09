const $ = (id) => document.getElementById(id);
const BASIS = { observed: "From the source", inferred: "Interpretation", question: "Open question" };
const ISSUE = {
  dashboard_withheld: "Some material is withheld by privacy rules. Filtered identities and counts are not shown.",
  dashboard_schema_invalid: "An unsupported report was withheld. This is not evidence of a quiet day.",
  dashboard_source_changed: "Some review evidence changed. Stale findings are not shown.",
  dashboard_source_unavailable: "Some evidence is unavailable; the view is incomplete.",
  dashboard_source_bounded: "A source exceeds the bounded reader limit.",
  dashboard_read_limit: "The source-read limit was reached; this view is partial.",
};
const utcStamp = (value) => String(value).replace("T", " ").replace(/\+00:00$/, " UTC");

export class Dashboard {
  constructor(ui) {
    this.ui = ui;
    this.clear();
    $("knowledge-more").addEventListener("click", () => this.loadInbox(true));
  }

  clear() {
    this.today = null;
    this.inbox = null;
    this.reader = null;
    this.readSequence = (this.readSequence || 0) + 1;
    this.loadingToday = false;
    this.loadingInbox = false;
    for (const id of ["today-focus", "today-work", "today-new", "knowledge-list", "knowledge-status"]) $(id).replaceChildren();
    this.closeReader();
  }

  closeReader() {
    this.readSequence += 1;
    this.reader = null;
    $("knowledge-section").classList.remove("knowledge-selected");
    $("knowledge-reader").replaceChildren();
    this.ui.empty($("knowledge-reader"), "Start with the evidence.", "Open a finding to see its original quotations. Useful is feedback, not permission to act.");
    $("knowledge-reader").firstElementChild.className = "inspector-empty";
  }

  issues(parent, data) {
    if (data.partial) parent.append(this.ui.element("p", "This is a bounded, partial view. Unshown or unavailable material may still matter.", "review-due"));
    for (const issue of data.issues || []) parent.append(this.ui.element("p", ISSUE[issue] || "Some material could not be displayed.", "review-due"));
  }

  async loadToday(more = false) {
    if (this.loadingToday) return;
    const { state, api, showNotice } = this.ui;
    const generation = state.sessionGeneration;
    this.loadingToday = true;
    if (!this.today) {
      $("today-focus").textContent = "Loading the approved focus projection...";
      $("today-new").textContent = "Checking canonical sources...";
    }
    try {
      const offset = more ? this.today?.next_offset : 0;
      if (offset == null) return;
      const data = await api(`dashboard/today?offset=${offset}`);
      if (generation !== state.sessionGeneration) return;
      if (!Array.isArray(data.items) || !Array.isArray(data.focus?.items) || !data.visit_token) throw new Error("The Today response is incomplete.");
      if (more && (this.today.canonical_revision !== data.canonical_revision || this.today.since !== data.since)) {
        throw new Error("The source snapshot or recorded visit changed. Refresh Today before loading more.");
      }
      this.today = more ? { ...data, items: [...this.today.items, ...data.items] } : data;
      this.renderToday();
    } catch (error) {
      if (generation !== state.sessionGeneration) return;
      if (!this.today) {
        $("today-focus").textContent = "Approved focus is unavailable.";
        $("today-new").textContent = "Source availability has not been confirmed. Refresh to try again.";
      }
      showNotice(`${error.message} ${this.today ? "Previously loaded sources may be stale." : "No empty-vault claim is made."}`, "error");
    } finally {
      this.loadingToday = false;
    }
  }

  renderToday() {
    const { state, element, append, button, writeButton, empty, openTask, renderTaskRow } = this.ui;
    const work = $("today-work");
    work.replaceChildren();
    if (!state.overview) {
      empty(work, "Tasks are not available yet.", "Source failures are not an empty backlog.");
    } else {
      const records = state.overview.history || [];
      const decisions = records.filter((item) => item.status === "pending" && item.action);
      const results = records.filter((item) => item.status === "completed" && !item.reviewed && ["prepare_task", "research"].includes(item.kind));
      if (decisions.length || results.length) {
        work.append(element("p", `${decisions.length} loaded proposal(s) need a decision; ${results.length} prepared result(s) await review.`));
        work.append(button("Review decisions and results", () => this.ui.setView("activity")));
      }
      const attention = state.items.filter((item) => item.attention_eligible);
      work.append(element("h3", "Dates and follow-ups"));
      if (!attention.length) empty(work, "No matching tasks on the loaded page.", "That is not a statement about unassessed tasks.");
      for (const task of attention.slice(0, 5)) {
        const row = renderTaskRow(task, () => openTask(task.path));
        work.append(row);
      }
      const waiting = state.items.filter((item) => item.waiting_for || item.stage === "waiting");
      if (waiting.length) {
        work.append(element("h3", "Waiting, not automatically due"));
        for (const task of waiting.slice(0, 3)) work.append(renderTaskRow(task, () => openTask(task.path)));
      }
      work.append(element("p", `Task day ${state.overview.attention.calendar_date} / ${state.overview.attention.timezone}. This summary uses the task service's dates and eligibility, not your browser clock.`, "hint"));
      if (state.overview.attention_complete === false || state.overview.next_offset != null) {
        work.append(element("p", "More tasks or source issues remain. Check Tasks before concluding that nothing needs attention.", "review-due"));
      }
      work.append(button("Open Tasks", () => this.ui.setView("needs")));
    }
    if (!this.today) return;
    const focus = $("today-focus");
    focus.replaceChildren(element("h2", "Approved focus"));
    if (this.today.focus.draft_present) focus.append(element("p", "The North Star is a draft, not approved focus.", "hint"));
    for (const item of this.today.focus.items) {
      const row = element("div", null, "focus-line");
      append(row, element("span", item.status === "expired" ? "Expired window" : item.status === "upcoming" ? "Upcoming window" : "Approved", `status ${item.status === "expired" ? "expired" : ""}`),
        element("p", item.text), element("span", item.starts_on ? `${item.starts_on}${item.ends_on ? ` to ${item.ends_on}` : " (no end date recorded)"}` : "No dated window recorded", "hint"));
      focus.append(row);
    }
    if (!this.today.focus.items.length) empty(focus, "No eligible approved focus to show.", "Drafts and private context are not substituted for an approved focus.");
    if (this.today.focus.status !== "available") focus.append(element("p", "The focus projection is partial, withheld or unavailable. No private pointers are shown.", "review-due"));
    const news = $("today-new");
    news.replaceChildren();
    news.append(element("p", this.today.first_visit
      ? `${this.today.baseline_expired ? "The old baseline expired. " : "First recorded visit. "}These are selected current sources, not claims of new activity today.`
      : `Compared with the canonical snapshot recorded ${utcStamp(this.today.since)}.`, "hint"));
    news.append(element("p", `Observed ${utcStamp(this.today.observed_at)}. Source dates below are recorded metadata, not inferred creation or progress dates.`, "hint"));
    this.issues(news, this.today);
    for (const item of this.today.items) {
      const row = button("", () => this.open(item), "source-row");
      append(row, element("span", item.change === "initial" ? "Current source" : item.change === "new" ? "New canonical source" : "Updated canonical source", "hint"),
        element("strong", item.title), element("p", this.sourceDates(item)));
      news.append(row);
    }
    if (!this.today.items.length) empty(news, "No eligible changes in this window.", "This is not a claim about private, unread or unavailable sources.");
    if (this.today.next_offset != null) news.append(button("Load more source changes", () => this.loadToday(true)));
    const visit = writeButton("Record this visit", () => this.ui.mutate("dashboard/visit", { token: this.today.visit_token }, async () => {
      this.ui.showNotice("Visit marker saved privately. The next comparison starts from this snapshot; this does not mark material read.");
    }));
    news.append(append(element("div", null, "actions"), visit, button("Open Knowledge inbox", () => this.ui.setView("knowledge"))));
    news.append(element("p", "Record this snapshot as the next visit's baseline, including any unshown part. No reading, understanding or task completion is inferred.", "hint"));
  }

  sourceDates(item) {
    const dates = Object.entries(item.source_dates || {}).map(([name, value]) => `${name.replaceAll("_", " ")} ${value}`);
    return `${dates.join(" / ") || "Source date not recorded"} / revision ${item.revision.slice(0, 7)}`;
  }

  async loadInbox(more = false) {
    if (this.loadingInbox) return;
    const { state, api, showNotice } = this.ui;
    const generation = state.sessionGeneration;
    this.loadingInbox = true;
    $("knowledge-status").textContent = "Loading canonical reviews and checking their evidence...";
    try {
      const offset = more ? this.inbox?.next_offset : 0;
      if (offset == null) return;
      const data = await api(`dashboard/inbox?offset=${offset}`);
      if (generation !== state.sessionGeneration) return;
      if (!Array.isArray(data.items) || !Object.hasOwn(data, "next_offset")) throw new Error("The inbox response is incomplete.");
      if (more && this.inbox.canonical_revision !== data.canonical_revision) throw new Error("The review snapshot changed. Refresh the inbox before paging.");
      this.inbox = more ? { ...data, items: [...this.inbox.items, ...data.items] } : data;
      if (!more) this.closeReader();
      this.renderInbox();
    } catch (error) {
      if (generation === state.sessionGeneration) {
        $("knowledge-status").textContent = "The inbox could not be refreshed. Previously loaded material may be stale.";
        showNotice(error.message, "error");
      }
    } finally {
      this.loadingInbox = false;
    }
  }

  renderInbox() {
    if (!this.inbox) return;
    const { element, append, button, empty } = this.ui;
    if (this.ui.state.view === "knowledge") $("snapshot").textContent = `Canonical review snapshot ${this.inbox.canonical_revision.slice(0, 7)}`;
    $("knowledge-status").replaceChildren();
    this.issues($("knowledge-status"), this.inbox);
    const list = $("knowledge-list");
    list.replaceChildren();
    for (const review of this.inbox.items) {
      const rows = review.kind === "daily_review" ? review.findings : [null];
      if (!rows.length) {
        list.append(element("p", `Daily review ${review.as_of}: no findings in its bounded scope.`, "empty"));
      }
      for (const finding of rows) {
        const row = button("", () => this.open(review, finding?.id), "source-row");
        append(row, element("span", `${review.as_of} / ${finding ? BASIS[finding.basis] : "Weekly digest, draft"}`, "hint"),
          element("strong", finding?.statement || "Revisit the week's changed notes"),
          element("p", finding ? "Open finding and original evidence" : "Open digest and eligible source references"));
        list.append(row);
      }
    }
    if (!list.children.length) empty(list, this.inbox.issues?.length ? "No review can be safely shown on this page." : "No canonical reviews on this page.", "Unmerged, private and unsupported material is not presented as published knowledge.");
    $("knowledge-more").hidden = this.inbox.next_offset == null;
  }

  async open(item, findingId = null) {
    this.ui.setView("knowledge", false);
    const sequence = ++this.readSequence;
    const generation = this.ui.state.sessionGeneration;
    const target = $("knowledge-reader");
    target.replaceChildren(this.ui.element("p", "Rechecking the canonical source and its evidence...", "empty"));
    $("knowledge-section").classList.add("knowledge-selected");
    try {
      const source = await this.ui.api("dashboard/read", { id: item.id, revision: item.revision });
      if (sequence !== this.readSequence || generation !== this.ui.state.sessionGeneration) return;
      if (source.id !== item.id || source.revision !== item.revision) throw new Error("The source response does not match this selection.");
      this.reader = { source, findingId };
      this.renderReader();
      target.tabIndex = -1;
      target.focus({ preventScroll: true });
      if (matchMedia("(max-width: 690px)").matches) target.scrollIntoView({ block: "start" });
    } catch (error) {
      if (sequence !== this.readSequence || generation !== this.ui.state.sessionGeneration) return;
      target.replaceChildren(this.ui.button("Back to inbox", () => this.closeReader()), this.ui.element("p", error.message, "notice error"));
    }
  }

  prose(parent, text) {
    const { element } = this.ui;
    const blocks = text.split(/\n\s*\n/);
    for (const block of blocks) {
      if (!block.trim()) continue;
      if (/^#{1,6} /.test(block) && !block.includes("\n")) parent.append(element(/^#{1,2} /.test(block) ? "h2" : "h3", block.replace(/^#+ /, "")));
      else if (block.split("\n").every((line) => /^[-*] /.test(line))) {
        const list = element("ul");
        block.split("\n").forEach((line) => list.append(element("li", line.slice(2))));
        parent.append(list);
      } else parent.append(element("p", block));
    }
  }

  renderReader() {
    const { source, findingId } = this.reader;
    const { element, append, button, writeButton } = this.ui;
    const target = $("knowledge-reader");
    target.replaceChildren(button("Back to inbox", () => { this.closeReader(); if (!this.inbox) this.loadInbox(); }, "reader-back quiet"));
    const finding = source.findings?.find((item) => item.id === findingId);
    if (source.kind === "daily_review" && !finding) {
      target.append(element("p", "This finding is no longer available. Refresh the inbox.", "notice error"));
      return;
    }
    append(target, element("h2", finding?.statement || source.title),
      element("p", finding ? `${BASIS[finding.basis]} / review ${source.as_of}` : source.as_of ? `Draft digest ${source.period_start} to ${source.as_of}` : this.sourceDates(source), "hint"),
      element("p", `${source.path} / revision ${source.revision.slice(0, 7)}`, "hint source-meta"));
    if (finding) {
      target.append(element("p", "Possible next step, not approved: " + finding.next_step));
      target.append(element("h3", "Original evidence"));
      target.append(element("p", "Exact source quotations support inspection, not automatic agreement with the interpretation.", "hint"));
    } else {
      if (source.kind === "weekly_digest") target.append(element("p", "This generated digest is a historical draft, not a current task checklist or original evidence. Its links are checked against current sources; the digest has no per-source revisions to verify its historical summaries.", "notice pending"));
      const prose = element("div", null, "source-prose");
      this.prose(prose, source.text || "");
      target.append(prose);
      if (source.bounded) target.append(element("p", "Text shortened at the 10,000-character reader limit. This is not the full source.", "notice pending"));
    }
    for (const evidence of finding?.evidence || source.evidence || []) {
      const block = element("div", null, "evidence-block");
      if (evidence.quote) block.append(element("blockquote", evidence.quote));
      append(block, button(`Read source: ${evidence.title}`, () => this.open(evidence)),
        element("p", `${evidence.path} / ${this.sourceDates(evidence)}`, "hint source-meta"));
      target.append(block);
    }
    if (source.kind !== "weekly_digest") {
      target.append(button("Make a task", () => this.ui.beginSourceCapture(source, finding), "primary"));
      target.append(element("p", "Edit a capture, review the exact record, then explicitly confirm. Feedback never creates a task.", "hint"));
    }
    if (!finding) return;
    target.append(element("h3", "Useful to you?"));
    if (!source.feedback?.available) {
      target.append(element("p", "Feedback is disabled, its private receipt changed, or the fourteen-day window expired. No choice will be saved.", "hint"));
      return;
    }
    const current = source.feedback.findings[finding.id];
    const actions = element("div", null, "actions");
    for (const [value, label] of [["useful", "Useful"], ["known", "Already know"], ["dismiss", "Not useful"]]) {
      actions.append(writeButton(label, () => this.saveFeedback(source, finding, value)));
    }
    target.append(actions);
    if (current.value) target.append(element("p", `Saved: ${current.value.text}. Recorded ${current.value.recorded_on}.`, "hint"));
    const details = element("details");
    details.append(element("summary", "Snooze this finding"));
    const form = element("form", null, "feedback-form");
    const label = element("label", "Reconsider on (UTC date)");
    label.htmlFor = "finding-snooze";
    const input = element("input"); input.type = "date"; input.id = "finding-snooze"; input.required = true;
    const submit = element("button", "Save snooze"); submit.type = "submit";
    append(form, label, input, element("p", `Choose a future date before ${source.feedback.expires_on}. Feedback expires with the review.`, "hint"), submit);
    form.addEventListener("submit", (event) => { event.preventDefault(); this.saveFeedback(source, finding, "snooze", input.value); });
    details.append(form);
    target.append(details);
    this.ui.setBusy(this.ui.state.busy);
  }

  saveFeedback(source, finding, value, reviewOn) {
    const version = source.feedback.findings[finding.id].version;
    this.ui.mutate("dashboard/feedback", {
      id: source.id, revision: source.revision, finding: finding.id, version, value,
      ...(reviewOn ? { review_on: reviewOn } : {}),
    }, async (result) => {
      source.feedback.findings[finding.id] = { value: result.value, version: result.version };
      this.ui.showNotice(result.message);
      if (this.reader?.source === source) this.renderReader();
    });
  }
}
