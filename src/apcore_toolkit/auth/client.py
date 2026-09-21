"""``DeviceAuthClient`` — the grant-independent half: storage, expiry, refresh.

The bottom edge of this class is the point of the whole design: its output
plugs into an integration point that already exists.
:class:`~apcore_toolkit.output.http_proxy_writer.HTTPProxyRegistryWriter` has
accepted a pluggable ``auth_header_factory`` since it shipped, and calls it
once per request rather than once at construction — so a managed credential
slots in with no new integration surface at all.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from apcore_toolkit.auth.builder import build_refresh_request, build_revoke_request, decode_response
from apcore_toolkit.auth.config import DeviceAuthConfig
from apcore_toolkit.auth.errors import (
    AuthorizationProtocolError,
    NoCredentialError,
    RefreshFailedError,
    TransportError,
)
from apcore_toolkit.auth.grant import DeviceCodeGrant, Grant
from apcore_toolkit.auth.parsing import raw_error_identifier
from apcore_toolkit.auth.store import FileTokenStore, TokenStore
from apcore_toolkit.auth.tokens import DEFAULT_SKEW_SECONDS, TokenSet
from apcore_toolkit.auth.transport import HttpxTransport, Transport

logger = logging.getLogger("apcore_toolkit")

#: A refresh rejected with this identifier is terminal: under the rotation the
#: spec assumes, the refresh token is spent or revoked and retrying cannot
#: succeed.
TERMINAL_REFRESH_ERROR = "invalid_grant"


class DeviceAuthClient:
    """Obtain, persist, and keep fresh a credential for one authorization server.

    The clock and sleep are injected so the polling state machine is
    deterministic under test; both default to the real implementations.
    Elapsed time uses ``time.monotonic``; ``expires_at`` uses ``time.time``.
    """

    def __init__(
        self,
        config: DeviceAuthConfig,
        store: TokenStore | None = None,
        *,
        transport: Transport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wall_clock: Callable[[], float] = time.time,
        grant: Grant | None = None,
    ) -> None:
        self.config = config
        self.store: TokenStore = store if store is not None else FileTokenStore()
        self.transport: Transport = transport or HttpxTransport(config.http_client, timeout=config.http_timeout)
        self.clock = clock
        self.sleep = sleep
        self.wall_clock = wall_clock
        self.grant: Grant = grant or DeviceCodeGrant(
            config,
            self.transport,
            clock=clock,
            sleep=sleep,
            wall_clock=wall_clock,
        )

    @property
    def store_key(self) -> str:
        return self.config.store_key

    # -- flow --------------------------------------------------------------

    def login(
        self,
        *,
        on_user_code: Callable[..., Any] | None = None,
        on_poll: Callable[..., Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> TokenSet:
        """Run the configured grant and persist the result.

        ``on_user_code`` is invoked once with ``verification_uri``,
        ``user_code``, ``verification_uri_complete`` (nullable) and
        ``expires_in``; ``on_poll`` before each poll with ``attempt``,
        ``interval`` and ``elapsed``. Both are keyword arguments, both are
        optional, and neither may influence protocol behaviour — omitting them
        yields a silent, headless flow suitable for daemons and tests.

        ``timeout_seconds`` is a hard ceiling independent of the server's
        ``expires_in``; when both apply, the shorter wins.
        """
        tokens = self.grant.authorize(
            on_user_code=on_user_code,
            on_poll=on_poll,
            timeout_seconds=timeout_seconds,
        )
        self.store.save(self.store_key, tokens)
        return tokens

    def ensure_valid(self, skew_seconds: int = DEFAULT_SKEW_SECONDS) -> TokenSet:
        """Return a credential valid for at least ``skew_seconds`` more.

        Idempotent and network-free while the stored token is still good.
        """
        tokens = self.store.load(self.store_key)
        if tokens is None:
            raise NoCredentialError(f"no stored credential for {self.store_key!r}; call login() first")
        if not tokens.is_expired(skew_seconds, now=self.wall_clock):
            return tokens
        if not tokens.refresh_token:
            # Common for short-lived scopes: the server issued no refresh
            # token, so there is nothing to exchange and a fresh login is the
            # only way forward.
            raise NoCredentialError(
                f"the stored credential for {self.store_key!r} has expired and carries no refresh token; "
                f"call login() again"
            )
        return self.refresh(tokens)

    def refresh(self, tokens: TokenSet | None = None) -> TokenSet:
        """Exchange the refresh token for a new ``TokenSet``.

        **Rotation is assumed.** The stored record is replaced wholesale — a
        new access token is never merged into the old record, because that
        would keep a refresh token the server has already invalidated.
        """
        current = tokens if tokens is not None else self.store.load(self.store_key)
        if current is None:
            raise NoCredentialError(f"no stored credential for {self.store_key!r}; call login() first")
        if not current.refresh_token:
            raise NoCredentialError(f"the credential for {self.store_key!r} carries no refresh token")

        # Transport errors propagate here: unlike polling, there is no deadline
        # to bound retries.
        response = self.transport.send(build_refresh_request(self.config, current.refresh_token))
        body = decode_response(self.config, "refresh", response)

        if response.is_success and body is not None:
            refreshed = TokenSet.from_response(body, now=self.wall_clock(), raw_body=response.body)
            self.store.save(self.store_key, refreshed)
            return refreshed

        # ``error_aliases`` deliberately does NOT apply here. It exists to feed
        # the device-flow dispatch table, and the one alias the spec explicitly
        # describes — ``invalid_grant`` -> ``expired_token``, for providers that
        # fold device-code expiry into it — would otherwise disable the terminal
        # rule that protects the store: the refresh path would see a
        # non-terminal state and leave a spent refresh token on disk. The raw
        # identifier is what decides.
        identifier = raw_error_identifier(body or {}, self.config.field_aliases)
        if identifier == TERMINAL_REFRESH_ERROR:
            self.store.clear(self.store_key)
            raise RefreshFailedError(
                f"refresh for {self.store_key!r} was rejected with {TERMINAL_REFRESH_ERROR}; "
                f"the stored credential has been discarded and a fresh login is required"
            )
        raise AuthorizationProtocolError(
            f"refresh failed with HTTP {response.status} ({identifier or 'no error identifier'})",
            raw_body=response.body,
            status=response.status,
        )

    def logout(self) -> None:
        """Clear the stored credential, revoking it first where possible.

        Revocation is best-effort: RFC 7009 is not universally implemented, and
        a server that refuses to revoke must not prevent the local credential
        from being discarded.
        """
        tokens = self.store.load(self.store_key)
        if tokens is not None and self.config.revocation_endpoint:
            try:
                self.transport.send(build_revoke_request(self.config, tokens.access_token, "access_token"))
            except TransportError as exc:
                logger.warning("revocation request failed; clearing the local credential anyway: %s", exc)
        self.store.clear(self.store_key)

    # -- composition -------------------------------------------------------

    def as_auth_header_factory(
        self,
        *,
        header_name: str = "Authorization",
        value_template: str = "{token_type} {access_token}",
    ) -> Callable[[], dict[str, str]]:
        """A callable returning a complete header **mapping**, not a token string.

        The header carrying a credential is not universally
        ``Authorization: Bearer`` — surveyed APIs also use ``x-api-key`` and
        ``api-key``, and one vendor accepts both, choosing by credential type.
        Returning a mapping (and letting the caller name the header) keeps that
        as data rather than a code branch.

        The factory calls :meth:`ensure_valid` on every invocation, so a
        long-running process refreshes transparently without the proxy writer
        knowing anything about OAuth. It returns headers for one configured
        authorization server: consumers **must not** reuse a factory across
        hosts, because sending a bearer token to an unintended host leaks it.
        """

        def factory() -> dict[str, str]:
            tokens = self.ensure_valid()
            return {
                header_name: value_template.format(
                    token_type=tokens.token_type,
                    access_token=tokens.access_token,
                )
            }

        return factory
