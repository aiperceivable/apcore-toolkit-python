"""Tests for ConventionScanner (§5.14)."""

import pytest
from apcore_toolkit.convention_scanner import ConventionScanner


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
