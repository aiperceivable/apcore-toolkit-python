"""Cross-adapter conformance checks for registry writers.

Import into an adapter's test suite to assert that registering a scanned module
**preserves its behavioral annotations**. Approval and ACL gating key on
``requires_approval`` (and read ``destructive``); if a writer drops annotations
during registration the gate silently never fires — no error, no warning. This
helper turns that otherwise-invisible regression into a failing test, and is the
shared guard behind the writer-centralization work (see the ``RegistryWriter``
hooks ``_adapt_func`` / ``_build_input_schema`` / ``_build_output_schema``).
"""

from __future__ import annotations

from typing import Any, Sequence

# Governance-relevant flags whose loss silently disables approval/ACL gating.
DEFAULT_FIELDS: tuple[str, ...] = ("requires_approval", "destructive")


def assert_annotations_preserved(
    writer: Any,
    scanned_module: Any,
    registry: Any,
    *,
    fields: Sequence[str] = DEFAULT_FIELDS,
) -> None:
    """Register ``scanned_module`` via ``writer`` and assert its behavioral
    annotations survive ``registry.get_definition``.

    Framework-agnostic: raises ``AssertionError`` on a dropped/changed field, so
    it works from any test runner. Use a **real** apcore ``Registry`` (not a
    mock) and a module whose ``target`` resolves to a real callable and whose
    ``annotations`` are set to the values under test.

    Args:
        writer: A ``RegistryWriter`` (or subclass) instance.
        scanned_module: A ``ScannedModule`` with ``annotations`` set and a
            resolvable ``target``.
        registry: A real apcore ``Registry``.
        fields: Annotation fields to compare. Defaults to the governance flags
            (``requires_approval``, ``destructive``).

    Raises:
        AssertionError: if the module was not registered, lost its annotations,
            or any checked field changed value during registration.
    """
    source = scanned_module.annotations
    assert source is not None, (
        "assert_annotations_preserved expects scanned_module.annotations to be set "
        "(that is what the round-trip is verifying)"
    )

    module_id = scanned_module.module_id
    writer.write([scanned_module], registry)
    definition = registry.get_definition(module_id)

    assert definition is not None, f"conformance: module {module_id!r} was not registered"
    assert definition.annotations is not None, (
        f"conformance: module {module_id!r} lost its annotations during registration — "
        f"approval/ACL gating that keys on requires_approval will silently never fire"
    )

    for field in fields:
        expected = getattr(source, field, None)
        actual = getattr(definition.annotations, field, None)
        assert actual == expected, (
            f"conformance: module {module_id!r} annotation {field!r} changed during "
            f"registration — expected {expected!r}, got {actual!r}"
        )
