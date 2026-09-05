"""Conformance harness: assert Python's OpenAPIScanner reference impl matches
the shared fixture corpus at
``apcore-toolkit/conformance/fixtures/openapi_scan.json``.

The TypeScript and Rust SDKs run the same fixture file through their own
``OpenAPIScanner`` and assert structurally identical (parsed-JSON deep
equality) module lists — unlike ``view_model.json``, ``expected`` here is a
structured object, not a canonical string, since ``ScannedModule`` output is
compared field-by-field rather than byte-for-byte. See
``apcore-toolkit/docs/features/openapi-scanner.md``.

Fixture cases ``openapi_scan_021`` through ``openapi_scan_023`` install a
named test-only hook from ``_HOOKS`` below — the fixture's ``input.hooks``
key names which one, so all three SDKs install byte-identical hook behavior
without serializing a callable through JSON.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from apcore_toolkit.openapi_scanner import OpenAPIScanner

_CONFORMANCE_DIR = Path(__file__).resolve().parent.parent.parent / "apcore-toolkit" / "conformance" / "fixtures"


def _load_fixture() -> list[dict[str, Any]]:
    path = _CONFORMANCE_DIR / "openapi_scan.json"
    if not path.exists():
        pytest.skip(f"conformance fixture not found at {path}", allow_module_level=True)
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["test_cases"]


def _skip_if_x_skip_true(path: str, method: str, operation: dict[str, Any]) -> dict[str, Any] | None:
    if operation.get("x-skip"):
        return None
    return operation


def _custom_name_for_operation_id_custom_else_default(path: str, method: str, operation: dict[str, Any]) -> str | None:
    if operation.get("operationId") == "custom":
        return "custom.name"
    return None


def _always_returns_dup_op(path: str, method: str, operation: dict[str, Any]) -> str:
    return "dup.op"


_HOOKS: dict[str, Any] = {
    "skip_if_x_skip_true": ("transform_operation", _skip_if_x_skip_true),
    "custom_name_for_operation_id_custom_else_default": (
        "derive_module_id",
        _custom_name_for_operation_id_custom_else_default,
    ),
    "always_returns_dup_op": ("derive_module_id", _always_returns_dup_op),
}


def _module_repr(m: Any) -> dict[str, Any]:
    ann = m.annotations
    ann_dict: dict[str, Any] = {}
    if ann is not None:
        for flag in ("readonly", "destructive", "idempotent", "cacheable"):
            if getattr(ann, flag, False):
                ann_dict[flag] = True
        if ann.extra:
            ann_dict["extra"] = ann.extra
    return {
        "module_id": m.module_id,
        "description": m.description,
        "documentation": m.documentation,
        "tags": m.tags,
        "version": m.version,
        "target": m.target,
        "annotations": ann_dict,
        "metadata": m.metadata,
        "input_schema": m.input_schema,
        "output_schema": m.output_schema,
        "warnings": m.warnings,
    }


_CASES = _load_fixture()


@pytest.mark.parametrize("case", _CASES, ids=lambda c: c["id"])
def test_openapi_scan_conformance(case: dict[str, Any]) -> None:
    inp = case["input"]
    exp = case["expected"]
    spec = inp["spec"]
    options = dict(inp.get("options", {}))

    for hook_key, hook_name in (inp.get("hooks") or {}).items():
        _, fn = _HOOKS[hook_name]
        options[hook_key] = fn

    scanner = OpenAPIScanner()

    if "raises" in exp:
        with pytest.raises(ValueError):
            scanner.scan(spec, **options)
        return

    modules = scanner.scan(spec, **options)
    actual = [_module_repr(m) for m in modules]
    assert actual == exp["modules"], (
        f"\nCase {case['id']}: {case['description']}\n"
        f"Expected: {json.dumps(exp['modules'], indent=2)}\n"
        f"Actual:   {json.dumps(actual, indent=2)}"
    )
