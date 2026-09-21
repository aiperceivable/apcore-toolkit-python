"""Conformance harness: assert Python's device-authorization client matches the
shared fixture corpus at ``apcore-toolkit/conformance/fixtures/device_auth.json``.

The TypeScript and Rust SDKs run the same 57 cases through their own state
machines and must produce identical answers. This is the cross-SDK behavioural
contract for RFC 8628 (see ``apcore-toolkit/docs/features/device-auth.md``).

**No HTTP mocking anywhere.** The state machine is pure over an injected
monotonic clock and a scripted response sequence, so this harness feeds
``token_responses`` in order through the ``Transport`` seam and records the
sleeps. Nothing patches ``httpx``; if it needed to, the injection seam would be
in the wrong place.

Harness conventions, which are the fixture's and are not guessable:

``poll_delays[i]``
    The sleep performed **before** ``token_responses[i]``. The first entry is
    therefore the initial wait, never ``0``.
``polls_made``
    How many scripted responses were actually *consumed*. A case scripting more
    responses than this is asserting the client **stopped** — so
    :class:`_ScriptedTransport` fails loudly rather than wrapping around.
``repeat_last_response``
    The final scripted response repeats indefinitely (case 023, whose 15-minute
    deadline would otherwise need 180 literal entries).
Wall clock
    Pinned to ``1000`` wherever a case asserts ``expires_at`` or
    ``obtained_at``. The *polling* clock stays monotonic and advances only by
    the sleeps.
``transport_error: true``
    A scripted connection failure rather than an HTTP response.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote_plus

import pytest

from apcore_toolkit.auth import (
    DeviceAuthClient,
    DeviceAuthConfig,
    DeviceCodeGrant,
    DiscoveryError,
    HttpResponse,
    MemoryTokenStore,
    PreparedRequest,
    TokenSet,
    TransportError,
    build_device_request,
    build_refresh_request,
    build_token_request,
    classify,
    decode_response,
    discovery_candidates,
)
from apcore_toolkit.auth.errors import (
    NO_METADATA,
    AuthorizationDeniedError,
    AuthorizationExpiredError,
    AuthorizationProtocolError,
    ConfigurationError,
)

_CONFORMANCE_DIR = Path(__file__).resolve().parent.parent.parent / "apcore-toolkit" / "conformance" / "fixtures"

#: Case kinds this harness dispatches on. A fixture case carrying anything else
#: would be parametrized into no test at all and pass vacuously.
_DISPATCHED_KINDS = frozenset(
    {
        "poll",
        "expiry",
        "refresh",
        "callback",
        "discovery_url",
        "discovery",
        "parse",
        "request",
        "redaction",
        "alias_validation",
        "hook",
    }
)

#: The corpus pins wall-clock time wherever it asserts an absolute instant.
_PINNED_WALL_CLOCK = 1000.0

_DEVICE_ENDPOINT = "https://e.example/device_authorization"
_TOKEN_ENDPOINT = "https://e.example/token"


def _load_fixture() -> list[dict[str, Any]]:
    path = _CONFORMANCE_DIR / "device_auth.json"
    if not path.exists():
        pytest.skip(f"conformance fixture not found at {path}", allow_module_level=True)
    data = json.loads(path.read_text(encoding="utf-8"))
    cases: list[dict[str, Any]] = data["test_cases"]
    return cases


_CASES = _load_fixture()


def _cases_of(kind: str) -> list[dict[str, Any]]:
    return [c for c in _CASES if c["kind"] == kind]


def _case_by_id(case_id: str) -> dict[str, Any]:
    for case in _CASES:
        if case["id"] == case_id:
            return case
    raise KeyError(case_id)


# --------------------------------------------------------------------------
# Harness doubles
# --------------------------------------------------------------------------


class _FakeClock:
    """A monotonic clock that advances only when the client sleeps.

    This is the whole reason the corpus needs no real time and no network: the
    state machine's only inputs are this clock and the scripted responses.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.delays: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.delays.append(seconds)
        self.now += seconds


def _as_response(entry: dict[str, Any]) -> HttpResponse:
    """Turn one scripted entry into a decoded HTTP response."""
    if "raw_body" in entry:
        return HttpResponse(
            status=int(entry.get("status", 200)),
            body=entry["raw_body"],
            content_type=entry.get("content_type"),
        )
    return HttpResponse(
        status=int(entry.get("status", 200)),
        body=json.dumps(entry.get("body", {})),
        content_type="application/json",
    )


class _ScriptedTransport:
    """Replays a scripted response sequence and records what was sent."""

    def __init__(
        self,
        *,
        device_response: dict[str, Any] | None = None,
        token_responses: list[dict[str, Any]] | None = None,
        repeat_last: bool = False,
        discovery_responses: list[dict[str, Any]] | None = None,
        stop_after_device_code: bool = False,
    ) -> None:
        self._device_response = device_response
        self._token_responses = list(token_responses or [])
        self._repeat_last = repeat_last
        self._discovery_responses = list(discovery_responses or [])
        self._stop_after_device_code = stop_after_device_code
        self.requests: list[PreparedRequest] = []
        self.polls_made = 0
        self.discovery_attempts = 0

    def send(self, request: PreparedRequest) -> HttpResponse:
        self.requests.append(request)
        if request.kind == "device":
            assert self._device_response is not None, "no device response scripted"
            return _as_response({"status": 200, "body": self._device_response})
        if request.kind == "discovery":
            self.discovery_attempts += 1
            index = self.discovery_attempts - 1
            if index >= len(self._discovery_responses):
                # Exhausted candidates behave like a provider that publishes
                # nothing at that path.
                return HttpResponse(status=404, body="", content_type=None)
            return _dispatch_entry(self._discovery_responses[index])
        # token / refresh / revoke all draw from the token script.
        index = self.polls_made
        if not self._token_responses and self._stop_after_device_code:
            raise _StopAfterDeviceCode
        if index >= len(self._token_responses):
            if self._repeat_last and self._token_responses:
                index = len(self._token_responses) - 1
            else:
                raise AssertionError(
                    f"the client made {index + 1} {request.kind} requests but only "
                    f"{len(self._token_responses)} were scripted — it polled past a terminal state"
                )
        self.polls_made += 1
        return _dispatch_entry(self._token_responses[index])


def _dispatch_entry(entry: dict[str, Any]) -> HttpResponse:
    if entry.get("transport_error"):
        raise TransportError("scripted connection failure")
    return _as_response(entry)


class _StopAfterDeviceCode(Exception):
    """Raised by the transport on the first token request.

    ``callback`` cases script only a device response: what they assert is the
    single ``on_user_code`` invocation, so the flow is stopped the moment it
    reaches the token endpoint. Deliberately not a ``TransportError`` — that
    would be retried — and deliberately not a scripted protocol response, which
    would invent behaviour the case does not specify.
    """


class _RecordingStore(MemoryTokenStore):
    """A memory store that records whether ``clear`` was called."""

    def __init__(self) -> None:
        super().__init__()
        self.cleared: list[str] = []

    def clear(self, key: str) -> None:
        self.cleared.append(key)
        super().clear(key)


def _config(**overrides: Any) -> DeviceAuthConfig:
    """A conforming baseline config; a case overrides only what it asserts."""
    params: dict[str, Any] = {
        "client_id": "cid",
        "device_authorization_endpoint": _DEVICE_ENDPOINT,
        "token_endpoint": _TOKEN_ENDPOINT,
    }
    params.update(overrides)
    return DeviceAuthConfig(**params)


@dataclass
class _PollResult:
    """Everything a ``poll`` case can assert, in one comparable value."""

    outcome: str
    poll_delays: list[float] = field(default_factory=list)
    final_interval: int | None = None
    polls_made: int = 0
    tokens: TokenSet | None = None


def _run_poll(case: dict[str, Any], *, hooks: dict[str, Any] | None = None) -> _PollResult:
    inp = case["input"]
    config = _config(**(inp.get("config") or {}), **(hooks or {}))
    transport = _ScriptedTransport(
        device_response=inp["device_response"],
        token_responses=inp["token_responses"],
        repeat_last=bool(inp.get("repeat_last_response")),
    )
    clock = _FakeClock()
    client = DeviceAuthClient(
        config,
        store=MemoryTokenStore(),
        transport=transport,
        clock=clock.monotonic,
        sleep=clock.sleep,
        wall_clock=lambda: _PINNED_WALL_CLOCK,
    )

    tokens: TokenSet | None = None
    try:
        tokens = client.login()
        outcome = "success"
    except AuthorizationDeniedError:
        outcome = "access_denied"
    except AuthorizationExpiredError as exc:
        outcome = exc.reason
    except AuthorizationProtocolError:
        outcome = "protocol_error"

    grant = client.grant
    assert isinstance(grant, DeviceCodeGrant)
    return _PollResult(
        outcome=outcome,
        poll_delays=list(clock.delays),
        final_interval=grant.final_interval,
        polls_made=transport.polls_made,
        tokens=tokens,
    )


def _assert_subset(expected: dict[str, Any], actual: dict[str, Any], case: dict[str, Any]) -> None:
    for key, value in expected.items():
        assert key in actual, f"\nCase {case['id']}: {case['description']}\nMissing key {key!r} in {actual!r}"
        assert actual[key] == value, (
            f"\nCase {case['id']}: {case['description']}\n"
            f"Key {key!r}\nExpected: {value!r}\nActual:   {actual[key]!r}"
        )


# --------------------------------------------------------------------------
# poll — the state machine
# --------------------------------------------------------------------------


@pytest.mark.parametrize("case", _cases_of("poll"), ids=lambda c: c["id"])
def test_device_auth_poll(case: dict[str, Any]) -> None:
    expected = case["expected"]
    result = _run_poll(case)
    context = f"\nCase {case['id']}: {case['description']}\n"

    assert (
        result.outcome == expected["outcome"]
    ), f"{context}Expected outcome: {expected['outcome']!r}\nActual outcome:   {result.outcome!r}"

    if "poll_delays" in expected:
        assert (
            result.poll_delays == expected["poll_delays"]
        ), f"{context}Expected delays: {expected['poll_delays']!r}\nActual delays:   {result.poll_delays!r}"
    if "poll_delays_length" in expected:
        assert (
            len(result.poll_delays) == expected["poll_delays_length"]
        ), f"{context}Expected {expected['poll_delays_length']} sleeps, got {len(result.poll_delays)}"
    if "poll_delays_all_equal" in expected:
        assert set(result.poll_delays) == {expected["poll_delays_all_equal"]}, (
            f"{context}Expected every sleep to be {expected['poll_delays_all_equal']}, "
            f"got {sorted(set(result.poll_delays))}"
        )
    if "final_interval" in expected:
        assert (
            result.final_interval == expected["final_interval"]
        ), f"{context}Expected final_interval {expected['final_interval']!r}, got {result.final_interval!r}"
    if "polls_made" in expected:
        assert (
            result.polls_made == expected["polls_made"]
        ), f"{context}Expected {expected['polls_made']} polls, got {result.polls_made}"
    if "token_set" in expected:
        assert result.tokens is not None, f"{context}expected a TokenSet but the flow did not succeed"
        _assert_subset(expected["token_set"], result.tokens.to_dict(), case)


# --------------------------------------------------------------------------
# expiry
# --------------------------------------------------------------------------


@pytest.mark.parametrize("case", _cases_of("expiry"), ids=lambda c: c["id"])
def test_device_auth_expiry(case: dict[str, Any]) -> None:
    inp = case["input"]
    tokens = TokenSet(access_token="t", expires_at=inp["token_set"]["expires_at"])
    actual = tokens.is_expired(inp["skew_seconds"], now=inp["now"])
    assert actual is case["expected"]["is_expired"], (
        f"\nCase {case['id']}: {case['description']}\n"
        f"expires_at={tokens.expires_at!r} now={inp['now']!r} skew={inp['skew_seconds']!r}\n"
        f"Expected: {case['expected']['is_expired']!r}\nActual:   {actual!r}"
    )


# --------------------------------------------------------------------------
# refresh
# --------------------------------------------------------------------------


@pytest.mark.parametrize("case", _cases_of("refresh"), ids=lambda c: c["id"])
def test_device_auth_refresh(case: dict[str, Any]) -> None:
    inp = case["input"]
    expected = case["expected"]
    # Case 059 carries `error_aliases`, which must NOT reach the refresh path.
    config = _config(**(inp.get("config") or {}))
    store = _RecordingStore()
    store.save(config.store_key, TokenSet.from_dict({"token_type": "Bearer", **inp["stored"]}))
    transport = _ScriptedTransport(token_responses=[inp["response"]])
    client = DeviceAuthClient(
        config,
        store=store,
        transport=transport,
        wall_clock=lambda: float(inp["now"]),
    )

    try:
        client.refresh()
        outcome = "success"
    except Exception as exc:  # noqa: BLE001 - the case's expected outcome is the assertion
        outcome = {"RefreshFailedError": "refresh_failed"}.get(type(exc).__name__, type(exc).__name__)

    context = f"\nCase {case['id']}: {case['description']}\n"
    assert outcome == expected["outcome"], f"{context}Expected {expected['outcome']!r}, got {outcome!r}"

    stored = store.load(config.store_key)
    if expected["stored"] is None:
        assert stored is None, f"{context}expected the store to be empty, found {stored!r}"
    else:
        assert stored is not None, f"{context}expected a stored credential"
        _assert_subset(expected["stored"], stored.to_dict(), case)
    if expected.get("store_cleared"):
        assert store.cleared == [config.store_key], f"{context}expected clear() to have been called"


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------


@pytest.mark.parametrize("case", _cases_of("discovery_url"), ids=lambda c: c["id"])
def test_device_auth_discovery_url(case: dict[str, Any]) -> None:
    candidates = discovery_candidates(case["input"]["issuer"])
    expected = case["expected"]
    context = f"\nCase {case['id']}: {case['description']}\n"
    if "candidates" in expected:
        assert (
            candidates == expected["candidates"]
        ), f"{context}Expected: {expected['candidates']!r}\nActual:   {candidates!r}"
    if "third_candidate" in expected:
        assert (
            candidates[2] == expected["third_candidate"]
        ), f"{context}Expected third candidate {expected['third_candidate']!r}, got {candidates[2]!r}"


@pytest.mark.parametrize("case", _cases_of("discovery"), ids=lambda c: c["id"])
def test_device_auth_discovery(case: dict[str, Any], caplog: pytest.LogCaptureFixture) -> None:
    inp = case["input"]
    expected = case["expected"]
    context = f"\nCase {case['id']}: {case['description']}\n"

    # A case supplies either a metadata document directly, or a scripted
    # response sequence when what it asserts is *which* candidate was used.
    if "metadata" in inp:
        responses = [
            {
                "status": 200,
                "content_type": "application/json",
                "raw_body": json.dumps(inp["metadata"]),
            }
        ]
    else:
        responses = inp["responses"]

    config = DeviceAuthConfig(client_id="cid", **inp["config"])
    transport = _ScriptedTransport(discovery_responses=responses)

    with caplog.at_level(logging.WARNING, logger="apcore_toolkit"):
        try:
            discovered = config.discover(transport=transport)
            accepted = True
            reason = None
        except DiscoveryError as exc:
            discovered = None
            accepted = False
            reason = exc.reason

    if expected.get("accepted") is False:
        assert not accepted, f"{context}expected the metadata document to be rejected"
        # Assert the REJECTION, not merely that discovery failed. Cases 054 and
        # 055 now ship complete metadata precisely so that an implementation
        # which normalises the issuer before comparing — and therefore accepts
        # the document, then falls over on something incidental — cannot pass:
        # any such failure surfaces as `no_metadata`, never as a refusal.
        assert reason != NO_METADATA, (
            f"{context}discovery ended with {reason!r} — it failed to FIND metadata rather than "
            f"refusing the document it found, so the security check never fired"
        )
        if "reason" in expected:
            assert (
                reason == expected["reason"]
            ), f"{context}Expected refusal reason {expected['reason']!r}, got {reason!r}"
        return

    assert accepted, f"{context}expected discovery to succeed"
    assert discovered is not None
    if "proceeds" in expected:
        assert expected["proceeds"] is True

    for endpoint in ("token_endpoint", "device_authorization_endpoint"):
        if endpoint in expected:
            actual = getattr(discovered, endpoint)
            assert actual == expected[endpoint], f"{context}Expected {endpoint} {expected[endpoint]!r}, got {actual!r}"
    if "candidate_used" in expected:
        assert transport.discovery_attempts == expected["candidate_used"], (
            f"{context}Expected candidate {expected['candidate_used']} to be the one used, "
            f"after {transport.discovery_attempts} attempts"
        )
    if "warned" in expected:
        # Any warning counts. Two distinct advisories reach this assertion —
        # unrecognised `grant_types_supported` (case 030) and a cross-origin
        # endpoint (case 057) — and both mean the same thing to a caller: the
        # client proceeded but had something to say about it.
        warned = bool(caplog.records)
        assert warned is expected["warned"], (
            f"{context}Expected warned={expected['warned']!r}; log records: "
            f"{[r.getMessage() for r in caplog.records]!r}"
        )


# --------------------------------------------------------------------------
# parse
# --------------------------------------------------------------------------


@pytest.mark.parametrize("case", _cases_of("parse"), ids=lambda c: c["id"])
def test_device_auth_parse(case: dict[str, Any]) -> None:
    inp = case["input"]
    expected = case["expected"]
    context = f"\nCase {case['id']}: {case['description']}\n"
    config = _config()
    response = HttpResponse(
        status=int(inp.get("status", 200)),
        body=inp["raw_body"],
        content_type=inp.get("content_type"),
    )

    if inp.get("as") == "error":
        grant = DeviceCodeGrant(config, _ScriptedTransport())
        with pytest.raises(AuthorizationProtocolError) as exc_info:
            grant._dispatch(response, 5)
        assert exc_info.value.raw_body == inp["raw_body"], (
            f"{context}the protocol error must carry the raw body verbatim; " f"got {exc_info.value.raw_body!r}"
        )
        assert expected["outcome"] == "protocol_error"
        return

    kind = "device" if inp.get("as") == "device" else "token"
    parsed = decode_response(config, kind, response)
    assert parsed is not None, f"{context}the body did not parse at all"
    _assert_subset(expected["parsed"], parsed, case)


# --------------------------------------------------------------------------
# request — body encoding and client-authentication placement
# --------------------------------------------------------------------------


def _prepare(kind: str, config: DeviceAuthConfig) -> PreparedRequest:
    if kind == "device":
        return build_device_request(config)
    if kind == "token":
        return build_token_request(config, "dc")
    if kind == "refresh":
        return build_refresh_request(config, "rt")
    raise AssertionError(f"unsupported request kind {kind!r}")


def _assert_basic_roundtrip(expected: dict[str, str], prepared: PreparedRequest, case: dict[str, Any]) -> None:
    """Decode the Basic header the way a server does, and compare the credentials.

    Deliberately NOT an exact-bytes comparison. The three SDKs' form encoders
    disagree on how space, ``*`` and ``~`` are spelled, and every one of those
    spellings decodes identically — so no server can tell them apart, and
    pinning one would force two SDKs to hand-roll an encoder for nothing. What
    must not vary is that encoding happens **at all**: raw concatenation turns
    the secret's ``+`` into a space, and the server then sees a different
    secret.
    """
    header = prepared.headers.get("Authorization")
    assert header and header.startswith("Basic "), f"\nCase {case['id']}: expected a Basic header, got {header!r}"
    decoded = base64.b64decode(header.removeprefix("Basic ")).decode()
    # Split on the FIRST colon: the *encoded* secret may contain %3A, but never
    # a literal ':' — which is exactly what the encoding buys.
    raw_id, separator, raw_secret = decoded.partition(":")
    assert separator, f"\nCase {case['id']}: the decoded credential pair has no ':' separator"
    actual = {"client_id": unquote_plus(raw_id), "client_secret": unquote_plus(raw_secret)}
    assert actual == expected, (
        f"\nCase {case['id']}: {case['description']}\n"
        f"Credentials did not survive the round trip.\nExpected: {expected!r}\nActual:   {actual!r}\n"
        f"(header was {header!r})"
    )


def _assert_form_roundtrip(expected: dict[str, str], prepared: PreparedRequest, case: dict[str, Any]) -> None:
    """Form-decode the encoded body; every listed parameter must be unchanged.

    A subset assertion, because a real token request necessarily also carries
    ``grant_type`` and ``device_code``, which the case does not enumerate.
    """
    assert prepared.encoding == "form", f"\nCase {case['id']}: expected a form-encoded body"
    decoded = dict(parse_qsl(prepared.body, keep_blank_values=True))
    _assert_subset(expected, decoded, case)


@pytest.mark.parametrize("case", _cases_of("request"), ids=lambda c: c["id"])
def test_device_auth_request(case: dict[str, Any]) -> None:
    inp = case["input"]
    expected = case["expected"]
    context = f"\nCase {case['id']}: {case['description']}\n"

    overrides = dict(inp["config"])
    params = dict(inp.get("params") or {})
    # ``input.params.scope`` is configuration, not a literal parameter: the
    # point of case 024 is how the configured *scope list* gets encoded.
    scope = params.pop("scope", None)
    if scope is not None:
        overrides["scope"] = list(scope)
    # Anything else in ``input.params`` is a literal body parameter (case 061's
    # reserved-character probes), which reaches the wire through the ``extra_*``
    # configuration for whichever request kind is built.
    if params:
        overrides["extra_device_params"] = dict(params)
        overrides["extra_token_params"] = dict(params)
    config = _config(**overrides)

    kinds = inp.get("kinds") or [inp["kind"]]
    for kind in kinds:
        prepared = _prepare(kind, config)
        if "body_contains" in expected:
            _assert_subset(expected["body_contains"], dict(prepared.params), case)
        for excluded in expected.get("body_excludes", ()):
            assert excluded not in prepared.params, f"{context}{kind}: body must not carry {excluded!r}"
        if "headers_contain" in expected:
            _assert_subset(expected["headers_contain"], dict(prepared.headers), case)
        for excluded in expected.get("headers_exclude", ()):
            assert excluded not in prepared.headers, f"{context}{kind}: headers must not carry {excluded!r}"
        if "encoding_by_kind" in expected and kind in expected["encoding_by_kind"]:
            assert prepared.encoding == expected["encoding_by_kind"][kind], (
                f"{context}{kind}: expected {expected['encoding_by_kind'][kind]!r} encoding, "
                f"got {prepared.encoding!r}"
            )
        if "basic_credentials_roundtrip" in expected:
            _assert_basic_roundtrip(expected["basic_credentials_roundtrip"], prepared, case)
        if "form_body_roundtrip" in expected:
            _assert_form_roundtrip(expected["form_body_roundtrip"], prepared, case)


# --------------------------------------------------------------------------
# callback — what the consumer is actually told
# --------------------------------------------------------------------------


@pytest.mark.parametrize("case", _cases_of("callback"), ids=lambda c: c["id"])
def test_device_auth_callback(case: dict[str, Any]) -> None:
    """``on_user_code`` fires once, with the deadline actually in force.

    Two folds produce that number and the callback must see both. Case 060:
    with ``expires_in`` absent the callback receives the 900 fallback, never
    null. Case 060c: with a caller ``timeout_seconds`` shorter than the
    server's value, it receives the timeout. A consumer rendering a countdown
    has to be told when the client will actually give up — anything else makes
    every consumer reimplement the arithmetic, which is precisely the per-CLI
    duplication the toolkit exists to remove.
    """
    inp = case["input"]
    expected = case["expected"]["on_user_code"]
    context = f"\nCase {case['id']}: {case['description']}\n"

    seen: list[dict[str, Any]] = []
    clock = _FakeClock()
    client = DeviceAuthClient(
        _config(**(inp.get("config") or {})),
        store=MemoryTokenStore(),
        transport=_ScriptedTransport(device_response=inp["device_response"], stop_after_device_code=True),
        clock=clock.monotonic,
        sleep=clock.sleep,
        wall_clock=lambda: _PINNED_WALL_CLOCK,
    )

    with pytest.raises(_StopAfterDeviceCode):
        client.login(
            on_user_code=lambda **event: seen.append(event),
            timeout_seconds=inp.get("timeout_seconds"),
        )

    assert len(seen) == 1, f"{context}on_user_code must fire exactly once, fired {len(seen)} times"
    assert seen[0] == expected, f"{context}Expected: {expected!r}\nActual:   {seen[0]!r}"


# --------------------------------------------------------------------------
# redaction
# --------------------------------------------------------------------------


@pytest.mark.parametrize("case", _cases_of("redaction"), ids=lambda c: c["id"])
def test_device_auth_redaction(case: dict[str, Any]) -> None:
    tokens = TokenSet.from_dict(case["input"]["token_set"])
    rendered = f"{tokens!r} {tokens!s} {tokens}"
    for secret in case["expected"]["must_not_contain"]:
        assert (
            secret not in rendered
        ), f"\nCase {case['id']}: {case['description']}\nDebug string leaked {secret!r}: {rendered}"


# --------------------------------------------------------------------------
# alias_validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize("case", _cases_of("alias_validation"), ids=lambda c: c["id"])
def test_device_auth_alias_validation(case: dict[str, Any]) -> None:
    aliases = case["input"]["error_aliases"]
    context = f"\nCase {case['id']}: {case['description']}\n"
    if case["expected"]["valid"]:
        config = _config(error_aliases=dict(aliases))
        assert config.error_aliases == aliases, f"{context}the alias table was not preserved"
    else:
        with pytest.raises(ConfigurationError):
            _config(error_aliases=dict(aliases))


# --------------------------------------------------------------------------
# hook contracts
# --------------------------------------------------------------------------


def test_hook_043_transform_request() -> None:
    case = _case_by_id("device_auth_hook_043_transform_request")
    returns = case["input"]["returns"]

    def transform(kind: str, params: dict[str, str], headers: dict[str, str]) -> tuple[dict, dict]:
        return {**params, **returns["params"]}, {**headers, **returns["headers"]}

    prepared = build_token_request(_config(transform_request=transform), "dc")
    _assert_subset(case["expected"]["body_contains"], dict(prepared.params), case)
    _assert_subset(case["expected"]["headers_contain"], dict(prepared.headers), case)


def test_hook_044_parse_response_none_falls_back() -> None:
    case = _case_by_id("device_auth_hook_044_parse_response_none_falls_back")
    inp = case["input"]
    calls: list[str] = []

    def parse(kind: str, status: int, content_type: str | None, raw_body: str) -> None:
        calls.append(kind)
        return None

    config = _config(parse_response=parse)
    response = HttpResponse(status=200, body=inp["raw_body"], content_type=inp["content_type"])
    parsed = decode_response(config, "token", response)
    assert calls == ["token"], "the hook must be consulted before the built-in parsers"
    assert parsed is not None
    _assert_subset(case["expected"]["parsed"], parsed, case)


def test_hook_045_classify_error_invalid_return() -> None:
    """A return value outside the four identifiers is rejected loudly.

    The fixture's body carries a NON-standard identifier on purpose: an
    already-standard one resolves before the hook is consulted (case 047), so
    the hook would never run and the case would assert nothing.
    """
    case = _case_by_id("device_auth_hook_045_classify_error_invalid_return")
    config = _config(classify_error=lambda body: case["input"]["returns"])
    assert case["expected"]["raises"] is True
    with pytest.raises(ConfigurationError):
        classify(config, case["input"]["body"])


def test_hook_046_classify_error_none_falls_back() -> None:
    """An unresolved identifier plus a hook returning None is a protocol error."""
    case = _case_by_id("device_auth_hook_046_classify_error_none_falls_back")
    calls: list[Any] = []

    def hook(body: Any) -> None:
        calls.append(body)
        return case["input"]["returns"]

    config = _config(classify_error=hook)
    assert classify(config, case["input"]["body"]) == case["expected"]["identifier"]
    assert calls == [case["input"]["body"]], "the hook must actually be consulted for an unresolved identifier"

    # ...and the resulting dispatch is the default outcome, not a silent skip.
    grant = DeviceCodeGrant(config, _ScriptedTransport())
    with pytest.raises(AuthorizationProtocolError):
        grant._dispatch(_as_response({"status": 400, "body": case["input"]["body"]}), 5)
    assert case["expected"]["outcome"] == "protocol_error"


def test_hook_047_hook_order() -> None:
    case = _case_by_id("device_auth_hook_047_hook_order")
    inp = case["input"]
    config = _config(
        error_aliases=dict(inp["config"]["error_aliases"]),
        classify_error=lambda body: inp["returns"],
    )
    assert classify(config, inp["body"]) == case["expected"]["identifier"], (
        "error_aliases must apply before classify_error: 'the user refused' and "
        "'the code timed out' are not interchangeable"
    )


def test_hook_048_no_hooks_matches_baseline() -> None:
    case = _case_by_id("device_auth_hook_048_no_hooks_matches_baseline")
    baseline = _case_by_id(case["input"]["baseline_case"])
    assert case["input"]["hooks"] == {}

    without = _run_poll(baseline)
    explicitly_none = _run_poll(
        baseline,
        hooks={"transform_request": None, "parse_response": None, "classify_error": None},
    )
    assert (without.outcome, without.poll_delays, without.final_interval, without.polls_made) == (
        explicitly_none.outcome,
        explicitly_none.poll_delays,
        explicitly_none.final_interval,
        explicitly_none.polls_made,
    ), "installing no hooks must be byte-identical to the corpus baseline"
    assert case["expected"]["identical_to_baseline"] is True


# --------------------------------------------------------------------------
# corpus coverage
# --------------------------------------------------------------------------


def test_every_fixture_case_is_covered() -> None:
    """Guard against a fixture case silently going unrun.

    ``hook`` cases are asserted by name rather than by parametrization (each
    one exercises a different seam), so they are checked explicitly here.
    """
    assert _CASES, f"fixture at {_CONFORMANCE_DIR / 'device_auth.json'} has no test_cases"
    unknown = sorted({c["kind"] for c in _CASES} - _DISPATCHED_KINDS)
    assert not unknown, f"fixture has case kinds this harness does not run: {unknown}"

    hook_case_ids = {c["id"] for c in _cases_of("hook")}
    covered_by_name = {
        "device_auth_hook_043_transform_request",
        "device_auth_hook_044_parse_response_none_falls_back",
        "device_auth_hook_045_classify_error_invalid_return",
        "device_auth_hook_046_classify_error_none_falls_back",
        "device_auth_hook_047_hook_order",
        "device_auth_hook_048_no_hooks_matches_baseline",
    }
    assert hook_case_ids == covered_by_name, f"unrun hook cases: {sorted(hook_case_ids - covered_by_name)}"

    parametrized = sum(len(_cases_of(kind)) for kind in _DISPATCHED_KINDS)
    assert parametrized == len(_CASES), "every case must be dispatched by exactly one kind"
