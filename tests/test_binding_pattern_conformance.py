"""Conformance harness: assert Python's ``BindingLoader.load(..., pattern=...)``
matches the shared fixture corpus at
``apcore-toolkit/conformance/fixtures/binding_pattern.json``.

The TypeScript and Rust SDKs run the same fixture file through their own
matcher and directory-selection code and must produce identical answers. This
is the cross-SDK behavioural contract for the ``pattern`` argument (see
``apcore-toolkit/docs/features/binding-loader.md#pattern-matching``).

Two case kinds. There is no ``validate`` kind: as of 0.13.0 every string is a
valid pattern and the loader never raises on one for syntactic reasons,
matching apcore's Algorithm A25 requirement 2.

``match``
    The pure name matcher, with no filesystem involved.

``select``
    How ``pattern`` composes with ``recursive`` over a real directory tree.
    Each entry of ``input.files`` is materialized under ``tmp_path`` (parent
    directories created as needed), so an entry that is a strict path prefix of
    another entry is a *directory*, not a file, and must never be selected
    (case 034). A case may also carry ``input.symlinks`` (link -> target,
    both relative to the root) plus ``requires: "symlinks"``; where symlink
    creation is unavailable those cases are SKIPPED with a visible reason,
    never silently passed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from apcore_toolkit.binding_loader import (
    BindingLoader,
    _match_name,
    _select_files,
)

#: Case kinds this harness dispatches on. A fixture case carrying anything else
#: would otherwise be parametrized into no test at all and pass vacuously.
_DISPATCHED_KINDS = frozenset({"match", "select"})

_CONFORMANCE_DIR = Path(__file__).resolve().parent.parent.parent / "apcore-toolkit" / "conformance" / "fixtures"


def _load_fixture() -> list[dict[str, Any]]:
    path = _CONFORMANCE_DIR / "binding_pattern.json"
    if not path.exists():
        pytest.skip(f"conformance fixture not found at {path}", allow_module_level=True)
    data = json.loads(path.read_text(encoding="utf-8"))
    cases: list[dict[str, Any]] = data["test_cases"]
    return cases


_CASES = _load_fixture()
_MATCH_CASES = [c for c in _CASES if c["kind"] == "match"]
_SELECT_CASES = [c for c in _CASES if c["kind"] == "select"]


def _binding_yaml(relative_path: str) -> str:
    """A parseable one-entry binding document whose ``module_id`` is the file's
    own path relative to the tree root.

    This is what lets the harness recover *which files the loader read* from
    the value ``load`` actually returns, without weakening any case: the loader
    emits one ``ScannedModule`` per selected file, in the order it read them.
    """
    return (
        "spec_version: '1.0'\nbindings:\n  - module_id: " + json.dumps(relative_path) + "\n    target: fixture:noop\n"
    )


def _materialize(root: Path, entries: list[str]) -> None:
    """Create ``entries`` under ``root``.

    Per the fixture's harness convention, an entry that is a strict path prefix
    of another entry is a DIRECTORY, not a file.
    """
    directories = {e for e in entries if any(o != e and o.startswith(e + "/") for o in entries)}
    for entry in entries:
        target = root.joinpath(*entry.split("/"))
        if entry in directories:
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_binding_yaml(entry), encoding="utf-8")


def _make_symlinks(root: Path, symlinks: dict[str, str]) -> None:
    """Create ``input.symlinks`` (link name -> target, both relative to ``root``).

    Targets are created by :func:`_materialize` first, so ``target_is_directory``
    can be resolved from the real entry — it matters on Windows, where the two
    symlink flavours are distinct.
    """
    for link, target in symlinks.items():
        link_path = root.joinpath(*link.split("/"))
        target_path = root.joinpath(*target.split("/"))
        link_path.parent.mkdir(parents=True, exist_ok=True)
        link_path.symlink_to(target_path, target_is_directory=target_path.is_dir())


@pytest.mark.parametrize("case", _MATCH_CASES, ids=lambda c: c["id"])
def test_binding_pattern_match(case: dict[str, Any]) -> None:
    actual = _match_name(case["input"]["pattern"], case["input"]["name"])
    assert actual is case["expected"]["matches"], (
        f"\nCase {case['id']}: {case['description']}\n"
        f"pattern={case['input']['pattern']!r} name={case['input']['name']!r}\n"
        f"Expected: {case['expected']['matches']!r}\nActual:   {actual!r}"
    )


@pytest.mark.parametrize("case", _SELECT_CASES, ids=lambda c: c["id"])
def test_binding_pattern_select(case: dict[str, Any], tmp_path: Path) -> None:
    inp = case["input"]
    root = tmp_path / "tree"
    root.mkdir()
    _materialize(root, list(inp["files"]))

    symlinks: dict[str, str] = inp.get("symlinks") or {}
    if symlinks:
        assert case.get("requires") == "symlinks", f"{case['id']} has input.symlinks but no requires marker"
        try:
            _make_symlinks(root, symlinks)
        except (OSError, NotImplementedError) as exc:
            # Never silently pass a case whose precondition could not be met.
            pytest.skip(f"{case['id']} requires symlink creation, unavailable here: {exc!r}")

    # Assert on the selected *paths*, not on parsed module_ids: case 040
    # selects a symlink alias whose target file carries the target's own id,
    # so the alias is unrecoverable from the parse result. ``_select_files``
    # is the same call ``load`` makes, so this is the real selection contract.
    selected = [
        f.relative_to(root).as_posix() for f in _select_files(root, recursive=inp["recursive"], pattern=inp["pattern"])
    ]

    assert selected == case["expected"]["selected"], (
        f"\nCase {case['id']}: {case['description']}\n"
        f"Expected: {case['expected']['selected']!r}\nActual:   {selected!r}"
    )

    # And end-to-end: ``load`` really reads exactly those files. Each
    # materialized file embeds its own root-relative path as module_id, so for
    # symlink-free cases the parsed ids are the selected paths verbatim.
    modules = BindingLoader().load(root, recursive=inp["recursive"], pattern=inp["pattern"])
    assert len(modules) == len(case["expected"]["selected"])
    if not symlinks:
        assert [m.module_id for m in modules] == case["expected"]["selected"]


def test_every_fixture_case_is_covered() -> None:
    """Guard against a fixture case kind silently going unrun.

    Derived from the fixture rather than a hardcoded count, so the corpus can
    grow without a false failure here — but a case carrying a ``kind`` this
    harness does not dispatch on (which would be parametrized into no test at
    all, and pass vacuously) still fails loudly.
    """
    assert _CASES, f"fixture at {_CONFORMANCE_DIR / 'binding_pattern.json'} has no test_cases"
    unknown = sorted({c["kind"] for c in _CASES} - _DISPATCHED_KINDS)
    assert not unknown, f"fixture has case kinds this harness does not run: {unknown}"
    assert len(_CASES) == len(_MATCH_CASES) + len(_SELECT_CASES)
