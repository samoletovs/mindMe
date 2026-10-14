const $ = (id) => document.getElementById(id);
const copy = (en, ru) => document.documentElement?.lang === "ru" ? ru : en;
const basis = (value) => ({
  observed: copy("From the source", "Из источника"),
  inferred: copy("Interpretation", "Интерпретация"),
  question: copy("Open question", "Открытый вопрос"),
})[value] || value;
const ISSUE = {
  dashboard_withheld: "Some material is withheld by privacy rules. Filtered identities and counts are not shown.",
  dashboard_schema_invalid: "An unsupported report was withheld. This is not evidence of a quiet day.",
  dashboard_source_changed: "Some review evidence changed. Stale findings are not shown.",
  dashboard_source_unavailable: "Some evidence is unavailable; the view is incomplete.",
  dashboard_source_bounded: "A source exceeds the bounded reader limit.",
  dashboard_read_limit: "The source-read limit was reached; this view is partial.",
};
const ISSUE_RU = {
  dashboard_withheld: "Часть материалов скрыта правилами приватности. Идентификаторы и количество скрытых записей не показаны.",
  dashboard_schema_invalid: "Неподдерживаемый отчёт скрыт. Это не свидетельствует о спокойном дне.",
  dashboard_source_changed: "Некоторые источники обзора изменились. Устаревшие выводы не показаны.",
  dashboard_source_unavailable: "Часть доказательств недоступна; обзор неполный.",
  dashboard_source_bounded: "Источник превышает лимит ограниченного просмотра.",
  dashboard_read_limit: "Достигнут лимит чтения источников; обзор частичный.",
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
    this.readerOrigin = null;
    this.readerSelection = null;
    for (const id of ["today-focus", "today-work", "today-new", "knowledge-list", "knowledge-status"]) $(id).replaceChildren();
    this.closeReader();
  }

  closeReader(restore = false) {
    this.readSequence += 1;
    this.reader = null;
    $("knowledge-section").classList.remove("knowledge-selected");
    $("knowledge-reader").replaceChildren();
    this.ui.empty($("knowledge-reader"), copy("Start with the evidence.", "Начните с доказательств."),
      copy("Open a finding to see its original quotations. Useful is feedback, not permission to act.", "Откройте вывод, чтобы увидеть точные цитаты. «Полезно» — отзыв, а не разрешение действовать."));
    $("knowledge-reader").firstElementChild.className = "inspector-empty";
    if (restore) {
      const row = Array.from($("knowledge-list").querySelectorAll(".source-row")).find((node) =>
        node.dataset.sourceId === this.readerSelection?.id && node.dataset.findingId === (this.readerSelection?.findingId || ""));
      this.ui.restoreFocus(this.readerOrigin, row || $("view-title"));
    }
    $("knowledge-reader").removeAttribute?.("aria-busy");
    this.markSelection();
    this.readerOrigin = null;
    this.readerSelection = null;
  }

  markSelection() {
    for (const row of $("knowledge-list").querySelectorAll?.(".source-row") || []) {
      row.setAttribute("aria-pressed", String(Boolean(this.reader) && row.dataset.sourceId === this.readerSelection?.id
        && row.dataset.findingId === (this.readerSelection?.findingId || "")));
    }
  }

  issues(parent, data) {
    if (data.partial) parent.append(this.ui.element("p", copy("This is a bounded, partial view. Unshown or unavailable material may still matter.", "Это ограниченный, частичный обзор. Непоказанные или недоступные материалы тоже могут быть важны."), "review-due"));
    for (const issue of data.issues || []) parent.append(this.ui.element("p", copy(ISSUE[issue], ISSUE_RU[issue]) || copy("Some material could not be displayed.", "Некоторые материалы не удалось показать."), "review-due"));
  }

  async loadToday(more = false) {
    if (this.loadingToday) return;
    const { state, api, showNotice } = this.ui;
    const generation = state.sessionGeneration;
    this.loadingToday = true;
    if (!this.today) {
      $("today-focus").textContent = copy("Loading the approved focus projection...", "Загружаем утверждённый фокус...");
      $("today-new").textContent = copy("Checking canonical sources...", "Проверяем канонические источники...");
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
        $("today-focus").textContent = copy("Approved focus is unavailable.", "Утверждённый фокус недоступен.");
        $("today-new").textContent = copy("Source availability has not been confirmed. Refresh to try again.", "Доступность источников не подтверждена. Обновите, чтобы попробовать снова.");
      }
      showNotice(`${error.message} ${this.today ? copy("Previously loaded sources may be stale.", "Ранее загруженные источники могут устареть.") : copy("No empty-vault claim is made.", "Это не означает, что хранилище пусто.")}`, "error");
    } finally {
      this.loadingToday = false;
    }
  }

  renderToday() {
    const { state, element, append, button, writeButton, empty, infoTip, openTask, renderTaskRow } = this.ui;
    const work = $("today-work");
    const priorities = this.priorityTasks();
    this.renderPriorities(priorities);
    const shown = new Set(priorities.map((task) => task.path));
    work.replaceChildren();
    if (!state.overview) {
      empty(work, copy("Tasks are not available yet.", "Задачи пока недоступны."), copy("Source failures are not an empty backlog.", "Ошибка источника не означает пустой список задач."));
    } else {
      const records = state.overview.history || [];
      const decisions = records.filter((item) => item.status === "pending" && item.action);
      const results = records.filter((item) => item.status === "completed" && !item.reviewed && ["prepare_task", "research"].includes(item.kind));
      if (decisions.length || results.length) {
        work.append(element("p", copy(`${decisions.length} loaded proposal(s) need a decision; ${results.length} prepared result(s) await review.`,
          `Ожидают решения: ${decisions.length}; подготовленных результатов для проверки: ${results.length}.`)));
        work.append(button(copy("Review decisions and results", "Проверить решения и результаты"), () => this.ui.setView("activity", { arrival: true })));
      }

      const attention = state.items.filter((item) => item.attention_eligible && !shown.has(item.path));
      if (attention.length) work.append(element("h3", copy("More dates and follow-ups", "Другие сроки и пересмотры")));
      else if (priorities.length) work.append(element("p", copy(
        "The loaded tasks needing attention are in Next moves above, with their dates and dependencies.",
        "Загруженные задачи, требующие внимания, показаны выше — со сроками и зависимостями."), "hint"));
      else empty(work, copy("No due tasks in this loaded set.", "В загруженной выборке нет задач со сроком на сегодня."),
        copy("Only loaded tasks are checked.", "Проверены только загруженные задачи."));
      for (const task of attention) {
        const row = renderTaskRow(task, () => openTask(task.path));
        work.append(row);
        shown.add(task.path);
      }
      const waiting = state.items.filter((item) => (item.waiting_for || item.stage === "waiting") && !shown.has(item.path));
      if (waiting.length) {
        work.append(element("h3", copy("Waiting, not automatically due", "Ожидают, но не обязательно требуют внимания")));
        for (const task of waiting) work.append(renderTaskRow(task, () => openTask(task.path)));
      }
      const day = element("p", `${copy("Task day", "День задач")} ${state.overview.attention.calendar_date} / ${state.overview.attention.timezone} `, "hint");
      day.append(infoTip(copy("Uses the task service's dates and eligibility, not your browser clock.", "Используются даты и условия сервиса задач, а не часы браузера.")));
      work.append(day);
      if (state.overview.attention_complete === false || state.overview.next_offset != null) {
        work.append(element("p", copy("More tasks or source issues remain. Check Tasks before concluding that nothing needs attention.", "Остались другие задачи или проблемы источников. Проверьте «Поток», прежде чем считать, что ничего не требует внимания."), "review-due"));
      }
      work.append(button(copy("Open all loaded work in Flow", "Открыть все загруженные задачи в «Потоке»"), () => this.ui.setView("tasks", { arrival: true })));
    }
    if (!this.today) return;
    const focus = $("today-focus");
    focus.replaceChildren(element("h2", copy("Approved focus", "Утверждённый фокус")));
    if (this.today.focus.draft_present) focus.append(element("p", copy("The North Star is a draft, not approved focus.", "North Star — черновик, а не утверждённый фокус."), "hint"));
    for (const item of this.today.focus.items) {
      const row = element("div", null, "focus-line");
      append(row, element("span", item.status === "expired" ? copy("Expired window", "Период истёк") : item.status === "upcoming" ? copy("Upcoming window", "Предстоящий период") : copy("Approved", "Утверждено"), `status ${item.status === "expired" ? "expired" : ""}`),
        element("p", item.text), element("span", item.starts_on ? `${item.starts_on}${item.ends_on ? ` — ${item.ends_on}` : copy(" (no end date recorded)", " (дата окончания не указана)")}` : copy("No dated window recorded", "Период не указан"), "hint"));
      focus.append(row);
    }
    if (!this.today.focus.items.length) empty(focus, copy("No eligible approved focus to show.", "Нет доступного утверждённого фокуса."),
      copy("Drafts and private context are not substituted for an approved focus.", "Черновики и личный контекст не подменяют утверждённый фокус."));
    if (this.today.focus.status !== "available") focus.append(element("p", copy("The focus projection is partial, withheld or unavailable. No private pointers are shown.", "Фокус частичный, скрыт или недоступен. Личные ссылки не показаны."), "review-due"));
    const news = $("today-new");
    news.replaceChildren();
    const since = element("p", this.today.first_visit
      ? `${this.today.baseline_expired ? copy("Fresh start", "Новая точка отсчёта") : copy("First visit", "Первый визит")} · ${copy("current sources", "текущие источники")} `
      : `${copy("Since", "С")} ${utcStamp(this.today.since)} `, "hint");
    since.append(infoTip(`${this.today.first_visit ? copy("These are selected current sources, not claims of new activity today. ", "Это выбранные текущие источники, а не заявление о новой активности сегодня. ") : ""}${copy("Observed", "Проверено")} ${utcStamp(this.today.observed_at)}. ${copy("Source dates are recorded metadata, not inferred progress dates.", "Даты источников взяты из записанных метаданных, а не выведены из предполагаемого прогресса.")}`));
    news.append(since);
    this.issues(news, this.today);
    for (const item of this.today.items) {
      const row = button("", () => this.open(item), "source-row");
      row.dataset.sourceId = item.id;
      append(row, element("span", item.change === "initial" ? copy("Current source", "Текущий источник") : item.change === "new" ? copy("New canonical source", "Новый канонический источник") : copy("Updated canonical source", "Обновлённый канонический источник"), "hint"),
        element("strong", item.title), element("p", this.sourceDates(item)));
      news.append(row);
    }
    if (!this.today.items.length) {
      const none = element("div", null, "empty");
      append(none, element("strong", this.today.partial || this.today.issues?.length
        ? copy("No source changes in this partial view. ", "В частичном обзоре нет изменений источников. ")
        : this.today.first_visit ? copy("No sources in this initial selection. ", "В начальной выборке нет источников. ")
          : copy("Nothing new in the checked sources. ", "В проверенных источниках нет нового. ")),
      infoTip(copy("Only eligible canonical sources are compared; private, unread or unavailable sources are not assessed.", "Сравниваются только разрешённые канонические источники; личные, непрочитанные и недоступные источники не оцениваются.")));
      news.append(none);
    }
    if (this.today.next_offset != null) news.append(button(copy("Load more source changes", "Загрузить ещё изменения источников"), () => this.loadToday(true)));
    const visit = writeButton(copy("Mark as seen", "Отметить как увиденное"), () => this.ui.mutate("dashboard/visit", { token: this.today.visit_token }, async () => {
      this.ui.showNotice(copy("Marked as seen. Next time starts from this snapshot.", "Отмечено как увиденное. Следующее сравнение начнётся с этого снимка."));
    }));
    news.append(append(element("div", null, "actions"), visit,
      infoTip(copy("Saves this snapshot, including any unshown part, as the next comparison baseline. It does not mark anything read or done.", "Сохраняет этот снимок, включая непоказанную часть, как базу следующего сравнения. Ничего не отмечается прочитанным или выполненным.")),
      button(copy("Open Knowledge inbox", "Открыть входящие знаний"), () => this.ui.setView("knowledge", { arrival: true }))));
  }

  priorityTasks() {
    const rank = (task) => (task.attention_reasons || []).reduce((score, reason) =>
      score + (({ deadline_due: 16, deadline_soon: 8, focus_today: 4, review_due: 2 })[reason] || 0), 0);
    return this.ui.state.items.filter((task) => task.attention_eligible)
      .sort((a, b) => rank(b) - rank(a)).slice(0, 3);
  }

  renderPriorities(top = this.priorityTasks()) {
    const { state, element, append, button, openTask, taskTiming, statusBadge } = this.ui;
    const target = $("today-priorities");
    target.replaceChildren();
    const ru = document.documentElement?.lang === "ru";
    const estimated = top.filter((task) => Number.isFinite(task.estimate_min) && task.estimate_min >= 0);
    const minutes = estimated.reduce((sum, task) => sum + task.estimate_min, 0);
    const duration = minutes >= 60
      ? `${Math.floor(minutes / 60)}${ru ? " ч" : "h"}${minutes % 60 ? ` ${minutes % 60}${ru ? " мин" : "m"}` : ""}`
      : `${minutes}${ru ? " мин" : "m"}`;
    const count = top.length;
    const partial = state.overview?.attention_complete !== true || state.overview?.next_offset != null;
    const line = !state.overview ? copy("No task snapshot yet.", "Снимок задач пока недоступен.")
      : !count ? partial ? copy("There is more to check.", "Есть что ещё проверить.")
        : copy("Space for your next move.", "Простор для следующего шага.")
      : ru ? `${count} ${count === 1 ? "шаг" : "шага"} на сегодня.` : `${count} next ${count === 1 ? "move" : "moves"} for today.`;
    if ($("hero-line")) $("hero-line").textContent = line;
    if ($("hero-detail")) {
      $("hero-detail").textContent = count
        ? `${copy("Start with", "Начните с")}: ${top[0].title}${estimated.length ? copy(` · ~${duration} recorded across ${estimated.length}/${count} tasks.`, ` · ~${duration} по оценкам ${estimated.length}/${count} задач.`) : ""}`
        : !state.overview ? copy("Refresh to check your sources. Unavailable does not mean an empty backlog.", "Обновите источники. Недоступность не означает пустой список.")
          : partial ? copy("Only loaded tasks are shown. Unchecked work may still need attention.", "Показаны только загруженные задачи. Непроверенная работа ещё может требовать внимания.")
            : copy("No due or selected tasks in this loaded snapshot. Choose a task when you are ready.", "В загруженном снимке нет задач со сроком на сегодня или выбранным фокусом. Выберите задачу, когда будете готовы.");
    }
    if (!top.length) {
      this.ui.empty(target, copy("No next moves to show yet.", "Пока нет следующих шагов для показа."),
        copy("Open Flow to inspect the task inventory and its source status.", "Откройте «Поток», чтобы проверить задачи и состояние источников."));
      return;
    }
    top.forEach((task, i) => {
      const card = button("", () => openTask(task.path), "priority-card");
      card.dataset.path = task.path;
      const number = element("span", `0${i + 1}`, "priority-number"); number.setAttribute("aria-hidden", "true");
      append(card, number,
        element("strong", task.title),
        element("p", task.next_action || (ru ? "Уточните следующий шаг." : "Clarify the next step.")),
        append(element("div", null, "priority-reason"),
          statusBadge(task.stage), taskTiming(task)));
      if (task.estimate_min != null) card.append(element("span", `${task.estimate_min} ${copy("min", "мин")}`, "status"));
      target.append(card);
    });
  }

  sourceDates(item) {
    const dates = Object.entries(item.source_dates || {}).map(([name, value]) => `${name.replaceAll("_", " ")} ${value}`);
    return `${dates.join(" / ") || copy("Source date not recorded", "Дата источника не указана")} / ${copy("revision", "версия")} ${item.revision.slice(0, 7)}`;
  }

  async loadInbox(more = false) {
    if (this.loadingInbox) return;
    const { state, api, showNotice } = this.ui;
    const generation = state.sessionGeneration;
    this.loadingInbox = true;
    $("knowledge-status").textContent = copy("Loading canonical reviews and checking their evidence...", "Загружаем канонические обзоры и проверяем доказательства...");
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
        $("knowledge-status").textContent = copy("The inbox could not be refreshed. Previously loaded material may be stale.", "Не удалось обновить входящие. Ранее загруженные материалы могут устареть.");
        showNotice(error.message, "error");
      }
    } finally {
      this.loadingInbox = false;
    }
  }

  renderInbox() {
    if (!this.inbox) return;
    const { element, append, button, empty } = this.ui;
    if (this.ui.state.view === "knowledge") $("snapshot").textContent = `${copy("Canonical review snapshot", "Канонический снимок обзоров")} ${this.inbox.canonical_revision.slice(0, 7)}`;
    $("knowledge-status").replaceChildren();
    this.issues($("knowledge-status"), this.inbox);
    const list = $("knowledge-list");
    list.replaceChildren();
    for (const review of this.inbox.items) {
      const rows = review.kind === "daily_review" ? review.findings : [null];
      if (!rows.length) {
        list.append(element("p", copy(`Daily review ${review.as_of}: no findings in its bounded scope.`, `Ежедневный обзор ${review.as_of}: нет выводов в его ограниченном объёме.`), "empty"));
      }
      for (const finding of rows) {
        const row = button("", () => this.open(review, finding?.id), "source-row");
        row.dataset.sourceId = review.id; row.dataset.findingId = finding?.id || "";
        append(row, element("span", `${review.as_of} / ${finding ? basis(finding.basis) : copy("Weekly digest, draft", "Недельный дайджест, черновик")}`, "hint"),
          element("strong", finding?.statement || copy("Revisit the week's changed notes", "Вернуться к изменённым заметкам недели")),
          element("p", finding ? copy("Open finding and original evidence", "Открыть вывод и исходные доказательства") : copy("Open digest and eligible source references", "Открыть дайджест и доступные ссылки на источники")));
        list.append(row);
      }
    }
    if (!list.children.length) empty(list, this.inbox.issues?.length ? copy("No review can be safely shown on this page.", "На этой странице нельзя безопасно показать обзор.") : copy("No canonical reviews on this page.", "На этой странице нет канонических обзоров."),
      copy("Unmerged, private and unsupported material is not presented as published knowledge.", "Необъединённые, личные и неподдерживаемые материалы не выдаются за опубликованные знания."));
    $("knowledge-more").hidden = this.inbox.next_offset == null;
    this.markSelection();
  }

  async open(item, findingId = null) {
    if (!$("knowledge-reader").contains(document.activeElement)) {
      this.readerOrigin = document.activeElement;
      this.readerSelection = { id: item.id, findingId };
    }
    this.ui.setView("knowledge", { load: false });
    const sequence = ++this.readSequence;
    const generation = this.ui.state.sessionGeneration;
    const target = $("knowledge-reader");
    target.replaceChildren(this.ui.element("p", copy("Rechecking the canonical source and its evidence...", "Проверяем канонический источник и его доказательства..."), "empty"));
    target.setAttribute("aria-busy", "true");
    $("knowledge-section").classList.add("knowledge-selected");
    try {
      const source = await this.ui.api("dashboard/read", { id: item.id, revision: item.revision });
      if (sequence !== this.readSequence || generation !== this.ui.state.sessionGeneration) return;
      if (source.id !== item.id || source.revision !== item.revision) throw new Error("The source response does not match this selection.");
      this.reader = { source, findingId };
      this.renderReader();
      this.markSelection();
      if (this.ui.state.view === "knowledge") {
        target.scrollTop = 0;
        this.ui.restoreFocus(target);
        target.scrollIntoView({ block: "start" });
      }
    } catch (error) {
      if (sequence !== this.readSequence || generation !== this.ui.state.sessionGeneration) return;
      const failure = this.ui.element("p", error.message, "notice error");
      failure.setAttribute("role", "alert");
      target.replaceChildren(this.ui.button(copy("Back to inbox", "К входящим"), () => this.closeReader(true)), failure);
      if (this.ui.state.view === "knowledge") this.ui.restoreFocus(target);
    } finally {
      if (sequence === this.readSequence) target.removeAttribute("aria-busy");
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
    const progress = element("span", null, "reader-progress"); progress.setAttribute("aria-hidden", "true");
    target.replaceChildren(progress, button(copy("Back to inbox", "К входящим"), () => { this.closeReader(true); if (!this.inbox) this.loadInbox(); }, "reader-back quiet"));
    const finding = source.findings?.find((item) => item.id === findingId);
    if (source.kind === "daily_review" && !finding) {
      target.append(element("p", copy("This finding is no longer available. Refresh the inbox.", "Вывод больше недоступен. Обновите входящие."), "notice error"));
      return;
    }
    append(target, element("h2", finding?.statement || source.title),
      element("p", finding ? `${basis(finding.basis)} / ${copy("review", "обзор")} ${source.as_of}` : source.as_of ? `${copy("Draft digest", "Черновик дайджеста")} ${source.period_start} — ${source.as_of}` : this.sourceDates(source), "hint"),
      element("p", `${source.path} / ${copy("revision", "версия")} ${source.revision.slice(0, 7)}`, "hint source-meta"));
    if (finding) {
      target.append(element("p", copy("Possible next step, not approved: ", "Возможный шаг, не утверждён: ") + finding.next_step));
      target.append(element("h3", copy("Original evidence", "Исходные доказательства")));
      target.append(element("p", copy("Exact source quotations support inspection, not automatic agreement with the interpretation.", "Точные цитаты служат проверке, а не автоматическому согласию с интерпретацией."), "hint"));
    } else {
      if (source.kind === "weekly_digest") target.append(element("p", copy("This generated digest is a historical draft, not a current task checklist or original evidence. Its links are checked against current sources; the digest has no per-source revisions to verify its historical summaries.", "Этот созданный дайджест — исторический черновик, а не текущий список задач или исходное доказательство. Ссылки проверяются по текущим источникам; версий каждого источника для проверки исторических сводок у дайджеста нет."), "notice pending"));
      const prose = element("div", null, "source-prose");
      this.prose(prose, source.text || "");
      target.append(prose);
      if (source.bounded) target.append(element("p", copy("Text shortened at the 10,000-character reader limit. This is not the full source.", "Текст сокращён до лимита просмотра в 10 000 символов. Это не полный источник."), "notice pending"));
    }
    for (const evidence of finding?.evidence || source.evidence || []) {
      const block = element("div", null, "evidence-block");
      if (evidence.quote) block.append(element("blockquote", evidence.quote));
      append(block, button(`${copy("Read source", "Читать источник")}: ${evidence.title}`, () => this.open(evidence)),
        element("p", `${evidence.path} / ${this.sourceDates(evidence)}`, "hint source-meta"));
      target.append(block);
    }
    if (source.kind !== "weekly_digest") {
      target.append(button(copy("Make a task", "Создать задачу"), () => this.ui.beginSourceCapture(source, finding), "primary"));
      target.append(element("p", copy("Edit a capture, review the exact record, then explicitly confirm. Feedback never creates a task.", "Отредактируйте ввод, проверьте точную запись и явно подтвердите. Отзыв не создаёт задачу."), "hint"));
    }
    if (!finding) return;
    target.append(element("h3", copy("Useful to you?", "Это полезно?")));
    if (!source.feedback?.available) {
      target.append(element("p", copy("Feedback is disabled, its private receipt changed, or the fourteen-day window expired. No choice will be saved.", "Отзывы отключены, личная квитанция изменилась или истёк четырнадцатидневный период. Выбор не будет сохранён."), "hint"));
      return;
    }
    const current = source.feedback.findings[finding.id];
    const actions = element("div", null, "actions");
    for (const [value, label] of [["useful", copy("Useful", "Полезно")], ["known", copy("Already know", "Уже знаю")], ["dismiss", copy("Not useful", "Не полезно")]]) {
      actions.append(writeButton(label, () => this.saveFeedback(source, finding, value)));
    }
    target.append(actions);
    if (current.value) target.append(element("p", `${copy("Saved", "Сохранено")}: ${current.value.text}. ${copy("Recorded", "Записано")} ${current.value.recorded_on}.`, "hint"));
    const details = element("details");
    details.append(element("summary", copy("Snooze this finding", "Отложить этот вывод")));
    const form = element("form", null, "feedback-form");
    const label = element("label", copy("Reconsider on (UTC date)", "Пересмотреть (дата UTC)"));
    label.htmlFor = "finding-snooze";
    const input = element("input"); input.type = "date"; input.id = "finding-snooze"; input.required = true;
    const submit = element("button", copy("Save snooze", "Сохранить отсрочку")); submit.type = "submit";
    append(form, label, input, element("p", copy(`Choose a future date before ${source.feedback.expires_on}. Feedback expires with the review.`, `Выберите будущую дату до ${source.feedback.expires_on}. Отзыв истекает вместе с обзором.`), "hint"), submit);
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
