"""Shared application service for approval-mediated external effects."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .approvals import (
    ApprovalJournal,
    ApprovalManifest,
    ApprovalRecord,
    EffectHost,
    EffectRecord,
)


class ApprovalEffectService:
    """Expose the journal without granting Git, GitHub, or network capability."""

    def __init__(self, database: str | Path) -> None:
        self.journal = ApprovalJournal(database)

    def prepare(
        self, manifest: ApprovalManifest, *, expires_at: datetime
    ) -> ApprovalRecord:
        return self.journal.prepare(manifest, expires_at=expires_at)

    def grant(self, approval_id: str) -> ApprovalRecord:
        return self.journal.grant(approval_id)

    def deny(self, approval_id: str) -> ApprovalRecord:
        return self.journal.deny(approval_id)

    def claim(self, approval_id: str, manifest: ApprovalManifest) -> EffectRecord:
        return self.journal.claim(approval_id, manifest)

    def recover(self, effect_id: str, host: EffectHost) -> EffectRecord:
        return self.journal.recover(effect_id, host)
