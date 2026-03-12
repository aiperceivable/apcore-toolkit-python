"""Shared types for output writers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class VerifyResult:
    """Result of a single verifier check.

    Attributes:
        ok: Whether the verification passed.
        error: Error message if verification failed.
    """

    ok: bool
    error: str | None = None


@runtime_checkable
class Verifier(Protocol):
    """Protocol for pluggable output verifiers.

    Implementations check that a written artifact is well-formed
    according to domain-specific rules.
    """

    def verify(self, path: str, module_id: str) -> VerifyResult: ...


@dataclass
class WriteResult:
    """Result of writing a single module.

    Attributes:
        module_id: The module that was written.
        path: Output file path (None for RegistryWriter).
        verified: Whether verification passed (always True if verify=False).
        verification_error: Error message if verification failed.
    """

    module_id: str
    path: str | None = None
    verified: bool = True
    verification_error: str | None = None
