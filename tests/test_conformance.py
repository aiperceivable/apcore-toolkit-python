"""Tests for apcore_toolkit.conformance.assert_annotations_preserved."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from apcore import ModuleAnnotations, Registry

from apcore_toolkit.conformance import assert_annotations_preserved
from apcore_toolkit.output.registry_writer import RegistryWriter
from apcore_toolkit.types import ScannedModule


def _handler(user_id: int) -> dict:
    return {"user_id": user_id}


def _module(annotations: ModuleAnnotations | None) -> ScannedModule:
    return ScannedModule(
        module_id="orders.delete_order",
        description="Delete an order",
        input_schema={},
        output_schema={},
        tags=[],
        target="unused:patched",
        annotations=annotations,
    )


class _DroppingWriter(RegistryWriter):
    """Reproduces the historical adapter bug: overrides _to_function_module and
    forgets to forward annotations."""

    def _to_function_module(self, mod, *, allowed_prefixes=None):  # type: ignore[no-untyped-def]
        from apcore import FunctionModule

        # Reference the module-level names so the test's patch of
        # ``registry_writer.resolve_target`` applies here too.
        from apcore_toolkit.output import registry_writer as rw

        func = rw.flatten_pydantic_params(rw.resolve_target(mod.target, allowed_prefixes=allowed_prefixes))
        return FunctionModule(func=func, module_id=mod.module_id, description=mod.description)


class TestAssertAnnotationsPreserved:
    def test_passes_for_correct_writer(self) -> None:
        mod = _module(ModuleAnnotations(destructive=True, requires_approval=True))
        with patch("apcore_toolkit.output.registry_writer.resolve_target", return_value=_handler):
            assert_annotations_preserved(RegistryWriter(), mod, Registry())

    def test_raises_for_writer_that_drops_annotations(self) -> None:
        mod = _module(ModuleAnnotations(requires_approval=True))
        with patch("apcore_toolkit.output.registry_writer.resolve_target", return_value=_handler):
            with pytest.raises(AssertionError, match="requires_approval"):
                assert_annotations_preserved(_DroppingWriter(), mod, Registry())

    def test_requires_annotations_on_input_module(self) -> None:
        with pytest.raises(AssertionError, match="annotations to be set"):
            assert_annotations_preserved(RegistryWriter(), _module(None), Registry())

    def test_custom_fields(self) -> None:
        mod = _module(ModuleAnnotations(readonly=True))
        with patch("apcore_toolkit.output.registry_writer.resolve_target", return_value=_handler):
            assert_annotations_preserved(RegistryWriter(), mod, Registry(), fields=("readonly",))
