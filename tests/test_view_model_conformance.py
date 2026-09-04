"""Conformance harness: assert Python's TuiViewModel reference impl matches
the shared fixture corpus at ``apcore-toolkit/conformance/fixtures/view_model.json``.

The TypeScript and Rust SDKs run the same fixture file through their own
``modules_to_view_model`` / ``format_view_model`` and assert byte-identical
JSON output for ``expected``. This is the cross-SDK byte-identity contract
for the TUI View Model proposal (see
``apcore-toolkit/docs/features/tui-view-model.md``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from apcore_toolkit.tui_view_model import Filter, Sort, ToneRule, TonePalette, format_view_model, modules_to_view_model
from apcore_toolkit.types import ScannedModule

_CONFORMANCE_DIR = Path(__file__).resolve().parent.parent.parent / "apcore-toolkit" / "conformance" / "fixtures"


def _load_fixture() -> list[dict[str, Any]]:
    path = _CONFORMANCE_DIR / "view_model.json"
    if not path.exists():
        pytest.skip(f"conformance fixture not found at {path}", allow_module_level=True)
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["test_cases"]


def _build_module(raw: dict[str, Any]) -> ScannedModule:
    annotations = None
    raw_ann = raw.get("annotations")
    if raw_ann is not None:
        from apcore import ModuleAnnotations

        annotations = ModuleAnnotations(
            discoverable=raw_ann.get("discoverable", True),
            extra=raw_ann.get("extra") or {},
        )
    return ScannedModule(
        module_id=raw["module_id"],
        description=raw.get("description", ""),
        input_schema={},
        output_schema={},
        tags=list(raw.get("tags", [])),
        target="fixture:noop",
        annotations=annotations,
        display=raw.get("display"),
    )


def _build_filter(raw: dict[str, Any] | None) -> Filter | None:
    if raw is None:
        return None
    return Filter(
        tags=tuple(raw.get("tags", [])),
        search=raw.get("search", ""),
        annotations=tuple(raw.get("annotations", [])),
        exposure=raw.get("exposure", "all"),
        deprecated=raw.get("deprecated", True),
    )


def _build_sort(raw: dict[str, Any] | None) -> Sort | None:
    if raw is None:
        return None
    return Sort(key=raw["key"], direction=raw.get("direction", "asc"))


def _build_tone_palettes(raw: list[dict[str, Any]] | None) -> tuple[TonePalette, ...]:
    if not raw:
        return ()
    palettes = []
    for p in raw:
        rules = [ToneRule(value=r["value"], tone=r["tone"]) for r in p.get("rules", [])]
        palettes.append(TonePalette(name=p["name"], rules=rules))
    return tuple(palettes)


_CASES = _load_fixture()


@pytest.mark.parametrize("case", _CASES, ids=lambda c: c["id"])
def test_view_model_conformance(case: dict[str, Any]) -> None:
    inp = case["input"]
    options = inp.get("options", {})

    modules = [_build_module(m) for m in inp["modules"]]
    vm = modules_to_view_model(
        modules,
        view=options.get("view", "list"),
        columns=tuple(options.get("columns", [])),
        title=options.get("title"),
        filter=_build_filter(options.get("filter")),
        sort=_build_sort(options.get("sort")),
        group_by=options.get("group_by"),
        tone_palettes=_build_tone_palettes(options.get("tone_palettes")),
        display=options.get("display", True),
    )
    actual = format_view_model(vm)
    assert actual == case["expected"], (
        f"\nCase {case['id']}: {case['description']}\n" f"Expected: {case['expected']!r}\n" f"Actual:   {actual!r}"
    )
