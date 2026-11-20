"""Shared preflight for proxying effective OpenAPI operations."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from apcore_toolkit.http_verb_map import _effective_openapi_path, _openapi_path_param_names
from apcore_toolkit.openapi_parameters import OpenApiProxyInputs
from apcore_toolkit.output.http_proxy_writer import proxy_uses_json_body, read_proxy_route

if TYPE_CHECKING:
    from apcore_toolkit.openapi_parameters import OpenApiParameterProblem
    from apcore_toolkit.types import ScannedModule

# Fetch cannot send TRACE, so the portable proxy contract excludes it.
_SUPPORTED_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})


@dataclass(frozen=True)
class EffectiveProxyHazard:
    """A selected operation's input cannot be forwarded without ambiguity."""

    module_id: str
    http_method: str
    url_path: str
    kind: str
    parameters: tuple[str, ...] = ()
    locations: tuple[str, ...] = ()
    detail: str = ""
    problem: OpenApiParameterProblem | None = None
    key: str = ""
    undeclared: tuple[str, ...] = ()
    unused: tuple[str, ...] = ()
    unlocated: tuple[str, ...] = ()
    unpublished: tuple[str, ...] = ()

    def describe(self) -> str:
        return f"{self.module_id} ({self.http_method} {self.url_path}): {self.detail}"

    def to_dict(self) -> dict[str, Any]:
        """Serialize using the shared effective proxy hazard contract."""
        kind: dict[str, Any] = {"code": self.kind}
        if self.kind == "invalid_effective_inputs":
            kind["detail"] = self.detail
        elif self.kind == "missing_route_metadata":
            kind["key"] = self.key
        elif self.kind == "unsupported_http_method":
            kind["method"] = self.http_method
        elif self.kind == "parameter_resolution" and self.problem is not None:
            kind["problem"] = self.problem.to_dict()
        elif self.kind == "query_on_body_method":
            kind["parameters"] = list(self.parameters)
        elif self.kind == "body_on_query_method":
            kind["properties"] = list(self.parameters)
        elif self.kind == "name_collision":
            kind.update(name=self.parameters[0], locations=list(self.locations))
        elif self.kind == "path_template_mismatch":
            kind.update(undeclared=list(self.undeclared), unused=list(self.unused))
        elif self.kind == "input_schema_mismatch":
            kind.update(unlocated=list(self.unlocated), unpublished=list(self.unpublished))
        return {
            "module_id": self.module_id,
            "http_method": self.http_method,
            "url_path": self.url_path,
            "kind": kind,
        }


def detect_effective_proxy_hazards(modules: Sequence[ScannedModule]) -> list[EffectiveProxyHazard]:
    """Check selected modules from ``scan_with_effective_parameters``.

    The same scanner result supplied the tool's input schema. Pass the final
    selection so findings in excluded operations do not affect the result.
    A module lacking valid metadata yields a finding.
    """
    hazards: list[EffectiveProxyHazard] = []
    for module in modules:
        raw_method, raw_path = read_proxy_route(module)
        method = raw_method if isinstance(raw_method, str) and raw_method.strip() else None
        path = _effective_openapi_path(raw_path)
        route = (module.module_id, method or "?", path or "?")
        try:
            inputs = OpenApiProxyInputs.from_module(module)
        except ValueError as error:
            hazards.append(EffectiveProxyHazard(*route, "invalid_effective_inputs", detail=str(error)))
            continue
        if inputs is None:
            hazards.append(
                EffectiveProxyHazard(*route, "missing_effective_inputs", detail="effective input metadata is missing")
            )
            continue
        hazards.extend(_check_effective_inputs(route, method, path, inputs, module.input_schema))
    return sorted(hazards, key=lambda hazard: (hazard.module_id, hazard.http_method, hazard.url_path))


def _check_effective_inputs(
    route: tuple[str, str, str],
    method: str | None,
    path: str | None,
    inputs: OpenApiProxyInputs,
    schema: dict[str, Any],
) -> list[EffectiveProxyHazard]:
    hazards = [
        EffectiveProxyHazard(*route, "parameter_resolution", detail=problem.describe(), problem=problem)
        for problem in inputs.problems
    ]
    method_hazard = _check_http_method(route, method, inputs)
    if method_hazard is not None:
        hazards.append(method_hazard)
    path_hazard = _check_path_template(route, path, inputs.path)
    if path_hazard is not None:
        hazards.append(path_hazard)

    names: dict[str, list[str]] = {}
    for location, parameters in (("path", inputs.path), ("query", inputs.query), ("body", inputs.body)):
        for name in dict.fromkeys(parameters):
            names.setdefault(name, []).append(location)
    for name, locations in sorted(names.items()):
        if len(locations) > 1:
            hazards.append(
                EffectiveProxyHazard(
                    *route,
                    "name_collision",
                    parameters=(name,),
                    locations=tuple(locations),
                    detail=f"input {name!r} has conflicting locations: {', '.join(locations)}",
                )
            )
    properties = schema.get("properties") if isinstance(schema, dict) else None
    published = set(properties) if isinstance(properties, dict) else set()
    if set(names) != published:
        unlocated, unpublished = sorted(published - names.keys()), sorted(names.keys() - published)
        hazards.append(
            EffectiveProxyHazard(
                *route,
                "input_schema_mismatch",
                parameters=tuple(unlocated + unpublished),
                unlocated=tuple(unlocated),
                unpublished=tuple(unpublished),
                detail=f"schema differs from effective inputs; unlocated: {unlocated!r}; unpublished: {unpublished!r}",
            )
        )
    return hazards


def _check_http_method(
    route: tuple[str, str, str], method: str | None, inputs: OpenApiProxyInputs
) -> EffectiveProxyHazard | None:
    if method is None:
        return _missing_route_metadata(route, "http_method")
    if method not in _SUPPORTED_HTTP_METHODS:
        return EffectiveProxyHazard(*route, "unsupported_http_method", detail=f"unsupported HTTP method: {method!r}")
    if proxy_uses_json_body(method):
        if inputs.query:
            return EffectiveProxyHazard(
                *route,
                "query_on_body_method",
                parameters=inputs.query,
                locations=("query",),
                detail=f"query parameters cannot be forwarded by the JSON body method: {', '.join(inputs.query)}",
            )
    elif inputs.body:
        properties = tuple(sorted(inputs.body))
        return EffectiveProxyHazard(
            *route,
            "body_on_query_method",
            parameters=properties,
            locations=("body",),
            detail=f"JSON body properties cannot be forwarded by the query method: {', '.join(properties)}",
        )
    return None


def _check_path_template(
    route: tuple[str, str, str], path: str | None, path_inputs: tuple[str, ...]
) -> EffectiveProxyHazard | None:
    if path is None:
        return _missing_route_metadata(route, "url_path")
    template = _openapi_path_param_names(path)
    declared = set(path_inputs)
    if template == declared:
        return None
    undeclared, unused = sorted(template - declared), sorted(declared - template)
    return EffectiveProxyHazard(
        *route,
        "path_template_mismatch",
        parameters=tuple(undeclared + unused),
        locations=("path",),
        undeclared=tuple(undeclared),
        unused=tuple(unused),
        detail=f"path template differs from declarations; undeclared: {undeclared!r}; unused: {unused!r}",
    )


def _missing_route_metadata(route: tuple[str, str, str], key: str) -> EffectiveProxyHazard:
    return EffectiveProxyHazard(*route, "missing_route_metadata", key=key, detail=f"valid {key!r} is required")


def validate_effective_proxy_modules(modules: Sequence[ScannedModule]) -> None:
    """Raise ValueError before registering any unforwardable selected operation."""
    hazards = detect_effective_proxy_hazards(modules)
    if hazards:
        details = "; ".join(hazard.describe() for hazard in hazards)
        raise ValueError(f"OpenAPI proxy cannot forward selected operations: {details}")
