"""Tests for apcore_toolkit.output.python_writer — PythonWriter."""

from __future__ import annotations

from pathlib import Path

import pytest

from apcore_toolkit.output.python_writer import PythonWriter
from apcore_toolkit.output.types import WriteResult
from apcore_toolkit.types import ScannedModule


class TestPythonWriterDryRun:
    def setup_method(self) -> None:
        self.writer = PythonWriter()

    def test_empty_modules(self) -> None:
        result = self.writer.write([], "/tmp/out", dry_run=True)
        assert result == []

    def test_single_module(self, sample_module: ScannedModule) -> None:
        result = self.writer.write([sample_module], "/tmp/out", dry_run=True)
        assert len(result) == 1
        assert isinstance(result[0], WriteResult)
        assert result[0].module_id == "users.get_user"

    def test_function_name_sanitized(self) -> None:
        module = ScannedModule(
            module_id="api.v1.123-bad-name",
            description="test",
            input_schema={"type": "object", "properties": {}},
            output_schema={},
            tags=[],
            target="mod.path:func",
        )
        result = self.writer.write([module], "/tmp/out", dry_run=True)
        assert result[0].module_id == "api.v1.123-bad-name"

    def test_parameters_from_schema(self, sample_module: ScannedModule) -> None:
        code = self.writer._generate_code(sample_module, "2026-01-01")
        assert "user_id: int" in code

    def test_optional_parameters(self) -> None:
        module = ScannedModule(
            module_id="test.func",
            description="test",
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
                "required": ["name"],
            },
            output_schema={},
            tags=[],
            target="mod:func",
        )
        code = self.writer._generate_code(module, "2026-01-01")
        assert "name: str" in code
        assert "age: int | None = None" in code

    def test_annotations_in_decorator(self, annotated_module: ScannedModule) -> None:
        code = self.writer._generate_code(annotated_module, "2026-01-01")
        assert "annotations=" in code

    def test_no_annotations_omitted(self, sample_module: ScannedModule) -> None:
        code = self.writer._generate_code(sample_module, "2026-01-01")
        assert "annotations=" not in code

    def test_multiple_modules(self, sample_module: ScannedModule, annotated_module: ScannedModule) -> None:
        result = self.writer.write([sample_module, annotated_module], "/tmp/out", dry_run=True)
        assert len(result) == 2

    def test_generated_code_has_imports(self, sample_module: ScannedModule) -> None:
        code = self.writer._generate_code(sample_module, "2026-01-01")
        assert "from apcore import module" in code
        assert "@module(" in code
        assert "id='users.get_user'" in code
        assert "from myapp.views import get_user as _original" in code


class TestPythonWriterValidation:
    def setup_method(self) -> None:
        self.writer = PythonWriter()

    def test_invalid_target_no_colon(self) -> None:
        module = ScannedModule(
            module_id="test.func",
            description="test",
            input_schema={"type": "object", "properties": {}},
            output_schema={},
            tags=[],
            target="no_colon_here",
        )
        with pytest.raises(ValueError, match="Invalid target format"):
            self.writer.write([module], "/tmp/out", dry_run=True)

    def test_invalid_module_path(self) -> None:
        module = ScannedModule(
            module_id="test.func",
            description="test",
            input_schema={"type": "object", "properties": {}},
            output_schema={},
            tags=[],
            target="123invalid:func",
        )
        with pytest.raises(ValueError, match="Invalid module path"):
            self.writer.write([module], "/tmp/out", dry_run=True)

    def test_unsafe_module_id_raises_value_error(self) -> None:
        """module_id containing quotes must be rejected to prevent docstring injection."""
        module = ScannedModule(
            module_id='foo"""bar',
            description="test",
            input_schema={"type": "object", "properties": {}},
            output_schema={},
            tags=[],
            target="pkg.mod:func",
        )
        with pytest.raises(ValueError, match="unsafe"):
            self.writer.write([module], "/tmp/out", dry_run=True)

    def test_safe_module_id_passes(self) -> None:
        """module_id with only safe characters must not raise."""
        module = ScannedModule(
            module_id="users.get_user",
            description="test",
            input_schema={"type": "object", "properties": {}},
            output_schema={},
            tags=[],
            target="pkg.mod:func",
        )
        results = self.writer.write([module], "/tmp/out", dry_run=True)
        assert len(results) == 1


class TestPythonWriterFileOutput:
    def setup_method(self) -> None:
        self.writer = PythonWriter()

    def test_writes_files(self, tmp_path: Path, sample_module: ScannedModule) -> None:
        results = self.writer.write([sample_module], str(tmp_path))
        files = list(tmp_path.glob("*.py"))
        assert len(files) == 1
        code = files[0].read_text()
        assert "Auto-generated apcore module" in code
        assert len(results) == 1
        assert results[0].path is not None

    def test_creates_output_dir(self, tmp_path: Path, sample_module: ScannedModule) -> None:
        out_dir = tmp_path / "nested" / "output"
        self.writer.write([sample_module], str(out_dir))
        assert out_dir.exists()
        assert len(list(out_dir.glob("*.py"))) == 1

    def test_overwrite_existing(self, tmp_path: Path, sample_module: ScannedModule) -> None:
        self.writer.write([sample_module], str(tmp_path))
        self.writer.write([sample_module], str(tmp_path))
        files = list(tmp_path.glob("*.py"))
        assert len(files) == 1


class TestPythonWriterVerification:
    def setup_method(self) -> None:
        self.writer = PythonWriter()

    def test_verify_valid_file(self, tmp_path: Path, sample_module: ScannedModule) -> None:
        results = self.writer.write([sample_module], str(tmp_path), verify=True)
        assert len(results) == 1
        assert results[0].verified is True
        assert results[0].verification_error is None

    def test_verify_detects_syntax_error(self, tmp_path: Path, sample_module: ScannedModule) -> None:
        results = self.writer.write([sample_module], str(tmp_path))
        file_path = Path(results[0].path)

        # Corrupt the file with invalid syntax
        file_path.write_text("def broken(:\n    pass\n", encoding="utf-8")

        result = WriteResult(module_id="users.get_user", path=str(file_path))
        verified = PythonWriter._verify(result, file_path)
        assert verified.verified is False
        assert "Invalid Python syntax" in verified.verification_error

    def test_verify_not_run_in_dry_run(self, sample_module: ScannedModule) -> None:
        results = self.writer.write([sample_module], "/tmp/out", dry_run=True, verify=True)
        assert len(results) == 1
        assert results[0].verified is True
