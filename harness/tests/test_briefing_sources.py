from __future__ import annotations

import base64
import hashlib
import json
import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from briefing_sources import (
    MAX_CONTENT_FETCHES,
    MAX_SOURCE_CHARS,
    MAX_TOTAL_CHARS,
    MAX_TREE_ENTRIES,
    SourceError,
    load_sources,
    read_source_revision,
)

REPO = "example/mindVault"
TOKEN = "synthetic-token"
HEAD = "a" * 40
TREE = "b" * 40
OLDER = "c" * 40
OLDER_TREE = "d" * 40
HOME = """# Home
## Current focus
| Area | Approved goal | Next step |
| --- | --- | --- |
| Learning | Practise a small experiment | Read [Project](projects/learning/README.md) |
### Example
Do not treat this example as a goal.
## Annual goals (draft)
This annual draft is not an approved goal.
## Historical focus
This historical goal is no longer active.
<!-- BEGIN TASK-BOARD -->
## Focus
Generated old task is not a goal.
<!-- END TASK-BOARD -->
"""


def test_review_evidence_uses_original_byte_hash_and_does_not_invent_joined_quotes(monkeypatch):
    monkeypatch.setenv("DIG_REPO", REPO)
    raw = "# Evidence\r\nA real source sentence.\r\nBefore<details>hidden metadata</details>after\r\n"
    vault = Vault({"notes/pilot.md": raw, "home.md": HOME})
    result = load_sources(
        vault.client, token=TOKEN, repo=REPO,
        sections=["knowledge", "goals"], include_evidence=True,
    )
    source = next(item for item in result["sources"] if item["kind"] == "note")
    assert source["sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    assert "A real source sentence." in source["evidence_text"]
    assert "Beforeafter" not in source["evidence_text"]
    assert all(line in raw for line in source["evidence_text"].splitlines())
    assert "evidence_text" not in next(item for item in result["sources"] if item["kind"] == "goal")


def test_review_writer_byte_and_depth_caps_do_not_change_legacy_briefing_selection(monkeypatch):
    monkeypatch.setenv("DIG_REPO", REPO)
    large = "Permitted source evidence.\n" + "x" * 64_000
    deep = "wiki/insights/a/b/c/d/e.md"
    files = {"notes/large.md": large, deep: "Permitted source evidence in a deep page."}
    review_vault = Vault(files)
    review = load_sources(review_vault.client, token=TOKEN, repo=REPO, sections=["knowledge"], include_evidence=True)
    assert not review["sources"]
    assert review["complete"] is False
    assert not any("/contents/" in request.url.path for request in review_vault.requests)
    legacy_vault = Vault(files)
    legacy = load_sources(legacy_vault.client, token=TOKEN, repo=REPO, sections=["knowledge"])
    assert {source["path"] for source in legacy["sources"]} == set(files)


def test_review_aggregate_source_bytes_match_the_writer_limit(monkeypatch):
    monkeypatch.setenv("DIG_REPO", REPO)
    text = "Permitted source evidence.\n"
    text += "x" * (64_000 - len(text))
    vault = Vault({f"notes/source-{index}.md": text for index in range(10)})
    result = load_sources(vault.client, token=TOKEN, repo=REPO, sections=["knowledge"], include_evidence=True)
    assert len(result["sources"]) == 8
    assert sum(source["byte_size"] for source in result["sources"]) == 512_000
    assert result["complete"] is False


def test_review_does_not_offer_focus_only_project_evidence_the_writer_cannot_authorize(monkeypatch):
    monkeypatch.setenv("DIG_REPO", REPO)
    files = {"home.md": HOME, "projects/learning/README.md": "# Learning\nRun a small experiment."}
    vault = Vault(files)
    review = load_sources(vault.client, token=TOKEN, repo=REPO, sections=["goals", "focus"], include_evidence=True)
    assert all(source["kind"] != "project" for source in review["sources"])
    legacy_vault = Vault(files)
    legacy = load_sources(legacy_vault.client, token=TOKEN, repo=REPO, sections=["goals", "focus"])
    assert any(source["kind"] == "project" for source in legacy["sources"])


@pytest.mark.parametrize("raw", [
    "---\nvisibility: internal\n---\n# Pilot\nPermitted ordinary briefing evidence.",
    "---\nvisibility: non-sensitive\n---\n# Pilot\nPermitted ordinary briefing evidence.",
    "---\nroute: personal-non-sensitive\n---\n# Pilot\nPermitted ordinary briefing evidence.",
    "---\nclassification: internal\n---\n# Pilot\nPermitted ordinary briefing evidence.",
    "# Pilot\nThis generic article discusses medical systems.",
    "# Pilot\nThe source mentions a passport as an example.",
    "---\nscope: work\n---\n# Pilot\nOrdinary text cannot override the scope.",
    "---\ngenerated: true\n---\n# Pilot\nThis is a derived observation.",
    "---\nrole: journal\n---\n# Pilot\nThis is not eligible knowledge evidence.",
])
def test_review_excludes_sources_the_publication_writer_would_refuse(monkeypatch, raw):
    monkeypatch.setenv("DIG_REPO", REPO)
    path = "notes/pilot.md"
    vault = Vault({path: raw, "notes/allowed.md": "# Allowed\nAn ordinary permitted observation."})
    result = load_sources(
        vault.client, token=TOKEN, repo=REPO, sections=["knowledge"], include_evidence=True,
    )
    assert [source["path"] for source in result["sources"]] == ["notes/allowed.md"]
    assert path not in result["source_revisions"]
    assert path not in result["fingerprints"]
    assert any("publication policy" in warning for warning in result["warnings"])


def test_review_filter_does_not_change_ordinary_briefing_eligibility(monkeypatch):
    monkeypatch.setenv("DIG_REPO", REPO)
    path = "notes/pilot.md"
    vault = Vault({path: "---\nclassification: internal\n---\n# Pilot\nA generic observation."})
    result = vault.load(["knowledge"])
    assert [source["path"] for source in result["sources"]] == [path]


def test_review_revalidation_applies_the_same_publication_policy(monkeypatch):
    monkeypatch.setenv("DIG_REPO", REPO)
    path = "notes/pilot.md"
    vault = Vault({path: "---\nclassification: internal\n---\n# Pilot\nA generic observation."})
    assert read_source_revision(vault.client, token=TOKEN, repo=REPO, path=path)
    with pytest.raises(SourceError, match="source_no_longer_permitted"):
        read_source_revision(vault.client, token=TOKEN, repo=REPO, path=path, include_evidence=True)


def blob(text: str) -> str:
    data = text.encode()
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


def contents(path: str, text: str) -> dict[str, Any]:
    return {
        "path": path, "type": "file", "encoding": "base64", "sha": blob(text),
        "size": len(text.encode()), "content": base64.b64encode(text.encode()).decode(),
    }


class Vault:
    def __init__(
        self, files: dict[str, str], *, touched: list[str] | None = None,
        override: Callable[[httpx.Request], httpx.Response | None] | None = None,
    ) -> None:
        self.files = files
        self.touched = touched or []
        self.override = override
        self.requests: list[httpx.Request] = []
        self.tree = [
            {"path": path, "type": "blob", "mode": "100644", "sha": blob(text), "size": len(text.encode())}
            for path, text in files.items()
        ]
        self.client = httpx.Client(transport=httpx.MockTransport(self.handle), follow_redirects=True)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.host == "api.github.com"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        if self.override and (response := self.override(request)) is not None:
            return response
        root = "/repos/" + REPO
        path = request.url.path
        if path == root:
            return httpx.Response(200, json={"default_branch": "main"})
        if path == root + "/git/ref/heads/main":
            return httpx.Response(200, json={
                "object": {"type": "commit", "sha": HEAD},
            })
        if path == root + "/git/commits/" + HEAD:
            return httpx.Response(200, json={"sha": HEAD, "tree": {"sha": TREE}})
        if path == root + "/git/trees/" + TREE:
            return httpx.Response(200, json={"truncated": False, "tree": self.tree})
        if path == root + "/commits":
            assert request.url.params["sha"] == HEAD
            return httpx.Response(200, json=[
                {"sha": HEAD}, {"sha": OLDER, "commit": {"tree": {"sha": OLDER_TREE}}},
            ])
        if path == root + "/git/trees/" + OLDER_TREE:
            older = [
                {**file, "sha": "e" * 40} if file["path"] in self.touched else file
                for file in self.tree
            ]
            return httpx.Response(200, json={"truncated": False, "tree": older})
        if path.startswith(root + "/contents/"):
            source = path.removeprefix(root + "/contents/")
            if source not in self.files:
                return httpx.Response(404, json={"message": "synthetic missing"})
            return httpx.Response(200, json=contents(source, self.files[source]))
        raise AssertionError("Unexpected synthetic API request")

    def load(
        self, sections: list[str] | None = None, previous: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return load_sources(
            self.client, token=TOKEN, repo=REPO,
            sections=sections if sections is not None else ["focus", "loops", "knowledge"],
            previous=previous,
        )

    @property
    def reads(self) -> list[httpx.Request]:
        return [request for request in self.requests if "/contents/" in request.url.path]


@pytest.fixture(autouse=True)
def configure_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIG_REPO", REPO)


def test_pins_every_content_read_to_one_canonical_default_branch_head() -> None:
    vault = Vault({"home.md": HOME, "notes/old-note.md": "# Evidence\nUseful evidence."})
    result = vault.load()
    assert sum(request.url.path.endswith("/git/ref/heads/main") for request in vault.requests) == 1
    assert not any("/commits/main" in request.url.path for request in vault.requests)
    assert all(request.url.params["ref"] == HEAD for request in vault.reads)
    assert result["revision"] == HEAD
    assert result["source_revisions"]["notes/old-note.md"] == blob(vault.files["notes/old-note.md"])
    assert all(f"/blob/{HEAD}/" in source["url"] for source in result["sources"])
    json.dumps(result)


def test_unchanged_recent_sources_do_not_starve_the_unprocessed_backlog() -> None:
    files = {f"notes/n-{number:02}.md": f"# Note {number}\nA useful public learning observation." for number in range(30)}
    first_vault = Vault(files)
    first = load_sources(
        first_vault.client, token=TOKEN, repo=REPO, sections=["knowledge"], known_revisions={},
    )
    second_vault = Vault(files, touched=list(first["processed_revisions"]))
    second = load_sources(
        second_vault.client, token=TOKEN, repo=REPO, sections=["knowledge"],
        previous=first["fingerprints"], known_revisions=first["processed_revisions"],
        scan_cursor=first["scan_cursor"],
    )
    assert len(first_vault.reads) <= MAX_CONTENT_FETCHES
    assert len(second_vault.reads) <= MAX_CONTENT_FETCHES
    assert set(first["processed_revisions"]) | set(second["processed_revisions"]) == set(files)


def test_changed_old_source_outranks_unchanged_recent_commit_paths() -> None:
    old = {f"notes/n-{number:02}.md": f"# Note {number}\nAn earlier public observation." for number in range(30)}
    files = {**old, "notes/n-28.md": "# Note 28\nA materially different observation."}
    vault = Vault(files, touched=["notes/n-00.md", "notes/n-01.md"])
    result = load_sources(
        vault.client, token=TOKEN, repo=REPO, sections=["knowledge"],
        previous={path: "old-semantic-digest" for path in old},
        known_revisions={path: blob(text) for path, text in old.items()},
    )
    assert result["sources"][0]["path"] == "notes/n-28.md"


def test_context_records_match_the_integrated_planner_contract() -> None:
    path = "notes/changed.md"
    before = Vault({path: "# Evidence\nEarlier observation."}).load()["fingerprints"]
    sections = ["focus", "knowledge"]
    result = Vault({"home.md": HOME, path: "# Evidence\nA material new observation."}).load(sections, before)
    expected = {"path", "revision", "digest", "title", "text", "kind", "url"}
    assert result["sections"] == sections
    assert result["sections"] is not sections
    for key in ("sources", "goals", "changes"):
        assert result[key]
        assert all(isinstance(source, dict) and expected <= source.keys() for source in result[key])


def test_edited_old_filename_is_a_material_change_before_recent_additions() -> None:
    path = "notes/2001-01-01-old-note.md"
    before = Vault({path: "# Observation\nTry reading."}).load()
    files = {f"notes/2026-09-{day:02d}-new.md": "# Idea\nConsider a walk." for day in range(1, 23)}
    files[path] = "# Observation\nThe earlier reading method did not help; try practice."
    vault = Vault(files, touched=[path])
    result = vault.load(previous=before["fingerprints"])
    assert result["sources"][0]["path"] == path
    assert result["changes"][0]["text"].endswith("try practice.")
    assert result["complete"] is False
    assert len(vault.reads) <= MAX_CONTENT_FETCHES


def test_old_uncheckpointed_file_touched_in_git_is_not_ranked_by_filename_date() -> None:
    path = "notes/1999-01-01-revised.md"
    files = {f"notes/2026-09-{day:02d}-note.md": "# Note\nOther material." for day in range(1, 23)}
    files[path] = "# Revised\nNew evidence in an older file."
    result = Vault(files, touched=[path]).load()
    assert result["sources"][0]["path"] == path
    assert result["initial_baseline"] is True
    assert result["changes"] == []
    assert any("Initial" in warning for warning in result["warnings"])


def test_generation_metadata_and_index_only_edits_do_not_manufacture_changes() -> None:
    path = "notes/observation.md"
    old = """---
updated: 2026-01-01
generated_at: first
---
# Practice
Use retrieval practice.
## Index
- [Index one](one.md)
"""
    new = old.replace("2026-01-01", "2026-09-13").replace("first", "second").replace("one", "two")
    previous = Vault({path: old}).load()["fingerprints"]
    result = Vault({path: new, "notes/index.md": "# New index"}).load(previous=previous)
    assert result["fingerprints"] == previous
    assert result["changes"] == []
    assert "notes/index.md" not in result["source_revisions"]
    assert result["sources"][0]["revision"] == blob(new)


def test_generation_timestamp_comment_does_not_remove_material_research_body() -> None:
    path = "areas/agents/research/finding.md"
    old = "# Finding\n<!-- Generated at: 2026-01-01 -->\nRetrieval practice supports learning."
    new = old.replace("2026-01-01", "2026-09-13")
    previous = Vault({path: old}).load()["fingerprints"]
    result = Vault({path: new}).load(previous=previous)
    assert "Retrieval practice supports learning" in result["sources"][0]["text"]
    assert result["changes"] == []


def test_generated_task_board_underscore_markers_do_not_hide_subsequent_approved_table() -> None:
    home = (
        "<!-- BEGIN_TASK_BOARD -->\n## Current focus\n"
        "| Goal | Next step |\n| --- | --- |\n| Generated task | Not authoritative |\n"
        "<!-- END_TASK_BOARD -->\n" + HOME
    )
    text = Vault({"home.md": home}).load()["goals"][0]["text"]
    assert "Practise a small experiment" in text
    assert "Generated task" not in text


def test_approved_focus_table_is_preserved_without_drafts_history_or_generated_tasks() -> None:
    result = Vault({"home.md": HOME}).load()
    text = result["goals"][0]["text"]
    assert "Practise a small experiment" in text
    for excluded in ("annual draft", "historical goal", "this example", "Generated old task"):
        assert excluded not in text


def test_approved_focus_link_admits_non_sensitive_project_without_metadata() -> None:
    result = Vault({
        "home.md": HOME, "projects/learning/README.md": "# Learning\nNext: practise one example.",
        "projects/paused/README.md": "---\nstatus: paused\n---\n# Paused\nNot active.",
    }).load()
    assert "projects/learning/README.md" in result["fingerprints"]
    assert "projects/paused/README.md" not in result["fingerprints"]


@pytest.mark.parametrize("path", [
    "notes/../home.md", "notes/.hidden.md", "notes/test.private.md", "notes/test-private.md",
    "notes/test.full.md", "notes/_originals/source.md", ".me/note.md", "inbox/raw/source.md",
    "areas/legal/case.md", "projects/tax-return/README.md", "projects/medical-plan/README.md",
    "projects/contract-dispute/README.md", "journal/today.md", "archive/project/README.md",
    "wiki/index.md", "notes/%2e%2e/home.md", "notes\\note.md", "notes/name.md?ref=evil",
])
def test_disallowed_paths_are_never_fetched(path: str) -> None:
    vault = Vault({path: "# Content\nDo not read."})
    result = vault.load()
    assert not vault.reads
    assert not result["source_revisions"]
    assert not result["sources"]


@pytest.mark.parametrize("metadata", [
    "private: true", "sensitive: true", "sensitivity: sensitive", "routing: .me",
    "routing: aibsVault", "destination: familyVault", "ownership: employer",
])
def test_sensitive_or_routed_metadata_never_reaches_context(metadata: str) -> None:
    path = "notes/synthetic.md"
    result = Vault({path: f"---\n{metadata}\n---\n# Restricted\nRestricted payload."}).load()
    assert result["sources"] == []
    assert path not in result["fingerprints"]
    assert path not in result["source_revisions"]
    assert path in result["inventory_paths"]
    assert "Restricted payload" not in json.dumps(result)


def test_collapsed_metadata_is_checked_before_being_removed() -> None:
    result = Vault({"notes/synthetic.md": (
        "# Restricted\n<details><summary>Metadata</summary>\n"
        "```yaml\nprivate: true\n```\n</details>\nDo not disclose this."
    )}).load()
    assert result["sources"] == []


@pytest.mark.parametrize("metadata", [
    "sensitivity:\n  level: sensitive", "private: true", '"private": true', "'private': true",
    '"routing": work', "private:\n  true", '"\\u0070rivate": true',
])
def test_unfenced_and_nested_privacy_metadata_is_fail_closed(metadata: str) -> None:
    assert Vault({"notes/synthetic.md": metadata + "\n# Restricted\nDo not disclose."}).load()["sources"] == []


@pytest.mark.parametrize("metadata", ['"private": true', "private:\n  true", "{private: true}", "private: *flag"])
def test_ambiguous_or_complex_privacy_metadata_cannot_bypass_loader_or_revision(metadata: str) -> None:
    path = "notes/synthetic.md"
    vault = Vault({path: f"---\n{metadata}\n---\n# Restricted\nDo not disclose."})
    assert vault.load()["sources"] == []
    with pytest.raises(SourceError, match="source_no_longer_permitted"):
        read_source_revision(vault.client, token=TOKEN, repo=REPO, path=path)


def test_home_source_only_includes_approved_table_not_future_focus_or_private_subsections() -> None:
    home = HOME + """
## Future focus
| Goal | Next step |
| --- | --- |
| Unapproved goal | Not a commitment |
## Current focus
Unapproved surrounding prose.
| Goal | Next step |
| --- | --- |
| Approved learning | A small experiment |
### Private notes
This private prose must not appear.
| Goal | Next step |
| --- | --- |
| Private goal | Do not disclose |
"""
    text = Vault({"home.md": home}).load()["goals"][0]["text"]
    assert "Approved learning" in text
    for excluded in ("Unapproved", "private prose", "Private goal"):
        assert excluded not in text


def test_newly_changed_source_is_not_permanently_starved_by_full_prior_checkpoint() -> None:
    files = {f"notes/checkpointed-{index:02d}.md": "# Prior\nUnchanged evidence." for index in range(16)}
    previous = Vault(files).load()["fingerprints"]
    path = "notes/new-material.md"
    files[path] = "# Material\nA newly changed finding."
    result = Vault(files, touched=[path]).load(previous=previous)
    assert result["sources"][0]["path"] == path
    assert path in {source["path"] for source in result["changes"]}


@pytest.mark.parametrize("secret", [
    "ghp_" + "x" * 36, "AccountKey=synthetic-not-a-real-key", "password: synthetic-value",
    "patient name: synthetic person", "Case number: synthetic-value",
    "Records are in ../.me/records/", "synthetic@example.invalid",
    '{"access_token": "synthetic-value"}', "Balance: synthetic amount",
])
def test_credential_and_sensitive_content_guards_reject_whole_note(secret: str) -> None:
    result = Vault({"notes/synthetic.md": "# Restricted\n" + secret}).load()
    assert result["sources"] == []
    assert secret not in json.dumps(result)


def test_source_caps_are_visible_and_unprocessed_items_are_not_checkpointed() -> None:
    files = {f"notes/item-{number:02d}.md": "# Material\n" + "word " * 500 for number in range(25)}
    vault = Vault(files)
    result = vault.load()
    assert len(vault.reads) <= MAX_CONTENT_FETCHES
    assert all(len(source["text"]) <= MAX_SOURCE_CHARS for source in result["sources"])
    assert sum(len(source["text"]) for source in result["sources"]) <= MAX_TOTAL_CHARS
    assert result["complete"] is False
    assert any("unprocessed" in warning for warning in result["warnings"])
    assert len(result["fingerprints"]) < len(result["source_revisions"]) == len(files)
    assert set(result["fingerprints"]) == {source["path"] for source in result["sources"]}
    assert set(result["inventory_paths"]) == set(files)


@pytest.mark.parametrize("sections", [[], ["weather"], ["unknown"]])
def test_all_source_sections_off_makes_no_external_reads(sections: list[str]) -> None:
    vault = Vault({"home.md": HOME})
    result = load_sources(vault.client, token="", repo="", sections=sections)
    assert result["complete"] is True
    assert result["sources"] == []
    assert "inventory_paths" not in result
    assert vault.requests == []


def test_sections_preserve_disabled_knowledge_and_loop_preferences() -> None:
    vault = Vault({
        "home.md": HOME, "ideas/idea.md": "# Idea\nTry it.",
        "notes/note.md": "# Note\nRead it.", "areas/agents/research/report.md": "# Research\nEvidence.",
        "wiki/insights/insight.md": "# Insight\nUseful finding.",
    })
    result = vault.load(["focus"])
    assert [source["path"] for source in result["sources"]] == ["home.md"]
    assert all(request.url.path.endswith("home.md") for request in vault.reads)
    assert set(result["source_revisions"]) == {"home.md"}
    assert set(result["inventory_paths"]) == set(vault.files)


def test_inventory_preserves_all_regular_blobs_independent_of_read_permissions() -> None:
    files = {
        "home.md": HOME,
        "notes/disabled.md": "# Knowledge\nA disabled source.",
        "notes/private.private.md": "# Restricted\nNever fetched.",
        "notes/index.md": "# Index\nNever fetched.",
        ".settings/config.json": "{}",
        "projects/legal-case/README.md": "# Restricted\nNever fetched.",
        "tasks/current.md": "# Task\nA current task.",
    }
    vault = Vault(files)
    result = vault.load(["focus"])
    assert result["inventory_paths"] == sorted(files)
    assert set(result["source_revisions"]) == {"home.md"}
    assert len(vault.reads) == 1


def test_task_revisions_are_enabled_only_by_loops_without_reading_task_content() -> None:
    path = "tasks/current.md"
    vault = Vault({path: "# Task\nA current task."})
    assert vault.load(["knowledge"])["source_revisions"] == {}
    assert vault.load(["loops"])["source_revisions"] == {path: blob(vault.files[path])}
    assert vault.reads == []


@pytest.mark.parametrize("status", [401, 403, 429, 500, 302])
def test_revision_outages_and_redirects_are_not_source_deletions(status: int) -> None:
    vault = Vault({}, override=lambda request: httpx.Response(status, headers={"Location": "https://not-github.invalid/"}))
    with pytest.raises(SourceError):
        read_source_revision(vault.client, token=TOKEN, repo=REPO, path="notes/existing.md")
    assert len(vault.requests) == 1


def test_revision_missing_is_none_only_on_real_404() -> None:
    vault = Vault({})
    assert read_source_revision(vault.client, token=TOKEN, repo=REPO, path="notes/missing.md") is None


def test_success_response_with_null_body_is_not_mistaken_for_a_deletion() -> None:
    vault = Vault({}, override=lambda request: httpx.Response(200, content="null"))
    with pytest.raises(SourceError, match="source_content_invalid"):
        read_source_revision(vault.client, token=TOKEN, repo=REPO, path="notes/existing.md")


def test_revision_returns_current_blob_and_invalidates_newly_private_content() -> None:
    path = "tasks/synthetic-task.md"
    vault = Vault({path: "# Task\nTake one step."})
    assert read_source_revision(vault.client, token=TOKEN, repo=REPO, path=path) == blob(vault.files[path])
    vault.files[path] = "---\nprivate: true\n---\n# Task\nRestricted."
    with pytest.raises(SourceError, match="source_no_longer_permitted"):
        read_source_revision(vault.client, token=TOKEN, repo=REPO, path=path)


def test_deleted_sources_are_absent_from_current_inventory_and_fingerprints() -> None:
    previous = Vault({"notes/removed.md": "# Removed\nEarlier evidence."}).load()["fingerprints"]
    result = Vault({"notes/remaining.md": "# Remaining\nCurrent evidence."}).load(previous=previous)
    for key in ("fingerprints", "source_revisions", "inventory_paths"):
        assert "notes/removed.md" not in result[key]


def test_truncated_tree_is_not_certified_complete() -> None:
    vault = Vault({}, override=lambda request: (
        httpx.Response(200, json={"truncated": True, "tree": []})
        if "/git/trees/" in request.url.path else None
    ))
    with pytest.raises(SourceError, match="source_tree_incomplete"):
        vault.load()


def test_oversized_tree_is_not_certified_complete() -> None:
    vault = Vault({})
    vault.tree = [{}] * (MAX_TREE_ENTRIES + 1)
    with pytest.raises(SourceError, match="source_tree_too_large"):
        vault.load()


def test_symlink_is_not_read_even_if_its_name_is_allowed() -> None:
    vault = Vault({"notes/link.md": "notes/elsewhere.md"})
    vault.tree[0]["mode"] = "120000"
    result = vault.load()
    assert result["sources"] == []
    assert result["inventory_paths"] == []
    assert not vault.reads


@pytest.mark.parametrize("field", ["type", "mode"])
def test_malformed_eligible_tree_entry_is_not_reported_as_source_removal(field: str) -> None:
    vault = Vault({"notes/source.md": "# Note\nCurrent evidence."})
    del vault.tree[0][field]
    with pytest.raises(SourceError, match="source_tree_invalid"):
        vault.load()


def test_source_snapshot_mismatch_fails_closed() -> None:
    vault = Vault({"notes/test.md": "# Real\nOne snapshot."})
    vault.tree[0]["sha"] = "f" * 40
    with pytest.raises(SourceError, match="source_snapshot_mismatch"):
        vault.load()


def test_source_errors_and_http_logs_never_expose_paths_content_or_tokens(caplog: pytest.LogCaptureFixture) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("raw sensitive error " + TOKEN, request=request)

    vault = Vault({}, override=fail)
    with caplog.at_level(logging.DEBUG), pytest.raises(SourceError) as error:
        read_source_revision(vault.client, token=TOKEN, repo=REPO, path="notes/synthetic.md")
    assert str(error.value) == "source_read_failed"
    assert not caplog.records
    assert error.value.__suppress_context__


@pytest.mark.parametrize("repo", ["example/workVault", "other/mindVault", "https://api.github.com/example/mindVault"])
def test_repo_must_be_the_configured_personal_vault(repo: str) -> None:
    vault = Vault({})
    with pytest.raises(SourceError, match="sources_not_configured"):
        load_sources(vault.client, token=TOKEN, repo=repo, sections=["knowledge"])
    assert not vault.requests
