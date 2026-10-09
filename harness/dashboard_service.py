"""Owner-authenticated dashboard reads and explicit, bounded private mutations."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from datetime import date, datetime, timezone
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from briefing_plan import fingerprint
from dashboard_sources import DashboardError, DashboardRepository, HEX64
from evolve_feedback import ReviewFeedback
from task_service import TaskService
from task_state import workspace

VISIT_RETENTION_SECONDS = 35 * 86400
VISIT_TOKEN_SECONDS = 900


class DashboardService:
    def __init__(
        self, *, tasks: TaskService, repository: DashboardRepository,
        feedback: Callable[[], ReviewFeedback], cipher: Fernet,
        feedback_enabled: Callable[[], bool] = lambda: True,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.tasks, self.repository, self.feedback_factory = tasks, repository, feedback
        self.cipher, self.clock, self.feedback_enabled = cipher, clock, feedback_enabled

    def _public(self, item: dict[str, Any], today: date) -> dict[str, Any]:
        result = {key: value for key, value in item.items() if not key.startswith("_")}
        # Canonical evidence is served in-app; do not offer arbitrary external readers.
        result.pop("url", None)
        if item["kind"] == "daily_review":
            result["feedback"] = (
                self.feedback_factory().projection(item["_review"], today)
                if self.feedback_enabled() else {"available": False, "findings": {}}
            )
        return result

    def today(self, offset: int, today: date) -> dict[str, Any]:
        prior = workspace(self.tasks.store.read()).get("dashboard_visit")
        now = int(self.clock().timestamp())
        active = prior if prior and 0 <= now - prior["observed_at"] < VISIT_RETENTION_SECONDS else None
        result = self.repository.today(self.tasks.owner_date(), active, offset)
        result["items"] = [self._public(item, today) for item in result["items"]]
        marker = {"revision": result["canonical_revision"], "observed_at": now}
        token = {"purpose": "dashboard_visit_v1", "previous": fingerprint(prior), "marker": marker}
        result["visit_token"] = self.cipher.encrypt(json.dumps(token).encode()).decode()
        result["observed_at"] = datetime.fromtimestamp(now, timezone.utc).isoformat()
        result["since"] = datetime.fromtimestamp(active["observed_at"], timezone.utc).isoformat() if active else None
        result["baseline_expired"] = prior is not None and active is None
        return result

    def visit(self, payload: dict[str, Any]) -> dict[str, Any]:
        if set(payload) != {"token"} or not isinstance(payload["token"], str) or len(payload["token"]) > 2000:
            raise DashboardError("dashboard_request_invalid")
        try:
            token = json.loads(self.cipher.decrypt(payload["token"].encode(), ttl=VISIT_TOKEN_SECONDS))
        except (InvalidToken, ValueError, UnicodeError):
            raise DashboardError("dashboard_visit_expired") from None
        if not isinstance(token, dict) or token.get("purpose") != "dashboard_visit_v1":
            raise DashboardError("dashboard_request_invalid")
        marker = token["marker"]
        if not 0 <= int(self.clock().timestamp()) - marker["observed_at"] <= VISIT_TOKEN_SECONDS:
            raise DashboardError("dashboard_visit_expired")

        def save(state: dict[str, Any]) -> dict[str, Any]:
            data = workspace(state)
            current = data.get("dashboard_visit")
            if current == marker:
                return {"recorded": True}
            if fingerprint(current) != token["previous"]:
                raise DashboardError("dashboard_visit_conflict")
            data["dashboard_visit"] = copy.deepcopy(marker)
            return {"recorded": True}

        return self.tasks.store.update(save)

    def inbox(self, offset: int, today: date) -> dict[str, Any]:
        result = self.repository.inbox(offset)
        result["items"] = [self._public(item, today) for item in result["items"]]
        return result

    def read(self, payload: dict[str, Any], today: date) -> dict[str, Any]:
        if set(payload) != {"id", "revision"}:
            raise DashboardError("dashboard_request_invalid")
        return self._public(self.repository.read_display(payload["id"], payload["revision"]), today)

    def feedback(self, payload: dict[str, Any], today: date) -> dict[str, Any]:
        if not self.feedback_enabled():
            raise DashboardError("dashboard_feedback_disabled")
        if (
            set(payload) not in (
                {"id", "revision", "finding", "value", "version"},
                {"id", "revision", "finding", "value", "version", "review_on"},
            )
            or not isinstance(payload["value"], str)
            or payload["value"] not in {"useful", "known", "dismiss", "snooze"}
            or not isinstance(payload["finding"], str)
            or not isinstance(payload["version"], str) or not HEX64.fullmatch(payload["version"])
            or (payload["value"] == "snooze") != ("review_on" in payload)
        ):
            raise DashboardError("dashboard_request_invalid")
        source = self.repository.read_display(payload["id"], payload["revision"])
        if source["kind"] != "daily_review":
            raise DashboardError("dashboard_request_invalid")
        value = {"useful": "Useful", "known": "Already familiar", "dismiss": "Not useful"}.get(payload["value"])
        if value is None:
            if not isinstance(payload["review_on"], str):
                raise DashboardError("dashboard_request_invalid")
            value = "snooze " + payload["review_on"]
        return self.feedback_factory().save_canonical(
            source["_review"], source["_packet"], source["canonical_revision"],
            payload["finding"], value, payload["version"], today,
        )

    def capture(self, payload: dict[str, Any], today: date) -> dict[str, Any]:
        if set(payload) != {"id", "revision", "finding", "request_id", "text", "definition"}:
            raise DashboardError("dashboard_request_invalid")
        source = self.repository.read_display(payload["id"], payload["revision"])
        if source["kind"] == "weekly_digest":
            raise DashboardError("dashboard_capture_requires_source")
        evidence = []
        if source["kind"] == "daily_review":
            finding = next((item for item in source["findings"] if item["id"] == payload["finding"]), None)
            if finding is None:
                raise DashboardError("dashboard_request_invalid")
            evidence = finding["evidence"]
        elif payload["finding"] is not None:
            raise DashboardError("dashboard_request_invalid")
        if not isinstance(payload["definition"], dict) or "context" in payload["definition"]:
            raise DashboardError("dashboard_request_invalid")
        provenance = f"Source: {source['path']} at {source['revision']}."
        if source["kind"] == "daily_review":
            provenance += f" Finding {finding['id']}, observed {source['as_of']}. Interpretation, not independent evidence."
        for item in evidence:
            provenance += f" Evidence: {item['path']} at {item['revision']}: \"{item['quote']}\"."
        if len(provenance) > 2000:
            raise DashboardError("dashboard_capture_bounded")
        return self.tasks.capture({
            "request_id": payload["request_id"], "text": payload["text"],
            "definition": {**payload["definition"], "context": provenance},
        }, today, source=source)
