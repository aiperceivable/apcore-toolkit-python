"""BaseScanner ABC — abstract interface for all framework scanners.

Provides shared filtering and deduplication utilities. The ``scan()`` method
is intentionally abstract with ``**kwargs`` so framework adapters can accept
their own parameters (e.g., Flask passes ``app``, Django passes nothing).
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import replace
from typing import Any

from apcore import DEFAULT_ANNOTATIONS, ModuleAnnotations, parse_docstring
from apcore_toolkit.types import ScannedModule


class BaseScanner(ABC):
    """Abstract base class for framework scanners.

    Subclasses must implement ``scan()`` and ``get_source_name()``.
    Utility methods ``filter_modules()`` and ``deduplicate_ids()`` are
    provided for common scanner operations.
    """

    @abstractmethod
    def scan(self, **kwargs: Any) -> list[ScannedModule]:
        """Scan endpoints and return module definitions.

        Keyword arguments are framework-specific (e.g., ``app`` for Flask).

        Returns:
            List of ScannedModule instances.
        """
        ...

    @abstractmethod
    def get_source_name(self) -> str:
        """Return human-readable scanner name (e.g., 'django-ninja')."""
        ...

    def extract_docstring(self, func: Any) -> tuple[str | None, str | None, dict[str, str]]:
        """Extract description, documentation, and parameter descriptions from a function.

        Convenience wrapper around apcore's docstring parser for use by
        concrete scanner implementations.

        Returns:
            Tuple of (description, documentation, param_descriptions).
        """
        return parse_docstring(func)

    @staticmethod
    def filter_modules(
        modules: list[ScannedModule],
        include: str | None = None,
        exclude: str | None = None,
    ) -> list[ScannedModule]:
        """Apply include/exclude regex filters to scanned modules.

        Args:
            modules: List of ScannedModule instances to filter.
            include: If set, only modules whose module_id matches are kept.
            exclude: If set, modules whose module_id matches are removed.

        Returns:
            Filtered list of ScannedModule instances.
        """
        result = modules

        if include is not None:
            try:
                pattern = re.compile(include)
            except re.error as exc:
                raise ValueError(f"invalid include/exclude pattern: {include!r}") from exc
            result = [m for m in result if pattern.search(m.module_id)]

        if exclude is not None:
            try:
                pattern = re.compile(exclude)
            except re.error as exc:
                raise ValueError(f"invalid include/exclude pattern: {exclude!r}") from exc
            result = [m for m in result if not pattern.search(m.module_id)]

        return result

    @staticmethod
    def infer_annotations_from_method(method: str) -> ModuleAnnotations:
        """Infer behavioral annotations from an HTTP method.

        Canonical mapping per
        ``apcore-toolkit/docs/features/scanning.md`` (RFC 9110 §9.2 safe-method
        semantics):
            GET     -> readonly=True, cacheable=True
            HEAD    -> readonly=True
            OPTIONS -> readonly=True
            PUT     -> idempotent=True
            DELETE  -> destructive=True
            Others  -> default (all False)

        Args:
            method: HTTP method string (e.g., "GET", "post"). Case-insensitive.

        Returns:
            ModuleAnnotations instance with inferred flags.
        """
        method_upper = method.upper()
        if method_upper == "GET":
            return replace(DEFAULT_ANNOTATIONS, readonly=True, cacheable=True)
        elif method_upper in ("HEAD", "OPTIONS"):
            return replace(DEFAULT_ANNOTATIONS, readonly=True)
        elif method_upper == "DELETE":
            return replace(DEFAULT_ANNOTATIONS, destructive=True)
        elif method_upper == "PUT":
            return replace(DEFAULT_ANNOTATIONS, idempotent=True)
        return DEFAULT_ANNOTATIONS

    @staticmethod
    def generate_suggested_alias(path: str, method: str) -> str:
        """Generate a dot-separated suggested alias from HTTP route info.

        Convenience wrapper around
        :func:`apcore_toolkit.http_verb_map.generate_suggested_alias` that
        exposes the utility on the familiar ``BaseScanner`` interface.
        Mirrors the pattern of :meth:`infer_annotations_from_method`.
        Never raises; any string input is accepted.

        Args:
            path: URL path (e.g., ``"/tasks/user_data/{id}"``).
            method: HTTP method (e.g., ``"POST"``).

        Returns:
            Dot-separated alias string (e.g., ``"tasks.user_data.get"``).
        """
        from apcore_toolkit.http_verb_map import generate_suggested_alias as _impl

        return _impl(path, method)

    def deduplicate_ids(self, modules: list[ScannedModule]) -> list[ScannedModule]:
        """Resolve duplicate module IDs by appending _2, _3, etc.

        Pre-scans all original IDs so generated suffixes never collide with
        an ID that already exists in the input list. For example, input
        ``[a, a, a_2]`` yields ``[a, a_3, a_2]`` rather than the colliding
        ``[a, a_2, a_2]`` produced by a naive counter. This matches the
        TypeScript and Rust implementations.

        Operates on ScannedModule instances directly, producing new instances
        with updated module_id via ``dataclasses.replace()``. A warning is
        appended to the module's warnings list when a rename occurs.
        """
        seen_count: dict[str, int] = {}
        # Pre-populate with all original IDs so generated suffixes never
        # collide with an ID that already exists in the input list.
        used_ids: set[str] = {m.module_id for m in modules}
        result: list[ScannedModule] = []
        for module in modules:
            mid = module.module_id
            count = seen_count.get(mid, 0)
            seen_count[mid] = count + 1
            if count > 0:
                counter = count + 1
                new_id = f"{mid}_{counter}"
                while new_id in used_ids:
                    counter += 1
                    new_id = f"{mid}_{counter}"
                used_ids.add(new_id)
                result.append(
                    replace(
                        module,
                        module_id=new_id,
                        warnings=[
                            *module.warnings,
                            f"Module ID renamed from '{mid}' to '{new_id}' to avoid collision",
                        ],
                    )
                )
            else:
                result.append(module)
        return result


# ---------------------------------------------------------------------------
# Module-level wrappers for tri-language parity.
#
# Rust exports `filter_modules`, `infer_annotations_from_method`, and
# `deduplicate_ids` as free functions at the crate root. These wrappers
# expose the same symbols at the Python module level so users can write
# `from apcore_toolkit import filter_modules` without instantiating
# BaseScanner first.
# ---------------------------------------------------------------------------


def filter_modules(
    modules: list[ScannedModule],
    include: str | None = None,
    exclude: str | None = None,
) -> list[ScannedModule]:
    """Free-function form of :meth:`BaseScanner.filter_modules`."""
    return BaseScanner.filter_modules(modules, include=include, exclude=exclude)


def infer_annotations_from_method(method: str) -> ModuleAnnotations:
    """Free-function form of :meth:`BaseScanner.infer_annotations_from_method`."""
    return BaseScanner.infer_annotations_from_method(method)


def deduplicate_ids(modules: list[ScannedModule]) -> list[ScannedModule]:
    """Free-function form of :meth:`BaseScanner.deduplicate_ids`.

    The underlying algorithm is stateless, so this helper constructs a
    tiny concrete subclass on the fly to reuse the method body without
    duplicating logic.
    """

    class _Helper(BaseScanner):
        def scan(self, **_: Any) -> list[ScannedModule]:  # pragma: no cover
            return []

        def get_source_name(self) -> str:  # pragma: no cover
            return "_deduplicate_ids_helper"

    return _Helper().deduplicate_ids(modules)
