"""Unit tests for github_reapers — the Azure-timer poller that replaces the
reapers' idle GitHub Actions polling.

All GitHub REST access is mocked (FakeClient); no network, no real PAT, per the
testing convention "mock external dependencies — never call real API services".
"""

from __future__ import annotations

import github_reapers as gr


# --- test doubles -----------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code: int = 200, json_data: object = None) -> None:
        self.status_code = status_code
        self._json = [] if json_data is None else json_data

    def json(self) -> object:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _parse_repo(url: str) -> tuple[str, list[str]]:
    """Split a GitHub API URL into ('owner/name', [remaining path segments])."""
    tail = url.split("/repos/", 1)[1]
    parts = tail.split("/")
    return f"{parts[0]}/{parts[1]}", parts[2:]


class FakeClient:
    """Routes GitHub REST calls to canned fixtures and records dispatches."""

    def __init__(
        self,
        prs: dict[str, list[dict]] | None = None,
        files: dict[tuple[str, int], list[dict]] | None = None,
        contents: dict[tuple[str, str], list[dict]] | None = None,
        fail_repos: set[str] | None = None,
    ) -> None:
        self.prs = prs or {}
        self.files = files or {}
        self.contents = contents or {}
        self.fail_repos = fail_repos or set()
        self.dispatched: list[tuple[str, str]] = []

    def get(self, url: str, headers=None, params=None) -> FakeResponse:
        repo, rest = _parse_repo(url)
        if repo in self.fail_repos:
            return FakeResponse(500)
        if rest == ["pulls"]:
            return FakeResponse(200, self.prs.get(repo, []))
        if len(rest) >= 3 and rest[0] == "pulls" and rest[2] == "files":
            return FakeResponse(200, self.files.get((repo, int(rest[1])), []))
        if rest and rest[0] == "contents":
            path = "/".join(rest[1:])
            data = self.contents.get((repo, path))
            return FakeResponse(404) if data is None else FakeResponse(200, data)
        return FakeResponse(404)

    def post(self, url: str, headers=None, json=None) -> FakeResponse:
        repo, rest = _parse_repo(url)
        # rest == ["actions", "workflows", "<file>.yml", "dispatches"]
        self.dispatched.append((repo, rest[2]))
        return FakeResponse(204)


def _agent_pr(number: int, title: str, ref: str = "copilot/x") -> dict:
    return {"number": number, "title": title, "user": {"login": "copilot-swe-agent"}, "head": {"ref": ref}}


# --- pure helpers -----------------------------------------------------------


def test_is_agent_recognizes_copilot_logins():
    assert gr._is_agent("copilot-swe-agent")
    assert gr._is_agent("app/copilot-swe-agent")
    assert gr._is_agent("Copilot")


def test_is_agent_rejects_human_login():
    assert not gr._is_agent("samoletovs")


def test_is_finished_title_rejects_wip():
    assert not gr._is_finished_title("[WIP] dig: something")
    assert not gr._is_finished_title("WIP: promote foo")


def test_is_finished_title_accepts_normal_title():
    assert gr._is_finished_title("dig: how do heat pumps scale")


def test_all_under_prefixes_true_when_every_path_matches():
    paths = ["02_areas/agents/research/a.md", "02_areas/agents/research/index.md"]
    assert gr._all_under_prefixes(paths, ("02_areas/agents/research/",))


def test_all_under_prefixes_false_when_a_path_escapes():
    paths = ["wiki/a.md", "scripts/evil.sh"]
    assert not gr._all_under_prefixes(paths, ("wiki/",))


def test_all_under_prefixes_false_on_empty_diff():
    assert not gr._all_under_prefixes([], ("wiki/",))


def test_outbox_has_md_detects_markdown_file():
    entries = [{"type": "file", "name": "theme-x.md"}]
    assert gr._outbox_has_md(entries)


def test_outbox_has_md_ignores_non_markdown():
    entries = [{"type": "file", "name": ".gitkeep"}, {"type": "dir", "name": "sub"}]
    assert not gr._outbox_has_md(entries)


def test_pr_matches_requires_title_prefix():
    target = gr.ReaperTarget("promote", "r", "w.yml", "main", "agent_pr",
                             path_prefixes=("wiki/",), title_prefix="promote:")
    assert gr._pr_matches(target, "promote: foo", "copilot/x", ["wiki/a.md"])
    assert not gr._pr_matches(target, "dig: foo", "copilot/x", ["wiki/a.md"])


def test_pr_matches_requires_branch_prefix():
    target = gr.ReaperTarget("dispatch", "r", "w.yml", "main", "agent_pr",
                             path_prefixes=("02_areas/agents/newsletters/",),
                             branch_prefix="copilot/dispatch")
    files = ["02_areas/agents/newsletters/x.md"]
    assert gr._pr_matches(target, "digest", "copilot/dispatch-abc", files)
    assert not gr._pr_matches(target, "digest", "copilot/other", files)


# --- run_reaper_poll --------------------------------------------------------


def test_poll_dispatches_dig_when_finished_research_pr_present(monkeypatch):
    # Arrange
    monkeypatch.setenv("REAPER_GITHUB_TOKEN", "tok")
    client = FakeClient(
        prs={"samoletovs/mindVault": [_agent_pr(1, "dig: heat pumps")]},
            files={("samoletovs/mindVault", 1): [{"filename": "areas/agents/research/2026-07-21-x.md"}]},
    )
    # Act
    summary = gr.run_reaper_poll(client=client)
    # Assert — only the dig reaper fires; the other mindVault reapers stay idle.
    assert ("samoletovs/mindVault", "dig-reaper.yml") in client.dispatched
    assert summary["results"]["dig"] == "dispatched"
    assert summary["results"]["newsletter"] == "idle"
    assert summary["dispatched"] == 1


def test_poll_dispatches_promote_forward_when_outbox_nonempty(monkeypatch):
    # Arrange
    monkeypatch.setenv("REAPER_GITHUB_TOKEN", "tok")
    client = FakeClient(
        contents={("samoletovs/mindVault", "wiki/_promotions/outbox"): [{"type": "file", "name": "x.md"}]},
    )
    # Act
    summary = gr.run_reaper_poll(client=client)
    # Assert
    assert ("samoletovs/mindVault", "promote-forward.yml") in client.dispatched
    assert summary["results"]["promote-forward"] == "dispatched"


def test_poll_is_idle_when_nothing_pending(monkeypatch):
    # Arrange
    monkeypatch.setenv("REAPER_GITHUB_TOKEN", "tok")
    client = FakeClient()
    # Act
    summary = gr.run_reaper_poll(client=client)
    # Assert
    assert client.dispatched == []
    assert summary["dispatched"] == 0
    assert all(state == "idle" for state in summary["results"].values())


def test_poll_isolates_a_failing_repo(monkeypatch):
    # Arrange — familyVault is unreachable; mindVault targets must still resolve.
    monkeypatch.setenv("REAPER_GITHUB_TOKEN", "tok")
    client = FakeClient(fail_repos={"samoletovs/familyVault"})
    # Act
    summary = gr.run_reaper_poll(client=client)
    # Assert
    assert summary["results"]["family-promote"] == "error"
    assert summary["results"]["dig"] == "idle"
    assert summary["errors"] == 1


def test_poll_skips_cleanly_without_a_token(monkeypatch):
    # Arrange
    monkeypatch.delenv("REAPER_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("DIG_GITHUB_TOKEN", raising=False)
    # Act
    summary = gr.run_reaper_poll()
    # Assert
    assert summary["skipped"] == "no_token"
    assert summary["dispatched"] == 0
