"""Regression coverage for cooperative HTTP cancellation and complete-body deadlines."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import textwrap
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from apcore import Registry
from apcore.cancel import CancelToken, ExecutionCancelledError
from apcore.errors import ModuleError

from apcore_toolkit.auth import (
    DeviceAuthConfig,
    DeviceCodeGrant,
    DeviceCodeResponse,
    HttpResponse,
    HttpxTransport,
    PreparedRequest,
    TransportError,
)
from apcore_toolkit.auth.errors import AuthorizationExpiredError
from apcore_toolkit.openapi_scanner import load_spec
from apcore_toolkit.output.http_proxy_writer import HTTPProxyRegistryWriter
from apcore_toolkit.types import ScannedModule


def _proxy(timeout: float = 1, auth: Any = None) -> Any:
    registry = Registry()
    scanned = ScannedModule(
        module_id="probe.get",
        description="probe",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        tags=[],
        target="probe:handler",
        metadata={"http_method": "GET", "url_path": "/probe"},
    )
    assert HTTPProxyRegistryWriter("https://example.test", auth, timeout).write([scanned], registry)[0].verified
    return registry.get("probe.get")


def test_proxy_rejects_pre_cancelled_context_without_auth_or_io(monkeypatch: pytest.MonkeyPatch) -> None:
    token = CancelToken()
    token.cancel()
    auth = MagicMock(return_value={})
    client = MagicMock()
    monkeypatch.setattr(httpx, "AsyncClient", client)
    with pytest.raises(ExecutionCancelledError):
        asyncio.run(_proxy(auth=auth).execute({}, SimpleNamespace(cancel_token=token)))
    auth.assert_not_called()
    client.assert_not_called()


def test_proxy_cancels_inflight_io_and_waits_for_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        started, cleaned = asyncio.Event(), asyncio.Event()

        async def request(*args: Any, **kwargs: Any) -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.set()

        client = SimpleNamespace(request=request)
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client)
        token = CancelToken()
        task = asyncio.create_task(_proxy().execute({}, SimpleNamespace(cancel_token=token)))
        await started.wait()
        token.cancel()
        with pytest.raises(ExecutionCancelledError):
            await asyncio.wait_for(task, 0.3)
        assert cleaned.is_set()
        assert len(asyncio.all_tasks()) == 1, "HTTP/cancellation helper tasks must be joined"

    asyncio.run(scenario())


def test_proxy_preserves_task_cancellation_and_joins_io(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        started, cleaned = asyncio.Event(), asyncio.Event()

        async def request(*args: Any, **kwargs: Any) -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: SimpleNamespace(request=request))
        token = CancelToken()
        task = asyncio.create_task(_proxy().execute({}, SimpleNamespace(cancel_token=token)))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned.is_set()
        assert len(asyncio.all_tasks()) == 1

    asyncio.run(scenario())


def test_proxy_does_not_accept_response_after_token_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    token = CancelToken()

    async def request(*args: Any, **kwargs: Any) -> httpx.Response:
        token.cancel()
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: SimpleNamespace(request=request))
    with pytest.raises(ExecutionCancelledError):
        asyncio.run(_proxy().execute({}, SimpleNamespace(cancel_token=token)))


def test_proxy_total_timeout_interrupts_dripping_response_and_closes_body(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        closed = asyncio.Event()

        class Drip(httpx.AsyncByteStream):
            async def __aiter__(self):
                for byte in b'{"ok":true}':
                    await asyncio.sleep(0.025)
                    yield bytes([byte])

            async def aclose(self) -> None:
                closed.set()

        client = httpx.AsyncClient(
            base_url="https://example.test",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Drip())),
        )
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client)
        try:
            with pytest.raises(ModuleError, match="timeout|timed out"):
                await _proxy(timeout=0.075).execute({})
            assert closed.is_set(), "timed-out response streams must be released"
            assert len(asyncio.all_tasks()) == 1
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.fixture
def slow_server():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            self.do_GET()

        def do_GET(self) -> None:
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                if self.path == "/stall":
                    time.sleep(0.7)
                    self.wfile.write(body)
                else:
                    for byte in body:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.07)
            except (OSError, ConnectionError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.mark.parametrize("path", ["/drip", "/stall"])
def test_openapi_url_timeout_covers_complete_body(slow_server: str, path: str) -> None:
    began = time.monotonic()
    with pytest.raises(httpx.TimeoutException):
        load_spec(slow_server + path, timeout=0.2)
    assert time.monotonic() - began < 0.6


def test_sync_openapi_loader_inside_running_loop_obeys_body_deadline(slow_server: str) -> None:
    async def scenario() -> None:
        before = {thread.ident for thread in threading.enumerate()}
        with pytest.raises(httpx.TimeoutException):
            load_spec(slow_server + "/drip", timeout=0.2)
        assert not [
            thread
            for thread in threading.enumerate()
            if thread.ident not in before and thread.name.startswith("apcore-http")
        ]

    asyncio.run(scenario())


@pytest.mark.parametrize("path", ["/drip", "/stall"])
def test_default_auth_transport_timeout_covers_complete_body(slow_server: str, path: str) -> None:
    request = PreparedRequest(kind="token", url=slow_server + path)
    began = time.monotonic()
    with pytest.raises(TransportError, match="timeout|timed out"):
        HttpxTransport(timeout=0.2).send(request)
    assert time.monotonic() - began < 0.6


def test_auth_transport_flow_budget_applies_even_without_configured_request_timeout(slow_server: str) -> None:
    request = PreparedRequest(kind="token", url=slow_server + "/drip", timeout_seconds=0.2)
    with pytest.raises(TransportError, match="timeout|timed out"):
        HttpxTransport().send(request)


def test_auth_transport_explicit_none_allows_unbounded_request(slow_server: str) -> None:
    assert HttpxTransport(timeout=None).send(PreparedRequest(kind="token", url=slow_server + "/drip")).is_success


def _grant(transport: Any, now: list[float], sleep: Any) -> DeviceCodeGrant:
    config = DeviceAuthConfig(
        client_id="probe",
        device_authorization_endpoint="https://e/device",
        token_endpoint="https://e/token",
        default_interval=1,
    )
    return DeviceCodeGrant(config, transport, clock=lambda: now[0], sleep=sleep)


def test_poll_deadline_rejects_late_success_from_custom_sync_transport() -> None:
    now = [0.0]

    class Transport:
        def send(self, request: PreparedRequest) -> HttpResponse:
            now[0] += 2
            return HttpResponse(200, json.dumps({"access_token": "late"}))

    grant = _grant(Transport(), now, lambda seconds: None)
    with pytest.raises(AuthorizationExpiredError) as exc:
        grant.poll_for_token(DeviceCodeResponse("dc", "uc", "https://e", interval=0), deadline=1)
    assert exc.value.reason == "deadline_exceeded"


def test_poll_sleep_is_clamped_to_remaining_flow_budget() -> None:
    now, slept = [0.0], []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    grant = _grant(SimpleNamespace(send=MagicMock()), now, sleep)
    with pytest.raises(AuthorizationExpiredError):
        grant.poll_for_token(DeviceCodeResponse("dc", "uc", "https://e", interval=5), deadline=0.2)
    assert slept == [0.2]


def test_poll_passes_remaining_budget_and_checks_after_on_poll() -> None:
    now = [0.0]
    observed = []

    class Transport:
        def send(self, request: PreparedRequest) -> HttpResponse:
            observed.append(request.timeout_seconds)
            return HttpResponse(200, '{"access_token":"ok"}')

    grant = _grant(Transport(), now, lambda seconds: now.__setitem__(0, now[0] + seconds))
    assert (
        grant.poll_for_token(DeviceCodeResponse("dc", "uc", "https://e", interval=1), deadline=3).access_token == "ok"
    )
    assert observed == [2]
    observed.clear()
    now[0] = 0

    def on_poll(**kwargs: Any) -> None:
        now[0] = 10

    with pytest.raises(AuthorizationExpiredError):
        grant.poll_for_token(DeviceCodeResponse("dc", "uc", "https://e", interval=0), deadline=3, on_poll=on_poll)
    assert observed == []


def test_default_poll_transport_stops_slow_body_at_flow_deadline(slow_server: str) -> None:
    config = DeviceAuthConfig(
        client_id="probe", device_authorization_endpoint=slow_server + "/device", token_endpoint=slow_server + "/drip"
    )
    grant = DeviceCodeGrant(config, HttpxTransport())
    began = time.monotonic()
    with pytest.raises(AuthorizationExpiredError) as exc:
        grant.poll_for_token(DeviceCodeResponse("dc", "uc", slow_server, interval=0), deadline=0.2)
    assert exc.value.reason == "deadline_exceeded"
    assert time.monotonic() - began < 0.6
    assert grant.polls_made == 1


def test_auth_client_injection_does_not_require_httpx_specific_timeout_attribute() -> None:
    client = SimpleNamespace(request=MagicMock(return_value=httpx.Response(200, json={"ok": True})))
    response = HttpxTransport(client).send(PreparedRequest("token", "https://e/token", timeout_seconds=1))
    assert response.is_success
    assert client.request.call_args.kwargs["timeout"] == 1


def test_auth_injected_client_keeps_shorter_stage_timeouts_and_ownership() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.extensions["timeout"] == {"connect": 0.01, "read": 0.2, "write": 0.2, "pool": 0.2}
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(handler), timeout=httpx.Timeout(1, connect=0.01)) as client:
        assert HttpxTransport(client).send(PreparedRequest("token", "https://e/token", timeout_seconds=0.2)).is_success
        assert not client.is_closed
        assert client.timeout.connect == 0.01
        assert client.timeout.read == 1


def test_auth_injected_sync_client_rejects_late_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        time.sleep(0.03)
        return httpx.Response(200, json={"access_token": "late"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TransportError, match="timed out"):
            HttpxTransport(client).send(PreparedRequest("token", "https://e/token", timeout_seconds=0.01))
        assert not client.is_closed


@pytest.mark.parametrize("callback_seconds", [1, 4])
def test_user_code_callback_time_counts_towards_flow_deadline(callback_seconds: float) -> None:
    now, sleeps, budgets, reported, elapsed = [0.0], [], [], [], []

    class Transport:
        def send(self, request: PreparedRequest) -> HttpResponse:
            if request.kind == "device":
                return HttpResponse(
                    200,
                    json.dumps(
                        {
                            "device_code": "dc",
                            "user_code": "uc",
                            "verification_uri": "https://e",
                            "expires_in": 10,
                            "interval": 1,
                        }
                    ),
                )
            budgets.append(request.timeout_seconds)
            return HttpResponse(200, '{"access_token":"ok"}')

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    def callback(**event: Any) -> None:
        reported.append(event["expires_in"])
        now[0] += callback_seconds

    grant = _grant(Transport(), now, sleep)
    if callback_seconds >= 3:
        with pytest.raises(AuthorizationExpiredError, match="deadline of 3s"):
            grant.authorize(timeout_seconds=3, on_user_code=callback)
        assert sleeps == []
        assert budgets == []
    else:
        tokens = grant.authorize(
            timeout_seconds=3,
            on_user_code=callback,
            on_poll=lambda **event: elapsed.append(event["elapsed"]),
        )
        assert tokens.access_token == "ok"
        assert sleeps == [1]
        assert budgets == [1]
        assert elapsed == [2]
    assert reported == [3]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX SIGINT subprocess behavior")
@pytest.mark.parametrize("signals", [1, 2])
def test_sync_auth_bridge_interrupt_cleans_http_worker(signals: int) -> None:
    code = textwrap.dedent("""
        import asyncio, threading, httpx
        from unittest.mock import patch
        from apcore_toolkit.auth import HttpxTransport, PreparedRequest
        class Client:
            def __init__(self, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args):
                await asyncio.sleep(0.15)
                print('CLIENT_CLOSED', flush=True)
            async def request(self, *args, **kwargs):
                print('READY', flush=True)
                try:
                    await asyncio.Event().wait()
                finally:
                    print('REQUEST_CLEANED', flush=True)
        async def main():
            HttpxTransport().send(PreparedRequest('token', 'https://probe.invalid/token'))
        try:
            with patch.object(httpx, 'AsyncClient', Client):
                asyncio.run(main())
        except KeyboardInterrupt:
            print('INTERRUPTED', flush=True)
        print('WORKERS', [t.name for t in threading.enumerate() if t.name.startswith('apcore-http')], flush=True)
    """)
    project = Path(__file__).resolve().parents[1]
    child = subprocess.Popen(
        [sys.executable, "-u", "-c", code],
        cwd=project,
        env={**os.environ, "PYTHONPATH": str(project / "src")},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "READY"
        child.send_signal(signal.SIGINT)
        if signals == 2:
            time.sleep(0.03)
            child.send_signal(signal.SIGINT)
        try:
            output, _ = child.communicate(timeout=1.5)
        except subprocess.TimeoutExpired:
            pytest.fail("SIGINT must cancel HTTP I/O, close its client, join its worker, and exit")
        assert child.returncode == 0, output
        assert "REQUEST_CLEANED" in output
        assert "CLIENT_CLOSED" in output
        assert "INTERRUPTED" in output
        assert "WORKERS []" in output
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate()


@pytest.mark.parametrize("inside_loop", [False, True])
@pytest.mark.parametrize("kind", ["openapi", "auth"])
def test_dns_resolution_counts_against_total_budget(
    monkeypatch: pytest.MonkeyPatch,
    inside_loop: bool,
    kind: str,
) -> None:
    gate = threading.Event()
    entered = threading.Event()

    def slow_dns(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        gate.wait(0.65)
        raise socket.gaierror("simulated slow resolver")

    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setattr(socket, "getaddrinfo", slow_dns)

    def call() -> None:
        began = time.monotonic()
        if kind == "openapi":
            with pytest.raises(httpx.TimeoutException):
                load_spec("http://probe.invalid/openapi.json", timeout=0.1)
        else:
            with pytest.raises(TransportError, match="timed out"):
                HttpxTransport(timeout=0.1).send(PreparedRequest("token", "http://probe.invalid/token"))
        assert time.monotonic() - began < 0.4, "shutdown must not wait for an OS resolver after HTTP timeout"

    async def caller() -> None:
        call()

    try:
        if inside_loop:
            asyncio.run(caller())
        else:
            call()
        assert entered.is_set(), "the test must reach DNS rather than only client initialization"
    finally:
        gate.set()
        time.sleep(0.02)


def test_dns_resources_are_bounded_and_cancelled_queries_never_send_http(monkeypatch: pytest.MonkeyPatch) -> None:
    from apcore_toolkit._http import request

    gate = threading.Event()
    lock = threading.Lock()
    counts = {"active": 0, "peak": 0, "started": 0, "http": 0}

    def dns(*args: Any, **kwargs: Any) -> Any:
        with lock:
            counts["active"] += 1
            counts["started"] += 1
            counts["peak"] = max(counts["peak"], counts["active"])
        try:
            gate.wait(0.7)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]
        finally:
            with lock:
                counts["active"] -= 1

    class Client:
        def __init__(self, **kwargs: Any):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: Any):
            pass

        async def request(self, *args: Any, **kwargs: Any):
            await asyncio.get_running_loop().getaddrinfo("probe.invalid", 80, type=socket.SOCK_STREAM)
            with lock:
                counts["http"] += 1
            return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    monkeypatch.setattr(httpx, "AsyncClient", Client)

    def caller() -> None:
        with pytest.raises(httpx.TimeoutException):
            request("GET", "http://probe.invalid/", timeout=0.1)

    timer = threading.Timer(0.7, gate.set)
    timer.start()
    try:
        with ThreadPoolExecutor(max_workers=8) as callers:
            list(callers.map(lambda _: caller(), range(8)))
        assert 1 <= counts["peak"] <= 2, "resolver workers must be shared and bounded across request loops"
        assert counts["started"] <= 2, "cancelled queued DNS must not run"
    finally:
        gate.set()
        timer.cancel()
        timer.join()
    for _ in range(100):
        if counts["active"] == 0:
            break
        time.sleep(0.01)
    assert counts["active"] == 0
    assert counts["http"] == 0, "late DNS completion cannot restart a cancelled HTTP request"


def test_sync_bridge_does_not_reapply_historical_task_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    from apcore_toolkit._http import request

    class Client:
        def __init__(self, **kwargs: Any):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: Any):
            pass

        async def request(self, *args: Any, **kwargs: Any):
            await asyncio.sleep(0)
            return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(httpx, "AsyncClient", Client)

    async def caller() -> None:
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            pass
        assert task.cancelling() == 1
        assert request("GET", "https://probe.invalid/", timeout=0.2).status_code == 200

    asyncio.run(caller())


@pytest.mark.parametrize("hook", ["transform", "parse"])
def test_token_hooks_cannot_spend_flow_budget_and_deliver_late_tokens(hook: str) -> None:
    now = [0.0]
    sent = []

    def transform(kind: str, params: dict[str, str], headers: dict[str, str]):
        if kind == "token" and hook == "transform":
            now[0] += 2
        return params, headers

    def parse(kind: str, status: int, content_type: str | None, body: str):
        if kind == "token" and hook == "parse":
            now[0] += 2
        return None

    config = DeviceAuthConfig(
        client_id="probe",
        device_authorization_endpoint="https://e/device",
        token_endpoint="https://e/token",
        transform_request=transform,
        parse_response=parse,
    )

    class Transport:
        def send(self, request: PreparedRequest) -> HttpResponse:
            sent.append(request)
            return HttpResponse(200, '{"access_token":"late"}')

    grant = DeviceCodeGrant(config, Transport(), clock=lambda: now[0], sleep=lambda seconds: None)
    with pytest.raises(AuthorizationExpiredError):
        grant.poll_for_token(DeviceCodeResponse("dc", "uc", "https://e", interval=0), deadline=1)
    assert len(sent) == (0 if hook == "transform" else 1)


def test_discovery_default_transport_obeys_configured_timeout(slow_server: str) -> None:
    from apcore_toolkit.auth.errors import DiscoveryError

    began = time.monotonic()
    config = DeviceAuthConfig(client_id="probe", issuer=slow_server, http_timeout=0.1)
    with pytest.raises(DiscoveryError):
        config.discover()
    assert time.monotonic() - began < 0.65, "each discovery candidate must enforce its configured request budget"


def test_proxy_timeout_also_covers_error_response_body(monkeypatch: pytest.MonkeyPatch) -> None:
    closed = []

    class Drip(httpx.AsyncByteStream):
        async def __aiter__(self):
            for byte in b'{"error":"slow"}':
                await asyncio.sleep(0.02)
                yield bytes([byte])

        async def aclose(self):
            closed.append(True)

    async def caller() -> None:
        async with httpx.AsyncClient(
            base_url="https://example.test",
            transport=httpx.MockTransport(lambda request: httpx.Response(500, stream=Drip())),
        ) as client:
            monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client)
            with pytest.raises(ModuleError, match="timed out"):
                await _proxy(timeout=0.06).execute({})
            assert closed == [True]

    asyncio.run(caller())


def test_sync_bridge_preserves_contextvars(monkeypatch: pytest.MonkeyPatch) -> None:
    import contextvars
    from apcore_toolkit._http import request

    trace_id = contextvars.ContextVar("http_trace", default="missing")

    class Client:
        def __init__(self, **kwargs: Any):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: Any):
            pass

        async def request(self, *args: Any, **kwargs: Any):
            return httpx.Response(200, json={"trace": trace_id.get()})

    monkeypatch.setattr(httpx, "AsyncClient", Client)

    async def caller() -> None:
        trace_id.set("request-123")
        assert request("GET", "https://probe.invalid/", timeout=0.2).json() == {"trace": "request-123"}

    asyncio.run(caller())


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX SIGINT subprocess behavior")
@pytest.mark.parametrize("window", ["start", "cancel", "pre_loop"])
def test_http_bridge_interrupt_windows_close_worker(window: str) -> None:
    code = textwrap.dedent("""
        import asyncio, threading, httpx, time, os
        from unittest.mock import patch
        import apcore_toolkit._http as helper
        window = os.environ['HTTP_WINDOW']
        ready_fd = int(os.environ['HTTP_READY_FD'])
        def signal_ready(marker): os.write(ready_fd, f'{marker}\\n'.encode())
        class Client:
            def __init__(self, **kwargs): print('CLIENT_CREATED', flush=True)
            async def __aenter__(self): return self
            async def __aexit__(self, *args): print('CLOSED', flush=True)
            async def request(self, *args, **kwargs):
                if window == 'cancel':
                    loop = asyncio.get_running_loop()
                    original = loop.call_soon_threadsafe
                    def protected(*args, **kwargs):
                        if threading.current_thread() is threading.main_thread():
                            raise AssertionError('main-thread cancellation scheduling is interruptible')
                        return original(*args, **kwargs)
                    loop.call_soon_threadsafe = protected
                    signal_ready('READY')
                try:
                    await asyncio.Event().wait()
                finally:
                    print('CLEANED', flush=True)
        original_start = threading.Thread.start
        def controlled_start(self):
            original_start(self)
            if self.name == 'apcore-http' and window == 'start':
                signal_ready('START_WINDOW')
                threading.Event().wait()
        original_loop = helper._new_loop
        def controlled_loop():
            if window == 'pre_loop':
                signal_ready('PRE_LOOP')
                time.sleep(0.15)
            return original_loop()
        async def main(): helper.request('GET', 'https://probe.invalid/', timeout=None)
        try:
            with patch.object(httpx, 'AsyncClient', Client), patch.object(threading.Thread, 'start', controlled_start), patch.object(helper, '_new_loop', controlled_loop):
                asyncio.run(main())
        except KeyboardInterrupt:
            print('INTERRUPTED', flush=True)
        print('WORKERS', [t.name for t in threading.enumerate() if t.name == 'apcore-http'], flush=True)
    """)
    project = Path(__file__).resolve().parents[1]
    ready_read, ready_write = os.pipe()
    try:
        child = subprocess.Popen(
            [sys.executable, "-u", "-c", code],
            cwd=project,
            env={
                **os.environ,
                "PYTHONPATH": str(project / "src"),
                "HTTP_WINDOW": window,
                "HTTP_READY_FD": str(ready_write),
            },
            pass_fds=(ready_write,),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except BaseException:
        os.close(ready_read)
        raise
    finally:
        os.close(ready_write)
    try:
        assert child.stdout is not None
        marker = {"start": "START_WINDOW", "cancel": "READY", "pre_loop": "PRE_LOOP"}[window]
        with os.fdopen(ready_read) as ready:
            observed = ready.readline().strip()
        assert observed == marker, "subprocess exited before entering the controlled interruption window"
        child.send_signal(signal.SIGINT)
        if window == "start":
            time.sleep(0.03)
            child.send_signal(signal.SIGINT)
        try:
            output, _ = child.communicate(timeout=1.5)
        except subprocess.TimeoutExpired:
            pytest.fail(f"HTTP worker must close after interruption in {window} window")
        assert child.returncode == 0, output
        assert "INTERRUPTED" in output
        assert "WORKERS []" in output
        if window != "pre_loop":
            assert "CLEANED" in output and "CLOSED" in output
        else:
            assert "CLIENT_CREATED" not in output, "cancellation before HTTP task startup must prevent I/O"
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate()


def test_resolver_failed_initialization_joins_workers_before_retry() -> None:
    code = textwrap.dedent("""
        import asyncio, threading, socket
        from unittest.mock import patch
        import apcore_toolkit._http as helper
        original_start = threading.Thread.start
        failed = False
        def start(self):
            global failed
            original_start(self)
            if self.name.startswith('apcore-dns-') and not failed:
                failed = True
                raise KeyboardInterrupt()
        async def resolve():
            return await asyncio.get_running_loop().getaddrinfo('probe.invalid', 80)
        with patch.object(threading.Thread, 'start', start), patch.object(socket, 'getaddrinfo', lambda *a, **kw: []):
            try:
                with asyncio.Runner(loop_factory=helper._new_loop) as runner: runner.run(resolve())
            except KeyboardInterrupt:
                pass
            assert helper._resolver is None
            assert not [t for t in threading.enumerate() if t.name.startswith('apcore-dns-')]
            with asyncio.Runner(loop_factory=helper._new_loop) as runner:
                assert runner.run(resolve()) == []
            assert len([t for t in threading.enumerate() if t.name.startswith('apcore-dns-')]) == 2
        print('BOUNDED', flush=True)
    """)
    project = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-u", "-c", code],
        cwd=project,
        env={**os.environ, "PYTHONPATH": str(project / "src")},
        capture_output=True,
        text=True,
        timeout=3,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "BOUNDED" in result.stdout


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX fork lifecycle")
def test_resolver_fork_uses_child_pool_and_avoids_inherited_queue_lock_at_exit() -> None:
    code = textwrap.dedent("""
        import asyncio, os, socket, sys
        from unittest.mock import patch
        import apcore_toolkit._http as helper
        async def resolve():
            return await asyncio.get_running_loop().getaddrinfo(None, '80', family=socket.AF_INET, type=socket.SOCK_STREAM, proto=6, flags=socket.AI_PASSIVE)
        def dns(*args):
            assert args == (None, '80', socket.AF_INET, socket.SOCK_STREAM, 6, socket.AI_PASSIVE)
            return []
        with patch.object(socket, 'getaddrinfo', dns):
            with asyncio.Runner(loop_factory=helper._new_loop) as runner: assert runner.run(resolve()) == []
            old = helper._resolver
            old._jobs.mutex.acquire()
            pid = os.fork()
            if pid == 0:
                assert helper._resolver is None
                with asyncio.Runner(loop_factory=helper._new_loop) as runner: assert runner.run(resolve()) == []
                print('CHILD_CLEAN', flush=True)
                sys.exit(0)
            old._jobs.mutex.release()
            _, status = os.waitpid(pid, 0)
            assert os.waitstatus_to_exitcode(status) == 0
        print('PARENT_CLEAN', flush=True)
    """)
    project = Path(__file__).resolve().parents[1]
    try:
        result = subprocess.run(
            [sys.executable, "-u", "-c", code],
            cwd=project,
            env={**os.environ, "PYTHONPATH": str(project / "src")},
            capture_output=True,
            text=True,
            timeout=3,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("forked child must not wait on the inherited resolver queue lock during exit")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CHILD_CLEAN" in result.stdout and "PARENT_CLEAN" in result.stdout
