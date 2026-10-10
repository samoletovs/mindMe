"""Transport-independent, revision-aware feedback in the existing evolve ledger."""

from __future__ import annotations

import copy
import re
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from typing import Any

from briefing_plan import fingerprint, safe_text
from briefing_sources import SourceError, _SENSITIVE_CONTENT
from briefing_state import BriefingStore
from execution_budget import checkpoint
from vault_evolve import EvolveError

RETENTION_DAYS = 14
SAVED = "Feedback saved for this review for up to 14 days. It will guide later reviews. No task, research or note edit was approved."


class ReviewFeedback:
    def __init__(
        self, *, store: BriefingStore, revision: Callable[[str], str | None],
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.store, self.revision, self.clock = store, revision, clock

    def _expire(self, today: date) -> None:
        def remove(state: dict[str, Any]) -> None:
            for key in list(state["deliveries"]):
                if today >= date.fromisoformat(key) + timedelta(days=RETENTION_DAYS):
                    del state["deliveries"][key]
        self.store.update(remove)

    def _invalidate(self, key: str, expected: dict[str, Any] | None = None) -> None:
        def remove(state: dict[str, Any]) -> None:
            current = state["deliveries"].get(key)
            if current and expected is not None and current.get("review") != expected:
                raise EvolveError("review_feedback_conflict")
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
                self._invalidate(key, record.get("review"))
                return False
            if revision != source["revision"]:
                self._invalidate(key, record.get("review"))
                return False
        return True

    def _live_record(self, key: str, today: date) -> dict[str, Any] | None:
        self._expire(today)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", key):
            raise EvolveError("invalid_review_identifier")
        record = self.store.read()["deliveries"].get(key)
        if not record or not record.get("review"):
            return None
        return record if self._sources_current(key, record) else None

    def projection(self, review: dict[str, Any], today: date) -> dict[str, Any]:
        self._expire(today)
        key = review["as_of"]
        expires = date.fromisoformat(key) + timedelta(days=RETENTION_DAYS)
        record = self.store.read()["deliveries"].get(key)
        matched = record is None or record.get("review") == review
        active = date.fromisoformat(key) <= today < expires and matched
        feedback = record.get("feedback", {}) if record and active else {}
        for value in feedback.values():
            text = value.get("text")
            if not isinstance(text, str) or len(text) > 280 or _SENSITIVE_CONTENT.search(text):
                raise EvolveError("invalid_review_feedback")
            safe_text(text, 280)
        return {
            "available": active, "expires_on": expires.isoformat(),
            "findings": {
                finding["id"]: {
                    "version": fingerprint(feedback.get(finding["id"])),
                    "value": copy.deepcopy(feedback.get(finding["id"])),
                } for finding in review["findings"]
            } if active else {},
        }

    def save(
        self, record: dict[str, Any], finding_id: str, value: str, today: date, *,
        expected: str | None = None, canonical: bool = False,
    ) -> dict[str, Any]:
        key = record["review"]["as_of"]
        if not date.fromisoformat(key) <= today < date.fromisoformat(key) + timedelta(days=RETENTION_DAYS):
            raise EvolveError("review_feedback_expired")
        if not any(item["id"] == finding_id for item in record["review"]["findings"]):
            raise EvolveError("invalid_finding_identifier")
        value = value.strip()
        if not value or len(value) > 280 or _SENSITIVE_CONTENT.search(value):
            raise EvolveError("invalid_review_feedback")
        safe_text(value, 280)
        review_on = None
        if value.lower().startswith("snooze "):
            try:
                review_on = date.fromisoformat(value[7:].strip())
            except ValueError:
                raise EvolveError("invalid_review_snooze") from None
            if not today < review_on < date.fromisoformat(key) + timedelta(days=RETENTION_DAYS):
                raise EvolveError("invalid_review_snooze")
        feedback = {"text": value, "recorded_on": today.isoformat(), "review_on": review_on.isoformat() if review_on else None}
        if not self._sources_current(key, record):
            raise EvolveError("review_source_changed")

        def persist(state: dict[str, Any]) -> dict[str, Any]:
            current = state["deliveries"].get(key)
            if current is None and canonical:
                current = copy.deepcopy(record)
                state["deliveries"][key] = current
            if not current or current.get("review") != record["review"]:
                raise EvolveError("review_feedback_conflict")
            previous = current.setdefault("feedback", {}).get(finding_id)
            # The same lost-response retry cannot clobber a later, different decision.
            if expected is not None and fingerprint(previous) != expected:
                if previous and previous["text"] == value and previous.get("review_on") == feedback["review_on"]:
                    return copy.deepcopy(previous)
                raise EvolveError("review_feedback_conflict")
            current["feedback"][finding_id] = feedback
            return copy.deepcopy(feedback)

        saved = self.store.update(persist)
        return {"value": saved, "version": fingerprint(saved), "message": SAVED}

    def save_canonical(
        self, review: dict[str, Any], packet: list[dict[str, Any]], revision: str,
        finding_id: str, value: str, expected: str, today: date,
    ) -> dict[str, Any]:
        self._expire(today)
        # This is NOT a Telegram delivery receipt. The ordinary loop may later send
        # this already-canonical review, but must never regenerate or republish it.
        record = {
            "date": review["as_of"], "status": "sending", "phase": "canonical",
            "message_ids": [], "action_id": fingerprint(["vault-evolve-v1", review["as_of"]])[:32],
            "review": review, "packet": {"sources": packet}, "source_revision": revision,
            "receipt": {"status": "merged"}, "feedback": {}, "attempts": 0,
            "processed_revisions": {}, "scan_cursor": (self.store.read().get("last_delivered") or {}).get("scan_cursor"),
        }
        return self.save(record, finding_id, value, today, expected=expected, canonical=True)

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
                "These quotes show what the sources say. They do not prove the interpretation is true.",
                *[f"\u201c{item['quote']}\u201d" for item in finding["evidence"]],
            ])
        if value.casefold() in {"yes", "approve", "do it", "research this", "create task"}:
            return "No work was approved. Use /dig with a public research question, or /task with the exact task. Note edits still need review."
        if value.casefold() == "later":
            return "Use 'snooze YYYY-MM-DD' to choose when to reconsider this finding."
        try:
            self.save(record, finding_id, value, today)
        except EvolveError as error:
            if str(error) == "invalid_review_snooze":
                return "Choose a future date within 14 days of this review. Use 'snooze YYYY-MM-DD'. The saved review expires after that."
            raise
        return SAVED
