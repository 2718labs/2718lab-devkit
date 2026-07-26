"""Compatibility exports for the shared repository adapters."""

from orchestrator.adapters import (
    CommandSpec,
    DetectionResult,
    LanguageKind,
    ProjectProfile,
    detect_repository,
)

__all__ = [
    "CommandSpec",
    "DetectionResult",
    "LanguageKind",
    "ProjectProfile",
    "detect_repository",
]
