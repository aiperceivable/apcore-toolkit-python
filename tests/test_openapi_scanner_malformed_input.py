"""Regression tests for OpenAPIScanner's handling of malformed/non-conforming
field types — found by a cross-SDK audit that compared this reference
implementation against the TypeScript and Rust ports. Each case documents a
type coercion that used to diverge from at least one other SDK; the fix
makes Python require the OpenAPI-spec-declared type (matching TS/Rust) and
degrade to the documented default otherwise, rather than truthy-coercing or
blindly wrapping the wrong-typed value.
"""

from __future__ import annotations

from apcore_toolkit.openapi_scanner import OpenAPIScanner

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
    spec = {"openapi": "3.0.3", "info": {"title": "t", "version": 1.5}, "paths": {"/widgets": {"get": {"responses": {"200": {"description": "ok"}}}}}}
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
    """A fullwidth-digit status key (e.g. "2２２") must NOT count
    as a 2xx success response — only ASCII digits match, so the module
    still gets the "no 2xx response defined" warning. Matches TypeScript's
    non-unicode `\\d` and Rust's `is_ascii_digit()`."""
    modules = _scan({"/widgets": {"get": {"responses": {"2２２": {"description": "fullwidth 200"}}}}})
    assert any("no 2xx response defined" in w for w in modules[0].warnings)
