"""The ``Grant`` seam and RFC 8628's ``DeviceCodeGrant``.

Six of the eight components in this package — ``TokenSet``, expiry, the store,
refresh, provider configuration, hooks, redaction — are grant-independent.
Only the polling state machine and the device authorization request are
specific to RFC 8628. The interface exists in V1 precisely so a second grant
is one implementation against a stable seam rather than a rewrite: a vendor
"device flow" that is *not* RFC 8628 (JSON bodies, no ``device_code``, pending
signalled purely by HTTP status, polling that returns an authorization code
rather than a token) cannot be reached by any alias list or hook, because the
shape of the flow differs.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from apcore_toolkit.auth.builder import build_device_request, build_token_request, decode_response
from apcore_toolkit.auth.config import DeviceAuthConfig
from apcore_toolkit.auth.errors import (
    DEADLINE_EXCEEDED,
    EXPIRED_TOKEN,
    AuthorizationDeniedError,
    AuthorizationExpiredError,
    AuthorizationProtocolError,
    ConfigurationError,
    TransportError,
)
from apcore_toolkit.auth.parsing import (
    STANDARD_ERROR_IDENTIFIERS,
    apply_error_aliases,
    raw_error_identifier,
)
from apcore_toolkit.auth.tokens import TokenSet, coerce_int
from apcore_toolkit.auth.transport import HttpResponse, Transport

logger = logging.getLogger("apcore_toolkit")

#: Fallback deadline when the device response omits ``expires_in`` — rare, but
#: real. Fifteen minutes, rather than polling forever.
DEFAULT_DEVICE_EXPIRES_IN = 900

#: RFC 8628 §3.5's fixed increment. **Not** a multiplier: the server's rate
#: limiter is written against the RFC's behaviour, so exponential backoff trips
#: it.
SLOW_DOWN_INCREMENT = 5


def effective_deadline(expires_in: int, timeout_seconds: float | None) -> float:
    """How many seconds the client will actually poll for.

    ``timeout_seconds`` is a hard ceiling independent of the server's
    ``expires_in``; **when both apply, the shorter wins**.

    This is the single derivation of that number, and it exists as a function
    rather than an inline ``min`` for one reason: ``on_user_code`` reports this
    value *and* the poll loop stops on it. Computing it twice is how a
    consumer ends up rendering "expires in 600s" while the flow dies at 7 —
    which is exactly what all three SDKs did before this was pinned.

    The parse-time fallback for a device response that omits ``expires_in``
    (:data:`DEFAULT_DEVICE_EXPIRES_IN`) has already been applied by the time
    this runs; the clamp is a separate fold on top of it.
    """
    if timeout_seconds is None:
        return expires_in
    return min(expires_in, timeout_seconds)


def classify(config: DeviceAuthConfig, body: Mapping[str, Any]) -> str | None:
    """Resolve an error body to one of the four RFC identifiers, or ``None``.

    The return value is **always** a standard identifier or ``None``; a vendor
    code that nothing resolved comes back as ``None`` and dispatches to a
    protocol error. Callers wanting the provider's own spelling for a
    diagnostic read :func:`~apcore_toolkit.auth.parsing.raw_error_identifier`.

    Fixed order, conformance-tested: field-name aliasing, then
    ``error_aliases``, then ``classify_error``, then dispatch. The hook is
    consulted **only when what came before did not already resolve to a
    standard identifier** — an alias that resolves to one wins, because "the
    user refused" and "the code timed out" are not interchangeable. This
    matters beyond the corpus: the alternative reading (always consult, alias
    wins) is observationally different for a hook with side effects or one that
    raises.
    """
    raw = raw_error_identifier(body, config.field_aliases)
    identifier = apply_error_aliases(raw, config.error_aliases)
    if identifier in STANDARD_ERROR_IDENTIFIERS:
        return identifier

    if config.classify_error is not None:
        result = config.classify_error(body)
        if result is not None:
            # Returning anything outside the four identifiers is a programming
            # error. Rejecting it loudly is what keeps the hook out of the
            # closed protocol-decision layer: a hook that can invent a fifth
            # state can drive the machine somewhere it has no branch for.
            if not isinstance(result, str) or result not in STANDARD_ERROR_IDENTIFIERS:
                raise ConfigurationError(
                    f"classify_error returned {result!r}, which is not one of "
                    f"{sorted(STANDARD_ERROR_IDENTIFIERS)} or None"
                )
            return result
    return None


@dataclass(frozen=True)
class DeviceCodeResponse:
    """The parsed device authorization response.

    ``device_code`` is a short-lived pre-authorization secret and is never
    written to the store — only the resulting ``TokenSet`` is persisted.
    """

    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None = None
    expires_in: int = DEFAULT_DEVICE_EXPIRES_IN
    interval: int | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        return (
            f"DeviceCodeResponse(device_code='***REDACTED***', user_code={self.user_code!r}, "
            f"verification_uri={self.verification_uri!r}, "
            f"verification_uri_complete={self.verification_uri_complete!r}, "
            f"expires_in={self.expires_in!r}, interval={self.interval!r})"
        )


class Grant(ABC):
    """A way of obtaining a :class:`TokenSet`.

    Implementations own the protocol dance and nothing else: storage, expiry,
    refresh, redaction, and provider configuration are shared.
    """

    #: Stable identifier for the grant, used in diagnostics.
    name: str = "grant"

    @abstractmethod
    def authorize(
        self,
        *,
        on_user_code: Callable[..., Any] | None = None,
        on_poll: Callable[..., Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> TokenSet:
        """Run the grant to completion and return the credential."""
        raise NotImplementedError


class DeviceCodeGrant(Grant):
    """RFC 8628's polling state machine.

    Pure over an injected monotonic clock and sleep plus a response sequence,
    which is what makes the corpus runnable with no HTTP mocking anywhere.

    Two clocks, deliberately: elapsed time uses a **monotonic** clock so an NTP
    correction or a laptop suspend cannot make the deadline jump backwards,
    while ``TokenSet.expires_at`` uses **wall-clock** time because it must
    survive a process restart, where a monotonic value is meaningless.
    """

    name = "device_code"

    def __init__(
        self,
        config: DeviceAuthConfig,
        transport: Transport,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.transport = transport
        self.clock = clock
        self.sleep = sleep
        self.wall_clock = wall_clock
        #: Polling interval in force when the most recent ``authorize()``
        #: finished — asserted by the corpus as ``final_interval``.
        self.final_interval: int | None = None
        #: How many token-endpoint responses the most recent ``authorize()``
        #: consumed, transport failures included.
        self.polls_made = 0

    # -- public ------------------------------------------------------------

    def authorize(
        self,
        *,
        on_user_code: Callable[..., Any] | None = None,
        on_poll: Callable[..., Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> TokenSet:
        device = self.request_device_code()
        # ONE number, derived once. The callback reports it and the poll loop
        # stops on it, so a consumer's countdown cannot disagree with when the
        # client actually gives up — and a later edit cannot let the two drift.
        deadline = effective_deadline(device.expires_in, timeout_seconds)
        self._notify(
            on_user_code,
            "on_user_code",
            verification_uri=device.verification_uri,
            user_code=device.user_code,
            verification_uri_complete=device.verification_uri_complete,
            expires_in=deadline,
        )
        return self.poll_for_token(device, on_poll=on_poll, deadline=deadline)

    def request_device_code(self) -> DeviceCodeResponse:
        """Step 1: ``POST`` to the device authorization endpoint."""
        request = build_device_request(self.config)
        response = self.transport.send(request)
        body = decode_response(self.config, "device", response)
        if body is None or not response.is_success:
            raise AuthorizationProtocolError(
                f"device authorization request failed with HTTP {response.status}",
                raw_body=response.body,
                status=response.status,
            )

        device_code = body.get("device_code")
        user_code = body.get("user_code")
        verification_uri = body.get("verification_uri")
        missing = [
            name
            for name, value in (
                ("device_code", device_code),
                ("user_code", user_code),
                ("verification_uri", verification_uri),
            )
            if not isinstance(value, str) or not value
        ]
        if missing:
            raise AuthorizationProtocolError(
                f"device authorization response is missing {', '.join(missing)}",
                raw_body=response.body,
                status=response.status,
            )

        complete = body.get("verification_uri_complete")
        expires_in = coerce_int(body.get("expires_in"))
        return DeviceCodeResponse(
            # ``user_code`` passes through byte-for-byte: no upper-casing, no
            # stripping, no re-grouping. It is case-sensitive at some providers
            # and at least one embeds it unmodified into a URL query parameter.
            device_code=str(device_code),
            user_code=str(user_code),
            verification_uri=str(verification_uri),
            verification_uri_complete=str(complete) if isinstance(complete, str) and complete else None,
            expires_in=expires_in if expires_in is not None else DEFAULT_DEVICE_EXPIRES_IN,
            interval=coerce_int(body.get("interval")),
            raw=body,
        )

    def poll_for_token(
        self,
        device: DeviceCodeResponse,
        *,
        on_poll: Callable[..., Any] | None = None,
        deadline: float | None = None,
    ) -> TokenSet:
        """Steps 3-6: wait, poll, dispatch, terminate.

        ``deadline`` is the number of seconds to poll for, already clamped by
        any caller timeout — :func:`effective_deadline` is the single place
        that clamp is computed. Omitting it polls for the device response's own
        ``expires_in``.
        """
        interval = device.interval if device.interval is not None else self.config.default_interval
        if deadline is None:
            deadline = float(device.expires_in)

        self.final_interval = interval
        self.polls_made = 0
        started = self.clock()
        attempt = 0

        while True:
            # RFC 8628 §3.5: the client must not poll faster than the
            # interval, and the user has not had time to act yet — so the wait
            # comes BEFORE the first poll, never after it.
            self.sleep(interval)
            elapsed = self.clock() - started

            # Stop at expires_in even when the server never says
            # expired_token. Relying on the server's error alone leaves a
            # client polling indefinitely against a server that never sends it.
            if elapsed >= deadline:
                raise AuthorizationExpiredError(
                    f"device authorization deadline of {deadline:g}s elapsed without a decision",
                    reason=DEADLINE_EXCEEDED,
                )

            attempt += 1
            self._notify(on_poll, "on_poll", attempt=attempt, interval=interval, elapsed=elapsed)

            try:
                response = self.transport.send(build_token_request(self.config, device.device_code))
            except TransportError as exc:
                # Retryable, not terminal: a dropped connection mid-flow is
                # common on flaky networks, and the deadline already bounds the
                # total wait, so retrying cannot loop forever.
                self.polls_made += 1
                logger.debug("token poll %d failed at the transport layer, retrying: %s", attempt, exc)
                continue

            self.polls_made += 1
            tokens, interval = self._dispatch(response, interval)
            self.final_interval = interval
            if tokens is not None:
                return tokens

    # -- internals ---------------------------------------------------------

    def _dispatch(self, response: HttpResponse, interval: int) -> tuple[TokenSet | None, int]:
        """Dispatch on the response **body**, never on the HTTP status code.

        The status is used for exactly one thing: deciding whether the body is
        a success payload (2xx) or an error payload (everything else). A
        surveyed provider returns ``authorization_pending`` as HTTP 428 and
        both ``slow_down`` and ``access_denied`` as 403; a status-driven client
        treats all three as fatal and breaks against it entirely.
        """
        body = decode_response(self.config, "token", response)
        if body is None:
            raise AuthorizationProtocolError(
                f"token response (HTTP {response.status}) could not be parsed",
                raw_body=response.body,
                status=response.status,
            )

        if response.is_success:
            return TokenSet.from_response(body, now=self.wall_clock(), raw_body=response.body), interval

        identifier = classify(self.config, body)
        if identifier == "authorization_pending":
            return None, interval
        if identifier == "slow_down":
            return None, self._backoff(body, interval)
        if identifier == "access_denied":
            raise AuthorizationDeniedError("the user denied the authorization request")
        if identifier == "expired_token":
            raise AuthorizationExpiredError("the device code expired", reason=EXPIRED_TOKEN)

        # `classify` returned None: nothing — not the RFC spelling, not an
        # alias, not the hook — resolved this body to one of the four states.
        # Fail soft on shape and report a protocol error carrying the raw body
        # rather than crashing on a missing key; that body is often the only
        # diagnostic an operator has. `invalid_grant` lands here by design —
        # mapping it to expiry is correct only for providers that fold expiry
        # into it, and wrong everywhere else, so it is never aliased by default.
        # The provider's own spelling goes in the message, never in dispatch.
        vendor_code = raw_error_identifier(body, self.config.field_aliases)
        raise AuthorizationProtocolError(
            f"token endpoint returned an unrecognised error {vendor_code!r} (HTTP {response.status})"
            if vendor_code
            else f"token endpoint returned an unrecognised error envelope (HTTP {response.status})",
            raw_body=response.body,
            status=response.status,
        )

    @staticmethod
    def _backoff(body: Mapping[str, Any], interval: int) -> int:
        """``new_interval = body.interval if present else current + 5``.

        Some providers return an updated ``interval`` inside the ``slow_down``
        response. When present it is authoritative and used verbatim —
        preferring the server's own number is strictly better than guessing.
        """
        supplied = coerce_int(body.get("interval"))
        if supplied is not None and supplied > 0:
            return supplied
        return interval + SLOW_DOWN_INCREMENT

    @staticmethod
    def _notify(callback: Callable[..., Any] | None, label: str, **kwargs: Any) -> None:
        """Invoke a consumer callback, swallowing any failure.

        A rendering failure in the UI layer is not a reason to lose an
        in-flight authorization, so callback exceptions are recorded as a
        warning and never abort the flow.
        """
        if callback is None:
            return
        try:
            callback(**kwargs)
        except Exception:
            logger.warning("%s callback raised; continuing the flow", label, exc_info=True)
