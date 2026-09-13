from __future__ import annotations

from briefing_sources import _material


def test_current_dashboard_date_range_keeps_all_generic_approved_priorities():
    dashboard = """# Personal dashboard
Private detail stays in .me.
## North Star (2026)
Working draft: not confirmed.
## Approved focus - 2026-09-13 to 2026-10-13
| Goal | Evidence | Gap | Target |
|---|---|---|---|
| Complete the learning phase | Prior exam passed | Continue preparation | October |
| Clarify financial follow-through | Private checklist updated | Reconcile evidence | [Private task](tasks/2026-09-13-private-follow-through.md) |
| Establish a health and family routine | [Old baseline](areas/health/README.md) | Confirm available time | October |
## Historical goals
| Goal | Evidence |
|---|---|
| Abandoned goal | Old |
"""
    title, material = _material("home.md", dashboard, "goal", set())
    assert title == "Approved current focus"
    assert "Complete the learning phase" in material
    assert "Clarify financial follow-through" in material
    assert "Establish a health and family routine" in material
    assert "private-follow-through.md" not in material
    assert "areas/health" not in material
    assert "Abandoned goal" not in material
    assert ".me" not in material
