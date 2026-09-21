"""The approval and delivery loop, with all external effects injected."""

from __future__ import annotations

import copy
import logging
import re
from collections.abc import Callable
from datetime import date
from typing import Any

from briefing_plan import (
    PlanError,
    RETRYABLE_PLAN_ERRORS,
    fingerprint,
    model_input,
    proposal_allowed,
    render_briefing,
    render_proposal,
    safe_text,
    task_due,
    validate_plan,
)
from briefing_state import (
    BriefingStore, StateError, is_expired, parse_reply, prune_state,
    record_transition, trim_deliveries,
)
from execution_budget import checkpoint

log = logging.getLogger(__name__)
_ID = re.compile(r"^[a-f0-9]{24}$")
_ACTIVE = {"pending", "accepted", "snoozed", "executing", "submitted", "uncertain"}


class LoopError(RuntimeError):
    """Safe failure code for the host; no source or message content."""


class BriefingLoop:
    def __init__(
        self,
        *,
        store: BriefingStore,
        sources: Callable[[dict[str, str], list[str]], dict[str, Any]],
        loops: Callable[[], dict[str, Any]],
        generate: Callable[[dict[str, Any]], dict[str, Any]],
        send: Callable[[str, list[list[dict[str, str]]] | None], int],
        revision: Callable[[str], str | None],
        execute: Callable[[dict[str, Any], bool], dict[str, Any]],
        extras: Callable[[list[str]], dict[str, Any]],
    ) -> None:
        self.store = store
        self.sources = sources
        self.loops = loops
        self.generate = generate
        self.send = send
        self.revision = revision
        self.execute = execute
        self.extras = extras

    def context(
        self, today: date, sections: list[str], *, previous: dict[str, str] | None = None,
        prune_sources: bool = True,
    ) -> dict[str, Any]:
        state = self.store.read()
        baseline = (state.get("last_delivered") or {}).get("baseline", {})
        if previous is None:
            previous = {**baseline, **state["fingerprints"]}
        context = self.sources(previous, sections)
        for source in context.get("changes", []):
            source["change_kind"] = "modified" if source["path"] in previous else "newly_available"
        context.update({"date": today.isoformat(), "sections": sections, "tasks": []})
        context.setdefault("warnings", [])
        if "loops" in sections:
            loops = self.loops()
            context["open_loops"] = loops
            if loops.get("status") != "available":
                context["warnings"].append("Task state is unavailable, not an empty task list.")
            else:
                context["tasks"] = loops["tasks"]["items"]
                if not all(task.get("revision") for task in context["tasks"]):
                    context["warnings"].append("Some task records lack current source revisions; actions on them are disabled.")
                if loops.get("complete") is False:
                    context["warnings"].append("The task view is bounded; some records were not inspected.")
        context["extras"] = self.extras(sections)
        context["warnings"].extend(context["extras"].get("warnings", []))
        inventory = context.get("inventory_paths")
        if inventory is not None and prune_sources:
            def prune(current: dict[str, Any]) -> None:
                paths = set(inventory)
                prune_state(current, inventory_paths=paths, today=today)
                metadata = [current.get("last_delivered") or {}, *current["deliveries"].values()]
                for item in metadata:
                    for field in ("baseline", "known_revisions"):
                        for path in list(item.get(field, {})):
                            if path not in paths:
                                del item[field][path]
                    if item.get("scan_cursor") not in paths:
                        item.pop("scan_cursor", None)

            self.store.update(prune)
        return context

    def reconcile(self, today: date | None = None) -> None:
        today = today or date.today()
        pending = [
            copy.deepcopy(record) for record in self.store.read()["proposals"].values()
            if record.get("status") in {"submitted", "uncertain", "executing"}
            and record.get("invalidation_reason") != "source_removed"
        ][:10]
        for record in pending:
            result = self.execute(record, True)
            status = result.get("status")
            if status not in {"submitted", "merged", "conflict", "failed", "in_progress", "unknown"}:
                raise LoopError("invalid_action_receipt")

            def save(state: dict[str, Any]) -> None:
                current = state["proposals"].get(record["id"])
                if current and current.get("status") in {"submitted", "uncertain", "executing"}:
                    current["result"] = result
                    if status == "merged":
                        record_transition(current, "snoozed" if current.get("requested_snooze") else "completed", today)
                    elif status == "conflict":
                        record_transition(current, "invalidated", today)
                    elif status == "submitted":
                        record_transition(current, "submitted", today)
                    elif status == "failed":
                        record_transition(current, "failed", today)
                    elif status == "unknown":
                        record_transition(current, "uncertain", today)

            self.store.update(save)

    def deliver(self, today: date, sections: list[str]) -> None:
        self.reconcile(today)
        context = self.context(today, sections)
        state = self.store.read()
        for identifier, delivery in state["deliveries"].items():
            if delivery.get("kind") == "weekly":
                continue
            if delivery["status"] != "sending":
                continue
            if delivery["date"] == today.isoformat() and delivery.get("revision") == context.get("revision"):
                self._complete_delivery(identifier)
                return

            def abandon(current: dict[str, Any]) -> None:
                old = current["deliveries"][identifier]
                old["status"] = "abandoned"
                old.pop("text", None)
                old.pop("fingerprints", None)
                pending = current["proposals"].get(old.get("proposal_id"))
                if pending and pending["status"] == "pending":
                    pending["status"] = "expired"
                    for message_id in pending.get("message_ids", []):
                        current["messages"].pop(str(message_id), None)

            self.store.update(abandon)
        state = self.store.read()
        announced = set((state.get("last_delivered") or {}).get("announced_actions", []))
        context["results"] = [
            {"id": identifier, "url": item["result"].get("pr_url") or item["result"].get("issue_url", "")}
            for identifier, item in state["proposals"].items()
            if identifier not in announced and (item.get("result") or {}).get("status") == "merged"
        ][:5]
        context["decisions"] = [
            {key: item.get(key) for key in ("source_path", "source_digest", "status", "review_on")}
            for item in state["proposals"].values()
            if item.get("source_path") in context.get("source_revisions", {})
        ]
        memories = [
            item for item in state["memories"].values()
            if item.get("active", True)
            and item.get("source_path") in context.get("source_revisions", {})
        ]
        if not sections:
            plan = {"focus": "Good morning. Your briefing sections are switched off.", "changes": [], "proposal": None}
        else:
            packet = model_input(context, memories)
            evidence_paths = {item["path"] for item in packet["sources"]}
            context["warnings"] = packet["warnings"]
            for attempt in range(2):
                checkpoint()
                try:
                    plan = validate_plan(
                        self.generate(packet), context, today, evidence_paths=evidence_paths,
                    )
                    break
                except PlanError as error:
                    log.warning("briefing plan rejected attempt=%d code=%s", attempt + 1, error.code)
                    if attempt or error.code not in RETRYABLE_PLAN_ERRORS:
                        raise
                    packet = {**packet, "validation_feedback": error.code}
            urgent = [item for item in context["tasks"] if task_due(item, today)]
            if urgent and any(section in sections for section in ("focus", "loops")):
                first = urgent[0]
                plan["focus"] = f"{first['title']}\nNext action: {first.get('next_action') or 'Review the source task.'}"
        proposal = plan["proposal"]
        if proposal and not proposal_allowed(proposal, state, today):
            proposal = None
            plan["proposal"] = None
        text, presented = render_briefing(plan, context, today)
        if proposal:
            presented.append(proposal["source_path"])
        delivery_id = fingerprint([today.isoformat(), sections, text, proposal])[:24]

        def prepare(current: dict[str, Any]) -> bool:
            previous = current["deliveries"].get(delivery_id)
            if previous:
                if previous["status"] == "sent":
                    return False
                raise LoopError("previous_delivery_uncertain")
            trim_deliveries(current)
            current["deliveries"][delivery_id] = {
                "status": "sending", "date": today.isoformat(), "message_ids": [],
                "text": text, "summary_sent": False, "revision": context.get("revision"),
                "proposal_id": proposal["id"] if proposal else None,
                "fingerprints": {
                    path: context["fingerprints"][path]
                    for path in presented if path in context.get("fingerprints", {})
                },
                "baseline": (
                    (current.get("last_delivered") or {}).get("baseline", {})
                    if current.get("last_delivered")
                    else context.get("fingerprints", {})
                ),
                "known_revisions": {
                    **(current.get("last_delivered") or {}).get("known_revisions", {}),
                    **context.get("processed_revisions", {}),
                },
                "scan_cursor": context.get("scan_cursor"),
                "announced_actions": sorted(announced | {item["id"] for item in context["results"]}),
            }
            if len(current["deliveries"][delivery_id]["known_revisions"]) > 1000:
                raise LoopError("source_tracking_capacity")
            if proposal:
                existing = current["proposals"].get(proposal["id"])
                if existing and existing.get("status") not in {"snoozed", "expired", "failed"}:
                    raise LoopError("proposal_changed_during_generation")
                for previous in current["proposals"].values():
                    if (
                        previous.get("source_path") == proposal["source_path"]
                        and previous.get("source_digest") == proposal["source_digest"]
                        and previous.get("id") != proposal["id"]
                        and previous.get("status") in {"expired", "snoozed", "failed"}
                    ):
                        previous["status"] = "superseded"
                        for message_id in previous.get("message_ids", []):
                            current["messages"].pop(str(message_id), None)
                current["proposals"][proposal["id"]] = proposal
            for memory in memories:
                saved = current["memories"].get(memory["id"])
                if saved:
                    saved["last_used_on"] = today.isoformat()
            return True

        if not self.store.update(prepare):
            return
        self._complete_delivery(delivery_id)

    def _complete_delivery(self, delivery_id: str) -> None:
        state = self.store.read()
        delivery = state["deliveries"][delivery_id]
        if delivery["status"] == "sent":
            return
        if not delivery.get("summary_sent"):
            message_id = self.send(delivery["text"], None)

            def record_summary(current: dict[str, Any]) -> None:
                current["deliveries"][delivery_id]["message_ids"].append(message_id)
                current["deliveries"][delivery_id]["summary_sent"] = True

            self.store.update(record_summary)
        proposal = state["proposals"].get(delivery.get("proposal_id"))
        if proposal and not proposal.get("message_ids"):
            keyboard = [[
                {"text": "Approve", "callback_data": f"brief1|approve|{proposal['id']}"},
                {"text": "Decline", "callback_data": f"brief1|decline|{proposal['id']}"},
                {"text": "Why?", "callback_data": f"brief1|explain|{proposal['id']}"},
            ]]
            proposal_message = self.send(render_proposal(proposal), keyboard)

            def bind(current: dict[str, Any]) -> None:
                record = current["proposals"][proposal["id"]]
                record["message_ids"].append(proposal_message)
                current["messages"][str(proposal_message)] = proposal["id"]
                current["deliveries"][delivery_id]["message_ids"].append(proposal_message)

            self.store.update(bind)

        def finish(current: dict[str, Any]) -> None:
            receipt = current["deliveries"][delivery_id]
            receipt["status"] = "sent"
            current["fingerprints"].update(receipt.pop("fingerprints", {}))
            receipt.pop("text", None)
            current["last_delivered"] = {
                "id": delivery_id, "date": receipt["date"], "revision": receipt.get("revision"),
                "baseline": receipt.pop("baseline", {}),
                "known_revisions": receipt.pop("known_revisions", {}),
                "scan_cursor": receipt.pop("scan_cursor", None),
                "announced_actions": receipt.pop("announced_actions", []),
            }

        self.store.update(finish)

    def target(self, message_id: object) -> str | None:
        if type(message_id) is not int:
            return None
        return self.store.read()["messages"].get(str(message_id))

    def reply(self, proposal_id: str, text: str, today: date) -> str:
        if not _ID.fullmatch(proposal_id):
            return "That proposal identifier is invalid."
        decision = parse_reply(text, today)
        intent = decision["intent"]
        state = self.store.read()
        proposal = state["proposals"].get(proposal_id)
        if not proposal or not proposal.get("source_path"):
            return "That proposal is unavailable or its source was removed. Request a new briefing."
        task_snooze = intent == "snooze" and proposal["kind"] in {"review_task", "update_task"}
        if intent == "unknown":
            return decision.get("clarification") or (
                "Reply approve, decline, why, done, 'snooze YYYY-MM-DD', "
                "'correction: ...', or 'change: ...'. No action was taken."
            )
        if intent == "explain":
            return render_proposal(proposal)
        if intent == "correct":
            correction = safe_text(decision["text"], 280)
            identifier = fingerprint([proposal_id, correction])[:24]

            def remember(current: dict[str, Any]) -> None:
                item = current["proposals"][proposal_id]
                if identifier in current["memories"]:
                    return
                predecessors = []
                for memory in current["memories"].values():
                    if memory.get("source_path") == item["source_path"] and memory.get("active", True):
                        memory["active"] = False
                        predecessors.append(memory["id"])
                current["memories"][identifier] = {
                    "id": identifier, "kind": "correction", "text": correction,
                    "source_path": item["source_path"], "proposal_id": proposal_id,
                    "created_on": today.isoformat(), "last_used_on": today.isoformat(),
                    "active": True,
                }
                if predecessors:
                    current["memories"][identifier]["supersedes"] = predecessors[-1]
                if item["status"] in {"pending", "accepted", "snoozed", "corrected"}:
                    record_transition(item, "corrected", today)

            self.store.update(remember)
            return "Correction saved for future briefings. Any existing action receipt is preserved; no source plan was edited."
        if proposal.get("status") in {"executing", "submitted", "completed", "uncertain"}:
            return self._receipt_text(proposal)
        if intent in {"approve", "done", "change"} or task_snooze:
            if is_expired(proposal["expires_on"], today):
                return "That proposal expired. Request a fresh proposal before acting."
            if self.revision(proposal["source_path"]) != proposal["source_revision"]:
                self.store.update(lambda current: current["proposals"][proposal_id].update(status="invalidated"))
                return "The source changed or was removed. The old approval cannot be used; request a fresh proposal."
        if intent == "change":
            revised = copy.deepcopy(proposal)
            revised["text"] = safe_text(decision["text"])
            revised["action"] = {"kind": "create_task", "text": revised["text"]}
            revised["kind"] = "create_task"
            if proposal["kind"] in {"review_task", "update_task"}:
                revised["kind"] = "update_task"
                revised["action"] = {
                    "kind": "update_task", "path": proposal["source_path"],
                    "change": {"next_action": revised["text"]},
                }
            elif proposal["kind"] == "research":
                revised["kind"] = "research"
                revised["action"]["kind"] = "research"
            revised["id"] = fingerprint([proposal_id, revised["action"]])[:24]
            revised["status"] = "pending"
            revised["message_ids"] = []
            revised.pop("activity", None)
            revised.pop("approved_on", None)
            revised.pop("action_id", None)
            revised.pop("result", None)
            revised.pop("requested_snooze", None)

            def save_revision(current: dict[str, Any]) -> bool:
                if revised["id"] in current["proposals"]:
                    return False
                if current["proposals"][proposal_id]["status"] not in {
                    "pending", "accepted", "snoozed", "declined", "corrected", "failed",
                }:
                    raise LoopError("proposal_action_already_claimed")
                record_transition(current["proposals"][proposal_id], "superseded", today)
                current["proposals"][revised["id"]] = revised
                return True

            if not self.store.update(save_revision):
                return "That revision is already recorded. Use its existing proposal message; no new action was started."
            message_id = self.send(render_proposal(revised), [[
                {"text": "Approve revision", "callback_data": f"brief1|approve|{revised['id']}"},
                {"text": "Decline", "callback_data": f"brief1|decline|{revised['id']}"},
            ]])

            def bind_revision(current: dict[str, Any]) -> None:
                current["proposals"][revised["id"]]["message_ids"].append(message_id)
                current["messages"][str(message_id)] = revised["id"]

            self.store.update(bind_revision)
            return "Revised proposal saved. The task or plan has not changed; approve the revision to proceed."
        if intent == "done" and proposal["kind"] not in {"review_task", "update_task"}:
            return "This proposal does not identify an existing task to complete. No task was changed."
        if intent == "decline" or (intent == "snooze" and not task_snooze):
            def record(current: dict[str, Any]) -> None:
                item = current["proposals"][proposal_id]
                if intent == "decline" and item["status"] == "declined":
                    return
                if item["status"] not in {"pending", "accepted", "snoozed", "failed"}:
                    raise LoopError("decision_conflict")
                if intent == "decline":
                    record_transition(item, "declined", today)
                elif intent == "snooze":
                    record_transition(item, "snoozed", today)
                    item["review_on"] = decision["review_on"]
            self.store.update(record)
            if intent == "snooze":
                return (
                    f"Proposal snoozed until {decision['review_on']}. "
                    "The source task's review date and hard deadline are unchanged."
                )
            if intent == "decline":
                return "Declined and saved. This unchanged proposal will not be repeated."
        if intent not in {"approve", "done"} and not task_snooze:
            return "No action was taken; please clarify the decision."

        def claim(current: dict[str, Any]) -> dict[str, Any] | None:
            item = current["proposals"][proposal_id]
            if item["status"] not in {"pending", "accepted", "snoozed", "failed"}:
                return None
            if intent == "approve" and item["kind"] == "review_task":
                record_transition(item, "accepted", today)
                return None
            if intent == "done":
                item["action"] = {
                    "kind": "update_task", "path": item["source_path"], "change": {"status": "done"},
                }
            elif task_snooze:
                item["action"] = {
                    "kind": "update_task", "path": item["source_path"],
                    "change": {"review_on": decision["review_on"]},
                }
                item["review_on"] = decision["review_on"]
                item["requested_snooze"] = True
            record_transition(item, "executing", today)
            item["approved_on"] = today.isoformat()
            item["action_id"] = fingerprint([proposal_id, item["action"]])[:32]
            return copy.deepcopy(item)

        claimed = self.store.update(claim)
        if claimed is None:
            return self._receipt_text(self.store.read()["proposals"][proposal_id])
        result = self.execute(claimed, False)
        status = result.get("status")
        if status not in {"merged", "submitted", "failed", "conflict", "in_progress", "unknown"}:
            raise LoopError("invalid_action_receipt")

        def save_result(current: dict[str, Any]) -> None:
            item = current["proposals"][proposal_id]
            item["result"] = result
            next_status = {
                "merged": "snoozed" if item.get("requested_snooze") else "completed",
                "submitted": "submitted", "conflict": "invalidated",
                "failed": "failed", "in_progress": "executing", "unknown": "uncertain",
            }[status]
            record_transition(item, next_status, today)

        self.store.update(save_result)
        return self._receipt_text(self.store.read()["proposals"][proposal_id])

    @staticmethod
    def _receipt_text(proposal: dict[str, Any]) -> str:
        status = proposal.get("status")
        label = {
            "accepted": "Next action selected and saved. The task remains open; reply done only when completed.",
            "submitted": "Approved work submitted. Canonical completion is not yet verified.",
            "completed": "Canonical result verified.",
            "executing": "The approved action is in progress. A repeated reply will not start another.",
            "uncertain": "The external result is unconfirmed. No duplicate action will be started.",
            "invalidated": "The source changed; this action requires a fresh proposal.",
            "failed": "The approved action failed. No completion is claimed.",
            "snoozed": f"Review deferred until {proposal.get('review_on')}. The hard deadline is unchanged.",
        }.get(status, f"Proposal status: {status}.")
        result = proposal.get("result") or {}
        link = result.get("pr_url") or result.get("issue_url") or ""
        return f"{label}\n{link}".strip()

    def memory_command(self, argument: str) -> str:
        arg = argument.strip()
        if arg.startswith("forget "):
            identifier = arg[7:].strip()
            if not _ID.fullmatch(identifier):
                return "Use /memory forget <memory-id> from /memory."
            self.store.update(lambda state: state["memories"].pop(identifier, None))
            return "Memory removed. Repeating this deletion is safe; source notes are unchanged."
        if arg:
            return "Use /memory to inspect corrections, or /memory forget <memory-id>."
        records = list(self.store.read()["memories"].values())
        if not records:
            return "No learned corrections are stored."
        return "\n\n".join(
            f"{item['id']} [{item['kind']}; {'active' if item.get('active', True) else 'superseded'}]\n{item['text']}"
            for item in records
        )

    def proposals_command(self, include_history: bool = False) -> str:
        records = self.store.read()["proposals"].values()
        lines = [
            f"{item['id']} [{item['status']}]: {item.get('text', 'source removed')}\n{self._receipt_text(item)}"
            + (
                "\nRecorded observations:\n"
                + "\n".join(f"- {event['date']}: {event['status'].replace('_', ' ')}" for event in item["activity"])
                if item.get("activity") else ""
            )
            for item in records if include_history or item.get("status") in _ACTIVE
        ]
        return "\n\n".join(lines) or "No pending proposals."
