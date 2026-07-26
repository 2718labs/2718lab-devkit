"""Explicit Bugkiller case transitions; flags and events are not states."""

from __future__ import annotations

from enum import Enum

from .models import BugState


class CaseEvent(str, Enum):
    TRIAGED = "TRIAGED"
    REPRODUCED = "REPRODUCED"
    LOCALIZED = "LOCALIZED"
    DESIGNED = "DESIGNED"
    PATCHED = "PATCHED"
    VERIFIED = "VERIFIED"
    RESUMED = "RESUMED"
    REQUIRE_HUMAN_GATE = "REQUIRE_HUMAN_GATE"
    APPROVE_REVIEW = "APPROVE_REVIEW"
    COMPLETE = "COMPLETE"
    BLOCK = "BLOCK"
    FAIL = "FAIL"
    CANCEL = "CANCEL"


_CASE_STATES = frozenset(BugState)
_TRANSITIONS: dict[tuple[BugState, CaseEvent], BugState] = {
    (BugState.NEW, CaseEvent.TRIAGED): BugState.TRIAGED,
    (BugState.TRIAGED, CaseEvent.REPRODUCED): BugState.REPRODUCING,
    (BugState.TRIAGED, CaseEvent.RESUMED): BugState.REPRODUCING,
    (BugState.REPRODUCING, CaseEvent.LOCALIZED): BugState.LOCALIZING,
    (BugState.LOCALIZING, CaseEvent.DESIGNED): BugState.DESIGNING,
    (BugState.DESIGNING, CaseEvent.PATCHED): BugState.PATCHING,
    (BugState.PATCHING, CaseEvent.VERIFIED): BugState.VERIFYING,
    (BugState.VERIFYING, CaseEvent.COMPLETE): BugState.DONE,
    (BugState.NEW, CaseEvent.REQUIRE_HUMAN_GATE): BugState.HUMAN_GATE,
    (BugState.HUMAN_GATE, CaseEvent.APPROVE_REVIEW): BugState.REVIEWING,
}


def is_case_state(value: object) -> bool:
    """Return whether value identifies a persisted case state."""

    if isinstance(value, BugState):
        return value in _CASE_STATES
    try:
        return BugState(str(value)) in _CASE_STATES
    except ValueError:
        return False


def transition(current: BugState, event: CaseEvent) -> BugState:
    """Apply an allowed event or reject an invalid state transition."""

    if event is CaseEvent.BLOCK:
        return BugState.BLOCKED
    if event is CaseEvent.FAIL:
        return BugState.FAILED
    if event is CaseEvent.CANCEL:
        return BugState.CANCELLED
    try:
        return _TRANSITIONS[(current, event)]
    except KeyError as error:
        raise ValueError(
            f"invalid Bugkiller transition: {current.value} + {event.value}"
        ) from error
