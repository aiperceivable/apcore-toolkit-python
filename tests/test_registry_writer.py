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
        assert result[0].verification_error is not None
        assert "not found in registry" in result[0].verification_error

    def test_verify_registry_error(
        self, writer: RegistryWriter, mock_registry: MagicMock, sample_module: ScannedModule
    ) -> None:
        mock_registry.get.side_effect = RuntimeError("Registry corrupted")
        with patch.object(writer, "_to_function_module", return_value=MagicMock()):
            result = writer.write([sample_module], mock_registry, verify=True)

        assert result[0].verified is False
        assert result[0].verification_error is not None
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
        assert results[0].verification_error is not None
        assert "RuntimeError" in results[0].verification_error
        assert results[1].verified is True

    def test_registry_register_error_recorded_not_raised(
        self, writer: RegistryWriter, mock_registry: MagicMock, sample_module: ScannedModule
    ) -> None:
        mock_registry.register.side_effect = RuntimeError("duplicate")
        with patch.object(writer, "_to_function_module", return_value=MagicMock()):
            results = writer.write([sample_module], mock_registry)
        assert results[0].verified is False
        assert results[0].verification_error is not None
        assert "duplicate" in results[0].verification_error


class TestRegistryWriterStreaming:
    """RegistryWriter streaming-module detection (apcore 0.22.0 StreamingModule interface)."""

    def test_streaming_target_with_async_stream_creates_streaming_adapter(
        self, writer: RegistryWriter, mock_registry: MagicMock
    ) -> None:
        """Target with async stream() → _StreamingFunctionModule adapter registered."""
        import inspect
        from typing import Any, AsyncIterator
        from apcore_toolkit.output.registry_writer import _StreamingFunctionModule

        async def _fake_execute(name: str = "world") -> dict:
            return {"ok": True}

        class _FakeStreamer:
            execute = staticmethod(_fake_execute)

            async def stream(self, inputs: dict, context: Any) -> AsyncIterator[dict]:
                yield {"chunk": 1}

        fake_instance = _FakeStreamer()

        from apcore import ModuleAnnotations

        mod = ScannedModule(
            module_id="stream.test",
            description="streaming module",
            input_schema={},
            output_schema={},
            tags=[],
            target="fake:target",
            annotations=ModuleAnnotations(streaming=True),
        )

        with patch("apcore_toolkit.output.registry_writer.resolve_target", return_value=fake_instance):
            results = writer.write([mod], mock_registry)

        assert len(results) == 1
        registered_module = mock_registry.register.call_args[0][1]
        assert isinstance(
            registered_module, _StreamingFunctionModule
        ), f"Expected _StreamingFunctionModule, got {type(registered_module)}"
        assert inspect.isasyncgenfunction(registered_module.stream), (
            "stream() must be an async generator so the registry's " "_validate_streaming_signature passes"
        )

    def test_streaming_annotation_no_stream_method_clears_flag_and_warns(
        self, writer: RegistryWriter, mock_registry: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """annotations.streaming=True but no stream() → warning logged, streaming cleared."""
        import logging

        def plain_func(name: str = "world") -> dict:
            return {}

        from apcore import ModuleAnnotations

        mod = ScannedModule(
            module_id="stream.no_impl",
            description="claims streaming but isn't",
            input_schema={},
            output_schema={},
            tags=[],
            target="fake:plain_func",
            annotations=ModuleAnnotations(streaming=True),
        )

        with patch("apcore_toolkit.output.registry_writer.resolve_target", return_value=plain_func):
            with caplog.at_level(logging.WARNING, logger="apcore_toolkit"):
                results = writer.write([mod], mock_registry)

        assert len(results) == 1
        assert results[0].verified is True
        registered_module = mock_registry.register.call_args[0][1]
        streaming_val = getattr(registered_module.annotations, "streaming", None)
        assert streaming_val is False, f"Expected streaming=False after clearing, got {streaming_val!r}"
        assert any(
            "streaming" in r.message.lower() for r in caplog.records
        ), "Expected a warning mentioning 'streaming'"

    def test_non_streaming_module_produces_no_streaming_adapter(
        self, writer: RegistryWriter, mock_registry: MagicMock, sample_module: ScannedModule
    ) -> None:
        """Non-streaming modules follow the existing FunctionModule path."""
        from apcore import FunctionModule
        from apcore_toolkit.output.registry_writer import _StreamingFunctionModule

        with patch.object(writer, "_to_function_module") as mock_to_fm:
            mock_to_fm.return_value = MagicMock(spec=FunctionModule)
            writer.write([sample_module], mock_registry)

        registered = mock_registry.register.call_args[0][1]
        assert not isinstance(registered, _StreamingFunctionModule)


class TestRegistryWriterCustomVerifierAlwaysRuns:
    """Custom verifiers must run even when the built-in registry check fails."""

    def test_custom_verifiers_run_when_builtin_fails(
        self, writer: RegistryWriter, mock_registry: MagicMock, sample_module: ScannedModule
    ) -> None:
        # Built-in check: registry.get returns None → verified=False
        mock_registry.get.return_value = None
        call_count = 0

        class CountingVerifier:
            def verify(self, path: str, module_id: str):
                nonlocal call_count
                call_count += 1
                from apcore_toolkit.output.types import VerifyResult

                return VerifyResult(ok=True)

        with patch.object(writer, "_to_function_module", return_value=MagicMock()):
            writer.write([sample_module], mock_registry, verify=True, verifiers=[CountingVerifier()])

        assert call_count == 1, "Custom verifier must run even when built-in registry check fails"


def _handler(user_id: int) -> dict:
    """A real resolvable callable for round-trip registration tests."""
    return {"user_id": user_id}


class TestAnnotationRoundTrip:
    """Behavioral annotations must survive scan -> register -> get_definition,
    or approval/ACL gating that keys on ``requires_approval`` silently no-ops.

    These use a REAL apcore Registry (not a MagicMock) so the round-trip is
    actually exercised — the earlier writer tests mock the registry and would
    not catch a dropped-annotations regression.
    """

    def _module(self, annotations):
        return ScannedModule(
            module_id="orders.delete_order",
            description="Delete an order",
            input_schema={"type": "object", "properties": {"user_id": {"type": "integer"}}},
            output_schema={"type": "object"},
            tags=["orders"],
            target="unused:patched",
            annotations=annotations,
        )

    def test_base_writer_preserves_annotations_into_real_registry(self) -> None:
        from apcore import ModuleAnnotations, Registry

        registry = Registry()
        mod = self._module(ModuleAnnotations(destructive=True, requires_approval=True))
        with patch(
            "apcore_toolkit.output.registry_writer.resolve_target", return_value=_handler
        ):
            RegistryWriter().write([mod], registry)

        definition = registry.get_definition("orders.delete_order")
        assert definition.annotations is not None, "annotations were dropped during registration"
        assert definition.annotations.destructive is True
        assert definition.annotations.requires_approval is True

    def test_hook_only_subclass_cannot_drop_annotations(self) -> None:
        """A subclass that customizes ONLY the narrow hooks (not
        ``_to_function_module``) still preserves annotations — this is the point
        of centralizing field mapping in ``_build_function_module``."""
        from apcore import ModuleAnnotations, Registry
        from pydantic import create_model

        adapted: list[str] = []

        class HookOnlyWriter(RegistryWriter):
            def _adapt_func(self, func, mod):  # type: ignore[no-untyped-def]
                adapted.append(mod.module_id)
                return func

            def _build_input_schema(self, mod):  # type: ignore[no-untyped-def]
                return create_model("CustomInput")

        registry = Registry()
        mod = self._module(ModuleAnnotations(requires_approval=True))
        with patch(
            "apcore_toolkit.output.registry_writer.resolve_target", return_value=_handler
        ):
            HookOnlyWriter().write([mod], registry)

        # Hooks ran...
        assert adapted == ["orders.delete_order"]
        definition = registry.get_definition("orders.delete_order")
        # ...and annotations still survived (subclass had no way to drop them).
        assert definition.annotations is not None
        assert definition.annotations.requires_approval is True
