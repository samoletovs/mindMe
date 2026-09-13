from __future__ import annotations

from datetime import date

import pytest

from briefing_plan import PlanError, model_input, plan_schema, validate_plan

TODAY = date(2026, 9, 13)
REVISION = "a" * 40


def source(path: str, kind: str = "note") -> dict:
    return {
        "path": path, "kind": kind, "title": path, "text": "Synthetic source evidence.",
        "revision": REVISION, "digest": "b" * 64,
        "url": "https://example.invalid/source",
    }


def test_tasks_cannot_crowd_out_changed_evidence_and_goals() -> None:
    changed = source("notes/changed.md")
    goal = source("home.md", "goal")
    tasks = [
        {"path": f"tasks/task-{n}.md", "title": f"Task {n}", "revision": REVISION}
        for n in range(24)
    ]
    context = {
        "date": TODAY.isoformat(), "sources": [changed, goal], "changes": [changed],
        "goals": [goal], "tasks": tasks, "warnings": [],
    }

    packet = model_input(context, [])

    paths = {item["path"] for item in packet["sources"]}
    assert changed["path"] in paths
    assert goal["path"] in paths
    assert len(paths) == 24
    assert packet["deferred_source_count"] == 2
    assert packet["changed_paths"] == [changed["path"]]
    assert packet["warnings"]
    assert context["warnings"] == []


def test_changed_paths_and_schema_only_name_supplied_evidence() -> None:
    records = [source(f"notes/source-{n}.md") for n in range(30)]
    packet = model_input({
        "date": TODAY.isoformat(), "sources": records, "changes": records, "tasks": [],
    }, [])

    paths = {item["path"] for item in packet["sources"]}
    assert set(packet["changed_paths"]) <= paths
    assert set(packet["change_kinds"]) <= paths
    assert records[-1]["path"] not in packet["changed_paths"]
    schema = plan_schema(packet)["properties"]
    assert set(schema["changes"]["items"]["properties"]["path"]["enum"]) <= paths


@pytest.mark.parametrize("field", ["focus", "changes", "proposal"])
def test_existing_but_omitted_source_cannot_validate(field: str) -> None:
    records = [source(f"notes/source-{n}.md") for n in range(30)]
    context = {
        "date": TODAY.isoformat(), "sources": records, "changes": records, "tasks": [],
    }
    omitted = records[-1]["path"]
    assert omitted not in {item["path"] for item in model_input(context, [])["sources"]}
    raw = {"focus": None, "changes": [], "proposal": None}
    if field == "focus":
        raw[field] = {"path": omitted, "text": "An unsupported recommendation."}
    elif field == "changes":
        raw[field] = [{"path": omitted, "why": "An unsupported explanation."}]
    else:
        raw[field] = {
            "kind": "research", "source_path": omitted,
            "text": "Which public learning methods are effective?", "why": "Choose a method.",
        }

    with pytest.raises(PlanError, match="unbacked"):
        validate_plan(raw, context, TODAY)
