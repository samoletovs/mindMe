"""Synthetic hosted dashboard contracts; no personal vault, identity or network."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import httpx
import pytest
from cryptography.fernet import Fernet

from briefing_plan import fingerprint
from briefing_sources import SourceError, _kind, _review_source_allowed, load_sources
from briefing_state import StateError, _encode
from dashboard_service import DashboardService
from dashboard_sources import (
    BYTE_LIMIT, READ_LIMIT, DashboardError, DashboardRepository, DisplaySnapshot, source_id,
    validate_review,
)
from evolve_feedback import ReviewFeedback
from evolve_loop import DailyEvolve
from task_state import workspace
from task_web import TaskWeb
from test_briefing_sources import HEAD, OLDER, OLDER_TREE, REPO, TOKEN, Vault, blob
from test_briefing_state import FakeBlob, store_for
from test_task_auth import identity
from test_task_service import TODAY, approve, service
from test_task_web import data, logged_in, request
from vault_evolve import EvolveError, evidence_packet

DAY = TODAY.isoformat()
NOTE = "wiki/sources/2026-10-08-synthetic-pilot.md"
DAILY = f"reviews/vault-evolve/{DAY}/review.json"
WEEKLY = "reviews/digests/2026-W41-digest.md"
QUOTE = "The pilot changed both the model and the procedure."
TEXT = f"---\ntype: source\ncaptured: {DAY}\n---\n# Synthetic pilot\n\n{QUOTE}\n"


@pytest.fixture(autouse=True)
def configure(monkeypatch):
    monkeypatch.setenv("DIG_REPO", REPO)


def receipt(raw=TEXT, path=NOTE):
    return {
        "version": 1, "as_of": DAY,
        "scope": {
            "question": "What did this pilot establish?", "coverage": "bounded",
            "personal_knowledge": "not_assessed", "limitations": ["One bounded selection."],
        },
        "sources": [{"id": "S1", "path": path, "sha256": hashlib.sha256(raw.encode()).hexdigest()}],
        "findings": [{
            "id": "F1", "kind": "evidence", "basis": "observed",
            "statement": "This comparison does not isolate the model change.",
            "evidence": [{"source": "S1", "quote": QUOTE}],
        }],
        "proposals": [{
            "id": "P1", "finding": "F1", "action": "experiment", "status": "proposed",
            "next_step": "Plan one controlled comparison.",
        }],
    }


def digest(link="../../" + NOTE):
    return (
        f"---\ntype: digest\nperiod: 2026-10-02 \u2026 {DAY}\ngenerated: {DAY}\n"
        "generated-by: vault-digest\nstatus: draft\n---\n# Synthetic weekly digest\n\n"
        "## This week\n\nThe notes discuss a pilot.\n\n"
        f"## What moved forward\n\n- [Pilot]({link}). A comparison to revisit.\n\n"
        "## Next actions to review\n\nConfirm checklist currency.\n\n"
        "<!-- digest-details -->\n\n## Full digest\n\n"
        f"- [Pilot]({link}) (new)\n\n"
        "### Coverage\n\n999 private/reference notes withheld.\n\n"
        "### Technical activity\n\nDetails not needed in the reader.\n"
    )


def repository(files=None, **kwargs):
    vault = Vault(files if files is not None else {NOTE: TEXT, DAILY: json.dumps(receipt())}, **kwargs)
    return DashboardRepository(vault.client, token=TOKEN, repo=REPO), vault


def dashboard(files=None):
    repo, vault = repository(files)
    tasks, _, execute, generate, publish, _ = service(capture_reader=repo.capture_source)
    feedback_blob = FakeBlob()
    feedback = ReviewFeedback(store=store_for(feedback_blob), revision=repo.evidence_revision)
    subject = DashboardService(
        tasks=tasks, repository=repo, feedback=lambda: feedback, cipher=Fernet(Fernet.generate_key()),
        clock=lambda: datetime(TODAY.year, TODAY.month, TODAY.day, 12, tzinfo=timezone.utc),
    )
    return subject, vault, feedback, execute, generate, publish


def selection(vault, path=DAILY):
    return {"id": source_id(path), "revision": blob(vault.files[path])}


def test_persisted_daily_schema_literal_evidence_reader_and_generation_exclusion():
    repo, vault = repository()
    result = repo.inbox()
    assert len(result["items"]) == 1 and result["issues"] == []
    review = result["items"][0]
    assert review["findings"][0]["evidence"][0]["quote"] == QUOTE
    assert review["as_of"] == DAY and review["status"] == "canonical"
    evidence = review["findings"][0]["evidence"][0]
    source = repo.read_display(evidence["id"], evidence["revision"])
    assert QUOTE in source["text"] and source["source_dates"] == {"captured": DAY}
    assert all(req.url.params["ref"] == HEAD for req in vault.reads)
    assert _kind(DAILY) is None
    assert not _review_source_allowed(DAILY, vault.files[DAILY], "review")
    generated = load_sources(vault.client, token=TOKEN, repo=REPO, sections=["knowledge"], include_evidence=True)
    assert {item["path"] for item in generated["sources"]} == {NOTE}


@pytest.mark.parametrize("change", [
    lambda review: review.update(version=2),
    lambda review: review.update(version=True),
    lambda review: review.update(as_of="2026-02-30"),
    lambda review: review.update(extra="not allowed"),
    lambda review: review["scope"].update(personal_knowledge="assessed"),
    lambda review: review["sources"][0].update(sha256="bad"),
    lambda review: review["sources"].append(copy.deepcopy(review["sources"][0])),
    lambda review: review["findings"][0].update(relationship="supports"),
    lambda review: review["findings"][0].update(kind="connection", relationship="supports"),
    lambda review: review["findings"][0]["evidence"][0].update(source="S99"),
    lambda review: review["proposals"][0].update(status="approved"),
    lambda review: review["proposals"][0].update(finding="F2"),
])
def test_unsupported_review_schema_is_not_a_plausible_empty_review(change):
    review = receipt()
    change(review)
    repo, _ = repository({NOTE: TEXT, DAILY: json.dumps(review)})
    result = repo.inbox()
    assert result["items"] == [] and result["issues"] == ["dashboard_schema_invalid"]
    assert result["partial"] is True


def test_duplicate_json_keys_are_rejected():
    raw = json.dumps(receipt()).replace('"version": 1', '"version": 1, "version": 1')
    with pytest.raises(DashboardError, match="dashboard_schema_invalid"):
        validate_review(raw, DAILY)


@pytest.mark.parametrize("field", ["statement", "next_step", "question", "limitation", "quote"])
def test_privacy_covers_every_review_field_not_only_source_body(field):
    review = receipt()
    private = "email: synthetic@example.invalid"
    if field == "statement":
        review["findings"][0]["statement"] = private
    elif field == "next_step":
        review["proposals"][0]["next_step"] = private
    elif field == "question":
        review["scope"]["question"] = private
    elif field == "limitation":
        review["scope"]["limitations"] = [private]
    else:
        review["findings"][0]["evidence"][0]["quote"] = private
    repo, _ = repository({NOTE: TEXT, DAILY: json.dumps(review)})
    result = repo.inbox()
    assert result["items"] == []
    assert private not in json.dumps(result) and NOTE not in json.dumps(result)


@pytest.mark.parametrize("raw", [
    TEXT.replace("type: source", "type: source\nignored: true"),
    TEXT.replace("type: source", "type: source\nclassification: confidential"),
    TEXT.replace("type: source", "type: source\nrouting: aibsVault"),
    TEXT.replace("type: source", "type: source\ntitle: synthetic@example.invalid"),
    TEXT + "\nPrivate contact: synthetic@example.invalid\n",
    TEXT.replace("# Synthetic pilot", "# Bank account comparison"),
    TEXT.replace("type: source", "type: source\ngenerated: true"),
    TEXT + "\nhttps://synthetic.example/?token=synthetic-credential\n",
])
def test_hidden_metadata_body_and_titles_do_not_leak_catalogue_or_dates(raw):
    repo, _ = repository({NOTE: raw, DAILY: json.dumps(receipt(raw))})
    result = repo.inbox()
    assert result["items"] == []
    assert "Synthetic pilot" not in json.dumps(result) and NOTE not in json.dumps(result)
    today = repo.today(TODAY, None)
    assert not today["items"]
    assert "candidate_count" not in today


def test_current_source_revision_and_exact_quotes_are_required():
    repo, vault = repository()
    vault.files[NOTE] = TEXT + "\nA new result.\n"
    for entry in vault.tree:
        if entry["path"] == NOTE:
            entry.update(sha=blob(vault.files[NOTE]), size=len(vault.files[NOTE].encode()))
    result = repo.inbox()
    assert result["items"] == [] and result["issues"] == ["dashboard_source_changed"]
    changed = receipt()
    changed["findings"][0]["evidence"][0]["quote"] = QUOTE + " Invented."
    repo, _ = repository({NOTE: TEXT, DAILY: json.dumps(changed)})
    assert repo.inbox()["issues"] == ["dashboard_source_changed"]


def test_missing_source_and_symlink_are_unavailable_before_contents_fetch():
    repo, vault = repository()
    next(entry for entry in vault.tree if entry["path"] == NOTE)["mode"] = "120000"
    assert repo.inbox()["issues"] == ["dashboard_source_unavailable"]
    assert all(NOTE not in req.url.path for req in vault.reads)
    with pytest.raises(DashboardError, match="dashboard_source_unavailable"):
        repo.read_display(source_id(NOTE), blob(TEXT))


@pytest.mark.parametrize("value", ["../../.me/one.md", "https://evil.example/x", "notes/one.md", [], None])
def test_reader_accepts_only_host_resolved_ids_not_caller_urls_paths_or_objects(value):
    repo, vault = repository()
    with pytest.raises(DashboardError, match="dashboard_request_invalid"):
        repo.read_display(value, blob(TEXT))
    assert not vault.requests


def test_contents_revision_mismatch_and_upstream_redirect_fail_closed():
    def mismatch(req):
        if "/contents/" in req.url.path:
            return httpx.Response(200, json={
                "path": DAILY, "type": "file", "encoding": "base64", "sha": "f" * 40, "content": "",
            })
    repo, _ = repository(override=mismatch)
    with pytest.raises(SourceError, match="source_snapshot_mismatch"):
        repo.inbox()
    repo, vault = repository(override=lambda req: httpx.Response(302, headers={"Location": "https://evil.example/"}))
    with pytest.raises(SourceError):
        repo.inbox()
    assert len(vault.requests) == 1


def test_weekly_digest_validates_relative_evidence_keeps_draft_and_removes_unfiltered_counts():
    repo, vault = repository({NOTE: TEXT, WEEKLY: digest()})
    result = repo.inbox()["items"][0]
    assert result["kind"] == "weekly_digest" and result["status"] == "canonical_draft"
    assert result["period_start"] == "2026-10-02" and result["as_of"] == DAY
    assert result["evidence"][0]["id"] == source_id(NOTE)
    assert "999" not in result["text"] and "Technical activity" not in result["text"]
    assert _kind(WEEKLY) is None
    assert all(req.url.params["ref"] == HEAD for req in vault.reads)


@pytest.mark.parametrize("target", [
    "../../areas/finances/account.md", "../../../../private.md",
    "https://user:credential@synthetic.example", "../../wiki/sources/missing.md",
    "../../wiki/sources/unsafe.md", "../../wiki/sources/%252e%252e/secret.md",
])
def test_digest_links_do_not_bypass_privacy_or_reveal_filtered_catalogues(target):
    repo, vault = repository({
        NOTE: TEXT, WEEKLY: digest(target),
        "wiki/sources/unsafe.md": "---\nignored: true\n---\n# Withheld identity\nA detail.",
    })
    result = repo.inbox()
    assert result["items"] == [] and result["issues"]
    assert "Withheld identity" not in json.dumps(result) and target not in json.dumps(result)
    assert all("finances" not in req.url.path and "/private" not in req.url.path for req in vault.reads)


def test_unsupported_personal_weekly_notes_are_not_in_the_display_allowlist():
    path = "reviews/2026-w41-weekly.md"
    repo, vault = repository({path: "# Private weekly note\nDo not read."})
    assert repo.inbox()["items"] == []
    assert not vault.reads


def test_older_digests_do_not_displace_recent_daily_findings_by_lexical_week_sort():
    files = {NOTE: TEXT, DAILY: json.dumps(receipt())}
    for week in range(1, 6):
        files[f"reviews/digests/2026-W{week:02}-digest.md"] = digest()
    repo, _ = repository(files)
    assert any(item["kind"] == "daily_review" for item in repo.inbox()["items"])


def test_long_digest_is_explicitly_bounded_after_validation():
    raw = digest().replace("Confirm checklist currency.", "A long synthetic explanation. " * 500)
    repo, _ = repository({NOTE: TEXT, WEEKLY: raw})
    result = repo.inbox()["items"][0]
    assert result["bounded"] is True and len(result["text"]) == 10_000


def test_focus_only_approved_safe_rows_with_draft_and_expired_window():
    home = """# Home
Private material stays in .me.
## North Star (2026)
Working draft: not confirmed.
## Approved focus - 2026-09-01 to 2026-09-30
| Goal | Next action |
|---|---|
| Practise a controlled comparison | Read one experiment |
| Financial follow-through | See [Private](areas/finances/account.md) |
## Proposed focus
| Goal | Next action |
|---|---|
| A speculative target | Not selected |
"""
    repo, _ = repository({"home.md": home})
    result = repo.today(TODAY, None)["focus"]
    assert result["draft_present"] is True
    assert len(result["items"]) == 1 and result["items"][0]["status"] == "expired"
    assert result["items"][0]["ends_on"] == "2026-09-30"
    assert ".me" not in json.dumps(result) and "Financial" not in json.dumps(result)
    assert "speculative" not in json.dumps(result)


def test_today_new_and_updated_are_pinned_tree_facts_not_filename_dates():
    older = [{
        "path": NOTE, "type": "blob", "mode": "100644", "sha": "f" * 40, "size": len(TEXT),
    }]
    def history(req):
        if req.url.path.endswith("/git/commits/" + OLDER):
            return httpx.Response(200, json={"sha": OLDER, "tree": {"sha": OLDER_TREE}})
        if req.url.path.endswith("/git/trees/" + OLDER_TREE):
            return httpx.Response(200, json={"truncated": False, "tree": older})
    other = "wiki/sources/2020-01-01-newly-added.md"
    repo, _ = repository({NOTE: TEXT, other: TEXT}, override=history)
    result = repo.today(TODAY, {"revision": OLDER, "observed_at": 1})
    assert {item["path"]: item["change"] for item in result["items"]} == {NOTE: "updated", other: "new"}
    assert result["first_visit"] is False
    initial = repo.today(TODAY, None)
    assert initial["first_visit"] and all(item["change"] == "initial" for item in initial["items"])


def test_today_page_limits_and_metadata_privacy_have_no_total_catalogue_counts():
    repo, vault = repository({f"wiki/sources/synthetic-{i:02}.md": TEXT for i in range(16)})
    result = repo.today(TODAY, None)
    assert len(result["items"]) == 12 and result["partial"] is True and result["next_offset"] == 12
    assert len(vault.reads) == 12
    assert "candidate_count" not in result and "inventory_paths" not in result
    assert len(repo.today(TODAY, None, 12)["items"]) == 4


def test_display_limits_fail_explicitly_and_oversized_files_are_not_fetched():
    path = "notes/large.md"
    repo, vault = repository({path: "x" * 64_001})
    with pytest.raises(DashboardError, match="dashboard_source_bounded"):
        repo.read_display(source_id(path), blob(vault.files[path]))
    assert not vault.reads
    repo, _ = repository({f"notes/source-{i:02}.md": TEXT for i in range(READ_LIMIT + 1)})
    snapshot = DisplaySnapshot(repo)
    for i in range(READ_LIMIT):
        snapshot.source(f"notes/source-{i:02}.md")
    with pytest.raises(DashboardError, match="dashboard_read_limit"):
        snapshot.source(f"notes/source-{READ_LIMIT:02}.md")
    assert snapshot.bytes <= BYTE_LIMIT


def test_source_title_cannot_expose_withheld_reference_identity():
    hidden_path = "wiki/sources/withheld-note.md"
    raw = TEXT.replace("# Synthetic pilot", f"# [WithheldIdentity]({hidden_path})")
    repo, _ = repository({
        NOTE: raw, hidden_path: "---\nignored: true\n---\n# WithheldIdentity\nExcluded.",
        DAILY: json.dumps(receipt(raw)),
    })
    result = repo.inbox()
    assert hidden_path not in json.dumps(result) and "WithheldIdentity" not in json.dumps(result)
    direct = repo.read_display(source_id(NOTE), blob(raw))
    assert hidden_path not in json.dumps(direct) and "WithheldIdentity" not in json.dumps(direct)


def test_reference_style_digest_cannot_bypass_source_validation():
    raw = digest().replace(f"[Pilot](../../{NOTE})", "[WithheldIdentity][ref]")
    raw += "\n[ref]: ../../wiki/sources/withheld-identity.md\n"
    repo, _ = repository({NOTE: TEXT, WEEKLY: raw})
    result = repo.inbox()
    assert result["items"] == [] and result["issues"] == ["dashboard_withheld"]


def test_reference_crossing_excerpt_boundary_is_filtered_before_truncation():
    path = "notes/long-excerpt.md"
    raw = "# Excerpt\n" + "x" * 9950 + "\n[WithheldIdentity](wiki/sources/withheld-note.md)\n" + "y" * 80
    repo, _ = repository({path: raw})
    result = repo.read_display(source_id(path), blob(raw))
    assert "WithheldIdentity" not in result["text"] and "wiki/sources/withheld" not in result["text"]
    assert result["bounded"] is True


def test_last_visit_is_single_bounded_private_marker_and_cas_safe():
    subject, *_ = dashboard()
    first = subject.today(0, TODAY)
    assert first["first_visit"] and "dashboard_visit" not in workspace(subject.tasks.store.read())
    assert subject.visit({"token": first["visit_token"]}) == {"recorded": True}
    assert subject.visit({"token": first["visit_token"]}) == {"recorded": True}
    saved = workspace(subject.tasks.store.read())["dashboard_visit"]
    assert set(saved) == {"revision", "observed_at"} and saved["revision"] == HEAD
    second = subject.today(0, TODAY)
    assert second["first_visit"] is False and second["items"] == []
    subject.tasks.store.update(lambda state: workspace(state)["dashboard_visit"].update(revision="e" * 40))
    with pytest.raises(DashboardError, match="dashboard_visit_conflict"):
        subject.visit({"token": second["visit_token"]})
    with pytest.raises(DashboardError, match="dashboard_visit_expired"):
        subject.visit({"token": "forged"})
    _encode(subject.tasks.store.read())


def test_expired_visit_has_explicit_fresh_baseline_not_false_new_changes():
    subject, *_ = dashboard()
    subject.tasks.store.update(lambda state: workspace(state).update(
        dashboard_visit={"revision": HEAD, "observed_at": int(subject.clock().timestamp()) - 36 * 86400},
    ))
    result = subject.today(0, TODAY)
    assert result["first_visit"] and result["baseline_expired"]
    assert all(item["change"] == "initial" for item in result["items"])
    with pytest.raises(StateError, match="invalid_dashboard_visit"):
        subject.tasks.store.update(lambda state: workspace(state).update(
            dashboard_visit={"revision": HEAD, "observed_at": 1, "private_text": "not allowed"},
        ))


def feedback_payload(vault, **changes):
    return {
        **selection(vault), "finding": "F1", "value": "useful", "version": fingerprint(None), **changes,
    }


def test_web_feedback_uses_same_store_logic_and_telegram_sees_it_without_approval():
    subject, vault, feedback, execute, generate, publish = dashboard()
    result = subject.feedback(feedback_payload(vault), TODAY)
    assert result["value"]["text"] == "Useful"
    assert subject.feedback(feedback_payload(vault), TODAY) == result
    record = feedback.store.read()["deliveries"][DAY]
    assert record["phase"] == "canonical" and record["message_ids"] == []
    assert feedback.feedback(DAY, "F1", "Already familiar", TODAY).startswith("Feedback saved")
    with pytest.raises(EvolveError, match="review_feedback_conflict"):
        subject.feedback(feedback_payload(vault), TODAY)
    public = subject.read(selection(vault), TODAY)
    assert public["feedback"]["findings"]["F1"]["value"]["text"] == "Already familiar"
    assert "_review" not in public and "_packet" not in public
    assert not subject.tasks.store.read()["proposals"]
    execute.assert_not_called()
    generate.assert_not_called()
    publish.assert_not_called()
    _encode(feedback.store.read())


def test_web_feedback_snooze_and_expiry_match_telegram_window():
    subject, vault, feedback, *_ = dashboard()
    for day in [DAY, (TODAY + timedelta(days=14)).isoformat(), "not-a-day"]:
        with pytest.raises(EvolveError, match="invalid_review_snooze"):
            subject.feedback(feedback_payload(vault, value="snooze", review_on=day), TODAY)
    result = subject.feedback(feedback_payload(vault, value="snooze", review_on=(TODAY + timedelta(days=2)).isoformat()), TODAY)
    assert result["value"]["review_on"] == (TODAY + timedelta(days=2)).isoformat()
    assert subject.read(selection(vault), TODAY + timedelta(days=14))["feedback"]["available"] is False
    with pytest.raises(EvolveError, match="review_feedback_expired"):
        subject.feedback(feedback_payload(vault), TODAY + timedelta(days=14))
    assert not feedback.store.read()["deliveries"]


def test_canonical_feedback_can_be_delivered_later_without_generating_or_publishing_again():
    subject, vault, feedback, *_ = dashboard()
    subject.feedback(feedback_payload(vault), TODAY)
    generate, publish, send = Mock(), Mock(), Mock(side_effect=[71, 72])
    loop = DailyEvolve(
        store=feedback.store, sources=Mock(), generate=generate, publish=publish, send=send,
        revision=subject.repository.evidence_revision,
    )
    assert loop.run(TODAY) == "Knowledge review sent. No proposed work was approved."
    generate.assert_not_called()
    publish.assert_not_called()
    assert send.call_count == 2
    assert loop.target(72) == (DAY, "F1")


def capture_payload(vault, **changes):
    return {
        **selection(vault), "finding": "F1", "request_id": "1" * 32,
        "text": "Plan one controlled comparison.",
        "definition": {"title": "Plan one controlled comparison"}, **changes,
    }


def test_make_task_is_preview_then_existing_explicit_confirmation_and_publication():
    subject, vault, _, execute, generate, publish = dashboard()
    preview = subject.capture(capture_payload(vault), TODAY)
    assert preview["status"] == "pending" and preview["source_status"] == "current"
    context = preview["action"]["definition"]["context"]
    assert NOTE in context and DAILY in context and QUOTE in context and blob(TEXT) in context
    assert preview["action"]["definition"]["stage"] == "clarify"
    execute.assert_not_called()
    generate.assert_not_called()
    assert subject.capture(capture_payload(vault), TODAY)["id"] == preview["id"]
    result = approve(subject.tasks, preview)
    assert result["status"] == "submitted" and execute.call_count == 1 and publish.call_count == 1
    approve(subject.tasks, preview)
    assert execute.call_count == 1
    assert publish.call_args_list[0].args[1] == publish.call_args_list[1].args[1]


def test_changed_evidence_blocks_preview_approval_and_redacts_cached_task_receipts():
    subject, vault, _, execute, _, publish = dashboard()
    preview = subject.capture(capture_payload(vault), TODAY)
    vault.files[NOTE] = TEXT + "\nChanged evidence.\n"
    next(item for item in vault.tree if item["path"] == NOTE).update(
        sha=blob(vault.files[NOTE]), size=len(vault.files[NOTE].encode()),
    )
    with pytest.raises(DashboardError, match="dashboard_source_changed"):
        subject.capture(capture_payload(vault), TODAY)
    result = approve(subject.tasks, preview)
    assert result["status"] == "invalidated" and "action" not in result
    assert NOTE not in json.dumps(subject.tasks.history(0, TODAY))
    execute.assert_not_called()
    publish.assert_not_called()


def test_completed_capture_does_not_restore_withheld_origin_evidence():
    subject, vault, _, _, _, publish = dashboard()
    preview = subject.capture(capture_payload(vault), TODAY)
    result_path = "tasks/synthetic-result.md"
    result_revision = "9" * 40
    subject.tasks.repository.records[result_path] = {"path": result_path, "revision": result_revision}
    publish.return_value = {"status": "merged", "path": result_path, "source_revision": result_revision}
    assert approve(subject.tasks, preview)["status"] == "completed"
    vault.tree = [item for item in vault.tree if item["path"] != NOTE]
    history = subject.tasks.history(0, TODAY)["items"][0]
    assert history["status"] == "completed" and "action" not in history
    assert QUOTE not in json.dumps(history)
    saved = subject.tasks.store.read()["proposals"][preview["id"]]
    assert saved["status"] == "completed" and saved["action_id"]


def test_evidence_change_after_atomic_claim_becomes_conflict_not_replayed_action():
    subject, vault, _, execute, _, publish = dashboard()
    preview = subject.capture(capture_payload(vault), TODAY)
    before = subject.tasks.loop.before_claim
    def claim(state, record, day):
        before(state, record, day)
        vault.tree = [entry for entry in vault.tree if entry["path"] != NOTE]
    subject.tasks.loop.before_claim = claim
    result = approve(subject.tasks, preview)
    assert result["status"] == "invalidated"
    execute.assert_not_called()
    publish.assert_not_called()


def test_automatic_reconciliation_cannot_publish_revoked_dashboard_evidence():
    from briefing_actions import ActionGateway
    from task_service import TaskService

    subject, vault, _, _, generate, _ = dashboard()
    packets, receipt_by_id = [], {}
    def writer(req):
        packet = json.loads(req.content)
        packets.append(packet)
        identifier = packet["action_id"]
        if packet["kind"] == "publish_task":
            if sum(item["kind"] == "publish_task" for item in packets) == 1:
                raise httpx.ConnectError("Synthetic lost publication response", request=req)
            return httpx.Response(202, json={
                **receipt_by_id[packet["target_action_id"]], "action_id": identifier,
                "target_action_id": packet["target_action_id"],
            })
        receipt_by_id[identifier] = {
            "version": 1, "action_id": identifier, "status": "submitted",
            "path": f"tasks/{DAY}-action-{identifier}.md", "source_revision": "8" * 40,
            "pr_url": "https://github.com/example/mindVault/pull/7",
        }
        return httpx.Response(202, json=receipt_by_id[identifier])
    gateway = ActionGateway(
        client=httpx.Client(transport=httpx.MockTransport(writer)), token="synthetic", repo=REPO,
        memex_url="https://synthetic.example/api/personal_action", chat_id=7, task_enabled=lambda: True,
    )
    subject.tasks.loop.execute = gateway
    subject.tasks = TaskService(
        loop=subject.tasks.loop, repository=subject.tasks.repository, generate=generate,
        publish=gateway.publish_task, attention_timezone=subject.tasks.attention_timezone,
        capture_reader=subject.repository.capture_source, clock=subject.tasks.clock,
    )
    preview = subject.capture(capture_payload(vault), TODAY)
    assert approve(subject.tasks, preview)["status"] == "uncertain"
    saved = subject.tasks.store.read()["proposals"][preview["id"]]
    vault.tree = [entry for entry in vault.tree if entry["path"] != NOTE]
    subject.tasks.loop.reconcile(TODAY)
    assert [packet["kind"] for packet in packets] == ["create_task", "publish_task"]
    after = subject.tasks.store.read()["proposals"][preview["id"]]
    assert after["status"] == "uncertain"
    assert after["action_id"] == saved["action_id"] and after["publication_action_id"] == saved["publication_action_id"]


def test_capture_provenance_cannot_be_supplied_by_the_browser():
    subject, vault, *_ = dashboard()
    with pytest.raises(DashboardError, match="dashboard_request_invalid"):
        subject.capture(capture_payload(vault, definition={"title": "Synthetic", "context": "Fake evidence"}), TODAY)
    with pytest.raises(DashboardError, match="dashboard_source_changed"):
        subject.capture(capture_payload(vault, revision="f" * 40), TODAY)


def test_new_dashboard_routes_retain_owner_csrf_origin_and_no_store(logged_in):
    auth, cookie, csrf = logged_in
    subject, vault, *_ = dashboard()
    factory = Mock(return_value=subject)
    web = TaskWeb(
        env={"MINDME_WEB_ENABLED": "true", "MINDME_TASKS_ENABLED": "true"},
        auth=lambda: auth, service=lambda: subject.tasks, dashboard=factory,
    )
    for path in ("api/dashboard/today", "api/dashboard/inbox"):
        denied = web.handle(request(path), path)
        assert denied.status_code == 401
    factory.assert_not_called()
    for path in ("api/dashboard/read", "api/dashboard/feedback", "api/dashboard/capture", "api/dashboard/visit"):
        denied = web.handle(request(path, {}, cookie=cookie, csrf="forged"), path)
        assert denied.status_code == 403
        denied = web.handle(request(path, {}, cookie=cookie, csrf=csrf, origin="https://evil.example"), path)
        assert denied.status_code == 403
    factory.assert_not_called()
    result = web.handle(request("api/dashboard/read", selection(vault), cookie=cookie, csrf=csrf), "api/dashboard/read")
    assert result.status_code == 200 and result.headers["Cache-Control"] == "no-store, private"
    assert data(result)["findings"][0]["evidence"][0]["quote"] == QUOTE
    assert "_packet" not in result.get_body().decode()


def test_feedback_persistence_failure_never_reports_success(logged_in):
    auth, cookie, csrf = logged_in
    subject, vault, feedback, *_ = dashboard()
    feedback.store.update = Mock(side_effect=StateError("state_save_failed"))
    web = TaskWeb(
        env={"MINDME_WEB_ENABLED": "true", "MINDME_TASKS_ENABLED": "true"},
        auth=lambda: auth, service=lambda: subject.tasks, dashboard=lambda *_: subject,
    )
    result = web.handle(request(
        "api/dashboard/feedback", feedback_payload(vault), cookie=cookie, csrf=csrf,
    ), "api/dashboard/feedback")
    assert result.status_code == 503 and "value" not in data(result)
