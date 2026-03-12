"""Built-in verifiers for output writers.

Each verifier implements the Verifier protocol and checks a specific
aspect of a written artifact.
"""

from __future__ import annotations

import ast
import json
import logging
from typing import Any

import yaml

from apcore_toolkit.output.types import Verifier, VerifyResult

logger = logging.getLogger("apcore_toolkit")


class YAMLVerifier:
    """Verify that a YAML binding file is parseable and contains required fields."""

    def verify(self, path: str, module_id: str) -> VerifyResult:
        try:
            with open(path, encoding="utf-8") as f:
                parsed = yaml.safe_load(f)
        except yaml.YAMLError as exc:
            return VerifyResult(ok=False, error=f"Invalid YAML: {exc}")

        if not isinstance(parsed, dict):
            return VerifyResult(ok=False, error="YAML root is not a mapping")

        bindings = parsed.get("bindings")
        if not isinstance(bindings, list) or len(bindings) == 0:
            return VerifyResult(ok=False, error="Missing or empty 'bindings' list")

        first = bindings[0]
        for field in ("module_id", "target"):
            if not first.get(field):
                return VerifyResult(ok=False, error=f"Missing required field '{field}' in binding")

        return VerifyResult(ok=True)


class SyntaxVerifier:
    """Verify that a Python source file has valid syntax."""

    def verify(self, path: str, module_id: str) -> VerifyResult:
        try:
            with open(path, encoding="utf-8") as f:
                source = f.read()
            ast.parse(source, filename=path)
        except SyntaxError as exc:
            return VerifyResult(ok=False, error=f"Invalid Python syntax: {exc}")
        return VerifyResult(ok=True)


class RegistryVerifier:
    """Verify that a module is registered and retrievable from a registry.

    Args:
        registry: The apcore Registry instance to check against.
    """

    def __init__(self, registry: Any) -> None:
        self._registry = registry

    def verify(self, path: str, module_id: str) -> VerifyResult:
        try:
            retrieved = self._registry.get(module_id)
            if retrieved is None:
                return VerifyResult(
                    ok=False,
                    error=f"Module '{module_id}' not found in registry after registration",
                )
        except Exception as exc:
            return VerifyResult(ok=False, error=f"Registry lookup failed: {exc}")
        return VerifyResult(ok=True)


class MagicBytesVerifier:
    """Verify that a file starts with expected magic bytes.

    Args:
        expected: The byte sequence the file should start with.
    """

    def __init__(self, expected: bytes) -> None:
        self._expected = expected

    def verify(self, path: str, module_id: str) -> VerifyResult:
        try:
            with open(path, "rb") as f:
                header = f.read(len(self._expected))
        except OSError as exc:
            return VerifyResult(ok=False, error=f"Cannot read file: {exc}")

        if header != self._expected:
            return VerifyResult(
                ok=False,
                error=f"Magic bytes mismatch: expected {self._expected!r}, got {header!r}",
            )
        return VerifyResult(ok=True)


class JSONVerifier:
    """Verify that a file contains valid JSON, with optional schema validation.

    Args:
        schema: Optional JSON Schema dict to validate against. If not provided,
            only checks that the file is valid JSON.
    """

    def __init__(self, schema: dict[str, Any] | None = None) -> None:
        self._schema = schema

    def verify(self, path: str, module_id: str) -> VerifyResult:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as exc:
            return VerifyResult(ok=False, error=f"Invalid JSON: {exc}")

        if self._schema is not None:
            try:
                import jsonschema

                jsonschema.validate(data, self._schema)
            except ImportError:
                return VerifyResult(
                    ok=False,
                    error="jsonschema package required for schema validation but not installed",
                )
            except Exception as exc:
                return VerifyResult(ok=False, error=f"JSON schema validation failed: {exc}")

        return VerifyResult(ok=True)


def run_verifier_chain(
    verifiers: list[Verifier],
    path: str,
    module_id: str,
) -> VerifyResult:
    """Run verifiers in order; stop on first failure.

    Args:
        verifiers: List of Verifier instances.
        path: File path (or empty string for registry-based verification).
        module_id: The module ID being verified.

    Returns:
        VerifyResult from the first failing verifier, or ok=True if all pass.
    """
    for verifier in verifiers:
        try:
            result = verifier.verify(path, module_id)
        except Exception as exc:
            return VerifyResult(ok=False, error=f"Verifier crashed: {exc}")
        if not result.ok:
            return result
    return VerifyResult(ok=True)
