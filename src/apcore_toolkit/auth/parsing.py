"""Response decoding and field/error normalisation.

Everything here is pure: bytes in, mapping out. That is deliberate — this is
the *serialisation* layer of the spec's layering rule, where a vendor quirk is
a parsing problem rather than a state-machine problem, and where the
``parse_response`` hook is therefore allowed to run.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import parse_qsl

#: The four identifiers RFC 8628 §3.5 defines. The state machine dispatches on
#: these and nothing else; aliases and hooks may map *onto* them but can never
#: invent a fifth.
STANDARD_ERROR_IDENTIFIERS: frozenset[str] = frozenset(
    {
        "authorization_pending",
        "slow_down",
        "access_denied",
        "expired_token",
    }
)

#: Logical field -> accepted response field names, in priority order. The
#: standard name is always tried first, so a conforming provider is
#: unaffected; ``field_aliases`` in the configuration extends these lists.
DEFAULT_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "verification_uri": ("verification_uri", "verification_url"),
    "verification_uri_complete": ("verification_uri_complete", "verification_url_complete"),
    "error": ("error", "error_code"),
}


def _try_json(raw_body: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(raw_body)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _try_form(raw_body: str) -> dict[str, Any] | None:
    """Parse ``a=1&b=2``.

    Requires at least one key/value pair: ``parse_qsl`` happily returns an
    empty list for arbitrary text (an HTML document, for instance), and
    treating that as a successful parse is exactly how a ``200`` serving an
    SPA gets mistaken for metadata.
    """
    try:
        pairs = parse_qsl(raw_body, keep_blank_values=True, strict_parsing=False)
    except ValueError:
        return None
    if not pairs:
        return None
    return dict(pairs)


def parse_body(content_type: str | None, raw_body: str) -> dict[str, Any] | None:
    """Decode a response body into a mapping, or ``None`` if it is not one.

    The client sends ``Accept: application/json`` on every request *and* falls
    back to form-urlencoded parsing, because some providers return
    form-encoded by default and are otherwise perfectly conforming.
    """
    if raw_body is None:
        return None
    media_type = (content_type or "").split(";")[0].strip().lower()
    if "json" in media_type:
        return _try_json(raw_body) or _try_form(raw_body)
    if media_type == "application/x-www-form-urlencoded":
        return _try_form(raw_body) or _try_json(raw_body)
    return _try_json(raw_body) or _try_form(raw_body)


def _accepted_names(
    logical_field: str,
    field_aliases: Mapping[str, Sequence[str]] | None,
) -> tuple[str, ...]:
    names = list(DEFAULT_FIELD_ALIASES.get(logical_field, (logical_field,)))
    for extra in (field_aliases or {}).get(logical_field, ()):  # configuration extends, never replaces
        if extra not in names:
            names.append(extra)
    return tuple(names)


def normalise_fields(
    body: Mapping[str, Any],
    field_aliases: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """Copy ``body``, writing each aliased field under its canonical name.

    Unknown fields are **kept, never rejected** — providers add proprietary
    fields (a localised prompt, a creation timestamp) to both responses, and a
    strict parser that fails on unrecognised keys breaks against a perfectly
    functional server.
    """
    normalised = dict(body)
    for logical_field in {*DEFAULT_FIELD_ALIASES, *(field_aliases or {})}:
        if logical_field in normalised and normalised[logical_field] is not None:
            continue
        for candidate in _accepted_names(logical_field, field_aliases):
            value = body.get(candidate)
            if value is not None:
                normalised[logical_field] = value
                break
    return normalised


def raw_error_identifier(
    body: Mapping[str, Any],
    field_aliases: Mapping[str, Sequence[str]] | None = None,
) -> str | None:
    """The provider's own error identifier, before aliasing."""
    for candidate in _accepted_names("error", field_aliases):
        value = body.get(candidate)
        if isinstance(value, str) and value:
            return value
    return None


def apply_error_aliases(identifier: str | None, error_aliases: Mapping[str, str] | None) -> str | None:
    """Map a provider identifier onto a standard one.

    Aliasing is data rather than code so that the state machine's logic — and
    its conformance corpus — stay defined purely over the four RFC
    identifiers, while a consumer adapts to a non-conforming provider without
    waiting for a toolkit release.
    """
    if identifier is None:
        return None
    return (error_aliases or {}).get(identifier, identifier)
