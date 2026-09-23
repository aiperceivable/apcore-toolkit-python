"""Regression tests for OpenAPIScanner's handling of malformed/non-conforming
field types — found by a cross-SDK audit that compared this reference
implementation against the TypeScript and Rust ports. Each case documents a
type coercion that used to diverge from at least one other SDK; the fix
makes Python require the OpenAPI-spec-declared type (matching TS/Rust) and
degrade to the documented default otherwise, rather than truthy-coercing or
blindly wrapping the wrong-typed value.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest

from apcore_toolkit.openapi_scanner import OpenAPIScanner, load_spec

_BASE = {"openapi": "3.0.3", "info": {"title": "t", "version": "1.0.0"}}


def _scan(paths: dict) -> list:
    return OpenAPIScanner().scan({**_BASE, "paths": paths})


def test_deprecated_string_value_is_not_truthy_coerced() -> None:
    """`"deprecated": "false"` (a string, not a bool) must NOT be treated
    as deprecated — only the literal JSON `true` counts."""
    modules = _scan({"/widgets": {"get": {"deprecated": "false", "responses": {"200": {"description": "ok"}}}}})
    assert len(modules) == 1
    assert modules[0].annotations is not None
    assert modules[0].annotations.extra.get("deprecated") is not True


def test_tags_non_array_degrades_to_empty_list_not_character_split() -> None:
    """`"tags": "foo"` must degrade to `[]`, not silently become
    `["f", "o", "o"]` via `list("foo")`."""
    modules = _scan({"/widgets": {"get": {"tags": "foo", "responses": {"200": {"description": "ok"}}}}})
    assert len(modules) == 1
    assert modules[0].tags == []


def test_tags_array_drops_non_string_entries() -> None:
    """`"tags": ["users", 5, null, "active"]` must silently drop the
    non-string entries (`5`, `null`) and preserve the string entries in
    original order — `ScannedModule.tags` is documented/typed as
    `list[str]`, matching the Rust SDK's `filter_map(Value::as_str)`."""
    modules = _scan(
        {"/widgets": {"get": {"tags": ["users", 5, None, "active"], "responses": {"200": {"description": "ok"}}}}}
    )
    assert len(modules) == 1
    assert modules[0].tags == ["users", "active"]


def test_operation_id_non_string_is_omitted_from_metadata() -> None:
    """A non-string `operationId` (e.g. a JSON number) must not leak into
    `metadata.openapi.operation_id`."""
    modules = _scan({"/widgets": {"get": {"operationId": 12345, "responses": {"200": {"description": "ok"}}}}})
    assert len(modules) == 1
    assert "operation_id" not in modules[0].metadata["openapi"]
    # Falls back to path-derived id since the non-string operationId is ignored.
    assert modules[0].module_id == "widgets.get"


def test_info_version_non_string_falls_back_to_default() -> None:
    """A non-string `info.version` (e.g. a JSON number) must fall back to
    the documented default `"1.0.0"`, not leak a non-string value into the
    `str`-typed `version` field."""
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "t", "version": 1.5},
        "paths": {"/widgets": {"get": {"responses": {"200": {"description": "ok"}}}}},
    }
    modules = OpenAPIScanner().scan(spec)
    assert modules[0].version == "1.0.0"


def test_summary_non_string_falls_through_to_description() -> None:
    """A non-string `summary` must be treated as absent, falling through to
    `description`'s first line rather than leaking a wrong-typed value."""
    modules = _scan(
        {
            "/widgets": {
                "get": {
                    "summary": 42,
                    "description": "Real description first line.\nMore text.",
                    "responses": {"200": {"description": "ok"}},
                }
            }
        }
    )
    assert modules[0].description == "Real description first line."


def test_2xx_status_check_is_ascii_only() -> None:
    """A fullwidth-digit status key (Unicode U+FF12, "\\uff12\\uff12") must
    NOT count as a 2xx success response — only ASCII digits match, so the
    module still gets the "no 2xx response defined" warning. Matches
    TypeScript's non-unicode `\\d` and Rust's `is_ascii_digit()`."""
    fullwidth_status = "2" + "\uff12\uff12"
    modules = _scan({"/widgets": {"get": {"responses": {fullwidth_status: {"description": "fullwidth 200"}}}}})
    assert any("no 2xx response defined" in w for w in modules[0].warnings)


# --------------------------------------------------------------------------
# load_spec() \u2014 http(s):// branch
# --------------------------------------------------------------------------


def _http_response(status_code: int, text: str, url: str = "http://example.com/openapi.json") -> httpx.Response:
    return httpx.Response(status_code, text=text, request=httpx.Request("GET", url))


def test_load_spec_fetches_json_from_http_url() -> None:
    """A successful fetch of a JSON document from an ``http://`` URL is parsed
    and returned as a dict, and request headers/auth are forwarded."""
    body = json.dumps({**_BASE, "paths": {}})
    with patch("httpx.get", return_value=_http_response(200, body)) as mock_get:
        spec = load_spec(
            "http://example.com/openapi.json",
            headers={"X-Custom": "1"},
            auth_header_factory=lambda: {"Authorization": "Bearer tok"},
        )
    assert spec == {**_BASE, "paths": {}}
    args, kwargs = mock_get.call_args
    assert args[0] == "http://example.com/openapi.json"
    assert kwargs["headers"]["X-Custom"] == "1"
    assert kwargs["headers"]["Authorization"] == "Bearer tok"


def test_load_spec_fetches_yaml_from_http_url() -> None:
    """A successful fetch of a YAML document (non-JSON body) is parsed too."""
    body = "openapi: 3.0.3\ninfo:\n  title: t\n  version: 1.0.0\npaths: {}\n"
    with patch("httpx.get", return_value=_http_response(200, body)):
        spec = load_spec("http://example.com/openapi.yaml")
    assert spec["openapi"] == "3.0.3"


def test_load_spec_non_2xx_response_raises() -> None:
    """A non-2xx HTTP response must raise rather than silently returning an
    error page parsed as if it were a spec."""
    with patch("httpx.get", return_value=_http_response(404, "not found")):
        with pytest.raises(httpx.HTTPStatusError):
            load_spec("http://example.com/missing.json")


def test_load_spec_malformed_json_body_raises() -> None:
    """A body that looks like JSON (starts with ``{``) but is malformed must
    raise rather than returning a partially-parsed/garbage document."""
    with patch("httpx.get", return_value=_http_response(200, "{not valid json,,,")):
        with pytest.raises(json.JSONDecodeError):
            load_spec("http://example.com/openapi.json")


def test_load_spec_malformed_yaml_body_raises_value_error() -> None:
    """A body that is neither valid JSON nor valid YAML must raise a
    ``ValueError`` that names the source, per ``_parse_document``."""
    with patch("httpx.get", return_value=_http_response(200, "openapi: [unterminated")):
        with pytest.raises(ValueError, match="malformed YAML"):
            load_spec("http://example.com/openapi.yaml")
