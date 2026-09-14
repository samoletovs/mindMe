"""Cooperative elapsed-time limits for synchronous, bounded network work.

Deadlines are invocation-local, not mutable client settings. A child leaves its
parent's reserve available for receipts and lease release, even when it expires.
Network timeouts bound a blocked read; checkpoints also stop a trickling stream.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from threading import BoundedSemaphore
from typing import Any, TypeVar

import httpx
from azure.core.pipeline import PipelineRequest, PipelineResponse
from azure.core.pipeline.transport import HttpRequest, HttpResponse, RequestsTransport

_deadline: ContextVar[float | None] = ContextVar("execution_deadline", default=None)
_outcome: ContextVar[bool] = ContextVar("execution_outcome", default=False)
_io_worker: ContextVar[bool] = ContextVar("execution_io_worker", default=False)
_work_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mindme-io")
_work_slots = BoundedSemaphore(2)
_outcome_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mindme-outcome")
_outcome_slots = BoundedSemaphore(1)
_T = TypeVar("_T")


class BudgetExceeded(RuntimeError):
    """Only a fixed safe code; never attach network or source content."""

    def __init__(self) -> None:
        super().__init__("execution_budget_exhausted")


def remaining_seconds() -> float | None:
    deadline = _deadline.get()
    return None if deadline is None else max(0.0, deadline - time.monotonic())


def checkpoint() -> None:
    remaining = remaining_seconds()
    if remaining is not None and remaining <= 0:
        raise BudgetExceeded()


@contextmanager
def execution_budget(seconds: float, *, reserve: float = 0, outcome: bool = False) -> Iterator[None]:
    if seconds <= 0 or reserve < 0:
        raise ValueError("invalid_execution_budget")
    parent = _deadline.get()
    deadline = time.monotonic() + seconds
    if parent is not None:
        deadline = min(deadline, parent - reserve)
    token = _deadline.set(deadline)
    outcome_token = _outcome.set(_outcome.get() or outcome)
    try:
        checkpoint()
        yield
        checkpoint()
    finally:
        _deadline.reset(token)
        _outcome.reset(outcome_token)


def _sdk_call(operation: Callable[[], _T]) -> _T:
    """Bound one SDK operation, including buffering before its next checkpoint.

    Slots are capped with no unbounded queue. An issued mutation may finish
    remotely after timeout: receipts must reconcile it, not claim cancellation.
    """
    checkpoint()
    remaining = remaining_seconds()
    if remaining is None or _io_worker.get():
        return operation()
    pool, slots = (_outcome_pool, _outcome_slots) if _outcome.get() else (_work_pool, _work_slots)
    if not slots.acquire(blocking=False):
        raise BudgetExceeded()
    context = copy_context()

    def invoke() -> _T:
        token = _io_worker.set(True)
        try:
            checkpoint()
            return operation()
        finally:
            _io_worker.reset(token)

    try:
        future = pool.submit(context.run, invoke)
    except RuntimeError:
        slots.release()
        raise
    future.add_done_callback(lambda completed: slots.release())
    try:
        result = future.result(timeout=remaining)
    except FutureTimeoutError:
        if future.done():
            raise
        future.cancel()
        raise BudgetExceeded() from None
    checkpoint()
    return result


def bounded_timeout(cap: float, *, stages: int = 1) -> float:
    """Share the remaining time between sequential socket timeout stages."""
    checkpoint()
    remaining = remaining_seconds()
    return cap if remaining is None else min(cap, remaining / stages)


def http_request_hook(request: httpx.Request) -> None:
    if remaining_seconds() is None:
        return
    remaining = bounded_timeout(float("inf"))
    configured = request.extensions.get("timeout", {})
    setup = min(1.0, remaining / 4)
    timeouts = {}
    for stage in ("connect", "write", "pool"):
        timeouts[stage] = setup if configured.get(stage) is None else min(configured[stage], setup)
    read = remaining - sum(timeouts.values())
    timeouts["read"] = read if configured.get("read") is None else min(configured["read"], read)
    request.extensions["timeout"] = timeouts


class _BudgetStream(httpx.SyncByteStream):
    def __init__(self, stream: httpx.SyncByteStream) -> None:
        self._stream = stream

    def __iter__(self) -> Iterator[bytes]:
        chunks = iter(self._stream)
        while True:
            checkpoint()
            try:
                chunk = next(chunks)
            except StopIteration:
                return
            checkpoint()
            yield chunk

    def close(self) -> None:
        self._stream.close()


def http_response_hook(response: httpx.Response) -> None:
    if remaining_seconds() is None:
        return
    try:
        checkpoint()
    except BudgetExceeded:
        response.close()
        raise
    response.stream = _BudgetStream(response.stream)


def sdk_request_hook(request: PipelineRequest) -> None:
    request.context.options.update(
        connection_timeout=bounded_timeout(5.0, stages=2),
        read_timeout=bounded_timeout(5.0, stages=2),
    )


class _BudgetSdkStream:
    """Keep the SDK's writable stream metadata while checking every chunk."""

    def __init__(self, stream: Iterator[bytes], close: Callable[[], None]) -> None:
        self._original = stream
        self._stream = iter(stream)
        self._close = close

    def __getattr__(self, name: str) -> Any:
        return getattr(self._original, name)

    def __iter__(self) -> _BudgetSdkStream:
        return self

    def __next__(self) -> bytes:
        return _sdk_call(self._next_checked)

    def _next_checked(self) -> bytes:
        try:
            checkpoint()
            chunk = next(self._stream)
            checkpoint()
            return chunk
        except (BudgetExceeded, StopIteration):
            self._close()
            raise


class BudgetRequestsTransport(RequestsTransport):
    """Also check buffered SDK/auth bodies, before Azure deserializes them."""

    def send(self, request: HttpRequest, **kwargs: Any) -> HttpResponse:
        if remaining_seconds() is None:
            return super().send(request, **kwargs)
        return _sdk_call(lambda: self._send_checked(request, **kwargs))

    def _send_checked(self, request: HttpRequest, **kwargs: Any) -> HttpResponse:
        timeout = bounded_timeout(5.0, stages=2)
        for option in ("connection_timeout", "read_timeout"):
            configured = kwargs.get(option)
            kwargs[option] = timeout if configured is None else min(configured, timeout)
        streaming = kwargs.pop("stream", False)
        response = super().send(request, stream=True, **kwargs)
        try:
            checkpoint()
            if not streaming:
                body = b"".join(_BudgetSdkStream(
                    response.stream_download(None), response.internal_response.close,
                ))
                # Populate requests' normal buffer after deadline-checked consumption.
                response.internal_response._content = body
                response.internal_response._content_consumed = True
            return response
        except BudgetExceeded:
            response.internal_response.close()
            raise


def sdk_response_hook(response: PipelineResponse) -> None:
    if remaining_seconds() is None:
        return
    raw = response.http_response
    original = raw.stream_download

    def checked_stream(*args: Any, **kwargs: Any) -> _BudgetSdkStream:
        return _BudgetSdkStream(original(*args, **kwargs), raw.internal_response.close)

    raw.stream_download = checked_stream
    try:
        checkpoint()
    except BudgetExceeded:
        raw.internal_response.close()
        raise


def sdk_timeouts() -> dict[str, Any]:
    """Disable SDK retries and bound both synchronous transport timeout stages."""
    timeout = bounded_timeout(5.0, stages=2)
    return {
        "connection_timeout": timeout, "read_timeout": timeout,
        "retry_total": 0, "retry_connect": 0, "retry_read": 0, "retry_status": 0,
        "raw_request_hook": sdk_request_hook, "raw_response_hook": sdk_response_hook,
    }
