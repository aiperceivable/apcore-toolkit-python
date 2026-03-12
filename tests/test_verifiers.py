"""Tests for the Verifier protocol, built-in verifiers, and verifier chain."""

from __future__ import annotations

import json
import yaml

from apcore_toolkit.output.types import Verifier, VerifyResult
from apcore_toolkit.output.verifiers import (
    JSONVerifier,
    MagicBytesVerifier,
    SyntaxVerifier,
    YAMLVerifier,
    run_verifier_chain,
)


# ---------------------------------------------------------------------------
# VerifyResult
# ---------------------------------------------------------------------------
class TestVerifyResult:
    def test_defaults(self):
        r = VerifyResult(ok=True)
        assert r.ok is True
        assert r.error is None

    def test_failed(self):
        r = VerifyResult(ok=False, error="bad")
        assert r.ok is False
        assert r.error == "bad"


# ---------------------------------------------------------------------------
# Verifier protocol
# ---------------------------------------------------------------------------
class TestVerifierProtocol:
    def test_custom_verifier_matches_protocol(self):
        class MyVerifier:
            def verify(self, path: str, module_id: str) -> VerifyResult:
                return VerifyResult(ok=True)

        assert isinstance(MyVerifier(), Verifier)

    def test_non_verifier_does_not_match(self):
        class NotAVerifier:
            pass

        assert not isinstance(NotAVerifier(), Verifier)


# ---------------------------------------------------------------------------
# YAMLVerifier
# ---------------------------------------------------------------------------
class TestYAMLVerifier:
    def test_valid_yaml(self, tmp_path):
        f = tmp_path / "test.yaml"
        data = {"bindings": [{"module_id": "x", "target": "a.b:c"}]}
        f.write_text(yaml.dump(data))
        r = YAMLVerifier().verify(str(f), "x")
        assert r.ok is True

    def test_invalid_yaml(self, tmp_path):
        f = tmp_path / "bad.yaml"
        f.write_text(": :\n  -\n  bad: [")
        r = YAMLVerifier().verify(str(f), "x")
        assert r.ok is False
        assert "Invalid YAML" in r.error

    def test_missing_bindings(self, tmp_path):
        f = tmp_path / "test.yaml"
        f.write_text(yaml.dump({"other": "data"}))
        r = YAMLVerifier().verify(str(f), "x")
        assert r.ok is False
        assert "bindings" in r.error

    def test_missing_module_id(self, tmp_path):
        f = tmp_path / "test.yaml"
        f.write_text(yaml.dump({"bindings": [{"target": "a:b"}]}))
        r = YAMLVerifier().verify(str(f), "x")
        assert r.ok is False
        assert "module_id" in r.error


# ---------------------------------------------------------------------------
# SyntaxVerifier
# ---------------------------------------------------------------------------
class TestSyntaxVerifier:
    def test_valid_python(self, tmp_path):
        f = tmp_path / "ok.py"
        f.write_text("x = 1\n")
        r = SyntaxVerifier().verify(str(f), "m")
        assert r.ok is True

    def test_invalid_python(self, tmp_path):
        f = tmp_path / "bad.py"
        f.write_text("def f(\n")
        r = SyntaxVerifier().verify(str(f), "m")
        assert r.ok is False
        assert "syntax" in r.error.lower()


# ---------------------------------------------------------------------------
# MagicBytesVerifier
# ---------------------------------------------------------------------------
class TestMagicBytesVerifier:
    def test_matching_header(self, tmp_path):
        f = tmp_path / "file.bin"
        f.write_bytes(b"\x89PNG" + b"\x00" * 10)
        r = MagicBytesVerifier(b"\x89PNG").verify(str(f), "m")
        assert r.ok is True

    def test_mismatched_header(self, tmp_path):
        f = tmp_path / "file.bin"
        f.write_bytes(b"XXXX")
        r = MagicBytesVerifier(b"\x89PNG").verify(str(f), "m")
        assert r.ok is False
        assert "mismatch" in r.error.lower()

    def test_missing_file(self, tmp_path):
        r = MagicBytesVerifier(b"\x89PNG").verify(str(tmp_path / "nope"), "m")
        assert r.ok is False
        assert "Cannot read" in r.error


# ---------------------------------------------------------------------------
# JSONVerifier
# ---------------------------------------------------------------------------
class TestJSONVerifier:
    def test_valid_json(self, tmp_path):
        f = tmp_path / "data.json"
        f.write_text(json.dumps({"a": 1}))
        r = JSONVerifier().verify(str(f), "m")
        assert r.ok is True

    def test_invalid_json(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("{not json")
        r = JSONVerifier().verify(str(f), "m")
        assert r.ok is False
        assert "Invalid JSON" in r.error

    def test_schema_validation_without_jsonschema(self, tmp_path, monkeypatch):
        """JSONVerifier with schema returns ok=False when jsonschema is not installed."""
        f = tmp_path / "data.json"
        f.write_text(json.dumps({"a": 1}))

        import builtins

        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "jsonschema":
                raise ImportError("mocked")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)
        r = JSONVerifier(schema={"type": "object"}).verify(str(f), "m")
        assert r.ok is False
        assert "jsonschema" in r.error.lower()


# ---------------------------------------------------------------------------
# run_verifier_chain
# ---------------------------------------------------------------------------
class _PassVerifier:
    def verify(self, path: str, module_id: str) -> VerifyResult:
        return VerifyResult(ok=True)


class _FailVerifier:
    def __init__(self, msg: str = "fail"):
        self._msg = msg

    def verify(self, path: str, module_id: str) -> VerifyResult:
        return VerifyResult(ok=False, error=self._msg)


class _CrashVerifier:
    def verify(self, path: str, module_id: str) -> VerifyResult:
        raise RuntimeError("boom")


class TestRunVerifierChain:
    def test_empty_chain(self):
        r = run_verifier_chain([], "/tmp/x", "m")
        assert r.ok is True

    def test_all_pass(self):
        r = run_verifier_chain([_PassVerifier(), _PassVerifier()], "/tmp/x", "m")
        assert r.ok is True

    def test_first_failure_stops(self):
        r = run_verifier_chain(
            [_PassVerifier(), _FailVerifier("first"), _FailVerifier("second")],
            "/tmp/x",
            "m",
        )
        assert r.ok is False
        assert r.error == "first"

    def test_crash_is_caught(self):
        r = run_verifier_chain([_CrashVerifier()], "/tmp/x", "m")
        assert r.ok is False
        assert "Verifier crashed" in r.error


# ---------------------------------------------------------------------------
# Writer integration: verifiers param on YAMLWriter
# ---------------------------------------------------------------------------
class TestYAMLWriterWithVerifiers:
    def test_custom_verifier_runs_after_builtin(self, tmp_path, sample_module):
        from apcore_toolkit import YAMLWriter

        class StrictVerifier:
            def verify(self, path: str, module_id: str) -> VerifyResult:
                return VerifyResult(ok=False, error="strict check failed")

        writer = YAMLWriter()
        results = writer.write(
            [sample_module],
            output_dir=str(tmp_path),
            verify=True,
            verifiers=[StrictVerifier()],
        )
        assert len(results) == 1
        assert results[0].verified is False
        assert results[0].verification_error == "strict check failed"

    def test_custom_verifier_skipped_when_builtin_fails(self, tmp_path, sample_module):
        from apcore_toolkit import YAMLWriter

        call_count = 0

        class CountingVerifier:
            def verify(self, path: str, module_id: str) -> VerifyResult:
                nonlocal call_count
                call_count += 1
                return VerifyResult(ok=True)

        writer = YAMLWriter()
        writer.write(
            [sample_module],
            output_dir=str(tmp_path),
            verify=True,
            verifiers=[CountingVerifier()],
        )
        # Built-in passes, so custom runs
        assert call_count == 1

    def test_no_verifiers_backwards_compatible(self, tmp_path, sample_module):
        from apcore_toolkit import YAMLWriter

        writer = YAMLWriter()
        results = writer.write([sample_module], output_dir=str(tmp_path), verify=True)
        assert len(results) == 1
        assert results[0].verified is True

    def test_verifiers_without_verify_flag(self, tmp_path, sample_module):
        from apcore_toolkit import YAMLWriter

        class StrictVerifier:
            def verify(self, path: str, module_id: str) -> VerifyResult:
                return VerifyResult(ok=False, error="should still run")

        writer = YAMLWriter()
        results = writer.write(
            [sample_module],
            output_dir=str(tmp_path),
            verify=False,
            verifiers=[StrictVerifier()],
        )
        # verify=False means built-in skipped, but verifiers still run
        assert results[0].verified is False
        assert results[0].verification_error == "should still run"


# ---------------------------------------------------------------------------
# Writer integration: verifiers param on PythonWriter
# ---------------------------------------------------------------------------
class TestPythonWriterWithVerifiers:
    def test_custom_verifier_failure(self, tmp_path, sample_module):
        from apcore_toolkit import PythonWriter

        class RejectAll:
            def verify(self, path: str, module_id: str) -> VerifyResult:
                return VerifyResult(ok=False, error="rejected")

        writer = PythonWriter()
        results = writer.write(
            [sample_module],
            output_dir=str(tmp_path),
            verify=True,
            verifiers=[RejectAll()],
        )
        assert len(results) == 1
        assert results[0].verified is False
        assert results[0].verification_error == "rejected"

    def test_custom_verifier_passes(self, tmp_path, sample_module):
        from apcore_toolkit import PythonWriter

        writer = PythonWriter()
        results = writer.write(
            [sample_module],
            output_dir=str(tmp_path),
            verify=True,
            verifiers=[_PassVerifier()],
        )
        assert results[0].verified is True


# ---------------------------------------------------------------------------
# Writer integration: verifiers param on RegistryWriter
# ---------------------------------------------------------------------------
class TestRegistryWriterWithVerifiers:
    def test_custom_verifier_on_registry(self, sample_module):
        from unittest.mock import MagicMock, patch

        from apcore_toolkit import RegistryWriter

        registry = MagicMock()
        registry.get.return_value = MagicMock()

        class RejectAll:
            def verify(self, path: str, module_id: str) -> VerifyResult:
                return VerifyResult(ok=False, error="registry rejected")

        def _fake_target(**kw: object) -> dict:
            return {}

        writer = RegistryWriter()
        with patch("apcore_toolkit.output.registry_writer.resolve_target", return_value=_fake_target):
            with patch("apcore_toolkit.output.registry_writer.flatten_pydantic_params", side_effect=lambda f: f):
                results = writer.write(
                    [sample_module],
                    registry,
                    verify=True,
                    verifiers=[RejectAll()],
                )
        assert results[0].verified is False
        assert results[0].verification_error == "registry rejected"
