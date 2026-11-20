"""Resolve effective OpenAPI parameters for schema and HTTP proxy diagnostics."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from apcore_toolkit.openapi import _deep_resolve_refs, _extract_input_schema, _walk_local_pointer

if TYPE_CHECKING:
    from apcore_toolkit.types import ScannedModule


_METADATA_KEY = "proxy_inputs"
_EXTERNAL_REF = "<external-ref>"
_LOCATIONS = ("path", "query", "header", "cookie")
_PROBLEM_FIELDS: dict[str, dict[str, type]] = {
    "invalid_list": {},
    "invalid_parameter": {"index": int},
    "reference": {"index": int, "reference": str, "reason": str},
    "duplicate": {"name": str, "location": str},
    "unsupported_input": {"name": str, "location": str, "reason": str},
}


@dataclass(frozen=True)
class OpenApiParameterProblem:
    """An operation-local issue encountered while resolving a parameter."""

    kind: str
    scope: str
    index: int | None = None
    reference: str | None = None
    reason: str | None = None
    name: str | None = None
    location: str | None = None

    def describe(self) -> str:
        scope = "Path Item" if self.scope == "path_item" else "operation"
        if self.kind == "invalid_list":
            return f"{scope} 'parameters' must be an array"
        if self.kind == "invalid_parameter":
            return f"{scope} parameter at index {self.index} has an invalid name or location"
        if self.kind == "reference":
            return (
                f"{scope} parameter at index {self.index} has an unresolved reference {self.reference!r}: {self.reason}"
            )
        if self.kind == "unsupported_input":
            return f"{scope} cannot proxy {self.name!r} in {self.location!r}: {self.reason}"
        return f"{scope} declares parameter {self.name!r} in {self.location!r} more than once"

    def to_dict(self) -> dict[str, Any]:
        fields = _PROBLEM_FIELDS[self.kind]
        return {"kind": self.kind, "scope": self.scope, **{key: getattr(self, key) for key in fields}}

    @classmethod
    def _from_dict(cls, value: Any) -> OpenApiParameterProblem:
        if not isinstance(value, dict):
            raise ValueError("effective input problem must be a mapping")
        kind, scope = value.get("kind"), value.get("scope")
        if not isinstance(kind, str) or kind not in _PROBLEM_FIELDS:
            raise ValueError("effective input problem has an unrecognized kind")
        if scope not in ("path_item", "operation"):
            raise ValueError("effective input problem has an unrecognized scope")
        fields = _PROBLEM_FIELDS[kind]
        for key, kind_type in fields.items():
            field = value.get(key)
            if type(field) is not kind_type or (kind_type is int and field < 0):
                raise ValueError(f"effective input problem has an invalid {key!r}")
        if kind == "unsupported_input" and (scope != "operation" or value["location"] not in ("path", "query", "body")):
            raise ValueError("unsupported input problems must identify a path, query or body input on an operation")
        attributes = {key: value[key] for key in fields}
        if kind == "reference" and attributes["reference"] != "<non-string>":
            attributes["reference"] = _diagnostic_reference(attributes["reference"])
        return cls(kind, scope, **attributes)


@dataclass(frozen=True)
class OpenApiProxyInputs:
    """Names exposed in the flat tool input, grouped by OpenAPI origin."""

    path: tuple[str, ...]
    query: tuple[str, ...]
    body: tuple[str, ...]
    problems: tuple[OpenApiParameterProblem, ...]
    required_json_body: bool = False

    def to_dict(self) -> dict[str, Any]:
        serialized: dict[str, Any] = {
            "path": list(self.path),
            "query": list(self.query),
            "body": list(self.body),
            "problems": [problem.to_dict() for problem in self.problems],
        }
        if self.required_json_body:
            serialized["required_json_body"] = True
        return serialized

    @classmethod
    def from_module(cls, module: ScannedModule) -> OpenApiProxyInputs | None:
        """Read opt-in scanner metadata. Raise ValueError for malformed metadata."""
        openapi = module.metadata.get("openapi") if isinstance(module.metadata, dict) else None
        if not isinstance(openapi, dict) or _METADATA_KEY not in openapi:
            return None
        value = openapi[_METADATA_KEY]
        if not isinstance(value, dict):
            raise ValueError("effective input metadata must be a mapping")

        def names(location: str) -> tuple[str, ...]:
            entries = value.get(location)
            if not isinstance(entries, list) or any(not isinstance(name, str) for name in entries):
                raise ValueError(f"effective input {location!r} must be an array of strings")
            return tuple(entries)

        problems = value.get("problems")
        if not isinstance(problems, list):
            raise ValueError("effective input 'problems' must be an array")
        required_json_body = value.get("required_json_body", False)
        if not isinstance(required_json_body, bool):
            raise ValueError("effective input 'required_json_body' must be a boolean")
        return cls(
            path=names("path"),
            query=names("query"),
            body=names("body"),
            problems=tuple(OpenApiParameterProblem._from_dict(problem) for problem in problems),
            required_json_body=required_json_body,
        )


def _diagnostic_reference(reference: str) -> str:
    return reference if reference.startswith("#") else _EXTERNAL_REF


def _resolve_parameter(
    parameter: Any, document: dict[str, Any], scope: str, index: int
) -> tuple[Any, OpenApiParameterProblem | None]:
    seen: set[str] = set()
    while isinstance(parameter, dict) and "$ref" in parameter:
        reference = parameter["$ref"]
        if not isinstance(reference, str):
            return None, OpenApiParameterProblem(
                "reference", scope, index, "<non-string>", "expected a string reference"
            )
        if reference in seen:
            return None, OpenApiParameterProblem("reference", scope, index, reference, "cyclic parameter reference")
        seen.add(reference)
        try:
            parameter = _walk_local_pointer(document, reference)
            if not isinstance(parameter, dict):
                raise ValueError("pointer does not identify an object")
        except ValueError as error:
            return None, OpenApiParameterProblem(
                "reference", scope, index, _diagnostic_reference(reference), str(error)
            )
    return parameter, None


def _resolve_list(
    owner: dict[str, Any],
    document: dict[str, Any],
    scope: str,
    problems: list[OpenApiParameterProblem],
) -> dict[tuple[str, str], dict[str, Any]]:
    entries = owner.get("parameters", [])
    if not isinstance(entries, list):
        problems.append(OpenApiParameterProblem("invalid_list", scope))
        return {}
    parameters = {}
    for index, entry in enumerate(entries):
        parameter, problem = _resolve_parameter(entry, document, scope, index)
        if problem is not None:
            problems.append(problem)
            continue
        if isinstance(parameter, dict):
            name, location = parameter.get("name"), parameter.get("in")
            if isinstance(name, str) and name and isinstance(location, str) and location in _LOCATIONS:
                key = (name, location)
                if key in parameters:
                    problems.append(OpenApiParameterProblem("duplicate", scope, name=name, location=location))
                else:
                    parameters[key] = parameter
                continue
        problems.append(OpenApiParameterProblem("invalid_parameter", scope, index))
    return parameters


def _resolve_request_body(body: Any, document: dict[str, Any]) -> dict[str, Any]:
    seen: set[str] = set()
    while isinstance(body, dict) and "$ref" in body:
        reference = body["$ref"]
        if not isinstance(reference, str) or reference in seen:
            raise ValueError("invalid or cyclic Request Body Object reference")
        seen.add(reference)
        body = _walk_local_pointer(document, reference)
    if not isinstance(body, dict):
        raise ValueError("expected a Request Body Object")
    return body


def _effective_request_body_schema(
    operation: dict[str, Any], document: dict[str, Any]
) -> tuple[dict[str, Any], bool, str | None]:
    if "requestBody" not in operation:
        return {}, False, None
    try:
        body = _resolve_request_body(operation["requestBody"], document)
    except ValueError:
        return {}, False, "request_body_reference"
    content = body.get("content")
    if not isinstance(content, dict) or "application/json" not in content:
        return {}, False, "request_body_media_type"
    media = content["application/json"]
    raw = media.get("schema") if isinstance(media, dict) else None
    if not isinstance(raw, dict):
        return {}, False, "request_body_schema"
    schema = _deep_resolve_refs(raw, document, boolean_refs=True)
    if (
        not isinstance(schema, dict)
        or schema.get("type", "object") != "object"
        or not isinstance(schema.get("properties"), dict)
        or not schema["properties"]
        or any(key in schema for key in ("allOf", "anyOf", "oneOf", "not", "if"))
    ):
        return {}, False, "request_body_schema"
    return schema, body.get("required") is True, None


def _schema_branches(schema: Any) -> Iterator[dict[str, Any]]:
    if not isinstance(schema, dict):
        return
    yield schema
    for keyword in ("allOf", "anyOf", "oneOf"):
        clauses = schema.get(keyword)
        if isinstance(clauses, list):
            for clause in clauses:
                yield from _schema_branches(clause)


def _explicit_type(schema: dict[str, Any], *names: str) -> bool:
    schema_type = schema.get("type")
    return schema_type in names or (isinstance(schema_type, list) and any(name in schema_type for name in names))


def _array_item_schemas(schema: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield from _schema_branches(schema.get("items"))
    prefix = schema.get("prefixItems")
    if isinstance(prefix, list):
        for item in prefix:
            yield from _schema_branches(item)


def _explicit_scalar_type(schema: Any) -> bool:
    scalar_types = ("string", "integer", "number", "boolean")
    declared = schema.get("type") if isinstance(schema, dict) else None
    return declared in scalar_types or (
        isinstance(declared, list) and bool(declared) and all(kind in scalar_types for kind in declared)
    )


def _unsupported_serialization(location: str, parameter: dict[str, Any], schema: Any) -> bool:
    if "content" in parameter:
        return True
    branches = tuple(_schema_branches(schema))
    if location == "path":
        return parameter.get("style", "simple") != "simple" or any(
            _explicit_type(branch, "object", "array") for branch in branches
        )

    if parameter.get("style", "form") != "form" or any(_explicit_type(branch, "object") for branch in branches):
        return True
    if parameter.get("explode", True) is not True and not _explicit_scalar_type(schema):
        return True
    if not any(_explicit_type(branch, "array") for branch in branches):
        return False
    return any(
        _explicit_type(items, "array", "object", "null") for branch in branches for items in _array_item_schemas(branch)
    )


def _effective_input_schema(
    path_item: dict[str, Any], operation: dict[str, Any], document: dict[str, Any]
) -> tuple[dict[str, Any], OpenApiProxyInputs]:
    problems: list[OpenApiParameterProblem] = []
    parameters = _resolve_list(path_item, document, "path_item", problems)
    parameters.update(_resolve_list(operation, document, "operation", problems))
    body, required_json_body, body_problem = _effective_request_body_schema(operation, document)
    schema = _extract_input_schema(list(parameters.values()), body, document, boolean_refs=True)
    if body_problem:
        problems.append(
            OpenApiParameterProblem(
                "unsupported_input", "operation", name="requestBody", location="body", reason=body_problem
            )
        )
    for (name, location), parameter in parameters.items():
        if location in ("path", "query") and _unsupported_serialization(
            location, parameter, schema["properties"].get(name)
        ):
            problems.append(
                OpenApiParameterProblem(
                    "unsupported_input", "operation", name=name, location=location, reason=f"{location}_serialization"
                )
            )
    inputs = OpenApiProxyInputs(
        path=tuple(name for name, location in parameters if location == "path"),
        query=tuple(name for name, location in parameters if location == "query"),
        body=tuple((body.get("properties") or {}).keys()),
        problems=tuple(problems),
        required_json_body=required_json_body,
    )
    return schema, inputs


__all__ = ["OpenApiParameterProblem", "OpenApiProxyInputs"]
