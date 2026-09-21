"""Exceptions raised by the device-authorization client.

Every error the ``apcore_toolkit.auth`` package raises derives from
:class:`AuthError`, so a consumer can catch the whole surface with one clause
while still discriminating on the specific failure when it matters.

The taxonomy follows ``docs/features/device-auth.md`` — see the
``Contract: DeviceAuthClient.login`` and ``Contract: TokenStore`` blocks.
"""

from __future__ import annotations

#: ``AuthorizationExpiredError.reason`` when the *server* reported the device
#: code as expired.
EXPIRED_TOKEN = "expired_token"

#: ``AuthorizationExpiredError.reason`` when the *client* stopped because the
#: locally-tracked deadline elapsed. Both are "the authorization is over", but
#: only the first one is the server's own verdict.
DEADLINE_EXCEEDED = "deadline_exceeded"

#: ``DiscoveryError.reason``: the metadata document's own ``issuer`` is not the
#: issuer used to build the URL, so it is not authoritative for this issuer.
ISSUER_MISMATCH = "issuer_mismatch"

#: ``DiscoveryError.reason``: a discovered endpoint is not ``https://``. The
#: hard half of the endpoint rule — the soft half (a different *origin*) is a
#: warning, and the endpoint is followed.
INSECURE_ENDPOINT = "insecure_endpoint"

#: ``DiscoveryError.reason``: no candidate yielded a usable metadata document.
NO_METADATA = "no_metadata"


class AuthError(Exception):
    """Base class for every error raised by :mod:`apcore_toolkit.auth`."""


class ConfigurationError(AuthError, ValueError):
    """A :class:`~apcore_toolkit.auth.config.DeviceAuthConfig` is invalid.

    Raised at *construction* time, not at request time: an alias mapping onto
    a non-RFC identifier, a plaintext token endpoint, or an unknown client
    authentication method are all programming errors that should surface
    before any network access happens.
    """


class TransportError(AuthError):
    """The HTTP request did not complete (connection reset, timeout, DNS).

    During polling this is *retried* rather than raised — see the dispatch
    table in the spec. It surfaces to the caller only outside the poll loop.
    """


class DiscoveryError(AuthError):
    """Authorization-server metadata could not be resolved, or was refused.

    ``reason`` is one of :data:`ISSUER_MISMATCH`, :data:`INSECURE_ENDPOINT`, or
    :data:`NO_METADATA` — a stable identifier, because the human wording is
    idiomatic per SDK while the *reason* is part of the cross-SDK contract.
    """

    def __init__(self, message: str, *, reason: str = "no_metadata") -> None:
        super().__init__(message)
        self.reason = reason


class AuthorizationDeniedError(AuthError):
    """The server returned ``access_denied`` — the user refused."""


class AuthorizationExpiredError(AuthError):
    """The device code expired, or the client-side deadline elapsed.

    ``reason`` is either :data:`EXPIRED_TOKEN` (the server said so) or
    :data:`DEADLINE_EXCEEDED` (the client stopped on its own clock).
    """

    def __init__(self, message: str, *, reason: str = EXPIRED_TOKEN) -> None:
        super().__init__(message)
        self.reason = reason


class AuthorizationProtocolError(AuthError):
    """An unrecognised error identifier, a malformed body, or an unknown envelope.

    ``raw_body`` carries the undecoded response body verbatim. The spec
    requires the built-in parser to *fail soft on shape*: when neither a
    standard nor an aliased error identifier can be found, the client reports
    a protocol error carrying the raw body rather than crashing on a missing
    key — that body is often the only diagnostic an operator has.
    """

    def __init__(self, message: str, *, raw_body: str | None = None, status: int | None = None) -> None:
        super().__init__(message)
        self.raw_body = raw_body
        self.status = status


class NoCredentialError(AuthError):
    """Nothing is stored for this issuer/client; the caller must ``login()``."""


class RefreshFailedError(AuthError):
    """A refresh was rejected with ``invalid_grant``.

    Terminal by specification: the refresh token is spent or revoked under
    rotation, so the stored credential is cleared and a fresh login is
    required. Retrying cannot succeed.
    """


class CredentialPermissionError(AuthError):
    """An existing credentials file is readable by users other than its owner.

    The store refuses to read it rather than silently using a credential other
    local users can see.
    """
