from __future__ import annotations

import copy
import html
from datetime import date
from html.parser import HTMLParser
from typing import Any

import pytest

from briefing_plan import PlanError, model_input
from weekly_plan import (
    TELEGRAM_LIMIT,
    render_weekly,
    render_weekly_proposal,
    render_weekly_sources,
    validate_weekly_plan,
    weekly_plan_schema,
)

TODAY = date(2026, 9, 21)
START = date(2026, 9, 15)


def source(path: str, kind: str, title: str) -> dict:
    return {
        "path": path, "kind": kind, "title": title, "text": "Synthetic public evidence.",
        "revision": "a" * 40, "digest": "b" * 64,
        "url": f"https://github.com/example/vault/blob/main/{path}",
    }


@pytest.fixture
def context() -> dict:
    records = [
        source("tasks/compare.md", "task", "Compare learning methods"),
        source("ideas/practice.md", "idea", "Try spaced practice"),
        source("notes/evidence.md", "note", "Learning research"),
        source("home.md", "goal", "Choose a learning method"),
    ]
    return {
        "date": TODAY.isoformat(), "sources": records, "tasks": [],
        "changes": [{**records[1], "change_kind": "modified"},
                    {**records[2], "change_kind": "newly_available"}],
        "goals": [records[3]], "warnings": [], "complete": True,
        "source_status": "available", "extras": {"freshness": {"status": "current"}},
    }


def raw_plan(context: dict) -> dict:
    return {
        "focus": {"path": context["sources"][3]["path"], "text": "Choose one learning method."},
        "changes": [
            {"path": item["path"], "why": "The comparison now includes spaced practice."}
            for item in context["changes"]
        ],
        "proposals": [
            {"kind": kind, "source_path": item["path"], "text": text,
             "why": "Choose a small, evidence-backed next step."}
            for kind, item, text in zip(
                ("review_task", "create_task", "research"), context["sources"],
                ("Compare the two methods.", "Draft a practice experiment.",
                 "Which public studies compare these methods?"),
            )
        ],
    }


def validate(raw: dict, context: dict) -> dict:
    return validate_weekly_plan(
        raw, context, TODAY, evidence_paths={item["path"] for item in context["sources"]},
    )


def render(plan: dict, context: dict, **kwargs: Any) -> str:
    return render_weekly(
        plan, context, kwargs.pop("activity", []), kwargs.pop("pending", []),
        TODAY, START, has_baseline=kwargs.pop("has_baseline", True), **kwargs,
    )


class TelegramHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.text: list[str] = []
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        assert tag in {"b", "a", "code"}
        self.stack.append(tag)
        if tag == "a":
            self.links.append(dict(attrs)["href"] or "")

    def handle_endtag(self, tag: str) -> None:
        assert self.stack.pop() == tag

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def parsed(text: str) -> TelegramHTML:
    parser = TelegramHTML()
    parser.feed(text)
    parser.close()
    assert not parser.stack
    return parser


def telegram_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def test_schema_preserves_source_and_kind_constraints(context: dict) -> None:
    packet = model_input(context, [])
    original = copy.deepcopy(packet)
    schema = weekly_plan_schema(packet)
    properties = schema["properties"]

    assert set(properties) == {"focus", "changes", "proposals"}
    assert schema["required"] == ["focus", "changes", "proposals"]
    assert schema["additionalProperties"] is False
    assert properties["changes"]["maxItems"] == 2
    assert properties["proposals"]["maxItems"] == 3
    assert set(properties["focus"]["anyOf"][1]["properties"]["path"]["enum"]) == {
        item["path"] for item in context["sources"]
    }
    assert properties["changes"]["items"]["properties"]["path"]["enum"] == [
        "ideas/practice.md", "notes/evidence.md",
    ]
    variants = properties["proposals"]["items"]["anyOf"]
    assert all(variant["type"] == "object" for variant in variants)
    assert all(variant["additionalProperties"] is False for variant in variants)
    paths = {
        variant["properties"]["kind"]["enum"][0]:
        variant["properties"]["source_path"]["enum"]
        for variant in variants
    }
    assert paths == {
        "review_task": ["tasks/compare.md"],
        "create_task": ["ideas/practice.md"],
        "research": ["ideas/practice.md", "notes/evidence.md"],
    }
    assert packet == original


def test_schema_requires_empty_array_when_no_action_source_is_allowed() -> None:
    for packet in ({}, {"sources": [{"path": "home.md", "kind": "goal"}]}):
        schema = weekly_plan_schema(packet)
        assert schema["properties"]["proposals"]["maxItems"] == 0
        assert "proposal" not in schema["properties"]
        assert schema["properties"]["changes"]["maxItems"] == 0
    assert weekly_plan_schema({})["properties"]["focus"] == {"type": "null"}


@pytest.mark.parametrize("slots", [0, 1, 2, 3])
def test_schema_reserves_slots_for_current_pending_decisions(context: dict, slots: int) -> None:
    packet = {**model_input(context, []), "proposal_slots": slots, "review_kind": "weekly"}
    schema = weekly_plan_schema(packet)
    assert schema["properties"]["proposals"]["maxItems"] == slots
    assert schema["properties"]["proposals"]["items"]["anyOf"]


@pytest.mark.parametrize("slots", [-1, 4, True, None, "2"])
def test_schema_rejects_invalid_proposal_slot_counts(slots: object) -> None:
    with pytest.raises(PlanError, match="invalid_weekly_proposal_slots"):
        weekly_plan_schema({"proposal_slots": slots})


def test_empty_plan_validates_without_sources() -> None:
    plan = validate_weekly_plan(
        {"focus": None, "changes": [], "proposals": []}, {}, TODAY, evidence_paths=set(),
    )
    assert isinstance(plan["focus"], str)
    assert plan["changes"] == plan["proposals"] == []


def test_normalization_retains_three_independent_actions_without_mutation(context: dict) -> None:
    raw = raw_plan(context)
    original = copy.deepcopy((raw, context))

    plan = validate(raw, context)

    assert len(plan["proposals"]) == 3
    assert len({item["id"] for item in plan["proposals"]}) == 3
    assert plan["focus"].endswith("https://github.com/example/vault/blob/main/home.md")
    assert all(item["status"] == "pending" for item in plan["proposals"])
    assert (raw, context) == original


def test_four_actions_are_rejected_before_normalization(context: dict) -> None:
    raw = raw_plan(context)
    raw["proposals"].append(copy.deepcopy(raw["proposals"][0]))
    with pytest.raises(PlanError, match="invalid_weekly_proposals"):
        validate(raw, context)


def test_same_source_cannot_have_two_kinds_of_proposal(context: dict) -> None:
    raw = raw_plan(context)
    raw["proposals"] = [
        raw["proposals"][1],
        {**raw["proposals"][1], "kind": "research"},
    ]
    with pytest.raises(PlanError, match="duplicate_weekly_source"):
        validate(raw, context)


@pytest.mark.parametrize("field", ["focus", "changes", "proposals"])
def test_evidence_omitted_from_packet_cannot_be_used(context: dict, field: str) -> None:
    raw = {"focus": None, "changes": [], "proposals": []}
    raw[field] = raw_plan(context)[field]
    with pytest.raises(PlanError, match="unbacked"):
        validate_weekly_plan(raw, context, TODAY, evidence_paths=set())


@pytest.mark.parametrize("bad", [None, {}, "proposal", [None], [1], [{"kind": "research"}]])
def test_malformed_proposals_are_rejected(context: dict, bad: object) -> None:
    raw = raw_plan(context)
    raw["proposals"] = bad
    with pytest.raises(PlanError):
        validate(raw, context)


def test_unknown_root_keys_and_excess_changes_are_rejected(context: dict) -> None:
    raw = raw_plan(context)
    with pytest.raises(PlanError, match="invalid_weekly_plan"):
        validate({**raw, "debug": "untrusted"}, context)
    raw["changes"].append(copy.deepcopy(raw["changes"][0]))
    with pytest.raises(PlanError, match="invalid_changes"):
        validate(raw, context)


def test_first_review_has_baseline_not_invented_deltas_or_completion(context: dict) -> None:
    text = render(validate(raw_plan(context), context), context, has_baseline=False)

    assert "2026-09-15 to 2026-09-21" in text
    assert "First review with this format" in text
    assert "Changed since last review" not in text
    assert "New to this review: Try spaced practice" in text
    assert "No verified outcomes recorded in this window; that does not mean no progress." in text
    assert "Result verified" not in text
    assert "created this week" not in text


def test_current_source_focus_survives_independently_stale_mirror(context: dict) -> None:
    context["extras"] = {
        "freshness": {"status": "stale", "age_days": 52},
        "signals": ["PRIVATE MIRROR FACT MUST NOT APPEAR"],
    }
    text = render(validate(raw_plan(context), context), context)

    assert text.index("Limited information") < text.index("What changed")
    assert "Your personal snapshot is 52 days old" in text
    assert "separate from your connected notes" in text
    assert "Current connected notes were read for this review." in text
    assert "canonical" not in text and "Private mirror" not in text
    assert "Choose one learning method." in text
    assert "PRIVATE MIRROR FACT" not in text
    assert "Changed since last review: Try spaced practice" in text


def test_bounded_connected_notes_are_not_reported_as_missing(context: dict) -> None:
    context["complete"] = False
    context["coverage"] = {"candidate_files": 150, "read_files": 16, "included_notes": 11}
    context["warnings"] = [
        "Initial source baseline; these records are not changes since yesterday.",
        "Some source excerpts are shortened; source links retain the full permitted context.",
        "Source context is bounded; some eligible records remain unprocessed and may have changed.",
    ]
    text = render(validate(raw_plan(context), context), context, has_baseline=False)
    assert "Connected notes are reachable" in text
    assert "missing or could not be read" not in text
    assert "/review sources" in text
    assert "More information gaps could not fit here" not in text
    assert telegram_units(text) <= TELEGRAM_LIMIT
    parsed(text)


def test_unselected_snapshot_does_not_falsely_report_unknown_freshness(context: dict) -> None:
    context["extras"]["freshness"] = {"status": "not_requested"}
    text = render(validate(raw_plan(context), context), context)
    assert "The private snapshot is not selected" in text
    assert "unknown" not in text and "Limited information" not in text


def test_source_check_preserves_all_warnings_without_private_content(context: dict) -> None:
    context["coverage"] = {"candidate_files": 150, "read_files": 16, "included_notes": 11}
    context["complete"] = False
    context["warnings"] = ["First constraint.", "Second constraint.", "Third constraint.", "w" * 180]
    context["extras"] = {"freshness": {"status": "stale", "age_days": 53}, "signals": ["PRIVATE SNAPSHOT FACT"]}
    context["inventory_paths"] = ["PRIVATE PATH"]
    text = render_weekly_sources(context, {"date": TODAY.isoformat()})
    assert "Read 16 of 150 candidate files; 11 usable notes" in text
    assert all(warning in text for warning in context["warnings"])
    assert "Snapshot sync is manual" in text
    assert "Last successfully delivered weekly baseline: 2026-09-21" in text
    assert "Missing outcomes do not establish inactivity" in text
    assert "404" in text and "personal GitHub account" in text
    assert "PRIVATE" not in text
    assert all(item["text"] not in text and item["path"] not in text for item in context["sources"])


def test_source_check_discloses_missing_task_access_and_first_comparison(context: dict) -> None:
    context["open_loops"] = {"status": "unavailable"}
    text = render_weekly_sources(context, {})
    assert "Task state is unavailable, not an empty task list." in text
    assert "first successful review starts the comparison" in text


@pytest.mark.parametrize("freshness", ["missing", "unknown"])
def test_missing_or_unknown_mirror_is_never_claimed_current(context: dict, freshness: str) -> None:
    context["extras"]["freshness"] = {"status": freshness}
    context["source_status"] = "unavailable"
    context["complete"] = False
    text = render(validate(raw_plan(context), context), context)
    assert "Limited information" in text
    assert "missing or could not be read" in text
    assert "Current connected notes were read" not in text


@pytest.mark.parametrize(("status", "label"), [
    ("completed", "Result verified"),
    ("submitted", "Work submitted - awaiting verification"),
    ("failed", "Failed"),
    ("uncertain", "Unconfirmed"),
    ("executing", "In progress - unconfirmed"),
    ("accepted", "Next step selected - task still open"),
    ("snoozed", "Review deferred"),
    ("declined", "Dismissed"),
])
def test_activity_dates_describe_observations_not_task_completion(
    context: dict, status: str, label: str,
) -> None:
    text = render(validate(raw_plan(context), context), context, activity=[
        {"date": "2026-09-18", "status": status, "text": "Compare public methods.",
         "url": "https://github.com/example/vault/pull/7"},
    ])
    assert f"2026-09-18 — {label}" in text
    assert "when updates were noted, not necessarily when the work happened" in text
    assert "task completed" not in text.lower()
    assert ("No verified outcomes recorded" in text) == (status != "completed")


def test_activity_is_windowed_sorted_bounded_and_remainder_disclosed(context: dict) -> None:
    observations = [
        {"date": day, "status": "completed", "text": f"Observation {index}.", "url": ""}
        for index, day in enumerate([
            "2026-09-01", "2026-09-16", "2026-09-19", "2026-09-20",
            "2026-09-21", "2026-09-22", "invalid",
        ])
    ]
    text = render(validate(raw_plan(context), context), context, activity=observations)
    assert text.index("2026-09-21 —") < text.index("2026-09-20 —") < text.index("2026-09-19 —")
    assert text.count("Result verified") == 3
    assert "Observation 0" not in text and "Observation 5" not in text
    assert "More recorded actions: /proposals all." in text


def test_pending_decision_is_named_with_remainder_without_claiming_due_task_listing(context: dict) -> None:
    pending = [
        {"text": "Choose comparison criteria.", "status": "pending", "id": "hidden-identifier"},
        {"text": "Draft the learning experiment.", "status": "pending"},
    ]
    text = render(validate(raw_plan(context), context), context, pending=pending)
    assert "Waiting for your decision: Choose comparison criteria." in text
    assert "Other waiting decisions: /proposals all." in text
    assert "not every due task" in text
    assert "hidden-identifier" not in text


@pytest.mark.parametrize(("status", "heading", "label"), [
    ("failed", "Needs attention", "Failed"),
    ("uncertain", "Needs attention", "Unconfirmed"),
    ("executing", "Still waiting", "In progress - unconfirmed"),
    ("submitted", "Still waiting", "Work submitted - awaiting verification"),
])
def test_old_approved_work_stays_visible_without_a_weekly_transition(
    context: dict, status: str, heading: str, label: str,
) -> None:
    context["open_actions"] = [{
        "text": "Compare the public learning studies.", "status": status,
        "approved_on": "2026-08-01", "url": "https://github.com/example/vault/pull/7",
    }]
    text = render(validate(raw_plan(context), context), context)
    follow_up = text.split(f"<b>{heading}</b>", 1)[1].split("<b>Your choices</b>", 1)[0]
    changed = text.split("<b>What changed</b>", 1)[1].split("<b>One focus</b>", 1)[0]

    assert f"{label}: Compare the public learning studies." in follow_up
    assert "Full approved action: /proposals all." in follow_up
    assert "2026-08-01" not in text
    assert "Compare the public learning studies." not in changed
    assert "Result verified" not in follow_up
    assert "No verified outcomes recorded in this window" in text
    assert "https://github.com/example/vault/pull/7" in parsed(follow_up).links


def test_highest_concern_is_selected_and_other_unresolved_work_is_disclosed(context: dict) -> None:
    context["open_actions"] = [
        {"text": title, "status": status, "approved_on": "2026-08-01", "url": ""}
        for status, title in [
            ("submitted", "Await verification."), ("executing", "Running research."),
            ("uncertain", "Check the uncertain handoff."), ("failed", "Repair the research handoff."),
        ]
    ]
    text = render(validate(raw_plan(context), context), context)
    follow_up = text.split("<b>Needs attention</b>", 1)[1].split("<b>Your choices</b>", 1)[0]

    assert "Failed: Repair the research handoff." in follow_up
    assert "Await verification." not in follow_up
    assert "Running research." not in follow_up
    assert "Other unresolved approved work and full details: /proposals all." in follow_up
    assert follow_up.count("• ") == 1


def test_awaiting_approval_and_finished_work_are_not_reclassified_as_open_execution(context: dict) -> None:
    context["open_actions"] = [
        {"status": status, "text": "Not unresolved execution.", "url": ""}
        for status in ("pending", "completed", "accepted", "declined", "snoozed")
    ]
    text = render(validate(raw_plan(context), context), context, pending=[
        {"status": "pending", "text": "Choose a comparison question."},
    ])

    assert "Waiting for your decision: Choose a comparison question." in text
    assert "<b>Needs attention</b>" not in text
    assert "<b>Still waiting</b>" not in text
    assert "Not unresolved execution." not in text


def test_open_work_is_html_escaped_and_cannot_inject_a_link(context: dict) -> None:
    context["open_actions"] = [{
        "status": "uncertain", "text": "<unsafe> & handoff", "url": "javascript:bad()",
    }]
    text = render(validate(raw_plan(context), context), context)
    follow_up = text.split("<b>Needs attention</b>", 1)[1].split("<b>Your choices</b>", 1)[0]

    assert "&lt;unsafe&gt; &amp; handoff" in follow_up
    assert not parsed(follow_up).links


def test_daily_packet_warning_does_not_claim_weekly_due_task_inventory(context: dict) -> None:
    context["warnings"] = [
        "6 source records were not included in the model evidence packet; "
        "due tasks are still listed independently."
    ]
    text = render(validate(raw_plan(context), context), context)
    assert "6 notes were not included in this review" in text
    assert "not a complete task inventory" in text
    assert "due tasks are still listed independently" not in text


def test_due_tasks_show_real_dates_and_links_with_at_most_three_rows(context: dict) -> None:
    context["tasks"] = [
        {**source(f"tasks/item-{index}.md", "task", f"Task {index}"),
         "deadline": "2026-09-24", "review_on": "2026-09-20"}
        for index in range(5)
    ]
    text = render(validate(raw_plan(context), context), context)
    task_section = text.split("<b>Date-relevant tasks</b>", 1)[1].split("<b>Your choices</b>", 1)[0]

    assert task_section.count("• ") == 3
    assert task_section.count("deadline 2026-09-24, review 2026-09-20") == 3
    assert all(f"Task {index}" in task_section for index in range(3))
    assert "Task 3" not in task_section and "Task 4" not in task_section
    assert "More due items are in the task sources:" in task_section
    assert "https://github.com/example/vault/tree/main" in parsed(task_section).links
    assert "/proposals" not in task_section


def test_task_due_selection_excludes_unrelated_future_or_undated_tasks(context: dict) -> None:
    context["tasks"] = [
        {**source("tasks/future.md", "task", "Future only"), "deadline": "2026-10-20"},
        {**source("tasks/review.md", "task", "Future review"), "review_on": "2026-09-22"},
        source("tasks/undated.md", "task", "No date"),
        {**source("tasks/today.md", "task", "Focus today"), "focus_on": TODAY.isoformat()},
    ]
    text = render(validate(raw_plan(context), context), context)

    assert "Focus today" in text and "focus 2026-09-21" in text
    assert "Future only" not in text and "Future review" not in text and "No date" not in text
    assert "More due items" not in text


def test_unlinked_due_remainder_is_disclosed_without_inventing_navigation(context: dict) -> None:
    context["tasks"] = [
        {**source(f"tasks/item-{index}.md", "task", f"Task {index}"),
         "deadline": "2026-09-24", "url": "javascript:bad()"}
        for index in range(5)
    ]
    text = render(validate(raw_plan(context), context), context)
    task_section = text.split("<b>Date-relevant tasks</b>", 1)[1].split("<b>Your choices</b>", 1)[0]

    assert "Other date-relevant tasks are not shown" in task_section
    assert "complete task-source link is unavailable" in task_section
    assert "More due items are in the task sources" not in task_section
    assert not parsed(task_section).links
    assert "/proposals" not in task_section


def test_task_source_directory_keeps_owner_repo_and_branch(context: dict) -> None:
    context["tasks"] = [
        {**source(f"tasks/item-{index}.md", "task", f"Task {index}"),
         "deadline": "2026-09-24",
         "url": f"https://github.com/blob/vault/blob/review/branch/tasks/item-{index}.md"}
        for index in range(4)
    ]
    text = render(validate(raw_plan(context), context), context)
    assert "https://github.com/blob/vault/tree/review/branch" in parsed(text).links


def test_due_task_titles_are_escaped_and_invalid_dates_are_not_inferred(context: dict) -> None:
    context["tasks"] = [
        {**source("tasks/escaped.md", "task", "<unsafe> & title"), "deadline": "2026-09-24"},
        {**source("tasks/bad.md", "task", "Unreadable date"), "deadline": "not-a-date"},
    ]
    text = render(validate(raw_plan(context), context), context)

    assert "&lt;unsafe&gt; &amp; title" in text
    assert "Some task dates could not be read" in text
    assert "Unreadable date" not in text and "not-a-date" not in text
    parsed(text)


def test_due_tasks_cannot_displace_limitations_or_overflow_largest_summary(context: dict) -> None:
    raw = raw_plan(context)
    raw["focus"]["text"] = "f" * 500
    for item in raw["changes"]:
        item["why"] = "&" * 500
    context["warnings"] = ["w" * 160] * 3
    context["extras"]["freshness"] = {"status": "stale", "age_days": 9999}
    context["tasks"] = [
        {**source(f"tasks/item-{index}.md", "task", "<" * 1000),
         "deadline": "2026-09-24", "review_on": "2026-09-20"}
        for index in range(20)
    ]
    observations = [
        {"date": TODAY.isoformat(), "status": "submitted", "text": "<" * 1000, "url": ""}
    ] * 5
    waiting = [{"status": "pending", "text": "<" * 1000}] * 5
    text = render(
        validate(raw, context), context, activity=observations,
        pending=waiting, has_baseline=False,
    )

    assert telegram_units(text) <= TELEGRAM_LIMIT
    assert "More source limits: /review sources" in text
    assert "Your personal snapshot is 9999 days old" in text
    assert "Some date-relevant tasks do not fit here" in text or "More due items" in text
    parsed(text)


def test_unresolved_work_remains_named_with_maximum_summary_and_due_details(context: dict) -> None:
    raw = raw_plan(context)
    raw["focus"]["text"] = "f" * 500
    context["sources"][3]["url"] = "https://github.com/example/vault/blob/main/" + "g" * 225
    for change in raw["changes"]:
        change["why"] = "<" * 500
    for item in context["sources"][:3]:
        item["title"] = "<" * 1000
        item["url"] = ""
    context["warnings"] = ["w" * 160] * 3
    context["extras"]["freshness"] = {"status": "stale", "age_days": 9999}
    context["tasks"] = [
        {**source(f"tasks/item-{index}.md", "task", "<" * 1000),
         "deadline": "2026-09-24", "review_on": "2026-09-20"}
        for index in range(5)
    ]
    context["open_actions"] = [
        {"status": "submitted", "text": "Verify the research report. " + "<" * 1000, "url": ""}
    ] * 5
    observations = [
        {"date": TODAY.isoformat(), "status": "submitted", "text": "<" * 1000, "url": ""}
    ] * 5
    waiting = [{"status": "pending", "text": "<" * 1000}] * 5
    text = render(
        validate(raw, context), context, activity=observations,
        pending=waiting, has_baseline=False,
    )

    assert telegram_units(text) <= TELEGRAM_LIMIT
    assert "Your personal snapshot is 9999 days old" in text
    assert "More source limits: /review sources" in text
    assert "<b>Still waiting</b>" in text and "Verify the research report." in text
    assert "Other unresolved approved work" in text
    assert "task list" in text or "More due items" in text
    parsed(text)


def test_summary_escapes_every_variable_and_hides_technical_identifiers(context: dict) -> None:
    raw = raw_plan(context)
    raw["focus"]["text"] = '<b>Compare</b> & choose "carefully".'
    raw["changes"][0]["why"] = '<a href="javascript:alert(1)">bad</a>'
    context["changes"][0]["title"] = "<unsafe>"
    context["sources"][1]["title"] = "<unsafe>"
    context["warnings"] = ["<script>bad</script> & limited."]
    plan = validate(raw, context)
    text = render(plan, context, pending=[
        {"status": "pending", "text": "<i>Decide</i>", "id": "secret-id"},
    ], activity=[
        {"date": TODAY.isoformat(), "status": "submitted", "text": "<img> & report",
         "url": "javascript:alert(1)"},
    ])
    result = parsed(text)
    visible = "".join(result.text)

    assert "<b>Compare</b>" in visible
    assert "<script>bad</script>" in visible
    assert "&lt;img&gt;" in text
    assert all(link.startswith("https://github.com/") for link in result.links)
    assert "tasks/compare.md" not in visible
    assert all(item["id"] not in visible for item in plan["proposals"])
    assert "a" * 40 not in visible and "b" * 64 not in visible


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "data:text/html,bad", "http://github.com/example/vault/issues/1",
    "https://github.com.evil.invalid/example/vault/issues/1",
    "https://github.com@evil.invalid/example/vault/issues/1",
    "https://evil.invalid@github.com/example/vault/issues/1",
    "https://github.com:443/example/vault/issues/1",
    'https://github.com/example/vault/issues/1"><b>bad</b>',
    "https://github.com/example/vault/issues/%0a1",
    "https://github.com/example/vault/issues/1?token=hidden",
    "https://github.com/login",
])
def test_arbitrary_and_injected_links_are_not_rendered(context: dict, url: str) -> None:
    plan = validate(raw_plan(context), context)
    proposal = {**plan["proposals"][0], "source_url": url}
    card, _ = render_weekly_proposal(proposal, 1, 1)
    assert not parsed(card).links
    assert "Source link unavailable." in card
    plan["focus"] = "Choose a method.\nSource: " + url
    plan["changes"] = []
    assert not parsed(render(plan, context)).links


@pytest.mark.parametrize(("index", "button", "scope"), [
    (0, "Select next step", "does not complete it"),
    (1, "Draft task", "draft task for review"),
    (2, "Start research", "at most 5 sources"),
])
def test_each_card_preserves_exact_action_and_truthful_scope(
    context: dict, index: int, button: str, scope: str,
) -> None:
    proposal = validate(raw_plan(context), context)["proposals"][index]
    text, keyboard = render_weekly_proposal(proposal, index + 1, 3)

    assert proposal["text"] in "".join(parsed(text).text)
    assert scope in text
    assert "change: your correction" in text and "snooze YYYY-MM-DD" in text
    assert "Use the button or reply approve to this card. Other actions stay unchanged." in text
    assert "Only its own button" not in text
    assert proposal["id"] not in text
    assert keyboard == [
        [{"text": button, "callback_data": f"brief1|approve|{proposal['id']}"}],
        [{"text": "Dismiss", "callback_data": f"brief1|decline|{proposal['id']}"},
         {"text": "Why this?", "callback_data": f"brief1|explain|{proposal['id']}"}],
    ]


def test_existing_pending_task_revision_renders_exact_edit_not_selection(context: dict) -> None:
    proposal = validate(raw_plan(context), context)["proposals"][0]
    new_action = "Compare <three> public studies & record the differences. 🧭"
    proposal.update(
        kind="update_task", text=new_action,
        action={
            "kind": "update_task", "path": proposal["source_path"],
            "change": {"next_action": new_action},
        },
    )
    text, keyboard = render_weekly_proposal(proposal, 1, 1)
    visible = "".join(parsed(text).text)

    assert "Set next action to:\n" + new_action in visible
    assert "Prepare one edit for review" in visible
    assert "Only the next-action field changes" in visible
    assert "the task is not completed" in visible
    assert "Select this next action only" not in visible
    assert keyboard[0] == [{
        "text": "Approve edit", "callback_data": f"brief1|approve|{proposal['id']}",
    }]
    assert "Use the button or reply approve to this card. Other actions stay unchanged." in visible
    assert telegram_units(text) <= TELEGRAM_LIMIT


def test_new_model_task_edits_remain_forbidden(context: dict) -> None:
    packet = model_input(context, [])
    variants = weekly_plan_schema(packet)["properties"]["proposals"]["items"]["anyOf"]
    assert all("update_task" not in variant["properties"]["kind"]["enum"] for variant in variants)
    raw = raw_plan(context)
    raw["proposals"][0]["kind"] = "update_task"
    with pytest.raises(PlanError, match="unsupported_action"):
        validate(raw, context)


@pytest.mark.parametrize("change", [
    {"status": "done"},
    {"next_action": "Choose a method.", "status": "done"},
    {},
])
def test_task_edit_card_rejects_hidden_or_unsupported_mutations(context: dict, change: dict) -> None:
    proposal = validate(raw_plan(context), context)["proposals"][0]
    proposal.update(
        kind="update_task",
        action={"kind": "update_task", "path": proposal["source_path"], "change": change},
    )
    with pytest.raises(PlanError, match="unsupported_task_edit"):
        render_weekly_proposal(proposal, 1, 1)


def test_task_edit_preview_cannot_disagree_with_executed_value(context: dict) -> None:
    proposal = validate(raw_plan(context), context)["proposals"][0]
    proposal.update(
        kind="update_task",
        action={
            "kind": "update_task", "path": proposal["source_path"],
            "change": {"next_action": "A different change than the displayed text."},
        },
    )
    with pytest.raises(PlanError, match="task_edit_preview_mismatch"):
        render_weekly_proposal(proposal, 1, 1)


def test_maximum_valid_lengths_keep_full_action_and_complete_html(context: dict) -> None:
    raw = raw_plan(context)
    raw["focus"]["text"] = "f" * 500
    for item in raw["changes"]:
        item["why"] = "w" * 500
    for item in raw["proposals"]:
        item["text"], item["why"] = "x" * 700, "y" * 500
    plan = validate(raw, context)

    for index, proposal in enumerate(plan["proposals"], 1):
        card, _ = render_weekly_proposal(proposal, index, 3)
        assert telegram_units(card) <= TELEGRAM_LIMIT
        assert proposal["text"] in "".join(parsed(card).text)
    assert telegram_units(render(plan, context)) <= TELEGRAM_LIMIT


def test_escaped_summary_expansion_and_large_inputs_remain_bounded(context: dict) -> None:
    raw = raw_plan(context)
    raw["focus"]["text"] = "&" * 500
    for item in raw["changes"]:
        item["why"] = "<" * 500
    for item in context["sources"]:
        item["title"] = "<" * 10000
        item["url"] = "https://github.com/example/vault/blob/main/" + "x" * 1000
    context["warnings"] = ["Important qualifier " + "&" * 10000] * 100
    context["extras"]["freshness"] = {"status": "stale", "age_days": 9999}
    plan = validate(raw, context)
    pending = [{"status": "pending", "text": "<" * 10000}] * 100
    observations = [
        {"date": TODAY.isoformat(), "status": "submitted", "text": "<" * 10000,
         "url": "https://github.com/example/vault/issues/" + "1" * 1000}
    ] * 100
    text = render(plan, context, pending=pending, activity=observations, has_baseline=False)

    assert telegram_units(text) <= TELEGRAM_LIMIT
    parsed(text)
    assert "More source limits: /review sources" in text
    assert "This review is incomplete" in text
    assert "More recorded actions: /proposals all." in text
    assert "Other waiting decisions: /proposals all." in text
    assert "full recommendation does not fit" in text
    assert "consult linked or connected sources" in text


def test_card_rationale_can_shorten_but_never_approved_action_or_scope(context: dict) -> None:
    proposal = validate(raw_plan(context), context)["proposals"][0]
    proposal["text"] = "<" * 700
    proposal["why"] = "&" * 500
    text, _ = render_weekly_proposal(proposal, 1, 1)

    assert telegram_units(text) <= TELEGRAM_LIMIT
    assert proposal["text"] in "".join(parsed(text).text)
    assert "Rationale abbreviated to fit" in text
    assert "does not complete it" in text


def test_unrenderable_exact_action_is_rejected_not_silently_shortened(context: dict) -> None:
    raw = raw_plan(context)
    raw["proposals"][0]["text"] = "&" * 700
    raw["proposals"][0]["why"] = "&" * 500
    with pytest.raises(PlanError, match="weekly_card_too_long"):
        validate(raw, context)


def test_emoji_and_escaped_card_keeps_exact_action_within_utf16_limit(context: dict) -> None:
    raw = raw_plan(context)
    raw["proposals"] = [raw["proposals"][0]]
    raw["proposals"][0]["text"] = "😀" * 300 + "&" * 400
    raw["proposals"][0]["why"] = "🧭" * 500
    proposal = validate(raw, context)["proposals"][0]
    text, _ = render_weekly_proposal(proposal, 1, 1)

    assert telegram_units(text) <= TELEGRAM_LIMIT
    assert telegram_units(text) > len(text)
    assert proposal["text"] in "".join(parsed(text).text)
    assert "Rationale abbreviated to fit" in text
    assert "does not complete it" in text


def test_emoji_rich_worst_case_summary_uses_utf16_budgets_for_every_section(context: dict) -> None:
    raw = raw_plan(context)
    raw["focus"]["text"] = "😀" * 250
    context["sources"][3]["url"] = "https://github.com/example/vault/blob/main/" + "g" * 225
    for change in raw["changes"]:
        change["why"] = "🧭" * 500
    for item in context["sources"][:3]:
        item["title"], item["url"] = "🌿" * 1000, ""
    context["warnings"] = ["🟡" * 80] * 3
    context["extras"]["freshness"] = {"status": "stale", "age_days": 9999}
    context["tasks"] = [
        {**source(f"tasks/item-{index}.md", "task", "🍃" * 1000),
         "deadline": "2026-09-24", "review_on": "2026-09-20"}
        for index in range(5)
    ]
    context["open_actions"] = [
        {"status": "submitted", "text": "Verify the report. " + "📖" * 1000, "url": ""}
    ] * 5
    observations = [
        {"date": TODAY.isoformat(), "status": "submitted", "text": "📚" * 1000, "url": ""}
    ] * 5
    waiting = [{"status": "pending", "text": "🌻" * 1000}] * 5
    text = render(
        validate(raw, context), context, activity=observations,
        pending=waiting, has_baseline=False,
    )

    assert telegram_units(text) <= TELEGRAM_LIMIT
    assert telegram_units(text) > len(text)
    assert "Your personal snapshot is 9999 days old" in text
    assert "More source limits: /review sources" in text
    assert "Verify the report." in text
    assert "Other unresolved approved work" in text
    parsed(text)


def test_escaped_link_markup_is_bounded_before_budgeting_other_fields(context: dict) -> None:
    proposal = validate(raw_plan(context), context)["proposals"][0]
    proposal["source_url"] = "https://github.com/example/vault/blob/main/" + "&" * 150
    text, _ = render_weekly_proposal(proposal, 1, 1)

    assert "Source link unavailable." in text
    assert not parsed(text).links
    assert telegram_units(text) <= TELEGRAM_LIMIT


@pytest.mark.parametrize(("field", "limit"), [("text", 700), ("why", 500)])
def test_overlong_card_fields_are_rejected(context: dict, field: str, limit: int) -> None:
    proposal = validate(raw_plan(context), context)["proposals"][0]
    proposal[field] = "x" * (limit + 1)
    with pytest.raises(PlanError, match="invalid_text"):
        render_weekly_proposal(proposal, 1, 1)


def test_callback_delimiter_injection_is_rejected(context: dict) -> None:
    proposal = validate(raw_plan(context), context)["proposals"][0]
    proposal["id"] = "id|approve|other"
    with pytest.raises(PlanError, match="invalid_weekly_callback"):
        render_weekly_proposal(proposal, 1, 1)


def test_typical_summary_is_concise_and_action_first(context: dict) -> None:
    text = render(validate(raw_plan(context), context), context, activity=[
        {"date": TODAY.isoformat(), "status": "completed", "text": "Compared two public studies.",
         "url": "https://github.com/example/vault/pull/7"},
    ])
    visible = html.unescape("".join(parsed(text).text))
    assert len(visible.split()) <= 230
    assert "Compared two public studies." in visible
    assert "One focus" in visible
