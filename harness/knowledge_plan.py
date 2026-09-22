"""Bounded synthesis schema, evidence validation and source-linked presentation."""

from __future__ import annotations

import copy
import re
from typing import Any

from briefing_plan import public_research_question, safe_text
from briefing_sources import _SENSITIVE_CONTENT
from briefing_state import _SECRET

SECTION_LIMITS = {"explanation": 2, "agreement": 1, "conflict": 1, "gaps": 2, "understanding": 2}
MAX_QUOTE_CHARS = 16000
MAX_QUOTES_PER_SOURCE = 48


class KnowledgeError(ValueError):
    """Safe code only, without content from a source, model or user."""


def shown_reference(query: str, shown_text: object) -> tuple[str | None, bool]:
    """Bounded display context disambiguates references; it is never source evidence."""
    shown = (
        shown_text.strip()
        if isinstance(shown_text, str) and 20 <= len(shown_text.strip()) <= 4096
        and not _SECRET.search(shown_text)
        else None
    )
    ordinal = re.search(
        r"\b(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|\d+(?:st|nd|rd|th))"
        r"\s+(?:idea|point|takeaway|claim|item|step|example|argument|one)\b",
        query, re.I,
    )
    numbered = re.search(r"\b(?:idea|point|takeaway|claim|item|step|example|argument)\s*#?\s*(\d+)\b", query, re.I)
    deictic = re.search(r"\b(?:it|its|their|theirs|this|that|these|those|them|above|earlier|previous|former|latter)\b", query, re.I)
    quoted = bool(re.search(r"""["“']([^"”'\r\n]{12,240})["”']""", query))
    if not (ordinal or numbered or deictic) or quoted:
        return shown, False
    if shown is None:
        return None, True
    if ordinal or numbered:
        words = ("first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth")
        value = ordinal[1].casefold() if ordinal else numbered[1]
        number = words.index(value) + 1 if value in words else int(re.match(r"\d+", value)[0])
        marker = re.search(
            rf"(?im)^\s*(?:(?:idea|point|takeaway|claim|item|step|example|argument)\s+)?"
            rf"{number}\s*[.):—-]\s*\S", shown,
        )
        if marker is None:
            # An unlabelled continuation cannot establish the original list's offset.
            section = re.search(
                r"(?im)^\s*(?:what it says\s*[—–:-]\s*)?"
                r"(?:key ideas|main ideas|main takeaways|takeaways|ideas)\s*:?\s*$",
                shown,
            )
            bullets = 0
            if section:
                for line in shown[section.end():].splitlines():
                    if not line.strip():
                        continue
                    if re.match(r"^\s*[-*•]\s+\S", line):
                        bullets += 1
                    elif line[:1].isspace() and bullets:
                        continue
                    else:
                        break
            if bullets < number:
                return shown, True
    return shown, False


def public_knowledge_question(value: object) -> str:
    text = public_research_question(value)
    if _SECRET.search(text) or _SENSITIVE_CONTENT.search(text) or re.search(
        r"\b(?:my|our|employer|client|household|personal|private|repository|mindvault)\b", text, re.I,
    ):
        raise KnowledgeError("research_not_public")
    return text


def knowledge_schema(context: dict[str, Any]) -> dict[str, Any]:
    paths = [item["path"] for item in context["sources"]]
    citation = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "path": {"type": "string", "enum": paths},
            "quote": {"type": "string", "minLength": 12, "maxLength": 240},
        }, "required": ["path", "quote"],
    }
    item = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "text": {"type": "string", "minLength": 1, "maxLength": 600},
            "evidence": {"type": "array", "minItems": 1, "maxItems": 3, "items": citation},
        }, "required": ["text", "evidence"],
    }
    properties: dict[str, Any] = {
        key: {"type": "array", "maxItems": limit, "items": copy.deepcopy(item)}
        for key, limit in SECTION_LIMITS.items()
    }
    properties.update({
        "continuity": {"type": "string", "minLength": 1, "maxLength": 280},
        "experiment": {"anyOf": [
            {"type": "null"}, {"type": "string", "minLength": 1, "maxLength": 280},
        ]},
        "used_memory_ids": {
            "type": "array", "maxItems": 6,
            "items": {"type": "string", **(
                {"enum": [item["id"] for item in context["memories"]]} if context["memories"] else {}
            )},
        },
        "proposal": {
            "anyOf": [
                {"type": "null"},
                {"type": "object", "additionalProperties": False,
                 "properties": {"text": {"type": "string", "minLength": 1, "maxLength": 500}},
                 "required": ["text"]},
            ],
        },
    })
    return {
        "type": "object", "additionalProperties": False, "properties": properties,
        "required": list(properties),
    }


def _quote_candidates(text: str) -> list[str]:
    """Slice exact substrings, never normalize/reconstruct source wording."""
    quotes = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(("#", "<!--", "|--")):
            continue
        while len(line) >= 12:
            end = min(240, len(line))
            sentence = re.search(r"[.!?](?=\s|$)", line[:end])
            if sentence and sentence.end() >= 12:
                end = sentence.end()
            elif end < len(line):
                boundary = line.rfind(" ", 80, end)
                if boundary >= 80:
                    end = boundary
            quote = line[:end].strip()
            if len(quote) >= 12 and quote not in quotes:
                quotes.append(quote)
            line = line[end:].lstrip()
    return quotes


def knowledge_evidence_packet(context: dict[str, Any]) -> dict[str, Any]:
    """Like vault-evolve's evidence_packet: only host-issued quote IDs reach generation."""
    packet = {
        key: copy.deepcopy(context[key]) for key in (
            "action", "query", "memories", "warnings", "shown_message_reference",
        ) if key in context
    }
    packet.setdefault("memories", [])
    packet.setdefault("warnings", [])
    packet["sources"] = []
    remaining = MAX_QUOTE_CHARS
    query = context.get("query", "")
    reference = context.get("shown_message_reference", {})
    if isinstance(reference, dict):
        query += " " + str(reference.get("text", ""))
    terms = set(re.findall(r"\w{4,}", query.casefold())) - {
        "this", "that", "what", "which", "explain", "source", "idea", "second", "with", "from",
    }
    bounded_sources = context["sources"][:5]
    for index, source in enumerate(bounded_sources, 1):
        text = source.get("text")
        if not isinstance(text, str):
            raise KnowledgeError("invalid_evidence_excerpt")
        candidates = _quote_candidates(text[:10000])
        # Reserve opening context, rank the rest by the explicit query/displayed referent.
        ranked = sorted(
            enumerate(candidates),
            key=lambda pair: (
                pair[0] >= 2,
                -len(terms & set(re.findall(r"\w{4,}", pair[1].casefold()))),
                pair[0],
            ),
        )[:MAX_QUOTES_PER_SOURCE]
        selected = []
        source_budget = remaining // (len(bounded_sources) - index + 1)
        for _, quote in sorted(ranked):
            if len(quote) <= source_budget:
                selected.append(quote)
                remaining -= len(quote)
                source_budget -= len(quote)
        if not selected:
            continue
        identifier = f"S{index}"
        packet["sources"].append({
            "id": identifier, "path": source["path"], "title": source.get("title", ""),
            "quotes": {f"{identifier}Q{number}": quote for number, quote in enumerate(selected, 1)},
        })
        if len(selected) < len(candidates) or len(text) > 10000:
            packet["warnings"].append("Evidence quotes are bounded; some canonical excerpt material was not supplied to synthesis.")
    if not packet["sources"]:
        raise KnowledgeError("no_citable_evidence")
    packet["warnings"] = list(dict.fromkeys(packet["warnings"]))
    return packet


def knowledge_model_schema(packet: dict[str, Any]) -> dict[str, Any]:
    """Constrain each source/quote pair together, not independent combinable enums."""
    schema = knowledge_schema(packet)
    evidence = [
        {
            "type": "object", "additionalProperties": False,
            "properties": {
                "source": {"type": "string", "enum": [source["id"]]},
                "quote_id": {"type": "string", "enum": list(source["quotes"])},
            },
            "required": ["source", "quote_id"],
        }
        for source in packet["sources"]
    ]
    for key in SECTION_LIMITS:
        schema["properties"][key]["items"]["properties"]["evidence"]["items"] = {"anyOf": copy.deepcopy(evidence)}
    return schema


def hydrate_synthesis(raw: object, packet: dict[str, Any]) -> dict[str, Any]:
    """Restore immutable host quotes before the unchanged canonical-evidence validator."""
    if not isinstance(raw, dict) or set(raw) != set(knowledge_schema(packet)["properties"]):
        raise KnowledgeError("invalid_synthesis")
    result = copy.deepcopy(raw)
    sources = {source["id"]: source for source in packet["sources"]}
    for key, limit in SECTION_LIMITS.items():
        rows = result[key]
        if not isinstance(rows, list) or len(rows) > limit:
            raise KnowledgeError("invalid_synthesis_sections")
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"text", "evidence"}:
                raise KnowledgeError("invalid_synthesis_claim")
            if not isinstance(row["evidence"], list) or not 1 <= len(row["evidence"]) <= 3:
                raise KnowledgeError("invalid_evidence_reference")
            restored = []
            for citation in row["evidence"]:
                if (
                    not isinstance(citation, dict) or set(citation) != {"source", "quote_id"}
                    or not isinstance(citation["source"], str) or citation["source"] not in sources
                    or not isinstance(citation["quote_id"], str)
                    or citation["quote_id"] not in sources[citation["source"]]["quotes"]
                ):
                    raise KnowledgeError("invalid_evidence_reference")
                source = sources[citation["source"]]
                restored.append({"path": source["path"], "quote": source["quotes"][citation["quote_id"]]})
            row["evidence"] = restored
    return result


def validate_synthesis(raw: object, context: dict[str, Any]) -> dict[str, Any]:
    schema = knowledge_schema(context)
    if not isinstance(raw, dict) or set(raw) != set(schema["properties"]):
        raise KnowledgeError("invalid_synthesis")
    source_map = {item["path"]: item for item in context["sources"]}
    total = 0
    for key in ("explanation", "agreement", "conflict", "gaps", "understanding"):
        items = raw[key]
        if not isinstance(items, list) or len(items) > (5 if key == "explanation" else 2):
            raise KnowledgeError("invalid_synthesis_sections")
        total += len(items)
        for item in items:
            if not isinstance(item, dict) or set(item) != {"text", "evidence"}:
                raise KnowledgeError("invalid_synthesis_claim")
            safe_text(item["text"], 600)
            if _SECRET.search(item["text"]):
                raise KnowledgeError("unsafe_synthesis")
            evidence = item["evidence"]
            if not isinstance(evidence, list) or not 1 <= len(evidence) <= 3:
                raise KnowledgeError("unbacked_synthesis")
            for cite in evidence:
                if (
                    not isinstance(cite, dict) or set(cite) != {"path", "quote"}
                    or not isinstance(cite["path"], str) or cite["path"] not in source_map
                    or not isinstance(cite["quote"], str) or not 12 <= len(cite["quote"]) <= 240
                    or cite["quote"] not in source_map[cite["path"]]["text"]
                ):
                    raise KnowledgeError("unbacked_synthesis")
            if key in {"agreement", "conflict"} and len({cite["path"] for cite in evidence}) < 2:
                raise KnowledgeError("comparison_requires_two_sources")
    if not 1 <= total <= 8:
        raise KnowledgeError("synthesis_size")
    continuity = safe_text(raw["continuity"], 280)
    if "\n" in continuity or _SECRET.search(continuity) or _SENSITIVE_CONTENT.search(continuity):
        raise KnowledgeError("unsafe_continuity")
    allowed_memories = {item["id"] for item in context["memories"]}
    if raw["experiment"] is not None:
        safe_text(raw["experiment"], 280)
        if _SECRET.search(raw["experiment"]) or _SENSITIVE_CONTENT.search(raw["experiment"]):
            raise KnowledgeError("unsafe_experiment")
    used = raw["used_memory_ids"]
    if not isinstance(used, list) or len(used) > 6 or any(not isinstance(item, str) or item not in allowed_memories for item in used):
        raise KnowledgeError("invalid_memory_usage")
    proposal = raw["proposal"]
    if proposal is not None:
        if context["action"] not in {"dig", "apply"} or not isinstance(proposal, dict) or set(proposal) != {"text"}:
            raise KnowledgeError("unrequested_proposal")
        text = safe_text(proposal["text"], 500)
        if "\n" in text or _SECRET.search(text) or _SENSITIVE_CONTENT.search(text):
            raise KnowledgeError("unsafe_proposal")
        if context["action"] == "dig":
            public_knowledge_question(text)
    return copy.deepcopy(raw)


def render_synthesis(plan: dict[str, Any], context: dict[str, Any]) -> str:
    sources = {item["path"]: item for item in context["sources"]}
    lines = ["Topic brief" if context["action"] == "topic" else "Source-bound follow-up"]
    labels = {
        "explanation": "Explanation", "agreement": "Agreement (not independent verification)",
        "conflict": "Conflict / tension", "gaps": "Evidence gaps in this bounded selection",
        "understanding": "New understanding (interpretation)",
    }
    for key, label in labels.items():
        items = plan[key]
        if not items:
            if context["action"] == "topic" and key != "explanation":
                lines.append(f"\n{label}\nNot established by the selected evidence.")
            continue
        lines.append("\n" + label)
        for index, item in enumerate(items, 1):
            lines.append(f"{index}. {item['text']}")
            for cite in item["evidence"]:
                lines.append(f'“{cite["quote"]}”\n{sources[cite["path"]]["url"]}')
    lines.append("\nScope: canonical excerpts only; no external browsing. Claims belong to the cited sources, not verified truth.")
    if plan["experiment"]:
        lines.append("\nOptional experiment (proposal only)\n" + plan["experiment"])
    lines.extend(context.get("warnings", []))
    if plan["proposal"] is None:
        lines.append("No action started. Reply Dig or Apply to prepare a proposal.")
    return "\n".join(lines)
