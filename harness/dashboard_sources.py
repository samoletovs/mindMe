"""Display-only canonical projections. Generated reviews never become model inputs."""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from datetime import date
from typing import Any
from urllib.parse import unquote, urlsplit

from briefing_plan import fingerprint
from briefing_state import _SECRET
from briefing_sources import (
    SourceError, _DRAFT, _HEADING, _REVIEW_SENSITIVE_CONTENT, _SENSITIVE_CONTENT,
    _SENSITIVE_PATH, _canonical_inventory, _excluded_metadata, _focus, _kind,
    _material, _metadata, _review_source_allowed, _strip_generated,
)
from task_sources import TaskRepository
from vault_evolve import ACTIONS, BASIS_LABELS, KINDS, MAX_FINDINGS, MAX_SOURCES, RELATIONSHIPS

READ_LIMIT = 32
BYTE_LIMIT = 512_000
FILE_LIMIT = 64_000
PAGE_SIZE = 4
SOURCE_PAGE_SIZE = 12
TEXT_LIMIT = 10_000
DAILY = re.compile(r"reviews/vault-evolve/(\d{4}-\d{2}-\d{2})/review\.json\Z")
DIGEST = re.compile(r"reviews/digests/(\d{4})-W(\d{2})-digest\.md\Z")
HEX40 = re.compile(r"[a-f0-9]{40}\Z")
HEX64 = re.compile(r"[a-f0-9]{64}\Z")
LINK = re.compile(r"!?\[([^\]\n]*)\]\(([^)\n]+)\)|\[\[([^\]\n]+)\]\]")


class DashboardError(ValueError):
    """Only a fixed, content-free reason may cross the web boundary."""


def source_id(path: str) -> str:
    return fingerprint(["dashboard-source-v1", path])


def _day(value: object) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise DashboardError("dashboard_schema_invalid")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise DashboardError("dashboard_schema_invalid") from None


def _text(value: object, limit: int = 2000) -> str:
    if (
        not isinstance(value, str) or not value.strip() or len(value) > limit
        or _SENSITIVE_CONTENT.search(value) or _REVIEW_SENSITIVE_CONTENT.search(value) or _SECRET.search(value)
        or any(ord(char) < 32 and char not in "\n\t\r" for char in value)
    ):
        raise DashboardError("dashboard_withheld")
    return value


def _private(raw: str, *, generated: bool = False) -> None:
    _text(raw, FILE_LIMIT)
    _, pairs = _metadata(raw)
    if _excluded_metadata(pairs):
        raise DashboardError("dashboard_withheld")
    for key, value in pairs:
        value = value.casefold()
        if key in {"ignored", "work", "private", "sensitive", "confidential"} and value not in {"", "false", "no", "0"}:
            raise DashboardError("dashboard_withheld")
        if key in {"scope", "routed_to"} and value not in {"", "personal", "mindme", "mindvault", "non-sensitive", "personal-non-sensitive"}:
            raise DashboardError("dashboard_withheld")
        if not generated and key in {"generated", "derived"} and value not in {"", "false", "no", "0", "none"}:
            raise DashboardError("dashboard_withheld")
    for line in raw.splitlines():
        heading = _HEADING.match(line)
        if heading and _SENSITIVE_PATH.search(heading[2]):
            raise DashboardError("dashboard_withheld")


def display_kind(path: str) -> str | None:
    if DAILY.fullmatch(path):
        return "daily_review"
    if DIGEST.fullmatch(path):
        return "weekly_digest"
    kind = _kind(path)
    return kind if kind in {"wiki", "note", "idea", "project", "research"} else None


def _report_order(path: str) -> tuple[date, str]:
    try:
        if match := DAILY.fullmatch(path):
            return date.fromisoformat(match[1]), path
        if match := DIGEST.fullmatch(path):
            return date.fromisocalendar(int(match[1]), int(match[2]), 7), path
    except ValueError:
        return date.min, path
    return date.min, path


def _link_target(match: re.Match[str], parent: str) -> tuple[str | None, str]:
    label, target = (match[1], match[2]) if match[2] is not None else (
        match[3].split("|", 1)[-1], match[3].split("|", 1)[0],
    )
    target = target.strip()
    if target.startswith("#"):
        return None, label
    try:
        url = urlsplit(target)
    except ValueError:
        raise DashboardError("dashboard_withheld") from None
    if url.scheme:
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            raise DashboardError("dashboard_withheld")
        _text(target, 2048)
        return None, label
    if url.netloc or url.query or target.startswith(("/", "\\")):
        raise DashboardError("dashboard_withheld")
    path = unquote(url.path)
    if any(char in path for char in "\\%?#:") or any(ord(char) < 32 for char in path):
        raise DashboardError("dashboard_withheld")
    if path.startswith(("wiki/", "notes/", "ideas/", "areas/", "projects/", "tasks/", "reviews/")):
        resolved = posixpath.normpath(path)
    else:
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(parent), path))
    if not resolved.endswith(".md"):
        resolved += ".md"
    if _kind(resolved) not in {"wiki", "note", "idea", "project", "research"}:
        raise DashboardError("dashboard_withheld")
    return resolved, label


def _display_text(raw: str, path: str, allowed: set[str]) -> str:
    _supported_references(raw)

    def replace(match: re.Match[str]) -> str:
        try:
            target, label = _link_target(match, path)
        except DashboardError:
            return "(reference withheld)"
        if target is None:
            return label
        return label if target in allowed else "(reference withheld)"

    return LINK.sub(replace, raw)


def _supported_references(raw: str) -> None:
    remaining = LINK.sub("", raw)
    if re.search(
        r"(?m)^\s{0,3}\[[^\]\n]+\]:|\]\s*[\[(]|\[\[|\]\]|<(?:a|img)\b",
        remaining, re.I,
    ):
        raise DashboardError("dashboard_withheld")


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DashboardError("dashboard_schema_invalid")
        result[key] = value
    return result


def validate_review(raw: str, path: str) -> dict[str, Any]:
    """Validate the persisted receipt, not the model's quote-ID generation schema."""
    try:
        review = json.loads(raw, object_pairs_hook=_unique)
    except (ValueError, RecursionError):
        raise DashboardError("dashboard_schema_invalid") from None
    if not isinstance(review, dict) or set(review) != {"version", "as_of", "scope", "sources", "findings", "proposals"}:
        raise DashboardError("dashboard_schema_invalid")
    if type(review["version"]) is not int or review["version"] != 1:
        raise DashboardError("dashboard_schema_invalid")
    match = DAILY.fullmatch(path)
    if not match or _day(review["as_of"]).isoformat() != match[1]:
        raise DashboardError("dashboard_schema_invalid")
    scope = review["scope"]
    if (
        not isinstance(scope, dict) or set(scope) != {"question", "coverage", "personal_knowledge", "limitations"}
        or scope["coverage"] != "bounded" or scope["personal_knowledge"] != "not_assessed"
        or not isinstance(scope["limitations"], list) or len(scope["limitations"]) > 16
    ):
        raise DashboardError("dashboard_schema_invalid")
    _text(scope["question"])
    for text in scope["limitations"]:
        _text(text)
    if not isinstance(review["sources"], list) or not 1 <= len(review["sources"]) <= MAX_SOURCES:
        raise DashboardError("dashboard_schema_invalid")
    sources: dict[str, Any] = {}
    paths: set[str] = set()
    for item in review["sources"]:
        if (
            not isinstance(item, dict) or set(item) != {"id", "path", "sha256"}
            or not isinstance(item["id"], str) or not re.fullmatch(r"S[1-9]\d?", item["id"])
            or item["id"] in sources or not isinstance(item["path"], str) or item["path"] in paths
            or _kind(item["path"]) not in {"wiki", "note", "idea", "project", "research"}
            or not isinstance(item["sha256"], str) or not HEX64.fullmatch(item["sha256"])
        ):
            raise DashboardError("dashboard_schema_invalid")
        sources[item["id"]] = item
        paths.add(item["path"])
    if not isinstance(review["findings"], list) or not 0 <= len(review["findings"]) <= MAX_FINDINGS:
        raise DashboardError("dashboard_schema_invalid")
    findings = set()
    for row in review["findings"]:
        if not isinstance(row, dict) or not {"id", "kind", "basis", "statement", "evidence"} <= row.keys():
            raise DashboardError("dashboard_schema_invalid")
        if (
            not row.keys() <= {"id", "kind", "basis", "statement", "evidence", "relationship"}
            or not isinstance(row["id"], str) or not re.fullmatch(r"F[1-3]", row["id"]) or row["id"] in findings
            or not isinstance(row["kind"], str) or row["kind"] not in KINDS
            or not isinstance(row["basis"], str) or row["basis"] not in BASIS_LABELS
            or not isinstance(row["evidence"], list) or not 1 <= len(row["evidence"]) <= 3
        ):
            raise DashboardError("dashboard_schema_invalid")
        findings.add(row["id"])
        _text(row["statement"], 700)
        for evidence in row["evidence"]:
            if (
                not isinstance(evidence, dict) or set(evidence) != {"source", "quote"}
                or not isinstance(evidence["source"], str) or evidence["source"] not in sources
            ):
                raise DashboardError("dashboard_schema_invalid")
            _text(evidence["quote"], 500)
        if row["kind"] == "connection":
            if row.get("relationship") not in RELATIONSHIPS or len({item["source"] for item in row["evidence"]}) < 2:
                raise DashboardError("dashboard_schema_invalid")
        elif "relationship" in row:
            raise DashboardError("dashboard_schema_invalid")
    if not isinstance(review["proposals"], list) or len(review["proposals"]) != len(findings):
        raise DashboardError("dashboard_schema_invalid")
    proposed: set[str] = set()
    identifiers: set[str] = set()
    for row in review["proposals"]:
        if (
            not isinstance(row, dict) or set(row) != {"id", "finding", "action", "status", "next_step"}
            or not isinstance(row["id"], str) or not re.fullmatch(r"P[1-3]", row["id"]) or row["id"] in identifiers
            or not isinstance(row["finding"], str) or row["finding"] not in findings or row["finding"] in proposed
            or not isinstance(row["action"], str) or row["action"] not in ACTIONS or row["status"] != "proposed"
        ):
            raise DashboardError("dashboard_schema_invalid")
        _text(row["next_step"], 700)
        proposed.add(row["finding"])
        identifiers.add(row["id"])
    return review


class DisplaySnapshot:
    def __init__(self, repository: TaskRepository) -> None:
        self.repository = repository
        self.head, self.entries = repository.snapshot()
        self.raw_cache: dict[str, str] = {}
        self.bytes = 0

    def raw(self, path: str) -> str:
        if path in self.raw_cache:
            return self.raw_cache[path]
        entry = self.entries.get(path)
        if entry is None:
            raise DashboardError("dashboard_source_unavailable")
        size = entry.get("size")
        if type(size) is not int or not 0 <= size <= FILE_LIMIT:
            raise DashboardError("dashboard_source_bounded")
        if len(self.raw_cache) >= READ_LIMIT or self.bytes + size > BYTE_LIMIT:
            raise DashboardError("dashboard_read_limit")
        raw = self.repository._raw(path, self.head, entry)
        self.bytes += len(raw.encode("utf-8"))
        self.raw_cache[path] = raw
        return raw

    def source(self, path: str, *, evidence: bool = False) -> dict[str, Any]:
        kind = _kind(path)
        if kind not in {"wiki", "note", "idea", "project", "research"}:
            raise DashboardError("dashboard_withheld")
        raw = self.raw(path)
        _private(raw, generated=kind == "research" and not evidence)
        if evidence or kind != "research":
            if not _review_source_allowed(path, raw, kind, allow_captured_sources=not evidence):
                raise DashboardError("dashboard_withheld")
        # Filter the complete document before title extraction or excerpt limits:
        # neither a short title nor a cut link may retain a withheld identity.
        material = _material(path, _display_text(raw, path, set()), kind, set())
        if material is None:
            raise DashboardError("dashboard_withheld")
        title, text = material
        _text(title, 160)
        if _SENSITIVE_PATH.search(title):
            raise DashboardError("dashboard_withheld")
        dates = {}
        for key, value in _metadata(raw)[1]:
            if key in {"captured", "created", "date", "as_of"}:
                day = _day(value).isoformat()
                if key in dates and dates[key] != day:
                    raise DashboardError("dashboard_schema_invalid")
                dates[key] = day
        return {
            "id": source_id(path), "path": path, "revision": self.entries[path]["sha"],
            "canonical_revision": self.head, "kind": kind, "title": title,
            "text": text[:TEXT_LIMIT],
            "bounded": len(text) > TEXT_LIMIT, "source_dates": dates,
            "digest": fingerprint(raw), "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            "url": f"https://github.com/{self.repository.repo}/blob/{self.head}/{path}",
        }

    def daily(self, path: str) -> dict[str, Any]:
        raw = self.raw(path)
        review = validate_review(raw, path)
        # Every field, including unpublished proposal text, must pass the same policy.
        _text(raw, FILE_LIMIT)
        sources = {}
        for row in review["sources"]:
            source = self.source(row["path"], evidence=True)
            if source["sha256"] != row["sha256"]:
                raise DashboardError("dashboard_source_changed")
            sources[row["id"]] = source
        findings = []
        proposals = {row["finding"]: row for row in review["proposals"]}
        for row in review["findings"]:
            evidence = []
            for item in row["evidence"]:
                source = sources[item["source"]]
                original = self.raw(source["path"]).replace("\r\n", "\n").replace("\r", "\n")
                if item["quote"] not in original:
                    raise DashboardError("dashboard_source_changed")
                # An exact quote containing an unvalidated reference is not safe to display.
                if LINK.search(item["quote"]):
                    raise DashboardError("dashboard_withheld")
                evidence.append({
                    **{key: source[key] for key in ("id", "title", "path", "revision", "source_dates")},
                    "quote": item["quote"],
                })
            findings.append({
                "id": row["id"], "kind": row["kind"], "basis": row["basis"],
                "statement": _display_text(row["statement"], path, set()),
                "relationship": row.get("relationship"), "evidence": evidence,
                "next_step": _display_text(proposals[row["id"]]["next_step"], path, set()),
            })
        return {
            "id": source_id(path), "path": path, "revision": self.entries[path]["sha"],
            "canonical_revision": self.head, "kind": "daily_review",
            "title": "Daily knowledge review", "as_of": review["as_of"], "findings": findings,
            "coverage": "bounded", "status": "canonical", "digest": fingerprint(raw),
            "url": f"https://github.com/{self.repository.repo}/blob/{self.head}/{path}",
            "_review": review, "_packet": [
                {"id": key, **{name: source[name] for name in ("path", "revision", "title", "url")}}
                for key, source in sources.items()
            ],
        }

    def digest(self, path: str) -> dict[str, Any]:
        raw = self.raw(path)
        _private(raw, generated=True)
        _supported_references(raw)
        header = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", raw, re.S)
        if not header:
            raise DashboardError("dashboard_schema_invalid")
        pairs = _metadata(raw)[1]
        metadata = _unique([(key, value) for key, value in pairs if key in {
            "type", "period", "generated", "generated_by", "status",
        }])
        if metadata.get("type") != "digest" or metadata.get("generated_by") != "vault-digest" or metadata.get("status") != "draft":
            raise DashboardError("dashboard_schema_invalid")
        period = re.fullmatch(r"(\d{4}-\d{2}-\d{2}) \u2026 (\d{4}-\d{2}-\d{2})", metadata.get("period", ""))
        if not period:
            raise DashboardError("dashboard_schema_invalid")
        start, end = _day(period[1]), _day(period[2])
        match = DIGEST.fullmatch(path)
        if (
            not match or start > end or (end - start).days > 7
            or _day(metadata.get("generated")) != end
            or (int(match[1]), int(match[2])) != end.isocalendar()[:2]
            or "## This week" not in raw or "<!-- digest-details -->" not in raw
        ):
            raise DashboardError("dashboard_schema_invalid")
        evidence = {}
        for link in LINK.finditer(raw):
            target, _ = _link_target(link, path)
            if target and target not in evidence:
                evidence[target] = self.source(target)
        body = raw[header.end():]
        body = re.split(r"(?m)^### (?:Coverage|Technical activity)\s*$", body)[0]
        body = re.sub(r"(?m)^\d+ more checklist items?.*\n?", "", body)
        body = re.sub(r"<!--.*?-->", "", body, flags=re.S).strip()
        body = _display_text(body, path, set(evidence))
        return {
            "id": source_id(path), "path": path, "revision": self.entries[path]["sha"],
            "canonical_revision": self.head, "kind": "weekly_digest", "title": "Weekly digest",
            "as_of": end.isoformat(), "period_start": start.isoformat(), "status": "canonical_draft",
            "text": body[:TEXT_LIMIT], "bounded": len(body) > TEXT_LIMIT,
            "evidence": [
                {key: source[key] for key in ("id", "title", "path", "revision", "source_dates")}
                for source in evidence.values()
            ],
            "coverage": "changed_notes_only", "digest": fingerprint(raw),
            "evidence_status": "current_references_not_historical_verification",
        }

    def read(self, path: str) -> dict[str, Any]:
        kind = display_kind(path)
        if kind is None:
            raise DashboardError("dashboard_withheld")
        return self.daily(path) if kind == "daily_review" else self.digest(path) if kind == "weekly_digest" else self.source(path)

    def focus(self, today: date) -> dict[str, Any]:
        if "home.md" not in self.entries:
            return {"status": "unavailable", "items": [], "draft_present": False}
        raw = self.raw("home.md")
        body, pairs = _metadata(raw)
        if _excluded_metadata(pairs) or any(
            key in {"ignored", "work", "private", "sensitive", "generated", "derived"} and value.lower() not in {"", "false", "no", "0", "none"}
            for key, value in pairs
        ):
            return {"status": "withheld", "items": [], "draft_present": False}
        draft = bool(re.search(r"(?is)#+[^\n]*North Star[^\n]*\n(?:(?!\n#).)*\b(?:draft|proposed|unconfirmed|not confirmed)\b", body))
        focused = _focus(_strip_generated(body))
        items = []
        heading = ""
        window: list[str] = []
        withheld = False
        for line in focused.splitlines():
            if match := _HEADING.match(line):
                heading = match[2]
                window = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", heading)
                continue
            if not re.search(r"\b(?:approved|confirmed)\b", heading, re.I) or not line.strip().startswith("|"):
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if all(re.fullmatch(r":?-+:?", cell) for cell in cells):
                continue
            if any(cell.casefold() in {"goal", "focus", "priority", "priorities"} for cell in cells):
                continue
            try:
                if _SENSITIVE_PATH.search(line) or _DRAFT.search(line) or "reference omitted" in line:
                    raise DashboardError("dashboard_withheld")
                _text(line, 2000)
                for link in LINK.finditer(line):
                    target, _ = _link_target(link, "home.md")
                    if target:
                        self.source(target)
                dates = [_day(day) for day in window]
                if len(dates) > 2 or (len(dates) == 2 and dates[0] > dates[1]):
                    raise DashboardError("dashboard_schema_invalid")
            except DashboardError as error:
                if str(error) == "dashboard_read_limit":
                    raise
                withheld = True
                continue
            status = "expired" if len(dates) == 2 and dates[-1] < today else (
                "upcoming" if dates and dates[0] > today else "approved"
            )
            items.append({
                "text": _display_text(" | ".join(cells), "home.md", set(self.raw_cache)),
                "status": status, "starts_on": window[0] if window else None,
                "ends_on": window[-1] if len(window) == 2 else None,
            })
            if len(items) == 5:
                return {"status": "partial", "items": items, "draft_present": draft}
        return {"status": "withheld" if withheld else "available", "items": items, "draft_present": draft}


class DashboardRepository(TaskRepository):
    def read_display(self, identifier: object, revision: object) -> dict[str, Any]:
        if not isinstance(identifier, str) or not HEX64.fullmatch(identifier) or not isinstance(revision, str) or not HEX40.fullmatch(revision):
            raise DashboardError("dashboard_request_invalid")
        snapshot = DisplaySnapshot(self)
        path = next((path for path in snapshot.entries if display_kind(path) and source_id(path) == identifier), None)
        if path is None:
            raise DashboardError("dashboard_source_unavailable")
        if snapshot.entries[path]["sha"] != revision:
            raise DashboardError("dashboard_source_changed")
        return snapshot.read(path)

    def capture_source(self, path: str) -> dict[str, Any] | None:
        try:
            return DisplaySnapshot(self).read(path)
        except DashboardError as error:
            if str(error) in {"dashboard_withheld", "dashboard_source_changed", "dashboard_source_unavailable", "dashboard_schema_invalid"}:
                return None
            raise SourceError("dashboard_source_unavailable") from None

    def evidence_revision(self, path: str) -> str | None:
        try:
            return DisplaySnapshot(self).source(path, evidence=True)["revision"]
        except DashboardError as error:
            if str(error) == "dashboard_source_unavailable":
                return None
            raise SourceError("source_no_longer_permitted") from None

    def inbox(self, offset: int = 0) -> dict[str, Any]:
        if type(offset) is not int or not 0 <= offset <= 5000 or offset % PAGE_SIZE:
            raise DashboardError("dashboard_request_invalid")
        snapshot = DisplaySnapshot(self)
        paths = sorted(
            (path for path in snapshot.entries if DAILY.fullmatch(path) or DIGEST.fullmatch(path)),
            key=_report_order, reverse=True,
        )
        items, issues = [], set()
        for path in paths[offset:offset + PAGE_SIZE]:
            try:
                items.append(snapshot.read(path))
            except DashboardError as error:
                issues.add(str(error))
        return {
            "items": sorted(items, key=lambda item: item["as_of"], reverse=True),
            "issues": sorted(issues), "canonical_revision": snapshot.head,
            "next_offset": offset + PAGE_SIZE if offset + PAGE_SIZE < len(paths) else None,
            "partial": bool(issues) or len(paths) > PAGE_SIZE or offset > 0,
        }

    def today(self, today: date, previous: dict[str, Any] | None, offset: int = 0) -> dict[str, Any]:
        if type(offset) is not int or not 0 <= offset <= 5000 or offset % SOURCE_PAGE_SIZE:
            raise DashboardError("dashboard_request_invalid")
        snapshot = DisplaySnapshot(self)
        old = None
        if previous:
            old = _canonical_inventory(self.client, self.base, self.headers, previous["revision"])
        paths = sorted(
            (path for path in snapshot.entries if (
                path.startswith("wiki/sources/") or _kind(path) == "research"
            ) and (old is None or old.get(path, {}).get("sha") != snapshot.entries[path]["sha"])),
            reverse=True,
        )
        items, issues = [], set()
        for path in paths[offset:offset + SOURCE_PAGE_SIZE]:
            try:
                item = snapshot.source(path)
                item["change"] = "initial" if old is None else "updated" if path in old else "new"
                items.append(item)
            except DashboardError as error:
                issues.add(str(error))
        try:
            focus = snapshot.focus(today)
        except DashboardError as error:
            focus = {"status": str(error).removeprefix("dashboard_"), "items": [], "draft_present": False}
        return {
            "focus": focus, "items": items, "issues": sorted(issues),
            "canonical_revision": snapshot.head, "first_visit": old is None,
            "since": previous["observed_at"] if previous else None,
            "next_offset": offset + SOURCE_PAGE_SIZE if offset + SOURCE_PAGE_SIZE < len(paths) else None,
            "partial": bool(issues) or offset > 0 or len(paths) > SOURCE_PAGE_SIZE,
        }
