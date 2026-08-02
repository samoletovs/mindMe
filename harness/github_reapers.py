"""GitHub reaper poller — moves the reapers' *idle polling* off GitHub Actions.

Background
----------
The mindVault / familyVault "reaper" workflows complete finished Copilot coding-agent
PRs (mark ready → squash-merge → deliver the report / rebuild the index). They were
scheduled on GitHub Actions and billed a whole minute per poll — even though the vast
majority of polls find nothing to do. That idle polling is what drained the account's
3,000 Actions-minutes/month budget.

This module runs the *polling* on the free Azure Functions timer instead. For each
reaper it asks GitHub — with one or two cheap REST calls — whether the repo actually
has a finished agent PR (or a non-empty promotion outbox). Only when there is real
work does it fire the existing workflow via ``workflow_dispatch``. The workflow itself
is unchanged: it still runs on Actions, but only a handful of times a month (on genuine
completions) instead of ~720 idle polls.

No repo checkout, no ``gh`` / ``git``, no duplicated delivery logic — pure GitHub REST
via ``httpx`` (already a harness dependency), mirroring the house style already used by
``function_app.py`` (``GITHUB_API`` + ``DIG_GITHUB_TOKEN`` + Bearer auth).

Logging policy (mindMe AGENTS.md Rule 1): only counts, repo slugs, workflow filenames,
durations, and status codes are logged — never PR titles, bodies, or branch content
(a research PR title can quote the private question text).
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

import httpx

import vault_layout

GITHUB_API = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"
_HTTP_TIMEOUT = 20.0
_PR_PAGE_SIZE = 50
_FILE_PAGE_SIZE = 100

# Copilot coding-agent author logins vary by surface (REST vs. app install).
_AGENT_LOGINS = {"copilot-swe-agent", "app/copilot-swe-agent", "copilot"}

log = logging.getLogger("mindMe.reapers")


@dataclass(frozen=True)
class ReaperTarget:
    """One reaper workflow the timer may trigger, plus the guard that decides when.

    The guard mirrors the workflow's own ``if`` checks closely enough to avoid
    needless dispatches; the workflow re-verifies precisely before it acts, so a
    slightly loose match here only ever costs one no-op workflow run.
    """

    key: str  # short id for logs
    repo: str  # "owner/name"
    workflow: str  # workflow filename to dispatch
    ref: str  # branch to dispatch on
    kind: str  # "agent_pr" | "outbox"
    # agent_pr: every changed file must sit under one of these path prefixes
    path_prefixes: tuple[str, ...] = ()
    # agent_pr: optional required PR-title prefix (compared lower-cased), e.g. "promote:"
    title_prefix: str | None = None
    # agent_pr: optional required head-branch prefix, e.g. "copilot/dispatch"
    branch_prefix: str | None = None
    # outbox: directory that must contain >= 1 ``.md`` file
    outbox_dir: str | None = None


# The six migrated reapers. Order is irrelevant; each is checked independently.
TARGETS: tuple[ReaperTarget, ...] = (
    ReaperTarget(
        key="dig",
        repo="samoletovs/mindVault",
        workflow="dig-reaper.yml",
        ref="main",
        kind="agent_pr",
        path_prefixes=(f"{vault_layout.folder(vault_layout.MINDVAULT, 'areas')}/agents/research/",),
    ),
    ReaperTarget(
        key="promote",
        repo="samoletovs/mindVault",
        workflow="promote-reaper.yml",
        ref="main",
        kind="agent_pr",
        path_prefixes=("wiki/",),
        title_prefix="promote:",
    ),
    ReaperTarget(
        key="newsletter",
        repo="samoletovs/mindVault",
        workflow="newsletter-reaper.yml",
        ref="main",
        kind="agent_pr",
        path_prefixes=(f"{vault_layout.folder(vault_layout.MINDVAULT, 'areas')}/agents/newsletters/",),
    ),
    ReaperTarget(
        key="dispatch",
        repo="samoletovs/mindVault",
        workflow="dispatch-reaper.yml",
        ref="main",
        kind="agent_pr",
        path_prefixes=(f"{vault_layout.folder(vault_layout.MINDVAULT, 'areas')}/agents/newsletters/",),
        branch_prefix="copilot/dispatch",
    ),
    ReaperTarget(
        key="promote-forward",
        repo="samoletovs/mindVault",
        workflow="promote-forward.yml",
        ref="main",
        kind="outbox",
        outbox_dir="wiki/_promotions/outbox",
    ),
    ReaperTarget(
        key="family-promote",
        repo="samoletovs/familyVault",
        workflow="promote-reaper.yml",
        ref="main",
        kind="agent_pr",
        # familyVault keeps its knowledge under knowledge/ rather than wiki/, and a promote
        # PR also touches the root register and log. These mirror that workflow's own guard
        # (`^(knowledge/|people/|decisions/|index\.md$|log\.md$)`) - every changed path has
        # to match, so listing only part of the set strands the PR instead of narrowing it.
        path_prefixes=("knowledge/", "people/", "decisions/", "index.md", "log.md"),
        title_prefix="promote:",
    ),
)


# --- token + headers --------------------------------------------------------


def _token() -> str | None:
    """The reaper PAT. Prefers a dedicated token, falls back to the dig token."""
    return os.environ.get("REAPER_GITHUB_TOKEN") or os.environ.get("DIG_GITHUB_TOKEN")


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "User-Agent": "mindMe-reaper/1.0",
    }


# --- pure decision helpers (no I/O — unit-tested directly) -------------------


def _is_agent(login: str) -> bool:
    """True if a PR author login belongs to the Copilot coding agent."""
    low = (login or "").lower()
    return low in _AGENT_LOGINS or low.startswith("copilot")


def _is_finished_title(title: str) -> bool:
    """True unless the PR is still marked in-progress ("[WIP] …" / "WIP: …")."""
    low = (title or "").lower().strip()
    return not (low.startswith("[wip]") or low.startswith("wip:"))


def _all_under_prefixes(paths: list[str], prefixes: tuple[str, ...]) -> bool:
    """True if ``paths`` is non-empty and every path sits under one prefix."""
    if not paths:
        return False
    return all(any(p.startswith(prefix) for prefix in prefixes) for p in paths)


def _outbox_has_md(entries: list[dict]) -> bool:
    """True if a Contents-API directory listing contains >= 1 ``.md`` file."""
    return any(
        e.get("type") == "file" and (e.get("name") or "").lower().endswith(".md")
        for e in entries
    )


def _pr_matches(target: ReaperTarget, title: str, head_ref: str, paths: list[str]) -> bool:
    """True if an agent PR satisfies this target's title / branch / path guard."""
    if target.title_prefix and not (title or "").lower().startswith(target.title_prefix):
        return False
    if target.branch_prefix and not (head_ref or "").startswith(target.branch_prefix):
        return False
    if target.path_prefixes and not _all_under_prefixes(paths, target.path_prefixes):
        return False
    return True


# --- GitHub REST calls ------------------------------------------------------


def _open_agent_prs(client: httpx.Client, repo: str, token: str) -> list[tuple[int, str, str]]:
    """Open, non-WIP PRs authored by the Copilot agent as ``(number, title, head_ref)``."""
    resp = client.get(
        f"{GITHUB_API}/repos/{repo}/pulls",
        headers=_headers(token),
        params={"state": "open", "per_page": _PR_PAGE_SIZE},
    )
    resp.raise_for_status()
    prs: list[tuple[int, str, str]] = []
    for pr in resp.json():
        login = ((pr.get("user") or {}).get("login")) or ""
        if not _is_agent(login):
            continue
        title = pr.get("title") or ""
        if not _is_finished_title(title):
            continue
        head_ref = ((pr.get("head") or {}).get("ref")) or ""
        prs.append((int(pr.get("number")), title, head_ref))
    return prs


def _pr_paths(client: httpx.Client, repo: str, number: int, token: str) -> list[str]:
    """Changed file paths for a PR (first page — reaper PRs are small)."""
    resp = client.get(
        f"{GITHUB_API}/repos/{repo}/pulls/{number}/files",
        headers=_headers(token),
        params={"per_page": _FILE_PAGE_SIZE},
    )
    resp.raise_for_status()
    return [(f.get("filename") or "") for f in resp.json()]


def _outbox_nonempty(client: httpx.Client, repo: str, path: str, token: str) -> bool:
    """True if a promotion outbox directory holds >= 1 ``.md`` file (404 → empty)."""
    resp = client.get(
        f"{GITHUB_API}/repos/{repo}/contents/{path}",
        headers=_headers(token),
    )
    if resp.status_code == 404:
        return False
    resp.raise_for_status()
    data = resp.json()
    return isinstance(data, list) and _outbox_has_md(data)


def _dispatch_workflow(client: httpx.Client, target: ReaperTarget, token: str) -> None:
    """Fire ``workflow_dispatch`` for the target's workflow on its branch."""
    resp = client.post(
        f"{GITHUB_API}/repos/{target.repo}/actions/workflows/{target.workflow}/dispatches",
        headers=_headers(token),
        json={"ref": target.ref},
    )
    if resp.status_code >= 300:
        log.error(
            "reaper dispatch failed key=%s repo=%s wf=%s status=%d",
            target.key, target.repo, target.workflow, resp.status_code,
        )
        resp.raise_for_status()
    log.info("reaper dispatched key=%s repo=%s wf=%s", target.key, target.repo, target.workflow)


# --- orchestration ----------------------------------------------------------


def _target_has_candidate(
    client: httpx.Client,
    target: ReaperTarget,
    token: str,
    pr_cache: dict[str, list[tuple[int, str, str]]],
    paths_cache: dict[tuple[str, int], list[str]],
) -> bool:
    """Cheap check: does this target have real work waiting? Caches per-repo PR
    lists and per-PR file lists so the shared mindVault targets reuse one fetch."""
    if target.kind == "outbox":
        return _outbox_nonempty(client, target.repo, target.outbox_dir or "", token)

    prs = pr_cache.get(target.repo)
    if prs is None:
        prs = _open_agent_prs(client, target.repo, token)
        pr_cache[target.repo] = prs

    for number, title, head_ref in prs:
        # Cheap title/branch skips before fetching the PR's files.
        if target.title_prefix and not title.lower().startswith(target.title_prefix):
            continue
        if target.branch_prefix and not head_ref.startswith(target.branch_prefix):
            continue
        cache_key = (target.repo, number)
        paths = paths_cache.get(cache_key)
        if paths is None:
            paths = _pr_paths(client, target.repo, number, token)
            paths_cache[cache_key] = paths
        if _pr_matches(target, title, head_ref, paths):
            return True
    return False


def run_reaper_poll(client: httpx.Client | None = None) -> dict[str, object]:
    """Poll every reaper target; dispatch the workflow for each that has real work.

    Returns a non-sensitive summary ``{"checked", "dispatched", "errors", "results"}``
    where ``results`` maps each target key to ``"dispatched" | "idle" | "error"``.
    Safe to call on a timer: idempotent (a dispatched workflow that finds the PR
    already merged simply exits) and never raises — per-target failures are isolated.
    """
    token = _token()
    if not token:
        log.warning("reaper poll skipped: no REAPER_GITHUB_TOKEN / DIG_GITHUB_TOKEN")
        return {"checked": 0, "dispatched": 0, "errors": 0, "results": {}, "skipped": "no_token"}

    own_client = client is None
    client = client or httpx.Client(timeout=_HTTP_TIMEOUT)
    pr_cache: dict[str, list[tuple[int, str, str]]] = {}
    paths_cache: dict[tuple[str, int], list[str]] = {}
    results: dict[str, str] = {}
    try:
        for target in TARGETS:
            try:
                if _target_has_candidate(client, target, token, pr_cache, paths_cache):
                    _dispatch_workflow(client, target, token)
                    results[target.key] = "dispatched"
                else:
                    results[target.key] = "idle"
            except Exception:
                # Isolate failures: one unreachable repo must not block the rest.
                log.exception("reaper target failed key=%s repo=%s", target.key, target.repo)
                results[target.key] = "error"
    finally:
        if own_client:
            client.close()

    dispatched = sum(1 for v in results.values() if v == "dispatched")
    errors = sum(1 for v in results.values() if v == "error")
    return {
        "checked": len(TARGETS),
        "dispatched": dispatched,
        "errors": errors,
        "results": results,
    }
