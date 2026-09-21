"""RFC 8628 Device Authorization Flow client — protocol only, no terminal UI.

This package ships the **protocol half** of the device grant: the polling state
machine, token lifecycle (expiry, refresh, persistence), and a portable storage
protocol. The presentation half — displaying the user code, opening a browser,
rendering a spinner — belongs to the consumer and is reached through callbacks.

**The toolkit never writes to a terminal.** No ``print``, no spinner, no
colour, no browser launch. A library that writes to stdout cannot be used by a
daemon, a GUI, or a test.

It also produces *tokens, not identities*. The access token is opaque: it is
never decoded, and no ``Identity`` is constructed from it. Only the party
holding the verification key can produce one honestly, and a client that parses
its own JWT has produced a self-asserted claim wearing the costume of a
verified one.

Usage::

    from apcore_toolkit.auth import DeviceAuthClient, DeviceAuthConfig, FileTokenStore

    config = DeviceAuthConfig(issuer="https://auth.example.com", client_id="apcore-cli",
                              scope=["openid", "api.read"])
    config = config.discover()          # explicit network step, never implicit

    client = DeviceAuthClient(config, store=FileTokenStore())
    tokens = client.login(on_user_code=lambda **event: show(event))
    headers = client.as_auth_header_factory()
"""

from apcore_toolkit.auth.builder import (
    basic_auth_header,
    build_device_request,
    build_refresh_request,
    build_revoke_request,
    build_token_request,
    decode_response,
)
from apcore_toolkit.auth.client import DeviceAuthClient
from apcore_toolkit.auth.config import (
    CLIENT_AUTH_METHODS,
    DEVICE_CODE_GRANT_TYPE,
    DeviceAuthConfig,
    discovery_candidates,
)
from apcore_toolkit.auth.errors import (
    AuthError,
    AuthorizationDeniedError,
    AuthorizationExpiredError,
    AuthorizationProtocolError,
    ConfigurationError,
    CredentialPermissionError,
    DiscoveryError,
    NoCredentialError,
    RefreshFailedError,
    TransportError,
)
from apcore_toolkit.auth.grant import (
    DEFAULT_DEVICE_EXPIRES_IN,
    SLOW_DOWN_INCREMENT,
    DeviceCodeGrant,
    DeviceCodeResponse,
    Grant,
    classify,
    effective_deadline,
)
from apcore_toolkit.auth.parsing import (
    DEFAULT_FIELD_ALIASES,
    STANDARD_ERROR_IDENTIFIERS,
    normalise_fields,
    parse_body,
)
from apcore_toolkit.auth.store import (
    FileTokenStore,
    MemoryTokenStore,
    TokenStore,
    default_credentials_path,
    store_key,
)
from apcore_toolkit.auth.tokens import DEFAULT_SKEW_SECONDS, REDACTED, TokenSet
from apcore_toolkit.auth.transport import (
    ENCODINGS,
    REQUEST_KINDS,
    HttpResponse,
    HttpxTransport,
    PreparedRequest,
    Transport,
    encode_body,
)

__all__ = [
    "AuthError",
    "AuthorizationDeniedError",
    "AuthorizationExpiredError",
    "AuthorizationProtocolError",
    "CLIENT_AUTH_METHODS",
    "ConfigurationError",
    "CredentialPermissionError",
    "DEFAULT_DEVICE_EXPIRES_IN",
    "DEFAULT_FIELD_ALIASES",
    "DEFAULT_SKEW_SECONDS",
    "DEVICE_CODE_GRANT_TYPE",
    "DeviceAuthClient",
    "DeviceAuthConfig",
    "DeviceCodeGrant",
    "DeviceCodeResponse",
    "DiscoveryError",
    "ENCODINGS",
    "FileTokenStore",
    "Grant",
    "HttpResponse",
    "HttpxTransport",
    "MemoryTokenStore",
    "NoCredentialError",
    "PreparedRequest",
    "REDACTED",
    "REQUEST_KINDS",
    "RefreshFailedError",
    "SLOW_DOWN_INCREMENT",
    "STANDARD_ERROR_IDENTIFIERS",
    "TokenSet",
    "TokenStore",
    "Transport",
    "TransportError",
    "basic_auth_header",
    "build_device_request",
    "build_refresh_request",
    "build_revoke_request",
    "build_token_request",
    "classify",
    "decode_response",
    "default_credentials_path",
    "discovery_candidates",
    "effective_deadline",
    "encode_body",
    "normalise_fields",
    "parse_body",
    "store_key",
]
