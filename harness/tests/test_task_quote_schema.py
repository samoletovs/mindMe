"""Synthetic exact-evidence generation constraints; no live provider calls."""

from __future__ import annotations

import copy
import json
from unittest.mock import Mock

import httpx
import pytest
from openai import OpenAI

import function_app as fa
from task_service import (
    MAX_PREPARATION_QUOTE_CHARS, MAX_PREPARATION_QUOTES, PREPARATION_SCHEMA,
    TaskError, TaskService, preparation_schema,
)
from test_task_service import PATH, REVISION, TODAY, approve, prepare_payload, service


def test_quote_enum_is_bounded_exact_and_does_not_mutate_shared_schema():
    original = copy.deepcopy(PREPARATION_SCHEMA)
    text = "# Synthetic\n" + "\n".join(
        f"Constraint {index}: " + ("bounded wording " * 9) + "." for index in range(40)
    )
    assert len(text) < 9000
    schema = preparation_schema({"source": {"text": text}})
    quotes = schema["properties"]["source_quote"]["enum"]
    assert 1 <= len(quotes) <= MAX_PREPARATION_QUOTES
    assert sum(map(len, quotes)) <= MAX_PREPARATION_QUOTE_CHARS
    assert all(len(quote) <= 500 and quote in text for quote in quotes)
    assert len(quotes) == len(set(quotes))
    assert "maxItems" not in json.dumps(schema)
    assert PREPARATION_SCHEMA == original
    schema["properties"]["source_quote"]["enum"].append("not a source quote")
    assert "enum" not in PREPARATION_SCHEMA["properties"]["source_quote"]


def test_candidate_selection_does_not_turn_removed_metadata_into_invented_evidence():
    text = (
        "# Synthetic\n\n<details>\n```yaml\ncaptured: 2026-10-10\nstage: ready\n```\n</details>\n"
        "Before<details>not evidence</details>after\n"
        "A canonical sentence stays unchanged."
    )
    quotes = preparation_schema({"source": {"text": text}})["properties"]["source_quote"]["enum"]
    assert quotes == ["A canonical sentence stays unchanged."]
    assert "Beforeafter" not in quotes


@pytest.mark.parametrize("text", [
    "# Heading\nTiny.",
    "# Metadata only\n<details>\n```yaml\ncaptured: 2026-10-10\n```\n</details>",
    "token: synthetic-not-a-credential",
])
def test_no_safe_quote_fails_before_model_construction(monkeypatch, text):
    client = Mock(side_effect=AssertionError("No provider client should be requested"))
    monkeypatch.setenv("MINDME_BRIEFING_MODEL", "existing-small-model")
    monkeypatch.setattr(fa, "_foundry", client)
    with pytest.raises(TaskError, match="task_preparation_evidence_invalid|task_text_not_permitted"):
        fa._generate_task_preparation({"source": {"text": text}, "scope": "Outline only."})
    client.assert_not_called()


def test_sensitive_source_is_rejected_before_building_generation_schema(monkeypatch, caplog):
    client = Mock(side_effect=AssertionError("Sensitive input must not reach a provider client"))
    monkeypatch.setenv("MINDME_BRIEFING_MODEL", "existing-small-model")
    monkeypatch.setattr(fa, "_foundry", client)
    marker = "synthetic-marker@example.invalid"
    with pytest.raises(TaskError, match="task_text_not_permitted"):
        fa._generate_task_preparation({
            "source": {"text": f"A safe-looking sentence.\n{marker}"}, "scope": "Draft only.",
        })
    client.assert_not_called()
    assert marker not in caplog.text


def test_quote_schema_is_counted_inside_existing_input_limit(monkeypatch):
    context = {"source": {"text": "A source sentence. " + "x" * 8100}, "scope": "Outline only."}
    schema = preparation_schema(context)
    assert len(json.dumps(context, ensure_ascii=False)) < 9000
    assert len(json.dumps(context, ensure_ascii=False)) + len(json.dumps(schema, ensure_ascii=False)) > 9000
    model = Mock(side_effect=AssertionError("Over-budget input must not construct a client"))
    monkeypatch.setenv("MINDME_BRIEFING_MODEL", "existing-small-model")
    monkeypatch.setattr(fa, "_foundry", model)
    with pytest.raises(TaskError, match="task_preparation_context_limit"):
        fa._generate_task_preparation(context)
    model.assert_not_called()


def test_full_wire_input_counts_prompts_fences_and_schema_not_only_source(monkeypatch):
    context = {"source": {"text": "A source sentence. " + "x" * 6000}, "scope": "Outline only."}
    schema = preparation_schema(context)
    assert len(json.dumps(context, ensure_ascii=False)) + len(json.dumps(schema, ensure_ascii=False)) < 9000
    model = Mock(side_effect=AssertionError("Over-budget full request must not construct a client"))
    monkeypatch.setenv("MINDME_BRIEFING_MODEL", "existing-small-model")
    monkeypatch.setattr(fa, "_foundry", model)
    with pytest.raises(TaskError, match="task_preparation_context_limit"):
        fa._generate_task_preparation(context)
    model.assert_not_called()


@pytest.mark.parametrize("violate_enum", [False, True])
def test_actual_sdk_constrained_quote_reaches_unchanged_local_check_and_receipt(monkeypatch, caplog, violate_enum):
    subject, repository, execute, _, publish, _ = service()
    source = repository.records[PATH]["text"]
    calls = []
    quoted = []

    def provider(request):
        packet = json.loads(request.content)
        calls.append(packet)
        schema = packet["response_format"]["json_schema"]["schema"]
        candidates = schema["properties"]["source_quote"]["enum"]
        assert len(json.dumps(packet, ensure_ascii=False)) <= 9000
        assert packet["response_format"]["json_schema"]["strict"] is True
        assert all(quote in source for quote in candidates)
        quote = "synthetic-unmatched-paraphrase" if violate_enum else candidates[-1]
        quoted.append(quote)
        result = {
            "summary": "A proposed outline draft.", "steps": ["Write one heading."],
            "uncertainties": [], "owner_next_action": "Review the proposed draft.",
            "source_quote": quote,
        }
        return httpx.Response(200, json={
            "id": "synthetic", "object": "chat.completion", "created": 1, "model": "existing-small-model",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps(result), "refusal": None,
            }}],
        })

    with httpx.Client(transport=httpx.MockTransport(provider)) as http:
        with OpenAI(
            api_key="synthetic-not-a-credential", base_url="https://synthetic.invalid/openai/v1/",
            http_client=http,
        ) as model:
            monkeypatch.setenv("MINDME_BRIEFING_MODEL", "existing-small-model")
            monkeypatch.setattr(fa, "_http_client", lambda: http)
            monkeypatch.setattr(fa, "_foundry", lambda: (None, model))
            subject.generate = fa._generate_task_preparation
            proposal = subject.prepare(prepare_payload(), TODAY)
            result = approve(subject, proposal)
            if violate_enum:
                assert result["status"] == "failed"
                assert result["result"]["error"] == "task_preparation_evidence_invalid"
                assert "preparation" not in result["result"]
            else:
                assert result["status"] == "completed"
                assert result["result"]["status"] == "prepared"
                assert result["result"]["preparation"]["source_quote"] == quoted[0]
            approve(subject, proposal)

    assert len(calls) == 1
    assert calls[0]["store"] is False
    assert calls[0]["max_completion_tokens"] == 1200 and "tools" not in calls[0]
    assert subject.store.read()["task_workspace"]["budget"]["daily_used"] == 1
    assert subject.store.read()["task_workspace"]["budget"]["used"] == 1
    assert "synthetic-unmatched-paraphrase" not in caplog.text
    execute.assert_not_called()
    publish.assert_not_called()


def test_no_quote_during_approved_preparation_retains_charge_without_provider_or_replay(monkeypatch):
    subject, repository, execute, _, publish, _ = service()
    repository.records[PATH]["text"] = "# Only a heading"
    model = Mock(side_effect=AssertionError("No citable evidence, no provider"))
    monkeypatch.setenv("MINDME_BRIEFING_MODEL", "existing-small-model")
    monkeypatch.setattr(fa, "_foundry", model)
    subject.generate = fa._generate_task_preparation
    proposal = subject.prepare(prepare_payload(), TODAY)
    result = approve(subject, proposal)
    assert result["status"] == "failed"
    assert result["result"]["error"] == "task_preparation_evidence_invalid"
    assert approve(subject, proposal)["status"] == "failed"
    assert subject.store.read()["task_workspace"]["budget"]["daily_used"] == 1
    model.assert_not_called()
    execute.assert_not_called()
    publish.assert_not_called()


@pytest.mark.parametrize("quote", [
    "Write a heading. Keep one topic.",
    "Write a heading.\\nKeep one topic.",
    "A rewritten source sentence.",
])
def test_local_validation_still_rejects_reconstructed_or_fuzzy_evidence(quote):
    output = {
        "summary": "A proposed draft.", "steps": [], "uncertainties": [],
        "owner_next_action": "Review it.", "source_quote": quote,
    }
    with pytest.raises(TaskError, match="task_preparation_evidence_invalid"):
        TaskService._preparation_result(output, {"text": "Write a heading.\r\nKeep one topic."})
