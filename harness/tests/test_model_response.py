"""A model answer can be unusable without any HTTP error; the inspector must say why."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from model_response import inspect_response

PRIVATE = "PRIVATE-PACKET-SENTENCE"


def response(
    *, status="completed", reason=None, refusal=False, error_code=None,
    usage=(1200, 300, 250), text=PRIVATE,
):
    return SimpleNamespace(
        status=status,
        output_text=text,
        incomplete_details=SimpleNamespace(reason=reason) if reason else None,
        error=SimpleNamespace(code=error_code, message=PRIVATE) if error_code else None,
        output=[SimpleNamespace(type="message", content=[SimpleNamespace(
            type="refusal" if refusal else "output_text", refusal=PRIVATE, text=PRIVATE,
        )])],
        usage=None if usage is None else SimpleNamespace(
            input_tokens=usage[0], output_tokens=usage[1],
            output_tokens_details=SimpleNamespace(reasoning_tokens=usage[2]),
        ),
    )


def test_a_completed_answer_has_no_defect_and_reports_token_counts():
    outcome = inspect_response(response())
    assert outcome.defect is None
    assert outcome.describe() == (
        "status=completed reason=- refused=no input_tokens=1200 output_tokens=300 reasoning_tokens=250"
    )


@pytest.mark.parametrize("reason", ["max_output_tokens", "something_new"])
def test_an_answer_cut_off_by_the_cap_is_truncated(reason):
    outcome = inspect_response(response(status="incomplete", reason=reason))
    assert outcome.defect == "truncated"
    assert f"status=incomplete reason={reason}" in outcome.describe()


def test_an_answer_withheld_by_the_content_filter_is_not_called_truncated():
    assert inspect_response(response(status="incomplete", reason="content_filter")).defect == "content_filtered"


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_a_failed_response_is_a_defect_with_the_service_error_code(status):
    outcome = inspect_response(response(status=status, error_code="server_error"))
    assert outcome.defect == "failed"
    assert "reason=server_error" in outcome.describe()


def test_a_refusal_is_a_defect_even_when_the_response_says_completed():
    outcome = inspect_response(response(refusal=True))
    assert outcome.refused and outcome.defect == "refused"
    assert "refused=yes" in outcome.describe()


def test_nothing_the_model_or_service_wrote_is_ever_described():
    for outcome in (
        inspect_response(response()),
        inspect_response(response(refusal=True)),
        inspect_response(response(status="failed", error_code="server_error")),
        inspect_response(response(status="incomplete", reason=PRIVATE)),
    ):
        assert PRIVATE not in outcome.describe()
        assert PRIVATE not in repr(outcome)


@pytest.mark.parametrize("reason", ["Free text, with spaces and a name", "x" * 80, "Quote: private"])
def test_a_reason_that_is_not_a_service_code_is_never_echoed(reason):
    outcome = inspect_response(response(status="incomplete", reason=reason))
    assert outcome.reason == "other"
    assert reason not in outcome.describe()


@pytest.mark.parametrize("usage", [None, SimpleNamespace(input_tokens=True, output_tokens=-1, output_tokens_details=None)])
def test_missing_or_invalid_usage_reads_as_unknown_counts(usage):
    answer = response()
    answer.usage = usage
    assert inspect_response(answer).describe() == (
        "status=completed reason=- refused=no input_tokens=- output_tokens=- reasoning_tokens=-"
    )


def test_test_doubles_read_as_an_unknown_status_instead_of_raising():
    for double in (Mock(), SimpleNamespace(output_text="{}"), None):
        outcome = inspect_response(double)
        assert outcome.status == "unknown"
        assert outcome.defect is None
