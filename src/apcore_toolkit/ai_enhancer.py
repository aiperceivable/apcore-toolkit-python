"""AI-driven metadata enhancement using local SLMs.

Uses an OpenAI-compatible local API (e.g., Ollama, vLLM, LM Studio) to fill
metadata gaps that static analysis cannot resolve: missing descriptions,
behavioral annotation inference, and schema inference for untyped functions.

All AI-generated fields are tagged with ``x-generated-by: slm`` in the module's
metadata dict for auditability.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import types
import typing
from dataclasses import replace
from typing import Any, Callable, Protocol

from apcore import DEFAULT_ANNOTATIONS, ModuleAnnotations

from apcore_toolkit.types import ScannedModule

logger = logging.getLogger("apcore_toolkit")

_DEFAULT_ENDPOINT = "http://localhost:11434/v1"
_DEFAULT_MODEL = "qwen:0.6b"
_DEFAULT_THRESHOLD = 0.7
_DEFAULT_BATCH_SIZE = 5
_DEFAULT_TIMEOUT = 30


# SLM must never write to ModuleAnnotations.extra: it is reserved for adapter
# extensions and is not user-facing semantic content.
_SLM_EXCLUDED_ANNOTATION_FIELDS = frozenset({"extra"})


def _build_annotation_field_validators() -> dict[str, Callable[[Any], bool]]:
    """Derive per-field type validators from ``ModuleAnnotations`` at import time.

    Introspecting the dataclass instead of hardcoding a whitelist ensures that
    when apcore adds new annotation fields the AI Enhancer automatically picks
    them up (still subject to confidence gating). ``extra`` is excluded so the
    SLM cannot inject arbitrary keys via that escape hatch.

    The order of ``isinstance`` checks matters: ``bool`` is a subclass of
    ``int`` in Python, so a boolean must not be silently accepted as an int
    field.
    """
    hints = typing.get_type_hints(ModuleAnnotations)
    validators: dict[str, Callable[[Any], bool]] = {}
    for field in dataclasses.fields(ModuleAnnotations):
        if field.name in _SLM_EXCLUDED_ANNOTATION_FIELDS:
            continue
        hint = hints.get(field.name)
        origin = typing.get_origin(hint)
        # Strip Optional[X] / X | None — SLM returns concrete values, not None.
        # PEP 604 (X | None) yields types.UnionType; typing.Optional yields typing.Union.
        if origin is typing.Union or origin is types.UnionType:
            args = [a for a in typing.get_args(hint) if a is not type(None)]
            if len(args) == 1:
                hint = args[0]
                origin = typing.get_origin(hint)
        if hint is bool:
            validators[field.name] = lambda v: isinstance(v, bool)
        elif hint is int:
            validators[field.name] = lambda v: isinstance(v, int) and not isinstance(v, bool)
        elif hint is str:
            validators[field.name] = lambda v: isinstance(v, str)
        elif origin in (list, tuple):
            validators[field.name] = lambda v: isinstance(v, list)
        else:
            logger.debug("AIEnhancer: skipping ModuleAnnotations field %r with unsupported type %r", field.name, hint)
    return validators


_ANNOTATION_FIELD_VALIDATORS = _build_annotation_field_validators()


class Enhancer(Protocol):
    """Protocol for pluggable metadata enhancement.

    Any class implementing this protocol can be used to fill metadata gaps
    in scanned modules. See the AI Enhancement Guide for details.
    """

    def enhance(self, modules: list[ScannedModule]) -> list[ScannedModule]: ...


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
        from urllib.parse import urlparse as _urlparse

        _parsed = _urlparse(self.endpoint)
        if _parsed.scheme not in ("http", "https"):
            raise ValueError(
                f"APCORE_AI_ENDPOINT must use http or https scheme, got: {self.endpoint!r}"
            )
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

        ``batch_size`` (configured via ``APCORE_AI_BATCH_SIZE``, default 5)
        currently controls only the outer iteration granularity — each
        module still produces its own prompt and API call. The setting
        is retained so a future implementation can coalesce prompts
        without changing the caller-facing configuration. When
        ``batch_size`` is 1, behaviour is identical to per-module
        processing. **It does not currently reduce API round-trips.**

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

        # TODO: coalesce batch_size modules into a single API call to reduce round-trips
        for idx, module, gaps in pending:
            try:
                enhanced = self._enhance_module(module, gaps)
                results[idx] = enhanced
            except Exception:
                logger.error("AI enhancement failed for %s, keeping original", module.module_id, exc_info=True)

        return results

    def _identify_gaps(self, module: ScannedModule) -> list[str]:
        """Identify which metadata fields are missing or at defaults."""
        gaps: list[str] = []
        if not module.description or module.description == module.module_id:
            gaps.append("description")
        if not module.documentation:
            gaps.append("documentation")
        if module.annotations is None or module.annotations == DEFAULT_ANNOTATIONS:
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

        # Guard: SLM may return confidence as a non-dict (e.g. "high" or 1).
        # Treat any non-dict value as absent — all fields default to 0.0.
        confidence_raw = parsed.get("confidence")
        if confidence_raw is not None and not isinstance(confidence_raw, dict):
            logger.warning(
                "Module '%s': SLM returned non-dict 'confidence' (%s) — treating as absent.",
                module.module_id,
                type(confidence_raw).__name__,
            )
        confidence_parsed: dict[str, Any] = confidence_raw if isinstance(confidence_raw, dict) else {}

        def _apply_simple(field: str) -> None:
            """Apply a simple scalar field from parsed SLM output if confidence is sufficient."""
            if field not in gaps or field not in parsed:
                return
            raw_conf = confidence_parsed.get(field, 0.0)
            if not isinstance(raw_conf, (int, float)) or isinstance(raw_conf, bool):
                logger.warning(
                    "Module '%s': non-numeric confidence for %r (%r) — treating as 0.0",
                    module.module_id,
                    field,
                    raw_conf,
                )
                field_conf: float = 0.0
            else:
                field_conf = float(raw_conf)
            confidence[field] = field_conf
            if field_conf >= self.threshold:
                updates[field] = parsed[field]
            else:
                warnings.append(f"Low confidence ({field_conf:.2f}) for {field} — skipped. Review manually.")

        _apply_simple("description")
        _apply_simple("documentation")

        # Apply annotations if above threshold. Field set is derived from
        # ModuleAnnotations at import time, so adding new fields upstream
        # automatically widens what the SLM may populate (extra excluded).
        if "annotations" in gaps and "annotations" in parsed and isinstance(parsed["annotations"], dict):
            ann_data = parsed["annotations"]
            accepted: dict[str, Any] = {}
            for field_name, validate in _ANNOTATION_FIELD_VALIDATORS.items():
                if field_name not in ann_data or not validate(ann_data[field_name]):
                    continue
                raw_ann_conf = confidence_parsed.get(
                    f"annotations.{field_name}", confidence_parsed.get(field_name, 0.0)
                )
                if not isinstance(raw_ann_conf, (int, float)) or isinstance(raw_ann_conf, bool):
                    logger.warning(
                        "Module '%s': non-numeric confidence for 'annotations.%s' (%r) — treating as 0.0",
                        module.module_id,
                        field_name,
                        raw_ann_conf,
                    )
                    field_conf = 0.0
                else:
                    field_conf = float(raw_ann_conf)
                confidence[f"annotations.{field_name}"] = field_conf
                if field_conf >= self.threshold:
                    accepted[field_name] = ann_data[field_name]
                else:
                    warnings.append(
                        f"Low confidence ({field_conf:.2f}) for annotations.{field_name} — skipped. Review manually."
                    )
            if accepted:
                base = module.annotations or DEFAULT_ANNOTATIONS
                updates["annotations"] = replace(base, **accepted)

        _apply_simple("input_schema")

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

        # Build confidence keys dynamically from gaps so the SLM is told to
        # supply confidence for every field it is being asked to fill. The
        # read side (_enhance_module) looks up annotations.<name> per-field
        # keys via _ANNOTATION_FIELD_VALIDATORS; keep the prompt and the
        # read logic symmetric by enumerating the same validator set.
        confidence_keys: list[str] = []
        if "description" in gaps:
            confidence_keys.append("description")
        if "documentation" in gaps:
            confidence_keys.append("documentation")
        if "input_schema" in gaps:
            confidence_keys.append("input_schema")
        if "annotations" in gaps:
            confidence_keys.extend(f"annotations.{name}" for name in _ANNOTATION_FIELD_VALIDATORS)

        parts.append('  "confidence": {')
        parts.append("    " + ", ".join(f'"{k}": 0.0' for k in confidence_keys))
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
            result = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"SLM returned invalid JSON: {exc}") from exc
        if not isinstance(result, dict):
            raise ValueError(f"SLM returned non-dict JSON ({type(result).__name__}); expected a JSON object")
        return result
