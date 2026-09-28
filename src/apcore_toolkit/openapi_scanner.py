"""OpenAPIScanner — turn an OpenAPI 3.x document into ``ScannedModule`` list.

Document-level traversal layered on top of the shipped operation-level
primitives in :mod:`apcore_toolkit.openapi` (``extract_input_schema``,
``extract_output_schema``, ``deep_resolve_refs``) and
:func:`apcore_toolkit.scanner.BaseScanner.infer_annotations_from_method`.

See ``apcore-toolkit/docs/features/openapi-scanner.md`` for the full V1
specification, worked examples, and conformance corpus.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from apcore_toolkit.openapi import extract_input_schema, extract_output_schema, resolve_ref
from apcore_toolkit.scanner import BaseScanner
from apcore_toolkit.types import ScannedModule

__all__ = ["OpenAPIScanner", "derive_module_id", "load_spec"]

# Only these path-item keys are treated as HTTP operations (OpenAPI 3.x Path
# Item Object). Everything else (`summary`, `parameters`, `servers`, `$ref`,
# vendor `x-*` extensions, ...) is skipped.
_RECOGNIZED_METHODS: frozenset[str] = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})

# `normalize_module_id` patterns. Every character class is an ASCII range
# written out: Python's `\d`/`\w` and `re.IGNORECASE` are Unicode-aware (under
# IGNORECASE, `[a-z]` matches U+212A KELVIN SIGN), and the TypeScript and Rust
# ports must produce byte-identical output from the same algorithm.
# `([A-Z])`, not `([A-Z]+)`: the output is identical, but the `+` form makes
# `re` rescan a long run of capitals from every start position — quadratic
# time on a crafted operationId (40,000 capitals took ~10 s).
_ACRONYM_BOUNDARY_RE = re.compile(r"([A-Z])([A-Z][a-z])")
_WORD_BOUNDARY_RE = re.compile(r"([a-z0-9])([A-Z])")
_OUTSIDE_ID_ALPHABET_RE = re.compile(r"[^A-Za-z0-9_.]")
# Matched with `fullmatch`, not `^...$`: Python's `$` also matches just before
# a trailing "\n", which would accept a segment the spec's regex rejects.
_LEGAL_SEGMENT_RE = re.compile(r"[a-z][a-z0-9_]*")


def _normalize_module_id(candidate: str) -> str:
    """Normalise a module-id candidate into apcore's Canonical ID alphabet.

    Implements ``normalize_module_id`` from ``apcore-toolkit/docs/features/
    openapi-scanner.md`` § ``module_id`` Derivation, byte-for-byte:

    1. insert ``_`` at word boundaries — ``([A-Z])([A-Z][a-z])`` (acronym
       followed by a word) and ``([a-z0-9])([A-Z])``; the two replace-alls
       commute, so their order is immaterial;
    2. replace every code point outside ``[A-Za-z0-9_.]`` with one ``_``;
    3. lowercase — only ASCII remains, so this cannot reintroduce a
       non-ASCII letter (U+212A KELVIN SIGN would lowercase to ``k`` had it
       survived step 2);
    4. split on ``.``; strip leading ``_`` from each segment; drop empty
       segments; join the rest with ``.``.

    Invariant (MUST): a legal apcore ID is returned unchanged — every step
    is the identity on ``^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)*$``. That is
    why runs of ``_`` are kept (FastAPI's ``read_item_items__item_id__get``
    registers today) and a trailing ``_`` is not stripped here (a hook's
    legal ``abc_`` must survive the scanner's final pass); only
    :func:`derive_module_id`'s ``operationId`` branch strips it. Also
    idempotent and segment-local. The result can still be illegal — a
    segment beginning with a digit, or empty — because repairing either
    would invent a name; the scanner reports that with a warning.
    Private: the scanner owns the alphabet, and hooks choose only the words.
    """
    candidate = _ACRONYM_BOUNDARY_RE.sub(r"\1_\2", candidate)
    candidate = _WORD_BOUNDARY_RE.sub(r"\1_\2", candidate)
    candidate = _OUTSIDE_ID_ALPHABET_RE.sub("_", candidate)
    candidate = candidate.lower()
    segments = (seg.lstrip("_") for seg in candidate.split("."))
    return ".".join(seg for seg in segments if seg)


def _legality_warning(module_id: str) -> str | None:
    """Return the pinned warning if *module_id* is not a legal apcore ID.

    Checks only the alphabet the registries enforce (PROTOCOL_SPEC §2.7's
    pattern): the first dot-separated segment that is not a full match of
    ``^[a-z][a-z0-9_]*$`` is named — for an empty ID, the empty string.
    §2.7's no-``__`` clause, the length limit and reserved first segments
    are the registry's to enforce, not the scanner's.
    """
    for segment in module_id.split("."):
        if _LEGAL_SEGMENT_RE.fullmatch(segment) is None:
            return (
                f"module_id '{module_id}' is not a legal apcore module ID: segment '{segment}' "
                "must match ^[a-z][a-z0-9_]*$; name this operation with a derive_module_id "
                "or transform_module hook"
            )
    return None


def derive_module_id(path: str, method: str, operation: dict[str, Any]) -> str:
    """Derive a stable, byte-identical ``module_id`` for an OpenAPI operation.

    See ``apcore-toolkit/docs/features/openapi-scanner.md`` § ``module_id``
    Derivation for the algorithm and worked examples. This function is the
    primary subject of the cross-SDK conformance corpus — implementations
    MUST match it byte-for-byte.

    A non-empty ``operationId`` is converted to snake_case
    (``getUserById`` -> ``get_user_by_id``) and a trailing ``_`` stripped
    (``getUser_`` -> ``get_user``, as V1 did); apart from that trailing
    ``_``, one that is already a legal ID (FastAPI's
    ``read_item_items__item_id__get``) is used unchanged.
    Otherwise each path segment is normalised the same way and the
    lowercased method appended (``GET /user-profiles/{userId}`` ->
    ``user_profiles.user_id.get``); a path with no surviving segment
    (``/``, ``/-``, ``/{}``) yields ``root.<method>``.

    Args:
        path: The OpenAPI path template (e.g. ``"/users/{user_id}"``).
        method: The HTTP method key as written in the document (e.g. ``"get"``).
        operation: The operation object, consulted only for ``operationId``.

    Returns:
        The derived module ID in apcore's Canonical ID alphabet
        (``[a-z0-9_.]``). Never empty — falls back to ``"root.<method>"``.
        It is a legal apcore ID unless a segment begins with a digit
        (``POST /v1/2fa`` -> ``v1.2fa.post``); this function does not warn,
        the scanner does.
    """
    operation_id = operation.get("operationId") if isinstance(operation, dict) else None
    if isinstance(operation_id, str) and operation_id != "":
        # The trailing-"_" strip belongs to this branch only: the scanner's
        # final normalisation must not rewrite a legal ID a hook returned.
        candidate = _normalize_module_id(operation_id).rstrip("_")
        if candidate:
            return candidate

    parts: list[str] = []
    for seg in path.split("/"):
        if len(seg) >= 2 and seg.startswith("{") and seg.endswith("}"):
            seg = seg[1:-1]
        part = _normalize_module_id(seg)
        if part:
            parts.append(part)

    if parts:
        return ".".join([*parts, method.lower()])
    return f"root.{method.lower()}"


# Private alias so `OpenAPIScanner.scan`'s `derive_module_id=` keyword
# argument can shadow the public function name without losing access to it.
_default_derive_module_id = derive_module_id


def _collect_refs(node: Any) -> list[str]:
    """Depth-first collect every ``$ref`` string appearing under *node*."""
    refs: list[str] = []
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            refs.append(ref)
        for value in node.values():
            refs.extend(_collect_refs(value))
    elif isinstance(node, list):
        for item in node:
            refs.extend(_collect_refs(item))
    return refs


def _ref_warnings(operation: dict[str, Any], spec: dict[str, Any]) -> list[str]:
    """Warn on unresolvable internal refs and refuse external refs.

    Internal refs (``#/...``) that resolve successfully are silent — this
    only flags the failure cases enumerated in the Error Model:
    unresolvable internal ``$ref`` and external ``$ref`` (never fetched).
    """
    warnings: list[str] = []
    seen: set[str] = set()
    refs = _collect_refs(operation.get("requestBody") or {}) + _collect_refs(operation.get("responses") or {})
    for ref in refs:
        if ref in seen:
            continue
        seen.add(ref)
        if not ref.startswith("#/"):
            warnings.append(f"external $ref not fetched: {ref}")
        elif not resolve_ref(ref, spec):
            warnings.append(f"unresolvable $ref: {ref}")
    return warnings


def _first_line(text: str | None) -> str | None:
    if not text:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return None


_ABS_URL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")
_TEMPLATE_VAR_RE = re.compile(r"\{([^}]+)\}")


def _resolve_server_url(spec: dict[str, Any]) -> str | None:
    """Best-effort resolution of ``servers[0].url``.

    Absolute URLs are used verbatim. Templated URLs are substituted from
    ``servers[0].variables[*].default`` when every variable has one;
    otherwise the URL is unusable and omitted. Relative URLs require the
    spec's *source* URL to resolve against, which ``scan()`` — pure and
    I/O-free — does not have; they are omitted here (advisory only; the
    caller supplies ``base_url`` to the writer regardless).
    """
    servers = spec.get("servers")
    if not isinstance(servers, list) or not servers:
        return None
    first = servers[0]
    if not isinstance(first, dict):
        return None
    url = first.get("url")
    if not isinstance(url, str) or not url:
        return None

    if not _ABS_URL_RE.match(url):
        return None

    variables = first.get("variables")
    if isinstance(variables, dict) and variables:
        substitutions: dict[str, str] = {}
        for name, var in variables.items():
            if not isinstance(var, dict) or "default" not in var:
                return None
            substitutions[name] = str(var["default"])

        def _sub(match: re.Match[str]) -> str:
            return substitutions.get(match.group(1), match.group(0))

        url = _TEMPLATE_VAR_RE.sub(_sub, url)
        if "{" in url or "}" in url:
            return None

    return url


class OpenAPIScanner(BaseScanner):
    """Turn an OpenAPI 3.0/3.1 document into a list of ``ScannedModule``.

    Pure and synchronous: ``scan()`` accepts an already-parsed document and
    performs no I/O. Use :func:`load_spec` to fetch/parse a document first.
    """

    def scan(  # type: ignore[override]
        self,
        spec: dict[str, Any],
        *,
        include: str | None = None,
        exclude: str | None = None,
        base_path_prefix: str | None = None,
        include_deprecated: bool = True,
        transform_operation: Callable[[str, str, dict[str, Any]], dict[str, Any] | None] | None = None,
        derive_module_id: Callable[[str, str, dict[str, Any]], str | None] | None = None,  # noqa: A002
        transform_module: Callable[[ScannedModule], ScannedModule | None] | None = None,
        **_: Any,
    ) -> list[ScannedModule]:
        """Scan an OpenAPI document, returning one ``ScannedModule`` per operation.

        Every emitted ``module_id`` is in apcore's Canonical ID alphabet: the
        final ID — after the ``derive_module_id`` hook, ``base_path_prefix``
        and the ``transform_module`` hook — is normalised to snake_case, and
        an ID that is already legal is left unchanged. One that is still not
        a legal apcore ID (a segment beginning with a digit, or an empty ID
        from a hook) is emitted anyway; after deduplication it carries a
        warning naming the ID actually emitted.

        See ``Contract: OpenAPIScanner.scan`` in
        ``apcore-toolkit/docs/features/openapi-scanner.md``.
        """
        self._validate_spec(spec)

        paths = spec.get("paths") or {}
        if not isinstance(paths, dict):
            paths = {}

        openapi_version = spec.get("openapi")
        raw_version = (spec.get("info") or {}).get("version")
        doc_version = raw_version if isinstance(raw_version, str) and raw_version else "1.0.0"
        server_url = _resolve_server_url(spec)

        modules: list[ScannedModule] = []

        for path, path_item in paths.items():
            if not isinstance(path_item, dict):
                continue
            for key, operation in path_item.items():
                method = key.lower()
                if method not in _RECOGNIZED_METHODS or not isinstance(operation, dict):
                    continue

                if transform_operation is not None:
                    operation = transform_operation(path, method, operation)
                    if operation is None:
                        continue

                # Strict boolean check (not truthy coercion): OpenAPI's
                # `deprecated` is typed `boolean` in the spec, so a malformed
                # non-boolean value (e.g. the string "false") should not
                # flip this on. Matches TypeScript/Rust.
                deprecated = operation.get("deprecated") is True
                if deprecated and not include_deprecated:
                    continue

                mid = derive_module_id(path, method, operation) if derive_module_id is not None else None
                if mid is None:
                    mid = _default_derive_module_id(path, method, operation)
                if base_path_prefix:
                    mid = f"{base_path_prefix}.{mid}"

                warnings = _ref_warnings(operation, spec)

                input_schema = extract_input_schema(operation, spec)
                output_schema = extract_output_schema(operation, spec)
                responses = operation.get("responses") or {}
                # `[0-9]` rather than `\d`: Python's `\d` matches any Unicode
                # decimal digit (e.g. fullwidth digits, U+FF10-FF19), while
                # TypeScript's `\d` and Rust's `is_ascii_digit()` are
                # ASCII-only. An explicit ASCII class keeps this check
                # identical everywhere.
                has_success = isinstance(responses, dict) and any(
                    re.match(r"^2[0-9][0-9]$", str(status)) for status in responses
                )
                if not has_success:
                    warnings.append("no 2xx response defined; output_schema is empty")

                annotations = self.infer_annotations_from_method(method)
                if deprecated:
                    # ModuleAnnotations has no first-class `deprecated` field;
                    # the toolkit convention is `annotations.extra["deprecated"]`
                    # (see also tui_view_model.py's Filter.deprecated handling).
                    annotations = replace(annotations, extra={**annotations.extra, "deprecated": True})

                # Strict-string checks below (rather than truthy fallbacks)
                # so a malformed non-string value degrades to "absent"
                # instead of leaking a wrong-typed value into a `str`-typed
                # field — matches TypeScript's `typeof x === 'string'` and
                # Rust's `Value::as_str()` guards.
                raw_summary = operation.get("summary")
                summary = raw_summary if isinstance(raw_summary, str) and raw_summary else None
                raw_documentation = operation.get("description")
                documentation = raw_documentation if isinstance(raw_documentation, str) else None
                description = summary or _first_line(documentation) or ""

                openapi_meta: dict[str, Any] = {"spec_version": openapi_version}
                operation_id = operation.get("operationId")
                if isinstance(operation_id, str) and operation_id:
                    openapi_meta["operation_id"] = operation_id
                if server_url:
                    openapi_meta["server_url"] = server_url
                if summary:
                    openapi_meta["summary"] = summary

                raw_tags = operation.get("tags")
                # `isinstance(..., list)` rather than `list(x or [])`: the
                # latter silently character-splits a non-array string value
                # (`list("foo")` -> `["f","o","o"]`) instead of degrading to
                # an empty list. Matches TypeScript/Rust, which both already
                # required an array. Non-string entries within the array are
                # also dropped rather than passed through, since
                # `ScannedModule.tags` is documented/typed as `list[str]` —
                # matches the Rust SDK's `filter_map(Value::as_str)`.
                tags = [t for t in raw_tags if isinstance(t, str)] if isinstance(raw_tags, list) else []

                module = ScannedModule(
                    module_id=mid,
                    description=description,
                    input_schema=input_schema,
                    output_schema=output_schema,
                    tags=tags,
                    target=f"{method.upper()} {path}",
                    version=doc_version,
                    annotations=annotations,
                    documentation=documentation,
                    metadata={
                        "http_method": method.upper(),
                        "url_path": path,
                        "openapi": openapi_meta,
                    },
                    warnings=warnings,
                )

                if transform_module is not None:
                    transformed = transform_module(module)
                    if transformed is None:
                        continue
                    module = transformed

                # Normalise the *final* ID — after the `derive_module_id` hook,
                # `base_path_prefix` and `transform_module` — so the alphabet
                # holds for every emitted ID. Runs before filter/dedup so
                # `include`/`exclude` match, and `deduplicate_ids` resolves, the
                # emitted ID. A legal ID passes through unchanged. `replace()`
                # rather than mutation: a `transform_module` hook may still hold
                # the object it returned.
                final_id = _normalize_module_id(module.module_id)
                if final_id != module.module_id:
                    module = replace(module, module_id=final_id)

                modules.append(module)

        modules = self.filter_modules(modules, include=include, exclude=exclude)
        modules = self.deduplicate_ids(modules)
        # The legality check runs last, so its warning names the ID actually
        # emitted (`3ds_2`, not `3ds`) and follows any deduplication warning.
        return [self._with_legality_warning(module) for module in modules]

    @staticmethod
    def _with_legality_warning(module: ScannedModule) -> ScannedModule:
        """Append the pinned legality warning if *module*'s ID is still illegal."""
        warning = _legality_warning(module.module_id)
        if warning is None:
            return module
        return replace(module, warnings=[*module.warnings, warning])

    def get_source_name(self) -> str:
        return "openapi"

    @staticmethod
    def _validate_spec(spec: Any) -> None:
        if not isinstance(spec, dict):
            raise ValueError("OpenAPIScanner.scan: spec must be an object")
        openapi_version = spec.get("openapi")
        if not isinstance(openapi_version, str) or not (
            openapi_version.startswith("3.0") or openapi_version.startswith("3.1")
        ):
            raise ValueError(
                "OpenAPIScanner.scan: unsupported spec — expected OpenAPI 3.0.x or 3.1.x, "
                f"got 'openapi': {openapi_version!r} (swagger 2.0 is not supported in V1)"
            )


def load_spec(
    source: str | Path,
    *,
    headers: dict[str, str] | None = None,
    auth_header_factory: Callable[[], dict[str, str]] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Load and parse an OpenAPI document from a local path or ``http(s)://`` URL.

    Convenience helper, explicitly outside the conformance corpus (I/O
    behaviour is deliberately not byte-specified). The URL/path is taken
    verbatim — no candidate paths are probed. See ``Contract: load_spec``
    in ``apcore-toolkit/docs/features/openapi-scanner.md``.

    Security: *source* is trusted input. Callers taking a URL from an
    untrusted source are responsible for their own allowlisting (SSRF).

    Args:
        source: Local filesystem path or ``http(s)://`` URL. Taken verbatim.
        headers: Extra request headers (ignored for local files).
        auth_header_factory: Optional callable returning auth headers,
            invoked once per fetch (ignored for local files).
        timeout: Request timeout in seconds (ignored for local files).

    Returns:
        The parsed document as a dict.
    """
    source_str = str(source)
    if source_str.startswith(("http://", "https://")):
        import httpx

        request_headers = dict(headers or {})
        if auth_header_factory is not None:
            request_headers.update(auth_header_factory())
        response = httpx.get(source_str, headers=request_headers, timeout=timeout)
        response.raise_for_status()
        return _parse_document(response.text, source_str)

    text = Path(source).read_text(encoding="utf-8")
    return _parse_document(text, source_str)


def _parse_document(text: str, source: str) -> dict[str, Any]:
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        parsed: Any = json.loads(text)
        return dict(parsed)
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise ValueError(f"cannot parse non-JSON spec {source!r} without PyYAML installed") from exc
    try:
        loaded: Any = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"malformed YAML spec at {source!r}: {exc}") from exc
    return dict(loaded)
