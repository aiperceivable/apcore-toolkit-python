"""Outbound request construction — parameters, headers, client authentication.

Kept separate from the state machine because it is grant-independent: the same
builder serves the device request, the token request, and refresh, and would
serve a second grant unchanged.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote_plus

from apcore_toolkit.auth.config import DEVICE_CODE_GRANT_TYPE, DeviceAuthConfig
from apcore_toolkit.auth.errors import ConfigurationError
from apcore_toolkit.auth.parsing import normalise_fields, parse_body
from apcore_toolkit.auth.transport import HttpResponse, PreparedRequest, encode_body


def decode_response(config: DeviceAuthConfig, kind: str, response: HttpResponse) -> dict[str, Any] | None:
    """``parse_response`` hook, then the built-in parsers, then field aliasing.

    This is the spec's fixed order made concrete. A hook returning ``None``
    means "no opinion, use the default", so it can special-case one endpoint
    and ignore the rest; the mapping it returns must already use standard field
    names, because field-name aliasing has conceptually been applied by the
    time the state machine reads it.
    """
    parsed: Mapping[str, Any] | None = None
    if config.parse_response is not None:
        parsed = config.parse_response(kind, response.status, response.content_type, response.body)
    if parsed is None:
        parsed = parse_body(response.content_type, response.body)
    if parsed is None:
        return None
    return normalise_fields(parsed, config.field_aliases)


def basic_auth_header(client_id: str, client_secret: str) -> str:
    """``Basic base64(urlencode(client_id):urlencode(client_secret))``.

    RFC 6749 §2.3.1 requires the client identifier and secret to be
    **form-urlencoded before** they are joined and base64-encoded. It is easy to
    miss because the two readings are byte-identical for the alphanumeric
    credentials most examples use — they diverge only once a secret contains a
    space, ``+``, or ``:``, which is exactly when a wrong implementation starts
    failing against a real provider and nobody can see why.

    Some providers do not follow the RFC here. The toolkit picks the RFC and
    states it; a provider that wants raw concatenation is reachable through
    ``transform_request``.
    """
    pair = f"{quote_plus(client_id)}:{quote_plus(client_secret)}"
    return "Basic " + base64.b64encode(pair.encode()).decode("ascii")


def _apply_client_auth(config: DeviceAuthConfig, params: dict[str, str], headers: dict[str, str]) -> None:
    """Place client credentials per ``client_auth_method``.

    Applied to **both** the device authorization and token requests — the easy
    half to miss. A provider can reject an unauthenticated ``/device/authorize``
    call with ``invalid_client``, long before any token request happens.
    """
    # ``client_id`` is in the body under every method, including
    # ``client_secret_basic``.
    params["client_id"] = config.client_id
    if config.client_auth_method == "client_secret_post":
        params["client_secret"] = config.client_secret or ""
    elif config.client_auth_method == "client_secret_basic":
        headers["Authorization"] = basic_auth_header(config.client_id, config.client_secret or "")


def _finalise(
    config: DeviceAuthConfig,
    kind: str,
    url: str,
    params: dict[str, str],
    headers: dict[str, str],
) -> PreparedRequest:
    """Run ``transform_request`` and encode the body.

    The hook runs immediately before each outbound request and receives **no
    URL**: request targeting stays with configuration and discovery, because a
    hook able to redirect the token request is a hook able to exfiltrate
    credentials.

    Unlike the observational callbacks, this hook is load-bearing — an
    exception propagates and fails the flow rather than being swallowed.
    """
    if config.transform_request is not None:
        result = config.transform_request(kind, params, headers)
        params, headers = _unpack_transform_result(result, params, headers)

    encoding = config.encoding_for(kind)
    content_type, body = encode_body(params, encoding)
    headers = {"Content-Type": content_type, **headers}
    return PreparedRequest(
        kind=kind,
        url=url,
        method="POST",
        params=params,
        headers=headers,
        encoding=encoding,
        body=body,
    )


def _unpack_transform_result(
    result: Any,
    params: dict[str, str],
    headers: dict[str, str],
) -> tuple[dict[str, str], dict[str, str]]:
    if result is None:
        return params, headers
    if not isinstance(result, tuple) or len(result) != 2:
        raise ConfigurationError(f"transform_request must return a (params, headers) pair, got {type(result).__name__}")
    new_params, new_headers = result
    if not isinstance(new_params, Mapping) or not isinstance(new_headers, Mapping):
        raise ConfigurationError("transform_request must return two mappings")
    return {str(k): str(v) for k, v in new_params.items()}, {str(k): str(v) for k, v in new_headers.items()}


def _base_headers(config: DeviceAuthConfig) -> dict[str, str]:
    # ``Accept: application/json`` is mandatory: some providers return
    # form-urlencoded unless the request asks otherwise.
    headers = {"Accept": "application/json"}
    headers.update(config.extra_headers)
    return headers


def build_device_request(config: DeviceAuthConfig) -> PreparedRequest:
    """``POST`` to the device authorization endpoint."""
    if not config.device_authorization_endpoint:
        raise ConfigurationError(
            "device_authorization_endpoint is not set; configure it explicitly or call config.discover()"
        )
    params: dict[str, str] = {}
    headers = _base_headers(config)
    _apply_client_auth(config, params, headers)
    scope = config.joined_scope()
    if scope is not None:
        params["scope"] = scope
    params.update(config.extra_device_params)
    return _finalise(config, "device", config.device_authorization_endpoint, params, headers)


def build_token_request(config: DeviceAuthConfig, device_code: str) -> PreparedRequest:
    """``POST`` to the token endpoint with the device-code grant."""
    if not config.token_endpoint:
        raise ConfigurationError("token_endpoint is not set; configure it explicitly or call config.discover()")
    params: dict[str, str] = {
        "grant_type": DEVICE_CODE_GRANT_TYPE,
        "device_code": device_code,
    }
    headers = _base_headers(config)
    _apply_client_auth(config, params, headers)
    params.update(config.extra_token_params)
    return _finalise(config, "token", config.token_endpoint, params, headers)


def build_refresh_request(config: DeviceAuthConfig, refresh_token: str) -> PreparedRequest:
    """``POST`` to the token endpoint with ``grant_type=refresh_token``."""
    if not config.token_endpoint:
        raise ConfigurationError("token_endpoint is not set; configure it explicitly or call config.discover()")
    params: dict[str, str] = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    headers = _base_headers(config)
    _apply_client_auth(config, params, headers)
    params.update(config.extra_token_params)
    return _finalise(config, "refresh", config.token_endpoint, params, headers)


def build_revoke_request(config: DeviceAuthConfig, token: str, token_type_hint: str | None = None) -> PreparedRequest:
    """``POST`` to the revocation endpoint (RFC 7009), where the provider has one."""
    if not config.revocation_endpoint:
        raise ConfigurationError("revocation_endpoint is not set; this provider may not implement RFC 7009")
    params: dict[str, str] = {"token": token}
    if token_type_hint:
        params["token_type_hint"] = token_type_hint
    headers = _base_headers(config)
    _apply_client_auth(config, params, headers)
    return _finalise(config, "revoke", config.revocation_endpoint, params, headers)
