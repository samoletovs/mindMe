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
