"""Standalone helpers for authoring and checking AstrBot plugins."""

__version__ = "1.0.0rc1"
__release__ = __version__.replace("rc", "-rc", 1)

from .scaffold import ScaffoldError, scaffold_plugin
from .validator import Diagnostic, ValidationReport, validate_plugin

__all__ = [
    "Diagnostic",
    "ScaffoldError",
    "ValidationReport",
    "__release__",
    "__version__",
    "scaffold_plugin",
    "validate_plugin",
]
