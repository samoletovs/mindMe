"""Daily proposal-only knowledge reviews with bounded, private delivery receipts."""

from __future__ import annotations

import copy
import re
import secrets
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from typing import Any

from briefing_plan import fingerprint, safe_text
from briefing_sources import SourceError, _SENSITIVE_CONTENT
from briefing_state import BriefingStore
from execution_budget import checkpoint, execution_budget
from vault_evolve import EvolveError, complete_review, evidence_packet, telegram_parts

EVOLVE_STATE_BLOB = "system/mindme/vault-evolve-state-v1.json"
RETENTION_DAYS = 14
LEASE_SECONDS = 600


class DailyEvolve:
    def __init__(
        self, *, store: BriefingStore,
        sources: Callable[[dict[str, Any]], dict[str, Any]],
        generate: Callable[[dict[str, Any]], dict[str, Any]],
        publish: Callable[[str, dict[str, Any], str], dict[str, Any]],
        send: Callable[[str, list[list[dict[str, str]]] | None], int],
        revision: Callable[[str], str | None],
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.store = store
        self.sources = sources
        self.generate = generate
        self.publish = publish
        self.send = send
        self.revision = revision
        self.clock = clock

    def _expire(self, today: date) -> None:
        def remove(state: dict[str, Any]) -> None:
            for key in list(state["deliveries"]):
                if today >= date.fromisoformat(key) + timedelta(days=RETENTION_DAYS):
                    del state["deliveries"][key]
        self.store.update(remove)

    def _invalidate(self, key: str) -> None:
        def remove(state: dict[str, Any]) -> None:
            current = state["deliveries"].get(key)
            if current:
                parts = current.get("parts", [])
                current["bindings"] = {
                    str(message_id): parts[index]["finding"]
                    for index, message_id in enumerate(current["message_ids"])
                    if index < len(parts) and parts[index].get("finding")
                } or current.get("bindings", {})
                for field in ("review", "packet", "parts", "feedback", "owner", "inflight"):
                    current.pop(field, None)
                current.update(phase="invalidated", status="failed", lease_until=0)
        self.store.update(remove)

    def _sources_current(self, key: str, record: dict[str, Any]) -> bool:
        for source in record.get("packet", {}).get("sources", []):
            checkpoint()
            try:
                revision = self.revision(source["path"])
                checkpoint()
            except SourceError as error:
                if str(error) != "source_no_longer_permitted":
                    raise
                self._invalidate(key)
                return False
            if revision != source["revision"]:
                self._invalidate(key)
                return False
        return True

    def _reconcile_sources(self, context: dict[str, Any]) -> None:
        inventory = context.get("inventory_paths")
        if inventory is None:
            raise EvolveError("review_source_inventory_unavailable")
        revisions = context.get("source_revisions", {})
        for key, record in self.store.read()["deliveries"].items():
            if not record.get("review"):
                continue
            for source in record["packet"]["sources"]:
                path = source["path"]
                if path not in inventory or revisions.get(path) != source["revision"]:
                    self._invalidate(key)
                    break

    def run(self, today: date, *, retry_delivery: bool = False) -> str:
        key = today.isoformat()
        owner = secrets.token_hex(12)
        claim_attempted = False

        def claiming() -> None:
            nonlocal claim_attempted
            claim_attempted = True

        with execution_budget(170):
            try:
                with execution_budget(150, reserve=20):
                    return self._run_work(
                        today, key, owner, retry_delivery=retry_delivery, claiming=claiming,
                    )
            finally:
                def release(state: dict[str, Any]) -> None:
                    current = state["deliveries"].get(key)
                    if current and current.get("owner") == owner:
                        current["lease_until"] = 0
                if claim_attempted:
                    with execution_budget(15, outcome=True):
                        self.store.update(release)

    def _run_work(
        self, today: date, key: str, owner: str, *, retry_delivery: bool,
        claiming: Callable[[], None],
    ) -> str:
        self._expire(today)
        checkpoint()
        now = self.clock().timestamp()

        def claim(state: dict[str, Any]) -> dict[str, Any] | None:
            for old in list(state["deliveries"]):
                if date.fromisoformat(old) < today - timedelta(days=RETENTION_DAYS - 1):
                    del state["deliveries"][old]
            record = state["deliveries"].get(key)
            if record and record["status"] == "sent":
                return None
            if record and record.get("lease_until", 0) > now:
                raise EvolveError("daily_review_busy")
            if record and record.get("inflight") is not None:
                if not retry_delivery:
                    raise EvolveError("review_delivery_unconfirmed_use_explicit_retry")
                record.pop("inflight")
            if record is None:
                record = {
                    "date": key, "status": "sending", "message_ids": [],
                    "action_id": fingerprint(["vault-evolve-v1", key])[:32],
                    "phase": "generating", "attempts": 0, "feedback": {},
                }
                state["deliveries"][key] = record
            if record.get("phase") == "invalidated":
                raise EvolveError("review_source_changed")
            record.update(owner=owner, lease_until=now + LEASE_SECONDS)
            claiming()
            return copy.deepcopy(record)

        record = self.store.update(claim)
        checkpoint()
        if record is None:
            return "Today's knowledge review is already delivered. Use /evolve to see its results."

        def save(change: Callable[[dict[str, Any], dict[str, Any]], None]) -> None:
            def mutate(state: dict[str, Any]) -> None:
                current = state["deliveries"].get(key)
                if not current or current.get("owner") != owner or current.get("phase") == "invalidated":
                    raise EvolveError("review_claim_lost")
                change(current, state)
            self.store.update(mutate)
            checkpoint()

        if record["phase"] == "generating":
            if record["attempts"] >= 2:
                raise EvolveError("daily_model_attempt_limit")
            with execution_budget(45, reserve=75):
                context = self.sources(self.store.read().get("last_delivered") or {})
            context["date"] = key
            if context.get("source_status") != "available":
                raise EvolveError("review_sources_unavailable")
            self._reconcile_sources(context)
            prior = list(self.store.read()["deliveries"].values())
            packet = evidence_packet(context, prior)
            save(lambda current, state: current.update(attempts=current["attempts"] + 1))
            with execution_budget(90, reserve=45):
                raw = self.generate(packet) if packet["sources"] else {"findings": []}
            review = complete_review(raw, packet)
            record.update(
                review=review, packet={"sources": packet["sources"]},
                source_revision=context["revision"],
                processed_revisions=context.get("processed_revisions", {}),
                scan_cursor=context.get("scan_cursor"), phase="prepared",
            )
            save(lambda current, state: current.update({
                name: record[name] for name in (
                    "review", "packet", "processed_revisions", "scan_cursor", "source_revision", "phase",
                )
            }))
        if record["phase"] == "prepared":
            with execution_budget(30, reserve=45):
                if not self._sources_current(key, record):
                    raise EvolveError("review_source_changed")
            with execution_budget(35, reserve=25):
                receipt = (
                    self.publish(record["action_id"], record["review"], record["source_revision"])
                    if record["review"]["findings"] else {"status": "no_action"}
                )
            if receipt.get("status") in {"closed", "conflict", "failed"}:
                save(lambda current, state: current.update(receipt=receipt, phase="invalidated", status="failed"))
                raise EvolveError("review_publication_rejected")
            if receipt.get("status") not in {"submitted", "merged", "no_action"}:
                raise EvolveError("review_publication_unconfirmed")
            parts = telegram_parts(record["review"], receipt, record["packet"])
            record.update(receipt=receipt, parts=parts, phase="delivering")
            save(lambda current, state: current.update(receipt=receipt, parts=parts, phase="delivering"))
        if record["phase"] != "delivering":
            raise EvolveError("invalid_review_phase")
        with execution_budget(20, reserve=15):
            if not self._sources_current(key, record):
                raise EvolveError("review_source_changed")
        for index in range(len(record["message_ids"]), len(record["parts"])):
            checkpoint()
            part = record["parts"][index]
            save(lambda current, state: current.update(inflight=index))
            with execution_budget(10, reserve=5):
                message_id = self.send(part["text"], part["keyboard"])
            if type(message_id) is not int or message_id <= 0:
                raise EvolveError("invalid_review_delivery_receipt")

            def delivered(current: dict[str, Any], state: dict[str, Any]) -> None:
                current["message_ids"].append(message_id)
                current.pop("inflight", None)

            save(delivered)

        def finish(current: dict[str, Any], state: dict[str, Any]) -> None:
            current.update(status="sent", phase="complete")
            known = dict((state.get("last_delivered") or {}).get("known_revisions", {}))
            for path, revision in current["processed_revisions"].items():
                known.pop(path, None)
                known[path] = revision
            while len(known) > 1000:
                del known[next(iter(known))]
            state["last_delivered"] = {
                "date": key, "known_revisions": known,
                "scan_cursor": current["scan_cursor"],
            }

        save(finish)
        return "Knowledge review delivered. Proposals remain unapproved."

    def _live_record(self, key: str, today: date) -> dict[str, Any] | None:
        self._expire(today)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", key):
            raise EvolveError("invalid_review_identifier")
        record = self.store.read()["deliveries"].get(key)
        if not record or not record.get("review"):
            return None
        return record if self._sources_current(key, record) else None

    def show(self, today: date) -> str:
        record = self._live_record(today.isoformat(), today)
        if not record:
            return "No current review is available. Use /evolve now; a changed-source review is not presented as current."
        review = record["review"]
        receipt = record.get("receipt", {})
        lines = [f"mindVault evolution - {review['as_of']}"]
        if receipt.get("pr_url"):
            lines.append(f"Review artifacts ({receipt['status']}): {receipt['pr_url']}")
        if not review["findings"]:
            lines.append("No new candidate earned attention in this bounded review.")
        for finding in review["findings"]:
            lines.append(f"{finding['id']} ({finding['basis']}): {finding['statement']}")
        lines.append("Reply to a finding or use its buttons. /evolve feedback lists feedback; /evolve forget YYYY-MM-DD removes it.")
        return "\n\n".join(lines)

    def target(self, message_id: object) -> tuple[str, str] | None:
        self._expire(self.clock().date())
        if type(message_id) is not int:
            return None
        for key, record in self.store.read()["deliveries"].items():
            if finding := record.get("bindings", {}).get(str(message_id)):
                return key, finding
            for index, identifier in enumerate(record["message_ids"]):
                if identifier == message_id:
                    parts = record.get("parts", [])
                    finding = parts[index].get("finding") if index < len(parts) else None
                    return (key, finding) if finding else None
        return None

    def feedback(self, key: str, finding_id: str, text: str, today: date) -> str:
        record = self._live_record(key, today)
        if not record:
            return "That review is unavailable or its evidence changed. No feedback or action was recorded."
        finding = next((item for item in record["review"]["findings"] if item["id"] == finding_id), None)
        if not finding:
            raise EvolveError("invalid_finding_identifier")
        value = text.strip()
        if not value or len(value) > 280 or _SENSITIVE_CONTENT.search(value):
            return "Use at most 280 non-sensitive characters for review feedback. Nothing was saved."
        safe_text(value, 280)
        if value.casefold() == "why":
            return "\n\n".join([
                "Evidence records source claims, not proof that the interpretation is true.",
                *[f"{item['source']}: {item['quote']}" for item in finding["evidence"]],
            ])
        if value.casefold() in {"yes", "approve", "do it", "research this", "create task"}:
            return "This finding has not authorized work. Use /dig with an explicit public research question, or /task with the exact task. Source-note changes still need review."
        review_on = None
        if value.casefold() == "later":
            return "Use 'snooze YYYY-MM-DD' to choose when to reconsider this finding."
        if value.lower().startswith("snooze "):
            try:
                review_on = date.fromisoformat(value[7:].strip())
            except ValueError:
                return "Use 'snooze YYYY-MM-DD' with a valid date."
            if not today < review_on < date.fromisoformat(key) + timedelta(days=RETENTION_DAYS):
                return "Choose a future date before this review's 14-day retention window ends."
        feedback = {"text": value, "recorded_on": today.isoformat(), "review_on": review_on.isoformat() if review_on else None}

        def persist(state: dict[str, Any]) -> None:
            current = state["deliveries"].get(key)
            if not current or current.get("review") != record["review"]:
                raise EvolveError("review_feedback_conflict")
            current.setdefault("feedback", {})[finding_id] = feedback

        self.store.update(persist)
        return "Scoped review feedback saved for up to 14 days. It will shape later reviews; no task, research or source edit was approved."

    def feedback_command(self, argument: str) -> str:
        today = self.clock().date()
        self._expire(today)
        value = argument.strip()
        if value.startswith("forget "):
            key = value[7:].strip()
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", key):
                return "Use /evolve forget YYYY-MM-DD."
            def forget(state: dict[str, Any]) -> None:
                if key in state["deliveries"]:
                    state["deliveries"][key]["feedback"] = {}
            self.store.update(forget)
            return "Review feedback removed. Published review artifacts and delivery receipts are unaffected."
        lines = []
        for key in list(self.store.read()["deliveries"]):
            record = self._live_record(key, today)
            if record:
                lines.extend(f"{key} {identifier}: {item['text']}" for identifier, item in record.get("feedback", {}).items())
        return "\n".join(lines) if lines else "No current knowledge-review feedback is stored."
