"""Registry writer for direct module registration.

Converts ScannedModule instances into apcore FunctionModule instances
and registers them directly into an apcore Registry. This is the default
output mode for framework adapters (no file I/O needed).

Extracted from flask-apcore's registry_writer.py into the shared toolkit.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from apcore_toolkit.output.types import Verifier, WriteResult
from apcore_toolkit.output.verifiers import RegistryVerifier, run_verifier_chain
from apcore_toolkit.pydantic_utils import flatten_pydantic_params, resolve_target
from apcore_toolkit.serializers import annotations_to_dict

if TYPE_CHECKING:
    from apcore import FunctionModule, Registry

    from apcore_toolkit.types import ScannedModule

logger = logging.getLogger("apcore_toolkit")


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

        Returns:
            List of WriteResult instances.
        """
        results: list[WriteResult] = []
        for mod in modules:
            if dry_run:
                results.append(WriteResult(module_id=mod.module_id))
                continue
            try:
                fm = self._to_function_module(mod)
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
            if result.verified and verifiers:
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

    def _to_function_module(self, mod: ScannedModule) -> FunctionModule:
        """Convert a ScannedModule to an apcore FunctionModule.

        Args:
            mod: The ScannedModule to convert.

        Returns:
            A FunctionModule instance ready for registry insertion.
        """
        from apcore import FunctionModule

        func = flatten_pydantic_params(resolve_target(mod.target))

        return FunctionModule(
            func=func,
            module_id=mod.module_id,
            description=mod.description,
            documentation=mod.documentation,
            tags=mod.tags,
            version=mod.version,
            annotations=annotations_to_dict(mod.annotations),
            metadata=mod.metadata,
            examples=mod.examples or None,
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
