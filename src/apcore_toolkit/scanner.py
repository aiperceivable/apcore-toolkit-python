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

from apcore import ModuleAnnotations, parse_docstring
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

    def filter_modules(
        self,
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
            pattern = re.compile(include)
            result = [m for m in result if pattern.search(m.module_id)]

        if exclude is not None:
            pattern = re.compile(exclude)
            result = [m for m in result if not pattern.search(m.module_id)]

        return result

    @staticmethod
    def infer_annotations_from_method(method: str) -> ModuleAnnotations:
        """Infer behavioral annotations from an HTTP method.

        Mapping:
            GET    -> readonly=True
            DELETE -> destructive=True
            PUT    -> idempotent=True
            Others -> default (all False)

        Args:
            method: HTTP method string (e.g., "GET", "post").

        Returns:
            ModuleAnnotations instance with inferred flags.
        """
        method_upper = method.upper()
        if method_upper == "GET":
            return ModuleAnnotations(readonly=True, cacheable=True)
        elif method_upper == "DELETE":
            return ModuleAnnotations(destructive=True)
        elif method_upper == "PUT":
            return ModuleAnnotations(idempotent=True)
        return ModuleAnnotations()

    def deduplicate_ids(self, modules: list[ScannedModule]) -> list[ScannedModule]:
        """Resolve duplicate module IDs by appending _2, _3, etc.

        Operates on ScannedModule instances directly, producing new instances
        with updated module_id via ``dataclasses.replace()``. A warning is
        appended to the module's warnings list when a rename occurs.
        """
        seen: dict[str, int] = {}
        result: list[ScannedModule] = []
        for module in modules:
            mid = module.module_id
            if mid in seen:
                seen[mid] += 1
                new_id = f"{mid}_{seen[mid]}"
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
                seen[mid] = 1
                result.append(module)
        return result
