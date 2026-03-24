"""Display overlay resolver for surface-facing presentation.

Implements §5.13 of the apcore PROTOCOL_SPEC: sparse binding.yaml display
section that controls how modules appear in CLI, MCP, and A2A surfaces
without changing the canonical module_id.
"""

from apcore_toolkit.display.resolver import DisplayResolver

__all__ = ["DisplayResolver"]
