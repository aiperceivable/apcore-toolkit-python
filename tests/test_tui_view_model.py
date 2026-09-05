"""Unit tests for TuiViewModel filter semantics (§ tui-view-model).

Complements the byte-equivalence conformance suite in
``tests/test_view_model_conformance.py`` (driven by the shared fixture
corpus) with targeted unit coverage for ``Filter.annotations`` name
recognition — specifically that only the 9 canonical boolean
``ModuleAnnotations`` flags are recognized, not arbitrary attribute names.
"""

from __future__ import annotations

import dataclasses

import pytest
from apcore import DEFAULT_ANNOTATIONS

from apcore_toolkit.tui_view_model import Filter, modules_to_view_model
from apcore_toolkit.types import ScannedModule

# The 9 canonical boolean flags `filter.annotations` is allowed to recognize.
_CANONICAL_FLAGS = (
    "readonly",
    "destructive",
    "idempotent",
    "requires_approval",
    "open_world",
    "streaming",
    "cacheable",
    "paginated",
    "discoverable",
)


def _module(annotations) -> ScannedModule:
    return ScannedModule(
        module_id="m.one",
        description="desc",
        input_schema={},
        output_schema={},
        tags=[],
        target="fixture:noop",
        annotations=annotations,
    )


class TestAnnotationFilterCanonicalFlags:
    """Positive path: the 9 canonical boolean flags still work as before."""

    @pytest.mark.parametrize("flag_name", _CANONICAL_FLAGS)
    def test_flag_true_includes_module(self, flag_name):
        annotations = dataclasses.replace(DEFAULT_ANNOTATIONS, **{flag_name: True})
        modules = [_module(annotations)]
        vm = modules_to_view_model(modules, columns=("module_id",), filter=Filter(annotations=(flag_name,)))
        assert len(vm.rows) == 1

    @pytest.mark.parametrize("flag_name", _CANONICAL_FLAGS)
    def test_flag_false_excludes_module(self, flag_name):
        annotations = dataclasses.replace(DEFAULT_ANNOTATIONS, **{flag_name: False})
        modules = [_module(annotations)]
        vm = modules_to_view_model(modules, columns=("module_id",), filter=Filter(annotations=(flag_name,)))
        assert len(vm.rows) == 0


class TestAnnotationFilterRejectsNonCanonicalNames:
    """Regression: non-boolean/non-canonical attribute names must not be
    reflected via getattr() — they must exclude the module instead of
    accidentally evaluating an unrelated field truthily."""

    def test_pagination_style_is_not_a_recognized_flag(self):
        # `pagination_style` defaults to "cursor", which is truthy as a
        # string. Under the old `getattr(annotations, name, False)`
        # reflection this would have made the module pass the filter.
        annotations = DEFAULT_ANNOTATIONS
        assert annotations.pagination_style == "cursor"
        modules = [_module(annotations)]
        vm = modules_to_view_model(
            modules, columns=("module_id",), filter=Filter(annotations=("pagination_style",))
        )
        assert len(vm.rows) == 0

    def test_cache_ttl_is_not_a_recognized_flag(self):
        # `cache_ttl` set to a truthy int would also have passed the old
        # reflection-based filter.
        annotations = dataclasses.replace(DEFAULT_ANNOTATIONS, cache_ttl=60)
        modules = [_module(annotations)]
        vm = modules_to_view_model(modules, columns=("module_id",), filter=Filter(annotations=("cache_ttl",)))
        assert len(vm.rows) == 0

    def test_extra_is_not_a_recognized_annotations_flag(self):
        # `extra` is a dict; a non-empty dict is truthy, but `extra` is not
        # one of the 9 canonical boolean flags this filter recognizes.
        annotations = dataclasses.replace(DEFAULT_ANNOTATIONS, extra={"deprecated": False, "note": "x"})
        modules = [_module(annotations)]
        vm = modules_to_view_model(modules, columns=("module_id",), filter=Filter(annotations=("extra",)))
        assert len(vm.rows) == 0
