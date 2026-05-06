"""HTTP proxy registry writer.

Registers scanned modules as HTTP proxy classes that forward requests
to a running web API. This enables CLI execution without invoking
route handlers directly (which depend on framework DI systems).

Requires the ``httpx`` optional dependency::

    pip install apcore-toolkit[http-proxy]

Usage::

    from apcore_toolkit.output.http_proxy_writer import HTTPProxyRegistryWriter

    writer = HTTPProxyRegistryWriter(
        base_url="http://localhost:8000",
        auth_header_factory=lambda: {"Authorization": "Bearer xxx"},
    )
    results = writer.write(modules, registry)

The writer reads ``http_method`` and ``url_path`` from each
``ScannedModule.metadata`` dict (the framework-agnostic convention).
Framework-specific ``ScannedModule`` subclasses with top-level
``http_method`` / ``url_path`` attributes are also supported.
"""

import logging
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from apcore import DEFAULT_ANNOTATIONS, ErrorCodes, Registry
from apcore.errors import ModuleError
from apcore_toolkit.http_verb_map import extract_path_param_names, substitute_path_params
from apcore_toolkit.output.types import WriteResult
from apcore_toolkit.types import ScannedModule

logger = logging.getLogger("apcore_toolkit")

# Methods that conventionally carry a JSON body. Other methods (GET,
# HEAD, DELETE, OPTIONS) forward non-path inputs via the query string
# so they are not silently dropped.
_BODY_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH"})

# Matches any unfilled placeholder ({name} or :name) left after substitution.
_UNFILLED_PARAM_RE: re.Pattern[str] = re.compile(r"\{[^}]+\}|:[a-zA-Z_]\w*")


def _percent_encode_path_segment(value: str) -> str:
    """Percent-encode a path-parameter value per RFC 3986 unreserved set.

    Mirrors Rust's ``percent_encode_path_segment`` so a path-param value
    containing ``/``, ``?``, ``#``, ``%``, or whitespace cannot break the
    request URL. ``safe=""`` keeps only ALPHA / DIGIT / ``-`` / ``.`` /
    ``_`` / ``~`` literal — the unreserved set.
    """
    return quote(value, safe="")


def _get_http_fields(mod: Any) -> tuple[str, str]:
    """Extract http_method and url_path from a ScannedModule.

    Supports both:
    - Toolkit ScannedModule (fields in ``metadata`` dict)
    - Framework-specific ScannedModule (top-level attributes)
    """
    metadata = (mod.metadata or {}) if hasattr(mod, "metadata") else {}
    http_method = getattr(mod, "http_method", None)
    if not http_method:
        http_method = metadata.get("http_method", "GET")
    url_path = getattr(mod, "url_path", None)
    if not url_path:
        url_path = metadata.get("url_path", "/") or "/"
    url_path = str(url_path)
    if url_path.startswith(("http://", "https://", "file://", "ftp://")):
        raise ValueError(
            f"url_path must be a relative path, not an absolute URL: {url_path!r} " f"(module: {mod.module_id!r})"
        )
    return str(http_method), url_path


class HTTPProxyRegistryWriter:
    """Register scanned modules as HTTP proxy classes in the registry.

    Each module's ``execute()`` sends an HTTP request to the target API
    instead of calling the route handler directly.

    Args:
        base_url: Base URL of the target API (e.g. ``http://localhost:8000``).
        auth_header_factory: Optional callable returning HTTP headers for
            authentication (e.g. ``{"Authorization": "Bearer xxx"}``).
            Called once per request.
        timeout: HTTP request timeout in seconds.
    """

    def __init__(
        self,
        base_url: str,
        auth_header_factory: Callable[[], dict[str, str]] | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._base_url = base_url
        self._auth_header_factory = auth_header_factory
        self._timeout = timeout

    def write(
        self,
        modules: list[ScannedModule],
        registry: Registry,
    ) -> list[WriteResult]:
        """Register each ScannedModule as an HTTP proxy module."""
        results: list[WriteResult] = []
        for mod in modules:
            try:
                module_instance = self._build_module_class(mod)()
                registry.register(mod.module_id, module_instance)
                results.append(WriteResult(module_id=mod.module_id, path=None, verified=True))
            except Exception as exc:
                logger.warning(
                    "HTTPProxyRegistryWriter: failed to register %s",
                    mod.module_id,
                    exc_info=True,
                )
                results.append(
                    WriteResult(
                        module_id=mod.module_id,
                        path=None,
                        verified=False,
                        verification_error=f"{type(exc).__name__}: {exc}",
                    )
                )
        return results

    def _build_module_class(self, mod: ScannedModule) -> type:
        """Build a module class with HTTP proxy execute method."""
        http_method, url_path = _get_http_fields(mod)
        # Recognise both brace-style ({id}) and colon-style (:id) params,
        # delegating to the canonical regex in http_verb_map so the two
        # conventions do not drift apart.
        path_params = extract_path_param_names(url_path)
        base_url = self._base_url
        auth_factory = self._auth_header_factory
        timeout = self._timeout

        annotations = mod.annotations or DEFAULT_ANNOTATIONS

        # Raw dict schemas are auto-wrapped by Registry._ensure_schema_adapter
        # (apcore >= 0.13.1) during register(), so no manual wrapping needed.
        raw_input = dict(mod.input_schema or {})
        raw_output = dict(mod.output_schema or {})

        class ProxyModule:
            input_schema = raw_input
            output_schema = raw_output
            description = mod.description
            documentation = mod.documentation
            # Shared client — created lazily on first execute(), reused across calls
            # for connection pooling. Each ProxyModule class has its own client
            # bound to its base_url/timeout.
            _client: Any = None

            async def execute(self, inputs: dict[str, Any], ctx: Any = None) -> dict[str, Any]:
                import httpx as _httpx

                if ProxyModule._client is None:
                    ProxyModule._client = _httpx.AsyncClient(base_url=base_url, timeout=timeout)

                headers: dict[str, str] = {}
                if auth_factory is not None:
                    auth_result = auth_factory()
                    if not isinstance(auth_result, dict):
                        raise TypeError(
                            f"auth_header_factory must return dict[str, str], got {type(auth_result).__name__}"
                        )
                    headers.update(auth_result)

                # Separate path params from the rest; percent-encode each
                # path-param value (RFC 3986 unreserved set) BEFORE
                # substitution so values containing "/", "?", "#", "%", or
                # whitespace cannot corrupt the URL. Mirrors the Rust
                # implementation's percent_encode_path_segment.
                path_values = {k: _percent_encode_path_segment(str(v)) for k, v in inputs.items() if k in path_params}
                actual_path = substitute_path_params(url_path, path_values)
                # Reject the request if any placeholder went unfilled —
                # otherwise a forgotten input would silently leak `{name}`
                # into the request URL.
                unfilled = _UNFILLED_PARAM_RE.search(actual_path)
                if unfilled is not None:
                    raise ModuleError(
                        code=ErrorCodes.MODULE_EXECUTE_ERROR,
                        message=(f"path parameter not provided: {unfilled.group(0)!r} " f"in url_path {url_path!r}"),
                        details={"url_path": url_path, "missing": unfilled.group(0)},
                    )
                non_path = {k: v for k, v in inputs.items() if k not in path_params}

                # Body for POST/PUT/PATCH; query for everything else
                # (GET/DELETE/HEAD/OPTIONS) so non-path inputs are never
                # silently dropped.
                kwargs: dict[str, Any] = {}
                if non_path:
                    if http_method in _BODY_METHODS:
                        kwargs["json"] = non_path
                    else:
                        if any(isinstance(v, (dict, list)) for v in non_path.values()):
                            logger.warning(
                                "HTTPProxyRegistryWriter: %s %s has nested dict/list params "
                                "that will be str()-serialized in the query string",
                                http_method,
                                url_path,
                            )
                        kwargs["params"] = non_path

                try:
                    resp = await ProxyModule._client.request(http_method, actual_path, headers=headers, **kwargs)
                except _httpx.HTTPError as exc:
                    raise ModuleError(
                        code=ErrorCodes.MODULE_EXECUTE_ERROR,
                        message=f"HTTP transport error: {exc}",
                        details={},
                    ) from exc

                if 200 <= resp.status_code < 300:
                    if resp.status_code == 204:
                        return {}
                    try:
                        body = resp.json()
                    except ValueError as exc:
                        raise ModuleError(
                            code=ErrorCodes.MODULE_EXECUTE_ERROR,
                            message=f"HTTP {resp.status_code}: invalid JSON in response body",
                            details={"http_status": resp.status_code},
                        ) from exc
                    if not isinstance(body, dict):
                        raise ModuleError(
                            code=ErrorCodes.MODULE_EXECUTE_ERROR,
                            message=f"HTTP {resp.status_code}: expected JSON object, got {type(body).__name__}",
                            details={"http_status": resp.status_code},
                        )
                    return body

                error_msg = _extract_error_message(resp)
                raise ModuleError(
                    code=ErrorCodes.MODULE_EXECUTE_ERROR,
                    message=f"HTTP {resp.status_code}: {error_msg}",
                    details={"http_status": resp.status_code},
                )

        ProxyModule.__name__ = re.sub(r"\W", "_", mod.module_id)
        ProxyModule.__qualname__ = ProxyModule.__name__
        ProxyModule.annotations = annotations  # type: ignore[assignment]
        ProxyModule.tags = list(mod.tags if isinstance(mod.tags, list) else [])  # type: ignore[attr-defined]

        return ProxyModule


def _extract_error_message(resp: Any) -> str:
    """Extract a human-readable error message from an HTTP error response."""
    content_type = resp.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        try:
            body = resp.json()
            return (
                body.get("error_message")
                or body.get("detail")
                or body.get("error")
                or body.get("message")
                or resp.text[:200]
            )
        except (ValueError, AttributeError):
            # ValueError: JSONDecodeError (bad body despite content-type header)
            # AttributeError: resp.json() returned a non-dict
            logger.debug("_extract_error_message: could not parse JSON body — falling back to text")
    return resp.text[:200]
