"""Cancellable HTTP behind the synchronous loader/authentication APIs.

HTTP tasks and bridge threads are always closed and joined. OS DNS cannot be
preempted safely, so request loops share a process-lifetime resolver with two
workers and two queued jobs; cancelled requests discard DNS results and never
continue sending HTTP. Loop shutdown never waits for an OS resolver.
"""

from __future__ import annotations

import atexit
import asyncio
import contextvars
import os
import queue
import signal
import socket
import threading
import time
from concurrent.futures import Future
from types import FrameType
from typing import Any


def _start_thread(worker: threading.Thread) -> None:
    """Keep SIGINT from leaving a spawned thread without a published identity."""
    if threading.current_thread() is not threading.main_thread():
        worker.start()
        return
    previous = signal.getsignal(signal.SIGINT)
    if not callable(previous):
        worker.start()
        return
    pending: list[tuple[int, FrameType | None]] = []

    def interrupted(signum: int, frame: FrameType | None) -> None:
        if worker.ident is None:
            pending.append((signum, frame))
        else:
            previous(signum, frame)

    signal.signal(signal.SIGINT, interrupted)
    try:
        worker.start()
    finally:
        signal.signal(signal.SIGINT, previous)
        for signum, frame in pending:
            previous(signum, frame)


def _join_thread(worker: threading.Thread, finished: threading.Event) -> None:
    # Event completion means HTTP/loop cleanup has finished. Only then join:
    # interrupted Thread.join() can corrupt CPython's liveness bookkeeping.
    while not finished.is_set():
        try:
            finished.wait(0.01)
        except KeyboardInterrupt:
            pass
    while worker.is_alive():
        try:
            worker.join()
        except KeyboardInterrupt:
            pass


class _Resolver:
    """Bound blocking OS DNS independently of request/loop lifetimes."""

    def __init__(self) -> None:
        self._closed = threading.Event()
        self._jobs: queue.Queue[tuple[Future[Any], tuple[Any, ...]]] = queue.Queue(maxsize=2)
        self._workers: list[tuple[threading.Thread, threading.Event]] = []
        try:
            for number in range(2):
                finished = threading.Event()
                worker = threading.Thread(target=self._work, args=(finished,), name=f"apcore-dns-{number}", daemon=True)
                self._workers.append((worker, finished))
                _start_thread(worker)
        except BaseException:
            # The instance has not been published globally yet. Failed startup
            # must retire every idle worker before a later attempt creates more.
            while True:
                try:
                    self.close()
                    for worker, finished in self._workers:
                        if worker.ident is not None:
                            _join_thread(worker, finished)
                    break
                except KeyboardInterrupt:
                    pass
            raise

    def close(self) -> None:
        self._closed.set()
        while True:
            try:
                future, _ = self._jobs.get_nowait()
            except queue.Empty:
                break
            future.cancel()
            self._jobs.task_done()

    def _work(self, finished: threading.Event) -> None:
        try:
            while not self._closed.is_set():
                try:
                    future, arguments = self._jobs.get(timeout=0.1)
                except queue.Empty:
                    continue
                try:
                    if future.set_running_or_notify_cancel():
                        try:
                            result = socket.getaddrinfo(*arguments)
                        except BaseException as exc:
                            future.set_exception(exc)
                        else:
                            future.set_result(result)
                finally:
                    self._jobs.task_done()
        finally:
            finished.set()

    async def getaddrinfo(
        self,
        host: Any,
        port: Any,
        *,
        family: int = 0,
        type: int = 0,
        proto: int = 0,
        flags: int = 0,
    ) -> Any:
        future: Future[Any] = Future()
        try:
            while True:
                if self._closed.is_set():
                    raise RuntimeError("HTTP resolver is closed")
                try:
                    self._jobs.put_nowait((future, (host, port, family, type, proto, flags)))
                    break
                except queue.Full:
                    # Waiting for resolver capacity is cancellable and consumes
                    # the same HTTP deadline as connection setup and body I/O.
                    await asyncio.sleep(0.01)
            return await asyncio.wrap_future(future)
        finally:
            # Queued jobs never start after cancellation. Running OS calls are
            # bounded by the fixed worker count; their results are discarded.
            future.cancel()


_resolver: _Resolver | None = None
_resolver_lock = threading.Lock()


def _shutdown_resolver() -> None:
    # Resolve the process's current pool at exit rather than capturing one:
    # a forked child must never close a parent's inherited, possibly locked pool.
    if _resolver is not None:
        _resolver.close()


atexit.register(_shutdown_resolver)


def _after_fork() -> None:
    # Daemon threads do not survive fork; a child needs its own process pool.
    global _resolver, _resolver_lock
    _resolver = None
    _resolver_lock = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def _new_loop() -> asyncio.AbstractEventLoop:
    loop = asyncio.new_event_loop()

    async def getaddrinfo(
        host: Any, port: Any, *, family: int = 0, type: int = 0, proto: int = 0, flags: int = 0
    ) -> Any:
        global _resolver
        with _resolver_lock:
            if _resolver is None:
                _resolver = _Resolver()
            resolver = _resolver
        return await resolver.getaddrinfo(host, port, family=family, type=type, proto=proto, flags=flags)

    # This is the public resolver hook on a loop owned exclusively by this
    # request. No caller loop or global asyncio policy is modified.
    loop.getaddrinfo = getaddrinfo  # type: ignore[method-assign]
    return loop


def request(method: str, url: str, *, timeout: float | None, **kwargs: Any) -> Any:
    """Perform a buffered request without abandoning HTTP work on interruption."""
    import httpx

    expires = None if timeout is None else time.monotonic() + timeout

    async def send() -> Any:
        try:
            remaining = None if expires is None else expires - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise TimeoutError
            async with asyncio.timeout(remaining):
                async with httpx.AsyncClient(timeout=remaining) as client:
                    response = await client.request(method, url, **kwargs)
                if expires is not None and time.monotonic() >= expires:
                    raise TimeoutError
                return response
        except TimeoutError as exc:
            raise httpx.TimeoutException(
                "HTTP request timed out" + (f" after {timeout:g}s" if timeout is not None else "")
            ) from exc

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        running_loop = False
    else:
        running_loop = True
    if not running_loop:
        with asyncio.Runner(loop_factory=_new_loop) as runner:
            return runner.run(send())

    caller = asyncio.current_task()
    initial_cancels = caller.cancelling() if caller is not None else 0
    cancelled = threading.Event()
    finished = threading.Event()
    outcome: Future[Any] = Future()

    async def tracked_send() -> Any:
        # The main thread only sets an Event. Cancellation is delivered once
        # here, avoiding an interruptible cross-thread scheduling/latch window.
        if cancelled.is_set():
            raise asyncio.CancelledError
        task = asyncio.create_task(send())
        try:
            while not task.done():
                if cancelled.is_set():
                    task.cancel()
                    return await task
                await asyncio.wait({task}, timeout=0.01)
            return await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def run() -> None:
        try:
            with asyncio.Runner(loop_factory=_new_loop) as runner:
                result = runner.run(tracked_send())
        except BaseException as exc:
            outcome.set_exception(exc)
        else:
            outcome.set_result(result)
        finally:
            finished.set()

    context = contextvars.copy_context()
    worker = threading.Thread(target=lambda: context.run(run), name="apcore-http")
    try:
        _start_thread(worker)
        while not finished.wait(0.01):
            # Runner's first SIGINT cancels its main task instead of raising
            # KeyboardInterrupt in this synchronous function. Do not reapply a
            # cancellation count left over from an already recovered exception.
            if caller is not None and caller.cancelling() > initial_cancels:
                raise asyncio.CancelledError
        worker.join()
        if caller is not None and caller.cancelling() > initial_cancels:
            raise asyncio.CancelledError
        return outcome.result()
    except BaseException:
        while True:
            try:
                # Event.set() itself can be interrupted; retry it together with
                # the wait instead of latching before cancellation is delivered.
                cancelled.set()
                if worker.ident is not None:
                    _join_thread(worker, finished)
                break
            except KeyboardInterrupt:
                pass
        raise
