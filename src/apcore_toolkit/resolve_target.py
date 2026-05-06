"""Module-path target resolution with optional allowlist.

The ``resolve_target`` helper turns a ``"module.path:qualname"`` string
into the underlying callable. An optional ``allowed_prefixes`` argument
restricts which module paths may be imported, mitigating arbitrary-code-
execution via forged binding files. Mirrors the TypeScript SDK's
``resolveTarget`` (in ``src/resolve-target.ts``) and the Rust SDK's
``resolve_target`` (in ``src/resolve_target.rs``).

This module was split out of ``pydantic_utils.py`` to match the layout of
the TypeScript and Rust SDKs (each language keeps target resolution in a
dedicated file). ``pydantic_utils.py`` re-exports ``resolve_target`` for
backwards compatibility.
"""

from __future__ import annotations

import importlib
from typing import Any


def resolve_target(target: str, allowed_prefixes: list[str] | None = None) -> Any:
    """Resolve a ``module.path:qualname`` target string to a callable.

    ``qualname`` may be a single identifier (``my_func``) or a dotted
    path (``MyClass.my_method``) — the resolver walks segments via
    repeated ``getattr``, matching Python's own ``__qualname__``
    convention for nested callables.

    When ``allowed_prefixes`` is provided, ``module_path`` must equal one
    of the listed prefixes or be a dotted descendant; otherwise
    ``PermissionError`` is raised **before** ``importlib.import_module``
    is called. This mitigates arbitrary-code-execution via forged binding
    files (e.g. a malicious ``target: "os:system"`` injected into
    untrusted YAML). Mirrors the TypeScript SDK's ``allowedPrefixes``
    directory allowlist, adapted to Python's module-name import model.

    Args:
        target: Target string in ``module.path:qualname`` format.
        allowed_prefixes: Optional list of module-name prefixes. When
            set, ``module_path`` must match one of them (exact or dotted
            descendant) or the call is rejected. Prefixes are matched
            case-sensitively; a trailing dot is tolerated and ignored.

    Returns:
        The resolved callable.

    Raises:
        ValueError: If the target format is invalid.
        PermissionError: If ``allowed_prefixes`` is set and
            ``module_path`` is not permitted.
        ImportError: If the module cannot be imported.
        AttributeError: If any segment of the qualified name cannot be
            resolved.
    """
    if ":" not in target:
        raise ValueError(f"Invalid target format: {target!r}. Expected 'module.path:qualname'.")
    module_path, _, qualname = target.rpartition(":")

    if allowed_prefixes:
        if not any(_module_path_matches_prefix(module_path, p) for p in allowed_prefixes):
            raise PermissionError(
                f"Import of {module_path!r} is not allowed: module path must "
                f"match one of allowed_prefixes={allowed_prefixes}"
            )

    obj: Any = importlib.import_module(module_path)
    for part in qualname.split("."):
        obj = getattr(obj, part)
    return obj


def _module_path_matches_prefix(module_path: str, prefix: str) -> bool:
    """Return True if ``module_path`` is exactly ``prefix`` or a dotted descendant.

    A trailing dot on ``prefix`` is tolerated. Boundary-aware so that
    ``"myapp"`` does NOT permit ``"myappx"``.
    """
    normalized = prefix.rstrip(".")
    if not normalized:
        return False
    return module_path == normalized or module_path.startswith(normalized + ".")
