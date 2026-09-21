"""``TokenSet`` — the credential produced by a successful grant.

The access token is treated as **opaque**: it is never decoded, and expiry is
read from the token response's ``expires_in`` rather than from any ``exp``
claim inside it. A client cannot verify its own token, so it must not pretend
to have done so (see the spec's "this produces tokens, not identities").
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from apcore_toolkit.auth.errors import AuthorizationProtocolError

#: What a token value is replaced with in every debug string. Fixed rather
#: than a truncated prefix: a prefix is still credential material.
REDACTED = "***REDACTED***"

#: Default clock skew for :meth:`TokenSet.is_expired`. A token is treated as
#: expired slightly *before* it really is, so a request is not dispatched with
#: a credential that will expire in flight.
DEFAULT_SKEW_SECONDS = 30


def coerce_int(value: Any) -> int | None:
    """Best-effort integer coercion.

    Form-urlencoded responses deliver every field as a string, so
    ``expires_in`` arrives as ``"3600"`` there and as ``3600`` in JSON. Both
    must produce the same ``TokenSet``.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _normalise_token_type(value: Any) -> str:
    """Servers vary between ``Bearer`` and ``bearer``; normalise the casing.

    Any other token type is passed through verbatim — normalising it would be
    guessing at a scheme the toolkit does not implement.
    """
    if not isinstance(value, str) or not value:
        return "Bearer"
    if value.lower() == "bearer":
        return "Bearer"
    return value


def _normalise_scope(value: Any) -> list[str]:
    """RFC 6749 encodes ``scope`` as a space-delimited string."""
    if value is None:
        return []
    if isinstance(value, str):
        return value.split()
    if isinstance(value, Sequence):
        return [str(item) for item in value]
    return []


@dataclass(frozen=True, repr=False)
class TokenSet:
    """An opaque bearer credential plus the metadata needed to manage it.

    ``expires_at`` is an absolute **wall-clock** instant rather than a
    duration, because a duration is meaningless after a process restart. The
    polling deadline uses a *monotonic* clock instead; the two serve different
    purposes and that difference is deliberate.
    """

    access_token: str
    token_type: str = "Bearer"
    expires_at: int | None = None
    refresh_token: str | None = None
    scope: list[str] = field(default_factory=list)
    obtained_at: int = 0

    def __repr__(self) -> str:
        """Redact both token values.

        A leaked debug log is the most common way CLI credentials escape, so
        this is a specification requirement asserted by conformance case
        ``device_auth_redaction_020`` — not a convention.
        """
        refresh = f"'{REDACTED}'" if self.refresh_token is not None else "None"
        return (
            f"TokenSet(access_token='{REDACTED}', token_type={self.token_type!r}, "
            f"expires_at={self.expires_at!r}, refresh_token={refresh}, "
            f"scope={self.scope!r}, obtained_at={self.obtained_at!r})"
        )

    __str__ = __repr__

    def is_expired(
        self,
        skew_seconds: int = DEFAULT_SKEW_SECONDS,
        *,
        now: Callable[[], float] | float | None = None,
    ) -> bool:
        """``True`` when ``now + skew >= expires_at``.

        A token with no stated expiry never auto-expires — the server simply
        did not say, and inventing a lifetime would discard a working
        credential.
        """
        if self.expires_at is None:
            return False
        if now is None:
            current = time.time()
        elif callable(now):
            current = float(now())
        else:
            current = float(now)
        return current + skew_seconds >= self.expires_at

    @classmethod
    def from_response(
        cls,
        body: Mapping[str, Any],
        *,
        now: float,
        raw_body: str | None = None,
    ) -> TokenSet:
        """Build a ``TokenSet`` from a parsed token-endpoint success payload.

        ``now`` is wall-clock seconds; ``expires_at`` is computed as
        ``now + expires_in`` at receipt.
        """
        access_token = body.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise AuthorizationProtocolError(
                "token response carried no access_token",
                raw_body=raw_body,
            )
        expires_in = coerce_int(body.get("expires_in"))
        refresh_token = body.get("refresh_token")
        return cls(
            access_token=access_token,
            token_type=_normalise_token_type(body.get("token_type")),
            expires_at=int(now) + expires_in if expires_in is not None else None,
            refresh_token=refresh_token if isinstance(refresh_token, str) and refresh_token else None,
            scope=_normalise_scope(body.get("scope")),
            obtained_at=int(now),
        )

    def to_dict(self) -> dict[str, Any]:
        """The persisted form. Field names are stable across SDKs."""
        return {
            "access_token": self.access_token,
            "token_type": self.token_type,
            "expires_at": self.expires_at,
            "refresh_token": self.refresh_token,
            "scope": list(self.scope),
            "obtained_at": self.obtained_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TokenSet:
        """Inverse of :meth:`to_dict`, tolerant of a sparse stored record."""
        access_token = data.get("access_token")
        if not isinstance(access_token, str):
            raise ValueError("stored credential has no access_token")
        refresh_token = data.get("refresh_token")
        return cls(
            access_token=access_token,
            token_type=_normalise_token_type(data.get("token_type")),
            expires_at=coerce_int(data.get("expires_at")),
            refresh_token=refresh_token if isinstance(refresh_token, str) and refresh_token else None,
            scope=_normalise_scope(data.get("scope")),
            obtained_at=coerce_int(data.get("obtained_at")) or 0,
        )
