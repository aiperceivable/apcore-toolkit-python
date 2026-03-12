"""Tests for apcore_toolkit.scanner — BaseScanner ABC."""

from __future__ import annotations

from typing import Any

from apcore_toolkit.scanner import BaseScanner
from apcore_toolkit.types import ScannedModule


class ConcreteScanner(BaseScanner):
    """Minimal concrete scanner for testing."""

    def scan(self, **kwargs: Any) -> list[ScannedModule]:
        return []

    def get_source_name(self) -> str:
        return "test-scanner"


def _make_module(module_id: str) -> ScannedModule:
    return ScannedModule(
        module_id=module_id,
        description=f"Module {module_id}",
        input_schema={"type": "object", "properties": {}},
        output_schema={"type": "object", "properties": {}},
        tags=[],
        target="mod:func",
    )


class TestFilterModules:
    def setup_method(self) -> None:
        self.scanner = ConcreteScanner()
        self.modules = [
            _make_module("users.list"),
            _make_module("users.create"),
            _make_module("tasks.list"),
            _make_module("tasks.delete"),
        ]

    def test_no_filters(self) -> None:
        result = self.scanner.filter_modules(self.modules)
        assert len(result) == 4

    def test_include_only(self) -> None:
        result = self.scanner.filter_modules(self.modules, include=r"^users\.")
        assert len(result) == 2
        assert all(m.module_id.startswith("users.") for m in result)

    def test_exclude_only(self) -> None:
        result = self.scanner.filter_modules(self.modules, exclude=r"\.delete$")
        assert len(result) == 3
        assert all("delete" not in m.module_id for m in result)

    def test_include_and_exclude(self) -> None:
        result = self.scanner.filter_modules(self.modules, include=r"tasks\.", exclude=r"delete")
        assert len(result) == 1
        assert result[0].module_id == "tasks.list"

    def test_include_matches_none(self) -> None:
        result = self.scanner.filter_modules(self.modules, include=r"^nonexistent")
        assert result == []

    def test_exclude_matches_all(self) -> None:
        result = self.scanner.filter_modules(self.modules, exclude=r".*")
        assert result == []


class TestDeduplicateIds:
    def setup_method(self) -> None:
        self.scanner = ConcreteScanner()

    def test_no_duplicates(self) -> None:
        modules = [_make_module("a"), _make_module("b"), _make_module("c")]
        result = self.scanner.deduplicate_ids(modules)
        assert [m.module_id for m in result] == ["a", "b", "c"]

    def test_two_duplicates(self) -> None:
        modules = [_make_module("x"), _make_module("x")]
        result = self.scanner.deduplicate_ids(modules)
        assert [m.module_id for m in result] == ["x", "x_2"]

    def test_three_duplicates(self) -> None:
        modules = [_make_module("x"), _make_module("x"), _make_module("x")]
        result = self.scanner.deduplicate_ids(modules)
        assert [m.module_id for m in result] == ["x", "x_2", "x_3"]

    def test_mixed_duplicates(self) -> None:
        modules = [_make_module("a"), _make_module("b"), _make_module("a"), _make_module("b"), _make_module("a")]
        result = self.scanner.deduplicate_ids(modules)
        assert [m.module_id for m in result] == ["a", "b", "a_2", "b_2", "a_3"]

    def test_original_modules_unchanged(self) -> None:
        modules = [_make_module("x"), _make_module("x")]
        result = self.scanner.deduplicate_ids(modules)
        assert modules[0].module_id == "x"
        assert modules[1].module_id == "x"
        assert result[1].module_id == "x_2"

    def test_empty_list(self) -> None:
        result = self.scanner.deduplicate_ids([])
        assert result == []


class TestDeduplicateWarnings:
    def setup_method(self) -> None:
        self.scanner = ConcreteScanner()

    def test_first_occurrence_has_no_warning(self) -> None:
        modules = [_make_module("x"), _make_module("x")]
        result = self.scanner.deduplicate_ids(modules)
        assert result[0].warnings == []

    def test_renamed_module_has_warning(self) -> None:
        modules = [_make_module("x"), _make_module("x")]
        result = self.scanner.deduplicate_ids(modules)
        assert len(result[1].warnings) == 1
        assert "renamed" in result[1].warnings[0]
        assert "'x'" in result[1].warnings[0]
        assert "'x_2'" in result[1].warnings[0]

    def test_existing_warnings_preserved(self) -> None:
        m = _make_module("x")
        m.warnings.append("pre-existing warning")
        modules = [_make_module("x"), m]
        result = self.scanner.deduplicate_ids(modules)
        assert len(result[1].warnings) == 2
        assert result[1].warnings[0] == "pre-existing warning"
        assert "renamed" in result[1].warnings[1]


class TestInferAnnotationsFromMethod:
    def test_get_readonly(self) -> None:
        ann = BaseScanner.infer_annotations_from_method("GET")
        assert ann.readonly is True
        assert ann.cacheable is True
        assert ann.destructive is False

    def test_delete_destructive(self) -> None:
        ann = BaseScanner.infer_annotations_from_method("DELETE")
        assert ann.destructive is True
        assert ann.readonly is False

    def test_put_idempotent(self) -> None:
        ann = BaseScanner.infer_annotations_from_method("PUT")
        assert ann.idempotent is True

    def test_post_default(self) -> None:
        ann = BaseScanner.infer_annotations_from_method("POST")
        assert ann.readonly is False
        assert ann.destructive is False
        assert ann.idempotent is False
        assert ann.cacheable is False

    def test_case_insensitive(self) -> None:
        ann = BaseScanner.infer_annotations_from_method("get")
        assert ann.readonly is True

    def test_patch_default(self) -> None:
        ann = BaseScanner.infer_annotations_from_method("PATCH")
        assert ann.readonly is False
        assert ann.destructive is False


class TestExtractDocstring:
    def test_function_with_docstring(self) -> None:
        def sample():
            """First line.

            Extended description here.
            """

        scanner = ConcreteScanner()
        desc, doc, params = scanner.extract_docstring(sample)
        assert desc is not None
        assert "First line" in desc

    def test_function_without_docstring(self) -> None:
        def no_doc():
            pass

        scanner = ConcreteScanner()
        desc, doc, params = scanner.extract_docstring(no_doc)
        assert desc is None
        assert doc is None


class TestAbstractInterface:
    def test_scan_returns_list(self) -> None:
        scanner = ConcreteScanner()
        assert scanner.scan() == []

    def test_get_source_name(self) -> None:
        scanner = ConcreteScanner()
        assert scanner.get_source_name() == "test-scanner"
