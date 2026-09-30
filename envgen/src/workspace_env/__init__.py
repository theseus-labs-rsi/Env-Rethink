"""Controlled, read-only workspace MCP capabilities for Workspace-Bench."""

from .manifest import ConditionManifest, ResolvedManifest, load_and_resolve_manifest

__all__ = ["ConditionManifest", "ResolvedManifest", "load_and_resolve_manifest"]
