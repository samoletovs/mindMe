from __future__ import annotations

import pytest

from briefing_plan import PROPOSAL_ACTIONS, render_proposal
from telegram_format import html_reply, units
from test_morning_presentation import SOURCE_URL, TelegramHTML
from weekly_plan import render_weekly_proposal


def proposal(kind: str = "research") -> dict:
    return {
        "id": "a" * 24, "kind": kind,
        "text": "Compare <folders> & conversations before choosing a tool.",
        "why": "Test one small change before changing the whole workflow.",
        "source_path": "notes/example.md", "source_url": SOURCE_URL,
    }


def visible(parts: tuple[str, ...]) -> str:
    result = []
    for part in parts:
        assert units(part) <= 3900
        parsed = TelegramHTML()
        parsed.feed(part)
        parsed.close()
        assert not parsed.tags
        result.append("".join(parsed.text))
    return "\n\n".join(result)


@pytest.mark.parametrize("kind", PROPOSAL_ACTIONS)
def test_explanation_is_readable_without_exposing_ids_or_raw_urls(kind: str) -> None:
    item = proposal(kind)
    reply = render_proposal(item)
    text = visible(reply.parts)

    assert len(reply.parts) == 1
    assert item["text"] in text and item["why"] in text
    assert PROPOSAL_ACTIONS[kind][2] in text
    assert "<b>Why now</b>" in reply.parts[0]
    assert "<b>What approval means</b>" in reply.parts[0]
    assert f'<a href="{SOURCE_URL}">Open source</a>' in reply.parts[0]
    assert "https://" not in text and item["id"] not in text
    assert "No work starts before approval." in text
    assert "original proposal" in text
    assert ("When finished, reply done" in text) == (kind in {"review_task", "update_task"})


@pytest.mark.parametrize("url", [
    None, "javascript:alert(1)", 'https://github.com/example/vault/blob/main/x"><b>bad</b>',
    "https://github.com/example/vault/blob/main/x?token=hidden",
])
def test_invalid_sources_are_disclosed_without_clickable_untrusted_links(url: str | None) -> None:
    reply = render_proposal({**proposal(), "source_url": url})
    assert "Source link unavailable." in visible(reply.parts)
    assert "<a " not in "".join(reply.parts)


@pytest.mark.parametrize(("action", "why"), [
    ("&" * 700, "<" * 500),
    ("\U0001f642" * 300 + "&" * 400, "\U0001f9ed" * 500),
])
def test_full_explanation_survives_html_expansion_and_utf16_limits(action: str, why: str) -> None:
    reply = render_proposal({**proposal(), "text": action, "why": why})
    text = visible(reply.parts)

    assert len(reply.parts) > 1
    assert action in text and why in text
    assert "abbreviated" not in text
    assert PROPOSAL_ACTIONS["research"][2] in text
    assert "original proposal" in text


def test_source_html_is_visible_text_not_executable_markup() -> None:
    item = {
        **proposal(), "why": '<a href="https://untrusted.example">Claim</a> & <b>context</b>',
    }
    reply = render_proposal(item)
    assert item["why"] in visible(reply.parts)
    assert "".join(reply.parts).count("<a ") == 1


def test_single_card_omits_counter_but_weekly_choices_stay_numbered() -> None:
    single, keyboard = render_weekly_proposal(proposal(), 1, 1)
    weekly, _ = render_weekly_proposal(proposal(), 2, 3)

    assert single.startswith("<b>Research</b>\n\n")
    assert "1 of 1" not in single
    assert "2 of 3" in weekly
    assert "<b>Why now</b>" in single
    assert keyboard[0][0]["callback_data"] == "brief1|approve|" + "a" * 24


def test_section_packing_preserves_exact_boundary_and_does_not_split_markup() -> None:
    section = "<b>" + "x" * 3893 + "</b>"
    reply = html_reply([section, "<b>Next</b>"])
    assert reply.parts == (section, "<b>Next</b>")
    visible(reply.parts)


@pytest.mark.parametrize("sections", [[], [""], ["x" * 3901]])
def test_invalid_html_sections_fail_explicitly(sections: list[str]) -> None:
    with pytest.raises(ValueError, match="[Tt]elegram"):
        html_reply(sections)
