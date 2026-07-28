"""Value types shared by Bugkiller policy modules."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping


class BugState(str, Enum):
    NEW = "NEW"
    TRIAGED = "TRIAGED"
    REPRODUCING = "REPRODUCING"
    LOCALIZING = "LOCALIZING"
    DESIGNING = "DESIGNING"
    PATCHING = "PATCHING"
    VERIFYING = "VERIFYING"
    DONE = "DONE"
    HUMAN_GATE = "HUMAN_GATE"
    REVIEWING = "REVIEWING"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    ABANDONED = "ABANDONED"
    ROLLED_BACK = "ROLLED_BACK"


class ModelRole(str, Enum):
    LUNA = "luna"
    TERRA = "terra"
    SOL = "sol"


class RiskTrigger(str, Enum):
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    CREDENTIALS = "credentials"
    CRYPTOGRAPHY = "cryptography"
    PAYMENTS = "payments"
    PRIVACY = "privacy"
    DATA_DELETION_MIGRATION = "data_deletion_migration"
    REMOTE_EXECUTION = "remote_execution"
    SUPPLY_CHAIN = "supply_chain"
    CI_RELEASE = "ci_release"
    PUBLIC_NETWORK_EXPOSURE = "public_network_exposure"
    EVIDENCE_CONFLICT = "evidence_conflict"
    TWO_FAILED_PATCH_ROUNDS = "two_failed_patch_rounds"


@dataclass(frozen=True)
class ModelBudgets:
    """Per-case call budget. Sol stays zero without an approved escalation."""

    calls: Mapping[ModelRole, int]

    def __getitem__(self, role: ModelRole) -> int:
        return self.calls.get(role, 0)
