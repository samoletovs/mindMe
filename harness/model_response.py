"""Content-free reading of an OpenAI Responses API result.

A structured-output call can succeed at HTTP level and still carry no usable
answer: the reply may be cut off at the token cap, withheld by a content filter,
refused, or have failed on the service side. The SDK folds every one of those into
an empty or partial ``output_text``, so a JSON parse error alone cannot say which
happened.

Only fixed codes and counts are read here. Model text, refusal text and service
error messages can echo private packet content, so they are never read, kept or
logged (AGENTS.md rule 1).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")


def _code(value: Any) -> str:
    """A short snake_case service code, "other" if it is not one, "-" if absent."""
    if not isinstance(value, str):
        return "-"
    lowered = value.lower()
    return lowered if _CODE.fullmatch(lowered) else "other"


def _count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _shown(value: int | None) -> str:
    return "-" if value is None else str(value)


def _refused(response: Any) -> bool:
    output = getattr(response, "output", None)
    if not isinstance(output, (list, tuple)):
        return False
    for item in output:
        parts = getattr(item, "content", None)
        if (
            getattr(item, "type", None) == "message" and isinstance(parts, (list, tuple))
            and any(getattr(part, "type", None) == "refusal" for part in parts)
        ):
            return True
    return False


@dataclass(frozen=True)
class ResponseOutcome:
    status: str
    reason: str
    refused: bool
    input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None

    @property
    def defect(self) -> str | None:
        """Why the model's answer is unusable, or None when it signalled no problem.

        "truncated" covers any incomplete answer other than a content filter, since
        the cap is the usual cause. An unknown status is not a defect: callers still
        validate the text itself.
        """
        if self.refused:
            return "refused"
        if self.status == "incomplete":
            return "content_filtered" if self.reason == "content_filter" else "truncated"
        if self.status in {"failed", "cancelled"}:
            return "failed"
        return None

    def describe(self) -> str:
        return (
            f"status={self.status} reason={self.reason} refused={'yes' if self.refused else 'no'} "
            f"input_tokens={_shown(self.input_tokens)} output_tokens={_shown(self.output_tokens)} "
            f"reasoning_tokens={_shown(self.reasoning_tokens)}"
        )


def inspect_response(response: Any) -> ResponseOutcome:
    """Summarise a Responses API result without reading any model-written text.

    Unexpected shapes, such as the test doubles that stand in for the SDK, read as
    an unknown status with no counts rather than raising.
    """
    status = _code(getattr(response, "status", None))
    reason = _code(getattr(getattr(response, "incomplete_details", None), "reason", None))
    if reason == "-":
        reason = _code(getattr(getattr(response, "error", None), "code", None))
    usage = getattr(response, "usage", None)
    return ResponseOutcome(
        status="unknown" if status == "-" else status,
        reason=reason,
        refused=_refused(response),
        input_tokens=_count(getattr(usage, "input_tokens", None)),
        output_tokens=_count(getattr(usage, "output_tokens", None)),
        reasoning_tokens=_count(getattr(getattr(usage, "output_tokens_details", None), "reasoning_tokens", None)),
    )
