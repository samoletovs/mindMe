import pytest

from briefing_sources import SourceError
from task_sources import parse_task
from test_task_service import PATH, source_text


def test_overview_preserves_canonical_estimate_without_a_model_call():
    source = source_text().replace("type: task\n", "type: task\nestimate_min: 25\n")
    item = parse_task(source, PATH)
    assert item["estimate_min"] == 25 and item["ai_hint"] is None


@pytest.mark.parametrize("value", ["true", "-1", "10001", '"30"', "3.5"])
def test_invalid_canonical_estimates_are_explicit_not_coerced(value):
    source = source_text().replace("type: task\n", f"type: task\nestimate_min: {value}\n")
    with pytest.raises(SourceError, match="task_definition_invalid"):
        parse_task(source, PATH)


@pytest.mark.parametrize("value,minutes", [
    ("~45m", 45), ("~1h 20m", 80), ("1h", 60), ("90m", 90),
    ("30-45m", None), ("30–45m", None), ("~1h 75m", None),
    ("10001m", None), ("-5m", None), ("45m probably", None),
])
def test_overview_derives_only_unambiguous_persisted_context_estimates(value, minutes):
    raw = (
        source_text() + "\n## Context and boundaries\n\n"
        "Drafted wording and proposed steps, not verified facts or permission to act.\n\n"
        f"Estimate: {value}\n\n## Capture\n\n> Estimate: ~8000m\n"
    )
    assert parse_task(raw, PATH)["estimate_min"] == minutes


def test_overview_estimate_is_null_without_context_and_ignores_capture_or_code():
    assert parse_task(source_text(), PATH)["estimate_min"] is None
    raw = source_text() + "\n## Context and boundaries\n\n```text\nEstimate: ~45m\n```\n\n## Capture\n\nEstimate: ~60m\n"
    assert parse_task(raw, PATH)["estimate_min"] is None


def test_duplicate_context_estimates_are_not_guessed():
    raw = source_text() + "\n## Context and boundaries\n\nEstimate: ~45m\nEstimate: ~90m\n"
    assert parse_task(raw, PATH)["estimate_min"] is None


def test_explicit_canonical_estimate_takes_precedence_over_context():
    raw = source_text().replace("type: task\n", "type: task\nestimate_min: 25\n")
    raw += "\n## Context and boundaries\n\nEstimate: ~45m\n"
    assert parse_task(raw, PATH)["estimate_min"] == 25
