"""Task-only Telegram adapter. Existing capture and knowledge namespaces stay separate."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
import re
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from briefing_plan import fingerprint
from briefing_state import parse_reply
from task_service import TaskError, TaskService
from task_state import workspace
from telegram_format import escape


_TASK_INTENT = re.compile(
    r"(?:\b(?:task|tasks|todo|to-do)\b|задач\w*|"
    r"\b(?:create|add|plan|estimate|finish|close|refine|execute)\s+(?:a\s+|the\s+)?task\b|"
    r"(?:создай|добавь|запланируй|оцени|закрой|выполни)\s+задач\w*|"
    r"\bwhat should I do (?:now|next|today)\b|"
    r"что (?:мне )?делать (?:сейчас|дальше|сегодня)|"
    r"^(?:I need to|remind me to|мне нужно|мне надо)\s)", re.I,
)
_APPROVE = {"yes", "approve", "да", "подтверждаю", "одобряю"}
_DECLINE = {"no", "skip", "decline", "нет", "пропустить", "отклонить"}


def task_assist_intent(text: str) -> bool:
    """Explicit capture commands and non-task conversation keep their existing route."""
    if not isinstance(text, str):
        return False
    command = re.match(r"^/([a-z]+)(?:@\w+)?(?:\s|$)", text, re.I)
    if command:
        return command[1].lower() in {"task", "tasks", "next", "plan"}
    if re.match(r"^(?:save|note|idea|diary|journal|n)\s*:", text, re.I):
        return False
    return bool(_TASK_INTENT.search(text))


class TaskAssistTelegram:
    """One owner DM; assist thread/message bindings never authorize other surfaces."""

    def __init__(self, assist: Any, *, send: Callable[[str, list | None], int]) -> None:
        self.assist, self.service, self.send = assist, assist.service, send

    def _data(self, state: dict[str, Any]) -> dict[str, Any]:
        return state.setdefault("telegram", {"bindings": {}, "events": {}, "latest": None})

    def _claim(self, event: str, payload: object, today: date) -> bool:
        key, digest = fingerprint(event), fingerprint(payload)

        def claim(state: dict[str, Any]) -> bool:
            data = self._data(state)
            existing = data["events"].get(key)
            if existing:
                if existing["digest"] != digest:
                    raise TaskError("assist_request_conflict")
                return False
            if len(data["events"]) >= 300:
                raise TaskError("assist_capacity")
            data["events"][key] = {"digest": digest, "status": "claimed", "created_on": today.isoformat()}
            return True

        return self.assist.store.update(claim)

    def _sent(self, event: str) -> None:
        self.assist.store.update(
            lambda state: self._data(state)["events"][fingerprint(event)].update(status="sent"),
        )

    def _binding(self, message_id: int, binding: dict[str, Any]) -> None:
        def save(state: dict[str, Any]) -> None:
            data = self._data(state)
            if len(data["bindings"]) >= 200:
                raise TaskError("assist_capacity")
            data["bindings"][str(message_id)] = binding
            if binding.get("proposal_id"):
                data["latest"] = binding.copy()
        self.assist.store.update(save)
        if binding.get("proposal_id"):
            def bind_proposal(state: dict[str, Any]) -> None:
                proposal_id = binding["proposal_id"]
                record = state["proposals"].get(proposal_id)
                if not record or record.get("approval_digest") != binding["approval_digest"]:
                    raise TaskError("task_source_changed")
                record["message_ids"].append(message_id)
                state["messages"][str(message_id)] = proposal_id
            self.service.store.update(bind_proposal)

    def _render(self, result: dict[str, Any], today: date) -> None:
        language = result.get("language", "en")
        russian = language == "ru"
        text = result["reply"]
        binding: dict[str, Any] = {"thread_id": result["thread_id"], "created_on": today.isoformat()}
        task_bindings: dict[str, dict[str, str]] = {}
        keyboard = []
        for card in result.get("cards", []):
            kind = card["kind"]
            if kind == "task":
                task_bindings.setdefault(card["path"], {"path": card["path"], "revision": card["revision"]})
                text += "\n\n" + card["title"]
            elif kind == "proposal":
                binding.update(proposal_id=card["proposal_id"], approval_digest=card["action_digest"])
                if card.get("task"):
                    task_bindings[card["task"]["path"]] = card["task"]
                text += "\n\n" + card["title"] + "\n" + card["summary"]
                keyboard.append([
                    {"text": "Одобрить" if russian else "Approve",
                     "callback_data": "brief1|approve|" + card["proposal_id"]},
                    {"text": "Пропустить" if russian else "Skip",
                     "callback_data": "brief1|decline|" + card["proposal_id"]},
                ])
                keyboard.append([{"text": "Изменить" if russian else "Edit",
                                  "callback_data": "assist1|edit|" + result["thread_id"]}])
            elif kind == "applied":
                text += "\n\n" + card["summary"]
                binding["receipt_id"] = card["receipt_id"]
                if card.get("task"):
                    task_bindings[card["task"]["path"]] = card["task"]
                if card.get("undo_available"):
                    keyboard.append([{"text": "Отменить" if russian else "Undo",
                                      "callback_data": "assist1|undo|" + card["receipt_id"]}])
                elif not card.get("saved") and card.get("status") not in {"failed", "invalidated", "conflict"}:
                    keyboard.append([{"text": "Проверить сохранение" if russian else "Check save status",
                                      "callback_data": "assist1|receipt|" + card["receipt_id"]}])
            elif kind in {"draft", "knowledge"}:
                text += "\n\n" + card["title"] + "\n" + card["body"]
            elif kind == "plan":
                text += "\n\n" + "\n".join(
                    f"{index + 1}. {step['text']} ({step['estimate_min']} min)"
                    for index, step in enumerate(card["steps"])
                )
            elif kind == "estimate":
                text += f"\n\n{card['minutes_low']}-{card['minutes_high']} min. {card['rationale']}"
            if kind in {"proposal", "applied"}:
                for change in card.get("diff", []):
                    text += f"\n{change['field']}: {change['before']} → {change['after']}"
        if len(task_bindings) == 1:
            binding["task"] = next(iter(task_bindings.values()))
        from telegram_format import escaped_chunks

        for part in escaped_chunks(text, 3500):
            message_id = self.send(part, keyboard or None)
            self._binding(message_id, binding)

    def _decision(self, binding: dict[str, Any], decision: str, today: date) -> None:
        result = self.service.decide({
            "proposal_id": binding["proposal_id"], "approval_digest": binding["approval_digest"],
            "decision": decision,
        }, today)
        self.send(escape(self.service.loop._receipt_text(result)), None)

    def handle(
        self, *, text: str, event: str, today: date, reply_to: int | None = None, voice: bool = False,
    ) -> bool:
        if text.startswith("/") and not task_assist_intent(text):
            return False
        if re.match(r"^(?:save|note|idea|diary|journal|n)\s*:", text, re.I):
            return False
        data = self._data(self.assist.store.read())
        binding = data["bindings"].get(str(reply_to)) if reply_to is not None else None
        short = text.strip().casefold().rstrip(".!")
        decision = "approve" if short in _APPROVE else "decline" if short in _DECLINE else None
        pending = binding if binding and binding.get("proposal_id") else data.get("latest")
        if decision and pending and (reply_to is None or binding is not None):
            if self._claim(event, [text, reply_to, voice], today):
                self._decision(pending, decision, today)
                self._sent(event)
            return True
        if not binding and not task_assist_intent(text):
            return False
        if reply_to is not None and not binding and not text.startswith("/"):
            return False
        if not self._claim(event, [text, reply_to, voice], today):
            return True
        command = re.match(r"^/(task|tasks|next|plan)(?:@\w+)?(?:\s|$)", text, re.I)
        request_id = str(uuid5(NAMESPACE_URL, "mindme-assist-telegram:" + event))
        if command and command[1].lower() == "tasks":
            overview = self.service.overview(0, today)
            items = [
                item for item in overview["items"]
                if item.get("attention_eligible") or item.get("stage") == "verify"
                or item.get("definition", {}).get("complete") is False
            ]
            lines = [
                "<b>" + escape(item["title"]) + "</b> — " + escape(item.get("next_action") or item["stage"])
                for item in items[:6]
            ]
            if not lines:
                lines = ["No focus, due or needs-you tasks in this permitted window."]
            if overview.get("attention_complete") is not True:
                lines.append("The inventory is incomplete; this is not proof that nothing is due.")
            self.send("\n\n".join(lines), None)
        elif command and command[1].lower() == "next":
            self._render(self.assist.step({
                "request_id": request_id, "step": "next", "task": None, "input": None, "language": "auto",
            }, today), today)
        else:
            task = binding.get("task") if binding else None
            if command and command[1].lower() == "task":
                result = self.assist.step({
                    "request_id": request_id, "step": "capture", "task": None,
                    "input": text[command.end():].strip(), "language": "auto",
                }, today)
            else:
                result = self.assist.chat({
                    "request_id": request_id, "message": text, "thread_id": binding["thread_id"] if binding else None,
                    "task": task, "language": "auto", "voice": voice,
                }, today)
            self._render(result, today)
        self._sent(event)
        return True

    def callback(self, *, message_id: int, data: str, event: str, today: date) -> bool:
        parts = data.split("|")
        if len(parts) != 3 or parts[0] != "assist1" or parts[1] not in {"undo", "edit", "receipt"}:
            raise TaskError("assist_request_invalid")
        binding = self._data(self.assist.store.read())["bindings"].get(str(message_id))
        field = "thread_id" if parts[1] == "edit" else "receipt_id"
        if not binding or binding.get(field) != parts[2]:
            raise TaskError("task_proposal_message_mismatch")
        if not self._claim(event, [message_id, data], today):
            return True
        if parts[1] == "edit":
            self.send("Reply to the task message with the changes you want. / "
                      "Ответьте на сообщение задачи: что нужно изменить?", None)
        else:
            operation = self.assist.undo if parts[1] == "undo" else self.assist.receipt
            self._render(operation({
                "request_id": str(uuid5(NAMESPACE_URL, "mindme-assist-telegram:" + event)),
                "receipt_id": parts[2],
            }, today), today)
        self._sent(event)
        return True


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
        decision_text = text.strip().casefold().rstrip(".!")
        decision = (
            "approve" if decision_text in _APPROVE else
            "decline" if decision_text in _DECLINE else parse_reply(text, today)["intent"]
        )
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
