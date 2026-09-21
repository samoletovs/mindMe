"""Weekly evidence and delivery, sharing the existing per-action approval boundary."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from typing import Any
from uuid import uuid4

from briefing_loop import BriefingLoop, LoopError
from briefing_plan import fingerprint, model_input, proposal_allowed
from briefing_state import is_expired, record_transition, trim_deliveries
from weekly_plan import render_weekly, render_weekly_proposal, render_weekly_sources, validate_weekly_plan


def latest_weekly(state: dict[str, Any]) -> dict[str, Any]:
    sent = [
        item for item in state["deliveries"].values()
        if item.get("kind") == "weekly" and item["status"] == "sent"
    ]
    return max(sent, key=lambda item: (item["date"], item.get("completed_order", 0)), default={})


def weekly_activity(state: dict[str, Any], start: date, today: date) -> list[dict[str, str]]:
    observed = []
    for item in state["proposals"].values():
        events = [
            event for event in item.get("activity", [])
            if start.isoformat() <= event["date"] <= today.isoformat()
        ]
        if not events:
            continue
        result = item.get("result") or {}
        observed.append({
            **events[-1], "text": item.get("text", "An approved action whose source was removed"),
            "url": result.get("pr_url") or result.get("issue_url", ""),
        })
    return sorted(observed, key=lambda item: item["date"], reverse=True)


class WeeklyReview:
    def __init__(
        self, *, loop: BriefingLoop,
        generate: Callable[[dict[str, Any]], dict[str, Any]],
        send: Callable[[str, list[list[dict[str, str]]] | None], int],
    ) -> None:
        self.loop = loop
        self.store = loop.store
        self.generate = generate
        self.send = send

    def source_status(self, today: date, sections: list[str]) -> list[str]:
        previous = latest_weekly(self.store.read())
        context = self.loop.context(
            today, sections, previous=previous.get("baseline", {}), reconcile_sources=False,
        )
        context["warnings"] = [
            "No earlier weekly baseline; existing notes are not new progress."
            if warning.startswith("Initial source baseline;") else warning
            for warning in model_input(context, [])["warnings"]
        ]
        return render_weekly_sources(context, previous)

    def run(self, today: date, sections: list[str], *, retry_delivery: bool = False) -> bool:
        self.loop.reconcile(today)
        previous = latest_weekly(self.store.read())
        checkpoint = (previous.get("date"), previous.get("completed_order"), previous.get("snapshot"))
        context = self.loop.context(today, sections, previous=previous.get("baseline", {}))
        previous = latest_weekly(self.store.read())
        if (previous.get("date"), previous.get("completed_order"), previous.get("snapshot")) != checkpoint:
            raise LoopError("weekly_checkpoint_changed")
        context["warnings"] = [
            "This is the first weekly comparison; older records are not new progress."
            if warning.startswith("Initial source baseline;") else warning
            for warning in context["warnings"]
        ]
        snapshot = fingerprint([
            context.get("revision"), context.get("source_revisions"), context.get("tasks"),
            sections, context.get("extras"),
            [
                [key, item["activity"]] for key, item in sorted(self.store.read()["proposals"].items())
                if item.get("activity")
            ],
        ])
        state = self.store.read()
        for identifier, delivery in state["deliveries"].items():
            if delivery.get("kind") != "weekly":
                continue
            matching = delivery["date"] == today.isoformat() and delivery.get("snapshot") == snapshot
            if matching and delivery["status"] == "sent":
                return False
            if delivery["status"] != "sending":
                continue
            if matching:
                self._complete(identifier, retry_delivery=retry_delivery)
                return True
            if not self._abandon(identifier, today):
                raise LoopError("weekly_checkpoint_changed")
        if latest_weekly(self.store.read()) != previous:
            raise LoopError("weekly_checkpoint_changed")

        state = self.store.read()
        context["open_actions"] = [
            {
                "text": item.get("text", "An approved action whose source was removed"),
                "status": item["status"], "approved_on": item.get("approved_on"),
                "url": (item.get("result") or {}).get("pr_url") or (item.get("result") or {}).get("issue_url", ""),
            }
            for item in state["proposals"].values()
            if item["status"] in {"submitted", "executing", "uncertain", "failed"}
        ]
        revisions = context.get("source_revisions", {})
        for task in context.get("tasks", []):
            if task.get("revision"):
                revisions[task["path"]] = task["revision"]
        context["decisions"] = [
            {key: item.get(key) for key in ("source_path", "source_digest", "status", "review_on")}
            for item in state["proposals"].values() if item.get("source_path") in revisions
        ]
        pending = [
            item for item in state["proposals"].values()
            if item.get("status") == "pending" and item.get("source_path") in revisions
            and item["source_revision"] == revisions[item["source_path"]]
            and not is_expired(item["expires_on"], today)
        ]
        pending.sort(key=lambda item: (item["created_on"], item["id"]))
        memories = [
            item for item in state["memories"].values()
            if item.get("active", True) and item.get("source_path") in revisions
        ]
        packet = model_input(context, memories)
        packet["review_kind"] = "weekly"
        packet["comparison_start"] = previous.get("date")
        packet["proposal_slots"] = max(0, 3 - len(pending))
        context["warnings"] = packet["warnings"]
        if packet["sources"]:
            plan = validate_weekly_plan(
                self.generate(packet), context, today,
                evidence_paths={item["path"] for item in packet["sources"]},
            )
        else:
            plan = {
                "focus": "No source-backed priority is available. Check the source notices before deciding.",
                "changes": [], "proposals": [],
            }
        proposals = list(pending[:3])
        for proposal in plan["proposals"]:
            if len(proposals) == 3:
                break
            if proposal["id"] not in state["proposals"] and proposal_allowed(proposal, state, today):
                proposals.append(proposal)
        plan["proposals"] = proposals
        for number, proposal in enumerate(proposals, 1):
            render_weekly_proposal(proposal, number, len(proposals))
        start = today - timedelta(days=6)
        if previous:
            start = min(today, max(start, date.fromisoformat(previous["date"]) + timedelta(days=1)))
        text = render_weekly(
            plan, context, weekly_activity(state, start, today), pending, today, start,
            has_baseline=bool(previous),
        )
        identifier = fingerprint(["weekly", today.isoformat(), snapshot])[:24]
        presented = [item["source"]["path"] for item in plan["changes"]]
        presented.extend(item["source_path"] for item in proposals)
        baseline = dict(previous.get("baseline", context.get("fingerprints", {})))
        baseline.update({
            path: context["fingerprints"][path]
            for path in presented if path in context.get("fingerprints", {})
        })

        def prepare(current: dict[str, Any]) -> bool:
            if latest_weekly(current) != previous:
                raise LoopError("weekly_checkpoint_changed")
            if identifier in current["deliveries"]:
                return False
            if any(
                item.get("kind") == "weekly" and item["status"] == "sending"
                for item in current["deliveries"].values()
            ):
                raise LoopError("weekly_delivery_already_claimed")
            trim_deliveries(current)
            new_ids = []
            for proposal in proposals:
                existing = current["proposals"].get(proposal["id"])
                if existing:
                    if existing != state["proposals"].get(proposal["id"]):
                        raise LoopError("weekly_decision_changed")
                else:
                    if not proposal_allowed(proposal, current, today):
                        raise LoopError("weekly_decision_changed")
                    current["proposals"][proposal["id"]] = proposal
                    new_ids.append(proposal["id"])
            known = {**previous.get("known_revisions", {}), **context.get("processed_revisions", {})}
            if len(known) > 1000 or len(baseline) > 1000:
                raise LoopError("source_tracking_capacity")
            current["deliveries"][identifier] = {
                "kind": "weekly", "status": "sending", "date": today.isoformat(),
                "message_ids": [], "snapshot": snapshot, "text": text,
                "proposal_ids": [item["id"] for item in proposals],
                "new_proposal_ids": new_ids, "sent_proposals": [], "summary_sent": False,
                "baseline": baseline, "known_revisions": known, "scan_cursor": context.get("scan_cursor"),
            }
            for memory in memories:
                if memory["id"] in current["memories"]:
                    current["memories"][memory["id"]]["last_used_on"] = today.isoformat()
            return True

        if not self.store.update(prepare):
            raise LoopError("weekly_delivery_already_claimed")
        self._complete(identifier)
        return True

    def _abandon(self, identifier: str, today: date) -> bool:
        def abandon(state: dict[str, Any]) -> bool:
            delivery = state["deliveries"][identifier]
            if delivery["status"] != "sending":
                return False
            delivery["status"] = "abandoned"
            delivery.pop("text", None)
            for proposal_id in delivery.get("new_proposal_ids", []):
                proposal = state["proposals"].get(proposal_id)
                if proposal and proposal["status"] == "pending" and not proposal.get("message_ids"):
                    record_transition(proposal, "expired", today)
            return True
        return self.store.update(abandon)

    def _complete(self, identifier: str, *, retry_delivery: bool = False) -> None:
        if retry_delivery:
            self.store.update(lambda state: state["deliveries"][identifier].pop("inflight", None))
        delivery = self.store.read()["deliveries"][identifier]
        parts = ["summary", *delivery["proposal_ids"]]
        for part in parts:
            state = self.store.read()
            current = state["deliveries"][identifier]
            if part == "summary" and current["summary_sent"]:
                continue
            if part in current["sent_proposals"]:
                continue
            keyboard = None
            text = current.get("text", "")
            if part != "summary":
                proposal = state["proposals"][part]
                if proposal["status"] != "pending" or not proposal.get("source_path"):
                    text = "A suggested action is no longer awaiting approval. Use /proposals for its current receipt."
                else:
                    text, keyboard = render_weekly_proposal(proposal, parts.index(part), len(parts) - 1)

            claim_id = uuid4().hex

            def claim(latest: dict[str, Any]) -> None:
                item = latest["deliveries"][identifier]
                if item["status"] != "sending" or item.get("inflight"):
                    raise LoopError("weekly_delivery_uncertain")
                if (part == "summary" and item["summary_sent"]) or part in item["sent_proposals"]:
                    raise LoopError("weekly_delivery_already_claimed")
                item["inflight"] = {"part": part, "claim": claim_id}

            self.store.update(claim)
            message_id = self.send(text, keyboard)

            def confirm(latest: dict[str, Any]) -> None:
                item = latest["deliveries"][identifier]
                if item["status"] != "sending" or item.get("inflight") != {"part": part, "claim": claim_id}:
                    raise LoopError("weekly_delivery_claim_changed")
                item.pop("inflight")
                item["message_ids"].append(message_id)
                if part == "summary":
                    item["summary_sent"] = True
                else:
                    item["sent_proposals"].append(part)
                    if keyboard:
                        latest["proposals"][part]["message_ids"].append(message_id)
                        latest["messages"][str(message_id)] = part

            self.store.update(confirm)

        def finish(state: dict[str, Any]) -> None:
            item = state["deliveries"][identifier]
            if item["status"] == "sent":
                return
            if (
                item["status"] != "sending" or item.get("inflight") or not item["summary_sent"]
                or item["sent_proposals"] != item["proposal_ids"]
            ):
                raise LoopError("weekly_delivery_claim_changed")
            item["completed_order"] = 1 + max(
                (record.get("completed_order", 0) for record in state["deliveries"].values()
                 if record.get("kind") == "weekly" and record["status"] == "sent"),
                default=0,
            )
            item["status"] = "sent"
            item.pop("text", None)
            trim_deliveries(state)
        self.store.update(finish)
