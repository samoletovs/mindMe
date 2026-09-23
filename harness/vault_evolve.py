"""Bounded cloud adapter for the shared vault-evolve review receipt, version 1."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from briefing_plan import fingerprint, safe_text

MAX_SOURCES = 12
MAX_FINDINGS = 3
KINDS = ["maintenance", "conceptual", "evidence", "application", "connection"]
RELATIONSHIPS = ["supports", "contradicts", "extends", "duplicates", "applies_to"]
ACTIONS = ["curate", "research", "experiment", "remind", "explain", "no_action"]
BASIS_LABELS = {"observed": "from the source", "inferred": "interpretation", "question": "open question"}
QUESTION = "What useful connections, evidence gaps or small applications emerge from the selected mindVault knowledge and current approved focus?"


class EvolveError(RuntimeError):
    """Content-free review failure code."""


def evidence_packet(context: dict[str, Any], previous: list[dict[str, Any]]) -> dict[str, Any]:
    """Select only permitted knowledge, never the private mirror or generated reports."""
    sources = []
    remaining_quote_chars = 9000
    for source in context.get("sources", []):
        if source.get("kind") not in {"note", "wiki", "research", "idea", "project"}:
            continue
        if not re.fullmatch(r"[a-f0-9]{64}", source.get("sha256", "")):
            raise EvolveError("missing_byte_evidence")
        text = source.get("evidence_text")
        if not isinstance(text, str) or len(text) > 1500:
            raise EvolveError("invalid_evidence_excerpt")
        quotes = []
        for line in text.replace("\r\n", "\n").replace("\r", "\n").splitlines():
            if (
                10 <= len(line) <= min(500, remaining_quote_chars)
                and not line.lstrip().startswith(("#", "<!--", "|--"))
                and len(quotes) < 6
            ):
                quotes.append(line)
                remaining_quote_chars -= len(line)
        if not quotes:
            continue
        sources.append({
            "id": f"S{len(sources) + 1}", "path": source["path"],
            "sha256": source["sha256"], "revision": source["revision"],
            "title": source["title"], "url": source["url"],
            "quotes": {f"Q{i + 1}": text for i, text in enumerate(quotes[:6])},
        })
        if len(sources) == MAX_SOURCES:
            break
    paths = {source["path"]: source["sha256"] for source in sources}
    recent = []
    for record in previous:
        report = record.get("review", {})
        manifest = {source["id"]: source for source in report.get("sources", [])}
        delivered_findings = {
            part.get("finding")
            for index, part in enumerate(record.get("parts", []))
            if index < len(record.get("message_ids", []))
        }
        for finding in report.get("findings", []):
            if finding["id"] not in delivered_findings:
                continue
            cited = [manifest[item["source"]] for item in finding["evidence"]]
            if any(paths.get(source["path"]) != source["sha256"] for source in cited):
                continue
            recent.append({
                "signature": finding_signature(finding, manifest),
                "statement": finding["statement"][:350],
                "feedback": record.get("feedback", {}).get(finding["id"], {}),
            })
    return {
        "date": context["date"], "question": QUESTION, "sources": sources,
        "focus": [
            {"title": goal["title"], "text": goal["text"][:700]}
            for goal in context.get("goals", [])[:5]
        ],
        "limitations": list(dict.fromkeys([
            "A bounded selection of non-sensitive mindVault excerpts, not the whole vault.",
            "Work vaults, the private mirror and generated reviews are excluded. Personal knowledge is not assessed.",
            *context.get("warnings", []),
        ]))[:8],
        "previous_findings": recent[-12:],
        "previous_decisions": [
            {"signature": item["signature"], "review_on": item["feedback"].get("review_on")}
            for item in recent
        ],
    }


def finding_signature(finding: dict[str, Any], manifest: dict[str, Any]) -> str:
    versions = sorted({
        (
            manifest[item["source"]]["path"], manifest[item["source"]]["sha256"],
            item["quote"].replace("\r\n", "\n").replace("\r", "\n"),
        )
        for item in finding["evidence"]
    })
    return fingerprint([finding["kind"], versions])


def review_schema(packet: dict[str, Any]) -> dict[str, Any]:
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
    finding = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "kind": {"type": "string", "enum": KINDS},
            "basis": {"type": "string", "enum": ["observed", "inferred", "question"]},
            "statement": {"type": "string"},
            "relationship": {"type": "string", "enum": ["none", *RELATIONSHIPS]},
            "evidence": {
                "type": "array", "minItems": 1, "maxItems": 3,
                "items": {"anyOf": evidence} if evidence else {"type": "null"},
            },
            "action": {"type": "string", "enum": ACTIONS},
            "next_step": {"type": "string"},
        },
        "required": ["kind", "basis", "statement", "relationship", "evidence", "action", "next_step"],
    }
    ordinary = deepcopy(finding)
    ordinary["properties"]["kind"]["enum"] = [kind for kind in KINDS if kind != "connection"]
    ordinary["properties"]["relationship"]["enum"] = ["none"]
    connection = deepcopy(finding)
    connection["properties"]["kind"]["enum"] = ["connection"]
    connection["properties"]["relationship"]["enum"] = RELATIONSHIPS
    connection["properties"]["evidence"]["minItems"] = 2
    return {
        "type": "object", "additionalProperties": False,
        "properties": {"findings": {
            "type": "array", "maxItems": MAX_FINDINGS if evidence else 0,
            "items": {"anyOf": [ordinary, connection]},
        }},
        "required": ["findings"],
    }


def complete_review(raw: object, packet: dict[str, Any]) -> dict[str, Any]:
    """Resolve quote IDs, not model-written quotes, and enforce proposal-only semantics."""
    if not isinstance(raw, dict) or set(raw) != {"findings"}:
        raise EvolveError("invalid_review_shape")
    rows = raw["findings"]
    if not isinstance(rows, list) or len(rows) > MAX_FINDINGS:
        raise EvolveError("review_capacity")
    sources = {source["id"]: source for source in packet["sources"]}
    review: dict[str, Any] = {
        "version": 1, "as_of": packet["date"],
        "scope": {
            "question": QUESTION, "coverage": "bounded",
            "personal_knowledge": "not_assessed", "limitations": packet["limitations"],
        },
        "sources": [
            {key: source[key] for key in ("id", "path", "sha256")}
            for source in packet["sources"]
        ],
        "findings": [], "proposals": [],
    }
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "kind", "basis", "statement", "relationship", "evidence", "action", "next_step",
        }:
            raise EvolveError("invalid_finding_shape")
        if (
            row["kind"] not in KINDS or row["basis"] not in ("observed", "inferred", "question")
            or row["action"] not in ACTIONS
            or not isinstance(row["evidence"], list) or not 1 <= len(row["evidence"]) <= 3
        ):
            raise EvolveError("invalid_finding")
        finding: dict[str, Any] = {
            "id": f"F{len(review['findings']) + 1}",
            "kind": row["kind"], "basis": row["basis"],
            "statement": safe_text(row["statement"]), "evidence": [],
        }
        for item in row["evidence"]:
            if not isinstance(item, dict) or set(item) != {"source", "quote_id"}:
                raise EvolveError("invalid_evidence_reference")
            source = sources.get(item["source"]) if isinstance(item["source"], str) else None
            if source is None or not isinstance(item["quote_id"], str) or item["quote_id"] not in source["quotes"]:
                raise EvolveError("unavailable_evidence")
            finding["evidence"].append({
                "source": item["source"], "quote": source["quotes"][item["quote_id"]],
            })
        if row["kind"] == "connection":
            if row["relationship"] not in RELATIONSHIPS or len({item["source"] for item in finding["evidence"]}) < 2:
                raise EvolveError("ungrounded_connection")
            finding["relationship"] = row["relationship"]
        elif row["relationship"] != "none":
            raise EvolveError("unexpected_relationship")
        signature = finding_signature(finding, sources)
        previous = [
            item for item in packet["previous_decisions"] if item["signature"] == signature
        ]
        if previous:
            revisit = previous[-1]["review_on"]
            if not revisit or revisit > packet["date"]:
                continue
        if any(finding_signature(item, sources) == signature for item in review["findings"]):
            continue
        review["findings"].append(finding)
        review["proposals"].append({
            "id": f"P{len(review['proposals']) + 1}", "finding": finding["id"],
            "action": row["action"], "status": "proposed",
            "next_step": safe_text(row["next_step"]),
        })
    return deepcopy(review)


def telegram_parts(review: dict[str, Any], receipt: dict[str, Any], packet: dict[str, Any]) -> list[dict[str, Any]]:
    date_key = review["as_of"]
    status = receipt.get("status")
    location = (
        f"Review draft: {receipt['pr_url']}\n"
        + ("Merged into mindVault." if status == "merged" else "Submitted for review; not yet added to mindVault.")
        if status in {"submitted", "merged"}
        else "Review saved privately. No new review request or work was created."
    )
    parts: list[dict[str, Any]] = [{
        "text": (
            f"Daily knowledge review - {date_key}\n{location}\n"
            f"Checked excerpts from {len(review['sources'])} sources, not the whole vault. "
            "This does not test what you know. Research, tasks and note edits still need approval."
            + ("\nNothing new to suggest from these sources." if not review["findings"] else "")
        ),
        "finding": None, "keyboard": None,
    }]
    sources = {source["id"]: source for source in packet["sources"]}
    proposals = {proposal["finding"]: proposal for proposal in review["proposals"]}
    labels = {
        "maintenance": "Note to update", "conceptual": "Idea to explore",
        "evidence": "Evidence to check", "application": "Something to try",
        "connection": "Related ideas",
    }
    for finding in review["findings"]:
        proposal = proposals[finding["id"]]
        links = list(dict.fromkeys(sources[item["source"]]["url"] for item in finding["evidence"]))
        text = (
            f"{labels[finding['kind']]} ({BASIS_LABELS[finding['basis']]})\n"
            f"{finding['statement']}\n\nPossible next step: {proposal['next_step']}\n\n"
            + "\n".join(links)
            + "\n\nReply with feedback about this idea, or use a button below. "
            "Feedback does not approve work."
        )
        parts.append({
            "text": text, "finding": finding["id"],
            "keyboard": [[
                {"text": label, "callback_data": f"evolve1|{verb}|{date_key}|{finding['id']}"}
                for label, verb in (("Useful", "useful"), ("Already know", "known"), ("Not useful", "dismiss"))
            ]],
        })
    if any(len(part["text"]) > 4000 for part in parts):
        raise EvolveError("telegram_review_capacity")
    return parts
