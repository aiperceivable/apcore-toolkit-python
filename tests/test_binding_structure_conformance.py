"""Verify shared binding-structure behavior through public loader entry points."""

import copy
import json
from pathlib import Path

import pytest
import yaml

from apcore_toolkit import BindingLoadError, BindingLoader

_FIXTURE = Path(__file__).resolve().parents[2] / "apcore-toolkit/conformance/fixtures/binding_structure.json"
_CASES = json.loads(_FIXTURE.read_text())["test_cases"]


@pytest.mark.parametrize("strict", [False, True], ids=["loose", "strict"])
@pytest.mark.parametrize("case", _CASES, ids=lambda case: case["id"])
def test_binding_structure(case: dict, strict: bool, tmp_path: Path) -> None:
    data = copy.deepcopy(case["data"])
    file_path = tmp_path / "case.binding.yaml"
    file_path.write_text(yaml.safe_dump(data))
    loader = BindingLoader()
    expected_error = case.get("strict_error" if strict else "loose_error")
    for load in [lambda: loader.load_data(data, strict=strict), lambda: loader.load(file_path, strict=strict)]:
        if expected_error:
            with pytest.raises(BindingLoadError, match=expected_error):
                load()
        else:
            modules = load()
            assert len(modules) == len(data["bindings"])
            for field, expected in case.get("fields", {}).items():
                assert getattr(modules[0], field) == expected
    assert data == case["data"]
