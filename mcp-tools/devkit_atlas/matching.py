"""Pure deterministic Atlas recipe matching."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import IntEnum
from pathlib import PurePosixPath

from project_index.models import SnapshotFacts

from .canonical import canonical_hash, normalize_intent_id
from .models import AtlasError, AtlasStatus, RecipeManifest


_FRAMEWORK_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_NUMERIC_VERSION = re.compile(r"^\d+(?:\.\d+)*$")
_SPECIFIER_CLAUSE = re.compile(r"^(>=|<=|==|!=|>|<)(\d+(?:\.\d+)*)$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_FRAMEWORK_LENGTH = 256
_MAX_FRAMEWORK_NAME_LENGTH = 128
_MAX_VERSION_COMPONENTS = 8
_MAX_VERSION_COMPONENT_DIGITS = 9
_MAX_NUMERIC_VERSION_LENGTH = (
    _MAX_VERSION_COMPONENTS * _MAX_VERSION_COMPONENT_DIGITS
    + _MAX_VERSION_COMPONENTS
    - 1
)
_MAX_SPECIFIER_CLAUSES = 16
_MAX_SPECIFIER_LENGTH = 512
_SUPPORTED_CONSTRAINTS = frozenset(
    {
        "required_node_kind",
        "required_symbol",
        "required_import",
        "path_suffix",
        "forbidden_gap",
    }
)


class MatchClass(IntEnum):
    """Discrete recipe applicability classes, ordered from most specific."""

    EXACT_REPOSITORY = 0
    EXACT_FRAMEWORK = 1
    LANGUAGE_GENERIC = 2


@dataclass(frozen=True, slots=True)
class MatchCandidate:
    """A compatible recipe and its deterministic applicability class."""

    manifest: RecipeManifest
    match_class: MatchClass
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))

    @property
    def reasons(self) -> tuple[str, ...]:
        """Compatibility alias for consumers that call these reason codes."""
        return self.reason_codes


@dataclass(frozen=True, slots=True)
class MatchResult:
    """A bounded deterministic selection result."""

    status: AtlasStatus
    winner: MatchCandidate | None = None
    candidates: tuple[MatchCandidate, ...] = ()
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "candidates",
            tuple(sorted(self.candidates, key=lambda item: item.manifest.recipe_id)),
        )
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))

    @property
    def best_candidates(self) -> tuple[MatchCandidate, ...]:
        """Name the sorted candidates exposed by the public matching contract."""
        return self.candidates

    @property
    def reasons(self) -> tuple[str, ...]:
        """Compatibility alias for consumers that call these reason codes."""
        return self.reason_codes


def _normalise_text(value: str, *, code: str) -> str:
    if not isinstance(value, str):
        raise AtlasError(code)
    normalized = value.strip().casefold()
    if not normalized:
        raise AtlasError(code)
    return normalized


def _numeric_version(value: str) -> tuple[int, ...] | None:
    if (
        not isinstance(value, str)
        or len(value) > _MAX_NUMERIC_VERSION_LENGTH
        or _NUMERIC_VERSION.fullmatch(value) is None
    ):
        return None
    parts = value.split(".")
    if len(parts) > _MAX_VERSION_COMPONENTS or any(
        len(part) > _MAX_VERSION_COMPONENT_DIGITS for part in parts
    ):
        return None
    try:
        parsed = tuple(int(part) for part in parts)
    except ValueError:
        return None
    while len(parsed) > 1 and parsed[-1] == 0:
        parsed = parsed[:-1]
    return parsed


def _compare_versions(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    width = max(len(left), len(right))
    padded_left = left + (0,) * (width - len(left))
    padded_right = right + (0,) * (width - len(right))
    return (padded_left > padded_right) - (padded_left < padded_right)


def _parse_specifier(value: object) -> tuple[tuple[str, tuple[int, ...]], ...] | None:
    if not isinstance(value, str) or not value or len(value) > _MAX_SPECIFIER_LENGTH:
        return None
    raw_clauses = value.split(",")
    if len(raw_clauses) > _MAX_SPECIFIER_CLAUSES:
        return None
    clauses: list[tuple[str, tuple[int, ...]]] = []
    for raw_clause in raw_clauses:
        clause = raw_clause.strip()
        match = _SPECIFIER_CLAUSE.fullmatch(clause)
        if match is None:
            return None
        parsed = _numeric_version(match.group(2))
        if parsed is None:
            return None
        clauses.append((match.group(1), parsed))
    return tuple(clauses) if clauses else None


def _version_matches(
    version: tuple[int, ...], clauses: tuple[tuple[str, tuple[int, ...]], ...]
) -> bool:
    for operator, expected in clauses:
        comparison = _compare_versions(version, expected)
        if (
            (operator == ">" and comparison <= 0)
            or (operator == ">=" and comparison < 0)
            or (operator == "<" and comparison >= 0)
            or (operator == "<=" and comparison > 0)
            or (operator == "==" and comparison != 0)
            or (operator == "!=" and comparison == 0)
        ):
            return False
    return True


def _parse_framework(value: object) -> tuple[str, tuple[int, ...] | None] | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AtlasError("invalid_framework")
    if len(value) > _MAX_FRAMEWORK_LENGTH:
        raise AtlasError("invalid_framework")
    raw = value.strip().casefold()
    if not raw:
        return None
    if raw.count("@") > 1:
        raise AtlasError("invalid_framework")
    name, separator, version_text = raw.partition("@")
    if (
        len(name) > _MAX_FRAMEWORK_NAME_LENGTH
        or _FRAMEWORK_NAME.fullmatch(name) is None
    ):
        raise AtlasError("invalid_framework")
    if not separator:
        return name, None
    version = _numeric_version(version_text)
    if version is None:
        raise AtlasError("invalid_framework")
    return name, version


def normalize_framework(value: str | None) -> str | None:
    """Return the deterministic framework request representation."""

    parsed = _parse_framework(value)
    if parsed is None:
        return None
    name, version = parsed
    if version is None:
        return name
    return f"{name}@{'.'.join(str(component) for component in version)}"


def _is_relative_fact_path(value: object) -> bool:
    if not isinstance(value, str) or not value or value.startswith(("/", "\\")):
        return False
    if len(value) >= 2 and value[1] == ":":
        return False
    path = PurePosixPath(value.replace("\\", "/"))
    return all(part not in {"", ".", ".."} for part in path.parts)


def structural_repository_signature(
    snapshot_facts: SnapshotFacts, *, language: str, framework: str | None
) -> str:
    """Hash parser-backed structural facts without workspace or source content."""

    language_name = _normalise_text(language, code="invalid_language")
    framework_value = _parse_framework(framework)
    paths = {
        path.replace("\\", "/")
        for path, _content_hash in snapshot_facts.file_hashes
        if _is_relative_fact_path(path)
    }
    paths.update(
        node.path.replace("\\", "/")
        for node in snapshot_facts.nodes
        if _is_relative_fact_path(node.path)
    )
    top_levels = sorted({PurePosixPath(path).parts[0] for path in paths})
    suffixes = sorted(
        {
            PurePosixPath(path).suffix.casefold()
            for path in paths
            if PurePosixPath(path).suffix
        }
    )
    extractor_set = sorted(
        {(node.extractor_id, node.extractor_version) for node in snapshot_facts.nodes}
    )
    node_kinds = sorted({node.kind for node in snapshot_facts.nodes})
    relation_facts = sorted(
        {
            (node.kind.casefold(), (node.qualified_name or node.name).casefold())
            for node in snapshot_facts.nodes
            if node.kind.casefold() in {"import", "call"}
            and (node.qualified_name or node.name)
        }
    )
    return canonical_hash(
        {
            "language": language_name,
            "framework": None if framework_value is None else framework_value[0],
            "extractors": extractor_set,
            "node_kinds": node_kinds,
            "top_levels": top_levels,
            "suffixes": suffixes,
            "qualified_facts": relation_facts,
        }
    )


def _constraints_apply(manifest: RecipeManifest, facts: SnapshotFacts) -> bool:
    node_kinds = {node.kind for node in facts.nodes}
    symbols = {node.name for node in facts.nodes}
    symbols.update(node.qualified_name for node in facts.nodes if node.qualified_name)
    imports = {
        value
        for node in facts.nodes
        if node.kind.casefold() == "import"
        for value in (node.name, node.qualified_name)
        if value
    }
    paths = {path for path, _content_hash in facts.file_hashes}
    gaps = {gap.code for gap in facts.gaps}
    for constraint in manifest.constraints:
        if (
            not isinstance(constraint.kind, str)
            or constraint.kind not in _SUPPORTED_CONSTRAINTS
            or not isinstance(constraint.subject, str)
            or not isinstance(constraint.value, str)
            or not constraint.value
        ):
            return False
        if (
            constraint.kind == "required_node_kind"
            and constraint.value not in node_kinds
        ):
            return False
        if constraint.kind == "required_symbol" and constraint.value not in symbols:
            return False
        if constraint.kind == "required_import" and constraint.value not in imports:
            return False
        if constraint.kind == "path_suffix" and not any(
            path.endswith(constraint.value) for path in paths
        ):
            return False
        if constraint.kind == "forbidden_gap" and constraint.value in gaps:
            return False
    return True


def _classify(
    manifest: RecipeManifest,
    *,
    intent_id: str,
    language: str,
    framework: tuple[str, tuple[int, ...] | None] | None,
    repository_signature: str,
    facts: SnapshotFacts,
) -> MatchClass | None:
    if (
        not isinstance(manifest.intent_id, str)
        or normalize_intent_id(manifest.intent_id) != intent_id
        or not isinstance(manifest.language_name, str)
        or manifest.language_name.strip().casefold() != language
    ):
        return None
    if manifest.framework_name is None:
        candidate_framework = None
        if manifest.framework_specifier not in (None, "") or framework is not None:
            return None
    else:
        if not isinstance(manifest.framework_name, str):
            return None
        if len(manifest.framework_name) > _MAX_FRAMEWORK_NAME_LENGTH:
            return None
        candidate_name = manifest.framework_name.strip().casefold()
        if _FRAMEWORK_NAME.fullmatch(candidate_name) is None:
            return None
        candidate_framework = (candidate_name, None)
        clauses = _parse_specifier(manifest.framework_specifier)
        if (
            clauses is None
            or framework is None
            or candidate_framework[0] != framework[0]
        ):
            return None
        if framework[1] is not None and not _version_matches(framework[1], clauses):
            return None
    if not _constraints_apply(manifest, facts):
        return None
    if not isinstance(manifest.repository_signature, str):
        return None
    if (
        manifest.repository_signature
        and _HASH.fullmatch(manifest.repository_signature) is None
    ):
        return None
    if (
        manifest.repository_signature
        and manifest.repository_signature == repository_signature
    ):
        return MatchClass.EXACT_REPOSITORY
    if candidate_framework is not None:
        return MatchClass.EXACT_FRAMEWORK
    if not manifest.repository_signature and candidate_framework is None:
        return MatchClass.LANGUAGE_GENERIC
    return None


def _deduplicated(candidates: Sequence[RecipeManifest]) -> tuple[RecipeManifest, ...]:
    chosen: dict[str, RecipeManifest] = {}
    ordered = sorted(
        candidates,
        key=lambda item: (str(item.recipe_id), str(item.manifest_hash), item.layer),
    )
    for manifest in ordered:
        if isinstance(manifest.recipe_id, str) and manifest.recipe_id:
            chosen.setdefault(manifest.recipe_id, manifest)
    return tuple(chosen[recipe_id] for recipe_id in sorted(chosen))


def _is_active(manifest: RecipeManifest) -> bool:
    return manifest.quarantine_state in (None, "", "ready", "READY")


def select_recipe(
    candidates: Sequence[RecipeManifest],
    *,
    intent_id: str,
    language: str,
    framework: str | None,
    repository_signature: str,
    snapshot_facts: SnapshotFacts,
    max_candidates: int = 200,
) -> MatchResult:
    """Return a unique discrete match or a stable non-ready result."""

    if type(max_candidates) is not int or max_candidates <= 0:
        raise AtlasError("invalid_max_candidates")
    if not isinstance(repository_signature, str):
        raise AtlasError("invalid_repository_signature")
    normalized_intent = normalize_intent_id(intent_id)
    if not normalized_intent:
        raise AtlasError("invalid_intent_id")
    normalized_language = _normalise_text(language, code="invalid_language")
    requested_framework = _parse_framework(framework)
    compatible: list[MatchCandidate] = []
    for manifest in _deduplicated(candidates):
        match_class = _classify(
            manifest,
            intent_id=normalized_intent,
            language=normalized_language,
            framework=requested_framework,
            repository_signature=repository_signature,
            facts=snapshot_facts,
        )
        if match_class is not None:
            compatible.append(
                MatchCandidate(manifest, match_class, (match_class.name.casefold(),))
            )
    if not compatible:
        return MatchResult(
            AtlasStatus.NO_VERIFIED_RECIPE,
            reason_codes=("no_compatible_recipe",),
        )

    superseded = {
        recipe_id
        for candidate in compatible
        for recipe_id in candidate.manifest.superseded_ids
        if isinstance(recipe_id, str)
    }
    compatible = [
        candidate
        for candidate in compatible
        if candidate.manifest.recipe_id not in superseded
    ]
    if not compatible:
        return MatchResult(
            AtlasStatus.NO_VERIFIED_RECIPE,
            reason_codes=("all_candidates_superseded",),
        )

    active = [candidate for candidate in compatible if _is_active(candidate.manifest)]
    if not active:
        return MatchResult(
            AtlasStatus.RECIPE_QUARANTINED,
            candidates=tuple(compatible),
            reason_codes=("best_recipe_quarantined",),
        )
    if len(active) > max_candidates:
        return MatchResult(
            AtlasStatus.AMBIGUOUS_MATCH,
            candidates=tuple(sorted(active, key=lambda item: item.manifest.recipe_id))[
                :max_candidates
            ],
            reason_codes=("candidate_limit_exceeded",),
        )

    best_class = min(candidate.match_class for candidate in active)
    active_best = [
        candidate for candidate in active if candidate.match_class == best_class
    ]
    if len(active_best) != 1:
        return MatchResult(
            AtlasStatus.AMBIGUOUS_MATCH,
            candidates=tuple(active_best),
            reason_codes=("equal_match_class",),
        )
    winner = active_best[0]
    return MatchResult(
        AtlasStatus.READY,
        winner=winner,
        candidates=(winner,),
        reason_codes=("unique_match",),
    )
