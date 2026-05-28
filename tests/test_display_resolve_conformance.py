"""Conformance harness for DisplayResolver — asserts the Python reference
impl matches the shared fixture corpus at
``apcore-toolkit/conformance/fixtures/display_resolve.json``.

The TypeScript and Rust SDKs run the same fixture cases through their own
DisplayResolver implementations and assert identical resolved output. This
is the cross-SDK behavioral contract for the display-overlay resolution
priority chain (surface-specific > display default > binding-level >
scanner-provided).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from apcore_toolkit.display.resolver import DisplayResolver
from apcore_toolkit.types import ScannedModule

_CONFORMANCE_DIR = Path(__file__).resolve().parent.parent.parent / "apcore-toolkit" / "conformance" / "fixtures"


def _load_fixture() -> list[dict[str, Any]]:
    path = _CONFORMANCE_DIR / "display_resolve.json"
    if not path.exists():
        pytest.skip(f"conformance fixture not found at {path}", allow_module_level=True)
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["test_cases"]


def _build_module(raw: dict[str, Any]) -> ScannedModule:
    """Construct a ScannedModule from the fixture's compact module shape.

    The fixture omits ``input_schema``, ``output_schema``, and ``target`` —
    DisplayResolver does not read them, so we default them to inert values.
    """
    return ScannedModule(
        module_id=raw["module_id"],
        description=raw.get("description", ""),
        input_schema=raw.get("input_schema", {}),
        output_schema=raw.get("output_schema", {}),
        tags=list(raw.get("tags", [])),
        target=raw.get("target", "fixture:noop"),
        documentation=raw.get("documentation"),
        metadata=dict(raw.get("metadata", {})),
    )


def _assert_partial_match(expected: dict[str, Any], actual: dict[str, Any], path: str = "display") -> None:
    """Assert every key in ``expected`` is present in ``actual`` with an
    equal value. Permits ``actual`` to carry additional keys.
    """
    for key, exp_val in expected.items():
        assert key in actual, f"missing key {path}.{key} in resolved display"
        act_val = actual[key]
        if isinstance(exp_val, dict) and isinstance(act_val, dict):
            _assert_partial_match(exp_val, act_val, f"{path}.{key}")
        else:
            assert act_val == exp_val, f"\nMismatch at {path}.{key}\nExpected: {exp_val!r}\nActual:   {act_val!r}"


_CASES = _load_fixture()


@pytest.mark.parametrize("case", _CASES, ids=lambda c: c["id"])
def test_display_resolve_conformance(case: dict[str, Any]) -> None:
    inp = case["input"]
    exp = case["expected"]
    binding_map = inp.get("binding_map", {})
    resolver = DisplayResolver()

    # Error cases (e.g. MCP alias exceeds 64-char hard limit)
    if "error" in exp:
        module = _build_module(inp["scanned_module"])
        with pytest.raises(ValueError) as excinfo:
            resolver.resolve([module], binding_data=binding_map)
        # Resolver raises with a message containing the surface and a hint;
        # don't lock the exact wording here.
        assert exp.get("surface", "") in str(excinfo.value) or exp.get("error", "") in str(excinfo.value)
        return

    # Multi-module case (results[])
    if "scanned_modules" in inp:
        modules = [_build_module(m) for m in inp["scanned_modules"]]
        resolved = resolver.resolve(modules, binding_data=binding_map)
        for exp_result in exp["results"]:
            mod = next(m for m in resolved if m.module_id == exp_result["module_id"])
            actual_display = mod.metadata.get("display", {})
            _assert_partial_match(exp_result["display"], actual_display)
        return

    # Warning + single-module fallback case (e.g. CLI alias with spaces)
    if "warning" in exp:
        module = _build_module(inp["scanned_module"])
        resolved = resolver.resolve([module], binding_data=binding_map)
        actual_display = resolved[0].metadata.get("display", {})
        _assert_partial_match(exp["display"], actual_display)
        return

    # Single-module display-equality case (cases 1-9, 11-12)
    module = _build_module(inp["scanned_module"])
    resolved = resolver.resolve([module], binding_data=binding_map)
    actual_display = resolved[0].metadata.get("display", {})
    _assert_partial_match(exp["display"], actual_display)
