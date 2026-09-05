"""Pure Fast Lane compiler runtime.

The package emits bounded, inert scheduling descriptors. A host adapter owns
model dispatch, worktree operations, capability/lease attestations, and lifecycle writes.
The large compatibility/CLI module is loaded lazily so importing the public
MCP server does not import subprocess or host execution helpers.
"""

from collections.abc import Mapping, Sequence
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


def record_model_selection(
    assignment: Mapping[str, Any],
    *,
    model_id: str,
    reasoning_effort: str,
    selection_reason: str,
) -> dict[str, Any]:
    """Record an exact coordinator choice without claiming dispatch."""

    from .scripts.team_efficiency import record_model_selection as _record

    return _record(
        assignment,
        model_id=model_id,
        reasoning_effort=reasoning_effort,
        selection_reason=selection_reason,
    )


def prepare_model_neutral_fast_lane_request(
    request: Mapping[str, Any],
    *,
    route_intents: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Prepare the default model-neutral Fast Lane request schema."""

    from .scripts.team_efficiency import (
        prepare_model_neutral_fast_lane_request as _prepare,
    )

    return _prepare(request, route_intents=route_intents)


def validate_model_selection_record(
    record: Mapping[str, Any], *, assignment: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate a selection record against its planned assignment."""

    from .scripts.team_efficiency import validate_model_selection_record as _validate

    return _validate(record, assignment=assignment)


__all__ = [
    "compile_fast_lane",
    "prepare_model_neutral_fast_lane_request",
    "record_model_selection",
    "validate_model_selection_record",
]
