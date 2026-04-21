"""BindingLoader — parse ``.binding.yaml`` files back into ``ScannedModule``.

The inverse of :class:`apcore_toolkit.output.yaml_writer.YAMLWriter`. Unlike
apcore's own ``BindingLoader`` (which ``importlib.import_module`` the target
and registers a ``FunctionModule``), this loader is pure data: it parses YAML
into a list of ``ScannedModule`` objects for validation, merging, diffing, or
round-trip workflows. No code is imported.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apcore import ModuleAnnotations, ModuleExample

from apcore_toolkit.types import ScannedModule

logger = logging.getLogger("apcore_toolkit")

_SUPPORTED_SPEC_VERSIONS = frozenset({"1.0"})
_STRICT_REQUIRED = ("module_id", "target", "input_schema", "output_schema")
_LOOSE_REQUIRED = ("module_id", "target")


class BindingLoadError(Exception):
    """Raised when a binding YAML entry cannot be parsed into a ``ScannedModule``.

    Attributes:
        file_path: Path of the binding file (``None`` when loading from pre-parsed data).
        module_id: ID of the offending binding entry (``None`` when structural error).
        missing_fields: Fields required but absent; empty when the failure is not about missing fields.
        reason: Human-readable description of the failure.
    """

    def __init__(
        self,
        reason: str,
        *,
        file_path: str | None = None,
        module_id: str | None = None,
        missing_fields: list[str] | None = None,
    ) -> None:
        self.reason = reason
        self.file_path = file_path
        self.module_id = module_id
        self.missing_fields = list(missing_fields or [])
        parts = [reason]
        if file_path:
            parts.append(f"file={file_path}")
        if module_id:
            parts.append(f"module_id={module_id}")
        if self.missing_fields:
            parts.append(f"missing={self.missing_fields}")
        super().__init__(" | ".join(parts))


@dataclass
class BindingLoader:
    """Loads ``.binding.yaml`` files into ``ScannedModule`` objects.

    Usage::

        loader = BindingLoader()
        modules = loader.load("bindings/")          # directory
        modules = loader.load("foo.binding.yaml")   # single file
        modules = loader.load_data(parsed_dict)      # pre-parsed YAML

    In loose mode (default), only ``module_id`` and ``target`` are required;
    missing optional fields fall back to dataclass defaults (empty schemas,
    empty tags, ``version="1.0.0"``, etc.).

    In strict mode (``strict=True``), ``input_schema`` and ``output_schema``
    are additionally required.
    """

    def load(
        self,
        path: str | Path,
        *,
        strict: bool = False,
        recursive: bool = False,
    ) -> list[ScannedModule]:
        """Load one file or every ``*.binding.yaml`` in a directory.

        Args:
            path: File or directory path.
            strict: Enforce presence of input_schema/output_schema in every
                binding entry.
            recursive: When ``path`` is a directory, also descend into
                subdirectories looking for ``*.binding.yaml``. Default
                ``False`` preserves the flat-layout contract. Ignored when
                ``path`` is a file.

        Raises:
            BindingLoadError: if the path is missing, YAML is malformed, or
                any entry fails validation.

        Note:
            Directory loads are all-or-nothing: the first malformed file
            aborts the load and any previously parsed files are discarded.
            Callers that need best-effort aggregation should iterate the
            files themselves and invoke ``load`` per file.
        """
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover
            raise BindingLoadError("PyYAML is required to load binding files") from exc

        p = Path(path)
        files: list[Path]
        if p.is_file():
            files = [p]
        elif p.is_dir():
            pattern = "**/*.binding.yaml" if recursive else "*.binding.yaml"
            files = sorted(p.glob(pattern))
        else:
            raise BindingLoadError(f"path does not exist: {p}", file_path=str(p))

        modules: list[ScannedModule] = []
        for f in files:
            try:
                raw = yaml.safe_load(f.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
                raise BindingLoadError(f"failed to parse YAML: {exc}", file_path=str(f)) from exc
            if raw is None:
                logger.warning("BindingLoader: %s is empty, skipping", f)
                continue
            modules.extend(self._parse_document(raw, file_path=str(f), strict=strict))
        return modules

    def load_data(self, data: dict[str, Any], *, strict: bool = False) -> list[ScannedModule]:
        """Parse a pre-loaded binding dict (``{"bindings": [...]}``)."""
        return self._parse_document(data, file_path=None, strict=strict)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _parse_document(
        self,
        data: Any,
        *,
        file_path: str | None,
        strict: bool,
    ) -> list[ScannedModule]:
        if not isinstance(data, dict):
            raise BindingLoadError(
                "top-level binding document must be a mapping",
                file_path=file_path,
            )

        self._check_spec_version(data.get("spec_version"), file_path=file_path)

        bindings = data.get("bindings")
        if not isinstance(bindings, list):
            raise BindingLoadError(
                "'bindings' key missing or not a list",
                file_path=file_path,
            )

        modules: list[ScannedModule] = []
        for entry in bindings:
            if not isinstance(entry, dict):
                raise BindingLoadError(
                    "binding entry must be a mapping",
                    file_path=file_path,
                )
            modules.append(self._parse_entry(entry, file_path=file_path, strict=strict))
        return modules

    @staticmethod
    def _check_spec_version(spec_version: Any, *, file_path: str | None) -> None:
        if spec_version is None:
            logger.warning(
                "BindingLoader: %s missing 'spec_version'; defaulting to '1.0'.",
                file_path or "<inline>",
            )
            return
        if spec_version not in _SUPPORTED_SPEC_VERSIONS:
            logger.warning(
                "BindingLoader: %s has spec_version=%r newer than supported %s; proceeding best-effort.",
                file_path or "<inline>",
                spec_version,
                sorted(_SUPPORTED_SPEC_VERSIONS),
            )

    def _parse_entry(
        self,
        entry: dict[str, Any],
        *,
        file_path: str | None,
        strict: bool,
    ) -> ScannedModule:
        required = _STRICT_REQUIRED if strict else _LOOSE_REQUIRED
        # A required field is "missing or invalid" when absent, null, or of
        # the wrong type. Previously only absent/None was rejected, so
        # ``module_id: 42`` or ``target: true`` silently coerced to
        # ``str(42)="42"`` downstream and corrupted the registered module.
        # This now matches the Rust loader's strict behaviour.
        missing = [f for f in required if self._required_field_invalid(f, entry)]
        if missing:
            raise BindingLoadError(
                "missing or invalid required fields",
                file_path=file_path,
                module_id=entry.get("module_id") if isinstance(entry.get("module_id"), str) else None,
                missing_fields=missing,
            )

        raw_input_schema = entry.get("input_schema")
        if raw_input_schema is not None and not isinstance(raw_input_schema, dict):
            raise BindingLoadError(
                f"'input_schema' must be a mapping, got {type(raw_input_schema).__name__!r}",
                file_path=file_path,
                module_id=entry.get("module_id"),
            )
        raw_output_schema = entry.get("output_schema")
        if raw_output_schema is not None and not isinstance(raw_output_schema, dict):
            raise BindingLoadError(
                f"'output_schema' must be a mapping, got {type(raw_output_schema).__name__!r}",
                file_path=file_path,
                module_id=entry.get("module_id"),
            )
        raw_tags = entry.get("tags")
        if raw_tags is not None and not isinstance(raw_tags, list):
            raise BindingLoadError(
                f"'tags' must be a list, got {type(raw_tags).__name__!r}",
                file_path=file_path,
                module_id=entry.get("module_id"),
            )

        # Deep-copy nested containers so later caller mutation of a
        # ScannedModule.input_schema/output_schema/metadata does not leak back
        # into the parsed YAML source graph. Matches the Rust loader
        # (serde_json::Value.clone is deep) and brings Python in line with the
        # defensive-copy contract already applied to display/examples.
        return ScannedModule(
            module_id=str(entry["module_id"]),
            description=entry.get("description") or "",
            input_schema=copy.deepcopy(raw_input_schema) if raw_input_schema else {},
            output_schema=copy.deepcopy(raw_output_schema) if raw_output_schema else {},
            tags=list(raw_tags) if raw_tags else [],
            target=str(entry["target"]),
            version=str(entry.get("version") or "1.0.0"),
            annotations=self._parse_annotations(entry.get("annotations"), module_id=entry["module_id"]),
            documentation=entry.get("documentation"),
            suggested_alias=entry.get("suggested_alias"),
            examples=self._parse_examples(entry.get("examples"), module_id=entry["module_id"]),
            metadata=copy.deepcopy(entry.get("metadata") or {}),
            display=self._parse_display(entry.get("display"), module_id=entry["module_id"]),
            warnings=list(entry.get("warnings") or []),
        )

    @staticmethod
    def _required_field_invalid(field: str, entry: dict[str, Any]) -> bool:
        """Return True if ``entry[field]`` is absent, null, or the wrong type.

        Schema fields (``input_schema``, ``output_schema``) must be mappings.
        All other required fields (``module_id``, ``target``) must be
        non-empty strings. This rejects YAML like ``module_id: 42`` or
        ``target: true`` that previously slipped through and got coerced
        to ``"42"`` / ``"True"`` downstream.
        """
        if field not in entry:
            return True
        value = entry[field]
        if value is None:
            return True
        if field in ("input_schema", "output_schema"):
            return not isinstance(value, dict)
        # module_id, target — must be non-empty string
        return not isinstance(value, str) or len(value) == 0

    @staticmethod
    def _parse_display(data: Any, *, module_id: str) -> dict[str, Any] | None:
        """Parse the optional display overlay field.

        Behaviour mirrors ``_parse_annotations``/``_parse_examples``: silently
        accepts absent or explicit-``None`` values, and emits a WARNING when
        the field is present but has the wrong shape. Returning ``None`` when
        a malformed overlay is dropped ensures callers do not silently persist
        corrupt data on round-trip.
        """
        if data is None:
            return None
        if not isinstance(data, dict):
            logger.warning(
                "BindingLoader: display for module %s is not a dict (%r); ignoring",
                module_id,
                type(data).__name__,
            )
            return None
        return copy.deepcopy(data)

    @staticmethod
    def _parse_annotations(data: Any, *, module_id: str) -> ModuleAnnotations | None:
        if data is None:
            return None
        if not isinstance(data, dict):
            logger.warning(
                "BindingLoader: annotations for module %s is not a dict (%r); treating as None",
                module_id,
                type(data).__name__,
            )
            return None
        try:
            return ModuleAnnotations.from_dict(data)
        except (TypeError, ValueError) as exc:
            logger.warning(
                "BindingLoader: failed to parse annotations for module %s: %s; treating as None",
                module_id,
                exc,
            )
            return None

    @staticmethod
    def _parse_examples(data: Any, *, module_id: str) -> list[ModuleExample]:
        if data is None:
            return []
        if not isinstance(data, list):
            logger.warning(
                "BindingLoader: examples for module %s is not a list; ignoring",
                module_id,
            )
            return []
        result: list[ModuleExample] = []
        for i, ex in enumerate(data):
            if not isinstance(ex, dict):
                logger.warning(
                    "BindingLoader: examples[%d] of module %s is not a dict; ignoring",
                    i,
                    module_id,
                )
                continue
            try:
                result.append(ModuleExample(**ex))
            except TypeError as exc:
                logger.warning(
                    "BindingLoader: examples[%d] of module %s malformed: %s; ignoring",
                    i,
                    module_id,
                    exc,
                )
        return result
