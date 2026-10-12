"""Task-only Telegram adapter. Existing capture and knowledge namespaces stay separate."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

from briefing_plan import fingerprint
from briefing_state import parse_reply
from task_service import TaskError, TaskService
from task_state import workspace
from telegram_format import escape


class TaskTelegram:
    def __init__(
        self, service: TaskService, *, lookup: Callable[[int, str | None], dict[str, Any] | None],
        send: Callable[[str, list | None], int],
    ) -> None:
        self.service, self.lookup, self.send = service, lookup, send

    def _claim(self, event: str, payload: object, today: date) -> bool:
        key, digest = fingerprint(["task-telegram", event]), fingerprint(payload)

        def claim(state: dict[str, Any]) -> bool:
            requests = workspace(state)["requests"]
            if key in requests:
                if requests[key]["digest"] != digest:
                    raise TaskError("task_update_conflict")
                return False
            requests[key] = {"digest": digest, "status": "claimed", "created_on": today.isoformat()}
            return True

        return self.service.store.update(claim)

    def _sent(self, event: str) -> None:
        self.service.store.update(
            lambda state: workspace(state)["requests"][fingerprint(["task-telegram", event])].update(status="sent")
        )

    def proposal(self, message_id: int, identifier: str, text: str, event: str, today: date) -> bool:
        state = self.service.store.read()
        record = state["proposals"].get(identifier)
        if not record or not record.get("task_workspace"):
            return False
        if state["messages"].get(str(message_id)) != identifier:
            raise TaskError("task_proposal_message_mismatch")
        if not self._claim(event, [message_id, identifier, text], today):
            return True
        if not record.get("source_path") or not isinstance(record.get("approval_digest"), str):
            self.send(
                "This action is no longer available because its source was removed or became ineligible. "
                "Open Tasks to inspect the retained receipt. No action was started.", None,
            )
            self._sent(event)
            return True
        decision = parse_reply(text, today)["intent"]
        if decision in {"approve", "decline"}:
            result = self.service.decide({
                "proposal_id": identifier, "approval_digest": record["approval_digest"], "decision": decision,
            }, today)
            self.send(escape(self.service.loop._receipt_text(result)), None)
        else:
            self.send(
                "Reply approve or decline to this action. Changes need a fresh proposal in Tasks; "
                "this message does not authorize completion or other work.", None,
            )
        self._sent(event)
        return True

    def handle(
        self, *, message_id: int, text: str, event: str, today: date, task_key: str | None = None,
    ) -> bool:
        state = self.service.store.read()
        proposal_id = state["messages"].get(str(message_id))
        if proposal_id:
            return self.proposal(message_id, proposal_id, text, event, today)
        data = workspace(state)
        binding = data["bindings"].get(str(message_id))
        answering = binding is not None and task_key is None
        if binding:
            clarification_id = binding["clarification_id"]
            record = data["clarifications"][clarification_id]
            if (
                record.get("expired") or record["expires_on"] <= today.isoformat()
                or binding["field"] != record["field"] or binding["turn"] != record["turns"]
                or not binding.get("question_token") or binding["question_token"] != record.get("question_token")
            ):
                if self._claim(event, [message_id, task_key, text], today):
                    self.send(
                        "That question is no longer current. Reply to the latest question or open Tasks. "
                        "No answer was applied to another field.", None,
                    )
                    self._sent(event)
                return True
            path, revision = record["source_path"], record["source_revision"]
        else:
            pointer = self.lookup(message_id, task_key)
            if pointer is None:
                return False
            path, revision = pointer["path"], pointer["source_revision"]
            source = self.service.repository.read(path)
            if source is None or source["revision"] != revision:
                if self._claim(event, [message_id, task_key, text], today):
                    self.send(
                        "This capture is still awaiting canonical publication, or its file version changed. "
                        "No refinement was saved. Open Tasks or the capture's Save status link; do not submit it again.",
                        None,
                    )
                    self._sent(event)
                return True
        if not self._claim(event, [message_id, task_key, text], today):
            return True
        try:
            draft = self.service.clarification(
                path, revision, today, answer=text if answering else None,
                request_id=fingerprint(["task-answer", event])[:32] if answering else None,
                question_token=binding["question_token"] if answering else None,
            )
        except TaskError as error:
            if str(error) != "task_clarification_stale":
                raise
            self.send(
                "That question changed while the answer was being saved. Reply to the latest question or open Tasks. "
                "No answer was applied to another field.", None,
            )
            self._sent(event)
            return True
        if draft["question"]:
            if not answering and any(
                value == {
                    "clarification_id": draft["id"], "field": draft["field"], "turn": draft["turns"],
                    "question_token": draft["question_token"],
                }
                for value in workspace(self.service.store.read())["bindings"].values()
            ):
                self._sent(event)
                return True
            question_id = self.send(
                "<b>Clarify one thing</b>\n\n" + escape(draft["question"])
                + "\n\nReply to this message. This is a draft; the saved task is unchanged.",
                None,
            )
            self.service.store.update(
                lambda current: workspace(current)["bindings"].update({str(question_id): {
                    "clarification_id": draft["id"], "field": draft["field"], "turn": draft["turns"],
                    "question_token": draft["question_token"],
                }})
            )
        else:
            proposal = self.service.clarification_proposal(draft["id"], today)
            if proposal:
                if draft["unresolved"]:
                    self.send(
                        "Three answers is the limit for this file version. The unresolved task stays in Clarify. "
                        "Review these exact changes; nothing has been saved yet.", None,
                    )
                self.service.telegram_card(proposal["id"], self.send)
            else:
                self.send(
                    "There is no missing definition question to answer here. Use Tasks to review scope, "
                    "select work or prepare an exact change. Nothing was changed.", None,
                )
        self._sent(event)
        return True
