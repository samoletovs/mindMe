"""Elapsed-time regressions with synthetic clocks and offline transports."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import Context
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
import requests
from azure.core.exceptions import AzureError, ResourceModifiedError
from azure.core.pipeline.transport import HttpRequest, RequestsTransport
from azure.storage.blob import ContainerClient
from openai import APIConnectionError, APITimeoutError, OpenAI

import execution_budget as budget
import function_app as fa
from briefing_sources import _json
from briefing_actions import ActionGateway
from briefing_state import BriefingStore, StateError, empty_state
from evolve_loop import DailyEvolve
from test_briefing_loop import MemoryStore
from test_briefing_state import FakeBlob, store_for
from test_briefing_webhook import request
from test_vault_evolve import SOURCE, TODAY, context, generated


@pytest.fixture
def clock(monkeypatch) -> list[float]:
    now = [0.0]
    monkeypatch.setattr(budget.time, "monotonic", lambda: now[0])
    return now


def client_for(handler) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(handler),
        event_hooks={
            "request": [budget.http_request_hook],
            "response": [budget.http_response_hook],
        },
    )


@contextmanager
def local_response(*, drip: bool = False, delay: float = 0) -> Iterator[str]:
    finished = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.do_GET()

        def do_GET(self) -> None:
            body = b"x" * 40 if drip else json.dumps({
                "action_id": "a" * 32, "status": "submitted",
                "pr_url": "https://github.com/example/vault/pull/1",
            }).encode()
            try:
                time.sleep(delay)
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                for chunk in ([bytes([byte]) for byte in body] if drip else [body]):
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    if drip:
                        time.sleep(0.05)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                finished.set()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        finished.wait(3)
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("streaming", [False, True])
def test_real_drip_fed_sdk_response_cannot_overrun_the_elapsed_budget(streaming: bool) -> None:
    with local_response(drip=True) as url, budget.BudgetRequestsTransport() as transport:
        started = time.monotonic()
        with pytest.raises(budget.BudgetExceeded), budget.execution_budget(0.4):
            response = transport.send(HttpRequest("GET", url), stream=streaming)
            if streaming:
                budget.sdk_response_hook(SimpleNamespace(http_response=response))
                list(response.stream_download(None))
        assert time.monotonic() - started < 0.9


def test_real_delayed_writer_uses_its_response_allowance_not_one_quarter() -> None:
    with local_response(delay=9.2) as url, httpx.Client() as forward:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.extensions["timeout"]["read"] == 30
            return forward.post(url, content=request.content, timeout=httpx.Timeout(**request.extensions["timeout"]))

        with client_for(handler) as client, budget.execution_budget(35):
            gateway = ActionGateway(
                client=client, token="synthetic", repo="example/vault",
                memex_url="https://synthetic.example/api/personal_action", chat_id=7,
            )
            assert gateway.save_review("a" * 32, {}, "c" * 40)["status"] == "submitted"


def test_child_expiration_preserves_parent_time_for_release(clock) -> None:
    with budget.execution_budget(100):
        with pytest.raises(budget.BudgetExceeded, match="^execution_budget_exhausted$"):
            with budget.execution_budget(90, reserve=20):
                clock[0] = 80
                budget.checkpoint()
        assert budget.remaining_seconds() == 20
        budget.checkpoint()
    assert budget.remaining_seconds() is None


def test_nested_budget_cannot_extend_parent_or_affect_other_invocations(clock) -> None:
    with budget.execution_budget(10):
        with budget.execution_budget(100):
            assert budget.remaining_seconds() == 10
            assert Context().run(budget.remaining_seconds) is None
    assert budget.bounded_timeout(20) == 20


def test_http_timeouts_shrink_and_overrides_cannot_extend_deadline(clock) -> None:
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.extensions["timeout"])
        return httpx.Response(200, json={})

    with client_for(respond) as client, pytest.raises(budget.BudgetExceeded), budget.execution_budget(20):
        client.get("https://synthetic.example", timeout=600)
        clock[0] = 16
        client.get("https://synthetic.example", timeout=20)
        clock[0] = 20
        client.get("https://synthetic.example", timeout=None)
    assert len(calls) == 2
    assert calls[0] == {"connect": 1, "write": 1, "pool": 1, "read": 17}
    assert set(calls[1].values()) == {1}


class SlowStream(httpx.SyncByteStream):
    def __init__(self, clock: list[float]) -> None:
        self.clock = clock
        self.reads = 0
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        for chunk in (b"[", b"0", b"]"):
            self.reads += 1
            self.clock[0] += 3
            yield chunk

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("source_reader", [False, True])
def test_trickling_body_stops_before_another_chunk(clock, source_reader: bool) -> None:
    stream = SlowStream(clock)
    response = lambda request: httpx.Response(200, stream=stream)
    client = httpx.Client(transport=httpx.MockTransport(response)) if source_reader else client_for(response)
    with client, pytest.raises(budget.BudgetExceeded), budget.execution_budget(5):
        if source_reader:
            _json(client, "/synthetic", {})
        else:
            client.get("https://synthetic.example")
    assert stream.reads == 2
    assert stream.closed


def test_source_calls_stop_at_elapsed_deadline_even_without_shared_client_hooks(clock) -> None:
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, json={})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(budget.BudgetExceeded), budget.execution_budget(12):
            _json(client, "/synthetic", {})
            clock[0] = 8
            _json(client, "/synthetic", {})
            clock[0] = 12
            _json(client, "/synthetic", {})
    assert calls == [3, 1]


@pytest.mark.parametrize("generate", [fa._generate_action_plan, fa._generate_evolve_review])
def test_model_budget_disables_sdk_retries_and_shrinks_with_elapsed_time(clock, monkeypatch, generate) -> None:
    client = Mock()
    client.with_options.return_value = client
    http = Mock()
    monkeypatch.setattr(fa, "_http_client", lambda: http)
    client.responses.create.return_value.output_text = json.dumps({"findings": []})
    client.responses.create.return_value.output = []
    monkeypatch.setenv("MINDME_BRIEFING_MODEL", "synthetic")
    monkeypatch.setattr(fa, "_foundry", lambda: (None, client))
    with pytest.raises(budget.BudgetExceeded), budget.execution_budget(40):
        generate({"sources": []})
        clock[0] = 36
        generate({"sources": []})
        clock[0] = 40
        generate({"sources": []})
    assert [call.kwargs for call in client.with_options.call_args_list] == [
        {"timeout": 10, "max_retries": 0, "http_client": http},
        {"timeout": 1, "max_retries": 0, "http_client": http},
    ]
    assert client.responses.create.call_count == 2


def test_state_conflict_retries_do_not_start_after_deadline(clock) -> None:
    blob = FakeBlob(empty_state())
    store = store_for(blob)

    def conflict(payload: bytes, **kwargs) -> None:
        assert kwargs["retry_total"] == 0
        assert kwargs["connection_timeout"] <= 2
        assert kwargs["read_timeout"] <= 2
        clock[0] = 4
        raise ResourceModifiedError("synthetic private response")

    blob.upload_blob = conflict
    with pytest.raises(budget.BudgetExceeded), budget.execution_budget(4):
        store.update(lambda state: None)
    assert blob.reads == 1


def test_state_sdk_failure_is_safe_and_never_includes_remote_content(clock) -> None:
    blob = FakeBlob(empty_state())
    blob.read_error = AzureError("synthetic private blob payload")
    with budget.execution_budget(10), pytest.raises(StateError, match="^state_read_unavailable$") as error:
        store_for(blob).read()
    assert error.value.__suppress_context__


def test_state_download_and_upload_have_bounded_transport_and_zero_retries(clock) -> None:
    payload = json.dumps(empty_state()).encode()
    blob = Mock()
    blob.download_blob.return_value = SimpleNamespace(
        properties=SimpleNamespace(etag="synthetic", size=len(payload)),
        readall=lambda: payload,
    )
    container = Mock()
    container.get_blob_client.return_value = blob
    with budget.execution_budget(8):
        BriefingStore(container).update(lambda state: clock.__setitem__(0, 6))
    read = blob.download_blob.call_args.kwargs
    write = blob.upload_blob.call_args.kwargs
    assert read["read_timeout"] == read["connection_timeout"] == 4
    assert write["read_timeout"] == write["connection_timeout"] == 1
    for options in (read, write):
        assert all(options[name] == 0 for name in ("retry_total", "retry_connect", "retry_read", "retry_status"))
        assert options["raw_request_hook"] is budget.sdk_request_hook
        assert options["raw_response_hook"] is budget.sdk_response_hook


def test_sdk_stream_expiration_closes_response_without_reading_next_chunk(clock) -> None:
    stream = SlowStream(clock)
    raw = SimpleNamespace(
        stream_download=lambda pipeline: iter(stream),
        internal_response=SimpleNamespace(close=stream.close),
    )
    with pytest.raises(budget.BudgetExceeded), budget.execution_budget(5):
        budget.sdk_response_hook(SimpleNamespace(http_response=raw))
        list(raw.stream_download(None))
    assert stream.reads == 2
    assert stream.closed


@pytest.mark.parametrize("slow", [False, True])
def test_real_blob_sdk_respects_hooks_and_writable_stream_metadata(clock, slow: bool) -> None:
    payload = json.dumps(empty_state()).encode()
    reads = []

    def chunks(*args, **kwargs) -> Iterator[bytes]:
        for chunk in (payload[:10], payload[10:]):
            reads.append(1)
            clock[0] += 3 if slow else 0
            yield chunk

    response = requests.Response()
    response.status_code = 206
    response.headers.update({
        "Content-Length": str(len(payload)),
        "Content-Range": f"bytes 0-{len(payload) - 1}/{len(payload)}",
        "ETag": '"synthetic"', "x-ms-blob-type": "BlockBlob",
        "Last-Modified": "Mon, 14 Sep 2026 00:00:00 GMT",
    })
    response.raw = SimpleNamespace(stream=chunks, close=Mock())
    session = Mock()
    session.request.return_value = response
    container = ContainerClient(
        "https://synthetic.example", "private", credential=None,
        transport=RequestsTransport(session=session, session_owner=False),
    )
    if slow:
        with pytest.raises(budget.BudgetExceeded), budget.execution_budget(5):
            BriefingStore(container).read()
        response.raw.close.assert_called_once()
    else:
        with budget.execution_budget(5):
            assert BriefingStore(container).read() == empty_state()
    assert session.request.call_count == 1
    assert session.request.call_args.kwargs["timeout"] == (2.5, 2.5)
    assert len(reads) == 2


@pytest.mark.parametrize("companion", [False, True])
def test_real_model_sdk_does_not_retry_timeout_with_default_ten_minute_settings(clock, monkeypatch, companion: bool) -> None:
    calls = []

    def timeout(request: httpx.Request) -> httpx.Response:
        calls.append(request.extensions["timeout"])
        raise httpx.ReadTimeout("synthetic remote content", request=request)

    with client_for(timeout) as http:
        client = OpenAI(api_key="synthetic", base_url="https://synthetic.example", http_client=http)
        assert client.timeout.read == 600
        assert client.max_retries == 2
        monkeypatch.setenv("MINDME_BRIEFING_MODEL", "synthetic")
        monkeypatch.setattr(fa, "_foundry", lambda: (None, client))
        monkeypatch.setattr(fa, "_http_client", lambda: http)
        with budget.execution_budget(40), pytest.raises(APITimeoutError):
            if companion:
                fa._ask_companion("Synthetic briefing.")
            else:
                fa._generate_evolve_review({"sources": []})
    assert len(calls) == 1
    assert calls[0] == {"connect": 1, "write": 1, "pool": 1, "read": 10}


def test_real_model_body_cannot_keep_running_by_trickling_chunks(clock, monkeypatch) -> None:
    stream = SlowStream(clock)
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, stream=stream)

    with client_for(respond) as http:
        client = OpenAI(api_key="synthetic", base_url="https://synthetic.example", http_client=http)
        monkeypatch.setenv("MINDME_BRIEFING_MODEL", "synthetic")
        monkeypatch.setattr(fa, "_foundry", lambda: (None, client))
        monkeypatch.setattr(fa, "_http_client", lambda: http)
        with pytest.raises(APIConnectionError) as error, budget.execution_budget(5):
            fa._generate_evolve_review({"sources": []})
    assert isinstance(error.value.__cause__, budget.BudgetExceeded)
    assert len(calls) == 1
    assert stream.reads == 2
    assert stream.closed


def test_expired_original_briefing_leaves_independent_review_and_receipt_time(clock, monkeypatch) -> None:
    events = []
    monkeypatch.setenv("MINDME_DAILY_EVOLVE_ENABLED", "true")
    monkeypatch.setattr(fa, "_briefing_prefs", lambda: ["knowledge"])

    def original() -> None:
        events.append(("original", budget.remaining_seconds()))
        clock[0] = 75
        budget.checkpoint()

    def review(today) -> None:
        events.append(("review", budget.remaining_seconds()))
        clock[0] += 140
        budget.checkpoint()

    monkeypatch.setattr(fa, "_deliver_morning_briefing", original)
    monkeypatch.setattr(fa, "_evolve_loop", lambda: SimpleNamespace(run=review))
    with pytest.raises(RuntimeError, match="Morning delivery incomplete"):
        fa.morning_briefing_timer(Mock())
    assert events == [("original", 75), ("review", 170)]
    assert clock[0] < 270 < 300
    assert budget.remaining_seconds() is None


def test_on_demand_expiration_returns_retryable_failure_with_reserved_notice_time(clock, monkeypatch) -> None:
    events = []
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "7")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "synthetic")
    monkeypatch.setenv("MINDME_DAILY_EVOLVE_ENABLED", "true")

    def run(today, *, retry_delivery: bool) -> None:
        assert budget.remaining_seconds() == 150
        clock[0] = 150
        budget.checkpoint()

    monkeypatch.setattr(fa, "_evolve_loop", lambda: SimpleNamespace(run=run))
    monkeypatch.setattr(fa, "_telegram_send", lambda *args: events.append(budget.remaining_seconds()))
    response = fa.telegram_webhook(request({"message": {"chat": {"id": 7}, "text": "/evolve now"}}))
    assert response.status_code == 503
    assert events == [10]
    assert clock[0] < 180 < 230
    assert budget.remaining_seconds() is None


def test_original_companion_keeps_existing_client_outside_evolve_deadline(monkeypatch) -> None:
    client = Mock()
    client.responses.create.return_value.output_text = "Synthetic reply."
    monkeypatch.setattr(fa, "_foundry", lambda: (None, client))
    assert fa._ask_companion("Synthetic input.") == "Synthetic reply."
    client.with_options.assert_not_called()


def test_original_mirror_reads_use_bounded_sdk_options_only_with_active_deadline(clock, monkeypatch) -> None:
    blob = Mock()
    blob.download_blob.return_value.readall.return_value = b"synthetic"
    container = Mock()
    container.get_blob_client.return_value = blob
    monkeypatch.setattr(fa, "_os_container_client", lambda: container)
    path = "system/mindme/synthetic.json"
    assert fa._read_os_text(path) == "synthetic"
    assert blob.download_blob.call_args.kwargs == {}
    with budget.execution_budget(8):
        assert fa._read_os_text(path) == "synthetic"
    options = blob.download_blob.call_args.kwargs
    assert options["connection_timeout"] == options["read_timeout"] == 4
    assert options["retry_total"] == 0
    with pytest.raises(budget.BudgetExceeded), budget.execution_budget(8):
        clock[0] = 8
        fa._read_os_text(path)
    assert blob.download_blob.call_count == 2


def test_original_mirror_listing_does_not_fetch_another_page_after_expiration(clock, monkeypatch) -> None:
    fetched = []

    def pages() -> Iterator[SimpleNamespace]:
        for index in range(3):
            fetched.append(index)
            clock[0] += 3
            yield SimpleNamespace(name=f"system/mindme/{index}.json", last_modified=None)

    container = Mock()
    container.list_blobs.return_value = pages()
    monkeypatch.setattr(fa, "_os_container_client", lambda: container)
    monkeypatch.setattr(fa, "_current_mirror_inventory", lambda: SimpleNamespace(source_files=None))
    with pytest.raises(budget.BudgetExceeded), budget.execution_budget(5):
        fa._os_blob_props("system/mindme/")
    assert fetched == [0, 1]
    options = container.list_blobs.call_args.kwargs
    assert options["connection_timeout"] == options["read_timeout"] == 2.5
    assert options["retry_total"] == 0


@pytest.mark.parametrize("slow", [False, True])
def test_buffered_sdk_and_auth_bodies_are_consumed_with_deadline_checks(clock, slow: bool) -> None:
    reads = []

    def chunks(*args, **kwargs) -> Iterator[bytes]:
        for chunk in (b'{"value":', b"1}"):
            reads.append(1)
            clock[0] += 3 if slow else 0
            yield chunk

    response = requests.Response()
    response.status_code = 200
    response.headers["content-type"] = "application/json"
    response.raw = SimpleNamespace(stream=chunks, close=Mock())
    session = Mock()
    session.request.return_value = response
    transport = budget.BudgetRequestsTransport(session=session, session_owner=False)
    if slow:
        with pytest.raises(budget.BudgetExceeded), budget.execution_budget(5):
            transport.send(HttpRequest("GET", "https://synthetic.example"), stream=False)
        assert response.raw.close.called
    else:
        with budget.execution_budget(5):
            result = transport.send(HttpRequest("GET", "https://synthetic.example"), stream=False)
        assert result.text() == '{"value":1}'
    assert session.request.call_count == 1
    assert session.request.call_args.kwargs["timeout"] == (2.5, 2.5)
    assert session.request.call_args.kwargs["stream"] is True
    assert len(reads) == 2


def test_sdk_transport_preserves_unbudgeted_request_settings(monkeypatch) -> None:
    send = Mock(return_value=object())
    monkeypatch.setattr(RequestsTransport, "send", send)
    transport = budget.BudgetRequestsTransport()
    request = HttpRequest("GET", "https://synthetic.example")
    assert transport.send(request, stream=False, read_timeout=60) is send.return_value
    send.assert_called_once_with(request, stream=False, read_timeout=60)


@pytest.fixture
def evolving() -> tuple[DailyEvolve, MemoryStore]:
    store = MemoryStore()
    loop = DailyEvolve(
        store=store, sources=Mock(side_effect=lambda metadata: context()),
        generate=Mock(side_effect=lambda packet: generated()),
        publish=Mock(return_value={"status": "submitted", "pr_url": "https://github.com/example/mindVault/pull/1"}),
        revision=Mock(return_value=SOURCE["revision"]),
        send=Mock(side_effect=[101, 102]),
    )
    return loop, store


@pytest.mark.parametrize("phase", ["sources", "generate", "revision", "publish", "send"])
def test_review_phase_expiration_releases_claim_without_claiming_delivery(clock, evolving, phase: str) -> None:
    loop, store = evolving
    callback = getattr(loop, phase)
    value = {
        "sources": context(), "generate": generated(), "revision": SOURCE["revision"],
        "publish": {"status": "submitted"}, "send": 101,
    }[phase]

    def expire(*args):
        clock[0] += budget.remaining_seconds()
        return value

    callback.side_effect = expire
    with pytest.raises(budget.BudgetExceeded):
        loop.run(TODAY)
    record = store.state["deliveries"][TODAY.isoformat()]
    assert record["lease_until"] == 0
    assert record["status"] != "sent"
    assert store.state["last_delivered"] is None
    if phase in {"sources", "generate", "revision"}:
        loop.publish.assert_not_called()
    if phase != "send":
        loop.send.assert_not_called()
    else:
        assert record["inflight"] == 0
        assert record["message_ids"] == []
    assert clock[0] < 170
    assert budget.remaining_seconds() is None


def test_exhausted_work_budget_leaves_reserved_state_release_time(clock, evolving) -> None:
    loop, store = evolving
    original_update = store.update
    release_time = []

    def update(mutate):
        result = original_update(mutate)
        record = store.state["deliveries"].get(TODAY.isoformat(), {})
        if record.get("phase") == "prepared":
            if record["lease_until"] == 0:
                release_time.append(budget.remaining_seconds())
            else:
                clock[0] = 150
        return result

    store.update = update
    with pytest.raises(budget.BudgetExceeded):
        loop.run(TODAY)
    assert release_time == [15]
    assert store.state["deliveries"][TODAY.isoformat()]["lease_until"] == 0
    loop.publish.assert_not_called()
    loop.send.assert_not_called()


def test_claim_is_released_when_write_completed_but_budget_expires_before_return(clock, evolving) -> None:
    loop, store = evolving
    original_update = store.update
    writes = []

    def update(mutate):
        result = original_update(mutate)
        writes.append(budget.remaining_seconds())
        if len(writes) == 2:
            clock[0] = 150
            budget.checkpoint()
        return result

    store.update = update
    with pytest.raises(budget.BudgetExceeded):
        loop.run(TODAY)
    assert writes == [150, 150, 15]
    assert store.state["deliveries"][TODAY.isoformat()]["lease_until"] == 0
    loop.sources.assert_not_called()
    loop.generate.assert_not_called()
    loop.publish.assert_not_called()
    loop.send.assert_not_called()


def test_useful_review_finishes_after_slow_sources_model_checks_and_publication(clock, evolving) -> None:
    loop, store = evolving
    budgets = []

    def elapsed(seconds, result):
        def call(*args):
            budgets.append(budget.remaining_seconds())
            clock[0] += seconds
            return result
        return call

    loop.sources.side_effect = elapsed(40, context())
    loop.generate.side_effect = elapsed(30, generated())
    loop.revision.side_effect = elapsed(10, SOURCE["revision"])
    loop.publish.side_effect = elapsed(20, {
        "status": "submitted", "pr_url": "https://github.com/example/mindVault/pull/1",
    })
    identifiers = iter([101, 102])

    def send(*args):
        clock[0] += 3
        return next(identifiers)

    loop.send.side_effect = send
    assert loop.run(TODAY) == "Knowledge review sent. No proposed work was approved."
    assert budgets == [45, 65, 30, 35, 20]
    assert clock[0] == 116
    record = store.state["deliveries"][TODAY.isoformat()]
    assert record["phase"] == "complete"
    assert record["message_ids"] == [101, 102]
    assert record["lease_until"] == 0
    assert store.state["last_delivered"]["date"] == TODAY.isoformat()
