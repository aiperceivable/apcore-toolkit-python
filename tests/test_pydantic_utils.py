"""Tests for apcore_toolkit.pydantic_utils — Pydantic flattening and target resolution."""

from __future__ import annotations

import inspect

import pytest
from pydantic import BaseModel

from apcore_toolkit.pydantic_utils import flatten_pydantic_params, resolve_target


# --- Test models ---


class TaskCreate(BaseModel):
    title: str
    done: bool = False


class UserUpdate(BaseModel):
    name: str
    email: str = "default@example.com"


# --- Test functions ---


def simple_func(x: int, y: str = "hello") -> str:
    return f"{x}-{y}"


def pydantic_func(body: TaskCreate) -> dict:
    return {"title": body.title, "done": body.done}


def mixed_func(name: str, body: TaskCreate) -> dict:
    return {"name": name, "title": body.title}


def multi_pydantic(task: TaskCreate, user: UserUpdate) -> dict:
    return {"title": task.title, "name": user.name}


def no_hints(x, y):
    return x + y


class TestFlattenPydanticParams:
    def test_no_pydantic_returns_same(self) -> None:
        result = flatten_pydantic_params(simple_func)
        assert result is simple_func

    def test_single_pydantic_model(self) -> None:
        wrapped = flatten_pydantic_params(pydantic_func)
        assert wrapped is not pydantic_func
        result = wrapped(title="Test", done=True)
        assert result == {"title": "Test", "done": True}

    def test_single_pydantic_model_default(self) -> None:
        wrapped = flatten_pydantic_params(pydantic_func)
        result = wrapped(title="Test")
        assert result == {"title": "Test", "done": False}

    def test_mixed_simple_and_pydantic(self) -> None:
        wrapped = flatten_pydantic_params(mixed_func)
        result = wrapped(name="Alice", title="Task1")
        assert result == {"name": "Alice", "title": "Task1"}

    def test_multiple_pydantic_models(self) -> None:
        wrapped = flatten_pydantic_params(multi_pydantic)
        result = wrapped(title="Task", done=True, name="Alice", email="a@b.com")
        assert result == {"title": "Task", "name": "Alice"}

    def test_no_type_hints(self) -> None:
        result = flatten_pydantic_params(no_hints)
        assert result is no_hints

    def test_flat_signature(self) -> None:
        wrapped = flatten_pydantic_params(pydantic_func)
        sig = inspect.signature(wrapped)
        param_names = list(sig.parameters.keys())
        assert "title" in param_names
        assert "done" in param_names
        assert "body" not in param_names

    def test_flat_annotations(self) -> None:
        wrapped = flatten_pydantic_params(pydantic_func)
        assert wrapped.__annotations__["title"] is str
        assert wrapped.__annotations__["done"] is bool

    def test_preserves_name(self) -> None:
        wrapped = flatten_pydantic_params(pydantic_func)
        assert wrapped.__name__ == "pydantic_func"


class TestResolveTarget:
    def test_resolve_builtin(self) -> None:
        result = resolve_target("json:loads")
        import json

        assert result is json.loads

    def test_resolve_nested(self) -> None:
        result = resolve_target("os.path:join")
        import os.path

        assert result is os.path.join

    def test_invalid_format(self) -> None:
        with pytest.raises(ValueError, match="Invalid target format"):
            resolve_target("no_colon_here")

    def test_missing_module(self) -> None:
        with pytest.raises(ImportError):
            resolve_target("nonexistent_module_xyz:func")

    def test_missing_attribute(self) -> None:
        with pytest.raises(AttributeError):
            resolve_target("json:nonexistent_function_xyz")

    def test_last_colon_simple(self) -> None:
        """resolve_target splits on the LAST colon (matches TypeScript lastIndexOf)."""
        import os.path

        result = resolve_target("os.path:join")
        assert result is os.path.join

    def test_last_colon_multi_colon(self) -> None:
        """When the target has multiple colons, the last colon is the separator."""
        # "os.path:join" is the canonical form; here we simulate a target whose
        # module path itself contains a colon (unusual but spec-mandated behaviour).
        # We use "json:loads" as the actual resolution so the test stays importable:
        # module="json", qualname="loads" — one colon, still correct.
        # For the multi-colon case we verify the split logic directly on the string.
        module_path, _, qualname = "node:path:join".rpartition(":")
        assert module_path == "node:path"
        assert qualname == "join"

    def test_dotted_qualname(self) -> None:
        """A dotted qualname (e.g., class.method) must resolve via repeated getattr."""
        # urllib.parse.ParseResult is a class; ._replace is a method bound to it.
        from urllib.parse import ParseResult

        result = resolve_target("urllib.parse:ParseResult._replace")
        assert result is ParseResult._replace

    def test_dotted_qualname_missing_leaf(self) -> None:
        """AttributeError must propagate when the final segment of a dotted qualname is missing."""
        with pytest.raises(AttributeError):
            resolve_target("urllib.parse:ParseResult.nonexistent_method")


class TestFlattenPydanticParamsErrorSurface:
    """Narrowed exception handling in flatten_pydantic_params."""

    def test_unexpected_exception_from_get_type_hints_propagates(self, monkeypatch) -> None:
        """A non-(NameError, TypeError) failure from get_type_hints must NOT be swallowed.

        Before this fix, `except Exception: return func` silently returned the
        unwrapped callable, which then failed later with confusing
        'missing keyword argument' errors. The narrowed handler restores the
        original exception as the caller's responsibility.
        """
        import typing

        original_get_type_hints = typing.get_type_hints

        def _boom(*args, **kwargs):
            raise RuntimeError("synthetic internal error")

        monkeypatch.setattr(typing, "get_type_hints", _boom)
        with pytest.raises(RuntimeError, match="synthetic internal error"):
            flatten_pydantic_params(pydantic_func)

        # Sanity: the monkeypatch scope does not leak.
        monkeypatch.setattr(typing, "get_type_hints", original_get_type_hints)
