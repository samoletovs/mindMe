"""Pure, source-bound weekly plans and bounded Telegram HTML presentation."""

from __future__ import annotations

import re
from datetime import date
from typing import Any
from urllib.parse import quote, urlsplit

from briefing_plan import PlanError, plan_schema, safe_text, task_due, validate_plan
from telegram_format import (
    escape as _escape, excerpt as _excerpt, github_link as _github_link,
    units as _units,
)

MAX_WEEKLY_PROPOSALS = 3
TELEGRAM_LIMIT = 3900
_ACTIVITY_LABELS = {
    "completed": "Result verified",
    "submitted": "Work submitted - awaiting verification",
    "failed": "Failed",
    "uncertain": "Unconfirmed",
    "executing": "In progress - unconfirmed",
    "accepted": "Next step selected - task still open",
    "snoozed": "Review deferred",
    "declined": "Dismissed",
}
_OPEN_ACTION_PRIORITY = {"failed": 0, "uncertain": 1, "executing": 2, "submitted": 3}
_ACTIONS = {
    "research": (
        "Research", "Start research",
        "One public question, at most 5 sources, one short report; no further jobs. "
        "Uses existing agent capacity.",
    ),
    "create_task": (
        "Draft task", "Draft task",
        "Prepare one draft task for review. Approval does not instantly publish "
        "the task or complete any work.",
    ),
    "review_task": (
        "Next step", "Select next step",
        "Select this next action only. The existing task stays open; approval "
        "does not complete it.",
    ),
    "update_task": (
        "Edit task", "Approve edit",
        "Prepare one edit for review. Only the next-action field changes when "
        "that edit is applied; the task is not completed.",
    ),
}


def weekly_plan_schema(packet: dict[str, Any]) -> dict[str, Any]:
    """Keep the daily evidence enums, but allow up to three separate decisions."""
    slots = packet.get("proposal_slots", MAX_WEEKLY_PROPOSALS)
    if (
        not isinstance(slots, int) or isinstance(slots, bool)
        or not 0 <= slots <= MAX_WEEKLY_PROPOSALS
    ):
        raise PlanError("invalid_weekly_proposal_slots")
    schema = plan_schema(packet)
    singular = schema["properties"].pop("proposal")
    variants = [
        variant for variant in singular.get("anyOf", [])
        if variant.get("type") != "null"
    ]
    schema["properties"]["proposals"] = {
        "type": "array",
        "maxItems": slots if variants else 0,
        "items": {"anyOf": variants} if variants else {
            "type": "object", "additionalProperties": False,
            "properties": {}, "required": [],
        },
    }
    schema["required"] = ["focus", "changes", "proposals"]
    return schema


def validate_weekly_plan(
    raw: dict[str, Any], context: dict[str, Any], today: date,
    *, evidence_paths: set[str],
) -> dict[str, Any]:
    """Validate every action against the same supplied evidence, without I/O."""
    if not isinstance(raw, dict) or set(raw) != {"focus", "changes", "proposals"}:
        raise PlanError("invalid_weekly_plan")
    proposals = raw["proposals"]
    if not isinstance(proposals, list) or len(proposals) > MAX_WEEKLY_PROPOSALS:
        raise PlanError("invalid_weekly_proposals")
    base = validate_plan(
        {"focus": raw["focus"], "changes": raw["changes"], "proposal": None},
        context, today, evidence_paths=evidence_paths,
    )
    normalized = []
    seen: set[str] = set()
    for proposal in proposals:
        if proposal is None:
            raise PlanError("invalid_proposal")
        action = validate_plan(
            {"focus": None, "changes": [], "proposal": proposal},
            context, today, evidence_paths=evidence_paths,
        )["proposal"]
        if action["source_path"] in seen:
            raise PlanError("duplicate_weekly_source")
        seen.add(action["source_path"])
        # Never accept an action whose exact scope cannot fit its approval card.
        render_weekly_proposal(action, len(normalized) + 1, len(proposals))
        normalized.append(action)
    return {"focus": base["focus"], "changes": base["changes"], "proposals": normalized}


def _row(label: str, text: object, url: object, limit: int) -> tuple[str, bool]:
    link = _github_link(url)
    suffix = f" ({link})" if link else ""
    omitted = bool(url) and not link
    if _units(label) + _units(suffix) + 40 > limit:
        suffix = ""
        omitted = True
    excerpt, shortened = _excerpt(text, limit - _units(label) - _units(suffix))
    return label + excerpt + suffix, omitted or shortened


def _snapshot_notice(context: dict[str, Any]) -> str:
    freshness = (context.get("extras") or {}).get("freshness") or {}
    status = freshness.get("status", "unknown")
    if status == "not_requested":
        return "The private snapshot is not selected; no details from it are used here."
    mirror = {
        "current": "Your personal snapshot is current",
        "stale": "Your personal snapshot is out of date",
        "missing": "Your personal snapshot is unavailable",
    }.get(status, "The age of your personal snapshot is unknown")
    age = freshness.get("age_days")
    if status == "stale" and isinstance(age, int) and not isinstance(age, bool) and 0 <= age < 10000:
        mirror = f"Your personal snapshot is {age} days old"
    return (
        mirror + ". It is separate from your connected notes; "
        "no details from the snapshot are used here."
    )


def _source_notice(context: dict[str, Any]) -> str:
    freshness = (context.get("extras") or {}).get("freshness") or {}
    available = context.get("source_status") == "available"
    complete = available and context.get("complete") is True
    warnings = context.get("warnings") or []
    limited = freshness.get("status") not in {"current", "not_requested"} or not complete or bool(warnings)
    lines = ["<b>Limited information</b>" if limited else "<b>Sources</b>"]
    lines.append(_escape(_snapshot_notice(context)))
    lines.append(
        "Current connected notes were read for this review."
        if complete else
        "Connected notes are reachable, but this check is incomplete; unread notes may contain changes."
        if available else
        "Some connected notes are missing or could not be read; this is not a full account."
    )
    omitted = False
    # Never cut a warning mid-sentence and silently lose its qualification.
    for warning in warnings[:2]:
        if isinstance(warning, str):
            warning = warning.replace(
                "due tasks are still listed independently.",
                "this is not a complete task inventory.",
            ).replace(
                "Initial source baseline; these records are not changes since yesterday.",
                "First review with this format; these notes are not new progress.",
            ).replace("source records", "notes").replace("the model evidence packet", "this review")
        if isinstance(warning, str) and _units(_escape(warning)) <= 160:
            lines.append(_escape(warning))
        else:
            omitted = True
    if len(warnings) > 2 or omitted:
        lines.append(
            "More source limits: /review sources. This review is incomplete; check before deciding."
        )
    else:
        lines.append("Source checks and private-link help: /review sources.")
    return "\n".join(lines)


def render_weekly_sources(context: dict[str, Any], previous: dict[str, Any]) -> str:
    """Plain-text diagnostics: no source bodies, paths, decisions or hidden snapshot facts."""
    lines = ["Weekly review source check", "", _snapshot_notice(context)]
    freshness = (context.get("extras") or {}).get("freshness") or {}
    if freshness.get("status") not in {"current", "not_requested"}:
        lines.append(
            "Snapshot sync is manual. Refresh only the verified Personal OS folder from the laptop "
            "using the private sync tool; deploying the bot does not sync it. "
            "Do not upload extra private files just to clear this warning."
        )
    lines.extend(["", "Connected notes"])
    if context.get("source_status") == "available":
        coverage = context.get("coverage")
        if coverage:
            lines.append(
                f"Read {coverage['read_files']} of {coverage['candidate_files']} candidate files; "
                f"{coverage['included_notes']} usable notes selected after filtering."
            )
        lines.append(
            "The source read completed."
            if context.get("complete") is True else
            "The source read is incomplete. A reading limit is not a broken connection; "
            "unread candidates have not been checked for permission, relevance or changes."
        )
    else:
        lines.append("Connected notes could not be read. Check the configured GitHub access.")
    loops = context.get("open_loops")
    if loops:
        lines.extend(["", "Tasks"])
        if loops.get("status") != "available":
            lines.append("Task state is unavailable, not an empty task list.")
        else:
            lines.append(f"{len(context.get('tasks', []))} task records available to this check.")
            if loops.get("complete") is False:
                lines.append("The task inventory is incomplete.")
    lines.extend(["", "Comparison history"])
    if previous:
        lines.append(
            f"Last successfully delivered weekly baseline: {previous['date']}. "
            "Later reviews compare against it; daily briefings do not replace it."
        )
    else:
        lines.append("No delivered weekly baseline yet. The first successful review starts the comparison.")
    lines.append(
        "Recorded outcomes cover verified agent actions, not all your work. "
        "Missing outcomes do not establish inactivity."
    )
    warnings = context.get("warnings") or []
    if warnings:
        lines.extend(["", "All limits from this check"])
        lines.extend("- " + warning for warning in dict.fromkeys(warnings))
    lines.extend([
        "", "Opening source links",
        "These are private GitHub notes. Sign in with the personal GitHub account that can access "
        "your vault, including in Telegram's browser. A signed-out or different account may see 404. "
        "No sharing permissions need to change.",
        "", "This check used current sources and no AI generation. It did not refresh the private "
        "snapshot, save a comparison, change a decision, or start an action.",
    ])
    return "\n".join(lines)


def _focus(plan: dict[str, Any]) -> str:
    text = plan["focus"]
    recommendation, separator, url = text.rpartition("\nSource: ")
    if not separator:
        recommendation, url = text, ""
    link = _github_link(url)
    suffix = "\n" + (link or "No usable source link is available.")
    if _units(_escape(recommendation)) + _units(suffix) > 800:
        recommendation = (
            "The full recommendation does not fit this summary. "
            "Review its source before choosing a focus."
        )
    return "<b>One focus</b>\n" + _escape(recommendation) + suffix


def _task_source_index(tasks: list[dict[str, Any]]) -> str:
    indexes: set[str] = set()
    for task in tasks:
        url, path = task.get("url"), task.get("path")
        if not _github_link(url) or not isinstance(path, str) or not path:
            return ""
        suffix = "/" + quote(path, safe="/")
        if not url.endswith(suffix):
            return ""
        parts = urlsplit(url[:-len(suffix)]).path.split("/")
        if len(parts) < 5 or parts[3] != "blob":
            return ""
        parts[3] = "tree"
        indexes.add("https://github.com" + "/".join(parts))
    if len(indexes) != 1:
        return ""
    return _github_link(indexes.pop(), "Task sources")


def _due_items(context: dict[str, Any], today: date, budget: int) -> str:
    due = []
    invalid_dates = False
    for task in context.get("tasks", []):
        try:
            if not task_due(task, today):
                continue
            timing = [
                f"{label} {date.fromisoformat(task[key]).isoformat()}"
                for key, label in (("deadline", "deadline"), ("review_on", "review"), ("focus_on", "focus"))
                if task.get(key)
            ]
        except (TypeError, ValueError):
            invalid_dates = True
            continue
        due.append((task, ", ".join(timing)))
    if not due and not invalid_dates:
        return ""
    heading = "<b>Date-relevant tasks</b>"
    unavailable = "Other date-relevant tasks are not shown; a complete task-source link is unavailable here."
    index = _task_source_index([item for item, _ in due])
    remainder = f"More due items are in the task sources: {index}." if index else unavailable
    date_notice = "Some task dates could not be read; this due-task view is incomplete."
    lines = [heading]
    if invalid_dates:
        lines.append(date_notice)
    if _units("\n".join(lines)) + _units(remainder) + 1 > budget:
        remainder = "Some date-relevant tasks do not fit here; this is not a complete task list."
    shortened = False
    shown = 0
    for task, timing in due[:3]:
        row, cut = _row(f"• {timing}: ", task.get("title") or "Task", task.get("url"), 220)
        reserve = _units(remainder) + 1 if shown + 1 < len(due) else 0
        if _units("\n".join([*lines, row])) + reserve > budget:
            break
        lines.append(row)
        shown += 1
        shortened |= cut
    if shown < len(due):
        lines.append(remainder)
    elif shortened:
        notice = "Task titles are abbreviated; consult connected sources for full detail."
        if _units("\n".join([*lines, notice])) <= budget:
            lines.append(notice)
    text = "\n".join(lines)
    if _units(text) > budget:
        return "Date-relevant tasks could not fit here; the review is not a complete task list."
    return text


def _open_work(actions: list[dict[str, Any]], budget: int) -> str:
    action = min(actions, key=lambda item: _OPEN_ACTION_PRIORITY[item["status"]])
    status = action["status"]
    heading = "<b>Needs attention</b>" if status in {"failed", "uncertain"} else "<b>Still waiting</b>"
    footer = (
        "Other unresolved approved work and full details: /proposals all."
        if len(actions) > 1 else "Full approved action: /proposals all."
    )
    row, _ = _row(
        f"• {_ACTIVITY_LABELS[status]}: ", action.get("text"), action.get("url"),
        min(230, budget - _units(heading) - _units(footer) - 2),
    )
    return "\n".join([heading, row, footer])


def render_weekly(
    plan: dict[str, Any], context: dict[str, Any],
    activity: list[dict[str, Any]], pending: list[dict[str, Any]],
    today: date, start: date, *, has_baseline: bool,
) -> str:
    """Present observations, not inferred event dates, completion, or inactivity."""
    if start > today:
        raise PlanError("invalid_weekly_window")
    open_actions = [
        item for item in context.get("open_actions", [])
        if item.get("status") in _OPEN_ACTION_PRIORITY
    ]
    observed = []
    for item in activity:
        try:
            day = date.fromisoformat(item.get("date", ""))
        except (TypeError, ValueError):
            continue
        if start <= day <= today:
            observed.append({**item, "date": day.isoformat()})
    observed.sort(key=lambda item: item["date"], reverse=True)
    changes = ["<b>What changed</b>"]
    if not has_baseline:
        changes.append("First review with this format; there is no earlier review to compare.")
    if not any(item.get("status") == "completed" for item in observed):
        changes.append(
            "No verified outcomes recorded in this window; that does not mean no progress."
        )
    shortened = False
    if observed:
        changes.append("Dates show when updates were noted, not necessarily when the work happened.")
    for item in observed[:3]:
        label = _ACTIVITY_LABELS.get(item.get("status"), "Unconfirmed")
        row, omitted = _row(
            f"• {item['date']} — {label}: ", item.get("text"), item.get("url"),
            200 if open_actions else 230,
        )
        changes.append(row)
        shortened |= omitted
    for change in plan.get("changes", [])[:2]:
        source = change["source"]
        label = (
            "Changed since last review"
            if has_baseline and source.get("change_kind") == "modified"
            else "New to this review"
        )
        title, omitted = _excerpt(source.get("title") or "Source update", 75)
        detail, cut = _row(
            f"• {label}: {title} — ", change["why"], source.get("url"), 260,
        )
        changes.append(detail)
        shortened |= omitted or cut
    if len(observed) > 3:
        changes.append("More recorded actions: /proposals all.")
    if len(context.get("changes", [])) > len(plan.get("changes", [])):
        shortened = True
    waiting = [item for item in pending if item.get("status") == "pending"]
    decisions = ["<b>Your choices</b>"]
    if waiting:
        preview, cut = _excerpt(waiting[0].get("text"), 150)
        decisions.append("Waiting for your decision: " + preview)
        shortened |= cut
        if len(waiting) > 1:
            decisions.append("Other waiting decisions: /proposals all.")
    elif not plan.get("proposals"):
        decisions.append("No new approval is requested in this review.")
    if plan.get("proposals"):
        decisions.append(
            "Choose on each separate card; the exact action and scope are there. "
            "Nothing starts automatically."
        )
    decisions.append("/proposals all shows recorded actions and decisions, not every due task.")
    sections = [
        f"<b>Weekly review · {start.isoformat()} to {today.isoformat()}</b>",
        _source_notice(context), "\n".join(changes), _focus(plan), "\n".join(decisions),
    ]
    if shortened:
        sections.append(
            "Some detail is abbreviated or unlinked here; consult linked or connected sources "
            "for full context."
        )
    if open_actions:
        reserve = 100 if context.get("tasks") else 0
        follow_up = _open_work(
            open_actions, min(320, TELEGRAM_LIMIT - _units("\n\n".join(sections)) - reserve - 2),
        )
        sections.insert(4, follow_up)
    due = _due_items(context, today, min(850, TELEGRAM_LIMIT - _units("\n\n".join(sections)) - 2))
    if due:
        sections.insert(5 if open_actions else 4, due)
    text = "\n\n".join(sections)
    # UTF-16 field budgets and row caps protect HTML, surrogate pairs, and meaning.
    if _units(text) > TELEGRAM_LIMIT:
        raise PlanError("weekly_summary_too_long")
    return text


def render_weekly_proposal(
    proposal: dict[str, Any], number: int, total: int,
) -> tuple[str, list[list[dict[str, str]]]]:
    """Keep the exact approved action and scope, even when rationale is abbreviated."""
    if not 1 <= number <= total <= MAX_WEEKLY_PROPOSALS:
        raise PlanError("invalid_weekly_card_number")
    kind = proposal.get("kind")
    if not isinstance(kind, str) or kind not in _ACTIONS:
        raise PlanError("unsupported_action")
    identifier = proposal.get("id")
    if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", identifier):
        raise PlanError("invalid_weekly_callback")
    title, approve, scope = _ACTIONS[kind]
    proposed_text = safe_text(proposal.get("text"))
    action = _escape(proposed_text)
    if kind == "update_task":
        mutation = proposal.get("action")
        if (
            not isinstance(mutation, dict) or mutation.get("kind") != "update_task"
            or not isinstance(proposal.get("source_path"), str) or not proposal["source_path"]
            or mutation.get("path") != proposal["source_path"]
            or not isinstance(mutation.get("change"), dict)
            or set(mutation["change"]) != {"next_action"}
        ):
            raise PlanError("unsupported_task_edit")
        value = safe_text(mutation["change"]["next_action"])
        if value != proposed_text:
            raise PlanError("task_edit_preview_mismatch")
        action = "Set next action to:\n" + _escape(value)
    why = safe_text(proposal.get("why"), 500)
    link = _github_link(proposal.get("source_url")) or "Source link unavailable."
    before = f"<b>{title} · {number} of {total}</b>\n\n<b>Proposed action</b>\n{action}\n\n<b>Why</b>\n"
    after = (
        f"\n\n<b>Scope</b>\n{scope}\n\n{link}\n\n"
        "Reply <code>change: your correction</code> or <code>snooze YYYY-MM-DD</code> "
        "to this card. Use the button or reply approve to this card. Other actions stay unchanged."
    )
    remaining = TELEGRAM_LIMIT - _units(before) - _units(after)
    note = "\nRationale abbreviated to fit; the action and scope are unchanged."
    if _units(_escape(why)) > remaining:
        if remaining < _units(note) + 30:
            raise PlanError("weekly_card_too_long")
        rationale, _ = _excerpt(why, remaining - _units(note))
        rationale += note
    else:
        rationale = _escape(why)
    text = before + rationale + after
    keyboard = [
        [{"text": approve, "callback_data": f"brief1|approve|{identifier}"}],
        [
            {"text": "Dismiss", "callback_data": f"brief1|decline|{identifier}"},
            {"text": "Why this?", "callback_data": f"brief1|explain|{identifier}"},
        ],
    ]
    return text, keyboard
