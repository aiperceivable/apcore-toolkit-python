"""Pydantic model flattening and target resolution utilities.

Extracted from flask-apcore's registry_writer.py. Provides the
framework-agnostic core for converting Pydantic model parameters into
flat keyword arguments suitable for MCP tool invocation.
"""

from __future__ import annotations

import functools
import importlib
import inspect
import logging
import typing
from typing import Annotated, Any

from pydantic import BaseModel, Field

logger = logging.getLogger("apcore_toolkit")


def resolve_target(target: str, allowed_prefixes: list[str] | None = None) -> Any:
    """Resolve a ``module.path:qualname`` target string to a callable.

    ``qualname`` may be a single identifier (``my_func``) or a dotted
    path (``MyClass.my_method``) — the resolver walks segments via
    repeated ``getattr``, matching Python's own ``__qualname__``
    convention for nested callables.

    When ``allowed_prefixes`` is provided, ``module_path`` must equal one of
    the listed prefixes or be a dotted descendant; otherwise
    ``PermissionError`` is raised **before** ``importlib.import_module`` is
    called. This mitigates arbitrary-code-execution via forged binding files
    (e.g. a malicious ``target: "os:system"`` injected into untrusted YAML).
    Mirrors the TypeScript SDK's ``allowedPrefixes`` directory allowlist,
    adapted to Python's module-name import model.

    Args:
        target: Target string in ``module.path:qualname`` format.
        allowed_prefixes: Optional list of module-name prefixes. When set,
            ``module_path`` must match one of them (exact or dotted
            descendant) or the call is rejected. Prefixes are matched
            case-sensitively; a trailing dot is tolerated and ignored.

    Returns:
        The resolved callable.

    Raises:
        ValueError: If the target format is invalid.
        PermissionError: If ``allowed_prefixes`` is set and ``module_path``
            is not permitted.
        ImportError: If the module cannot be imported.
        AttributeError: If any segment of the qualified name cannot be resolved.
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


def flatten_pydantic_params(func: Any) -> Any:
    """Wrap a function so Pydantic model params are flattened to scalar kwargs.

    View functions like ``create_task(body: TaskCreate)`` expect a Pydantic
    model instance. MCP tools should expose flat fields instead (``title``,
    ``description``, ...). This wrapper bridges the gap:

    1. Inspects the function signature for Pydantic BaseModel parameters.
    2. Creates a wrapper accepting the model's fields as flat kwargs.
    3. Reconstructs the Pydantic model(s) internally before calling
       the original function.

    If the function has no Pydantic model parameters, it is returned as-is.
    """
    try:
        hints = typing.get_type_hints(func, include_extras=True)
    except (NameError, TypeError) as exc:
        # Unresolved forward refs (NameError) and invalid annotations
        # (TypeError) are the documented get_type_hints failure modes:
        # treat them as "no flattening possible" and return the original
        # callable. Other exceptions (ImportError, RuntimeError, …) are
        # genuine bugs and must propagate — silently swallowing them
        # produces confusing 'missing keyword argument' errors at call
        # time instead of pointing at the real failure.
        logger.debug("flatten_pydantic_params: get_type_hints failed for %s: %s", func, exc)
        return func

    sig = inspect.signature(func)

    pydantic_params: dict[str, type[BaseModel]] = {}
    simple_params: list[tuple[str, inspect.Parameter]] = []

    for name, param in sig.parameters.items():
        hint = hints.get(name)
        if hint is not None and isinstance(hint, type) and issubclass(hint, BaseModel):
            pydantic_params[name] = hint
        else:
            simple_params.append((name, param))

    if not pydantic_params:
        return func

    # Build flat signature and annotations for the wrapper
    flat_params: list[inspect.Parameter] = []
    flat_annotations: dict[str, Any] = {}

    for name, param in simple_params:
        flat_params.append(param)
        if name in hints:
            flat_annotations[name] = hints[name]

    for model_cls in pydantic_params.values():
        for field_name, field_info in model_cls.model_fields.items():
            default = field_info.default if not field_info.is_required() else inspect.Parameter.empty

            annotation: Any = field_info.annotation
            field_kwargs: dict[str, Any] = {}
            if field_info.description is not None:
                field_kwargs["description"] = field_info.description
            if field_info.examples is not None:
                field_kwargs["examples"] = field_info.examples
            if field_info.json_schema_extra is not None:
                field_kwargs["json_schema_extra"] = field_info.json_schema_extra
            if field_kwargs:
                annotation = Annotated[annotation, Field(**field_kwargs)]

            flat_params.append(
                inspect.Parameter(
                    field_name,
                    kind=inspect.Parameter.KEYWORD_ONLY,
                    default=default,
                    annotation=annotation,
                )
            )
            flat_annotations[field_name] = annotation

    if "return" in hints:
        flat_annotations["return"] = hints["return"]

    simple_param_names = {name for name, _ in simple_params}

    @functools.wraps(func)
    def wrapper(**kwargs: Any) -> Any:
        call_kwargs: dict[str, Any] = {}
        remaining = dict(kwargs)

        for name in list(remaining):
            if name in simple_param_names:
                call_kwargs[name] = remaining.pop(name)

        for param_name, model_cls in pydantic_params.items():
            model_field_names = set(model_cls.model_fields.keys())
            model_data = {k: remaining.pop(k) for k in list(remaining) if k in model_field_names}
            call_kwargs[param_name] = model_cls(**model_data)

        if remaining:
            raise TypeError(f"Unexpected keyword arguments: {sorted(remaining)}")

        return func(**call_kwargs)

    wrapper.__signature__ = inspect.Signature(flat_params)  # type: ignore[attr-defined]
    wrapper.__annotations__ = flat_annotations
    return wrapper
