"""Conformance harness: assert Python's ``deep_resolve_refs`` matches the shared
fixture corpus at ``apcore-toolkit/conformance/fixtures/ref_resolution.json``.

The TypeScript and Rust SDKs run the same fixture file through their own
resolver and must produce identical output. This is the cross-SDK behavioural
contract for ``$ref`` resolution and sibling-key merging (see
``apcore-toolkit/docs/features/openapi.md#ref-sibling-keys-are-preserved``).

The sibling-merge half of this contract is a security property, not a fidelity
nicety: apcore reads ``x-sensitive`` off the *resolved* schema to decide what to
redact, and this toolkit produces the schemas apcore reads. A marking dropped
here is a credential logged in plaintext downstream.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from apcore_toolkit import deep_resolve_refs

_CONFORMANCE_DIR = Path(__file__).resolve().parent.parent.parent / "apcore-toolkit" / "conformance" / "fixtures"
_FIXTURE = _CONFORMANCE_DIR / "ref_resolution.json"

if not _FIXTURE.exists():  # pragma: no cover - the corpus repo is a sibling checkout
    pytest.skip(f"shared conformance corpus not found at {_FIXTURE}", allow_module_level=True)

_CASES: list[dict[str, Any]] = json.loads(_FIXTURE.read_text(encoding="utf-8"))["test_cases"]


@pytest.mark.parametrize("case", _CASES, ids=[c["id"] for c in _CASES])
def test_ref_resolution_matches_shared_corpus(case: dict[str, Any]) -> None:
    got = deep_resolve_refs(case["input"]["schema"], case["input"]["openapi_doc"])
    assert got == case["expected"], case["description"]


def test_every_fixture_case_is_covered() -> None:
    """A case the harness never runs reports coverage that does not exist."""
    assert _CASES, "the shared corpus is empty"
    assert len({c["id"] for c in _CASES}) == len(_CASES), "duplicate case ids in the corpus"


def test_input_schema_is_not_mutated() -> None:
    """``deep_resolve_refs`` is documented pure; the merge must not write back
    into the caller's schema."""
    schema = {"$ref": "#/components/schemas/T", "x-sensitive": True}
    before = json.dumps(schema, sort_keys=True)
    deep_resolve_refs(schema, {"components": {"schemas": {"T": {"type": "string"}}}})
    assert json.dumps(schema, sort_keys=True) == before
