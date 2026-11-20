"""HTTP verb semantic mapping utilities.

Provides the canonical mapping from HTTP methods to semantic verbs used
by scanner implementations when generating user-facing command aliases.
All functions are pure and do not raise exceptions.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = [
    "SCANNER_VERB_MAP",
    "extract_path_param_names",
    "generate_suggested_alias",
    "has_path_params",
    "resolve_http_verb",
    "substitute_path_params",
]


SCANNER_VERB_MAP: dict[str, str] = {
    "GET": "list",
    "GET_ID": "get",
    "POST": "create",
    "PUT": "update",
    "PATCH": "patch",
    "DELETE": "delete",
    "HEAD": "head",
    "OPTIONS": "options",
}
"""Canonical HTTP method to semantic verb mapping.

Keys are uppercase HTTP methods. ``GET_ID`` is a synthetic key used when
GET routes have path parameters (single-resource access). Values are
lowercase semantic verbs used by CLI and MCP surfaces. The mapping is
considered immutable by convention; downstream code MUST NOT mutate it.
"""


_PATH_PARAM_RE: re.Pattern[str] = re.compile(r"\{[^}]+\}|:[a-zA-Z_]\w*")

# Named-group regex used to extract parameter *names* (not the surrounding
# punctuation). Mirrors _PATH_PARAM_RE but binds the name for substitution.
_PATH_PARAM_NAMED_RE: re.Pattern[str] = re.compile(r"\{(?P<brace>[^}]+)\}|:(?P<colon>[a-zA-Z_]\w*)")


def has_path_params(path: str) -> bool:
    """Check if a URL path contains path parameter placeholders.

    Detects both brace-style ({param}) and colon-style (:param) parameters,
    covering all major web frameworks.

    Args:
        path: URL path string (e.g., "/tasks/{id}" or "/users/:userId").

    Returns:
        True if the path contains at least one path parameter, False otherwise.
    """
    return bool(_PATH_PARAM_RE.search(path))


def resolve_http_verb(method: str, has_path_params: bool) -> str:  # noqa: FBT001
    """Map an HTTP method to its semantic verb.

    GET is contextual: collection routes (no path params) map to "list",
    single-resource routes (with path params) map to "get". All other
    methods have a static mapping. Unknown methods fall through to
    the lowercase form of the input.

    Args:
        method: HTTP method string (case-insensitive).
        has_path_params: True if the corresponding route has path parameters.

    Returns:
        Semantic verb string (e.g., "create", "list", "get").
    """
    method_upper = method.upper()
    if method_upper == "GET":
        key = "GET_ID" if has_path_params else "GET"
        return SCANNER_VERB_MAP[key]
    return SCANNER_VERB_MAP.get(method_upper, method.lower())


def generate_suggested_alias(path: str, method: str) -> str:
    """Generate a dot-separated suggested alias from HTTP route info.

    The alias is built from non-parameter path segments joined with the
    resolved semantic verb. The output uses snake_case preserved from the
    path; surface adapters apply their own naming conventions (e.g., CLI
    converts underscores to hyphens).

    The GET-vs-list disambiguation checks whether the LAST path segment
    is a path parameter (single-resource access) rather than whether the
    path contains any parameters anywhere. This correctly treats nested
    collection endpoints like ``/orgs/{org_id}/members`` as "list".

    Examples:
        POST   /tasks/user_data                -> "tasks.user_data.create"
        GET    /tasks/user_data                -> "tasks.user_data.list"
        GET    /tasks/user_data/{id}           -> "tasks.user_data.get"
        PUT    /tasks/user_data/{id}           -> "tasks.user_data.update"
        DELETE /tasks/user_data/{id}           -> "tasks.user_data.delete"
        GET    /orgs/{org_id}/members          -> "orgs.members.list"

    Args:
        path: URL path (e.g., "/tasks/user_data/{id}").
        method: HTTP method (e.g., "POST").

    Returns:
        Dot-separated alias string. If the path has no non-parameter
        segments, returns just the semantic verb (e.g., "list").
    """
    raw_segments = [seg for seg in path.strip("/").split("/") if seg]
    segments = [seg for seg in raw_segments if not _PATH_PARAM_RE.fullmatch(seg)]
    is_single_resource = bool(raw_segments) and bool(_PATH_PARAM_RE.fullmatch(raw_segments[-1]))
    verb = resolve_http_verb(method, is_single_resource)
    return ".".join([*segments, verb])


def extract_path_param_names(path: str) -> set[str]:
    """Return the set of parameter *names* declared in a URL path.

    Recognises both brace-style (``/users/{id}``) and colon-style
    (``/users/:id``) placeholders, so the output is consistent across
    frameworks that use different conventions.

    Args:
        path: URL path string.

    Returns:
        Set of parameter names with the surrounding punctuation stripped
        (e.g., ``{"id", "org_id"}``).
    """
    names: set[str] = set()
    for match in _PATH_PARAM_NAMED_RE.finditer(path):
        name = match.group("brace") or match.group("colon")
        if name:
            names.add(name)
    return names


def substitute_path_params(path: str, values: dict[str, Any]) -> str:
    """Substitute ``{name}`` and ``:name`` placeholders with ``values[name]``.

    Keys in ``values`` that do not appear as placeholders are ignored. A
    placeholder whose name is absent from ``values`` is left unchanged
    (callers can detect the leftover via :func:`extract_path_param_names`).

    Args:
        path: URL path with placeholders.
        values: Mapping from parameter name to substitution value.

    Returns:
        Path with all matching placeholders replaced by their values.
    """

    def _replace(match: re.Match[str]) -> str:
        name = match.group("brace") or match.group("colon") or ""
        if name in values:
            return str(values[name])
        return match.group(0)

    return _PATH_PARAM_NAMED_RE.sub(_replace, path)


_OPENAPI_PATH_PARAM_RE: re.Pattern[str] = re.compile(r"\{([^{}]+)\}")


def _effective_openapi_path(path: object) -> str | None:
    """Return an origin-relative OpenAPI path with well-formed placeholders."""
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        return None
    if any(character in path for character in ("?", "#", "\\")):
        return None
    literals = _OPENAPI_PATH_PARAM_RE.sub("", path)
    return None if "{" in literals or "}" in literals else path


def _openapi_path_param_names(path: str) -> set[str]:
    return set(_OPENAPI_PATH_PARAM_RE.findall(path))


def _substitute_openapi_path_params(path: str, values: dict[str, str]) -> str:
    return _OPENAPI_PATH_PARAM_RE.sub(lambda match: values.get(match[1], match[0]), path)
