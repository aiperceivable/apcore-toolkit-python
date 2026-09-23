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
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apcore import ModuleAnnotations, ModuleExample

from apcore_toolkit.types import ScannedModule

logger = logging.getLogger("apcore_toolkit")

_SUPPORTED_SPEC_VERSIONS = frozenset({"1.0"})
_STRICT_REQUIRED = ("module_id", "target", "input_schema", "output_schema")
_LOOSE_REQUIRED = ("module_id", "target")
_MAX_BINDING_FILE_SIZE = 16 * 1024 * 1024  # 16 MiB
_MAX_BINDING_FILES_PER_DIR = 10_000

#: Canonical default for ``bindings.pattern`` (apcore 0.30 ``schemas/defaults.schema.json``).
_DEFAULT_BINDING_PATTERN = "*.binding.yaml"

# Rejected-pattern reasons. The shared conformance corpus
# Keys whose presence in a metadata dict is unsafe for cross-runtime
# round-trip — they correspond to JS prototype-pollution sinks. Filter
# them at parse time so a malicious or malformed binding YAML cannot
# carry an attacker-controlled "__proto__" / "constructor" / "prototype"
# entry into downstream consumers (matches the TypeScript loader's
# PROTO_DENY guard in src/binding-parser.ts).
_FORBIDDEN_METADATA_KEYS: frozenset[str] = frozenset({"__proto__", "constructor", "prototype"})


def _match_name(pattern: str, name: str) -> bool:
    """Match a file *name* against ``pattern``; the shared toolkit matcher.

    Two metacharacters are recognised — ``*`` (zero or more characters,
    including ``.``) and ``?`` (exactly one character). Everything else is a
    literal, brackets and braces included: character classes and brace
    expansion are deliberately unsupported because that is precisely where
    language glob implementations diverge. Matching is case-sensitive on
    every platform, applies no Unicode normalization, compares Python ``str``
    code points, and does not exclude leading-dot names.

    This is the normative two-pointer algorithm with single-star backtracking
    from docs/features/binding-loader.md#normative-matching-algorithm --
    O(len(pattern) x len(name)) worst case, so a pattern arriving from
    configuration cannot trigger the exponential blowup a naive recursive
    "try every split point" matcher suffers on inputs like ``*a*a*a*a*b``.

    Note:
        ``fnmatch`` and ``Path.glob`` are intentionally *not* used: both
        implement POSIX character classes, which would make
        ``[ab].binding.yaml`` match ``a.binding.yaml`` and diverge from the
        Rust and TypeScript SDKs.
    """
    p = 0
    n = 0
    star = -1
    mark = 0
    p_len = len(pattern)
    n_len = len(name)

    while n < n_len:
        if p < p_len and pattern[p] == "?":
            p += 1
            n += 1
        elif p < p_len and pattern[p] == "*":
            star = p
            mark = n
            p += 1  # consume zero characters for now
        elif p < p_len and pattern[p] == name[n]:
            p += 1
            n += 1
        elif star >= 0:
            p = star + 1
            mark += 1
            n = mark  # let the last '*' eat one more character
        else:
            return False

    while p < p_len and pattern[p] == "*":
        p += 1  # trailing stars may match nothing

    return p == p_len


def _iter_candidate_files(root: Path, *, recursive: bool) -> Iterator[Path]:
    """Yield every regular file under ``root``, without applying any name filter.

    ``recursive`` governs *which directories are traversed* — the immediate
    directory only, or the whole tree — and nothing else; name filtering is
    ``_match_name``'s job. Only regular files are yielded, so a directory
    named to look like a match (``api-b.cli.yaml/``) is never selectable and
    can never reach the reader as an ``EISDIR``.

    **The file-type check follows symlinks: it tests the target, not the
    link.** A symlink whose target is a regular file *is* yielded — dropping
    it would silently lose binding files, a data-loss-shaped regression with
    no error. A broken symlink is skipped like any other non-file. A symlink
    whose target is a *directory* is neither yielded nor descended into
    (``os.walk`` defaults to ``followlinks=False``, and such an entry is
    classified as a directory, so it never reaches ``file_names``); that is
    where cycles and tree-escape live.

    Per-entry I/O errors are best-effort: unreadable subdirectories are
    skipped rather than aborting the walk (``os.walk`` swallows them by
    default). Errors on ``root`` itself still surface to the caller.
    """
    if recursive:
        for dir_path, _dir_names, file_names in os.walk(root):
            base = Path(dir_path)
            for file_name in file_names:
                candidate = base / file_name
                # ``is_file`` follows symlinks and is False for a broken one.
                if candidate.is_file():
                    yield candidate
    else:
        for entry in root.iterdir():
            if entry.is_file():
                yield entry


def _select_files(root: Path, *, recursive: bool, pattern: str) -> list[Path]:
    """Return the files ``load`` would read from ``root``, in read order.

    Factored out of :meth:`BindingLoader.load` so the selection contract can
    be asserted directly — the shared conformance corpus checks *which paths*
    are selected, which a symlink alias makes unrecoverable from the parsed
    ``module_id`` values alone.

    ``key=str`` is load-bearing: ``sorted()`` over ``Path`` objects compares
    ``_str_normcase``, which case-folds on Windows, so ``["M.binding.yaml",
    "a.binding.yaml"]`` would come back reversed there. Rust's ``PathBuf``
    ordering and JavaScript's default string sort are already code-point
    order, so the explicit key is what keeps Python answering identically on
    every platform.
    """
    return sorted(
        (f for f in _iter_candidate_files(root, recursive=recursive) if _match_name(pattern, f.name)),
        key=str,
    )


def _safe_metadata(raw: Any) -> dict[str, Any]:
    """Return a deep-copied metadata dict with forbidden keys filtered out.

    A WARNING is logged once per forbidden key encountered so loading is
    observable without aborting parse for inputs that are otherwise valid.
    """
    if not isinstance(raw, dict):
        return {}
    cleaned: dict[str, Any] = {}
    for key, value in raw.items():
        if key in _FORBIDDEN_METADATA_KEYS:
            logger.warning(
                "BindingLoader: dropping forbidden metadata key %r (prototype-pollution guard)",
                key,
            )
            continue
        cleaned[key] = copy.deepcopy(value)
    return cleaned


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
        pattern: str = _DEFAULT_BINDING_PATTERN,
    ) -> list[ScannedModule]:
        """Load one file, or every matching file in a directory.

        Args:
            path: File or directory path.
            strict: Enforce presence of input_schema/output_schema in every
                binding entry.
            recursive: When ``path`` is a directory, also descend into
                subdirectories. Default ``False`` preserves the flat-layout
                contract. Governs traversal depth only. Ignored when ``path``
                is a file.
            pattern: Matched against each candidate's **file name** — never a
                directory component, never the full path — at whatever depth
                traversal reached. Supports ``*`` and ``?`` only; see
                :func:`_match_name`. The caller resolves the value (e.g. from
                ``bindings.pattern``); the loader merely matches it. Ignored
                when ``path`` is a file, exactly like ``recursive``.

        Raises:
            BindingLoadError: if ``pattern`` is empty or contains a path
                separator (checked before any filesystem access), the path is
                missing, YAML is malformed, or any entry fails validation.

        Note:
            Directory loads are all-or-nothing: the first malformed file
            aborts the load and any previously parsed files are discarded.
            Callers that need best-effort aggregation should iterate the
            files themselves and invoke ``load`` per file.

            ``pattern`` narrows *which* files are read; it changes nothing
            about how they are read. Ordering, the all-or-nothing contract
            and the safety caps all apply to the matched set unchanged.
            Matched files are sorted by the path string's code points,
            case-sensitively, on every platform.
        """
        # No pattern validation: every string is a valid pattern and the
        # loader never raises on one for syntactic reasons, matching apcore's
        # Algorithm A25 requirement 2 (PROTOCOL_SPEC §9.2.3) and §5.12.6
        # clause 6. `/` and `\\` are literals, so a pattern carrying one
        # simply matches no filename. See docs/features/binding-loader.md.
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover
            raise BindingLoadError("PyYAML is required to load binding files") from exc

        p = Path(path)
        files: list[Path]
        if p.is_file():
            files = [p]
        elif p.is_dir():
            try:
                files = _select_files(p, recursive=recursive, pattern=pattern)
            except OSError as exc:
                # Per-entry errors during a recursive walk are best-effort
                # (os.walk swallows them); an error listing the *root* itself
                # surfaces as BindingLoadError rather than a bare OSError.
                raise BindingLoadError(f"failed to list directory: {exc}", file_path=str(p)) from exc
            if len(files) > _MAX_BINDING_FILES_PER_DIR:
                raise BindingLoadError(
                    f"too many files in directory: {len(files)} exceeds limit of {_MAX_BINDING_FILES_PER_DIR}",
                    file_path=str(p),
                )
        else:
            raise BindingLoadError(f"path does not exist: {p}", file_path=str(p))

        modules: list[ScannedModule] = []
        for f in files:
            if f.stat().st_size > _MAX_BINDING_FILE_SIZE:
                raise BindingLoadError(
                    f"file too large: {f.stat().st_size} bytes exceeds limit of {_MAX_BINDING_FILE_SIZE} bytes",
                    file_path=str(f),
                )
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

        # Loose-mode wrong-type policy (apcore-toolkit/docs/features/binding-loader.md
        # § Loose-mode wrong-type policy): for non-required fields
        # (input_schema / output_schema / tags), strict mode raises
        # BindingLoadError, loose mode warns and coerces to the empty default —
        # mirroring the Rust and TypeScript loaders.
        raw_input_schema = entry.get("input_schema")
        if raw_input_schema is not None and not isinstance(raw_input_schema, dict):
            if strict:
                raise BindingLoadError(
                    f"'input_schema' must be a mapping, got {type(raw_input_schema).__name__!r}",
                    file_path=file_path,
                    module_id=entry.get("module_id"),
                )
            logger.warning(
                "binding entry %r: 'input_schema' must be a mapping, got %r — coercing to {}",
                entry.get("module_id"),
                type(raw_input_schema).__name__,
            )
            raw_input_schema = None
        raw_output_schema = entry.get("output_schema")
        if raw_output_schema is not None and not isinstance(raw_output_schema, dict):
            if strict:
                raise BindingLoadError(
                    f"'output_schema' must be a mapping, got {type(raw_output_schema).__name__!r}",
                    file_path=file_path,
                    module_id=entry.get("module_id"),
                )
            logger.warning(
                "binding entry %r: 'output_schema' must be a mapping, got %r — coercing to {}",
                entry.get("module_id"),
                type(raw_output_schema).__name__,
            )
            raw_output_schema = None
        raw_tags = entry.get("tags")
        if raw_tags is not None and not isinstance(raw_tags, list):
            if strict:
                raise BindingLoadError(
                    f"'tags' must be a list, got {type(raw_tags).__name__!r}",
                    file_path=file_path,
                    module_id=entry.get("module_id"),
                )
            logger.warning(
                "binding entry %r: 'tags' must be a list, got %r — coercing to []",
                entry.get("module_id"),
                type(raw_tags).__name__,
            )
            raw_tags = None

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
            metadata=_safe_metadata(entry.get("metadata") or {}),
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
