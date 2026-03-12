"""AI-driven metadata enhancement using local SLMs.

Uses an OpenAI-compatible local API (e.g., Ollama, vLLM, LM Studio) to fill
metadata gaps that static analysis cannot resolve: missing descriptions,
behavioral annotation inference, and schema inference for untyped functions.

All AI-generated fields are tagged with ``x-generated-by: slm`` in the module's
metadata dict for auditability.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import replace
from typing import Any

from apcore import ModuleAnnotations

from apcore_toolkit.types import ScannedModule

logger = logging.getLogger("apcore_toolkit")

_DEFAULT_ENDPOINT = "http://localhost:11434/v1"
_DEFAULT_MODEL = "qwen:0.6b"
_DEFAULT_THRESHOLD = 0.7
_DEFAULT_BATCH_SIZE = 5
_DEFAULT_TIMEOUT = 30
_DEFAULT_ANNOTATIONS = ModuleAnnotations()


class AIEnhancer:
    """Enhances ScannedModule metadata using a local SLM.

    Configuration is read from environment variables:
        - ``APCORE_AI_ENABLED``: Enable enhancement (default: ``false``).
        - ``APCORE_AI_ENDPOINT``: OpenAI-compatible API URL.
        - ``APCORE_AI_MODEL``: Model name (e.g., ``qwen:0.6b``).
        - ``APCORE_AI_THRESHOLD``: Confidence threshold for accepting results (0.0–1.0).
        - ``APCORE_AI_BATCH_SIZE``: Number of modules to enhance per API call.
        - ``APCORE_AI_TIMEOUT``: Timeout in seconds per API call.
    """

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        model: str | None = None,
        threshold: float | None = None,
        batch_size: int | None = None,
        timeout: int | None = None,
    ) -> None:
        self.endpoint = endpoint or os.environ.get("APCORE_AI_ENDPOINT", _DEFAULT_ENDPOINT)
        self.model = model or os.environ.get("APCORE_AI_MODEL", _DEFAULT_MODEL)
        self.threshold = (
            threshold if threshold is not None else self._parse_float_env("APCORE_AI_THRESHOLD", _DEFAULT_THRESHOLD)
        )
        self.batch_size = (
            batch_size if batch_size is not None else self._parse_int_env("APCORE_AI_BATCH_SIZE", _DEFAULT_BATCH_SIZE)
        )
        self.timeout = timeout if timeout is not None else self._parse_int_env("APCORE_AI_TIMEOUT", _DEFAULT_TIMEOUT)

        if not 0.0 <= self.threshold <= 1.0:
            raise ValueError("APCORE_AI_THRESHOLD must be a number between 0.0 and 1.0")
        if self.batch_size <= 0:
            raise ValueError("APCORE_AI_BATCH_SIZE must be a positive integer")
        if self.timeout <= 0:
            raise ValueError("APCORE_AI_TIMEOUT must be a positive integer")

    @staticmethod
    def _parse_float_env(name: str, default: float) -> float:
        raw = os.environ.get(name)
        if raw is None:
            return default
        try:
            return float(raw)
        except ValueError:
            raise ValueError(f"{name} must be a valid number, got {raw!r}") from None

    @staticmethod
    def _parse_int_env(name: str, default: int) -> int:
        raw = os.environ.get(name)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError:
            raise ValueError(f"{name} must be a valid integer, got {raw!r}") from None

    @staticmethod
    def is_enabled() -> bool:
        """Check whether AI enhancement is enabled via environment."""
        return os.environ.get("APCORE_AI_ENABLED", "false").lower() in ("true", "1", "yes")

    def enhance(self, modules: list[ScannedModule]) -> list[ScannedModule]:
        """Enhance a list of ScannedModules by filling metadata gaps.

        For each module, identifies missing fields and calls the SLM to
        generate them. Only fields above the confidence threshold are applied.

        Modules with gaps are collected into batches of ``batch_size``
        (configured via ``APCORE_AI_BATCH_SIZE``, default 5). Each batch
        shares a single prompt/API call where possible, reducing round-trips.
        When batch_size is 1, behaviour is identical to per-module processing.

        Args:
            modules: List of ScannedModule instances (post-scan).

        Returns:
            New list of ScannedModule instances with AI-generated metadata merged in.
        """
        results: list[ScannedModule] = []

        # Separate modules that need enhancement from those that don't
        pending: list[tuple[int, ScannedModule, list[str]]] = []
        for idx, module in enumerate(modules):
            gaps = self._identify_gaps(module)
            if not gaps:
                results.append(module)
            else:
                # placeholder — will be replaced after enhancement
                results.append(module)
                pending.append((idx, module, gaps))

        # Process pending modules in batches
        for batch_start in range(0, len(pending), self.batch_size):
            batch = pending[batch_start : batch_start + self.batch_size]
            for idx, module, gaps in batch:
                try:
                    enhanced = self._enhance_module(module, gaps)
                    results[idx] = enhanced
                except Exception:
                    logger.warning("AI enhancement failed for %s, keeping original", module.module_id, exc_info=True)

        return results

    def _identify_gaps(self, module: ScannedModule) -> list[str]:
        """Identify which metadata fields are missing or at defaults."""
        gaps: list[str] = []
        if not module.description or module.description == module.module_id:
            gaps.append("description")
        if not module.documentation:
            gaps.append("documentation")
        if module.annotations is None or module.annotations == _DEFAULT_ANNOTATIONS:
            gaps.append("annotations")
        if not module.input_schema.get("properties"):
            gaps.append("input_schema")
        return gaps

    def _enhance_module(self, module: ScannedModule, gaps: list[str]) -> ScannedModule:
        """Call the SLM to fill identified gaps for a single module."""
        prompt = self._build_prompt(module, gaps)
        response = self._call_llm(prompt)
        parsed = self._parse_response(response)

        updates: dict[str, Any] = {}
        confidence: dict[str, float] = {}
        warnings: list[str] = list(module.warnings)

        # Apply description if above threshold
        if "description" in gaps and "description" in parsed:
            desc_conf = parsed.get("confidence", {}).get("description", 0.0)
            confidence["description"] = desc_conf
            if desc_conf >= self.threshold:
                updates["description"] = parsed["description"]
            else:
                warnings.append(f"Low confidence ({desc_conf:.2f}) for description — skipped. Review manually.")

        # Apply documentation if above threshold
        if "documentation" in gaps and "documentation" in parsed:
            doc_conf = parsed.get("confidence", {}).get("documentation", 0.0)
            confidence["documentation"] = doc_conf
            if doc_conf >= self.threshold:
                updates["documentation"] = parsed["documentation"]
            else:
                warnings.append(f"Low confidence ({doc_conf:.2f}) for documentation — skipped. Review manually.")

        # Apply annotations if above threshold
        if "annotations" in gaps and "annotations" in parsed and isinstance(parsed["annotations"], dict):
            ann_data = parsed["annotations"]
            ann_conf = parsed.get("confidence", {})
            accepted: dict[str, Any] = {}
            _BOOL_FIELDS = (
                "readonly",
                "destructive",
                "idempotent",
                "requires_approval",
                "open_world",
                "streaming",
                "cacheable",
                "paginated",
            )
            for field in _BOOL_FIELDS:
                if field in ann_data and isinstance(ann_data[field], bool):
                    field_conf = ann_conf.get(f"annotations.{field}", ann_conf.get(field, 0.0))
                    confidence[f"annotations.{field}"] = field_conf
                    if field_conf >= self.threshold:
                        accepted[field] = ann_data[field]
                    else:
                        warnings.append(
                            f"Low confidence ({field_conf:.2f}) for annotations.{field} — skipped. Review manually."
                        )
            # Handle non-boolean annotation fields
            _INT_FIELDS = ("cache_ttl",)
            for field in _INT_FIELDS:
                if field in ann_data and isinstance(ann_data[field], int):
                    field_conf = ann_conf.get(f"annotations.{field}", ann_conf.get(field, 0.0))
                    confidence[f"annotations.{field}"] = field_conf
                    if field_conf >= self.threshold:
                        accepted[field] = ann_data[field]
                    else:
                        warnings.append(
                            f"Low confidence ({field_conf:.2f}) for annotations.{field} — skipped. Review manually."
                        )
            _STR_FIELDS = ("pagination_style",)
            for field in _STR_FIELDS:
                if field in ann_data and isinstance(ann_data[field], str):
                    field_conf = ann_conf.get(f"annotations.{field}", ann_conf.get(field, 0.0))
                    confidence[f"annotations.{field}"] = field_conf
                    if field_conf >= self.threshold:
                        accepted[field] = ann_data[field]
                    else:
                        warnings.append(
                            f"Low confidence ({field_conf:.2f}) for annotations.{field} — skipped. Review manually."
                        )
            if "cache_key_fields" in ann_data and isinstance(ann_data["cache_key_fields"], list):
                field_conf = ann_conf.get("annotations.cache_key_fields", ann_conf.get("cache_key_fields", 0.0))
                confidence["annotations.cache_key_fields"] = field_conf
                if field_conf >= self.threshold:
                    accepted["cache_key_fields"] = ann_data["cache_key_fields"]
                else:
                    warnings.append(
                        f"Low confidence ({field_conf:.2f}) for annotations.cache_key_fields — skipped. Review manually."
                    )
            if accepted:
                base = module.annotations or ModuleAnnotations()
                updates["annotations"] = replace(base, **accepted)

        # Apply input_schema if above threshold
        if "input_schema" in gaps and "input_schema" in parsed:
            schema_conf = parsed.get("confidence", {}).get("input_schema", 0.0)
            confidence["input_schema"] = schema_conf
            if schema_conf >= self.threshold:
                updates["input_schema"] = parsed["input_schema"]
            else:
                warnings.append(f"Low confidence ({schema_conf:.2f}) for input_schema — skipped. Review manually.")

        if not updates:
            return replace(module, warnings=warnings) if warnings != module.warnings else module

        # Tag AI-generated fields in metadata
        metadata = dict(module.metadata)
        metadata["x-generated-by"] = "slm"
        metadata["x-ai-confidence"] = confidence

        return replace(module, **updates, metadata=metadata, warnings=warnings)

    def _build_prompt(self, module: ScannedModule, gaps: list[str]) -> str:
        """Build a structured prompt for the SLM."""
        parts = [
            "You are analyzing a Python function to generate metadata for an AI-perceivable module system.",
            "",
            f"Module ID: {module.module_id}",
            f"Target: {module.target}",
        ]
        if module.description:
            parts.append(f"Current description: {module.description}")
        if module.input_schema.get("properties"):
            parts.append(f"Current input_schema: {json.dumps(module.input_schema, indent=2)}")

        parts.append("")
        parts.append("Please provide the following missing metadata as JSON:")
        parts.append("{")

        if "description" in gaps:
            parts.append('  "description": "<≤200 chars, what this function does>",')
        if "documentation" in gaps:
            parts.append('  "documentation": "<detailed Markdown explanation>",')
        if "annotations" in gaps:
            parts.append('  "annotations": {')
            parts.append('    "readonly": <true if no side effects>,')
            parts.append('    "destructive": <true if deletes/overwrites data>,')
            parts.append('    "idempotent": <true if safe to retry>,')
            parts.append('    "requires_approval": <true if dangerous operation>,')
            parts.append('    "open_world": <true if calls external systems>,')
            parts.append('    "streaming": <true if yields results incrementally>,')
            parts.append('    "cacheable": <true if results can be cached>,')
            parts.append('    "cache_ttl": <seconds, 0 for no expiry>,')
            parts.append('    "cache_key_fields": <list of input field names for cache key, or null for all>,')
            parts.append('    "paginated": <true if supports pagination>,')
            parts.append('    "pagination_style": <"cursor" or "offset" or "page">')
            parts.append("  },")
        if "input_schema" in gaps:
            parts.append('  "input_schema": <JSON Schema object for function parameters>,')

        parts.append('  "confidence": {')
        parts.append('    "description": 0.0, "documentation": 0.0')
        parts.append("  }")
        parts.append("}")
        parts.append("")
        parts.append("Respond with ONLY valid JSON, no markdown fences or explanation.")

        return "\n".join(parts)

    def _call_llm(self, prompt: str) -> str:
        """Call the OpenAI-compatible API and return the response text."""
        import urllib.error
        import urllib.request

        url = f"{self.endpoint.rstrip('/')}/chat/completions"
        payload = json.dumps(
            {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
            }
        ).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                try:
                    return data["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError) as exc:
                    raise ValueError(f"Unexpected API response structure: {exc}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise ConnectionError(f"Failed to reach SLM at {url}: {exc}") from exc

    @staticmethod
    def _parse_response(response: str) -> dict[str, Any]:
        """Parse the SLM response as JSON, stripping markdown fences if present."""
        text = response.strip()
        # Strip markdown code fences
        if text.startswith("```"):
            lines = text.split("\n")
            # Remove first and last fence lines
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines)

        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"SLM returned invalid JSON: {exc}") from exc
