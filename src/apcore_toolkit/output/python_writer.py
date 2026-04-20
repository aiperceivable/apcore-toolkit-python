"""Python @module code generator.

Generates Python files containing @module-decorated wrapper functions
with typed parameters derived from JSON Schema. Extracted from django-apcore's
output/python_writer.py — completely framework-agnostic.
"""

from __future__ import annotations

import ast
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from apcore_toolkit._type_mapping import JSON_SCHEMA_TO_PYTHON
from apcore_toolkit.output.errors import WriteError
from apcore_toolkit.output.types import Verifier, WriteResult
from apcore_toolkit.output.verifiers import run_verifier_chain

if TYPE_CHECKING:
    from apcore_toolkit.types import ScannedModule

logger = logging.getLogger("apcore_toolkit")

# JSON Schema type to Python type mapping — sourced from shared _type_mapping module.
_TYPE_MAP: dict[str, str] = JSON_SCHEMA_TO_PYTHON

_MODULE_PATH_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*)*$")
_MODULE_ID_SAFE_RE = re.compile(r"^[a-zA-Z0-9_.-]+$")


class PythonWriter:
    """Generates Python files with @module-decorated functions."""

    def write(
        self,
        modules: list[ScannedModule],
        output_dir: str,
        dry_run: bool = False,
        verify: bool = False,
        verifiers: list[Verifier] | None = None,
    ) -> list[WriteResult]:
        """Write Python module files for each ScannedModule.

        Args:
            modules: List of ScannedModule instances.
            output_dir: Directory path to write files to.
            dry_run: If True, return results without writing to disk.
            verify: If True, verify written files have valid Python syntax.
            verifiers: Optional list of custom Verifier instances. When provided,
                these run after the built-in check (if verify=True). First failure
                stops the chain.

        Returns:
            List of WriteResult instances.
        """
        if not modules:
            return []

        output_path = Path(output_dir).resolve()
        if not dry_run:
            output_path.mkdir(parents=True, exist_ok=True)

        results: list[WriteResult] = []
        timestamp = datetime.now(timezone.utc).isoformat()

        for module in modules:
            code = self._generate_code(module, timestamp)

            if dry_run:
                results.append(WriteResult(module_id=module.module_id))
                continue

            safe_name = re.sub(r"[^a-zA-Z0-9_]", "_", module.module_id)
            filename = f"{safe_name}.py"
            file_path = (output_path / filename).resolve()

            # Path traversal protection
            if not file_path.is_relative_to(output_path):
                logger.warning("Skipping file outside output directory: %s", file_path)
                continue

            if file_path.exists():
                logger.warning("Overwriting existing file: %s", file_path)

            try:
                file_path.write_text(code, encoding="utf-8")
            except OSError as exc:
                raise WriteError(str(file_path), exc) from exc
            logger.debug("Written: %s", file_path)

            result = WriteResult(module_id=module.module_id, path=str(file_path))
            if verify:
                result = self._verify(result, file_path)
            if result.verified and verifiers:
                chain_result = run_verifier_chain(verifiers, str(file_path), module.module_id)
                if not chain_result.ok:
                    result = WriteResult(
                        module_id=result.module_id,
                        path=result.path,
                        verified=False,
                        verification_error=chain_result.error,
                    )
            results.append(result)

        return results

    def _generate_code(self, module: ScannedModule, timestamp: str) -> str:
        """Generate Python code for a single ScannedModule."""
        if not _MODULE_ID_SAFE_RE.match(module.module_id):
            raise ValueError(
                f"module_id contains characters unsafe for code generation: {module.module_id!r}. "
                "Only [a-zA-Z0-9_.-] are allowed."
            )
        func_name = self._sanitize_identifier(module.module_id.split(".")[-1])

        if ":" not in module.target:
            raise ValueError(f"Invalid target format: {module.target!r}. " f"Expected 'module.path:callable'.")
        target_module, target_func = module.target.rsplit(":", 1)
        self._validate_module_path(target_module)

        # Validate target_func is a valid Python identifier
        target_func = self._sanitize_identifier(target_func)

        # Build function parameters from input_schema
        params = self._schema_to_params(module.input_schema)
        param_str = ", ".join(params) if params else ""

        # Build the call arguments (just the parameter names)
        call_args = self._schema_to_call_args(module.input_schema)
        call_str = ", ".join(call_args) if call_args else ""

        tags_str = repr(module.tags)
        description_repr = repr(module.description)

        decorator_lines = [
            "@module(",
            f"    id={repr(module.module_id)},",
            f"    description={description_repr},",
            f"    tags={tags_str},",
            f"    version={repr(module.version)},",
        ]
        if module.annotations is not None:
            decorator_lines.append(f"    annotations={repr(module.annotations)},")
        decorator_lines.append(")")

        lines = [
            f'"""Auto-generated apcore module: {module.module_id!r}',
            "",
            f"Generated: {timestamp}",
            "Do not edit manually unless you intend to customize behavior.",
            '"""',
            "",
            "from __future__ import annotations",
            "",
            "from apcore import module",
            "",
            "",
            *decorator_lines,
            f"def {func_name}({param_str}):",
            f"    {description_repr}",
            f"    from {target_module} import {target_func} as _original",
            "",
            f"    result = _original({call_str})",
            "    return result",
            "",
        ]

        return "\n".join(lines)

    @staticmethod
    def _sanitize_identifier(name: str) -> str:
        """Ensure a string is a valid Python identifier."""
        sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", name)
        if not sanitized or sanitized[0].isdigit():
            sanitized = f"_{sanitized}"
        return sanitized

    @staticmethod
    def _validate_module_path(path: str) -> None:
        """Validate that *path* is a valid dotted Python import path."""
        if not _MODULE_PATH_RE.match(path):
            raise ValueError(f"Invalid module path: {path!r}. Must be a valid dotted Python import path.")

    def _schema_to_params(self, schema: dict[str, Any]) -> list[str]:
        """Convert a JSON Schema to Python function parameters."""
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        params: list[str] = []

        for name, prop in properties.items():
            safe_name = self._sanitize_identifier(name)
            py_type = _TYPE_MAP.get(prop.get("type", ""), "Any")
            if name in required:
                params.append(f"{safe_name}: {py_type}")
            else:
                params.append(f"{safe_name}: {py_type} | None = None")

        return params

    def _schema_to_call_args(self, schema: dict[str, Any]) -> list[str]:
        """Extract parameter names from a JSON Schema for function call arguments."""
        properties = schema.get("properties", {})
        return [f"{self._sanitize_identifier(name)}={self._sanitize_identifier(name)}" for name in properties]

    @staticmethod
    def _verify(result: WriteResult, file_path: Path) -> WriteResult:
        """Verify that a written Python file has valid syntax."""
        try:
            source = file_path.read_text(encoding="utf-8")
            ast.parse(source, filename=str(file_path))
        except SyntaxError as exc:
            return WriteResult(
                module_id=result.module_id,
                path=result.path,
                verified=False,
                verification_error=f"Invalid Python syntax: {exc}",
            )
        return result
