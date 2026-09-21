"""``DeviceAuthConfig`` — the provider-compatibility surface, plus discovery.

RFC 8628 fixes the *shape* of the flow, not the URLs, not the extra parameters
each vendor demands, and not the response encoding. **No endpoint is hard-coded
anywhere in the toolkit**, and no vendor appears by name: every knob below
exists because a real, widely-deployed provider requires it, and a consumer
that wants a named-provider preset builds it in its own configuration layer
where it can be corrected without a toolkit release.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlsplit

from apcore_toolkit.auth.errors import (
    INSECURE_ENDPOINT,
    ISSUER_MISMATCH,
    NO_METADATA,
    ConfigurationError,
    DiscoveryError,
    TransportError,
)
from apcore_toolkit.auth.parsing import STANDARD_ERROR_IDENTIFIERS, parse_body
from apcore_toolkit.auth.transport import (
    ENCODINGS,
    REQUEST_KINDS,
    HttpxTransport,
    PreparedRequest,
    Transport,
)

logger = logging.getLogger("apcore_toolkit")

#: RFC 8628's grant type, sent verbatim in the token request.
DEVICE_CODE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"

#: The short form at least one provider advertises in ``grant_types_supported``
#: while still requiring the full URN in the actual token request.
DEVICE_CODE_GRANT_SHORT = "device_code"

#: Accepted client authentication methods. More exotic schemes
#: (``private_key_jwt``, DPoP) belong in ``transform_request``: they involve
#: signing and per-request proof construction, and enumerating them here would
#: mean a toolkit release per scheme.
CLIENT_AUTH_METHODS: frozenset[str] = frozenset({"none", "client_secret_post", "client_secret_basic"})

#: Defaults match RFC 6749, so a conforming provider needs no configuration.
DEFAULT_REQUEST_ENCODING: dict[str, str] = {kind: "form" for kind in REQUEST_KINDS}

_WELL_KNOWN_OAUTH = "oauth-authorization-server"
_WELL_KNOWN_OIDC = "openid-configuration"


def discovery_candidates(issuer: str) -> list[str]:
    """The three well-known URLs to try, in order.

    RFC 8414 **inserts** its suffix between host and path; OpenID Connect
    Discovery 1.0 **appends** it. For an issuer with no path the two collapse
    to the same shape, which is why the difference goes unnoticed until a
    tenant- or realm-scoped issuer appears — exactly the layout multi-tenant
    providers use.

    Step 3 is the backward-compatibility fallback for servers deployed against
    the older OIDC-only convention.
    """
    parts = urlsplit(issuer)
    origin = f"{parts.scheme}://{parts.netloc}"
    path = parts.path.rstrip("/")
    base = f"{origin}{path}"
    return [
        f"{origin}/.well-known/{_WELL_KNOWN_OAUTH}{path}",
        f"{origin}/.well-known/{_WELL_KNOWN_OIDC}{path}",
        f"{base}/.well-known/{_WELL_KNOWN_OIDC}",
    ]


def _is_https_or_localhost(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and parts.hostname in {"localhost", "127.0.0.1", "::1"}


@dataclass(frozen=True)
class DeviceAuthConfig:
    """Everything the client needs to reach one authorization server.

    Beyond ``client_id`` and a way to reach the endpoints, every field has a
    working default: a conforming provider needs three lines of configuration
    while a non-conforming one stays reachable without patching the toolkit.
    """

    client_id: str

    # Endpoints. Explicit values always win over discovered ones, so a
    # compromised or misconfigured discovery document cannot silently redirect
    # a token request.
    issuer: str | None = None
    device_authorization_endpoint: str | None = None
    token_endpoint: str | None = None
    revocation_endpoint: str | None = None

    # Client authentication — how, and (implicitly) on both endpoints.
    client_secret: str | None = None
    client_auth_method: str = "none"

    # Scope. RFC 8628 marks it OPTIONAL, yet at least one provider rejects a
    # device request without it, so sending a non-empty scope is the safer
    # default. A minority of providers expect commas rather than the
    # RFC-mandated space.
    scope: list[str] = field(default_factory=list)
    scope_separator: str = " "

    # Static per-provider additions.
    extra_device_params: dict[str, str] = field(default_factory=dict)
    extra_token_params: dict[str, str] = field(default_factory=dict)
    extra_headers: dict[str, str] = field(default_factory=dict)

    # Normalisation tables.
    error_aliases: dict[str, str] = field(default_factory=dict)
    field_aliases: dict[str, list[str]] = field(default_factory=dict)

    # Timing and wire format.
    default_interval: int = 5
    http_timeout: float | None = None
    request_encoding: dict[str, str] = field(default_factory=dict)

    # Extension hooks. All optional; omitting every one yields the fully
    # specified default behaviour the conformance corpus asserts.
    transform_request: Callable[[str, dict[str, str], dict[str, str]], tuple[Any, Any]] | None = None
    parse_response: Callable[[str, int, str | None, str], Mapping[str, Any] | None] | None = None
    classify_error: Callable[[Mapping[str, Any]], str | None] | None = None
    http_client: Any = None

    def __post_init__(self) -> None:
        if not self.client_id:
            raise ConfigurationError("client_id is required")

        if self.client_auth_method not in CLIENT_AUTH_METHODS:
            raise ConfigurationError(
                f"client_auth_method must be one of {sorted(CLIENT_AUTH_METHODS)}, got {self.client_auth_method!r}"
            )
        if self.client_auth_method != "none" and not self.client_secret:
            raise ConfigurationError(f"client_auth_method={self.client_auth_method!r} requires a client_secret")

        # Aliases may only map ONTO the four standard identifiers; allowing new
        # targets would let configuration introduce states the state machine has
        # no branch for.
        for source, target in self.error_aliases.items():
            if target not in STANDARD_ERROR_IDENTIFIERS:
                raise ConfigurationError(
                    f"error_aliases[{source!r}] = {target!r} is not one of the RFC 8628 identifiers "
                    f"{sorted(STANDARD_ERROR_IDENTIFIERS)}; aliases may map onto a standard identifier "
                    f"but may never invent a new one"
                )

        encoding = dict(DEFAULT_REQUEST_ENCODING)
        for kind, value in self.request_encoding.items():
            if kind not in REQUEST_KINDS:
                raise ConfigurationError(f"request_encoding key {kind!r} is not one of {list(REQUEST_KINDS)}")
            if value not in ENCODINGS:
                raise ConfigurationError(f"request_encoding[{kind!r}] must be 'form' or 'json', got {value!r}")
            encoding[kind] = value
        object.__setattr__(self, "request_encoding", encoding)

        if self.default_interval <= 0:
            raise ConfigurationError(f"default_interval must be positive, got {self.default_interval!r}")

        # A plaintext token endpoint is refused at construction time, not at
        # request time.
        for name in ("device_authorization_endpoint", "token_endpoint", "revocation_endpoint"):
            url = getattr(self, name)
            if url is not None and not _is_https_or_localhost(url):
                raise ConfigurationError(
                    f"{name} must be https:// (http:// is permitted only for localhost); got {url!r}"
                )

        if self.issuer is None and self.token_endpoint is None:
            raise ConfigurationError("configure either issuer (for discovery) or explicit endpoints")

    # -- derived -----------------------------------------------------------

    @property
    def store_key(self) -> str:
        """``"<issuer>|<client_id>"``.

        The issuer identifies the authorization server that minted the
        credential, so credentials are never reused across a change of server.
        When only explicit endpoints are configured there is no issuer to key
        on and the token endpoint stands in for it — it identifies the same
        server, just less canonically.
        """
        from apcore_toolkit.auth.store import store_key

        return store_key(self.issuer or self.token_endpoint or "", self.client_id)

    def encoding_for(self, kind: str) -> str:
        return self.request_encoding.get(kind, "form")

    def joined_scope(self) -> str | None:
        return self.scope_separator.join(self.scope) if self.scope else None

    # -- discovery ---------------------------------------------------------

    def discover(self, *, transport: Transport | None = None) -> DeviceAuthConfig:
        """Resolve endpoints from the issuer's metadata document.

        Network I/O, and therefore a **separate, explicit step** — never a
        hidden fetch inside ``login()``. A caller supplying endpoints
        explicitly performs no network access before the flow starts.

        Returns a new config; ``self`` is unchanged.
        """
        if not self.issuer:
            raise ConfigurationError("discover() requires an issuer")
        sender = transport or HttpxTransport(self.http_client, timeout=self.http_timeout)

        for index, url in enumerate(discovery_candidates(self.issuer), start=1):
            try:
                response = sender.send(
                    PreparedRequest(
                        kind="discovery",
                        url=url,
                        method="GET",
                        headers={"Accept": "application/json"},
                        encoding="form",
                    )
                )
            except TransportError as exc:
                logger.debug("discovery candidate %s unreachable: %s", url, exc)
                continue

            document = self._metadata_document(response.status, response.content_type, response.body)
            if document is None:
                # HTTP 200 does not mean you found metadata: a surveyed
                # provider serves an HTML single-page application at a
                # well-known path. Advancing on status alone accepts it, fails
                # while parsing, and never tries the remaining candidates.
                logger.debug("discovery candidate %s did not yield a metadata document", url)
                continue

            # Compare issuers as EXACT strings. No case folding, no default
            # port, no trailing-slash handling, no percent-encoding
            # normalisation — a general-purpose "normalise URL" helper is
            # exactly the reflex that weakens this check.
            if document.get("issuer") != self.issuer:
                raise DiscoveryError(
                    f"metadata at {url} declares issuer {document.get('issuer')!r}, "
                    f"which is not the configured issuer {self.issuer!r}; refusing to use it",
                    reason=ISSUER_MISMATCH,
                )
            logger.debug("discovery accepted candidate %d (%s)", index, url)
            return self._with_metadata(document)

        raise DiscoveryError(
            f"no authorization-server metadata found for issuer {self.issuer!r}",
            reason=NO_METADATA,
        )

    @staticmethod
    def _metadata_document(status: int, content_type: str | None, body: str) -> dict[str, Any] | None:
        """A candidate succeeds only on 2xx + a JSON object carrying metadata fields."""
        if not 200 <= status < 300:
            return None
        parsed = parse_body(content_type, body)
        if not isinstance(parsed, dict):
            return None
        if "issuer" not in parsed:
            return None
        return parsed

    def _with_metadata(self, document: Mapping[str, Any]) -> DeviceAuthConfig:
        """Merge a validated metadata document, explicit configuration winning."""
        self._check_grant_support(document.get("grant_types_supported"))

        device_endpoint = self.device_authorization_endpoint or _as_url(document.get("device_authorization_endpoint"))
        token_endpoint = self.token_endpoint or _as_url(document.get("token_endpoint"))
        revocation_endpoint = self.revocation_endpoint or _as_url(document.get("revocation_endpoint"))

        if device_endpoint is None:
            raise ConfigurationError(
                f"the metadata for {self.issuer!r} omits device_authorization_endpoint "
                f"(it is OPTIONAL in RFC 8414); set device_authorization_endpoint explicitly"
            )
        if token_endpoint is None:
            raise ConfigurationError(
                f"the metadata for {self.issuer!r} omits token_endpoint; set token_endpoint explicitly"
            )

        for name, url in (
            ("device_authorization_endpoint", device_endpoint),
            ("token_endpoint", token_endpoint),
            ("revocation_endpoint", revocation_endpoint),
        ):
            if url is None:
                continue
            # The hard half of the endpoint rule: a plaintext endpoint is
            # refused outright, because a token sent over http:// is a token
            # given away.
            if not _is_https_or_localhost(url):
                raise DiscoveryError(f"discovered {name} {url!r} is not https", reason=INSECURE_ENDPOINT)
            # The soft half: a *different origin* is warned about and then
            # followed. Refusing it — which an earlier reading of the spec
            # required — breaks real providers that host the token endpoint on
            # a separate host, and contradicts the explicit-override case.
            if self.issuer and urlsplit(url).netloc != urlsplit(self.issuer).netloc:
                logger.warning(
                    "discovered %s (%s) is not on the issuer's origin (%s); using it anyway",
                    name,
                    url,
                    self.issuer,
                )

        return replace(
            self,
            device_authorization_endpoint=device_endpoint,
            token_endpoint=token_endpoint,
            revocation_endpoint=revocation_endpoint,
        )

    def _check_grant_support(self, advertised: Any) -> None:
        """Advisory, permissive capability detection.

        Never the sole reason to refuse to start: one provider advertises the
        bare ``device_code`` token rather than the URN while still requiring
        the URN in the request, and another omits ``grant_types_supported``
        entirely — so its absence proves nothing. A provider under-reporting
        its grants is more common than one that genuinely cannot do device
        flow, and the server's own rejection is more authoritative than a
        guess made from its advertisement.
        """
        if advertised is None:
            return
        if not isinstance(advertised, Sequence) or isinstance(advertised, str):
            logger.warning("grant_types_supported is not a list; proceeding anyway")
            return
        values = {str(item) for item in advertised}
        if DEVICE_CODE_GRANT_TYPE in values or DEVICE_CODE_GRANT_SHORT in values:
            return
        logger.warning(
            "authorization server %s advertises grant_types_supported=%s, which lists neither %r nor %r; "
            "proceeding anyway because the metadata may simply be incomplete",
            self.issuer,
            sorted(values),
            DEVICE_CODE_GRANT_TYPE,
            DEVICE_CODE_GRANT_SHORT,
        )


def _as_url(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
