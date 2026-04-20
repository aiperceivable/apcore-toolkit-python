"""DisplayResolver — sparse binding.yaml display overlay (§5.13).

Resolves surface-facing presentation fields (alias, description, guidance)
for each ScannedModule by merging:
  surface-specific override > display default > binding-level > scanner value

The resolved fields are stored in ScannedModule.metadata["display"] and
travel through RegistryWriter into FunctionModule.metadata["display"],
where CLI/MCP/A2A surfaces read them at render time.
"""

from __future__ import annotations

import logging
import re
import dataclasses
from dataclasses import replace
from pathlib import Path
from typing import Any

logger = logging.getLogger("apcore_toolkit")

_MCP_ALIAS_MAX = 64
_MCP_ALIAS_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_-]*$")
_CLI_ALIAS_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")


class DisplayResolver:
    """Resolves display overlay fields for a list of ScannedModules.

    Usage::

        resolver = DisplayResolver()
        resolved = resolver.resolve(scanned_modules, binding_path="bindings/")

    The returned list contains the same ScannedModules with
    ``metadata["display"]`` populated for all surfaces.
    """

    def resolve(
        self,
        modules: list[Any],
        *,
        binding_path: str | Path | None = None,
        binding_data: dict[str, Any] | None = None,
    ) -> list[Any]:
        """Apply display overlay to a list of ScannedModules.

        Args:
            modules: ScannedModule instances from a framework scanner.
            binding_path: Path to a single ``.binding.yaml`` file or a
                directory of binding files. Optional.
            binding_data: Pre-parsed binding YAML content as a dict
                (``{"bindings": [...]}``) or a ``module_id → entry`` map.
                Takes precedence over ``binding_path``.

        Returns:
            New ScannedModule list with ``metadata["display"]`` populated.
        """
        binding_map = self._build_binding_map(binding_path, binding_data)
        if binding_map:
            matched = sum(1 for mod in modules if mod.module_id in binding_map)
            logger.info(
                "DisplayResolver: %d/%d modules matched binding entries.",
                matched,
                len(modules),
            )
            if matched == 0:
                logger.warning(
                    "DisplayResolver: binding map loaded %d entries but none matched "
                    "any scanned module_id — check binding.yaml module_id values.",
                    len(binding_map),
                )
        return [self._resolve_one(mod, binding_map) for mod in modules]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_binding_map(
        self,
        binding_path: str | Path | None,
        binding_data: dict[str, Any] | None,
    ) -> dict[str, dict[str, Any]]:
        """Return a module_id → binding-entry dict."""
        if binding_data is not None:
            return self._parse_binding_data(binding_data)
        if binding_path is not None:
            return self._load_binding_files(Path(binding_path))
        return {}

    def _parse_binding_data(self, data: Any) -> dict[str, dict[str, Any]]:
        """Parse pre-loaded binding data."""
        if not isinstance(data, dict):
            logger.warning(
                "DisplayResolver: binding data is not a dict (%s) — ignoring",
                type(data).__name__,
            )
            return {}
        # Accept either {"bindings": [...]} or a direct module_id → entry map
        if "bindings" in data:
            return {entry["module_id"]: entry for entry in data.get("bindings", []) if "module_id" in entry}
        # Already a map
        return {k: v for k, v in data.items() if isinstance(v, dict)}

    def _load_binding_files(self, path: Path) -> dict[str, dict[str, Any]]:
        """Load binding files from a path (file or directory)."""
        try:
            import yaml
        except ImportError:
            logger.warning("PyYAML not installed — display overlay from files unavailable")
            return {}

        result: dict[str, dict[str, Any]] = {}

        files: list[Path] = []
        if path.is_file():
            files = [path]
        elif path.is_dir():
            files = sorted(path.glob("*.binding.yaml"))
        else:
            logger.warning("DisplayResolver: binding path not found: %s", path)
            return {}

        for f in files:
            try:
                with f.open() as fh:
                    data = yaml.safe_load(fh)
                result.update(self._parse_binding_data(data or {}))
            except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
                logger.warning("DisplayResolver: failed to load %s: %s", f, exc)

        return result

    def _resolve_one(self, mod: Any, binding_map: dict[str, dict[str, Any]]) -> Any:
        """Resolve display fields for a single ScannedModule.

        ``suggested_alias`` is read from two sources in priority order:

        1. ``mod.suggested_alias`` (top-level field, preferred)
        2. ``mod.metadata["suggested_alias"]`` (legacy fallback)

        The top-level field takes precedence when set to a truthy value.
        """
        entry = binding_map.get(mod.module_id, {})
        display_cfg: dict[str, Any] = entry.get("display") or {}
        binding_desc: str | None = entry.get("description")
        binding_docs: str | None = entry.get("documentation")
        suggested_alias: str | None = getattr(mod, "suggested_alias", None) or (mod.metadata or {}).get(
            "suggested_alias"
        )

        # ── Resolve cross-surface defaults ──────────────────────────────
        default_alias: str = display_cfg.get("alias") or suggested_alias or mod.module_id
        default_description: str = display_cfg.get("description") or binding_desc or mod.description
        default_documentation: str | None = display_cfg.get("documentation") or binding_docs or mod.documentation
        default_guidance: str | None = display_cfg.get("guidance")
        resolved_tags: list[str] = display_cfg.get("tags") or entry.get("tags") or mod.tags

        # ── Resolve per-surface fields ───────────────────────────────────
        def _surface(key: str) -> tuple[dict[str, Any], bool]:
            """Return (surface_dict, alias_was_explicit)."""
            sc_raw = display_cfg.get(key)
            if sc_raw is not None and not isinstance(sc_raw, dict):
                logger.warning(
                    "Module '%s': display.%s must be a dict, got %s — ignoring.",
                    mod.module_id,
                    key,
                    type(sc_raw).__name__,
                )
                sc_raw = None
            sc: dict[str, Any] = sc_raw or {}
            alias_explicit = bool(sc.get("alias"))
            return (
                {
                    "alias": sc.get("alias") or default_alias,
                    "description": sc.get("description") or default_description,
                    "guidance": sc.get("guidance") or default_guidance,
                },
                alias_explicit,
            )

        cli_surface, cli_alias_explicit = _surface("cli")
        mcp_surface, _ = _surface("mcp")
        a2a_surface, _ = _surface("a2a")

        # Auto-sanitize MCP alias: replace non-[a-zA-Z0-9_-] chars with _,
        # then prepend _ if the result starts with a digit.
        # Module IDs contain dots (e.g. image.resize → image_resize).
        raw_mcp_alias = mcp_surface["alias"]
        sanitized = re.sub(r"[^a-zA-Z0-9_-]", "_", raw_mcp_alias)
        if sanitized and sanitized[0].isdigit():
            sanitized = "_" + sanitized
        mcp_surface["alias"] = sanitized
        if sanitized != raw_mcp_alias:
            logger.debug(
                "Module '%s': MCP alias auto-sanitized '%s' → '%s'.",
                mod.module_id,
                raw_mcp_alias,
                sanitized,
            )

        display: dict[str, Any] = {
            "alias": default_alias,
            "description": default_description,
            "documentation": default_documentation,
            "guidance": default_guidance,
            "tags": resolved_tags,
            "cli": cli_surface,
            "mcp": mcp_surface,
            "a2a": a2a_surface,
        }

        # ── Validate aliases ─────────────────────────────────────────────
        self._validate_aliases(display, mod.module_id, cli_alias_explicit=cli_alias_explicit)

        new_metadata = {**(mod.metadata or {}), "display": display}
        if dataclasses.is_dataclass(mod) and not isinstance(mod, type):
            return replace(mod, metadata=new_metadata)
        # Duck-typed module — update metadata attribute directly
        try:
            mod.metadata = new_metadata
        except AttributeError:
            pass
        return mod

    def _validate_aliases(
        self,
        display: dict[str, Any],
        module_id: str,
        *,
        cli_alias_explicit: bool = False,
    ) -> None:
        """Validate surface alias constraints per §5.13.6."""
        # MCP: MUST enforce 64-char hard limit (alias was already auto-sanitized)
        mcp_alias: str = display["mcp"]["alias"]
        if len(mcp_alias) > _MCP_ALIAS_MAX:
            raise ValueError(
                f"Module '{module_id}': MCP alias '{mcp_alias}' exceeds "
                f"{_MCP_ALIAS_MAX}-character hard limit (OpenAI spec). "
                f"Set display.mcp.alias to a shorter value."
            )
        if not _MCP_ALIAS_PATTERN.match(mcp_alias):
            raise ValueError(
                f"Module '{module_id}': MCP alias '{mcp_alias}' does not match "
                f"required pattern ^[a-zA-Z_][a-zA-Z0-9_-]*$."
            )

        # CLI: only validate user-explicitly-set aliases (module_id fallback is always valid).
        if cli_alias_explicit:
            cli_alias: str = display["cli"]["alias"]
            if not _CLI_ALIAS_PATTERN.match(cli_alias):
                logger.warning(
                    "Module '%s': CLI alias '%s' does not match shell-safe pattern "
                    "^[a-z][a-z0-9_-]*$ — falling back to default alias '%s'.",
                    module_id,
                    cli_alias,
                    display["alias"],
                )
                display["cli"]["alias"] = display["alias"]
