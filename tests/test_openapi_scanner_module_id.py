"""Unit tests for OpenAPIScanner's module-ID normalisation.

``apcore-toolkit/docs/features/openapi-scanner.md`` § ``module_id`` Derivation
requires every emitted ``module_id`` to be in apcore's Canonical ID alphabet
(PROTOCOL_SPEC §2.7). The conformance corpus (``openapi_scan.json`` cases 003,
015, 025-029) pins the cross-SDK outputs; these tests pin the edges the corpus
does not reach — non-ASCII input, idempotence, the empty ID, the legality
check's own regex semantics, and the scanner's hook interplay.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from apcore import MODULE_ID_PATTERN

import apcore_toolkit
import apcore_toolkit.openapi_scanner as openapi_scanner_module
from apcore_toolkit.openapi_scanner import (
    OpenAPIScanner,
    _legality_warning,
    _normalize_module_id,
    derive_module_id,
)
from apcore_toolkit.types import ScannedModule

_BASE = {"openapi": "3.0.3", "info": {"title": "t", "version": "1.0.0"}}
_OK = {"responses": {"200": {"description": "ok"}}}


def _scan(paths: dict[str, Any], **options: Any) -> list[ScannedModule]:
    return OpenAPIScanner().scan({**_BASE, "paths": paths}, **options)


def _expected_warning(module_id: str, segment: str) -> str:
    return (
        f"module_id '{module_id}' is not a legal apcore module ID: segment '{segment}' "
        "must match ^[a-z][a-z0-9_]*$; name this operation with a derive_module_id "
        "or transform_module hook"
    )


# ---------------------------------------------------------------------------
# _normalize_module_id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Acronym boundary `([A-Z]+)([A-Z][a-z])` runs before the word boundary.
        ("getHTTPResponse", "get_http_response"),
        ("HTTPServer", "http_server"),
        ("XMLHttpRequest", "xml_http_request"),
        ("getUserID", "get_user_id"),
        ("ABC", "abc"),
        ("ABCdEFGh", "ab_cd_ef_gh"),
        # Known ugly outputs, pinned rather than special-cased (spec: Design decisions).
        ("getIDs", "get_i_ds"),
        ("OAuth2Token", "o_auth2_token"),
        ("IPv6Address", "i_pv6_address"),
    ],
)
def test_normalize_acronym_boundaries(raw: str, expected: str) -> None:
    assert _normalize_module_id(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("v2Items", "v2_items"),  # a digit is a word boundary before an uppercase letter
        ("getV2API", "get_v2_api"),
        ("item2", "item2"),
        ("2fa", "2fa"),  # a leading digit is NOT repaired — that would invent a name
        ("3ds", "3ds"),
    ],
)
def test_normalize_digits(raw: str, expected: str) -> None:
    assert _normalize_module_id(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # FastAPI-style generated operationIds carry `__` runs.
        ("get_product_product__product_id__get", "get_product_product_product_id_get"),
        ("a__b", "a_b"),
        ("__private__", "private"),
        ("list-pets", "list_pets"),
        ("a  b", "a_b"),
        ("get_User", "get_user"),
        # Dots delimit segments; `_` runs are collapsed and stripped per segment,
        # and empty segments are dropped.
        ("Users.GetUser", "users.get_user"),
        ("Custom-Space.GetThing", "custom_space.get_thing"),
        ("x..y", "x.y"),
        (".a.", "a"),
        ("a._b_.c", "a.b.c"),
        ("already_snake.case", "already_snake.case"),
    ],
)
def test_normalize_separators_and_underscore_runs(raw: str, expected: str) -> None:
    assert _normalize_module_id(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # U+212A KELVIN SIGN lowercases to ASCII "k" under Unicode rules. Step 2
        # replaces it before step 3 lowercases, so it never survives as a letter.
        ("\u212aelvin", "elvin"),
        ("get\u212aey", "get_ey"),
        # U+0130 LATIN CAPITAL LETTER I WITH DOT ABOVE lowercases to two code
        # points ("i" + U+0307) — same reason for the step order.
        ("\u0130d", "d"),
        ("caf\u00e9", "caf"),
        # Fullwidth letters and digits are not `[A-Za-z0-9]`.
        ("\uff21\uff22\uff23", ""),
        ("\uff11\uff12", ""),
        # An astral character is one Python character; JavaScript without the
        # `u` flag sees two code units. Step 4's collapse makes both agree.
        ("\U0001f600emoji", "emoji"),
        ("a\U0001f600b", "a_b"),
        ("abc\n", "abc"),
    ],
)
def test_normalize_non_ascii_never_survives(raw: str, expected: str) -> None:
    result = _normalize_module_id(raw)
    assert result == expected
    assert result.isascii()


@pytest.mark.parametrize("raw", ["", "_", "___", ".", "_._", "-", "\U0001f600", "\u212a"])
def test_normalize_can_return_empty(raw: str) -> None:
    assert _normalize_module_id(raw) == ""


_IDEMPOTENCE_PROBES = [
    "getUserById",
    "getHTTPResponse",
    "OAuth2Token",
    "list-pets",
    "Custom-Space.GetThing",
    "__private__",
    "x..y",
    "2fa",
    "\u212aelvin",
    "\U0001f600emoji",
    "a._b_.c",
    "users.user_id.get",
    "",
]


@pytest.mark.parametrize("raw", _IDEMPOTENCE_PROBES)
def test_normalize_is_idempotent_and_in_alphabet(raw: str) -> None:
    once = _normalize_module_id(raw)
    assert _normalize_module_id(once) == once
    assert re.fullmatch(r"[a-z0-9_.]*", once)
    for segment in once.split(".") if once else []:
        assert segment, "empty segments are dropped"
        assert not segment.startswith("_") and not segment.endswith("_")
        assert "__" not in segment


def test_normalize_is_not_public_api() -> None:
    """The scanner owns the alphabet; the helper is deliberately private."""
    assert "normalize_module_id" not in openapi_scanner_module.__all__
    assert "_normalize_module_id" not in openapi_scanner_module.__all__
    assert not hasattr(apcore_toolkit, "normalize_module_id")
    assert "normalize_module_id" not in apcore_toolkit.__all__


# ---------------------------------------------------------------------------
# derive_module_id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "method", "operation", "expected"),
    [
        ("/users/{id}", "get", {"operationId": "getUserById"}, "get_user_by_id"),
        ("/user-profiles/{userId}", "get", {}, "user_profiles.user_id.get"),
        ("/v1/_debug/", "get", {}, "v1.debug.get"),
        ("/a b/c", "post", {}, "a_b.c.post"),
        # The method is normalised with the path, so a caller passing "GET" gets the same ID.
        ("/users", "GET", {}, "users.get"),
        ("/", "get", {}, "root.get"),
        ("", "delete", {}, "root.delete"),
        # A digit-leading segment is returned as-is; the scanner (not this function) warns.
        ("/v1/2fa", "post", {}, "v1.2fa.post"),
        # An operationId that normalises to nothing falls through to the path branch.
        ("/widgets", "get", {"operationId": "---"}, "widgets.get"),
        ("/widgets", "get", {"operationId": "\U0001f600"}, "widgets.get"),
        # ...and from there to the root fallback.
        ("/", "put", {"operationId": "___"}, "root.put"),
    ],
)
def test_derive_module_id(path: str, method: str, operation: dict[str, Any], expected: str) -> None:
    assert derive_module_id(path, method, operation) == expected


# ---------------------------------------------------------------------------
# _legality_warning
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module_id", ["users.get", "get_user_by_id", "v2_items", "a.b_c.d9"])
def test_legality_warning_none_for_legal_ids(module_id: str) -> None:
    assert _legality_warning(module_id) is None


@pytest.mark.parametrize(
    ("module_id", "segment"),
    [
        ("v1.2fa.post", "2fa"),
        ("3ds", "3ds"),
        ("a.1b.2c", "1b"),  # the FIRST failing segment is named
        ("", ""),  # an empty ID has one empty segment
    ],
)
def test_legality_warning_names_first_bad_segment(module_id: str, segment: str) -> None:
    assert _legality_warning(module_id) == _expected_warning(module_id, segment)


def test_legality_check_does_not_accept_trailing_newline() -> None:
    """Python's `$` also matches before a trailing newline, so `re.match(r"^...$")`
    would accept `"abc\\n"` where JavaScript and Rust reject it. The check uses
    `fullmatch`. Unreachable from the scanner (normalisation replaces `\\n`),
    pinned here so a refactor to `^...$` cannot quietly reintroduce it."""
    assert _legality_warning("abc\n") == _expected_warning("abc\n", "abc\n")


# ---------------------------------------------------------------------------
# OpenAPIScanner.scan — normalisation of the final ID, and the warning
# ---------------------------------------------------------------------------


def test_petstore_ids_are_registrable() -> None:
    """The canonical Swagger Petstore registered nothing under V1's verbatim IDs."""
    modules = _scan(
        {
            "/pets": {"get": {"operationId": "listPets", **_OK}, "post": {"operationId": "createPets", **_OK}},
            "/pets/{petId}": {"get": {"operationId": "showPetById", **_OK}},
        }
    )
    assert [m.module_id for m in modules] == ["list_pets", "create_pets", "show_pet_by_id"]
    assert all(MODULE_ID_PATTERN.match(m.module_id) for m in modules)
    assert all(m.warnings == [] for m in modules)
    # The raw operationId is kept verbatim for display and back-mapping.
    assert [m.metadata["openapi"]["operation_id"] for m in modules] == ["listPets", "createPets", "showPetById"]


def test_illegal_id_is_emitted_with_warning_after_existing_warnings() -> None:
    modules = _scan({"/v1/2fa": {"post": {"responses": {"404": {"description": "nope"}}}}})
    assert len(modules) == 1
    assert modules[0].module_id == "v1.2fa.post"
    assert modules[0].warnings == [
        "no 2xx response defined; output_schema is empty",
        _expected_warning("v1.2fa.post", "2fa"),
    ]


def test_legal_id_gets_no_legality_warning() -> None:
    modules = _scan({"/users": {"get": _OK}})
    assert modules[0].module_id == "users.get"
    assert modules[0].warnings == []


def test_derive_hook_returning_empty_string_warns_with_empty_segment() -> None:
    """Only a hook can produce an empty ID; it is emitted, not repaired or raised."""
    modules = _scan({"/users": {"get": _OK}}, derive_module_id=lambda p, m, o: "")
    assert len(modules) == 1
    assert modules[0].module_id == ""
    assert modules[0].warnings == [_expected_warning("", "")]


def test_derive_hook_returning_only_separators_normalises_to_empty() -> None:
    modules = _scan({"/users": {"get": _OK}}, derive_module_id=lambda p, m, o: "--.__")
    assert modules[0].module_id == ""
    assert modules[0].warnings == [_expected_warning("", "")]


def test_base_path_prefix_is_normalised_with_the_id() -> None:
    modules = _scan({"/users": {"get": {"operationId": "listUsers", **_OK}}}, base_path_prefix="Pet-Store")
    assert modules[0].module_id == "pet_store.list_users"
    assert modules[0].warnings == []


def test_digit_leading_prefix_warns() -> None:
    modules = _scan({"/users": {"get": _OK}}, base_path_prefix="2024-api")
    assert modules[0].module_id == "2024_api.users.get"
    assert modules[0].warnings == [_expected_warning("2024_api.users.get", "2024_api")]


def test_transform_module_output_is_normalised_without_mutating_hook_object() -> None:
    returned: list[ScannedModule] = []

    def rename(module: ScannedModule) -> ScannedModule:
        module.module_id = "Admin.ListUsers"
        returned.append(module)
        return module

    modules = _scan({"/users": {"get": _OK}}, transform_module=rename)
    assert modules[0].module_id == "admin.list_users"
    assert modules[0].warnings == []
    # The object the hook returned (and may still hold) is left as the hook left it.
    assert returned[0].module_id == "Admin.ListUsers"


def test_transform_module_sees_the_pre_normalisation_id() -> None:
    """Invocation order: normalisation runs after transform_module, so the hook
    sees the derive-hook output and prefix as produced."""
    seen: list[str] = []

    def spy(module: ScannedModule) -> ScannedModule:
        seen.append(module.module_id)
        return module

    modules = _scan(
        {"/users": {"get": _OK}},
        base_path_prefix="My-Api",
        derive_module_id=lambda p, m, o: "ListUsers",
        transform_module=spy,
    )
    assert seen == ["My-Api.ListUsers"]
    assert modules[0].module_id == "my_api.list_users"


def test_transform_module_producing_illegal_id_warns() -> None:
    def rename(module: ScannedModule) -> ScannedModule:
        module.module_id = "9lives.Cat"
        return module

    modules = _scan({"/users": {"get": _OK}}, transform_module=rename)
    assert modules[0].module_id == "9lives.cat"
    assert modules[0].warnings == [_expected_warning("9lives.cat", "9lives")]


def test_normalisation_collision_is_deduplicated() -> None:
    """`listPets` and `list-pets` are distinct in the document but normalise to
    the same ID; normalisation runs before deduplicate_ids, which resolves it."""
    modules = _scan(
        {"/a": {"get": {"operationId": "listPets", **_OK}}, "/b": {"get": {"operationId": "list-pets", **_OK}}}
    )
    assert [m.module_id for m in modules] == ["list_pets", "list_pets_2"]
    assert modules[0].warnings == []
    assert modules[1].warnings == ["Module ID renamed from 'list_pets' to 'list_pets_2' to avoid collision"]


def test_include_filter_matches_the_normalised_id() -> None:
    paths = {"/pets": {"get": {"operationId": "listPets", **_OK}}, "/users": {"get": _OK}}
    assert [m.module_id for m in _scan(paths, include=r"^list_pets$")] == ["list_pets"]
    assert _scan(paths, include=r"^listPets$") == []
