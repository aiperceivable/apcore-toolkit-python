"""Convention Module Scanner — discovers plain functions as apcore modules (§5.14).

.. danger::
    This module uses ``importlib.exec_module`` to load Python source files.
    **Only point it at trusted, reviewed source directories.** Scanning an
    untrusted or user-controlled directory is equivalent to executing arbitrary
    Python code with full process privileges. There is no sandboxing.

    If you need to introspect untrusted Python source, use an AST-only approach
    (e.g., ``ast.parse``) instead.
"""

from __future__ import annotations

import importlib.util
import inspect
import logging
import sys
from pathlib import Path
from typing import Any, get_type_hints

import builtins

from apcore_toolkit._type_mapping import PYTHON_TO_JSON_SCHEMA
from apcore_toolkit.scanner import BaseScanner
from apcore_toolkit.types import ScannedModule

logger = logging.getLogger("apcore_toolkit")

# Type hint → JSON Schema mapping (subset of §5.11.5)
# Derived from the shared PYTHON_TO_JSON_SCHEMA vocabulary.
_BUILTIN_TYPE_TO_SCHEMA: dict[type, dict[str, Any]] = {
    getattr(builtins, name): {"type": json_type} for name, json_type in PYTHON_TO_JSON_SCHEMA.items()
}


class ConventionScanner:
    """Scan a directory of plain Python files for public functions.

    Converts each discovered function into a ScannedModule with
    schema inferred from type annotations and description from docstrings.

    .. warning::
        :meth:`scan` executes every ``.py`` file it discovers via
        ``importlib``'s ``exec_module``. Point it only at **trusted,
        reviewed source directories** — scanning an untrusted directory
        is equivalent to running arbitrary Python code.

    Usage::

        scanner = ConventionScanner()
        modules = scanner.scan("commands/")
    """

    def scan(
        self,
        commands_dir: str | Path,
        *,
        include: str | None = None,
        exclude: str | None = None,
    ) -> list[ScannedModule]:
        """Scan a commands directory for convention modules.

        Args:
            commands_dir: Path to the commands directory.
            include: Regex pattern to include module IDs.
            exclude: Regex pattern to exclude module IDs.

        Returns:
            List of ScannedModule instances.
        """
        commands_path = Path(commands_dir)
        if not commands_path.is_dir():
            logger.warning("ConventionScanner: commands directory not found: %s", commands_path)
            return []

        modules: list[ScannedModule] = []
        for py_file in sorted(commands_path.rglob("*.py")):
            if py_file.name.startswith("_"):
                continue
            try:
                file_modules = self._scan_file(py_file, commands_path)
                modules.extend(file_modules)
            except Exception:
                # exc_info=True preserves the traceback so operators can locate
                # the offending line in user code, not just the exception message.
                logger.warning("ConventionScanner: failed to scan %s", py_file, exc_info=True)

        # Apply include/exclude filters — delegate to BaseScanner.filter_modules
        # (a @staticmethod) to avoid parallel implementations that can diverge
        # on edge cases.
        modules = BaseScanner.filter_modules(modules, include, exclude)

        logger.info("ConventionScanner: discovered %d modules from %s", len(modules), commands_path)
        return modules

    def _scan_file(self, py_file: Path, base_dir: Path) -> list[ScannedModule]:
        """Scan a single Python file for public functions."""
        # Derive prefix from relative path
        rel = py_file.relative_to(base_dir)
        parts = list(rel.with_suffix("").parts)
        file_prefix = ".".join(parts)

        # Load the module
        spec = importlib.util.spec_from_file_location(f"_convention_{file_prefix}", py_file)
        if spec is None or spec.loader is None:
            return []

        mod = importlib.util.module_from_spec(spec)
        # Snapshot sys.path so any mutation during exec_module — either the
        # entry we add or entries the scanned module itself appends — is
        # fully reverted. Append (not insert) the parent directory so real
        # installed packages keep precedence and a shadow module next to
        # the command file cannot hijack imports during scanning.
        parent = str(py_file.parent)
        sys_path_snapshot = sys.path[:]
        if parent not in sys.path:
            sys.path.append(parent)
        try:
            spec.loader.exec_module(mod)
        finally:
            sys.path[:] = sys_path_snapshot

        # Read module-level constants
        module_prefix = getattr(mod, "MODULE_PREFIX", None)
        cli_group = getattr(mod, "CLI_GROUP", None)
        file_tags = getattr(mod, "TAGS", None) or []

        prefix = module_prefix if isinstance(module_prefix, str) else file_prefix

        # Discover public functions
        results: list[ScannedModule] = []
        for name, obj in inspect.getmembers(mod, inspect.isfunction):
            # Skip private functions and imported functions
            if name.startswith("_"):
                continue
            if getattr(obj, "__module__", None) != mod.__name__:
                continue

            module_id = f"{prefix}.{name}"
            description = self._extract_description(obj)
            input_schema = self._build_input_schema(obj)
            output_schema = self._build_output_schema(obj)

            metadata: dict[str, Any] = {}
            if isinstance(cli_group, str):
                metadata.setdefault("display", {}).setdefault("cli", {})["group"] = cli_group

            results.append(
                ScannedModule(
                    module_id=module_id,
                    description=description,
                    input_schema=input_schema,
                    output_schema=output_schema,
                    tags=list(file_tags),
                    target=f"{py_file}:{name}",
                    metadata=metadata,
                )
            )

        return results

    def _extract_description(self, func: Any) -> str:
        """Extract first line of docstring as description."""
        doc = inspect.getdoc(func)
        if not doc:
            return "(no description)"
        return doc.split("\n")[0].strip()

    def _build_input_schema(self, func: Any) -> dict[str, Any]:
        """Build JSON Schema from function parameter type hints."""
        try:
            hints = get_type_hints(func)
        except (NameError, TypeError) as exc:
            # Unresolved forward refs (NameError) and invalid annotations
            # (TypeError) are the documented failure modes. Narrower than
            # `except Exception` so genuinely unexpected errors propagate
            # to scan()'s logger.warning(exc_info=True).
            logger.debug("ConventionScanner: get_type_hints failed for %s: %s", func, exc)
            hints = {}

        sig = inspect.signature(func)
        properties: dict[str, Any] = {}
        required: list[str] = []

        for param_name, param in sig.parameters.items():
            if param_name in ("self", "cls", "ctx", "context"):
                continue

            type_hint = hints.get(param_name)
            prop = self._type_to_schema(type_hint)

            if param.default is inspect.Parameter.empty:
                required.append(param_name)
            else:
                if param.default is not None:
                    prop["default"] = param.default

            properties[param_name] = prop

        schema: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            schema["required"] = required
        return schema

    def _build_output_schema(self, func: Any) -> dict[str, Any]:
        """Build output schema from return type hint."""
        try:
            hints = get_type_hints(func)
        except (NameError, TypeError) as exc:
            logger.debug("ConventionScanner: get_type_hints failed for %s: %s", func, exc)
            return {}

        ret = hints.get("return")
        if ret is None or ret is type(None):
            return {}
        return self._type_to_schema(ret)

    def _type_to_schema(self, type_hint: Any) -> dict[str, Any]:
        """Convert a Python type hint to JSON Schema."""
        if type_hint is None:
            return {"type": "string"}

        # Direct type mapping
        if type_hint in _BUILTIN_TYPE_TO_SCHEMA:
            return dict(_BUILTIN_TYPE_TO_SCHEMA[type_hint])

        # Handle Optional[X] / X | None
        origin = getattr(type_hint, "__origin__", None)
        args = getattr(type_hint, "__args__", None)

        if origin is list and args:
            return {"type": "array", "items": self._type_to_schema(args[0])}
        if origin is dict:
            return {"type": "object"}

        # Fallback
        return {"type": "string"}
