from __future__ import annotations

import json

import httpx
import pytest

from briefing_sources import SourceError
from task_sources import MAX_ATTENTION_READS, PAGE_SIZE, TaskRepository, parse_task
from test_briefing_sources import HEAD, REPO, TOKEN, Vault
from test_task_service import PATH, PROJECT, TODAY, source_text


@pytest.fixture(autouse=True)
def configure(monkeypatch):
    monkeypatch.setenv("DIG_REPO", REPO)


def repository(vault):
    return TaskRepository(vault.client, token=TOKEN, repo=REPO)


def test_multiline_definition_and_dates_are_not_flattened_or_reinterpreted():
    task = parse_task(source_text(), PATH)
    assert task["next_action"] == "Write a heading.\nKeep one topic."
    assert task["deadline"] == "2026-10-20"
    assert task["review_on"] == "2026-10-12"
    assert task["focus_on"] == "2026-10-07"
    assert task["definition"] == {"assessment": "structure_only", "complete": True, "missing": []}
    legacy = parse_task(source_text().replace("stage: ready\n", ""), PATH)
    assert legacy["stage"] == "untriaged"
    assert "stage" in legacy["definition"]["missing"]


def test_direct_read_requires_pinned_regular_tree_entry_not_contents_type_file():
    vault = Vault({PATH: source_text()})
    vault.tree[0]["mode"] = "120000"
    assert repository(vault).read(PATH) is None
    assert not vault.reads


def test_reader_verifies_returned_blob_matches_the_pinned_tree():
    def override(request):
        if "/contents/" in request.url.path:
            return httpx.Response(200, json={
                "path": PATH, "type": "file", "encoding": "base64", "sha": "1" * 40, "content": "",
            })
    vault = Vault({PATH: source_text()}, override=override)
    with pytest.raises(SourceError, match="source_snapshot_mismatch"):
        repository(vault).read(PATH)
    assert all(request.url.params.get("ref") == HEAD for request in vault.reads)


def test_bounded_paged_view_is_honest_about_partial_coverage():
    vault = Vault({
        f"tasks/2026-10-08-synthetic-{index:02}.md": source_text() for index in range(15)
    })
    result = repository(vault).page(today=TODAY)
    assert len(result["items"]) == PAGE_SIZE
    assert result["candidate_count"] == 15 and result["next_offset"] == 12
    assert result["complete"] is False
    assert len(vault.reads) == 15
    assert result["attention_complete"] is True
    second = repository(vault).page(12, today=TODAY)
    assert len(second["items"]) == 3 and second["next_offset"] is None
    assert second["complete"] is False


def test_attention_bound_has_explicit_cursor_instead_of_false_no_due_claim():
    undated = source_text().replace("deadline: 2026-10-20\n", "").replace("review_on: 2026-10-12\n", "")
    files = {f"tasks/2020-01-01-old-{index:03}.md": undated for index in range(MAX_ATTENTION_READS)}
    newer = "tasks/2026-10-08-new-due.md"
    files[newer] = undated.replace("type: task", "type: task\nreview_on: " + TODAY.isoformat())
    vault = Vault(files)
    first = repository(vault).page(today=TODAY)
    assert first["attention_complete"] is False
    assert first["attention"] == {
        "status": "incomplete", "assessed_from": 0,
        "attempted_count": MAX_ATTENTION_READS, "assessed_count": MAX_ATTENTION_READS,
        "excluded_count": 0, "unavailable_count": 0, "scope": "permitted",
        "candidate_count": MAX_ATTENTION_READS + 1, "next_cursor": MAX_ATTENTION_READS,
    }
    assert len(vault.reads) == MAX_ATTENTION_READS
    assert "does not mean nothing is due" in " ".join(first["warnings"])
    resumed = repository(vault).page(first["attention"]["next_cursor"], today=TODAY)
    assert resumed["items"][0]["path"] == newer
    assert resumed["attention_complete"] is False
    assert resumed["attention"]["next_cursor"] is None


def test_attention_pagination_has_no_missing_or_duplicate_rows_within_assessed_window():
    files = {
        f"tasks/2020-01-01-old-{index:02}.md": source_text().replace(
            "review_on: 2026-10-12", "review_on: 2026-10-07" if index % 3 == 0 else "review_on: 2026-11-12",
        )
        for index in range(27)
    }
    vault = Vault(files)
    seen, offset = [], 0
    while offset is not None:
        page = repository(vault).page(offset, today=TODAY)
        seen.extend(item["path"] for item in page["items"])
        offset = page["next_offset"]
    assert len(seen) == len(set(seen)) == len(files)


def test_failed_source_assessment_is_not_counted_as_successful_attention_coverage():
    due = "tasks/2026-10-08-due.md"
    unavailable = "tasks/2020-01-01-unavailable.md"

    def override(request):
        if request.url.path.endswith(unavailable):
            return httpx.Response(503, text="private upstream error detail")

    vault = Vault({
        due: source_text().replace("review_on: 2026-10-12", "review_on: " + TODAY.isoformat()),
        unavailable: source_text(),
    }, override=override)
    result = repository(vault).page(today=TODAY)
    assert [item["path"] for item in result["items"]] == [due]
    assert result["attention_complete"] is False
    assert result["attention"]["attempted_count"] == 2
    assert result["attention"]["assessed_count"] == 1
    assert result["errors"] == [{"code": "task_source_unavailable"}]
    assert unavailable not in json.dumps(result)
    assert "private upstream" not in json.dumps(result)


def test_attention_assessment_keeps_existing_bounded_source_response_reader():
    oversized = "tasks/2020-01-01-oversized-response.md"
    due = "tasks/2026-10-08-due.md"

    def override(request):
        if request.url.path.endswith(oversized):
            return httpx.Response(200, content=b"x" * 300_000)

    vault = Vault({
        oversized: source_text(),
        due: source_text().replace("review_on: 2026-10-12", "review_on: " + TODAY.isoformat()),
    }, override=override)
    result = repository(vault).page(today=TODAY)
    assert [item["path"] for item in result["items"]] == [due]
    assert result["attention_complete"] is False
    assert result["attention"]["assessed_count"] == 1
    assert result["errors"] == [{"code": "task_source_unavailable"}]


@pytest.mark.parametrize("marker", [
    "private: true", "routing: aibsVault", "ignored: true", "generated: true",
    "derived: true", "scope: work", "sensitivity: sensitive",
])
def test_ineligible_task_text_and_paths_are_not_exposed(marker):
    raw = source_text().replace("type: task", "type: task\n" + marker)
    vault = Vault({PATH: raw})
    result = repository(vault).page(today=TODAY)
    assert result["items"] == []
    assert result["errors"] == []
    assert result["excluded_count"] == 1
    assert result["attention_complete"] is True
    assert result["attention"]["scope"] == "permitted"
    assert PATH not in json.dumps(result)
    assert "Do not expand" not in json.dumps(result)


def test_project_inventory_is_verified_for_privacy_but_not_implicitly_selected():
    project = f"projects/{PROJECT}/README.md"
    hidden = "projects/2026-synthetic-hidden/README.md"
    vault = Vault({
        project: "# Synthetic project\nA generic source.",
        hidden: "---\nprivate: true\n---\n# Hidden\nDo not return this title.",
    })
    result = repository(vault).projects()
    assert [item["id"] for item in result["items"]] == [PROJECT]
    assert result["items"][0]["assessment"] == "inventory_only"
    assert result["excluded_count"] == 1 and result["errors"] == []
    assert "hidden" not in json.dumps(result)


@pytest.mark.parametrize("marker", [
    "private: maybe", "routing: '{{unresolved}}'", "type: {task: pending}",
    "private: &flag false", "sensitivity: undefined-value",
    'private: "true', 'routing: "another-vault',
    '"pr\\ivate": false', '"pr\\x69vate": false',
])
def test_ambiguous_policy_is_an_unresolved_error_not_complete_scope(marker):
    raw = source_text().replace("type: task", "type: task\n" + marker)
    vault = Vault({PATH: raw})
    result = repository(vault).page(today=TODAY)
    assert result["items"] == []
    assert result["excluded_count"] == 0
    assert result["errors"] == [{"code": "task_source_policy_unresolved"}]
    assert result["attention_complete"] is False
    assert result["attention"]["unavailable_count"] == 1
    assert PATH not in json.dumps(result) and raw not in json.dumps(result)


def test_unknown_reader_refusal_is_not_assumed_to_be_a_known_exclusion(monkeypatch):
    vault = Vault({PATH: source_text()})
    subject = repository(vault)
    monkeypatch.setattr(subject, "_record", lambda *_: (_ for _ in ()).throw(SourceError("task_source_not_permitted")))
    result = subject.page(today=TODAY)
    assert result["excluded_count"] == 0
    assert result["errors"] == [{"code": "task_source_unavailable"}]
    assert result["attention_complete"] is False


def test_fourteen_permitted_tasks_and_one_routed_source_keep_dates_and_pagination_intact():
    files = {
        f"tasks/2026-10-08-item-{index:02}.md": source_text()
        for index in range(14)
    }
    routed = "tasks/2026-10-08-out-of-scope.md"
    files[routed] = source_text().replace("type: task", "type: task\nrouting: another-vault")
    vault = Vault(files)
    first = repository(vault).page(today=TODAY)
    second = repository(vault).page(first["next_offset"], today=TODAY)
    assert first["candidate_count"] == second["candidate_count"] == 15
    assert len(first["items"]) == 12 and len(second["items"]) == 2
    assert first["excluded_count"] == second["excluded_count"] == 1
    assert first["errors"] == second["errors"] == []
    assert first["attention_complete"] is second["attention_complete"] is True
    assert first["complete"] is False
    assert second["next_offset"] is None
    assert {item["path"] for item in first["items"] + second["items"]} == set(files) - {routed}
    assert all(item["deadline"] == "2026-10-20" and item["focus_on"] == "2026-10-07"
               for item in first["items"] + second["items"])
    assert vault.files == files


def test_excluded_only_window_still_offers_next_unassessed_window():
    raw = source_text().replace("type: task", "type: task\nprivate: true")
    files = {f"tasks/2020-01-01-item-{index:03}.md": raw for index in range(MAX_ATTENTION_READS)}
    next_path = "tasks/2026-10-08-permitted.md"
    files[next_path] = source_text()
    vault = Vault(files)
    first = repository(vault).page(today=TODAY)
    assert first["items"] == [] and first["errors"] == []
    assert first["attention_complete"] is False
    assert first["excluded_count"] == MAX_ATTENTION_READS
    assert first["next_offset"] == first["attention"]["next_cursor"] == MAX_ATTENTION_READS
    second = repository(vault).page(first["next_offset"], today=TODAY)
    assert [item["path"] for item in second["items"]] == [next_path]
    assert second["next_offset"] is None
    assert second["attention_complete"] is False


def test_partial_project_failure_does_not_discard_verified_task_overview():
    failed = "projects/2026-failed/README.md"
    excluded = "projects/2026-excluded/README.md"
    allowed = "projects/2026-allowed/README.md"

    def override(request):
        if request.url.path.endswith(failed):
            return httpx.Response(503, text="untrusted private response")

    vault = Vault({
        PATH: source_text(), allowed: "# Project\nPermitted context.",
        excluded: "# Project\nReferences .me locally.",
        failed: "# Project\nUnavailable context.",
    }, override=override)
    result = repository(vault).page(today=TODAY)
    assert [item["path"] for item in result["items"]] == [PATH]
    assert result["attention_complete"] is True
    assert result["project_excluded_count"] == 1
    assert result["project_errors"] == [{"code": "task_source_unavailable"}]
    assert [item["id"] for item in result["projects"]] == ["2026-allowed"]
    assert failed not in json.dumps(result) and excluded not in json.dumps(result)


def test_project_pages_count_known_exclusions_without_misreporting_read_failure():
    files = {f"projects/2026-item-{index:02}/README.md": "# Project\nPermitted context." for index in range(6)}
    for index in (0, 3, 5):
        files[f"projects/2026-item-{index:02}/README.md"] = "# Project\nReferences .me locally."
    vault = Vault(files)
    first = repository(vault).page(today=TODAY)
    assert first["project_excluded_count"] == 2
    assert first["project_errors"] == []
    assert first["project_next_offset"] == 4
    assert first["project_candidate_count"] == 6
    second = repository(vault).projects(first["project_next_offset"])
    assert second["excluded_count"] == 1 and second["errors"] == []
    assert len(first["projects"] + second["items"]) == 3
    assert second["next_offset"] is None


def test_direct_rechecks_still_reject_sources_excluded_from_catalog():
    raw = source_text().replace("type: task", "type: task\nprivate: true")
    vault = Vault({PATH: raw})
    subject = repository(vault)
    assert subject.page(today=TODAY)["excluded_count"] == 1
    with pytest.raises(SourceError, match="task_source_not_permitted"):
        subject.read(PATH)
    with pytest.raises(SourceError, match="task_source_not_permitted"):
        subject.read_receipt_source(PATH)


@pytest.mark.parametrize("path", ["tasks/done/test.md", "../tasks/test.md", "tasks/test.private.md", "work/test.md"])
def test_reader_cannot_be_used_to_scan_arbitrary_or_closed_private_records(path):
    vault = Vault({PATH: source_text()})
    with pytest.raises(SourceError, match="task_source_not_permitted"):
        repository(vault).read(path)
    assert not vault.requests


def test_duplicate_operational_fields_fail_visibly():
    raw = source_text().replace("stage: ready", "stage: ready\nstage: doing")
    with pytest.raises(SourceError, match="task_metadata_invalid"):
        parse_task(raw, PATH)


def test_invalid_dates_are_not_silently_empty():
    with pytest.raises(SourceError, match="task_date_invalid"):
        parse_task(source_text().replace("2026-10-20", "2026-02-30"), PATH)
