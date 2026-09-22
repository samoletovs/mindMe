"""Bounded canonical vault evidence. Source text is data, never authority to act."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import re
import unicodedata
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from execution_budget import bounded_timeout, checkpoint

GITHUB_API = "https://api.github.com"
MAX_CONTENT_FETCHES = 16
MAX_SOURCE_CHARS = 1500
MAX_TOTAL_CHARS = 16000
MAX_TREE_ENTRIES = 5000
MAX_FILE_BYTES = 128_000
MAX_RECENT_COMMITS = 6
_SHA = re.compile(r"[a-f0-9]{40}")
_SECTIONS = {"goals", "focus", "week", "loops", "knowledge"}
_SENSITIVE_PATH = re.compile(
    r"(?:^|[-_. ])(?:private|sensitive|secret|originals?|raw|archive|archived|done|"
    r"finance|finances|financial|bank|banking|tax|taxes|salary|income|investment|"
    r"investments|mortgage|pension|insurance|medical|health|diagnosis|patient|"
    r"legal|lawyer|court|dispute|litigation|passport|identity|familyvault|aibsvault|"
    r"work|client|clients)(?:$|[-_. ])",
    re.IGNORECASE,
)
_SENSITIVE_CONTENT = re.compile(
    r"(?:-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\b(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,})\b|"
    r"\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}\b|"
    r"\b\d{6,12}:[A-Za-z0-9_-]{25,}\b|"
    r"\bBearer\s+[A-Za-z0-9._~-]{12,}|"
    r"\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|"
    r"\b(?:AccountKey|SharedAccessKey|password|api[_ -]?key|client[_ -]?secret|"
    r"access[_ -]?token|refresh[_ -]?token)[\"']?\s*[:=]\s*\S+|"
    r"[?&]sig=[A-Za-z0-9%+/=]{12,}|"
    r"\b[A-Z]{2}\d{2}(?:[ -]?[A-Z0-9]){11,30}\b|"
    r"\b\d{6}[- ]\d{5}\b|"
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b|"
    r"\b(?:salary|account balance|bank account|tax amount|passport number|"
    r"case number|medical record|diagnosis|patient name)\b|"
    r"\b(?:balance|income|tax owed|account number|national id|iban|social security number|"
    r"phone|mobile|postal address|home address|date of birth)\s*[:=]|"
    r"(?:^|[/\\\s])\.me(?:[/\\\s]|$)|"
    r"\b(?:aibsVault|familyVault)\b)",
    re.IGNORECASE | re.MULTILINE,
)
_META_KEY = re.compile(
    r"""^\s*(?:-\s+)?("(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|[A-Za-z][A-Za-z0-9_-]*)\s*:\s*(.*?)\s*$"""
)
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_DRAFT = re.compile(
    r"\b(?:draft|example|sample|historical|history|archive|previous|past|"
    r"unconfirmed|unapproved|proposed|annual|yearly)\b", re.IGNORECASE,
)
_INDEX_HEADINGS = {"index", "contents", "table of contents", "related notes", "source index"}
_FALSE = {"false", "no", "none", "null", "0", ""}
# Review publication independently enforces this policy in memex personal_metadata.
_REVIEW_SENSITIVE_CONTENT = re.compile(
    r"(?:\.me(?:[/\\]|\b)|\b(?:iban|passport|medical|diagnosis|bank account|"
    r"legal case|social security|client confidential)\b|"
    r"\b(?:gh[pousr]_|github_pat_|sk-)[A-Za-z0-9_]{12,}|"
    r"\b[A-Z]{2}\d{2}[A-Z0-9 ]{11,30}\b)", re.I,
)
_REVIEW_DERIVED = re.compile(
    r"(?:^|[-_./ ])(?:reviews?|reports?|generated|vault-evolve|briefing|recap)(?:$|[-_./ ])",
    re.I,
)
_SAFE_ROUTES = {"mindvault", "personal-non-sensitive", "non-sensitive"}
_MISSING = object()
_PRIVACY_KEYS = {
    "private", "sensitive", "confidential", "sensitivity", "classification", "visibility",
    "route", "routing", "vault", "destination", "owner", "ownership", "level",
}
_MATERIAL_KEYS = {
    "status", "next_action", "due", "deadline", "review_on", "waiting_on",
    "goal", "question", "decision",
}
_FOCUS_HEADINGS = {
    "focus", "current focus", "approved focus", "confirmed focus", "active focus",
    "current goals", "approved goals", "confirmed goals", "active goals",
    "this week s focus", "this weeks focus", "focus this week", "weekly priorities",
    "current priorities", "this week priorities",
}


class SourceError(RuntimeError):
    """A safe code only; never attach a remote response, path, or exception."""


def _headers(token: str, repo: str) -> dict[str, str]:
    configured = os.environ.get("DIG_REPO", "samoletovs/mindVault")
    if (
        not isinstance(repo, str) or repo != configured
        or not re.fullmatch(r"[A-Za-z0-9_-]+/mindVault", repo, re.IGNORECASE)
        or repo.split("/")[0].casefold().endswith("_microsoft")
        or not isinstance(token, str) or not token or any(c.isspace() for c in token)
    ):
        raise SourceError("sources_not_configured")
    # httpx logs request URLs at INFO, including private vault filenames.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _json(
    client: httpx.Client, endpoint: str, headers: dict[str, str], *,
    params: dict[str, str | int] | None = None, missing_ok: bool = False,
    limit: int = 2_000_000,
) -> Any:
    checkpoint()
    try:
        with client.stream(
            "GET", GITHUB_API + endpoint, headers=headers, params=params,
            follow_redirects=False, timeout=bounded_timeout(20.0, stages=4),
        ) as response:
            checkpoint()
            if response.status_code == 404 and missing_ok:
                return _MISSING
            if response.status_code in {401, 403}:
                raise SourceError("source_access_denied")
            if response.status_code == 429:
                raise SourceError("source_rate_limited")
            if response.status_code != 200:
                raise SourceError("source_unavailable")
            content = bytearray()
            chunks = iter(response.iter_bytes())
            while True:
                checkpoint()
                try:
                    chunk = next(chunks)
                except StopIteration:
                    break
                checkpoint()
                if len(content) + len(chunk) > limit:
                    raise SourceError("source_response_too_large")
                content.extend(chunk)
            checkpoint()
            return json.loads(content)
    except (httpx.HTTPError, ValueError, UnicodeError):
        raise SourceError("source_read_failed") from None


def _sha(value: Any) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise SourceError("source_revision_invalid")
    return value


def _kind(path: Any, *, tasks: bool = False) -> str | None:
    if (
        not isinstance(path, str) or len(path) > 240 or not path.endswith(".md")
        or any(c in path for c in "\\%?#:")
        or any(ord(c) < 32 or ord(c) == 127 for c in path)
        or path != path.strip()
    ):
        return None
    parts = path.split("/")
    if any(
        not part or part.startswith((".", "_")) or part != part.strip()
        or _SENSITIVE_PATH.search(part)
        for part in parts
    ):
        return None
    name = parts[-1].casefold()
    if name.endswith((".private.md", "-private.md", ".full.md", "-full.md")):
        return None
    if path == "home.md":
        return "goal"
    if len(parts) == 3 and parts[0] == "projects" and name == "readme.md":
        return "project"
    if (
        name in {"readme.md", "index.md", "log.md", "schema.md", "contents.md", "catalog.md"}
        or name.endswith(("-index.md", "_index.md"))
    ):
        return None
    if len(parts) >= 2 and parts[0] in {"notes", "ideas"}:
        return "note" if parts[0] == "notes" else "idea"
    if len(parts) >= 4 and parts[:3] == ["areas", "agents", "research"]:
        return "research"
    if len(parts) >= 3 and parts[:2] in [
        ["wiki", "sources"], ["wiki", "entities"], ["wiki", "insights"], ["wiki", "trends"],
    ]:
        return "wiki"
    if tasks and len(parts) == 2 and parts[0] == "tasks":
        return "task"
    return None


def _metadata_pair(line: str) -> tuple[str, str] | None:
    match = _META_KEY.match(line)
    if not match:
        return None
    key, value = match.groups()
    if key.startswith('"'):
        try:
            key = json.loads(key)
        except ValueError:
            return "private", "true"
    elif key.startswith("'"):
        key = key[1:-1].replace("''", "'")
    return key.casefold().replace("-", "_"), value.split(" #", 1)[0].strip().strip("'\"")


def _metadata(text: str) -> tuple[str, list[tuple[str, str]]]:
    blocks: list[str] = []
    safety = [
        pair for line in text.splitlines()
        if (pair := _metadata_pair(line)) and pair[0] in _PRIVACY_KEYS
    ]

    def remove(match: re.Match[str]) -> str:
        blocks.append(match.group(1))
        return ""

    text = re.sub(r"\A\ufeff?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", remove, text, flags=re.S)
    text = re.sub(r"```(?:yaml|yml|json)\s*\n(.*?)\n```", remove, text, flags=re.S | re.I)
    pairs = []
    for block in blocks:
        # Accept simple scalar/list metadata only. YAML flow mappings, explicit
        # keys and alias/merge semantics must not conceal a privacy declaration.
        if re.search(r"[{}]|^\s*(?:\?|<<)\s*:|(?:^|\s)[&*][A-Za-z_]", block, re.M):
            pairs.append(("private", "true"))
        for line in block.splitlines():
            if pair := _metadata_pair(line):
                pairs.append(pair)
    return re.sub(r"<details\b[^>]*>.*?</details>", "", text, flags=re.S | re.I), pairs + safety


def _excluded_metadata(pairs: list[tuple[str, str]]) -> bool:
    for key, raw in pairs:
        value = raw.casefold().strip()
        if key in {"private", "sensitive", "confidential"} and value not in {"false", "no", "0"}:
            return True
        if key in {"sensitivity", "classification", "visibility", "level"} and value not in {
            *_FALSE, "public", "non-sensitive", "personal-non-sensitive", "internal",
        }:
            return True
        if key in {"route", "routing", "vault", "destination", "owner", "ownership"}:
            if value not in _SAFE_ROUTES:
                return True
        if key in {"type", "category", "domain"} and _SENSITIVE_PATH.search(value):
            return True
    return False


def _captured_source_provenance(path: str, raw: str, pairs: list[tuple[str, str]]) -> bool:
    """Allow subject words such as 'report' only for host-marked external captures."""
    if (
        len(path.split("/")) != 3 or not path.startswith("wiki/sources/")
        or [value.casefold() for key, value in pairs if key == "type"] != ["source"]
        or len(re.findall(r"(?m)^Source ID: [a-f0-9]{64}[ \t]*\r?$", raw)) != 1
    ):
        return False
    urls = [value for key, value in pairs if key == "source"]
    if len(urls) != 1 or len(urls[0]) > 2048 or any(ord(char) < 32 or ord(char) == 127 for char in urls[0]):
        return False
    try:
        url = urlsplit(urls[0])
        return (
            url.scheme in {"http", "https"} and bool(url.hostname)
            and url.username is None and url.password is None
        )
    except ValueError:
        return False


def _review_source_allowed(
    path: str, raw: str, kind: str, *, allow_captured_sources: bool = False,
) -> bool:
    if len(raw.encode("utf-8")) > 64_000 or len(path.split("/")) > 6:
        return False
    if any(char in path for char in '<>"[]') or any(
        part.casefold().removesuffix(".md") in {"aibsvault", "kongsberg", "carlsberg", "scania"}
        for part in path.split("/")
    ):
        return False
    if _REVIEW_SENSITIVE_CONTENT.search(raw):
        return False
    _, pairs = _metadata(raw)
    if _REVIEW_DERIVED.search(path) and not (
        allow_captured_sources and _captured_source_provenance(path, raw, pairs)
    ):
        return False
    for key, value in pairs:
        value = value.casefold()
        if key in {"private", "sensitive", "ignored", "work"} and value not in {"", "false", "no", "0"}:
            return False
        if key in {"route", "routing", "routed_to", "scope", "visibility"} and value not in {
            "", "personal", "mindme", "mindvault",
        }:
            return False
        if key in {"sensitivity", "classification"} and value not in {
            "", "public", "personal", "non-sensitive", "none",
        }:
            return False
        if key in {"type", "kind", "role"} and value in {"home", "task", "journal"}:
            return False
        if key in {"generated", "derived"} and value not in {"", "false", "no", "0", "none"}:
            return False
        if key in {"type", "kind", "role", "origin"} and _REVIEW_DERIVED.search(value):
            if not (
                kind == "research" and key in {"type", "kind", "role"}
                and value in {"report", "dig-report", "research-report"}
            ):
                return False
    if kind == "project":
        statuses = [value.casefold() for key, value in pairs if key == "status"]
        if not statuses or any(value != "active" for value in statuses):
            return False
    return True


def _strip_generated(text: str) -> str:
    kept = []
    generated = False
    index_depth = 0
    for line in text.splitlines():
        if "<!--" in line:
            marker = re.sub(r"[_:-]", " ", line.casefold())
            if re.search(r"\b(?:end|stop)\b", marker):
                generated = False
                continue
            if (
                re.search(r"task\s*board|generated|auto\s*board", marker)
                and re.search(r"\b(?:begin|start)\b", marker)
            ):
                generated = True
                continue
        if generated:
            continue
        if match := _HEADING.match(line):
            depth = len(match.group(1))
            if index_depth and depth <= index_depth:
                index_depth = 0
            if match.group(2).casefold() in _INDEX_HEADINGS:
                index_depth = depth
        if index_depth:
            continue
        if re.match(
            r"^\s*(?:>\s*)?(?:\*\*)?(?:generated(?: at| on)?|last updated|updated|"
            r"auto[- ]generated|rendered at)\s*[:=]", line, re.I,
        ):
            continue
        kept.append(line)
    return re.sub(r"<!--.*?-->", "", "\n".join(kept), flags=re.S)


def _focus(text: str) -> str:
    result: list[str] = []
    stack: list[tuple[int, bool]] = []
    table: list[str] = []
    permitted = False
    heading = ""

    def finish_table() -> None:
        if len(table) >= 3:
            result.extend([heading, *table])
        table.clear()

    for line in text.splitlines():
        if match := _HEADING.match(line):
            finish_table()
            depth, title = len(match.group(1)), match.group(2)
            while stack and stack[-1][0] >= depth:
                stack.pop()
            blocked = bool(_DRAFT.search(title) or _SENSITIVE_PATH.search(title)) or any(item[1] for item in stack)
            dated_title = re.sub(
                r"\b\d{4}-\d{2}-\d{2}(?:\s*(?:to|through|[-\u2013\u2014])\s*\d{4}-\d{2}-\d{2})?\b",
                "", title, flags=re.I,
            )
            clean = " ".join(re.sub(r"[^a-z ]", " ", dated_title.casefold()).split())
            permitted = not blocked and clean in _FOCUS_HEADINGS
            stack.append((depth, blocked))
            heading = line
            continue
        if not permitted or "|" not in line:
            finish_table()
            continue
        if _DRAFT.search(line) or _SENSITIVE_CONTENT.search(line):
            continue
        links = re.findall(r"\]\(([^)]+)\)", line)
        if any(_SENSITIVE_PATH.search(part) for link in links for part in link.split("/")):
            line = re.sub(
                r"\[([^\]]+)\]\(([^)]+)\)",
                lambda match: "(private reference omitted)" if any(
                    _SENSITIVE_PATH.search(part) for part in match.group(2).split("/")
                ) else match.group(0),
                line,
            )
        if not table:
            if re.search(r"\b(?:goal|focus|priority|priorities|next action|next step)\b", line, re.I):
                table.append(line)
        elif len(table) == 1:
            if re.fullmatch(r"\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)+\|?\s*", line):
                table.append(line)
            else:
                table.clear()
        else:
            table.append(line)
    finish_table()
    return "\n".join(result)


def _normalize(text: str) -> str:
    lines = [
        re.sub(r"[ \t]+", " ", line).strip()
        for line in unicodedata.normalize("NFC", text).replace("\r\n", "\n").splitlines()
    ]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _material(
    path: str, raw: str, kind: str, focused_projects: set[str],
) -> tuple[str, str] | None:
    body, pairs = _metadata(raw)
    if _excluded_metadata(pairs):
        return None
    body = _strip_generated(body)
    if kind == "goal":
        body = _focus(body)
        title = "Approved current focus"
    else:
        if _SENSITIVE_CONTENT.search(raw):
            return None
        if kind == "project":
            statuses = [value.casefold() for key, value in pairs if key == "status"]
            if statuses and any(value not in {"active", "in-progress", "in_progress", "ongoing"} for value in statuses):
                return None
            if not statuses and path not in focused_projects:
                return None
        title = next(
            (match.group(2) for line in body.splitlines() if (match := _HEADING.match(line))),
            path.rsplit("/", 1)[-1][:-3].replace("-", " "),
        )
        attributes = [f"{key}: {value}" for key, value in sorted(set(pairs)) if key in _MATERIAL_KEYS]
        body = "\n".join([body, *attributes])
    normalized = _normalize(body)
    if not normalized:
        return None
    return _normalize(title)[:160], normalized


def _content(data: Any, path: str, revision: str | None = None) -> tuple[str, str]:
    if (
        not isinstance(data, dict) or data.get("type") != "file"
        or data.get("path") != path or data.get("encoding") != "base64"
        or not isinstance(data.get("content"), str)
    ):
        raise SourceError("source_content_invalid")
    actual = _sha(data.get("sha"))
    if revision is not None and actual != revision:
        raise SourceError("source_snapshot_mismatch")
    try:
        decoded = base64.b64decode(re.sub(r"\s", "", data["content"]), validate=True)
        if len(decoded) > MAX_FILE_BYTES:
            raise SourceError("source_file_too_large")
        return actual, decoded.decode("utf-8")
    except (ValueError, UnicodeError, binascii.Error):
        raise SourceError("source_content_invalid") from None


def _inventory(
    client: httpx.Client, base: str, headers: dict[str, str], tree_sha: str,
) -> dict[str, dict[str, Any]]:
    tree = _json(client, base + "/git/trees/" + tree_sha, headers, params={"recursive": 1})
    if not isinstance(tree, dict) or tree.get("truncated") is not False:
        raise SourceError("source_tree_incomplete")
    entries = tree.get("tree")
    if not isinstance(entries, list) or len(entries) > MAX_TREE_ENTRIES:
        raise SourceError("source_tree_too_large")
    inventory: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise SourceError("source_tree_invalid")
        path = entry.get("path")
        if not isinstance(path, str) or not path:
            raise SourceError("source_tree_invalid")
        entry_type, mode = entry.get("type"), entry.get("mode")
        valid_modes = {"blob": {"100644", "100755", "120000"}, "tree": {"040000"}, "commit": {"160000"}}
        if (
            not isinstance(entry_type, str) or entry_type not in valid_modes
            or not isinstance(mode, str) or mode not in valid_modes[entry_type]
        ):
            raise SourceError("source_tree_invalid")
        if entry_type != "blob" or mode == "120000":
            continue
        if path in inventory:
            raise SourceError("source_tree_invalid")
        _sha(entry.get("sha"))
        inventory[path] = entry
    return inventory


def _recent_paths(
    client: httpx.Client, base: str, headers: dict[str, str], revision: str,
    inventory: dict[str, dict[str, Any]],
) -> dict[str, int]:
    # Commit-detail REST responses contain patches for disallowed files. Compare
    # bounded immutable trees instead; no content outside the allowlist is read.
    commits = _json(client, base + "/commits", headers, params={
        "sha": revision, "per_page": MAX_RECENT_COMMITS,
    })
    if (
        not isinstance(commits, list) or not commits or not isinstance(commits[0], dict)
        or commits[0].get("sha") != revision
    ):
        raise SourceError("source_history_invalid")
    ranks: dict[str, int] = {}
    newer = inventory
    for rank, entry in enumerate(commits[1:MAX_RECENT_COMMITS]):
        if not isinstance(entry, dict):
            raise SourceError("source_history_invalid")
        _sha(entry.get("sha"))
        commit = entry.get("commit")
        tree = commit.get("tree") if isinstance(commit, dict) else None
        tree_sha = _sha(tree.get("sha") if isinstance(tree, dict) else None)
        older = _inventory(client, base, headers, tree_sha)
        for path, file in newer.items():
            if path in inventory and older.get(path, {}).get("sha") != file["sha"]:
                ranks.setdefault(path, rank)
        newer = older
    if len(commits) == 1:
        ranks = {path: 0 for path in inventory}
    return ranks


def _canonical_head(client: httpx.Client, base: str, headers: dict[str, str]) -> str:
    metadata = _json(client, base, headers)
    branch = metadata.get("default_branch") if isinstance(metadata, dict) else None
    if (
        not isinstance(branch, str) or not branch or len(branch) > 200
        or not re.fullmatch(r"[A-Za-z0-9_./-]+", branch) or ".." in branch
        or any(not part or part.startswith(".") for part in branch.split("/"))
    ):
        raise SourceError("source_branch_invalid")
    head = _json(client, base + "/git/ref/heads/" + quote(branch, safe=""), headers)
    ref = head.get("object") if isinstance(head, dict) else None
    if not isinstance(ref, dict) or ref.get("type") != "commit":
        raise SourceError("source_revision_invalid")
    return _sha(ref.get("sha"))


def load_sources(
    client: httpx.Client, *, token: str, repo: str, sections: list[str],
    previous: dict[str, str] | None = None,
    known_revisions: dict[str, str] | None = None,
    scan_cursor: str | None = None,
    include_evidence: bool = False,
    query: str | None = None,
    anchor_paths: tuple[str, ...] = (),
    allow_captured_sources: bool = False,
) -> dict[str, Any]:
    """Read one canonical snapshot; the host checkpoints only displayed changes.

    ``previous`` contains semantic digests, not dates or Git revisions. Unread paths
    therefore remain potentially changed. Git history is an ordering hint, never a
    substitute for reading content, and filename dates are not modification dates.
    ``inventory_paths`` proves physical existence, not permission to read a source.
    It is unfiltered and must never be passed to synthesis, logs, or telemetry.
    """
    enabled = set(sections) & _SECTIONS
    result: dict[str, Any] = {
        "version": 1, "revision": "", "sources": [], "goals": [], "changes": [],
        "fingerprints": {}, "source_revisions": {}, "complete": True,
        "warnings": [], "source_status": "available", "initial_baseline": not bool(previous),
        "sections": list(sections), "processed_revisions": {}, "scan_cursor": scan_cursor,
        "coverage": {"candidate_files": 0, "read_files": 0, "included_notes": 0},
    }
    if not enabled:
        return result
    if known_revisions is not None and (
        not isinstance(known_revisions, dict) or len(known_revisions) > 1000
        or any(not isinstance(path, str) or not isinstance(sha, str) or not _SHA.fullmatch(sha)
               for path, sha in known_revisions.items())
    ):
        raise SourceError("source_tracking_invalid")
    if scan_cursor is not None and not isinstance(scan_cursor, str):
        raise SourceError("source_tracking_invalid")
    headers = _headers(token, repo)
    base = "/repos/" + repo
    revision = _canonical_head(client, base, headers)
    commit = _json(client, base + "/git/commits/" + revision, headers)
    if not isinstance(commit, dict) or commit.get("sha") != revision:
        raise SourceError("source_revision_invalid")
    tree_ref = commit.get("tree")
    tree_sha = _sha(tree_ref.get("sha") if isinstance(tree_ref, dict) else None)
    inventory = _inventory(client, base, headers, tree_sha)
    result["revision"] = revision
    result["inventory_paths"] = sorted(inventory)
    previous = previous or {}
    if result["initial_baseline"]:
        result["warnings"].append("Initial source baseline; these records are not changes since yesterday.")
    candidates = {
        path: entry for path, entry in inventory.items()
        if (
            (_kind(path) in {"goal", "project"} and enabled & {"goals", "focus", "week"})
            or (_kind(path) == "idea" and "loops" in enabled)
            or (_kind(path) in {"note", "research", "wiki"} and "knowledge" in enabled)
        )
    }
    result["coverage"]["candidate_files"] = len(candidates)
    result["source_revisions"] = {
        path: entry["sha"] for path, entry in inventory.items()
        if path in candidates or ("loops" in enabled and _kind(path, tasks=True) == "task")
    }
    recent: dict[str, int] = {}
    if len(candidates) > MAX_CONTENT_FETCHES:
        try:
            recent = _recent_paths(client, base, headers, revision, inventory)
        except SourceError:
            result["complete"] = False
            result["warnings"].append("Recent source ordering is unavailable; no filename is treated as a modification date.")
    focused_projects: set[str] = set()
    total = fetches = 0
    evidence_bytes = 0

    def priority(candidate: str) -> tuple[int, int, str]:
        if query is not None:
            terms = set(re.findall(r"[a-z0-9]{3,}", query.casefold()))
            overlap = len(terms & set(re.findall(r"[a-z0-9]{3,}", candidate.casefold())))
            return (0 if candidate in anchor_paths else 1, -overlap, candidate)
        if known_revisions is not None:
            changed = known_revisions.get(candidate) != candidates[candidate]["sha"]
            rank = (
                0 if candidate == "home.md" else
                1 if changed and candidate in recent else
                2 if changed and candidate in focused_projects else
                3 if changed else
                4 if candidate in focused_projects else 5
            )
            rotation = int(bool(scan_cursor) and candidate <= scan_cursor) if rank >= 3 else recent.get(candidate, 0)
            return rank, rotation, candidate
        return (
            0 if candidate == "home.md" else
            1 if candidate in recent else
            2 if candidate in focused_projects else
            3 if candidate in previous else
            4 if _kind(candidate) == "project" else 5,
            recent.get(candidate, MAX_RECENT_COMMITS), candidate,
        )

    while candidates:
        path = min(candidates, key=priority)
        entry = candidates.pop(path)
        if fetches >= MAX_CONTENT_FETCHES or total >= MAX_TOTAL_CHARS:
            result["complete"] = False
            result["warnings"].append("Source context is bounded; some eligible records remain unprocessed and may have changed.")
            break
        size = entry.get("size")
        if not isinstance(size, int) or size < 0 or size > MAX_FILE_BYTES:
            result["complete"] = False
            result["warnings"].append("A source exceeds the bounded reader or lacks size information.")
            continue
        if include_evidence and path != "home.md" and (
            len(path.split("/")) > 6 or size > 64_000 or evidence_bytes + size > 512_000
        ):
            result["complete"] = False
            result["warnings"].append("Some source files exceed the review writer's byte or path-depth limits.")
            continue
        fetches += 1
        data = _json(
            client, base + "/contents/" + quote(path, safe="/"), headers,
            params={"ref": revision}, limit=MAX_FILE_BYTES * 2 + 8000,
        )
        _, raw = _content(data, path, entry["sha"])
        result["processed_revisions"][path] = entry["sha"]
        if path != "home.md":
            result["scan_cursor"] = path
        kind = _kind(path)
        assert kind is not None
        if include_evidence and kind != "goal" and not _review_source_allowed(
            path, raw, kind, allow_captured_sources=allow_captured_sources,
        ):
            result["source_revisions"].pop(path, None)
            result["warnings"].append("Some sources were excluded by the review publication policy.")
            continue
        material = _material(path, raw, kind, focused_projects)
        if material is None:
            result["source_revisions"].pop(path, None)
            continue
        title, text = material
        if kind == "goal":
            focused_projects.update(
                link for link in re.findall(r"\]\((projects/[^)#]+/README\.md)(?:#[^)]*)?\)", text)
                if _kind(link) == "project"
            )
        excerpt = text[:MAX_SOURCE_CHARS]
        if total + len(excerpt) > MAX_TOTAL_CHARS:
            result["complete"] = False
            result["warnings"].append("Source context is bounded; some eligible records remain unprocessed and may have changed.")
            continue
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        source = {
            "path": path, "revision": entry["sha"], "digest": digest,
            "title": title, "text": excerpt, "kind": kind,
            "url": f"https://github.com/{repo}/blob/{revision}/{quote(path, safe='/')}",
        }
        if include_evidence and kind != "goal":
            # Preserve literal source bytes; normalized briefing prose is not a quotation.
            source["sha256"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            source["byte_size"] = size
            evidence_bytes += size
            original = raw.replace("\r\n", "\n").replace("\r", "\n")
            source["evidence_text"] = "\n".join(
                line for line in _strip_generated(_metadata(raw)[0]).splitlines()
                if line and line in original
            )[:MAX_SOURCE_CHARS]
        result["sources"].append(source)
        result["fingerprints"][path] = digest
        if kind == "goal":
            result["goals"].append(source)
        if not result["initial_baseline"] and previous.get(path) != digest:
            result["changes"].append(source)
        if len(text) > len(excerpt):
            result["warnings"].append("Some source excerpts are shortened; source links retain the full permitted context.")
        total += len(excerpt)
    result["warnings"] = list(dict.fromkeys(result["warnings"]))
    result["coverage"].update(read_files=fetches, included_notes=len(result["sources"]))
    return result


def read_source_revision(
    client: httpx.Client, *, token: str, repo: str, path: str, include_evidence: bool = False,
    allow_captured_sources: bool = False,
) -> str | None:
    """Compare immediately before acting. Only a real content 404 means removal."""
    headers = _headers(token, repo)
    if _kind(path, tasks=True) is None:
        raise SourceError("source_path_not_allowed")
    data = _json(
        client, f"/repos/{repo}/contents/{quote(path, safe='/')}", headers,
        missing_ok=True, limit=MAX_FILE_BYTES * 2 + 8000,
    )
    if data is _MISSING:
        return None
    revision, raw = _content(data, path)
    _, pairs = _metadata(raw)
    if _excluded_metadata(pairs) or (_kind(path) != "goal" and _SENSITIVE_CONTENT.search(raw)):
        raise SourceError("source_no_longer_permitted")
    if include_evidence and (
        _kind(path) not in {"note", "wiki", "research", "idea", "project"}
        or not _review_source_allowed(
            path, raw, _kind(path) or "", allow_captured_sources=allow_captured_sources,
        )
    ):
        raise SourceError("source_no_longer_permitted")
    return revision


def read_knowledge_source(
    client: httpx.Client, *, token: str, repo: str, path: str,
) -> dict[str, Any] | None:
    """Read the current canonical file, not a supplied excerpt or unpublished PR."""
    headers = _headers(token, repo)
    kind = _kind(path)
    if kind not in {"note", "wiki", "research", "idea", "project"}:
        raise SourceError("source_path_not_allowed")
    head = _canonical_head(client, "/repos/" + repo, headers)
    data = _json(
        client, f"/repos/{repo}/contents/{quote(path, safe='/')}", headers,
        params={"ref": head}, missing_ok=True, limit=MAX_FILE_BYTES * 2 + 8000,
    )
    if data is _MISSING:
        return None
    revision, raw = _content(data, path)
    if not _review_source_allowed(path, raw, kind, allow_captured_sources=True):
        return None
    material = _material(path, raw, kind, set())
    if material is None:
        return None
    title, text = material
    return {
        "path": path, "revision": revision, "digest": hashlib.sha256(text.encode()).hexdigest(),
        "title": title, "text": text[:10000], "kind": kind,
        "url": f"https://github.com/{repo}/blob/{head}/{quote(path, safe='/')}",
        "bounded": len(text) > 10000,
    }


def load_topic_sources(
    client: httpx.Client, *, token: str, repo: str, query: str,
    anchor_paths: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Rank paths before 16 bounded reads, then rank permitted evidence; at most five sources."""
    result = load_sources(
        client, token=token, repo=repo, sections=["knowledge", "loops"],
        include_evidence=True, query=query, anchor_paths=anchor_paths, allow_captured_sources=True,
    )
    stop = {"the", "and", "this", "that", "with", "from", "about", "what", "into", "topic"}
    terms = set(re.findall(r"[a-z0-9]{3,}", query.casefold())) - stop

    def score(source: dict[str, Any]) -> int:
        title = set(re.findall(r"[a-z0-9]{3,}", (source["title"] + " " + source["path"]).casefold()))
        body = set(re.findall(r"[a-z0-9]{3,}", source["text"].casefold()))
        return 4 * len(terms & title) + len(terms & body)

    selected = sorted(
        (item for item in result["sources"] if item["path"] in anchor_paths or score(item) > 0),
        key=lambda item: (item["path"] not in anchor_paths, -score(item), item["path"]),
    )[:5]
    result["sources"] = [
        {key: item[key] for key in ("path", "revision", "digest", "title", "text", "kind", "url")}
        for item in selected
    ]
    result["warnings"] = [
        *result["warnings"],
        "Selection is bounded: at most 16 candidate files read, five relevant sources compared. "
        "Missing evidence is not proof of absence or unfamiliarity. Sources may share an origin.",
    ]
    return result
