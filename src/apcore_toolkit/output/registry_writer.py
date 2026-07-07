"""Registry writer for direct module registration.

Converts ScannedModule instances into apcore FunctionModule instances
and registers them directly into an apcore Registry. This is the default
output mode for framework adapters (no file I/O needed).

Extracted from flask-apcore's registry_writer.py into the shared toolkit.
"""

from __future__ import annotations

import inspect
import logging
from typing import TYPE_CHECKING, Any, AsyncIterator

from apcore_toolkit.output.types import Verifier, WriteResult
from apcore_toolkit.output.verifiers import RegistryVerifier, run_verifier_chain
from apcore_toolkit.pydantic_utils import flatten_pydantic_params, resolve_target
from apcore_toolkit.serializers import annotations_to_dict

if TYPE_CHECKING:
    from apcore import FunctionModule, Registry

    from apcore_toolkit.types import ScannedModule

logger = logging.getLogger("apcore_toolkit")

# Sentinel distinguishing "derive annotations from the module" from an explicit
# (possibly empty) annotations override passed to ``_build_function_module``.
_UNSET: Any = object()


class _StreamingFunctionModule:
    """Adapter that satisfies the apcore StreamingModule Protocol for a streaming target.

    Wraps a FunctionModule for the execute path and delegates stream() to the
    target object's async stream() method.  Because this class defines an async
    generator ``stream`` method, ``inspect.isasyncgenfunction(adapter.stream)``
    returns True and ``isinstance(adapter, StreamingModule)`` returns True under
    apcore's ``@runtime_checkable`` Protocol — satisfying the Registry's
    ``_validate_streaming_signature`` check.
    """

    def __init__(self, base: FunctionModule, streaming_target: Any) -> None:
        self._base = base
        self._streaming_target = streaming_target
        # Mirror every public attribute that Registry/Executor inspects on FunctionModule.
        self.module_id = base.module_id
        self.description = base.description
        self.documentation = base.documentation
        self.tags = base.tags
        self.version = base.version
        self.annotations = base.annotations
        self.metadata = base.metadata
        self.examples = base.examples
        self.input_schema = base.input_schema
        self.output_schema = base.output_schema
        # execute is a bound method/closure on FunctionModule — delegate directly.
        self.execute = base.execute

    async def stream(self, inputs: dict, context: Any) -> AsyncIterator[dict]:
        """Async generator delegating to the target's stream() method."""
        async for chunk in self._streaming_target.stream(inputs, context):
            yield chunk


class RegistryWriter:
    """Converts ScannedModule to FunctionModule and registers into Registry.

    This is the default writer used when no output_format is specified.
    Instead of writing YAML binding files, it registers modules directly
    into the apcore Registry for immediate use.
    """

    def write(
        self,
        modules: list[ScannedModule],
        registry: Registry,
        *,
        dry_run: bool = False,
        verify: bool = False,
        verifiers: list[Verifier] | None = None,
        allowed_prefixes: list[str] | None = None,
    ) -> list[WriteResult]:
        """Register scanned modules into the registry.

        Args:
            modules: List of ScannedModule instances to register.
            registry: The apcore Registry to register modules into.
            dry_run: If True, skip registration and return results only.
            verify: If True, verify modules are retrievable from the registry after registration.
            verifiers: Optional list of custom Verifier instances. When provided,
                these run after the built-in check (if verify=True). First failure
                stops the chain.
            allowed_prefixes: Optional allowlist of module-name prefixes
                forwarded to :func:`resolve_target`. When set, registration
                fails for any ``ScannedModule.target`` whose module path does
                not match one of the prefixes. Use this when loading bindings
                from untrusted sources to mitigate arbitrary-code-execution
                via forged ``target`` strings (matches the TypeScript SDK's
                ``allowedPrefixes`` option).

        Returns:
            List of WriteResult instances.
        """
        results: list[WriteResult] = []
        for mod in modules:
            if dry_run:
                results.append(WriteResult(module_id=mod.module_id))
                continue
            try:
                fm = self._to_function_module(mod, allowed_prefixes=allowed_prefixes)
                registry.register(mod.module_id, fm)
            except Exception as exc:
                logger.warning(
                    "RegistryWriter: failed to register %s: %s",
                    mod.module_id,
                    exc,
                    exc_info=True,
                )
                results.append(
                    WriteResult(
                        module_id=mod.module_id,
                        verified=False,
                        verification_error=f"{type(exc).__name__}: {exc}",
                    )
                )
                continue
            logger.debug("Registered module: %s", mod.module_id)

            result = WriteResult(module_id=mod.module_id)
            if verify:
                result = self._verify(result, mod.module_id, registry)
            if verifiers:
                chain_result = run_verifier_chain(verifiers, "", mod.module_id)
                if not chain_result.ok:
                    result = WriteResult(
                        module_id=result.module_id,
                        path=result.path,
                        verified=False,
                        verification_error=chain_result.error,
                    )
            results.append(result)
        return results

    def _to_function_module(
        self,
        mod: ScannedModule,
        *,
        allowed_prefixes: list[str] | None = None,
    ) -> FunctionModule | _StreamingFunctionModule:
        """Convert a ScannedModule to an apcore module ready for registry insertion.

        Returns a plain ``FunctionModule`` for non-streaming modules, or a
        ``_StreamingFunctionModule`` adapter when ``mod.annotations.streaming``
        is True and the resolved target exposes a valid async ``stream()`` method.

        If streaming is declared but the target has no async ``stream()`` method,
        a WARNING is logged and the ``streaming`` annotation is cleared so
        ``Registry.register`` does not raise ``StreamingInterfaceError``.

        Subclasses SHOULD NOT override this method — doing so has repeatedly
        dropped fields such as ``annotations`` (silently disabling approval/ACL
        gating that keys on ``requires_approval``). Override the narrow hooks
        instead: :meth:`_adapt_func` for framework-specific callable adaptation,
        and :meth:`_build_input_schema` / :meth:`_build_output_schema` for
        explicit schema models. Field mapping to ``FunctionModule`` stays
        centralized in :meth:`_build_function_module`.

        Args:
            mod: The ScannedModule to convert.
            allowed_prefixes: Forwarded to :func:`resolve_target` — when set,
                rejects any target whose module path is not under an allowed
                prefix.

        Returns:
            A module instance ready for registry insertion.
        """
        raw_target = resolve_target(mod.target, allowed_prefixes=allowed_prefixes)
        streaming_requested = bool(getattr(mod.annotations, "streaming", False))

        if streaming_requested:
            stream_method = getattr(raw_target, "stream", None)
            has_async_stream = callable(stream_method) and (
                inspect.iscoroutinefunction(stream_method) or inspect.isasyncgenfunction(stream_method)
            )
            if has_async_stream:
                # Target exposes a valid async stream() — build streaming adapter.
                # Use the target's execute() for the FunctionModule base when available;
                # fall back to treating the target itself as a callable.
                execute_callable = getattr(raw_target, "execute", raw_target)
                func = flatten_pydantic_params(self._adapt_func(execute_callable, mod))
                base = self._build_function_module(mod, func)
                return _StreamingFunctionModule(base=base, streaming_target=raw_target)

            # Streaming requested but no valid async stream() found.
            logger.warning(
                "RegistryWriter: module '%s' has annotations.streaming=True but target '%s' "
                "exposes no async stream() method; clearing streaming flag to avoid "
                "StreamingInterfaceError at registration. "
                "Implement stream(self, inputs, context) as an async generator on the target "
                "or register the module directly via Registry.register().",
                mod.module_id,
                mod.target,
            )
            func = flatten_pydantic_params(self._adapt_func(raw_target, mod))
            cleared = {**(annotations_to_dict(mod.annotations) or {}), "streaming": False}
            return self._build_function_module(mod, func, annotations=cleared)

        func = flatten_pydantic_params(self._adapt_func(raw_target, mod))
        return self._build_function_module(mod, func)

    def _adapt_func(self, func: Any, mod: ScannedModule) -> Any:
        """Hook: framework-specific adaptation of the resolved callable, applied
        BEFORE Pydantic flattening (e.g. strip a leading ``request`` parameter).

        Default: return the callable unchanged. Override this — not
        :meth:`_to_function_module` — so behavioral annotations and other fields
        are never accidentally dropped.
        """
        return func

    def _build_input_schema(self, mod: ScannedModule) -> Any | None:
        """Hook: explicit input Pydantic model, skipping function introspection.

        Default ``None`` — ``FunctionModule`` generates the model from the
        callable signature. Adapters with an authoritative scanned schema
        (e.g. OpenAPI) override this to build a model from ``mod.input_schema``.
        """
        return None

    def _build_output_schema(self, mod: ScannedModule) -> Any | None:
        """Hook: explicit output Pydantic model. See :meth:`_build_input_schema`."""
        return None

    def _build_function_module(
        self,
        mod: ScannedModule,
        func: Any,
        *,
        annotations: Any = _UNSET,
    ) -> FunctionModule:
        """Single, centralized ``ScannedModule`` -> ``FunctionModule`` field mapping.

        Every field a scanned module carries — including the behavioral
        ``annotations`` that approval/ACL gating depends on — is forwarded here,
        in one place, so no subclass override can silently drop it.

        Args:
            mod: The source scanned module.
            func: The final callable (already adapted and flattened).
            annotations: Override for the annotations dict; when ``_UNSET`` it is
                derived from ``mod.annotations``. Used to clear ``streaming``.
        """
        from apcore import FunctionModule

        ann = annotations_to_dict(mod.annotations) if annotations is _UNSET else annotations
        return FunctionModule(
            func=func,
            module_id=mod.module_id,
            description=mod.description,
            documentation=getattr(mod, "documentation", None),
            tags=mod.tags,
            version=mod.version,
            annotations=ann,
            metadata=getattr(mod, "metadata", None),
            display=getattr(mod, "display", None),
            examples=getattr(mod, "examples", None),
            input_schema=self._build_input_schema(mod),
            output_schema=self._build_output_schema(mod),
        )

    @staticmethod
    def _verify(result: WriteResult, module_id: str, registry: Registry) -> WriteResult:
        """Verify that a module was successfully registered and is retrievable."""
        vr = RegistryVerifier(registry).verify("", module_id)
        if not vr.ok:
            return WriteResult(
                module_id=module_id,
                verified=False,
                verification_error=vr.error,
            )
        return result
