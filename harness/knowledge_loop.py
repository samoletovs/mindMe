"""Connected capture replies, governed continuity and read-only topic synthesis."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from datetime import date, timedelta
from typing import Any

from briefing_loop import BriefingLoop
from briefing_plan import fingerprint
from briefing_state import BriefingStore, _SECRET, parse_reply, record_transition
from knowledge_plan import KnowledgeError, render_synthesis, shown_reference, validate_synthesis
from knowledge_state import consolidate, decide_write, forget_memory, mark_used, recall
from weekly_plan import render_weekly_proposal


class KnowledgeLoop:
    def __init__(
        self, *, store: BriefingStore, briefing: BriefingLoop,
        lookup: Callable[[int, str | None], dict[str, Any] | None],
        read: Callable[[str], dict[str, Any] | None],
        retrieve: Callable[[str, tuple[str, ...]], dict[str, Any]],
        generate: Callable[[dict[str, Any]], dict[str, Any]],
        send: Callable[[str], list[int]],
        send_parts: Callable[[str], Iterable[int]] | None = None,
    ) -> None:
        self.store, self.briefing = store, briefing
        self.lookup, self.read, self.retrieve = lookup, read, retrieve
        self.generate, self.send = generate, send
        self.send_parts = send_parts

    def resolve(self, message_id: int, today: date, key: str | None = None) -> dict[str, Any] | None:
        if type(message_id) is not int or message_id <= 0:
            raise KnowledgeError("invalid_context_message")
        saved = self.store.read()["knowledge"]["bindings"].get(str(message_id))
        # Capture buttons always round-trip to the capture owner's confirmed receipt.
        pointer = self.lookup(message_id, key) if key is not None or saved is None else None
        if key is not None and pointer is None:
            return None
        if not saved and not pointer:
            return None
        if saved and (saved.get("invalidated") or saved["expires_on"] <= today.isoformat()):
            raise KnowledgeError("context_expired")
        paths = list(saved["sources"]) if saved else [pointer["source_path"]]
        sources = []
        current = {}
        for path in paths:
            source = self.read(path)
            current[path] = source["revision"] if source else None
            if source:
                sources.append(source)
        self.store.update(lambda state: consolidate(state, revisions=current, today=today))
        if not saved and len(sources) != len(paths):
            raise KnowledgeError("canonical_source_pending_or_unavailable")
        if len(sources) != len(paths) or (saved and current != saved["sources"]):
            raise KnowledgeError("context_source_changed")
        if pointer and pointer["source_path"] not in current:
            raise KnowledgeError("context_binding_mismatch")
        binding = {
            "sources": current, "created_on": today.isoformat(),
            "expires_on": (today + timedelta(days=14)).isoformat(),
        }
        if not saved:
            self.store.update(lambda state: state["knowledge"]["bindings"].setdefault(str(message_id), binding))
        return {"sources": sources, "binding": binding}

    def handle(
        self, *, message_id: int, text: str, event: str, today: date,
        action: str | None = None, capture_key: str | None = None,
        shown_text: str | None = None,
    ) -> bool:
        context = self.resolve(message_id, today, capture_key)
        if context is None:
            return False
        if len(text) > 1200 or _SECRET.search(text):
            self.send("Use one focused, non-sensitive question of at most 1,200 characters; do not include credentials.")
            return True
        normalized = text.strip().casefold()
        decision = parse_reply(text, today)
        if action is None:
            action = (
                "known" if normalized in {"already familiar", "already known", "known"} else
                "useful" if normalized == "useful" else
                "dig" if re.match(r"^(?:dig|research)\b", normalized) else
                "apply" if re.match(r"^apply\b", normalized) else
                "topic" if re.match(r"^(?:topic|compare|synthesize)\b", normalized) else "explain"
            )
        if action not in {"explain", "dig", "apply", "topic", "known", "useful"}:
            raise KnowledgeError("unknown_capture_action")
        if decision["intent"] in {"approve", "done", "decline", "change", "snooze"}:
            self.send("This is source context, not approval. Reply to the specific proposal card; no action was taken.")
            return True
        refs = context["binding"]["sources"]
        request_id = fingerprint([event, message_id, action])[:32]
        if not self._claim(request_id, today):
            return True
        try:
            if action in {"known", "useful"} or decision["intent"] == "correct":
                kind = "correction" if decision["intent"] == "correct" else "feedback"
                value = decision["text"] if kind == "correction" else (
                    "Explicitly already familiar with this source; skip basics unless asked."
                    if action == "known" else "Explicitly useful source; prefer practical follow-through when requested."
                )
                self.store.update(lambda state: decide_write(state, kind=kind, text=value, sources=refs, today=today))
                self._send_bound(
                    request_id, "Scoped feedback saved. Inspect /knowledge or delete it there. "
                    "This does not create a permanent interest or approve work.", refs, today,
                )
                return True
            shown, clarify = shown_reference(text, shown_text)
            if clarify:
                self._send_bound(
                    request_id,
                    "Please quote the idea or sentence you mean in a short reply. I cannot reliably "
                    "identify that reference from the available shown message, and the canonical "
                    "note may order its ideas differently. No action was taken.", refs, today,
                )
                return True
            self._synthesize(context["sources"], text, action, request_id, today, shown_text=shown)
        except Exception:
            self.store.update(lambda state: state["knowledge"]["requests"][request_id].update(status="uncertain"))
            raise
        return True

    def _claim(self, identifier: str, today: date) -> bool:
        def claim(state: dict[str, Any]) -> bool:
            consolidate(state, revisions={}, today=today)
            requests = state["knowledge"]["requests"]
            if identifier in requests:
                return False
            requests[identifier] = {
                "status": "claimed", "created_on": today.isoformat(),
                "expires_on": (today + timedelta(days=35)).isoformat(),
            }
            return True

        return self.store.update(claim)

    def _finish(self, identifier: str, ids: list[int], refs: dict[str, str], today: date) -> None:
        if not ids or any(type(item) is not int or item <= 0 for item in ids):
            raise KnowledgeError("unconfirmed_followup_delivery")

        def finish(state: dict[str, Any]) -> None:
            state["knowledge"]["requests"][identifier]["status"] = "sent"
            for message_id in ids:
                state["knowledge"]["bindings"][str(message_id)] = {
                    "sources": refs, "created_on": today.isoformat(),
                    "expires_on": (today + timedelta(days=14)).isoformat(),
                }

        self.store.update(finish)

    def _send_bound(self, identifier: str, text: str, refs: dict[str, str], today: date) -> None:
        ids = []
        sender = self.send_parts or self.send
        for message_id in sender(text):
            if type(message_id) is not int or message_id <= 0:
                raise KnowledgeError("unconfirmed_followup_delivery")

            def checkpoint_message(state: dict[str, Any]) -> None:
                state["knowledge"]["bindings"][str(message_id)] = {
                    "sources": refs, "created_on": today.isoformat(),
                    "expires_on": (today + timedelta(days=14)).isoformat(),
                }
                state["knowledge"]["requests"][identifier].setdefault("message_ids", []).append(message_id)

            self.store.update(checkpoint_message)
            ids.append(message_id)
        self._finish(identifier, ids, refs, today)

    def _synthesize(
        self, sources: list[dict[str, Any]], query: str, action: str, request_id: str, today: date,
        *, selection: dict[str, Any] | None = None,
        shown_text: str | None = None,
    ) -> None:
        warnings = []
        if action == "topic":
            selection = selection or self.retrieve(query + " " + " ".join(item["title"] for item in sources), tuple(item["path"] for item in sources))
            # Never replace the current binding with another revision from a concurrent read.
            selected = {item["path"]: item for item in selection["sources"]}
            for source in sources:
                if source["path"] in selected and selected[source["path"]]["revision"] != source["revision"]:
                    raise KnowledgeError("context_source_changed")
            sources = [
                *[{**source, "text": source["text"][:1500]} for source in sources],
                *[item for path, item in selected.items() if path not in {source["path"] for source in sources}],
            ][:5]
            warnings = selection["warnings"]
        if len(sources) > 1:
            sources = [{**item, "text": item["text"][:3000]} for item in sources]
            warnings.append("Multi-source context uses at most 3,000 characters per source.")
        refs = {item["path"]: item["revision"] for item in sources}
        state = self.store.read()
        memories = recall(state, query=query, sources=refs, today=today)
        context = {
            "action": action, "query": query, "sources": sources,
            "memories": memories, "warnings": warnings,
        }
        if shown_text is not None:
            context["shown_message_reference"] = {
                "text": shown_text, "purpose": "untrusted reference disambiguation only",
                "is_evidence": False, "grants_permission": False,
            }
        if any(item.get("bounded") for item in sources):
            context["warnings"].append("The source exceeded the 10,000-character excerpt bound; unseen material is not covered.")
        plan = validate_synthesis(self.generate(context), context)
        # Recheck all evidence after synthesis, before retaining content or preparing a card.
        self._check_sources(refs, today)
        text = render_synthesis(plan, context)
        topic_id = fingerprint([query.casefold().strip(), refs, plan])[:24] if action == "topic" else None

        def retain(current: dict[str, Any]) -> None:
            if topic_id:
                previous = [
                    key for key, item in current["knowledge"]["topics"].items()
                    if item.get("topic_key") == fingerprint(query.casefold().strip())[:24]
                    and key != topic_id and item.get("active", True)
                ]
                for key in previous:
                    current["knowledge"]["topics"][key]["active"] = False
                current["knowledge"]["topics"][topic_id] = {
                    "sources": refs, "text": text, "created_on": today.isoformat(),
                    "expires_on": (today + timedelta(days=35)).isoformat(),
                    "topic_key": fingerprint(query.casefold().strip())[:24],
                    "supersedes": previous[-1] if previous else None,
                    "memory_ids": plan["used_memory_ids"],
                    "active": True,
                }

        self.store.update(retain)
        self._send_bound(request_id, text + (f"\n\nRetained topic receipt: {topic_id}" if topic_id else ""), refs, today)
        def remember(current: dict[str, Any]) -> None:
            mark_used(current, plan["used_memory_ids"], today)
            decide_write(
                current, kind="working", text=plan["continuity"], sources=refs, today=today,
                memory_ids=plan["used_memory_ids"],
            )

        self.store.update(remember)
        if plan["proposal"]:
            self._proposal(plan["proposal"]["text"], action, sources, today)

    def _check_sources(self, refs: dict[str, str], today: date) -> None:
        revisions = {}
        for path, revision in refs.items():
            source = self.read(path)
            revisions[path] = source["revision"] if source else None
            if source is None or source["revision"] != revision:
                self.store.update(lambda state: consolidate(state, revisions=revisions, today=today))
                raise KnowledgeError("context_source_changed")

    def _proposal(self, text: str, action: str, sources: list[dict[str, Any]], today: date) -> None:
        source = sources[0]
        kind = "research" if action == "dig" else "create_task"
        refs = {item["path"]: item["revision"] for item in sources}
        identifier = fingerprint(["knowledge", kind, refs, text, today.isoformat()])[:24]
        record = {
            "id": identifier, "kind": kind, "text": text,
            "source_path": source["path"], "source_revision": source["revision"],
            "source_digest": source["digest"], "source_url": source["url"],
            "status": "pending", "created_on": today.isoformat(),
            "expires_on": (today + timedelta(days=14)).isoformat(), "message_ids": [],
            "action": {"kind": kind, "text": text}, "knowledge_sources": refs,
            "why": "Requested from this canonical source. Nothing executes until this specific card is approved.",
        }

        def prepare(state: dict[str, Any]) -> bool:
            for previous in state["proposals"].values():
                if (
                    previous.get("knowledge_sources") == refs and previous["kind"] == kind
                    and previous["status"] == "pending" and previous["expires_on"] <= today.isoformat()
                    and not previous.get("action_id") and not previous.get("result")
                ):
                    record_transition(previous, "expired", today)
            if identifier in state["proposals"]:
                return False
            # Do not paraphrase the same scope into repeated pending/declined/completed work.
            if any(item.get("knowledge_sources") == refs and item["kind"] == kind
                   and item["status"] not in {"expired", "invalidated", "superseded"}
                   for item in state["proposals"].values()):
                return False
            state["proposals"][identifier] = record
            return True

        if not self.store.update(prepare):
            self.send("A proposal for this source and action is already recorded. Inspect /proposals all; no duplicate work was created.")
            return
        rendered, keyboard = render_weekly_proposal(record, 1, 1)
        message_id = self.briefing.send_html(rendered, keyboard)

        def bind(state: dict[str, Any]) -> None:
            state["proposals"][identifier]["message_ids"] = [message_id]
            state["messages"][str(message_id)] = identifier

        self.store.update(bind)

    def command(self, text: str, today: date, event: str) -> None:
        """Inspection is explicit, paginated and idempotently deletable."""
        self.store.update(lambda state: consolidate(state, revisions={}, today=today))
        command, _, argument = text.partition(" ")
        argument = argument.strip()
        if command == "/knowledge" and argument.startswith("proposal "):
            identifier = argument[9:].strip()
            proposal = self.store.read()["proposals"].get(identifier)
            if not proposal or not proposal.get("knowledge_sources") or proposal["status"] != "pending":
                self.send("Use /knowledge proposal <pending-proposal-id> from /proposals all. This explicitly re-presents a card, never re-executes an action.")
                return
            if proposal["expires_on"] <= today.isoformat():
                self.send("That proposal expired. Request a new source-bound proposal.")
                return
            self._check_sources(proposal["knowledge_sources"], today)
            request_id = fingerprint([event, "present-proposal", identifier])[:32]
            if not self._claim(request_id, today):
                return
            try:
                rendered, keyboard = render_weekly_proposal(proposal, 1, 1)
                message_id = self.briefing.send_html(rendered, keyboard)

                def bind(state: dict[str, Any]) -> None:
                    state["proposals"][identifier]["message_ids"].append(message_id)
                    state["messages"][str(message_id)] = identifier
                    state["knowledge"]["requests"][request_id]["status"] = "sent"

                self.store.update(bind)
            except Exception:
                self.store.update(lambda state: state["knowledge"]["requests"][request_id].update(status="uncertain"))
                raise
            return
        if command == "/knowledge" and (argument == "receipts" or argument.startswith("receipts ")):
            pieces = argument.split()
            page = int(pieces[1]) if len(pieces) == 2 and pieces[1].isdigit() else 1
            state = self.store.read()["knowledge"]
            rows = [
                f"Request {identifier}: {item['status']}; {item['created_on']}; expires {item['expires_on']}"
                for identifier, item in state["requests"].items()
            ] + [
                f"Binding {identifier}: expires {item['expires_on']}; "
                + ("invalidated; " if item.get("invalidated") else "")
                + ", ".join(f"{path} @ {sha}" for path, sha in item["sources"].items())
                for identifier, item in state["bindings"].items()
            ]
            self.send(f"Operational receipts page {page}. Uncertain requests are never silently replayed.\n"
                      + "\n".join(rows[(max(1, page) - 1) * 10:max(1, page) * 10])
                      + "\n/knowledge forget bindings removes follow-up bindings. Action receipts remain under /proposals all.")
            return
        if command == "/knowledge" and argument == "forget bindings":
            self.store.update(lambda state: state["knowledge"]["bindings"].clear())
            self.send("Follow-up bindings removed. Capture buttons still require memex's confirmed receipt. Action/request replay guards are preserved.")
            return
        if argument.startswith("forget "):
            identifier = argument[7:].strip()
            if not re.fullmatch(r"[a-f0-9]{24}", identifier):
                self.send("Use /knowledge forget <id> or /topics forget <id> from the inspection list.")
                return

            def forget(state: dict[str, Any]) -> None:
                if command == "/knowledge":
                    forget_memory(state, identifier)
                else:
                    state["knowledge"]["topics"].pop(identifier, None)

            self.store.update(forget)
            self.send("Removed idempotently. Canonical sources and action receipts are unchanged.")
            return
        if command == "/topics" and argument and not re.fullmatch(r"[a-f0-9]{24}|\d+", argument):
            if len(argument) > 400:
                self.send("Use a topic of at most 400 characters.")
                return
            selection = self.retrieve(argument, ())
            if not selection["sources"]:
                self.send("No relevant evidence in the bounded canonical selection. This is not proof that the vault has no relevant source.")
                return
            request_id = fingerprint([event, "topic"])[:32]
            if self._claim(request_id, today):
                try:
                    self._synthesize(selection["sources"], argument, "topic", request_id, today, selection=selection)
                except Exception:
                    self.store.update(lambda state: state["knowledge"]["requests"][request_id].update(status="uncertain"))
                    raise
            return
        records = self.store.read()["knowledge"]["topics" if command == "/topics" else "memories"]
        if argument in records:
            item = records[argument]
            self._check_sources(item["sources"], today)
            self.send(self._describe(argument, item))
            return
        if argument and not argument.isdigit():
            self.send("Use /knowledge [page|id], /knowledge forget <id>, /topics [page|id|query], or /topics forget <id>.")
            return
        page = max(1, int(argument or "1"))
        # Each record can depend on five sources; three records fit the 16-read bound.
        items = sorted(records.items(), reverse=True)[(page - 1) * 3:page * 3]
        revisions = {}
        for _, item in items:
            for path in item["sources"]:
                if path not in revisions:
                    if len(revisions) >= 16:
                        raise KnowledgeError("inspection_source_bound")
                    source = self.read(path)
                    revisions[path] = source["revision"] if source else None
        self.store.update(lambda state: consolidate(state, revisions=revisions, today=today))
        refreshed = self.store.read()["knowledge"]["topics" if command == "/topics" else "memories"]
        items = [(identifier, item) for identifier, item in items if identifier in refreshed]
        self.send(
            f"{command} page {page}; {len(refreshed)} retained records. Use {command} <id> to inspect, {command} forget <id> to delete.\n\n"
            + ("\n\n".join(self._describe(identifier, item, compact=True) for identifier, item in items) or "No records on this page.")
        )

    @staticmethod
    def _describe(identifier: str, item: dict[str, Any], *, compact: bool = False) -> str:
        text = item["text"]
        if compact and len(text) > 280:
            text = text[:280] + "… (inspect the ID for the complete retained brief)"
        refs = "\n".join(f"{path} @ {sha}" for path, sha in item["sources"].items())
        return (
            f"{identifier} [{item.get('kind', 'topic')}; {'active' if item.get('active', True) else 'superseded'}]\n{text}\n"
            f"{refs}\nCreated {item['created_on']}; expires {item.get('expires_on', 'on source invalidation/deletion')}; "
            f"used {item.get('use_count', 0)}, last used {item.get('last_used_on') or 'never'}"
            + (f"\nSupersedes: {item['supersedes']}" if item.get("supersedes") else "")
        )

    def maintenance(self, today: date) -> None:
        """Existing morning/weekly work only; no model, external research or new schedule."""
        state = self.store.read()
        active_since = (today - timedelta(days=1)).isoformat()
        if not any(item["created_on"] >= active_since for item in state["knowledge"]["requests"].values()):
            self.store.update(lambda current: consolidate(current, revisions={}, today=today))
            return
        paths = sorted({
            path for key in ("memories", "bindings", "topics")
            for item in state["knowledge"][key].values() for path in item["sources"]
        } | {
            path for item in state["proposals"].values() for path in item.get("knowledge_sources", {})
        })
        # Fail closed for inspections/recall; a bounded maintenance pass rotates daily.
        offset = (today.toordinal() * 16) % max(1, len(paths))
        selected = (paths[offset:] + paths[:offset])[:16]
        revisions = {}
        for path in selected:
            source = self.read(path)
            revisions[path] = source["revision"] if source else None
        self.store.update(lambda current: consolidate(current, revisions=revisions, today=today))
