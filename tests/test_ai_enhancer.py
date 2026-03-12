"""Tests for apcore_toolkit.ai_enhancer — AIEnhancer."""

from __future__ import annotations

import json
import os
import unittest.mock
from unittest.mock import patch

import pytest
from apcore import ModuleAnnotations

from apcore_toolkit.ai_enhancer import AIEnhancer
from apcore_toolkit.types import ScannedModule


@pytest.fixture
def enhancer() -> AIEnhancer:
    return AIEnhancer(endpoint="http://localhost:11434/v1", model="test-model", threshold=0.7, timeout=10)


@pytest.fixture
def sparse_module() -> ScannedModule:
    """A module with missing metadata (gaps to fill)."""
    return ScannedModule(
        module_id="legacy.handler",
        description="",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        tags=["legacy"],
        target="legacy.views:handler",
    )


@pytest.fixture
def complete_module() -> ScannedModule:
    """A module with all metadata filled (no gaps)."""
    return ScannedModule(
        module_id="users.get_user",
        description="Get a user by ID",
        input_schema={"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
        output_schema={"type": "object", "properties": {"name": {"type": "string"}}},
        tags=["users"],
        target="myapp.views:get_user",
        documentation="Returns a user object given an ID.",
        annotations=ModuleAnnotations(readonly=True),
    )


class TestIsEnabled:
    def test_disabled_by_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            assert AIEnhancer.is_enabled() is False

    def test_enabled_true(self) -> None:
        with patch.dict(os.environ, {"APCORE_AI_ENABLED": "true"}):
            assert AIEnhancer.is_enabled() is True

    def test_enabled_1(self) -> None:
        with patch.dict(os.environ, {"APCORE_AI_ENABLED": "1"}):
            assert AIEnhancer.is_enabled() is True

    def test_enabled_yes(self) -> None:
        with patch.dict(os.environ, {"APCORE_AI_ENABLED": "yes"}):
            assert AIEnhancer.is_enabled() is True

    def test_disabled_false(self) -> None:
        with patch.dict(os.environ, {"APCORE_AI_ENABLED": "false"}):
            assert AIEnhancer.is_enabled() is False


class TestIdentifyGaps:
    def test_no_gaps_for_complete_module(self, enhancer: AIEnhancer, complete_module: ScannedModule) -> None:
        gaps = enhancer._identify_gaps(complete_module)
        assert gaps == []

    def test_empty_description(self, enhancer: AIEnhancer, sparse_module: ScannedModule) -> None:
        gaps = enhancer._identify_gaps(sparse_module)
        assert "description" in gaps

    def test_description_equals_module_id(self, enhancer: AIEnhancer) -> None:
        module = ScannedModule(
            module_id="test.func",
            description="test.func",
            input_schema={"type": "object", "properties": {"x": {"type": "integer"}}},
            output_schema={},
            tags=[],
            target="m:f",
            documentation="Some docs",
            annotations=ModuleAnnotations(readonly=True),
        )
        gaps = enhancer._identify_gaps(module)
        assert "description" in gaps

    def test_missing_documentation(self, enhancer: AIEnhancer, sparse_module: ScannedModule) -> None:
        gaps = enhancer._identify_gaps(sparse_module)
        assert "documentation" in gaps

    def test_default_annotations(self, enhancer: AIEnhancer, sparse_module: ScannedModule) -> None:
        gaps = enhancer._identify_gaps(sparse_module)
        assert "annotations" in gaps

    def test_empty_input_schema(self, enhancer: AIEnhancer, sparse_module: ScannedModule) -> None:
        gaps = enhancer._identify_gaps(sparse_module)
        assert "input_schema" in gaps


class TestParseResponse:
    def test_valid_json(self) -> None:
        response = '{"description": "Hello", "confidence": {"description": 0.9}}'
        result = AIEnhancer._parse_response(response)
        assert result["description"] == "Hello"

    def test_json_with_markdown_fences(self) -> None:
        response = '```json\n{"description": "Hello"}\n```'
        result = AIEnhancer._parse_response(response)
        assert result["description"] == "Hello"

    def test_invalid_json(self) -> None:
        with pytest.raises(ValueError, match="SLM returned invalid JSON"):
            AIEnhancer._parse_response("not json at all")


class TestBuildPrompt:
    def test_prompt_contains_module_id(self, enhancer: AIEnhancer, sparse_module: ScannedModule) -> None:
        prompt = enhancer._build_prompt(sparse_module, ["description"])
        assert "legacy.handler" in prompt

    def test_prompt_requests_description(self, enhancer: AIEnhancer, sparse_module: ScannedModule) -> None:
        prompt = enhancer._build_prompt(sparse_module, ["description"])
        assert '"description"' in prompt

    def test_prompt_requests_annotations(self, enhancer: AIEnhancer, sparse_module: ScannedModule) -> None:
        prompt = enhancer._build_prompt(sparse_module, ["annotations"])
        assert '"readonly"' in prompt
        assert '"destructive"' in prompt

    def test_prompt_requests_input_schema(self, enhancer: AIEnhancer, sparse_module: ScannedModule) -> None:
        prompt = enhancer._build_prompt(sparse_module, ["input_schema"])
        assert '"input_schema"' in prompt


class TestEnhanceModule:
    def test_applies_description_above_threshold(self, enhancer: AIEnhancer, sparse_module: ScannedModule) -> None:
        llm_response = json.dumps(
            {
                "description": "Handle legacy requests",
                "confidence": {"description": 0.92},
            }
        )
        with patch.object(enhancer, "_call_llm", return_value=llm_response):
            result = enhancer._enhance_module(sparse_module, ["description"])

        assert result.description == "Handle legacy requests"
        assert result.metadata["x-generated-by"] == "slm"
        assert result.metadata["x-ai-confidence"]["description"] == 0.92

    def test_skips_description_below_threshold(self, enhancer: AIEnhancer, sparse_module: ScannedModule) -> None:
        llm_response = json.dumps(
            {
                "description": "Maybe this?",
                "confidence": {"description": 0.3},
            }
        )
        with patch.object(enhancer, "_call_llm", return_value=llm_response):
            result = enhancer._enhance_module(sparse_module, ["description"])

        assert result.description == ""  # unchanged
        assert any("Low confidence" in w for w in result.warnings)

    def test_applies_documentation_above_threshold(self, enhancer: AIEnhancer, sparse_module: ScannedModule) -> None:
        llm_response = json.dumps(
            {
                "documentation": "Detailed docs for legacy handler.\n\nHandles legacy HTTP requests.",
                "confidence": {"documentation": 0.85},
            }
        )
        with patch.object(enhancer, "_call_llm", return_value=llm_response):
            result = enhancer._enhance_module(sparse_module, ["documentation"])

        assert result.documentation == "Detailed docs for legacy handler.\n\nHandles legacy HTTP requests."
        assert result.metadata["x-generated-by"] == "slm"

    def test_skips_documentation_below_threshold(self, enhancer: AIEnhancer, sparse_module: ScannedModule) -> None:
        llm_response = json.dumps(
            {
                "documentation": "Maybe some docs?",
                "confidence": {"documentation": 0.2},
            }
        )
        with patch.object(enhancer, "_call_llm", return_value=llm_response):
            result = enhancer._enhance_module(sparse_module, ["documentation"])

        assert result.documentation is None or result.documentation == ""
        assert any("Low confidence" in w and "documentation" in w for w in result.warnings)

    def test_applies_annotations_selectively(self, enhancer: AIEnhancer, sparse_module: ScannedModule) -> None:
        llm_response = json.dumps(
            {
                "annotations": {
                    "readonly": True,
                    "destructive": False,
                    "open_world": True,
                },
                "confidence": {
                    "annotations.readonly": 0.85,
                    "annotations.destructive": 0.40,  # below threshold
                    "annotations.open_world": 0.90,
                },
            }
        )
        with patch.object(enhancer, "_call_llm", return_value=llm_response):
            result = enhancer._enhance_module(sparse_module, ["annotations"])

        assert result.annotations is not None
        assert result.annotations.readonly is True
        assert result.annotations.open_world is True
        # destructive was below threshold, should remain at default (False)
        assert result.annotations.destructive is False
        assert any("annotations.destructive" in w for w in result.warnings)

    def test_applies_input_schema(self, enhancer: AIEnhancer, sparse_module: ScannedModule) -> None:
        new_schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}, "count": {"type": "integer"}},
            "required": ["name"],
        }
        llm_response = json.dumps(
            {
                "input_schema": new_schema,
                "confidence": {"input_schema": 0.88},
            }
        )
        with patch.object(enhancer, "_call_llm", return_value=llm_response):
            result = enhancer._enhance_module(sparse_module, ["input_schema"])

        assert result.input_schema == new_schema

    def test_ignores_non_dict_annotations(self, enhancer: AIEnhancer, sparse_module: ScannedModule) -> None:
        llm_response = json.dumps(
            {
                "annotations": "readonly",  # string instead of dict
                "confidence": {},
            }
        )
        with patch.object(enhancer, "_call_llm", return_value=llm_response):
            result = enhancer._enhance_module(sparse_module, ["annotations"])

        # Should not crash, annotations unchanged
        assert result.annotations is None or result.annotations == sparse_module.annotations

    def test_ignores_non_bool_annotation_values(self, enhancer: AIEnhancer, sparse_module: ScannedModule) -> None:
        """Non-boolean annotation values from LLM should be silently skipped."""
        llm_response = json.dumps(
            {
                "annotations": {
                    "readonly": "true",  # string, not bool
                    "destructive": 1,  # int, not bool
                    "idempotent": True,  # valid bool
                },
                "confidence": {
                    "annotations.readonly": 0.9,
                    "annotations.destructive": 0.9,
                    "annotations.idempotent": 0.9,
                },
            }
        )
        with patch.object(enhancer, "_call_llm", return_value=llm_response):
            result = enhancer._enhance_module(sparse_module, ["annotations"])

        assert result.annotations is not None
        assert result.annotations.idempotent is True
        # Non-bool values should not have been accepted
        assert result.annotations.readonly is False  # default
        assert result.annotations.destructive is False  # default

    def test_no_updates_returns_original(self, enhancer: AIEnhancer, sparse_module: ScannedModule) -> None:
        llm_response = json.dumps({"confidence": {}})
        with patch.object(enhancer, "_call_llm", return_value=llm_response):
            result = enhancer._enhance_module(sparse_module, ["description"])

        assert result.description == sparse_module.description

    def test_low_confidence_warnings_for_nonbool_fields(
        self, enhancer: AIEnhancer, sparse_module: ScannedModule
    ) -> None:
        """Non-bool annotation fields (cache_ttl, pagination_style, cache_key_fields) emit warnings when below threshold."""
        llm_response = json.dumps(
            {
                "annotations": {
                    "cache_ttl": 300,
                    "pagination_style": "cursor",
                    "cache_key_fields": ["id", "page"],
                },
                "confidence": {
                    "annotations.cache_ttl": 0.3,
                    "annotations.pagination_style": 0.2,
                    "annotations.cache_key_fields": 0.1,
                },
            }
        )
        with patch.object(enhancer, "_call_llm", return_value=llm_response):
            result = enhancer._enhance_module(sparse_module, ["annotations"])

        assert any("cache_ttl" in w and "Low confidence" in w for w in result.warnings)
        assert any("pagination_style" in w and "Low confidence" in w for w in result.warnings)
        assert any("cache_key_fields" in w and "Low confidence" in w for w in result.warnings)


class TestEnhance:
    def test_skips_complete_modules(self, enhancer: AIEnhancer, complete_module: ScannedModule) -> None:
        result = enhancer.enhance([complete_module])
        assert len(result) == 1
        assert result[0] is complete_module  # no copy needed

    def test_enhances_sparse_modules(self, enhancer: AIEnhancer, sparse_module: ScannedModule) -> None:
        llm_response = json.dumps(
            {
                "description": "Handle legacy requests",
                "confidence": {"description": 0.92},
            }
        )
        with patch.object(enhancer, "_call_llm", return_value=llm_response):
            result = enhancer.enhance([sparse_module])

        assert len(result) == 1
        assert result[0].description == "Handle legacy requests"

    def test_handles_llm_failure_gracefully(self, enhancer: AIEnhancer, sparse_module: ScannedModule) -> None:
        with patch.object(enhancer, "_call_llm", side_effect=ConnectionError("Offline")):
            result = enhancer.enhance([sparse_module])

        assert len(result) == 1
        assert result[0] is sparse_module  # original returned on failure

    def test_mixed_modules(
        self, enhancer: AIEnhancer, complete_module: ScannedModule, sparse_module: ScannedModule
    ) -> None:
        llm_response = json.dumps(
            {
                "description": "Enhanced desc",
                "confidence": {"description": 0.95},
            }
        )
        with patch.object(enhancer, "_call_llm", return_value=llm_response):
            result = enhancer.enhance([complete_module, sparse_module])

        assert len(result) == 2
        assert result[0] is complete_module  # untouched
        assert result[1].description == "Enhanced desc"


class TestCallLLM:
    def test_successful_call(self, enhancer: AIEnhancer) -> None:
        """_call_llm should return the content from a valid API response."""
        response_body = json.dumps({"choices": [{"message": {"content": '{"description": "test"}'}}]}).encode("utf-8")

        mock_resp = unittest.mock.MagicMock()
        mock_resp.read.return_value = response_body
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = unittest.mock.MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = enhancer._call_llm("test prompt")

        assert result == '{"description": "test"}'

    def test_url_error_raises_connection_error(self, enhancer: AIEnhancer) -> None:
        """_call_llm should wrap URLError into ConnectionError."""
        import urllib.error

        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")):
            with pytest.raises(ConnectionError, match="Failed to reach SLM"):
                enhancer._call_llm("test prompt")

    def test_timeout_error_raises_connection_error(self, enhancer: AIEnhancer) -> None:
        """_call_llm should wrap TimeoutError into ConnectionError."""
        with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            with pytest.raises(ConnectionError, match="Failed to reach SLM"):
                enhancer._call_llm("test prompt")

    def test_malformed_response_raises_value_error(self, enhancer: AIEnhancer) -> None:
        """_call_llm should raise ValueError for unexpected API response structure."""
        response_body = json.dumps({"result": "no choices key"}).encode("utf-8")

        mock_resp = unittest.mock.MagicMock()
        mock_resp.read.return_value = response_body
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = unittest.mock.MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            with pytest.raises(ValueError, match="Unexpected API response structure"):
                enhancer._call_llm("test prompt")

    def test_request_payload_structure(self, enhancer: AIEnhancer) -> None:
        """_call_llm should send correct payload and headers."""
        response_body = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode("utf-8")

        mock_resp = unittest.mock.MagicMock()
        mock_resp.read.return_value = response_body
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = unittest.mock.MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            enhancer._call_llm("hello world")

        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "http://localhost:11434/v1/chat/completions"
        assert req.get_header("Content-type") == "application/json"
        payload = json.loads(req.data.decode("utf-8"))
        assert payload["model"] == "test-model"
        assert payload["messages"][0]["content"] == "hello world"


class TestConfiguration:
    def test_defaults_from_env(self) -> None:
        with patch.dict(
            os.environ,
            {
                "APCORE_AI_ENDPOINT": "http://custom:8080/v1",
                "APCORE_AI_MODEL": "custom-model",
                "APCORE_AI_THRESHOLD": "0.5",
                "APCORE_AI_TIMEOUT": "60",
            },
        ):
            e = AIEnhancer()
            assert e.endpoint == "http://custom:8080/v1"
            assert e.model == "custom-model"
            assert e.threshold == 0.5
            assert e.timeout == 60

    def test_constructor_overrides_env(self) -> None:
        with patch.dict(os.environ, {"APCORE_AI_ENDPOINT": "http://env:8080/v1"}):
            e = AIEnhancer(endpoint="http://override:9090/v1")
            assert e.endpoint == "http://override:9090/v1"

    def test_invalid_threshold_env_raises_value_error(self) -> None:
        with patch.dict(os.environ, {"APCORE_AI_THRESHOLD": "abc"}, clear=False):
            with pytest.raises(ValueError, match="APCORE_AI_THRESHOLD"):
                AIEnhancer()

    def test_invalid_timeout_env_raises_value_error(self) -> None:
        with patch.dict(os.environ, {"APCORE_AI_TIMEOUT": "not_a_number"}, clear=False):
            with pytest.raises(ValueError, match="APCORE_AI_TIMEOUT"):
                AIEnhancer()

    def test_threshold_out_of_range_raises_value_error(self) -> None:
        with patch.dict(os.environ, {"APCORE_AI_THRESHOLD": "1.5"}, clear=False):
            with pytest.raises(ValueError, match="APCORE_AI_THRESHOLD"):
                AIEnhancer()

    def test_negative_threshold_raises_value_error(self) -> None:
        with patch.dict(os.environ, {"APCORE_AI_THRESHOLD": "-0.1"}, clear=False):
            with pytest.raises(ValueError, match="APCORE_AI_THRESHOLD"):
                AIEnhancer()

    def test_negative_timeout_raises_value_error(self) -> None:
        with patch.dict(os.environ, {"APCORE_AI_TIMEOUT": "-5"}, clear=False):
            with pytest.raises(ValueError, match="APCORE_AI_TIMEOUT"):
                AIEnhancer()

    def test_zero_timeout_raises_value_error(self) -> None:
        with patch.dict(os.environ, {"APCORE_AI_TIMEOUT": "0"}, clear=False):
            with pytest.raises(ValueError, match="APCORE_AI_TIMEOUT"):
                AIEnhancer()

    def test_threshold_kwarg_out_of_range_raises(self) -> None:
        with pytest.raises(ValueError, match="APCORE_AI_THRESHOLD"):
            AIEnhancer(threshold=2.0)

    def test_timeout_kwarg_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="APCORE_AI_TIMEOUT"):
            AIEnhancer(timeout=0)

    def test_batch_size_from_env(self) -> None:
        with patch.dict(os.environ, {"APCORE_AI_BATCH_SIZE": "10"}):
            e = AIEnhancer()
            assert e.batch_size == 10

    def test_batch_size_kwarg_overrides_env(self) -> None:
        with patch.dict(os.environ, {"APCORE_AI_BATCH_SIZE": "10"}):
            e = AIEnhancer(batch_size=3)
            assert e.batch_size == 3

    def test_batch_size_default(self) -> None:
        e = AIEnhancer()
        assert e.batch_size == 5

    def test_invalid_batch_size_env_raises(self) -> None:
        with patch.dict(os.environ, {"APCORE_AI_BATCH_SIZE": "abc"}):
            with pytest.raises(ValueError, match="APCORE_AI_BATCH_SIZE"):
                AIEnhancer()

    def test_zero_batch_size_raises(self) -> None:
        with pytest.raises(ValueError, match="APCORE_AI_BATCH_SIZE"):
            AIEnhancer(batch_size=0)

    def test_negative_batch_size_raises(self) -> None:
        with pytest.raises(ValueError, match="APCORE_AI_BATCH_SIZE"):
            AIEnhancer(batch_size=-1)
