"""Tests for apcore_toolkit.output.registry_writer — RegistryWriter."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from apcore_toolkit.output.registry_writer import RegistryWriter
from apcore_toolkit.output.types import WriteResult
from apcore_toolkit.types import ScannedModule


@pytest.fixture
def writer() -> RegistryWriter:
    return RegistryWriter()


@pytest.fixture
def mock_registry() -> MagicMock:
    return MagicMock()


class TestRegistryWriter:
    def test_write_registers_modules(
        self, writer: RegistryWriter, mock_registry: MagicMock, sample_module: ScannedModule
    ) -> None:
        with patch.object(writer, "_to_function_module", return_value=MagicMock()) as mock_to_fm:
            result = writer.write([sample_module], mock_registry)

        assert len(result) == 1
        assert isinstance(result[0], WriteResult)
        assert result[0].module_id == "users.get_user"
        mock_to_fm.assert_called_once_with(sample_module, allowed_prefixes=None)
        mock_registry.register.assert_called_once()

    def test_write_dry_run(
        self, writer: RegistryWriter, mock_registry: MagicMock, sample_module: ScannedModule
    ) -> None:
        result = writer.write([sample_module], mock_registry, dry_run=True)

        assert len(result) == 1
        assert result[0].module_id == "users.get_user"
        mock_registry.register.assert_not_called()

    def test_write_empty_list(self, writer: RegistryWriter, mock_registry: MagicMock) -> None:
        result = writer.write([], mock_registry)
        assert result == []

    def test_write_multiple_modules(
        self,
        writer: RegistryWriter,
        mock_registry: MagicMock,
        sample_module: ScannedModule,
        annotated_module: ScannedModule,
    ) -> None:
        with patch.object(writer, "_to_function_module", return_value=MagicMock()):
            result = writer.write([sample_module, annotated_module], mock_registry)

        assert [r.module_id for r in result] == ["users.get_user", "tasks.create_task"]
        assert mock_registry.register.call_count == 2


class TestRegistryWriterVerification:
    def test_verify_success(
        self, writer: RegistryWriter, mock_registry: MagicMock, sample_module: ScannedModule
    ) -> None:
        mock_registry.get.return_value = MagicMock()
        with patch.object(writer, "_to_function_module", return_value=MagicMock()):
            result = writer.write([sample_module], mock_registry, verify=True)

        assert len(result) == 1
        assert result[0].verified is True
        mock_registry.get.assert_called_once_with("users.get_user")

    def test_verify_module_not_found(
        self, writer: RegistryWriter, mock_registry: MagicMock, sample_module: ScannedModule
    ) -> None:
        mock_registry.get.return_value = None
        with patch.object(writer, "_to_function_module", return_value=MagicMock()):
            result = writer.write([sample_module], mock_registry, verify=True)

        assert result[0].verified is False
        assert "not found in registry" in result[0].verification_error

    def test_verify_registry_error(
        self, writer: RegistryWriter, mock_registry: MagicMock, sample_module: ScannedModule
    ) -> None:
        mock_registry.get.side_effect = RuntimeError("Registry corrupted")
        with patch.object(writer, "_to_function_module", return_value=MagicMock()):
            result = writer.write([sample_module], mock_registry, verify=True)

        assert result[0].verified is False
        assert "Registry lookup failed" in result[0].verification_error

    def test_verify_not_run_in_dry_run(
        self, writer: RegistryWriter, mock_registry: MagicMock, sample_module: ScannedModule
    ) -> None:
        result = writer.write([sample_module], mock_registry, dry_run=True, verify=True)
        assert result[0].verified is True
        mock_registry.get.assert_not_called()


class TestGetWriterRegistry:
    def test_get_writer_registry(self) -> None:
        from apcore_toolkit.output import get_writer

        writer = get_writer("registry")
        assert isinstance(writer, RegistryWriter)


class TestRegistryWriterBatchResilience:
    """A registration error on one module must not abort subsequent modules."""

    def test_error_on_first_continues_to_second(self, writer: RegistryWriter, mock_registry: MagicMock) -> None:
        mod_a = ScannedModule(
            module_id="a.func", description="", input_schema={}, output_schema={}, tags=[], target="m:f"
        )
        mod_b = ScannedModule(
            module_id="b.func", description="", input_schema={}, output_schema={}, tags=[], target="m:f"
        )
        with patch.object(writer, "_to_function_module") as mock_to_fm:
            mock_to_fm.side_effect = [RuntimeError("collision"), MagicMock()]
            results = writer.write([mod_a, mod_b], mock_registry)
        assert len(results) == 2
        assert results[0].verified is False
        assert "RuntimeError" in results[0].verification_error
        assert results[1].verified is True

    def test_registry_register_error_recorded_not_raised(
        self, writer: RegistryWriter, mock_registry: MagicMock, sample_module: ScannedModule
    ) -> None:
        mock_registry.register.side_effect = RuntimeError("duplicate")
        with patch.object(writer, "_to_function_module", return_value=MagicMock()):
            results = writer.write([sample_module], mock_registry)
        assert results[0].verified is False
        assert "duplicate" in results[0].verification_error
