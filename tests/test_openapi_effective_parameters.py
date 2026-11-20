"""Effective parameter resolution and preservation of the legacy scanner."""

from copy import deepcopy

import pytest

from apcore_toolkit import OpenAPIScanner, OpenApiProxyInputs


def test_effective_parameters_drive_schema_and_locations():
    spec = {
        "openapi": "3.1.0",
        "components": {
            "parameters": {
                "Query": {"$ref": "#/components/parameters/Sort~1Order%20Else"},
                "Sort/Order Else": {"name": "term", "in": "query", "schema": {"type": "string"}},
            }
        },
        "paths": {
            "/items/{id}": {
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
                    {"name": "limit", "in": "query", "required": True, "schema": {"type": "string"}},
                ],
                "get": {
                    "operationId": "items",
                    "parameters": [
                        {"$ref": "#/components/parameters/Query"},
                        {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                    ],
                    "responses": {"200": {"description": "OK"}},
                },
            }
        },
    }
    original = deepcopy(spec)
    scanner = OpenAPIScanner()
    [legacy] = scanner.scan(spec)
    [effective] = scanner.scan_with_effective_parameters(spec)
    assert legacy.input_schema["properties"] == {"limit": {"type": "integer"}}
    assert OpenApiProxyInputs.from_module(legacy) is None
    assert effective.input_schema["properties"] == {
        "id": {"type": "string"},
        "limit": {"type": "integer"},
        "term": {"type": "string"},
    }
    assert effective.input_schema["required"] == ["id"]
    assert OpenApiProxyInputs.from_module(effective) == OpenApiProxyInputs(("id",), ("limit", "term"), (), ())
    assert spec == original


def test_resolution_problems_stay_on_the_affected_operation():
    spec = {
        "openapi": "3.1.0",
        "components": {"parameters": {"Cycle": {"$ref": "#/components/parameters/Cycle"}}},
        "paths": {
            "/bad": {
                "parameters": [
                    {"name": "term", "in": "query"},
                    {"name": "term", "in": "query"},
                    {"$ref": "#/components/parameters/Cycle"},
                    {"$ref": "other.yaml#/components/parameters/Term"},
                    {"$ref": "#/components/parameters/Missing"},
                ],
                "get": {"operationId": "bad", "parameters": {}},
            },
            "/good": {"get": {"operationId": "good"}},
        },
    }
    bad, good = OpenAPIScanner().scan_with_effective_parameters(spec)
    bad_inputs = OpenApiProxyInputs.from_module(bad)
    assert bad_inputs is not None
    assert [problem.kind for problem in bad_inputs.problems] == [
        "duplicate",
        "reference",
        "reference",
        "reference",
        "invalid_list",
    ]
    assert "cyclic parameter reference" in bad_inputs.problems[1].describe()
    assert OpenApiProxyInputs.from_module(good) == OpenApiProxyInputs((), (), (), ())
    bad.metadata["openapi"]["proxy_inputs"]["path"] = "invalid"
    with pytest.raises(ValueError, match="effective input 'path'"):
        OpenApiProxyInputs.from_module(bad)


def test_boolean_parameter_schemas_and_nested_schema_refs_preserve_constraints():
    nested = {
        "oneOf": [False, {"$ref": "#/components/schemas/Allowed"}],
        "properties": {
            "flag": {"$ref": "#/components/schemas/Disallowed"},
            "invalid": None,
            "annotation": {"$ref": "#/components/schemas/Disallowed", "allOf": None, "x-sensitive": True},
        },
        "patternProperties": {"^a": True},
        "prefixItems": [True, False],
    }
    spec = {
        "openapi": "3.1.0",
        "components": {
            "parameters": {"Blocked": {"name": "blocked", "in": "query", "schema": False}},
            "schemas": {"Allowed": True, "Disallowed": False},
        },
        "paths": {
            "/step": {
                "parameters": [{"$ref": "#/components/parameters/Blocked"}],
                "get": {
                    "operationId": "step",
                    "parameters": [
                        {"name": "allowed", "in": "query", "schema": True},
                        {"name": "nested", "in": "query", "schema": nested},
                    ],
                },
            },
            "/other": {"get": {"operationId": "other"}},
        },
    }
    scanned = {module.module_id: module for module in OpenAPIScanner().scan_with_effective_parameters(spec)}
    assert scanned["step"].input_schema["properties"] == {
        "blocked": False,
        "allowed": True,
        "nested": {
            "oneOf": [False, True],
            "properties": {
                "flag": False,
                "invalid": {},
                "annotation": {"allOf": [False], "x-sensitive": True},
            },
            "patternProperties": {"^a": True},
            "prefixItems": [True, False],
        },
    }
    assert scanned["other"].input_schema["properties"] == {}


def test_effective_schema_reference_intersects_assertions_and_resolves_fragment_targets():
    from jsonschema import Draft202012Validator

    document = {
        "openapi": "3.1.0",
        "components": {
            "schemas": {
                "Base": {"type": "string", "enum": ["red", "blue"]},
                "Object": {"type": "object", "properties": {"value": {"type": "string"}}},
                "Switch Name": {"anyOf": [False, True]},
            },
        },
        "paths": {
            "/colors": {
                "get": {
                    "parameters": [
                        {
                            "name": "color",
                            "in": "query",
                            "schema": {
                                "$ref": "#/components/schemas/Base",
                                "enum": ["red", "green"],
                                "x-sensitive": True,
                            },
                        },
                        {
                            "name": "flag",
                            "in": "query",
                            "schema": {"$ref": "#/components/schemas/Switch%20Name/anyOf/0"},
                        },
                        {
                            "name": "object",
                            "in": "query",
                            "schema": {"$ref": "#/components/schemas/Object", "unevaluatedProperties": False},
                        },
                    ],
                },
            },
        },
    }

    [module] = OpenAPIScanner().scan_with_effective_parameters(document)
    properties = module.input_schema["properties"]
    validator = Draft202012Validator(properties["color"])
    assert validator.is_valid("red")
    assert not validator.is_valid("green")
    assert not validator.is_valid("blue")
    assert properties["color"]["x-sensitive"] is True
    assert properties["flag"] is False
    object_validator = Draft202012Validator(properties["object"])
    assert object_validator.is_valid({"value": "ok"})
    assert not object_validator.is_valid({"other": "unexpected"})


@pytest.mark.parametrize("boolean", [True, False])
def test_effective_response_schema_does_not_crash_excluded_operations(boolean):
    scanner = OpenAPIScanner()
    document = {
        "openapi": "3.1.0",
        "paths": {
            "/keep": {"get": {"operationId": "keep"}},
            "/bool": {
                "get": {
                    "operationId": "boolean",
                    "responses": {"200": {"content": {"application/json": {"schema": boolean}}}},
                }
            },
        },
    }
    [selected] = scanner.scan_with_effective_parameters(document, include="^keep$")
    assert selected.module_id == "keep"
    [boolean_module] = scanner.scan_with_effective_parameters(document, include="^boolean$")
    assert boolean_module.output_schema == ({} if boolean else {"not": {}})


def test_effective_request_body_selects_actual_proxy_media_type_and_resolves_reference():
    body = {
        "content": {
            "application/vnd.api+json": {"schema": {"type": "object", "properties": {"vendor": {"type": "string"}}}},
            "application/json": {"schema": {"type": "object", "properties": {"plain": {"type": "string"}}}},
        }
    }
    document = {
        "openapi": "3.1.0",
        "components": {"requestBodies": {"Create": body}},
        "paths": {"/orders": {"post": {"requestBody": {"$ref": "#/components/requestBodies/Create"}}}},
    }
    [module] = OpenAPIScanner().scan_with_effective_parameters(document)
    assert module.input_schema["properties"] == {"plain": {"type": "string"}}
    assert OpenApiProxyInputs.from_module(module) == OpenApiProxyInputs((), (), ("plain",), ())
