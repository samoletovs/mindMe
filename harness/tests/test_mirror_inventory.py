"""A successful source inventory, not retained cloud blobs, defines the mirror."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from azure.functions import HttpRequest
from azure.core.exceptions import ResourceNotFoundError, ServiceRequestError

import function_app as app


@dataclass
class Mirror:
    blobs: dict[str, bytes]
    reads: list[str]
    client: Mock


_LEGACY = object()


def _manifest(
    source_files: object = _LEGACY, timestamp: str = "2026-09-06T07:00:00+00:00"
) -> bytes:
    document: dict[str, object] = {"synced_at_utc": timestamp}
    if source_files is not _LEGACY:
        document["source_files"] = source_files
    return json.dumps(document).encode()


@pytest.fixture
def mirror(monkeypatch: pytest.MonkeyPatch) -> Mirror:
    blobs: dict[str, bytes] = {}
    reads: list[str] = []
    container = Mock()

    def blob_client(name: str) -> Mock:
        client = Mock()

        def readall() -> bytes:
            reads.append(name)
            if name not in blobs:
                raise ResourceNotFoundError("Synthetic absent blob")
            return blobs[name]

        client.download_blob.return_value.readall.side_effect = readall
        client.upload_blob.side_effect = lambda data, **_: blobs.__setitem__(name, data)
        return client

    container.get_blob_client.side_effect = blob_client
    container.list_blobs.side_effect = lambda name_starts_with: [
        SimpleNamespace(
            name=name, last_modified=datetime(2026, 1, 1, tzinfo=timezone.utc)
        )
        for name in blobs
        if name.startswith(name_starts_with)
    ]

    class Today(date):
        @classmethod
        def today(cls) -> date:
            return cls(2026, 9, 6)

    monkeypatch.setattr(app, "date", Today)
    monkeypatch.setattr(app, "_os_container_client", lambda: container)
    monkeypatch.setattr(app, "_fetch_open_loops", app._empty_open_loops)
    return Mirror(blobs, reads, container)


def test_retained_deleted_files_do_not_feed_a_fresh_snapshot(mirror: Mirror) -> None:
    kept = [
        "projects/kept/README.md",
        "areas/kept/README.md",
        "inbox/2026-09-05-kept.md",
    ]
    retained = {
        "_dashboard.md": b"## Today\n- removed-dashboard",
        "journal/2026/2026-09-05.md": b"Mood: 9\nEnergy: 8\n- [ ] removed-journal",
        "projects/removed/README.md": b"# removed-project\nDue: 2026-09-07",
        "areas/removed/README.md": b"# removed-area",
        "inbox/2026-01-01-removed.md": b"removed-inbox",
        "reviews/2026-w35-weekly.md": b"removed-review",
    }
    mirror.blobs.update(retained)
    mirror.blobs.update({
        "_manifest.json": _manifest(kept),
        kept[0]: b"# kept-project\nDue: 2026-09-15",
        kept[1]: b"# kept-area",
        kept[2]: b"kept-inbox",
    })

    snapshot = app._build_briefing_snapshot()

    assert snapshot["today_focus"] == ""
    assert snapshot["yesterday"]["date"] is None
    assert snapshot["areas"] == ["kept-area"]
    assert snapshot["vault_state"]["projects"] == {
        "open_count": 1,
        "nearest_deadline": "2026-09-15",
        "nearest_project": "kept-project",
    }
    assert snapshot["vault_state"]["inbox"] == {"count": 1, "oldest_age_days": 1}
    assert snapshot["vault_state"]["reviews"]["last_weekly"] is None
    assert snapshot["source_freshness"]["status"] == "current"
    assert "removed" not in json.dumps(snapshot)
    assert not set(retained).intersection(mirror.reads)
    assert set(retained).issubset(mirror.blobs)
    assert mirror.reads.count("_manifest.json") == 1


def test_empty_inventory_hides_all_source_files_but_preserves_managed_state(
    mirror: Mirror,
) -> None:
    mirror.blobs.update({
        "_manifest.json": _manifest([]),
        "_dashboard.md": b"## Today\n- old dashboard",
        "areas/old/README.md": b"# Old area",
        "projects/old/README.md": b"# Old project",
        app._BRIEFING_PREFS_BLOB: b'{"sections": ["weather"]}',
        app._ONBOARDING_MARKER_BLOB: b"synthetic marker",
    })

    snapshot = app._build_briefing_snapshot()

    assert snapshot["today_focus"] == ""
    assert snapshot["areas"] == []
    assert snapshot["vault_state"]["projects"]["open_count"] == 0
    assert app._os_blob_props("projects/") == []
    assert app._briefing_prefs() == ["weather"]
    assert app._read_os_text(app._ONBOARDING_MARKER_BLOB) == "synthetic marker"
    assert app._save_briefing_prefs(["focus"])
    assert app._briefing_prefs() == ["focus"]


def test_legacy_inventory_can_read_but_cannot_claim_current(mirror: Mirror) -> None:
    mirror.blobs.update({
        "_manifest.json": _manifest(),
        "_dashboard.md": b"synthetic legacy dashboard",
    })

    assert app._read_os_text("_dashboard.md") == "synthetic legacy dashboard"
    freshness = app._mirror_freshness(date(2026, 9, 6))

    assert freshness["status"] == "unknown"
    assert freshness["inventory"] == "legacy"
    assert freshness["age_days"] == 0


def test_old_legacy_inventory_preserves_stale_age(mirror: Mirror) -> None:
    mirror.blobs["_manifest.json"] = _manifest(timestamp="2026-07-30T07:24:22+00:00")

    freshness = app._mirror_freshness(date(2026, 9, 6))

    assert freshness["status"] == "stale"
    assert freshness["age_days"] == 38
    assert freshness["inventory"] == "legacy"


@pytest.mark.parametrize(
    "inventory",
    [
        None, {}, "note.md", [3], ["../private.md"], ["/absolute.md"],
        ["a\\private.md"], ["a//private.md"], ["export.json"], ["same.md", "same.md"],
    ],
)
@pytest.mark.parametrize("reader", ["text", "properties", "areas"])
def test_invalid_inventory_never_broadens_source_visibility(
    mirror: Mirror, inventory: object, reader: str
) -> None:
    mirror.blobs.update({
        "_manifest.json": _manifest(inventory),
        "note.md": b"synthetic retained content",
    })

    with pytest.raises(ValueError, match="Invalid mirror source inventory"):
        if reader == "text":
            app._read_os_text("note.md")
        elif reader == "properties":
            app._os_blob_props("")
        else:
            app._list_area_h1s()

    assert mirror.reads == ["_manifest.json"]
    mirror.client.list_blobs.assert_not_called()


@pytest.mark.parametrize("raw", [b"[]", b"{bad"])
def test_invalid_manifest_fails_the_snapshot_without_reading_source(
    mirror: Mirror, raw: bytes
) -> None:
    mirror.blobs["_manifest.json"] = raw

    with pytest.raises(ValueError, match="Invalid mirror inventory manifest"):
        app._build_briefing_snapshot()

    assert mirror.reads == ["_manifest.json"]


def test_invalid_inventory_is_a_visible_tool_failure_not_an_empty_success(
    mirror: Mirror,
) -> None:
    mirror.blobs["_manifest.json"] = _manifest(None)

    response = app.tool_briefing_context(HttpRequest(
        method="POST", url="https://synthetic.invalid/tools/briefing_context", body=b"{}"
    ))

    assert response.status_code == 503
    assert mirror.reads == ["_manifest.json"]


def test_one_snapshot_pins_inventory_but_the_next_reads_the_new_inventory(
    mirror: Mirror,
) -> None:
    kept = ["_dashboard.md", "projects/kept/README.md", "areas/kept/README.md"]
    mirror.blobs.update({
        "_manifest.json": _manifest(kept),
        kept[0]: b"## Today\n- included focus",
        kept[1]: b"# included project",
        kept[2]: b"# included area",
    })
    original_client = mirror.client.get_blob_client.side_effect

    def changing_client(name: str) -> Mock:
        client = original_client(name)
        if name == "_dashboard.md":
            mirror.blobs["_manifest.json"] = _manifest([])
        return client

    mirror.client.get_blob_client.side_effect = changing_client

    first = app._build_briefing_snapshot()
    second = app._build_briefing_snapshot()

    assert first["today_focus"] == "included focus"
    assert first["vault_state"]["projects"]["open_count"] == 1
    assert first["areas"] == ["included area"]
    assert second["today_focus"] == ""
    assert second["vault_state"]["projects"]["open_count"] == 0
    assert second["areas"] == []
    assert mirror.reads.count("_manifest.json") == 2


def test_inventory_scope_is_reset_after_a_failed_snapshot(mirror: Mirror) -> None:
    mirror.blobs.update({
        "_manifest.json": _manifest(["_dashboard.md"]),
        "_dashboard.md": b"synthetic old source",
    })
    mirror.client.list_blobs.side_effect = ServiceRequestError("synthetic outage")

    with pytest.raises(ServiceRequestError):
        app._build_briefing_snapshot()

    mirror.blobs["_manifest.json"] = _manifest([])
    assert app._read_os_text("_dashboard.md") == ""
    assert mirror.reads.count("_manifest.json") == 2


def test_standalone_area_listing_reuses_one_inventory_for_all_reads(mirror: Mirror) -> None:
    names = [f"areas/area-{index}/README.md" for index in range(5)]
    mirror.blobs["_manifest.json"] = _manifest(names)
    mirror.blobs.update({name: f"# Area {index}".encode() for index, name in enumerate(names)})

    assert len(app._list_area_h1s()) == 5
    assert mirror.reads.count("_manifest.json") == 1


def test_standalone_project_listing_reuses_one_inventory_for_all_reads(
    mirror: Mirror,
) -> None:
    names = [f"projects/project-{index}/README.md" for index in range(5)]
    mirror.blobs["_manifest.json"] = _manifest(names)
    mirror.blobs.update({name: b"# Synthetic project" for name in names})

    assert app._projects_state(date(2026, 9, 6))["open_count"] == 5
    assert mirror.reads.count("_manifest.json") == 1


@dataclass
class TimerCalls:
    prefs: Mock
    companion: Mock
    fallback: Mock
    send: Mock


@pytest.fixture
def timer_calls(monkeypatch: pytest.MonkeyPatch) -> TimerCalls:
    calls = TimerCalls(
        prefs=Mock(return_value=["focus"]),
        companion=Mock(return_value="Synthetic briefing deliberately ignores freshness."),
        fallback=Mock(wraps=app._compose_local_briefing),
        send=Mock(),
    )
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "7")
    monkeypatch.setattr(app, "_briefing_prefs", calls.prefs)
    monkeypatch.setattr(app, "_ask_companion", calls.companion)
    monkeypatch.setattr(app, "_compose_local_briefing", calls.fallback)
    monkeypatch.setattr(app, "_telegram_send", calls.send)
    return calls


@pytest.mark.parametrize(
    ("manifest", "warning"),
    [
        (
            _manifest([], "2026-07-30T07:24:22+00:00"),
            "Personal context may be stale: mirror last synced 38d ago.",
        ),
        (_manifest(), "Personal context freshness is unknown."),
    ],
)
def test_timer_prepends_manifest_warning_even_when_companion_ignores_it(
    mirror: Mirror, timer_calls: TimerCalls, manifest: bytes, warning: str
) -> None:
    mirror.blobs["_manifest.json"] = manifest

    app.morning_briefing_timer(None)

    timer_calls.send.assert_called_once_with(
        7, warning + "\n\nSynthetic briefing deliberately ignores freshness."
    )
    assert mirror.reads == ["_manifest.json"]
    timer_calls.fallback.assert_not_called()


def test_sync_during_generation_cannot_remove_the_stale_warning(
    mirror: Mirror, timer_calls: TimerCalls,
) -> None:
    mirror.blobs["_manifest.json"] = _manifest([], "2026-07-30T07:24:22+00:00")

    def generate(seed: str) -> str:
        mirror.blobs["_manifest.json"] = _manifest([])
        return "Synthetic reply based on older context."

    timer_calls.companion.side_effect = generate
    app.morning_briefing_timer(None)

    timer_calls.send.assert_called_once_with(
        7,
        "Personal context may be stale: mirror last synced 38d ago.\n\n"
        "Synthetic reply based on older context.",
    )


@pytest.mark.parametrize("sections", [["weather"], []])
def test_timer_skips_metadata_and_personal_warning_without_personal_sections(
    mirror: Mirror, timer_calls: TimerCalls, sections: list[str]
) -> None:
    timer_calls.prefs.return_value = sections

    app.morning_briefing_timer(None)

    timer_calls.send.assert_called_once_with(7, timer_calls.companion.return_value)
    assert mirror.reads == []
    mirror.client.get_blob_client.assert_not_called()


def test_timer_does_not_add_freshness_warning_when_preferences_are_unreadable(
    mirror: Mirror, timer_calls: TimerCalls,
) -> None:
    timer_calls.prefs.side_effect = ValueError("synthetic invalid preferences")
    timer_calls.fallback.side_effect = ValueError("synthetic invalid preferences")

    app.morning_briefing_timer(None)

    reply = timer_calls.send.call_args.args[1]
    assert "No personal briefing was generated" in reply
    assert "freshness" not in reply
    assert mirror.reads == []
    timer_calls.companion.assert_not_called()


def test_timer_warns_unknown_and_logs_only_error_type_when_metadata_read_fails(
    mirror: Mirror, timer_calls: TimerCalls, caplog: pytest.LogCaptureFixture
) -> None:
    mirror.client.get_blob_client.side_effect = ServiceRequestError(
        "synthetic-private-response"
    )

    app.morning_briefing_timer(None)

    timer_calls.send.assert_called_once_with(
        7,
        "Personal context freshness is unknown.\n\n"
        "Synthetic briefing deliberately ignores freshness.",
    )
    assert "ServiceRequestError" in caplog.text
    assert "synthetic-private-response" not in caplog.text
    timer_calls.fallback.assert_not_called()


def test_timer_does_not_duplicate_the_local_fallback_warning(
    mirror: Mirror, timer_calls: TimerCalls, monkeypatch: pytest.MonkeyPatch
) -> None:
    mirror.blobs["_manifest.json"] = _manifest([], "2026-07-30T07:24:22+00:00")
    timer_calls.companion.side_effect = ServiceRequestError("synthetic model outage")
    monkeypatch.setattr(app, "_load_briefing", lambda: {
        "sections": ["focus"],
        "today_focus": "Synthetic focus",
        "source_freshness": {"status": "stale", "age_days": 38},
    })
    warning = "Personal context may be stale: mirror last synced 38d ago."

    app.morning_briefing_timer(None)

    reply = timer_calls.send.call_args.args[1]
    assert reply.startswith(warning + "\n\n")
    assert reply.count(warning) == 1
    assert "Synthetic focus" in reply
    timer_calls.fallback.assert_called_once()


def test_timer_leaves_current_briefing_without_a_warning(
    mirror: Mirror, timer_calls: TimerCalls,
) -> None:
    mirror.blobs["_manifest.json"] = _manifest([])

    app.morning_briefing_timer(None)

    timer_calls.send.assert_called_once_with(7, timer_calls.companion.return_value)
    assert mirror.reads == ["_manifest.json"]
