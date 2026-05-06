"""Tests for apcore_toolkit.openapi — OpenAPI utilities."""

from __future__ import annotations

from apcore_toolkit.openapi import (
    _deep_resolve_refs,
    deep_resolve_refs,
    extract_input_schema,
    extract_output_schema,
    resolve_ref,
    resolve_schema,
)

OPENAPI_DOC = {
    "components": {
        "schemas": {
            "User": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "name": {"type": "string"},
                },
                "required": ["id", "name"],
            },
            "UserList": {
                "type": "array",
                "items": {"$ref": "#/components/schemas/User"},
            },
            "Address": {
                "type": "object",
                "properties": {
                    "street": {"type": "string"},
                    "city": {"type": "string"},
                },
            },
            "UserWithAddress": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "address": {"$ref": "#/components/schemas/Address"},
                },
            },
            "AdminUser": {
                "allOf": [
                    {"$ref": "#/components/schemas/User"},
                    {
                        "type": "object",
                        "properties": {"role": {"type": "string"}},
                    },
                ],
            },
            "SelfRef": {
                "type": "object",
                "properties": {
                    "child": {"$ref": "#/components/schemas/SelfRef"},
                },
            },
        }
    }
}


class TestResolveRef:
    def test_resolve_existing_ref(self) -> None:
        result = resolve_ref("#/components/schemas/User", OPENAPI_DOC)
        assert result["type"] == "object"
        assert "name" in result["properties"]

    def test_resolve_nonexistent_ref(self) -> None:
        result = resolve_ref("#/components/schemas/Missing", OPENAPI_DOC)
        assert result == {}

    def test_non_hash_ref(self) -> None:
        result = resolve_ref("external.yaml#/Foo", OPENAPI_DOC)
        assert result == {}

    def test_ref_to_non_dict(self) -> None:
        doc = {"components": {"schemas": {"Leaf": "not-a-dict"}}}
        result = resolve_ref("#/components/schemas/Leaf", doc)
        assert result == {}

    def test_ref_through_missing_path(self) -> None:
        result = resolve_ref("#/a/b/c", OPENAPI_DOC)
        assert result == {}

    def test_rfc6901_slash_escape(self) -> None:
        # RFC 6901 §4: `/` inside a pointer segment is escaped as `~1`.
        # Without decoding, the resolver would split on `~1` literally
        # and miss the schema. With decoding, it walks `Foo/Bar` correctly.
        doc = {"components": {"schemas": {"Foo/Bar": {"type": "string"}}}}
        result = resolve_ref("#/components/schemas/Foo~1Bar", doc)
        assert result == {"type": "string"}

    def test_rfc6901_tilde_escape(self) -> None:
        # `~` inside a pointer segment is escaped as `~0`.
        doc = {"components": {"schemas": {"Foo~Bar": {"type": "integer"}}}}
        result = resolve_ref("#/components/schemas/Foo~0Bar", doc)
        assert result == {"type": "integer"}

    def test_rfc6901_decode_order(self) -> None:
        # `~01` MUST decode to `~1` (not `/`): `~0` decodes first to `~`,
        # then the `1` is left untouched. Verifies the spec-mandated order.
        doc = {"components": {"schemas": {"Foo~1Bar": {"type": "boolean"}}}}
        result = resolve_ref("#/components/schemas/Foo~01Bar", doc)
        assert result == {"type": "boolean"}


class TestExtractInputSchemaParametersGuard:
    """D11-7 regression: ``extract_input_schema`` must not crash when an
    OpenAPI operation supplies ``parameters`` as a non-list value."""

    def test_non_list_parameters_logs_and_returns_empty_schema(self, caplog) -> None:
        import logging

        operation = {"parameters": {"name": "x", "in": "query"}}  # malformed: object, not list
        with caplog.at_level(logging.WARNING, logger="apcore_toolkit"):
            schema = extract_input_schema(operation, openapi_doc=None)
        assert schema == {"type": "object", "properties": {}, "required": []}
        assert "parameters" in caplog.text.lower()

    def test_non_dict_parameter_entries_skipped(self) -> None:
        # An array of strings (instead of dicts) must not crash; entries
        # that are not dicts are silently skipped so the loop does not
        # invoke ``.get("in")`` on a string.
        operation = {"parameters": ["bad-entry", {"name": "id", "in": "query", "required": True}]}
        schema = extract_input_schema(operation, openapi_doc=None)
        assert "id" in schema["properties"]


class TestResolveSchema:
    def test_ref_schema(self) -> None:
        schema = {"$ref": "#/components/schemas/User"}
        result = resolve_schema(schema, OPENAPI_DOC)
        assert result["type"] == "object"

    def test_inline_schema(self) -> None:
        schema = {"type": "string"}
        result = resolve_schema(schema, OPENAPI_DOC)
        assert result == {"type": "string"}

    def test_no_openapi_doc(self) -> None:
        schema = {"$ref": "#/components/schemas/User"}
        result = resolve_schema(schema, None)
        assert result == schema


class TestDeepResolveRefs:
    def test_top_level_ref(self) -> None:
        schema = {"$ref": "#/components/schemas/User"}
        result = _deep_resolve_refs(schema, OPENAPI_DOC)
        assert result["type"] == "object"
        assert "id" in result["properties"]
        assert "name" in result["properties"]

    def test_nested_ref_in_properties(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "address": {"$ref": "#/components/schemas/Address"},
            },
        }
        result = _deep_resolve_refs(schema, OPENAPI_DOC)
        assert result["properties"]["address"]["type"] == "object"
        assert "street" in result["properties"]["address"]["properties"]

    def test_ref_in_allof(self) -> None:
        schema = {
            "allOf": [
                {"$ref": "#/components/schemas/User"},
                {"type": "object", "properties": {"extra": {"type": "boolean"}}},
            ]
        }
        result = _deep_resolve_refs(schema, OPENAPI_DOC)
        assert result["allOf"][0]["type"] == "object"
        assert "id" in result["allOf"][0]["properties"]
        assert result["allOf"][1]["properties"]["extra"]["type"] == "boolean"

    def test_ref_in_anyof(self) -> None:
        schema = {
            "anyOf": [
                {"$ref": "#/components/schemas/User"},
                {"$ref": "#/components/schemas/Address"},
            ]
        }
        result = _deep_resolve_refs(schema, OPENAPI_DOC)
        assert result["anyOf"][0]["type"] == "object"
        assert "name" in result["anyOf"][0]["properties"]
        assert "street" in result["anyOf"][1]["properties"]

    def test_ref_in_array_items(self) -> None:
        schema = {"type": "array", "items": {"$ref": "#/components/schemas/User"}}
        result = _deep_resolve_refs(schema, OPENAPI_DOC)
        assert result["items"]["type"] == "object"
        assert "id" in result["items"]["properties"]

    def test_deeply_nested_ref(self) -> None:
        result = _deep_resolve_refs({"$ref": "#/components/schemas/UserWithAddress"}, OPENAPI_DOC)
        assert result["properties"]["address"]["type"] == "object"
        assert "street" in result["properties"]["address"]["properties"]

    def test_circular_ref_depth_limit(self) -> None:
        result = _deep_resolve_refs({"$ref": "#/components/schemas/SelfRef"}, OPENAPI_DOC)
        # Should not raise — depth limit stops recursion.
        assert result["type"] == "object"
        assert "child" in result["properties"]

    def test_no_mutation_of_original(self) -> None:
        original_address = OPENAPI_DOC["components"]["schemas"]["Address"]
        original_props = dict(original_address.get("properties", {}))
        schema = {"$ref": "#/components/schemas/UserWithAddress"}
        _deep_resolve_refs(schema, OPENAPI_DOC)
        assert OPENAPI_DOC["components"]["schemas"]["Address"]["properties"] == original_props


class TestDeepResolveRefsPublic:
    """Tests for the public deep_resolve_refs wrapper."""

    def test_top_level_ref(self) -> None:
        schema = {"$ref": "#/components/schemas/User"}
        result = deep_resolve_refs(schema, OPENAPI_DOC)
        assert result["type"] == "object"
        assert "id" in result["properties"]
        assert "name" in result["properties"]

    def test_nested_ref_in_properties(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "address": {"$ref": "#/components/schemas/Address"},
            },
        }
        result = deep_resolve_refs(schema, OPENAPI_DOC)
        assert result["properties"]["address"]["type"] == "object"
        assert "street" in result["properties"]["address"]["properties"]

    def test_ref_in_allof(self) -> None:
        schema = {
            "allOf": [
                {"$ref": "#/components/schemas/User"},
                {"type": "object", "properties": {"extra": {"type": "boolean"}}},
            ]
        }
        result = deep_resolve_refs(schema, OPENAPI_DOC)
        assert result["allOf"][0]["type"] == "object"
        assert "id" in result["allOf"][0]["properties"]

    def test_ref_in_anyof(self) -> None:
        schema = {
            "anyOf": [
                {"$ref": "#/components/schemas/User"},
                {"$ref": "#/components/schemas/Address"},
            ]
        }
        result = deep_resolve_refs(schema, OPENAPI_DOC)
        assert "name" in result["anyOf"][0]["properties"]
        assert "street" in result["anyOf"][1]["properties"]

    def test_ref_in_oneof(self) -> None:
        schema = {
            "oneOf": [
                {"$ref": "#/components/schemas/User"},
                {"$ref": "#/components/schemas/Address"},
            ]
        }
        result = deep_resolve_refs(schema, OPENAPI_DOC)
        assert result["oneOf"][0]["type"] == "object"
        assert "name" in result["oneOf"][0]["properties"]
        assert "city" in result["oneOf"][1]["properties"]

    def test_ref_in_array_items(self) -> None:
        schema = {"type": "array", "items": {"$ref": "#/components/schemas/User"}}
        result = deep_resolve_refs(schema, OPENAPI_DOC)
        assert result["items"]["type"] == "object"
        assert "id" in result["items"]["properties"]

    def test_deeply_nested_ref(self) -> None:
        result = deep_resolve_refs({"$ref": "#/components/schemas/UserWithAddress"}, OPENAPI_DOC)
        assert result["properties"]["address"]["type"] == "object"
        assert "street" in result["properties"]["address"]["properties"]

    def test_circular_ref_depth_limit(self) -> None:
        result = deep_resolve_refs({"$ref": "#/components/schemas/SelfRef"}, OPENAPI_DOC)
        assert result["type"] == "object"
        assert "child" in result["properties"]

    def test_no_mutation_of_original(self) -> None:
        original_address = OPENAPI_DOC["components"]["schemas"]["Address"]
        original_props = dict(original_address.get("properties", {}))
        deep_resolve_refs({"$ref": "#/components/schemas/UserWithAddress"}, OPENAPI_DOC)
        assert OPENAPI_DOC["components"]["schemas"]["Address"]["properties"] == original_props

    def test_custom_depth_parameter(self) -> None:
        """Passing depth=17 should short-circuit immediately."""
        schema = {"$ref": "#/components/schemas/User"}
        result = deep_resolve_refs(schema, OPENAPI_DOC, depth=17)
        # Should return the unresolved schema since depth > 16
        assert "$ref" in result

    def test_plain_schema_no_refs(self) -> None:
        schema = {"type": "string", "description": "A simple string"}
        result = deep_resolve_refs(schema, OPENAPI_DOC)
        assert result == {"type": "string", "description": "A simple string"}

    def test_importable_from_package(self) -> None:
        from apcore_toolkit import deep_resolve_refs as public_fn

        assert callable(public_fn)

    def test_additional_properties_ref(self) -> None:
        schema = {
            "type": "object",
            "additionalProperties": {"$ref": "#/components/schemas/Address"},
        }
        result = deep_resolve_refs(schema, OPENAPI_DOC)
        assert result["additionalProperties"]["type"] == "object"
        assert "street" in result["additionalProperties"]["properties"]

    def test_pattern_properties_ref(self) -> None:
        schema = {
            "type": "object",
            "patternProperties": {
                "^S_": {"$ref": "#/components/schemas/Address"},
            },
        }
        result = deep_resolve_refs(schema, OPENAPI_DOC)
        assert result["patternProperties"]["^S_"]["type"] == "object"
        assert "city" in result["patternProperties"]["^S_"]["properties"]

    def test_not_ref(self) -> None:
        schema = {"not": {"$ref": "#/components/schemas/User"}}
        result = deep_resolve_refs(schema, OPENAPI_DOC)
        assert result["not"]["type"] == "object"
        assert "id" in result["not"]["properties"]

    def test_if_then_else_refs(self) -> None:
        schema = {
            "if": {"$ref": "#/components/schemas/User"},
            "then": {"$ref": "#/components/schemas/Address"},
            "else": {"type": "null"},
        }
        result = deep_resolve_refs(schema, OPENAPI_DOC)
        assert result["if"]["type"] == "object"
        assert "id" in result["if"]["properties"]
        assert result["then"]["type"] == "object"
        assert "street" in result["then"]["properties"]
        assert result["else"] == {"type": "null"}

    def test_prefix_items_refs(self) -> None:
        schema = {
            "type": "array",
            "prefixItems": [
                {"$ref": "#/components/schemas/User"},
                {"$ref": "#/components/schemas/Address"},
            ],
        }
        result = deep_resolve_refs(schema, OPENAPI_DOC)
        assert result["prefixItems"][0]["type"] == "object"
        assert "id" in result["prefixItems"][0]["properties"]
        assert "street" in result["prefixItems"][1]["properties"]

    def test_tuple_items_list_of_schemas(self) -> None:
        # Tuple-form items: items is a list (draft-4 tuple validation)
        schema = {
            "type": "array",
            "items": [
                {"$ref": "#/components/schemas/User"},
                {"$ref": "#/components/schemas/Address"},
            ],
        }
        result = deep_resolve_refs(schema, OPENAPI_DOC)
        assert isinstance(result["items"], list)
        assert result["items"][0]["type"] == "object"
        assert "id" in result["items"][0]["properties"]
        assert "street" in result["items"][1]["properties"]


class TestExtractInputSchema:
    def test_query_and_path_params(self) -> None:
        operation = {
            "parameters": [
                {"name": "user_id", "in": "path", "required": True, "schema": {"type": "integer"}},
                {"name": "q", "in": "query", "schema": {"type": "string"}},
            ]
        }
        result = extract_input_schema(operation)
        assert result["properties"]["user_id"] == {"type": "integer"}
        assert result["properties"]["q"] == {"type": "string"}
        assert "user_id" in result["required"]
        assert "q" not in result["required"]

    def test_request_body(self) -> None:
        operation = {
            "requestBody": {
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {"name": {"type": "string"}},
                            "required": ["name"],
                        }
                    }
                }
            }
        }
        result = extract_input_schema(operation)
        assert "name" in result["properties"]
        assert "name" in result["required"]

    def test_ref_in_request_body(self) -> None:
        operation = {
            "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/User"}}}}
        }
        result = extract_input_schema(operation, OPENAPI_DOC)
        assert "name" in result["properties"]

    def test_ref_in_param_schema(self) -> None:
        operation = {
            "parameters": [
                {"name": "user", "in": "query", "required": True, "schema": {"$ref": "#/components/schemas/User"}},
            ]
        }
        result = extract_input_schema(operation, OPENAPI_DOC)
        assert result["properties"]["user"]["type"] == "object"

    def test_nested_ref_in_body_properties(self) -> None:
        operation = {
            "requestBody": {
                "content": {"application/json": {"schema": {"$ref": "#/components/schemas/UserWithAddress"}}}
            }
        }
        result = extract_input_schema(operation, OPENAPI_DOC)
        assert result["properties"]["address"]["type"] == "object"
        assert "street" in result["properties"]["address"]["properties"]

    def test_empty_operation(self) -> None:
        result = extract_input_schema({})
        assert result["type"] == "object"
        assert result["properties"] == {}
        assert result["required"] == []

    def test_param_missing_name_is_skipped(self) -> None:
        # A malformed OpenAPI param dict without a "name" key must not raise KeyError;
        # it should be silently skipped so that valid params still appear in the schema.
        operation = {
            "parameters": [
                {"in": "query", "schema": {"type": "string"}},  # no "name"
                {"name": "valid_param", "in": "query", "schema": {"type": "integer"}},
            ]
        }
        result = extract_input_schema(operation)
        assert "valid_param" in result["properties"]
        assert len(result["properties"]) == 1


class TestExtractOutputSchema:
    def test_200_response(self) -> None:
        operation = {
            "responses": {
                "200": {
                    "content": {
                        "application/json": {"schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}}}
                    }
                }
            }
        }
        result = extract_output_schema(operation)
        assert result["properties"]["ok"]["type"] == "boolean"

    def test_201_response(self) -> None:
        operation = {
            "responses": {
                "201": {
                    "content": {
                        "application/json": {"schema": {"type": "object", "properties": {"id": {"type": "integer"}}}}
                    }
                }
            }
        }
        result = extract_output_schema(operation)
        assert "id" in result["properties"]

    def test_200_preferred_over_201(self) -> None:
        operation = {
            "responses": {
                "200": {"content": {"application/json": {"schema": {"type": "string"}}}},
                "201": {"content": {"application/json": {"schema": {"type": "integer"}}}},
            }
        }
        result = extract_output_schema(operation)
        assert result["type"] == "string"

    def test_ref_in_response(self) -> None:
        operation = {
            "responses": {"200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/User"}}}}}
        }
        result = extract_output_schema(operation, OPENAPI_DOC)
        assert result["type"] == "object"

    def test_array_with_ref_items(self) -> None:
        operation = {
            "responses": {
                "200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/UserList"}}}}
            }
        }
        result = extract_output_schema(operation, OPENAPI_DOC)
        assert result["type"] == "array"
        assert result["items"]["type"] == "object"

    def test_no_matching_response(self) -> None:
        operation = {"responses": {"404": {}}}
        result = extract_output_schema(operation)
        assert result == {"type": "object", "properties": {}}

    def test_nested_ref_in_response_properties(self) -> None:
        operation = {
            "responses": {
                "200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/UserWithAddress"}}}}
            }
        }
        result = extract_output_schema(operation, OPENAPI_DOC)
        assert result["type"] == "object"
        assert result["properties"]["address"]["type"] == "object"
        assert "street" in result["properties"]["address"]["properties"]

    def test_allof_composition_in_response(self) -> None:
        operation = {
            "responses": {
                "200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/AdminUser"}}}}
            }
        }
        result = extract_output_schema(operation, OPENAPI_DOC)
        assert result["allOf"][0]["type"] == "object"
        assert "id" in result["allOf"][0]["properties"]
        assert result["allOf"][1]["properties"]["role"]["type"] == "string"

    def test_empty_responses(self) -> None:
        result = extract_output_schema({})
        assert result == {"type": "object", "properties": {}}

    def test_204_response_with_schema_is_extracted(self) -> None:
        """D11-001: any 2xx status code with a schema body should be extracted."""
        operation = {
            "responses": {
                "204": {
                    "content": {
                        "application/json": {"schema": {"type": "object", "properties": {"status": {"type": "string"}}}}
                    }
                }
            }
        }
        result = extract_output_schema(operation)
        assert result != {"type": "object", "properties": {}}
        assert "status" in result["properties"]

    def test_arbitrary_2xx_response_is_extracted(self) -> None:
        """D11-001: non-standard 2xx codes like 206/299 should be accepted."""
        operation = {
            "responses": {
                "206": {
                    "content": {
                        "application/json": {
                            "schema": {"type": "object", "properties": {"partial": {"type": "boolean"}}}
                        }
                    }
                }
            }
        }
        result = extract_output_schema(operation)
        assert "partial" in result["properties"]

    def test_2xx_priority_order_lowest_wins(self) -> None:
        """D11-001: when multiple 2xx codes exist, lowest (200 < 201 < 204) wins."""
        operation = {
            "responses": {
                "204": {"content": {"application/json": {"schema": {"type": "string"}}}},
                "200": {"content": {"application/json": {"schema": {"type": "integer"}}}},
            }
        }
        result = extract_output_schema(operation)
        # 200 should win over 204
        assert result["type"] == "integer"
