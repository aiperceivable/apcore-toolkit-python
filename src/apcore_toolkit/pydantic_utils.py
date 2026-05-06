"""Pydantic model flattening utilities.

Provides the framework-agnostic core for converting Pydantic model
parameters into flat keyword arguments suitable for MCP tool invocation.

``resolve_target`` was previously defined in this module but has been
moved to ``apcore_toolkit.resolve_target`` to match the TypeScript and
Rust SDK layout. It is re-exported here for backwards compatibility —
existing ``from apcore_toolkit.pydantic_utils import resolve_target``
imports keep working.
"""

from __future__ import annotations

import functools
import inspect
import logging
import typing
from typing import Annotated, Any

from pydantic import BaseModel, Field

from apcore_toolkit.resolve_target import (
    _module_path_matches_prefix as _module_path_matches_prefix,
)
from apcore_toolkit.resolve_target import resolve_target as resolve_target

__all__ = [
    "_module_path_matches_prefix",
    "flatten_pydantic_params",
    "resolve_target",
]

logger = logging.getLogger("apcore_toolkit")


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
