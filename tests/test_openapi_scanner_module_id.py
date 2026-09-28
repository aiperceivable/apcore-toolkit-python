"""Unit tests for OpenAPIScanner's module-ID normalisation.

``apcore-toolkit/docs/features/openapi-scanner.md`` § ``module_id`` Derivation
requires every emitted ``module_id`` to be in apcore's Canonical ID alphabet
(PROTOCOL_SPEC §2.7), and requires normalisation to return an ID that is
already legal unchanged. The conformance corpus (``openapi_scan.json`` cases
003, 015, 025-031) pins the cross-SDK outputs; these tests pin the edges the
corpus does not reach — non-ASCII input, the legal-ID invariant, idempotence,
the empty ID, the legality check's own regex semantics, and the scanner's hook
interplay.
"""

from __future__ import annotations

import itertools
import re
import time
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
        # Acronym boundary `([A-Z])([A-Z][a-z])` and word boundary `([a-z0-9])([A-Z])`.
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
        # Runs of `_` are KEPT: FastAPI's generated IDs are legal and register today.
        ("get_product_product__product_id__get", "get_product_product__product_id__get"),
        ("a__b", "a__b"),
        # One `_` per replaced code point, and no collapse afterwards.
        ("list-pets", "list_pets"),
        ("a  b", "a__b"),
        ("a-_b", "a__b"),
        # Only LEADING `_` is stripped per segment; a trailing `_` is legal and kept.
        ("__private__", "private__"),
        ("abc_", "abc_"),
        ("list-", "list_"),
        ("get_User", "get_user"),
        # Dots delimit segments; empty segments are dropped.
        ("Users.GetUser", "users.get_user"),
        ("Custom-Space.GetThing", "custom_space.get_thing"),
        ("x..y", "x.y"),
        (".a.", "a"),
        ("a._b_.c", "a.b_.c"),
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
        ("caf\u00e9", "caf_"),  # a trailing `_` is kept by normalisation
        # Fullwidth letters and digits are not `[A-Za-z0-9]`.
        ("\uff21\uff22\uff23", ""),
        ("\uff11\uff12", ""),
        # An astral character is ONE code point and becomes ONE `_` (JavaScript
        # needs the `u` flag, where it is two code units; conformance case 025).
        ("\U0001f600emoji", "emoji"),
        ("a\U0001f600b", "a_b"),
        ("a\U0001f600\U0001f600b", "a__b"),
        ("abc\n", "abc_"),
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
    "a  b__",
    "",
]


@pytest.mark.parametrize("raw", _IDEMPOTENCE_PROBES)
def test_normalize_is_idempotent_and_in_alphabet(raw: str) -> None:
    once = _normalize_module_id(raw)
    assert _normalize_module_id(once) == once
    assert re.fullmatch(r"[a-z0-9_.]*", once)
    for segment in once.split(".") if once else []:
        assert segment, "empty segments are dropped"
        assert not segment.startswith("_"), "leading `_` is stripped"


@pytest.mark.parametrize(
    "legal_id",
    [
        "read_item_items__item_id__get",  # FastAPI
        "get_product_product__product_id__get",
        "abc_",
        "a__b.c_",
        "users.user_id.get",
        "x9.y_z",
    ],
)
def test_normalize_returns_a_legal_id_unchanged(legal_id: str) -> None:
    """The spec's MUST: a name apcore already accepts is never rewritten."""
    assert MODULE_ID_PATTERN.fullmatch(legal_id)
    assert _normalize_module_id(legal_id) == legal_id


def test_normalize_returns_every_short_legal_id_unchanged() -> None:
    """Exhaustive over the character classes a legal ID can contain (letter,
    digit, `_`, `.`), up to length 6 — 516 legal IDs."""
    checked = 0
    for length in range(1, 7):
        for chars in itertools.product("a0_.", repeat=length):
            candidate = "".join(chars)
            if MODULE_ID_PATTERN.fullmatch(candidate):
                checked += 1
                assert _normalize_module_id(candidate) == candidate
    assert checked == 516


def test_normalize_is_segment_local() -> None:
    """normalize(a.b) == normalize(a) + "." + normalize(b) — why the path branch
    may normalise one segment at a time."""
    pieces = ["getUser", "-x-", "", "__y", "Kz", "a  b", "2fa", "HTTPServer"]
    for left, right in itertools.product(pieces, repeat=2):
        joined = ".".join(part for part in (_normalize_module_id(left), _normalize_module_id(right)) if part)
        assert _normalize_module_id(f"{left}.{right}") == joined


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
        # The method is lowercased, so a caller passing "GET" gets the same ID.
        ("/users", "GET", {}, "users.get"),
        # Path segments are normalised one by one; empty results are dropped, and
        # with none left the ID falls back to root.<method> (conformance case 031).
        ("/", "get", {}, "root.get"),
        ("", "delete", {}, "root.delete"),
        ("/-", "get", {}, "root.get"),
        ("/{}", "post", {}, "root.post"),
        ("/_/{_}/", "GET", {}, "root.get"),
        ("/v1/-/items", "get", {}, "v1.items.get"),
        ("/items_/{item_id}", "get", {}, "items_.item_id.get"),  # trailing `_` kept on the path branch
        # A digit-leading segment is returned as-is; the scanner (not this function) warns.
        ("/v1/2fa", "post", {}, "v1.2fa.post"),
        # An operationId that normalises to nothing falls through to the path branch.
        ("/widgets", "get", {"operationId": "---"}, "widgets.get"),
        ("/widgets", "get", {"operationId": "\U0001f600"}, "widgets.get"),
        # ...and from there to the root fallback.
        ("/", "put", {"operationId": "___"}, "root.put"),
        # A legal operationId is used unchanged, `__` runs included (FastAPI).
        ("/items/{item_id}", "get", {"operationId": "read_item_items__item_id__get"}, "read_item_items__item_id__get"),
        # The operationId branch, and only it, strips a trailing `_` (as V1 did).
        ("/h", "get", {"operationId": "getUser_"}, "get_user"),
        ("/h", "get", {"operationId": "list-"}, "list"),
        ("/h", "get", {"operationId": "a.b__"}, "a.b"),
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


def test_legality_warning_follows_dedup_and_names_the_emitted_id() -> None:
    """The legality check runs after deduplicate_ids (conformance case 030)."""
    modules = _scan({"/a": {"get": {"operationId": "3ds", **_OK}}, "/b": {"get": {"operationId": "3ds", **_OK}}})
    assert [m.module_id for m in modules] == ["3ds", "3ds_2"]
    assert modules[0].warnings == [_expected_warning("3ds", "3ds")]
    assert modules[1].warnings == [
        "Module ID renamed from '3ds' to '3ds_2' to avoid collision",
        _expected_warning("3ds_2", "3ds_2"),
    ]


def test_filtered_out_module_is_not_checked() -> None:
    modules = _scan({"/v1/2fa": {"post": _OK}, "/users": {"get": _OK}}, exclude=r"^v1\.")
    assert [(m.module_id, m.warnings) for m in modules] == [("users.get", [])]


@pytest.mark.parametrize("hook_id", ["abc_", "read_item_items__item_id__get", "a__b.c_"])
def test_legal_hook_output_is_not_rewritten(hook_id: str) -> None:
    """The final normalisation strips no trailing `_` and collapses no `__`:
    a legal ID a hook returns is emitted as returned."""
    modules = _scan({"/users": {"get": _OK}}, derive_module_id=lambda p, m, o: hook_id)
    assert modules[0].module_id == hook_id
    assert modules[0].warnings == []


def test_transform_module_legal_output_is_not_rewritten() -> None:
    def rename(module: ScannedModule) -> ScannedModule:
        module.module_id = "custom__name_"
        return module

    modules = _scan({"/users": {"get": _OK}}, transform_module=rename)
    assert modules[0].module_id == "custom__name_"
    assert modules[0].warnings == []


def test_fastapi_document_ids_are_unchanged() -> None:
    """FastAPI-generated operationIds are legal apcore IDs and register as-is."""
    modules = _scan(
        {
            "/items/{item_id}": {"get": {"operationId": "read_item_items__item_id__get", **_OK}},
            "/items/": {"post": {"operationId": "create_item_items__post", **_OK}},
        }
    )
    assert [m.module_id for m in modules] == ["read_item_items__item_id__get", "create_item_items__post"]
    assert all(MODULE_ID_PATTERN.fullmatch(m.module_id) for m in modules)
    assert all(m.warnings == [] for m in modules)


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


def test_long_capital_run_is_linear_time() -> None:
    # The scanner reads documents it did not write. The `([A-Z]+)` form of the
    # acronym rule rescans a run of capitals from every start position
    # (40,000 capitals took ~10 s); the pinned `([A-Z])` form is linear and
    # inserts the `_` in the same place.
    crafted = "A" * 200_000 + "b"
    start = time.perf_counter()
    result = _normalize_module_id(crafted)
    elapsed = time.perf_counter() - start
    assert result == "a" * 199_999 + "_ab"
    assert elapsed < 2.0, f"normalisation took {elapsed:.2f}s on a 200,001-character input"
