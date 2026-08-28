"""Lightweight MCP tool metadata safe to import during server registration."""

from typing import Final

TOOL_ANNOTATIONS: Final[dict[str, tuple[bool, bool, bool, bool]]] = {
    "project_index_register": (False, False, True, False),
    "project_index_sync": (False, False, True, False),
    "project_index_status": (True, False, True, False),
    "project_index_query": (False, False, True, False),
    "worktree_checkpoint_create": (False, False, True, False),
    "worktree_checkpoint_status": (True, False, True, False),
    "worktree_checkpoint_restore": (False, True, False, False),
    "atlas_query": (True, False, True, False),
    "atlas_prepare": (False, False, True, False),
    "atlas_render": (True, False, True, False),
    "atlas_accept": (False, False, True, False),
    "relay_compile": (True, False, True, False),
    "fastlane_compile": (True, False, True, False),
    "relay_start": (False, False, True, False),
    "relay_status": (True, False, True, False),
    "relay_handoff": (False, False, False, False),
    "relay_integrate": (False, True, False, False),
}
TOOL_ANNOTATION_TABLE = TOOL_ANNOTATIONS


__all__ = ["TOOL_ANNOTATIONS", "TOOL_ANNOTATION_TABLE"]
