"""Pure Fast Lane compiler runtime.

The package emits bounded, inert scheduling descriptors.  A host adapter owns
model dispatch, worktree operations, quota attestations, and lifecycle writes.
The large compatibility/CLI module is loaded lazily so importing the public
MCP server does not import subprocess or host execution helpers.
"""

from collections.abc import Mapping
from typing import Any


def compile_fast_lane(
    request: Mapping[str, Any],
    *,
    reasoning_effort: str,
    enable: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Delegate to the bounded compiler without widening the public boundary."""

    from .scripts.team_efficiency import compile_fast_lane as _compile

    return _compile(
        request,
        reasoning_effort=reasoning_effort,
        enable=enable,
        **kwargs,
    )


__all__ = ["compile_fast_lane"]
