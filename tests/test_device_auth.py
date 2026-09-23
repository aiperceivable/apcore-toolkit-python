"""Unit tests for the device-authorization client beyond the shared corpus.

The conformance fixture pins cross-SDK *behaviour*; these cover the things that
are Python-specific or filesystem-bound and therefore cannot live there:
``FileTokenStore`` permissions and atomicity, redaction of every debug string,
callback conventions, and construction-time configuration validation.
"""

from __future__ import annotations

import base64
import json
import os
import stat
from pathlib import Path
from urllib.parse import unquote_plus
from typing import Any

import httpx
import pytest

from apcore_toolkit.auth import (
    DeviceAuthClient,
    DeviceAuthConfig,
    DeviceCodeGrant,
    DeviceCodeResponse,
    FileTokenStore,
    HttpResponse,
    HttpxTransport,
    MemoryTokenStore,
    PreparedRequest,
    TokenSet,
    TransportError,
    basic_auth_header,
    default_credentials_path,
    effective_deadline,
    store_key,
)
from apcore_toolkit.auth.errors import (
    AuthorizationExpiredError,
    AuthorizationProtocolError,
    ConfigurationError,
    CredentialPermissionError,
    NoCredentialError,
    RefreshFailedError,
)

_DEVICE_ENDPOINT = "https://auth.example.com/device"
_TOKEN_ENDPOINT = "https://auth.example.com/token"


def _config(**overrides: Any) -> DeviceAuthConfig:
    params: dict[str, Any] = {
        "client_id": "cid",
        "device_authorization_endpoint": _DEVICE_ENDPOINT,
        "token_endpoint": _TOKEN_ENDPOINT,
    }
    params.update(overrides)
    return DeviceAuthConfig(**params)


class _StubTransport:
    """Returns a queued response per request kind and records what was sent."""

    def __init__(self, responses: dict[str, list[HttpResponse]] | None = None) -> None:
        self.responses = responses or {}
        self.sent: list[PreparedRequest] = []

    def send(self, request: PreparedRequest) -> HttpResponse:
        self.sent.append(request)
        queue = self.responses.get(request.kind)
        if not queue:
            raise AssertionError(f"no response scripted for kind {request.kind!r}")
        return queue.pop(0)


def _json_response(body: dict[str, Any], status: int = 200) -> HttpResponse:
    return HttpResponse(status=status, body=json.dumps(body), content_type="application/json")


# --------------------------------------------------------------------------
# FileTokenStore
# --------------------------------------------------------------------------


def test_file_store_roundtrip_and_multiple_issuers(tmp_path: Path) -> None:
    store = FileTokenStore(tmp_path / "credentials.json")
    a = TokenSet(access_token="a", expires_at=100, scope=["openid"])
    b = TokenSet(access_token="b", refresh_token="rb")

    store.save(store_key("https://one.example", "cid"), a)
    store.save(store_key("https://two.example", "cid"), b)

    assert store.load("https://one.example|cid") == a
    assert store.load("https://two.example|cid") == b
    # Credentials for several authorization servers coexist without collision.
    assert set(json.loads((tmp_path / "credentials.json").read_text())) == {
        "https://one.example|cid",
        "https://two.example|cid",
    }


def test_file_store_load_missing_returns_none(tmp_path: Path) -> None:
    store = FileTokenStore(tmp_path / "nope" / "credentials.json")
    assert store.load("k") is None


def test_file_store_clear_is_idempotent(tmp_path: Path) -> None:
    store = FileTokenStore(tmp_path / "credentials.json")
    store.clear("absent")  # no file at all
    store.save("k", TokenSet(access_token="a"))
    store.clear("k")
    store.clear("k")
    assert store.load("k") is None


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_file_store_creates_file_with_0600(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    FileTokenStore(path).save("k", TokenSet(access_token="a"))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_file_store_refuses_over_permissive_file(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({"k": TokenSet(access_token="a").to_dict()}))
    path.chmod(0o644)

    store = FileTokenStore(path)
    with pytest.raises(CredentialPermissionError) as exc_info:
        store.load("k")
    # The error must be actionable: it names the file and the fix.
    assert str(path) in str(exc_info.value)
    assert "chmod 600" in str(exc_info.value)

    # Refusal applies to writes too — save reads the existing records first.
    with pytest.raises(CredentialPermissionError):
        store.save("k", TokenSet(access_token="b"))


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_file_store_never_widens_permissions_on_rewrite(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    store = FileTokenStore(path)
    store.save("k", TokenSet(access_token="a"))
    store.save("k", TokenSet(access_token="b"))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_file_store_writes_atomically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Temp file in the SAME directory, then rename.

    A cross-filesystem rename is not atomic, and a temp file elsewhere would
    silently degrade the guarantee, so the directory identity is the assertion.
    """
    path = tmp_path / "nested" / "credentials.json"
    observed: list[tuple[str, str]] = []
    real_replace = os.replace

    def spy(src: Any, dst: Any) -> None:
        observed.append((str(src), str(dst)))
        # The destination must not exist in a half-written state: assert the
        # temp file is complete before the rename makes it visible.
        json.loads(Path(src).read_text())
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    FileTokenStore(path).save("k", TokenSet(access_token="a"))

    assert len(observed) == 1
    src, dst = observed[0]
    assert Path(src).parent == Path(dst).parent == path.parent
    assert src != dst
    assert not list(path.parent.glob("*.tmp")), "the temporary file must not survive the rename"


def test_file_store_ignores_unreadable_record(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({"k": {"no_access_token": True}}))
    if os.name == "posix":
        path.chmod(0o600)
    assert FileTokenStore(path).load("k") is None


def test_default_credentials_path_is_under_apcore() -> None:
    path = default_credentials_path()
    assert path.name == "credentials.json"
    assert path.parent.name == "apcore"


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------


def test_token_set_repr_redacts_both_tokens() -> None:
    tokens = TokenSet(access_token="ACCESS", refresh_token="REFRESH", expires_at=1, scope=["openid"])
    for rendered in (repr(tokens), str(tokens), f"{tokens}", f"{tokens!r}", "%s" % (tokens,)):
        assert "ACCESS" not in rendered
        assert "REFRESH" not in rendered
        assert "***REDACTED***" in rendered
    # Non-secret metadata stays visible — redaction must not make the type
    # useless for diagnostics.
    assert "expires_at=1" in repr(tokens)
    assert "'openid'" in repr(tokens)


def test_token_set_repr_shows_absent_refresh_token_as_none() -> None:
    assert "refresh_token=None" in repr(TokenSet(access_token="a"))


def test_token_set_inside_a_container_is_still_redacted() -> None:
    """``repr`` of a list calls each element's ``repr`` — the common leak path."""
    assert "ACCESS" not in repr([TokenSet(access_token="ACCESS")])
    assert "ACCESS" not in repr({"tokens": TokenSet(access_token="ACCESS")})


def test_device_code_response_repr_redacts_the_device_code() -> None:
    """``device_code`` is a short-lived pre-authorization secret."""
    response = DeviceCodeResponse(
        device_code="DEVICE-SECRET",
        user_code="ABCD-EFGH",
        verification_uri="https://e.example/device",
    )
    assert "DEVICE-SECRET" not in repr(response)
    assert "ABCD-EFGH" in repr(response)


# --------------------------------------------------------------------------
# TokenSet
# --------------------------------------------------------------------------


def test_token_set_from_response_coerces_string_expires_in() -> None:
    """Form-encoded responses deliver every field as a string."""
    tokens = TokenSet.from_response({"access_token": "t", "expires_in": "3600"}, now=1000)
    assert tokens.expires_at == 4600
    assert tokens.obtained_at == 1000


def test_token_set_from_response_requires_an_access_token() -> None:
    with pytest.raises(AuthorizationProtocolError) as exc_info:
        TokenSet.from_response({"unexpected": "shape"}, now=0, raw_body='{"unexpected":"shape"}')
    assert exc_info.value.raw_body == '{"unexpected":"shape"}'


def test_token_set_passes_through_an_unknown_token_type() -> None:
    """Only Bearer casing is normalised; anything else is not ours to rewrite."""
    assert TokenSet.from_response({"access_token": "t", "token_type": "DPoP"}, now=0).token_type == "DPoP"


def test_token_set_dict_roundtrip() -> None:
    tokens = TokenSet(access_token="a", refresh_token="r", expires_at=5, scope=["x", "y"], obtained_at=1)
    assert TokenSet.from_dict(tokens.to_dict()) == tokens


def test_is_expired_defaults_to_the_real_clock() -> None:
    assert TokenSet(access_token="a", expires_at=0).is_expired() is True
    assert TokenSet(access_token="a").is_expired() is False


# --------------------------------------------------------------------------
# Configuration validation
# --------------------------------------------------------------------------


def test_plaintext_token_endpoint_is_refused_at_construction() -> None:
    with pytest.raises(ConfigurationError, match="https"):
        _config(token_endpoint="http://auth.example.com/token")


def test_localhost_http_is_permitted_for_development() -> None:
    config = DeviceAuthConfig(
        client_id="cid",
        device_authorization_endpoint="http://localhost:8080/device",
        token_endpoint="http://localhost:8080/token",
    )
    assert config.token_endpoint == "http://localhost:8080/token"


@pytest.mark.parametrize(
    "overrides",
    [
        {"client_auth_method": "private_key_jwt", "client_secret": "s"},
        {"client_auth_method": "client_secret_basic"},  # no secret
        {"request_encoding": {"token": "xml"}},
        {"request_encoding": {"nonsense": "json"}},
        {"default_interval": 0},
        {"error_aliases": {"x": "invented_state"}},
    ],
)
def test_invalid_configuration_is_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ConfigurationError):
        _config(**overrides)


def test_config_requires_an_issuer_or_explicit_endpoints() -> None:
    with pytest.raises(ConfigurationError):
        DeviceAuthConfig(client_id="cid")


def test_config_is_a_value_and_discover_does_not_mutate_it() -> None:
    config = _config(issuer="https://auth.example.com")
    transport = _StubTransport(
        {
            "discovery": [
                _json_response(
                    {
                        "issuer": "https://auth.example.com",
                        "device_authorization_endpoint": "https://auth.example.com/d",
                        "token_endpoint": "https://auth.example.com/t",
                        "revocation_endpoint": "https://auth.example.com/r",
                    }
                )
            ]
        }
    )
    discovered = config.discover(transport=transport)
    # Explicit configuration always wins, so a compromised discovery document
    # cannot silently redirect a token request.
    assert discovered.token_endpoint == _TOKEN_ENDPOINT
    assert discovered.revocation_endpoint == "https://auth.example.com/r"
    assert config.revocation_endpoint is None


@pytest.mark.parametrize(
    "client_id,client_secret",
    [
        ("cid", "sec"),  # unreserved: the common case, unchanged by encoding
        ("cid", "s p+a:ce"),  # the characters that discriminate
        ("cid", "a+b"),  # left raw this decodes to "a b" — a different secret
        ("cl:id", "p@ss word"),  # a colon in the *id* must not shift the split
    ],
)
def test_basic_auth_credentials_survive_the_server_side_round_trip(client_id: str, client_secret: str) -> None:
    """RFC 6749 §2.3.1 — form-urlencode before base64, asserted as a round trip.

    Not an exact-bytes assertion: form encoders legitimately differ on how
    space, ``*`` and ``~`` are spelled, and every spelling decodes identically,
    so the byte string is not the contract. What must hold is that a server
    decoding the header recovers exactly what was configured. Raw concatenation
    fails this — the secret's ``+`` comes back as a space.
    """
    header = basic_auth_header(client_id, client_secret)
    decoded = base64.b64decode(header.removeprefix("Basic ")).decode()
    raw_id, _, raw_secret = decoded.partition(":")
    assert (unquote_plus(raw_id), unquote_plus(raw_secret)) == (client_id, client_secret)


def test_store_key_is_issuer_pipe_client_id() -> None:
    assert _config(issuer="https://a.example").store_key == "https://a.example|cid"
    # Without an issuer the token endpoint identifies the same server.
    assert _config().store_key == f"{_TOKEN_ENDPOINT}|cid"


# --------------------------------------------------------------------------
# Client behaviour
# --------------------------------------------------------------------------


def _login_client(**kwargs: Any) -> tuple[DeviceAuthClient, _StubTransport]:
    transport = _StubTransport(
        {
            "device": [
                _json_response(
                    {
                        "device_code": "DEVICE-SECRET",
                        "user_code": "wdjb-mjht",
                        "verification_uri": "https://e.example/device",
                        "expires_in": 600,
                        "interval": 5,
                    }
                )
            ],
            "token": [_json_response({"access_token": "t", "token_type": "bearer", "expires_in": 3600})],
        }
    )
    client = DeviceAuthClient(
        _config(),
        store=MemoryTokenStore(),
        transport=transport,
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
        wall_clock=lambda: 1000.0,
        **kwargs,
    )
    return client, transport


def test_login_passes_callback_fields_as_keyword_arguments() -> None:
    seen: dict[str, Any] = {}
    polls: list[dict[str, Any]] = []
    client, _ = _login_client()

    client.login(
        on_user_code=lambda **event: seen.update(event),
        on_poll=lambda **event: polls.append(event),
    )

    assert seen == {
        "verification_uri": "https://e.example/device",
        # Byte-for-byte: no upper-casing, no stripping, no re-grouping.
        "user_code": "wdjb-mjht",
        "verification_uri_complete": None,
        "expires_in": 600,
    }
    assert polls == [{"attempt": 1, "interval": 5, "elapsed": 0.0}]


def test_a_raising_callback_never_aborts_the_flow(caplog: pytest.LogCaptureFixture) -> None:
    """A rendering failure in the UI layer is not a reason to lose an
    in-flight authorization."""

    def explode(**_event: Any) -> None:
        raise RuntimeError("the terminal caught fire")

    client, _ = _login_client()
    tokens = client.login(on_user_code=explode, on_poll=explode)
    assert tokens.access_token == "t"
    assert any("callback raised" in record.getMessage() for record in caplog.records)


def test_login_persists_the_token_set_but_never_the_device_code() -> None:
    client, _ = _login_client()
    tokens = client.login()

    assert client.store.load(client.store_key) == tokens
    assert tokens.token_type == "Bearer"
    assert tokens.expires_at == 4600
    serialised = json.dumps(tokens.to_dict())
    assert "DEVICE-SECRET" not in serialised


def test_login_sends_no_scope_when_none_is_configured() -> None:
    client, transport = _login_client()
    client.login()
    device_request = next(r for r in transport.sent if r.kind == "device")
    assert "scope" not in device_request.params
    assert device_request.headers["Accept"] == "application/json"


def test_device_request_transport_failure_propagates() -> None:
    """Only *polling* retries transport failures; the device request has no deadline."""

    class _Broken:
        def send(self, request: PreparedRequest) -> HttpResponse:
            raise TransportError("down")

    client = DeviceAuthClient(_config(), store=MemoryTokenStore(), transport=_Broken())
    with pytest.raises(TransportError):
        client.login()


def test_ensure_valid_returns_a_live_token_without_any_network() -> None:
    store = MemoryTokenStore()
    config = _config()
    store.save(config.store_key, TokenSet(access_token="live", expires_at=10_000))
    transport = _StubTransport()

    client = DeviceAuthClient(config, store=store, transport=transport, wall_clock=lambda: 1000.0)
    assert client.ensure_valid().access_token == "live"
    assert transport.sent == [], "a valid token must not trigger a refresh"


def test_ensure_valid_without_a_stored_credential_raises() -> None:
    client = DeviceAuthClient(_config(), store=MemoryTokenStore(), transport=_StubTransport())
    with pytest.raises(NoCredentialError):
        client.ensure_valid()


def test_ensure_valid_without_a_refresh_token_raises_rather_than_refreshing() -> None:
    """Common for short-lived scopes: there is nothing to exchange."""
    config = _config()
    store = MemoryTokenStore()
    store.save(config.store_key, TokenSet(access_token="stale", expires_at=100))
    client = DeviceAuthClient(config, store=store, transport=_StubTransport(), wall_clock=lambda: 1000.0)
    with pytest.raises(NoCredentialError):
        client.ensure_valid()


def test_ensure_valid_refreshes_within_the_skew_window() -> None:
    config = _config()
    store = MemoryTokenStore()
    store.save(config.store_key, TokenSet(access_token="old", refresh_token="r1", expires_at=1020))
    transport = _StubTransport(
        {"refresh": [_json_response({"access_token": "new", "expires_in": 3600, "refresh_token": "r2"})]}
    )
    client = DeviceAuthClient(config, store=store, transport=transport, wall_clock=lambda: 1000.0)

    # 1000 + 30 >= 1020, so the default skew makes this due for refresh.
    refreshed = client.ensure_valid()
    assert refreshed.access_token == "new"
    assert store.load(config.store_key) == refreshed
    assert transport.sent[0].params["grant_type"] == "refresh_token"


def test_refresh_failure_that_is_not_invalid_grant_is_not_terminal() -> None:
    """Only ``invalid_grant`` clears the store; a 500 must not destroy the credential."""
    config = _config()
    store = MemoryTokenStore()
    stored = TokenSet(access_token="old", refresh_token="r1", expires_at=1000)
    store.save(config.store_key, stored)
    transport = _StubTransport({"refresh": [_json_response({"error": "temporarily_unavailable"}, status=503)]})
    client = DeviceAuthClient(config, store=store, transport=transport, wall_clock=lambda: 2000.0)

    with pytest.raises(AuthorizationProtocolError):
        client.refresh()
    assert store.load(config.store_key) == stored


def test_refresh_with_invalid_grant_clears_the_store() -> None:
    config = _config()
    store = MemoryTokenStore()
    store.save(config.store_key, TokenSet(access_token="old", refresh_token="r1", expires_at=1000))
    transport = _StubTransport({"refresh": [_json_response({"error": "invalid_grant"}, status=400)]})
    client = DeviceAuthClient(config, store=store, transport=transport, wall_clock=lambda: 2000.0)

    with pytest.raises(RefreshFailedError):
        client.refresh()
    assert store.load(config.store_key) is None


def test_auth_header_factory_returns_a_mapping_and_refreshes_transparently() -> None:
    config = _config()
    store = MemoryTokenStore()
    store.save(config.store_key, TokenSet(access_token="old", refresh_token="r1", expires_at=1000))
    transport = _StubTransport({"refresh": [_json_response({"access_token": "fresh", "expires_in": 3600})]})
    client = DeviceAuthClient(config, store=store, transport=transport, wall_clock=lambda: 2000.0)

    factory = client.as_auth_header_factory()
    assert factory() == {"Authorization": "Bearer fresh"}
    # Second call is served from the store — the factory is per-request, but a
    # live token costs nothing.
    assert factory() == {"Authorization": "Bearer fresh"}
    assert len(transport.sent) == 1


def test_auth_header_factory_supports_a_non_bearer_header_shape() -> None:
    """Credential type -> header shape is data, not a code branch."""
    config = _config()
    store = MemoryTokenStore()
    store.save(config.store_key, TokenSet(access_token="k", expires_at=10_000))
    client = DeviceAuthClient(config, store=store, transport=_StubTransport(), wall_clock=lambda: 1000.0)

    factory = client.as_auth_header_factory(header_name="x-api-key", value_template="{access_token}")
    assert factory() == {"x-api-key": "k"}


def test_logout_revokes_then_clears() -> None:
    config = _config(revocation_endpoint="https://auth.example.com/revoke")
    store = MemoryTokenStore()
    store.save(config.store_key, TokenSet(access_token="a"))
    transport = _StubTransport({"revoke": [_json_response({}, status=200)]})
    client = DeviceAuthClient(config, store=store, transport=transport)

    client.logout()
    assert store.load(config.store_key) is None
    assert transport.sent[0].kind == "revoke"


def test_logout_clears_even_when_revocation_fails() -> None:
    class _Broken(_StubTransport):
        def send(self, request: PreparedRequest) -> HttpResponse:
            raise TransportError("down")

    config = _config(revocation_endpoint="https://auth.example.com/revoke")
    store = MemoryTokenStore()
    store.save(config.store_key, TokenSet(access_token="a"))
    client = DeviceAuthClient(config, store=store, transport=_Broken())

    client.logout()
    assert store.load(config.store_key) is None


# --------------------------------------------------------------------------
# Grant seam
# --------------------------------------------------------------------------


def test_a_custom_grant_can_replace_the_device_flow() -> None:
    """The ``Grant`` interface is the seam a second grant plugs into."""
    from apcore_toolkit.auth import Grant

    class _StubGrant(Grant):
        name = "stub"

        def authorize(self, **_kwargs: Any) -> TokenSet:
            return TokenSet(access_token="from-a-different-grant")

    config = _config()
    store = MemoryTokenStore()
    client = DeviceAuthClient(config, store=store, transport=_StubTransport(), grant=_StubGrant())

    tokens = client.login()
    assert tokens.access_token == "from-a-different-grant"
    assert store.load(config.store_key) == tokens


def test_grant_is_abstract() -> None:
    from apcore_toolkit.auth import Grant

    with pytest.raises(TypeError):
        Grant()  # type: ignore[abstract]


@pytest.mark.parametrize(
    "expires_in,timeout_seconds,expected",
    [
        (600, None, 600),  # no ceiling: the server's value stands
        (600, 7, 7),  # the ceiling is shorter, so it wins
        (600, 900, 600),  # the ceiling is longer, so expires_in wins
        (900, 900, 900),  # equal: either answer is the same answer
    ],
)
def test_effective_deadline_takes_the_shorter_of_the_two(
    expires_in: int, timeout_seconds: float | None, expected: float
) -> None:
    assert effective_deadline(expires_in, timeout_seconds) == expected


def test_the_callback_is_told_the_same_deadline_the_loop_stops_on() -> None:
    """The anti-drift property, asserted directly rather than inferred.

    The defect this guards against is not a wrong constant — it is two
    derivations of the same number. Here the callback's value and the elapsed
    time at which the flow gives up are compared to each other, so they cannot
    disagree even if both were wrong.
    """
    reported: list[float] = []
    delays: list[float] = []
    now = {"t": 0.0}

    def sleep(seconds: float) -> None:
        delays.append(seconds)
        now["t"] += seconds

    transport = _StubTransport(
        {
            "device": [
                _json_response(
                    {
                        "device_code": "dc",
                        "user_code": "AB-CD",
                        "verification_uri": "https://e.example/d",
                        "expires_in": 600,
                        "interval": 5,
                    }
                )
            ],
            "token": [_json_response({"error": "authorization_pending"}, status=400)] * 4,
        }
    )
    grant = DeviceCodeGrant(_config(), transport, clock=lambda: now["t"], sleep=sleep)

    with pytest.raises(AuthorizationExpiredError) as exc_info:
        grant.authorize(
            on_user_code=lambda **event: reported.append(event["expires_in"]),
            timeout_seconds=7,
        )

    assert exc_info.value.reason == "deadline_exceeded"
    assert reported == [7], "the callback must report the clamped deadline, not the server's 600"
    # The flow gave up on the first sleep that reached the reported number.
    assert sum(delays) >= reported[0]
    assert sum(delays[:-1]) < reported[0]


def test_timeout_seconds_wins_when_it_is_shorter_than_expires_in() -> None:
    delays: list[float] = []
    now = {"t": 0.0}

    def sleep(seconds: float) -> None:
        delays.append(seconds)
        now["t"] += seconds

    transport = _StubTransport(
        {
            "device": [
                _json_response(
                    {
                        "device_code": "dc",
                        "user_code": "AB-CD",
                        "verification_uri": "https://e.example/device",
                        "expires_in": 600,
                        "interval": 5,
                    }
                )
            ],
            "token": [_json_response({"error": "authorization_pending"}, status=400)] * 3,
        }
    )
    grant = DeviceCodeGrant(_config(), transport, clock=lambda: now["t"], sleep=sleep)

    with pytest.raises(Exception) as exc_info:
        grant.authorize(timeout_seconds=12)
    assert getattr(exc_info.value, "reason", None) == "deadline_exceeded"
    assert delays == [5, 5, 5]


# --------------------------------------------------------------------------
# HttpxTransport.send() — the default, httpx-backed Transport implementation.
# ``httpx.MockTransport`` swaps out the wire layer so these exercise the real
# httpx request/response path without any real network I/O.
# --------------------------------------------------------------------------


def test_httpx_transport_send_returns_decoded_response() -> None:
    """A successful request is decoded into an ``HttpResponse`` carrying the
    status code, body text, and content-type header."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url == "https://auth.example.com/token"
        assert request.content == b"grant_type=x"
        return httpx.Response(200, text='{"access_token": "abc"}', headers={"content-type": "application/json"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        transport = HttpxTransport(client=client)
        request = PreparedRequest(
            kind="token",
            url="https://auth.example.com/token",
            body="grant_type=x",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response = transport.send(request)
    finally:
        client.close()

    assert isinstance(response, HttpResponse)
    assert response.status == 200
    assert response.body == '{"access_token": "abc"}'
    assert response.content_type == "application/json"
    assert response.is_success is True


def test_httpx_transport_send_wraps_httpx_error_as_transport_error() -> None:
    """An ``httpx.HTTPError`` raised while sending (e.g. a connection failure)
    must surface as the documented :class:`TransportError`, not the raw
    httpx exception, so callers only ever catch the auth taxonomy."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        transport = HttpxTransport(client=client)
        request = PreparedRequest(kind="device", url="https://auth.example.com/device", body="")
        with pytest.raises(TransportError, match="device request to .* failed"):
            transport.send(request)
    finally:
        client.close()
