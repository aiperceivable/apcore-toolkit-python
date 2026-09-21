"""The transport seam: prepared requests in, decoded responses out.

This is the *transport* layer of the spec's layering rule — open to consumer
injection (proxies, mTLS, custom CA bundles, test doubles) because nothing
here carries protocol semantics.

It is also what makes the polling state machine conformance-testable without
HTTP mocking: a harness injects a :class:`Transport` that replays a scripted
response sequence, and the state machine above it is unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlencode

from apcore_toolkit.auth.errors import TransportError

#: Request kinds. Body encoding and the ``transform_request`` hook are both
#: keyed on these.
REQUEST_KINDS: tuple[str, ...] = ("device", "token", "refresh", "revoke")

#: Body encodings. ``form`` matches RFC 6749; ``json`` exists because at least
#: one vendor's *single* token endpoint takes form-encoded for the code
#: exchange and JSON for refresh.
ENCODINGS: frozenset[str] = frozenset({"form", "json"})


def encode_body(params: Mapping[str, str], encoding: str) -> tuple[str, str]:
    """Return ``(content_type, body)`` for ``params`` under ``encoding``."""
    if encoding == "json":
        return "application/json", json.dumps(dict(params))
    return "application/x-www-form-urlencoded", urlencode(dict(params))


@dataclass(frozen=True)
class PreparedRequest:
    """Everything an outbound request needs, with the body already encoded.

    Handed to the transport as a value so a test double can assert on it
    directly — which is how the corpus checks scope separators, client
    authentication placement, and per-kind encoding without a server.
    """

    kind: str
    url: str
    method: str = "POST"
    params: Mapping[str, str] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)
    encoding: str = "form"
    body: str = ""


@dataclass(frozen=True)
class HttpResponse:
    """A decoded HTTP response. ``status`` decides success-vs-error payload and nothing else."""

    status: int
    body: str
    content_type: str | None = None

    @property
    def is_success(self) -> bool:
        return 200 <= self.status < 300


class Transport(Protocol):
    """How bytes travel. Injectable; carries no protocol semantics."""

    def send(self, request: PreparedRequest) -> HttpResponse:
        """Perform ``request``, or raise :class:`TransportError`."""
        ...


class HttpxTransport:
    """The default transport, backed by ``httpx``.

    ``httpx`` is an optional dependency (``pip install apcore-toolkit[http-proxy]``)
    and is imported lazily at call time, matching how
    :class:`~apcore_toolkit.output.http_proxy_writer.HTTPProxyRegistryWriter`
    already does it: importing the package must not require it.

    Python's client is synchronous throughout, per the recorded decision on
    Open Question 1 — the shipped ``auth_header_factory`` seam is synchronous
    and the ``httpx`` path here already is too.
    """

    def __init__(self, client: Any = None, *, timeout: float | None = None) -> None:
        self._client = client
        self._timeout = timeout

    def send(self, request: PreparedRequest) -> HttpResponse:
        import httpx as _httpx

        client = self._client
        owns_client = client is None
        if owns_client:
            client = _httpx.Client(timeout=self._timeout)
        try:
            response = client.request(
                request.method,
                request.url,
                content=request.body.encode("utf-8") if request.body else None,
                headers=dict(request.headers),
            )
        except _httpx.HTTPError as exc:
            raise TransportError(f"{request.kind} request to {request.url} failed: {exc}") from exc
        finally:
            if owns_client:
                client.close()
        return HttpResponse(
            status=response.status_code,
            body=response.text,
            content_type=response.headers.get("content-type"),
        )
