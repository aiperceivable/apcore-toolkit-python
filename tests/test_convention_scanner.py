"""Tests for ConventionScanner (§5.14)."""

import logging
import sys

import pytest
from unittest.mock import patch
from apcore_toolkit.convention_scanner import ConventionScanner
from apcore_toolkit.scanner import BaseScanner


@pytest.fixture
def scanner():
    return ConventionScanner()


class TestConventionScannerBasic:
    def test_scan_simple_function(self, scanner, tmp_path):
        """Scan a file with one typed function."""
        (tmp_path / "deploy.py").write_text(
            'def deploy(env: str, tag: str = "latest") -> dict:\n' '    """Deploy the app."""\n' "    return {}\n"
        )
        modules = scanner.scan(tmp_path)
        assert len(modules) == 1
        m = modules[0]
        assert m.module_id == "deploy.deploy"
        assert m.description == "Deploy the app."
        assert "env" in m.input_schema["properties"]
        assert m.input_schema["required"] == ["env"]
        assert m.input_schema["properties"]["tag"]["default"] == "latest"

    def test_scan_skips_private_functions(self, scanner, tmp_path):
        (tmp_path / "utils.py").write_text(
            'def public_func(x: int) -> int:\n    """Public."""\n    return x\n\n'
            'def _private_func(x: int) -> int:\n    """Private."""\n    return x\n'
        )
        modules = scanner.scan(tmp_path)
        assert len(modules) == 1
        assert modules[0].module_id == "utils.public_func"

    def test_scan_skips_underscore_files(self, scanner, tmp_path):
        (tmp_path / "__init__.py").write_text("# init\n")
        (tmp_path / "_helpers.py").write_text("def helper(): pass\n")
        (tmp_path / "real.py").write_text('def func(x: str) -> str:\n    """Real."""\n    return x\n')
        modules = scanner.scan(tmp_path)
        assert len(modules) == 1
        assert modules[0].module_id == "real.func"

    def test_scan_subdirectory_prefix(self, scanner, tmp_path):
        sub = tmp_path / "monitoring"
        sub.mkdir()
        (sub / "health.py").write_text('def check(url: str) -> dict:\n    """Check health."""\n    return {}\n')
        modules = scanner.scan(tmp_path)
        assert len(modules) == 1
        assert modules[0].module_id == "monitoring.health.check"

    def test_module_prefix_override(self, scanner, tmp_path):
        (tmp_path / "deploy.py").write_text(
            'MODULE_PREFIX = "ops"\n\n' 'def deploy(env: str) -> dict:\n    """Deploy."""\n    return {}\n'
        )
        modules = scanner.scan(tmp_path)
        assert modules[0].module_id == "ops.deploy"

    def test_cli_group_in_metadata(self, scanner, tmp_path):
        (tmp_path / "deploy.py").write_text(
            'CLI_GROUP = "ops"\n\n' 'def deploy(env: str) -> dict:\n    """Deploy."""\n    return {}\n'
        )
        modules = scanner.scan(tmp_path)
        assert modules[0].metadata["display"]["cli"]["group"] == "ops"

    def test_tags_from_constant(self, scanner, tmp_path):
        (tmp_path / "deploy.py").write_text(
            'TAGS = ["devops", "deploy"]\n\n' 'def deploy(env: str) -> dict:\n    """Deploy."""\n    return {}\n'
        )
        modules = scanner.scan(tmp_path)
        assert modules[0].tags == ["devops", "deploy"]

    def test_scan_empty_dir(self, scanner, tmp_path):
        modules = scanner.scan(tmp_path)
        assert modules == []

    def test_scan_nonexistent_dir(self, scanner, tmp_path):
        modules = scanner.scan(tmp_path / "nonexistent")
        assert modules == []

    def test_no_docstring_uses_default(self, scanner, tmp_path):
        (tmp_path / "nodoc.py").write_text("def func(x: str) -> str:\n    return x\n")
        modules = scanner.scan(tmp_path)
        assert modules[0].description == "(no description)"

    def test_include_filter(self, scanner, tmp_path):
        (tmp_path / "a.py").write_text('def func1(x: str) -> str:\n    """A."""\n    return x\n')
        (tmp_path / "b.py").write_text('def func2(x: str) -> str:\n    """B."""\n    return x\n')
        modules = scanner.scan(tmp_path, include=r"^a\.")
        assert len(modules) == 1
        assert modules[0].module_id == "a.func1"

    def test_exclude_filter(self, scanner, tmp_path):
        (tmp_path / "a.py").write_text('def func1(x: str) -> str:\n    """A."""\n    return x\n')
        (tmp_path / "b.py").write_text('def func2(x: str) -> str:\n    """B."""\n    return x\n')
        modules = scanner.scan(tmp_path, exclude=r"^b\.")
        assert len(modules) == 1
        assert modules[0].module_id == "a.func1"

    def test_multiple_functions_per_file(self, scanner, tmp_path):
        (tmp_path / "math_ops.py").write_text(
            'def add(a: int, b: int) -> int:\n    """Add two numbers."""\n    return a + b\n\n'
            'def sub(a: int, b: int) -> int:\n    """Subtract b from a."""\n    return a - b\n'
        )
        modules = scanner.scan(tmp_path)
        assert len(modules) == 2
        ids = {m.module_id for m in modules}
        assert ids == {"math_ops.add", "math_ops.sub"}

    def test_output_schema_dict(self, scanner, tmp_path):
        (tmp_path / "f.py").write_text('def func() -> dict:\n    """F."""\n    return {}\n')
        modules = scanner.scan(tmp_path)
        assert modules[0].output_schema == {"type": "object"}

    def test_no_return_type_empty_output_schema(self, scanner, tmp_path):
        (tmp_path / "f.py").write_text('def func(x: str):\n    """F."""\n    pass\n')
        modules = scanner.scan(tmp_path)
        assert modules[0].output_schema == {}


class TestConventionScannerFailureLogging:
    """Verify scan() preserves traceback context on per-file failures."""

    def test_import_failure_log_includes_traceback(self, scanner, tmp_path, caplog):
        (tmp_path / "broken.py").write_text("def _bang():\n    raise RuntimeError('kaboom at import')\n\n_bang()\n")
        with caplog.at_level(logging.WARNING, logger="apcore_toolkit"):
            scanner.scan(tmp_path)

        failure_records = [r for r in caplog.records if "failed to scan" in r.getMessage()]
        assert len(failure_records) == 1
        record = failure_records[0]
        # exc_info must be populated so operators see *where* the failure
        # originated, not just str(exc).
        assert record.exc_info is not None
        assert record.exc_info[0] is RuntimeError
        assert "kaboom at import" in str(record.exc_info[1])


class TestConventionScannerSysPathIsolation:
    """Verify scan() does not leak sys.path mutations across calls."""

    def test_sys_path_restored_after_successful_scan(self, scanner, tmp_path):
        (tmp_path / "ok.py").write_text('def fn(x: str) -> str:\n    """OK."""\n    return x\n')
        before = list(sys.path)
        scanner.scan(tmp_path)
        assert sys.path == before

    def test_sys_path_restored_when_scanned_module_mutates_it(self, scanner, tmp_path):
        """A scanned module that itself appends to sys.path must not leak the entry."""
        (tmp_path / "greedy.py").write_text(
            "import sys\n"
            "sys.path.append('/tmp/apcore-toolkit-should-not-leak')\n"
            'def fn(x: str) -> str:\n    """Greedy."""\n    return x\n'
        )
        before = list(sys.path)
        scanner.scan(tmp_path)
        assert sys.path == before
        assert "/tmp/apcore-toolkit-should-not-leak" not in sys.path

    def test_sys_path_restored_when_scanned_module_raises(self, scanner, tmp_path):
        """A scanned module raising at import time must still not leak sys.path."""
        (tmp_path / "broken.py").write_text("raise RuntimeError('boom at import')\n")
        (tmp_path / "ok.py").write_text('def fn(x: str) -> str:\n    """OK."""\n    return x\n')
        before = list(sys.path)
        modules = scanner.scan(tmp_path)
        # ok.py still scanned successfully despite broken.py import failure.
        assert len(modules) == 1
        assert modules[0].module_id == "ok.fn"
        assert sys.path == before


class TestConventionScannerFilterDelegation:
    """Verify that ConventionScanner delegates include/exclude to BaseScanner.filter_modules."""

    @staticmethod
    def _extract_include_exclude(mock_filter):
        """Return (include, exclude) from the mocked call's positional args.

        filter_modules is a @staticmethod with signature (modules, include, exclude).
        """
        args, _ = mock_filter.call_args
        assert len(args) == 3, f"expected 3 positional args, got {len(args)}: {args!r}"
        _, include, exclude = args
        return include, exclude

    def test_include_delegates_to_base_scanner_filter_modules(self, scanner, tmp_path):
        """ConventionScanner.scan() must call BaseScanner.filter_modules for include filtering."""
        (tmp_path / "a.py").write_text('def func1(x: str) -> str:\n    """A."""\n    return x\n')
        (tmp_path / "b.py").write_text('def func2(x: str) -> str:\n    """B."""\n    return x\n')

        with patch.object(BaseScanner, "filter_modules", wraps=BaseScanner.filter_modules) as mock_filter:
            modules = scanner.scan(tmp_path, include=r"^a\.")

        mock_filter.assert_called_once()
        include, exclude = self._extract_include_exclude(mock_filter)
        assert include == r"^a\."
        assert exclude is None
        assert len(modules) == 1
        assert modules[0].module_id == "a.func1"

    def test_exclude_delegates_to_base_scanner_filter_modules(self, scanner, tmp_path):
        """ConventionScanner.scan() must call BaseScanner.filter_modules for exclude filtering."""
        (tmp_path / "a.py").write_text('def func1(x: str) -> str:\n    """A."""\n    return x\n')
        (tmp_path / "b.py").write_text('def func2(x: str) -> str:\n    """B."""\n    return x\n')

        with patch.object(BaseScanner, "filter_modules", wraps=BaseScanner.filter_modules) as mock_filter:
            modules = scanner.scan(tmp_path, exclude=r"^b\.")

        mock_filter.assert_called_once()
        include, exclude = self._extract_include_exclude(mock_filter)
        assert include is None
        assert exclude == r"^b\."
        assert len(modules) == 1
        assert modules[0].module_id == "a.func1"

    def test_no_filter_delegates_to_base_scanner_filter_modules(self, scanner, tmp_path):
        """ConventionScanner.scan() must call BaseScanner.filter_modules even with no filters."""
        (tmp_path / "a.py").write_text('def func1(x: str) -> str:\n    """A."""\n    return x\n')

        with patch.object(BaseScanner, "filter_modules", wraps=BaseScanner.filter_modules) as mock_filter:
            modules = scanner.scan(tmp_path)

        mock_filter.assert_called_once()
        include, exclude = self._extract_include_exclude(mock_filter)
        assert include is None
        assert exclude is None
        assert len(modules) == 1

    def test_delegates_with_both_include_and_exclude(self, scanner, tmp_path):
        """Regression: include and exclude must reach filter_modules in the correct slots."""
        (tmp_path / "a.py").write_text('def func1(x: str) -> str:\n    """A."""\n    return x\n')
        (tmp_path / "b.py").write_text('def func2(x: str) -> str:\n    """B."""\n    return x\n')

        with patch.object(BaseScanner, "filter_modules", wraps=BaseScanner.filter_modules) as mock_filter:
            modules = scanner.scan(tmp_path, include=r"\.", exclude=r"^b\.")

        mock_filter.assert_called_once()
        include, exclude = self._extract_include_exclude(mock_filter)
        assert include == r"\."
        assert exclude == r"^b\."
        assert len(modules) == 1
        assert modules[0].module_id == "a.func1"


class TestTypeToSchema:
    """Regression tests for _type_to_schema: Optional, Union, unannotated params."""

    def test_optional_int_param_schema(self, scanner, tmp_path):
        """typing.Optional[int] must not silently produce {"type":"string"}."""
        (tmp_path / "f.py").write_text(
            "from typing import Optional\n" "def fn(x: Optional[int]) -> dict:\n" '    """F."""\n' "    return {}\n"
        )
        modules = scanner.scan(tmp_path)
        schema = modules[0].input_schema["properties"]["x"]
        assert schema.get("type") == "integer"

    def test_pep604_int_or_none_param_schema(self, scanner, tmp_path):
        """PEP 604 int | None annotation must not silently produce {"type":"string"}."""
        (tmp_path / "f.py").write_text("def fn(x: int | None) -> dict:\n" '    """F."""\n' "    return {}\n")
        modules = scanner.scan(tmp_path)
        schema = modules[0].input_schema["properties"]["x"]
        assert schema.get("type") == "integer"

    def test_unannotated_param_not_typed_as_string(self, scanner, tmp_path):
        """A parameter with no annotation must not become {"type":"string"}."""
        (tmp_path / "f.py").write_text("def fn(x) -> dict:\n" '    """F."""\n' "    return {}\n")
        modules = scanner.scan(tmp_path)
        schema = modules[0].input_schema["properties"]["x"]
        assert schema.get("type") != "string"

    def test_unknown_annotated_type_returns_empty_schema(self, scanner, tmp_path):
        """Unknown annotated types (datetime, Enum, etc.) must produce {} not {"type":"string"}."""
        (tmp_path / "f.py").write_text(
            "from datetime import datetime\n" "def fn(x: datetime) -> dict:\n" '    """F."""\n' "    return {}\n"
        )
        modules = scanner.scan(tmp_path)
        schema = modules[0].input_schema["properties"]["x"]
        assert schema == {}, f"Expected empty schema for unknown type, got {schema!r}"


class TestTAGSShapeGuard:
    """TAGS module-level constant: non-list value must not silently corrupt tags."""

    def test_string_tags_constant_does_not_produce_character_list(self, scanner, tmp_path):
        """TAGS = "deploy" must produce [] or ["deploy"], never list("deploy")."""
        (tmp_path / "deploy.py").write_text(
            'TAGS = "deploy"\n\n' 'def deploy(env: str) -> dict:\n    """Deploy."""\n    return {}\n'
        )
        modules = scanner.scan(tmp_path)
        assert modules[0].tags != list("deploy"), "Character list from string TAGS detected"

    def test_integer_tags_constant_produces_empty_list(self, scanner, tmp_path):
        """TAGS = 42 (invalid type) must fall back to empty list."""
        (tmp_path / "deploy.py").write_text(
            "TAGS = 42\n\n" 'def deploy(env: str) -> dict:\n    """Deploy."""\n    return {}\n'
        )
        modules = scanner.scan(tmp_path)
        assert modules[0].tags == []


class TestFilterModulesErrorHandling:
    """Bad regex patterns in include/exclude must surface clearly."""

    def test_invalid_include_regex_raises_value_error(self, scanner, tmp_path):
        """A malformed include regex must surface as ValueError, not re.error."""
        (tmp_path / "a.py").write_text('def fn(x: str) -> str:\n    """A."""\n    return x\n')
        with pytest.raises(ValueError, match="invalid include/exclude pattern"):
            scanner.scan(tmp_path, include="[invalid")

    def test_invalid_exclude_regex_raises_value_error(self, scanner, tmp_path):
        """A malformed exclude regex must surface as ValueError, not re.error."""
        (tmp_path / "a.py").write_text('def fn(x: str) -> str:\n    """A."""\n    return x\n')
        with pytest.raises(ValueError, match="invalid include/exclude pattern"):
            scanner.scan(tmp_path, exclude="[invalid")
