"""Lazy, invocation-owned runtime adapters.

The process root retains only factories.  This module opens adapters on demand
and keeps every resulting connection or verified snapshot within one UoW.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from .config import RuntimeConfig, RuntimeConfigError

if TYPE_CHECKING:
    from devkit_atlas.models import AtlasError
    from devkit_atlas.service import AtlasService
    from devkit_atlas.store import AtlasStore
    from devkit_continuity.service import ContinuityService
    from devkit_relay.proofs import IntegrationProofResolver
    from devkit_relay.store import RelayStore
    from devkit_runtime.project_checkpoint import ProjectCheckpointRuntime
    from devkit_runtime.relay_runtime import (
        CapabilityBroker,
        RelayReadRuntime,
        RelayRuntime,
    )


@dataclass(frozen=True)
class ToolResultAdapter:
    """Typed bridge to the frozen public ToolResult projectors."""

    def success(self, data: object) -> dict[str, object]:
        from .tool_result import envelope_success

        return envelope_success(data)

    def failure(self, code: str) -> dict[str, object]:
        from .tool_result import envelope_failure

        return envelope_failure(code)

    def project(self, tool_name: str, value: object) -> dict[str, object]:
        from .tool_result import project_tool_result

        return project_tool_result(tool_name, value)


@dataclass(frozen=True)
class RuntimeAdapterFactories:
    """Typed construction seams; each factory must return call-owned state."""

    open_project_checkpoint: Callable[..., object]
    open_atlas_store: Callable[..., object]
    open_continuity: Callable[..., object]
    build_atlas: Callable[..., object]
    build_registry: Callable[..., object]
    open_relay: Callable[..., object]


@dataclass(frozen=True)
class _OwnedAdapter:
    """Bind a non-closeable adapter to the closeable resource it uses."""

    value: object
    closer: object


@dataclass(frozen=True)
class _OwnedClosers:
    """Close a composed adapter's private resources exactly once each."""

    resources: tuple[object, ...]

    def close(self) -> None:
        closed: set[int] = set()
        first_error: Exception | None = None
        for resource in reversed(self.resources):
            if id(resource) in closed:
                continue
            closed.add(id(resource))
            close = getattr(resource, "close", None)
            if not callable(close):
                continue
            try:
                close()
            except Exception as error:
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error


_UNSET = object()


class _ReplayProjectIndex:
    """A poison dependency proving replay never reaches the live project index."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"offline replay touched project index attribute: {name}")


class RuntimeUnitOfWork:
    """One closeable bundle of lazily opened, invocation-scoped adapters."""

    def __init__(
        self,
        *,
        config: RuntimeConfig,
        read_only: bool,
        factories: RuntimeAdapterFactories,
        capability_broker: object | None,
        integration_attestor: object | None,
        tool_results: ToolResultAdapter,
    ) -> None:
        self._config = config
        self._read_only = read_only
        self._factories = factories
        self._capability_broker = capability_broker
        self._integration_attestor = integration_attestor
        self._tool_results = tool_results
        self._opened: list[object] = []
        self._project_checkpoint: object = _UNSET
        self._atlas_store: object = _UNSET
        self._atlas: object = _UNSET
        self._continuity: object = _UNSET
        self._replay_continuity: object = _UNSET
        self._replay_atlas: object = _UNSET
        self._orchestrator_store: object = _UNSET
        self._acceptance_evidence_reader: object = _UNSET
        self._registry: object = _UNSET
        self._relay: object = _UNSET
        self._closed = False

    @property
    def read_only(self) -> bool:
        return self._read_only

    @property
    def project_checkpoint(self) -> ProjectCheckpointRuntime:
        self._assert_open()
        if self._project_checkpoint is _UNSET:
            self._project_checkpoint = self._remember(
                self._factories.open_project_checkpoint(
                    config=self._config,
                    read_only=self._read_only,
                )
            )
        return cast("ProjectCheckpointRuntime", self._project_checkpoint)

    @property
    def atlas_store(self) -> AtlasStore:
        self._assert_open()
        if self._atlas_store is _UNSET:
            self._atlas_store = self._remember(
                self._factories.open_atlas_store(
                    config=self._config,
                    read_only=self._read_only,
                )
            )
        return cast("AtlasStore", self._atlas_store)

    @property
    def atlas(self) -> AtlasService:
        self._assert_open()
        if self._atlas is _UNSET:
            self._atlas = self._remember(
                self._factories.build_atlas(
                    atlas_store=self.atlas_store,
                    project_checkpoint=self.project_checkpoint,
                    acceptance_evidence_reader=self._atlas_acceptance_evidence_reader(),
                )
            )
        return cast("AtlasService", self._atlas)

    def accept_atlas(
        self,
        workflow_id: str,
        code_task_id: str,
        acceptance_id: str,
        ingestion_key: str,
    ) -> object:
        """Project one accepted task and durably drain only its matching outbox row."""

        from devkit_atlas.models import AtlasError
        from devkit_atlas.store import StoreConflictError

        self._assert_open()
        if self._read_only:
            raise RuntimeConfigError("RUNTIME_READ_ONLY")
        marking_outbox = False
        try:
            replay_key = self._continuity_replay_service().find_replay_candidate(
                workflow_id, code_task_id, acceptance_id, ingestion_key
            )
            if replay_key is None:
                projection = self._accept_atlas_live(
                    workflow_id, code_task_id, acceptance_id, ingestion_key
                )
            else:
                projection = self._accept_atlas_replay(
                    replay_key, acceptance_id, ingestion_key
                )
            marking_outbox = True
            self._mark_atlas_acceptance_projected(acceptance_id, ingestion_key)
            return projection
        except StoreConflictError as error:
            atlas_error = AtlasError("ATLAS_EVIDENCE_CONFLICT")
            if not marking_outbox:
                self._record_atlas_acceptance_failure_safely(
                    acceptance_id, ingestion_key, atlas_error
                )
            raise atlas_error from error
        except AtlasError as error:
            atlas_error = self._atlas_acceptance_error(error)
            if not marking_outbox:
                self._record_atlas_acceptance_failure_safely(
                    acceptance_id, ingestion_key, atlas_error
                )
            if atlas_error is error:
                raise
            raise atlas_error from error
        except Exception as error:
            atlas_error = self._continuity_error(error)
            if not marking_outbox:
                self._record_atlas_acceptance_failure_safely(
                    acceptance_id, ingestion_key, atlas_error
                )
            raise atlas_error from error

    def _accept_atlas_live(
        self,
        workflow_id: str,
        code_task_id: str,
        acceptance_id: str,
        ingestion_key: str,
    ) -> object:
        """Retain CP-C's reader-backed first-freeze path only for no candidate."""

        prepared = self.atlas._prepare_accepted_projection(  # noqa: SLF001
            workflow_id, code_task_id, acceptance_id, ingestion_key
        )
        key = self._continuity_key(
            prepared, workflow_id, code_task_id, acceptance_id, ingestion_key
        )
        self._matching_atlas_outbox(acceptance_id, ingestion_key)
        continuity = self._continuity_service()
        attempt = continuity.claim_or_reuse(key)
        if attempt.state == "published":
            published_state = self._published_pointer(
                continuity,
                attempt.key,
                attempt.view_id,
                attempt.fence_epoch,
                expected_attempt=attempt,
            )
            projection = self.atlas._project_prepared_acceptance(prepared)  # noqa: SLF001
            continuity._verify_replay_state(attempt.key, published_state)  # noqa: SLF001
            return projection
        frozen_view, already_published = self._freeze_or_recover(
            continuity, attempt, prepared
        )
        published_state = None
        if already_published:
            published_state = self._published_pointer(
                continuity,
                attempt.key,
                frozen_view.view_id,
                attempt.fence_epoch,
            )
        projection = self.atlas._project_prepared_acceptance(prepared)  # noqa: SLF001
        if already_published:
            continuity._verify_replay_state(attempt.key, published_state)  # noqa: SLF001
        else:
            self._publish_or_recover(continuity, attempt, frozen_view)
        return projection

    def _accept_atlas_replay(
        self, key: object, acceptance_id: str, ingestion_key: str
    ) -> object:
        """Project one structurally verified frozen view without a live reader."""

        from devkit_atlas.models import AtlasError

        replay_continuity = self._continuity_replay_service()
        replay = replay_continuity.materialize_replay(key)
        continuity = replay_continuity
        if replay.attempt.state == "frozen":
            # Recheck through the writer immediately before projecting/publishing;
            # a concurrent pointer winner may only advance to the same view.
            continuity = self._continuity_service()
            replay = continuity.materialize_replay(key)
        if replay.attempt.state not in {"frozen", "published"}:
            raise AtlasError("ATLAS_EVIDENCE_CONFLICT")
        if (
            replay.request.acceptance_id != acceptance_id
            or replay.request.ingestion_key != ingestion_key
        ):
            raise AtlasError("ATLAS_EVIDENCE_CONFLICT")
        # The outbox is intentionally opened only after immutable replay is
        # complete, but before Atlas side effects, and never through the reader.
        self._matching_atlas_outbox(acceptance_id, ingestion_key)
        atlas = self._replay_atlas_service()
        prepared = atlas._prepare_projection_from_request(  # noqa: SLF001
            replay.request, replay.evidence
        )
        if getattr(prepared, "extraction", None) != replay.extraction:
            raise AtlasError("ATLAS_EVIDENCE_CONFLICT")
        replay_state = continuity._prove_materialized_replay(  # noqa: SLF001
            replay.attempt.key,
            replay.attempt,
            replay.view,
        )
        projection = atlas._project_prepared_acceptance(prepared)  # noqa: SLF001
        if replay.attempt.state == "published":
            continuity._verify_replay_state(replay.attempt.key, replay_state)  # noqa: SLF001
        elif replay.attempt.state == "frozen":
            self._publish_or_recover(continuity, replay.attempt, replay.view)
        return projection

    @property
    def registry(self) -> object:
        self._assert_open()
        if self._registry is _UNSET:
            self._registry = self._remember(
                self._factories.build_registry(
                    atlas_store=self.atlas_store,
                    project_checkpoint=self.project_checkpoint,
                )
            )
        return self._registry

    @property
    def relay(self) -> RelayReadRuntime | RelayRuntime:
        self._assert_open()
        if self._relay is _UNSET:
            self._relay = self._remember(
                self._factories.open_relay(
                    config=self._config,
                    read_only=self._read_only,
                    capability_broker=self._capability_broker,
                    integration_attestor=self._integration_attestor,
                )
            )
        return cast("RelayReadRuntime | RelayRuntime", self._relay)

    @property
    def tool_results(self) -> ToolResultAdapter:
        self._assert_open()
        return self._tool_results

    def close(self) -> None:
        """Close every opened resource once, in reverse dependency order."""

        if self._closed:
            return
        self._closed = True
        closed_ids: set[int] = set()
        first_error: Exception | None = None
        for resource in reversed(self._opened):
            if id(resource) in closed_ids:
                continue
            closed_ids.add(id(resource))
            close = getattr(resource, "close", None)
            if not callable(close):
                continue
            try:
                close()
            except Exception as error:
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error

    def __enter__(self) -> RuntimeUnitOfWork:
        self._assert_open()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _remember(self, opened: object) -> object:
        if isinstance(opened, _OwnedAdapter):
            self._opened.append(opened.closer)
            return opened.value
        self._opened.append(opened)
        return opened

    def _assert_open(self) -> None:
        if self._closed:
            raise RuntimeConfigError("RUNTIME_CLOSED")

    def _atlas_acceptance_evidence_reader(self) -> object | None:
        """Build immutable evidence reconstruction only for one RW Atlas call."""

        if self._read_only:
            return None
        if self._acceptance_evidence_reader is _UNSET:
            from devkit_atlas.receipts import ReceiptRepository
            from devkit_runtime.atlas_acceptance import (
                ProductionAcceptanceEvidenceReader,
            )
            from orchestrator.service import OrchestratorService

            project_checkpoint = self.project_checkpoint
            if self._orchestrator_store is _UNSET:
                self._orchestrator_store = self._remember(
                    _open_orchestrator_store_rw(self._config)
                )
            receipts = ReceiptRepository(self._config.data_root)
            orchestrator = OrchestratorService(
                self._orchestrator_store,
                index_service=project_checkpoint.project_index,
                checkpoint_service=project_checkpoint.checkpoint_service,
                receipt_repository=receipts,
            )
            self._acceptance_evidence_reader = ProductionAcceptanceEvidenceReader(
                orchestrator,
                project_checkpoint.project_index,
                project_checkpoint.checkpoint_service,
                receipts,
            )
        return self._acceptance_evidence_reader

    def _continuity_service(self) -> ContinuityService:
        """Open the private writer only when a write Atlas acceptance needs it."""

        self._assert_open()
        if self._read_only:
            raise RuntimeConfigError("RUNTIME_READ_ONLY")
        if self._continuity is _UNSET:
            self._continuity = self._remember(
                self._factories.open_continuity(
                    config=self._config,
                    read_only=False,
                )
            )
        return cast("ContinuityService", self._continuity)

    def _continuity_replay_service(self) -> ContinuityService:
        """Open an independently read-only Continuity view before any live reader."""

        self._assert_open()
        if self._read_only:
            raise RuntimeConfigError("RUNTIME_READ_ONLY")
        if self._replay_continuity is _UNSET:
            self._replay_continuity = self._remember(
                self._factories.open_continuity(
                    config=self._config,
                    read_only=True,
                )
            )
        return cast("ContinuityService", self._replay_continuity)

    def _replay_atlas_service(self) -> object:
        """Construct Atlas's prepared/project seam with a poison live index."""

        self._assert_open()
        if self._replay_atlas is _UNSET:
            # Test seams may provide a preconstructed private Atlas object.  In
            # production this branch bypasses the live factory entirely.
            if self._atlas is not _UNSET:
                self._replay_atlas = self._atlas
            else:
                from devkit_atlas import ASSET_ROOT
                from devkit_atlas.recipes import BundledRecipeLoader
                from devkit_atlas.service import AtlasService

                self._replay_atlas = AtlasService(
                    cast("AtlasStore", self.atlas_store),
                    BundledRecipeLoader(ASSET_ROOT),
                    _ReplayProjectIndex(),
                    acceptance_evidence_reader=None,
                )
        return self._replay_atlas

    def _orchestrator_store_for_outbox(self) -> object:
        """Open the exact outbox authority without constructing a live reader."""

        self._assert_open()
        if self._orchestrator_store is _UNSET:
            self._orchestrator_store = self._remember(
                _open_orchestrator_store_rw(self._config)
            )
        return self._orchestrator_store

    @staticmethod
    def _continuity_key(
        prepared: object,
        workflow_id: str,
        code_task_id: str,
        acceptance_id: str,
        ingestion_key: str,
    ) -> object:
        """Bind continuity only to the exact reader-produced seven-field key."""

        from devkit_atlas.models import AtlasError
        from devkit_continuity.models import ContinuityError, ContinuityKey

        request = getattr(prepared, "request", None)
        try:
            if (
                request is None
                or request.workflow_id != workflow_id
                or request.code_task_id != code_task_id
                or request.acceptance_id != acceptance_id
                or request.ingestion_key != ingestion_key
            ):
                raise AtlasError("ATLAS_EVIDENCE_CONFLICT")
            return ContinuityKey(
                request.workflow_id,
                request.code_task_id,
                request.code_task_version,
                request.acceptance_id,
                request.ingestion_key,
                request.payload_hash,
                request.evidence_binding_hash,
            )
        except AtlasError:
            raise
        except (AttributeError, ContinuityError, TypeError, ValueError) as error:
            raise AtlasError("ATLAS_EVIDENCE_CONFLICT") from error

    @staticmethod
    def _published_pointer(
        continuity: ContinuityService,
        key: object,
        view_id: object,
        fence_epoch: object,
        *,
        expected_attempt: object | None = None,
    ) -> object:
        """Require an exact, physically valid v2 published replay state."""

        from devkit_atlas.models import AtlasError

        try:
            state = continuity._prove_published_replay(  # noqa: SLF001
                key,
                view_id,
                fence_epoch,
                expected_attempt=expected_attempt,
            )
            pointer = getattr(state, "pointer", None)
            if pointer is None:
                raise AtlasError("ATLAS_EVIDENCE_CONFLICT")
            return state
        except AtlasError:
            raise
        except Exception as error:
            raise RuntimeUnitOfWork._continuity_error(error) from error

    def _freeze_or_recover(
        self, continuity: ContinuityService, attempt: object, prepared: object
    ) -> tuple[object, bool]:
        """Freeze once, retaining only a proven same-fence concurrent result."""

        from devkit_continuity.store import ContinuityStoreError

        try:
            return continuity.freeze(attempt, prepared.request, prepared.evidence), False
        except ContinuityStoreError as error:
            if str(error) != "CONTINUITY_STATE_CONFLICT":
                raise
            current = continuity.store.current_attempt(attempt.key)
            if (
                current is not None
                and current.key == attempt.key
                and current.fence_epoch == attempt.fence_epoch
                and current.state == "frozen"
            ):
                try:
                    return (
                        continuity.freeze(current, prepared.request, prepared.evidence),
                        False,
                    )
                except ContinuityStoreError as retry_error:
                    if str(retry_error) != "CONTINUITY_STATE_CONFLICT":
                        raise
                    return self._recover_published_freeze_race(
                        continuity, attempt, prepared
                    )
            return self._recover_published_freeze_race(
                continuity, attempt, prepared
            )

    def _recover_published_freeze_race(
        self,
        continuity: ContinuityService,
        attempt: object,
        prepared: object,
    ) -> tuple[object, bool]:
        """Resume a race only after proving its exact published deterministic view."""

        frozen_view = continuity._typed_view(  # noqa: SLF001
            attempt.key, prepared.request, prepared.evidence
        )
        self._published_pointer(
            continuity,
            attempt.key,
            frozen_view.view_id,
            attempt.fence_epoch,
        )
        return frozen_view, True

    def _publish_or_recover(
        self, continuity: ContinuityService, attempt: object, frozen_view: object
    ) -> object:
        """Recover only a proven same-view publication race before failure handling."""

        from devkit_continuity.store import ContinuityStoreError

        try:
            continuity.publish(attempt, frozen_view)
        except ContinuityStoreError as error:
            if str(error) != "CONTINUITY_STATE_CONFLICT":
                raise
        return self._published_pointer(
            continuity,
            attempt.key,
            frozen_view.view_id,
            attempt.fence_epoch,
        )

    @staticmethod
    def _continuity_error(error: Exception) -> AtlasError:
        """Map private state failures without carrying private details public."""

        from devkit_atlas.models import AtlasError
        from devkit_continuity.cas import ContinuityCasError
        from devkit_continuity.models import ContinuityError
        from devkit_continuity.store import ContinuityStoreError

        if isinstance(error, AtlasError):
            return error
        if isinstance(error, (ContinuityError, ContinuityStoreError, ContinuityCasError)):
            if str(error) in {
                "CONTINUITY_CAS_UNAVAILABLE",
                "CONTINUITY_STORE_UNPREPARED",
                "CONTINUITY_STORE_READ_ONLY",
            }:
                return AtlasError("ATLAS_EVIDENCE_UNAVAILABLE")
            return AtlasError("ATLAS_EVIDENCE_CONFLICT")
        if isinstance(error, (OSError, sqlite3.Error)):
            return AtlasError("ATLAS_EVIDENCE_UNAVAILABLE")
        return AtlasError("ATLAS_EVIDENCE_UNAVAILABLE")

    @staticmethod
    def _atlas_acceptance_error(error: AtlasError) -> AtlasError:
        """Translate private Atlas seam failures before public UoW handling."""

        from devkit_atlas.models import AtlasError

        if error.code in {
            "ATLAS_EVIDENCE_UNAVAILABLE",
            "ATLAS_EVIDENCE_CONFLICT",
        }:
            return error
        if error.code == "acceptance_evidence_unavailable":
            return AtlasError("ATLAS_EVIDENCE_UNAVAILABLE")
        return AtlasError("ATLAS_EVIDENCE_CONFLICT")

    def _matching_atlas_outbox(
        self, acceptance_id: str, ingestion_key: str
    ) -> object:
        from devkit_atlas.models import AtlasError

        store = self._orchestrator_store_for_outbox()
        outbox = store.atlas_outbox_for_acceptance(acceptance_id)
        if (
            outbox is None
            or outbox.acceptance_id != acceptance_id
            or outbox.ingestion_key != ingestion_key
            or outbox.payload_hash != ingestion_key
            or outbox.payload_hash != acceptance_id
        ):
            raise AtlasError("ATLAS_EVIDENCE_CONFLICT")
        return outbox

    def _mark_atlas_acceptance_projected(
        self, acceptance_id: str, ingestion_key: str
    ) -> None:
        from devkit_atlas.models import AtlasError
        from orchestrator.models import AtlasOutboxState
        from orchestrator.store import AtlasOutboxTransitionError

        outbox = self._matching_atlas_outbox(acceptance_id, ingestion_key)
        if outbox.state is AtlasOutboxState.PROJECTED:
            return
        if outbox.state is not AtlasOutboxState.PENDING:
            raise AtlasError("ATLAS_EVIDENCE_CONFLICT")
        try:
            updated = self._orchestrator_store.mark_atlas_outbox_projected(
                ingestion_key
            )
        except AtlasOutboxTransitionError as error:
            raise AtlasError("ATLAS_EVIDENCE_CONFLICT") from error
        if (
            updated.state is not AtlasOutboxState.PROJECTED
            or updated.acceptance_id != acceptance_id
            or updated.ingestion_key != ingestion_key
        ):
            raise AtlasError("ATLAS_EVIDENCE_CONFLICT")

    def _record_atlas_acceptance_failure(
        self, acceptance_id: str, ingestion_key: str, error: AtlasError
    ) -> None:
        """Persist only retryable/untrusted evidence outcomes for the exact key."""

        from orchestrator.models import AtlasOutboxState
        from orchestrator.store import (
            AtlasOutboxAttemptLimitError,
            AtlasOutboxTransitionError,
        )

        outbox = self._matching_atlas_outbox(acceptance_id, ingestion_key)
        if error.code == "ATLAS_EVIDENCE_UNAVAILABLE":
            if outbox.state is not AtlasOutboxState.PENDING:
                return
            try:
                self._orchestrator_store.mark_atlas_outbox_retry(
                    ingestion_key,
                    error_code=error.code,
                    reason_codes=(error.code,),
                )
            except (AtlasOutboxAttemptLimitError, AtlasOutboxTransitionError):
                return
            return
        if error.code == "ATLAS_EVIDENCE_CONFLICT":
            if outbox.state is not AtlasOutboxState.PENDING:
                return
            try:
                self._orchestrator_store.mark_atlas_outbox_quarantined(
                    ingestion_key,
                    error_code=error.code,
                    reason_codes=(error.code,),
                )
            except AtlasOutboxTransitionError:
                return

    def _record_atlas_acceptance_failure_safely(
        self, acceptance_id: str, ingestion_key: str, error: AtlasError
    ) -> None:
        """Keep the original evidence failure when its outbox is untrusted."""

        try:
            self._record_atlas_acceptance_failure(acceptance_id, ingestion_key, error)
        except Exception:
            return


def open_runtime_uow(
    *,
    config: RuntimeConfig,
    read_only: bool,
    capability_broker: object | None = None,
    integration_attestor: object | None = None,
    factories: RuntimeAdapterFactories | None = None,
    tool_results: ToolResultAdapter | None = None,
) -> RuntimeUnitOfWork:
    """Create one lazy UoW; this function itself opens no database."""

    return RuntimeUnitOfWork(
        config=config,
        read_only=read_only,
        factories=factories or DEFAULT_RUNTIME_ADAPTER_FACTORIES,
        capability_broker=capability_broker,
        integration_attestor=integration_attestor,
        tool_results=tool_results or ToolResultAdapter(),
    )


def _open_project_checkpoint(*, config: RuntimeConfig, read_only: bool) -> object:
    from .project_checkpoint import (
        open_project_checkpoint_ro,
        open_project_checkpoint_rw,
    )

    opener = open_project_checkpoint_ro if read_only else open_project_checkpoint_rw
    return opener(
        config.project_index_database,
        config.checkpoint_cas_root,
        scratch_root=config.scratch_root,
    )


def _atlas_cas_root(config: RuntimeConfig) -> Path:
    return config.atlas_database.parent / "cas"


def _open_atlas_store(*, config: RuntimeConfig, read_only: bool) -> object:
    from devkit_atlas.store import AtlasStore

    if read_only:
        return AtlasStore.open_readonly(
            config.atlas_database,
            _atlas_cas_root(config),
            scratch_root=config.scratch_root,
        )
    return _open_atlas_store_rw(config)


def _open_atlas_store_rw(config: RuntimeConfig) -> object:
    """Open an existing Atlas schema without a migration or WAL pragma."""

    from devkit_atlas.store import (
        AtlasStore,
        StoreConflictError,
        _capture_cas_directory_identities,
    )

    verified = AtlasStore.open_readonly(
        config.atlas_database,
        _atlas_cas_root(config),
        scratch_root=config.scratch_root,
    )
    verified.close()
    connection: sqlite3.Connection | None = None
    try:
        database = Path(config.atlas_database).absolute()
        connection = sqlite3.connect(
            database.as_uri() + "?mode=rw",
            uri=True,
            timeout=30.0,
        )
        connection.execute("PRAGMA foreign_keys=ON")
        row = connection.execute(
            "SELECT value FROM atlas_metadata WHERE key='schema_version'"
        ).fetchone()
        if row is None or row[0] != "1":
            raise StoreConflictError()
        instance = AtlasStore.__new__(AtlasStore)
        instance._database_path = database
        instance._cas_root = _atlas_cas_root(config)
        instance._cas_directory_identities = _capture_cas_directory_identities(
            instance._cas_root
        )
        instance._conn = connection
        instance._verified_sqlite_snapshot = None
        connection = None
        return instance
    except Exception:
        if connection is not None:
            connection.close()
        raise


def _open_continuity(*, config: RuntimeConfig, read_only: bool) -> object:
    """Open only an already-prepared private Continuity store and CAS."""

    from devkit_continuity.cas import ContinuityCas
    from devkit_continuity.service import ContinuityService
    from devkit_continuity.store import ContinuityStore

    store = (
        ContinuityStore.open_readonly(
            config.continuity_database,
            config.continuity_cas_root,
            config.scratch_root,
        )
        if read_only
        else ContinuityStore.open_readwrite(
            config.continuity_database,
            config.continuity_cas_root,
            config.scratch_root,
        )
    )
    cas: ContinuityCas | None = None
    try:
        cas = ContinuityCas.open_prepared(
            config.continuity_cas_root,
            config.scratch_root,
            read_only=read_only,
        )
        return _OwnedAdapter(
            value=ContinuityService(store, cas),
            closer=_OwnedClosers((store, cas)),
        )
    except Exception:
        if cas is not None:
            cas.close()
        store.close()
        raise


def _open_orchestrator_store_rw(config: RuntimeConfig) -> object:
    """Open one validated existing orchestrator store without schema creation."""

    from orchestrator.store import SQLiteStore

    connection: sqlite3.Connection | None = None
    try:
        database = Path(config.orchestrator_database).absolute()
        connection = sqlite3.connect(
            database.as_uri() + "?mode=rw",
            uri=True,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        SQLiteStore.validate_prepared_connection(connection)
        store = SQLiteStore.from_prepared_connection(connection)
        connection = None
        return store
    finally:
        if connection is not None:
            connection.close()


def _build_atlas(
    *,
    atlas_store: object,
    project_checkpoint: object,
    acceptance_evidence_reader: object | None = None,
) -> object:
    from devkit_atlas import ASSET_ROOT
    from devkit_atlas.recipes import BundledRecipeLoader
    from devkit_atlas.service import AtlasService

    project_index = getattr(project_checkpoint, "project_index", None)
    if project_index is None:
        raise RuntimeConfigError("RUNTIME_DEPENDENCY_UNAVAILABLE")
    return AtlasService(
        cast("AtlasStore", atlas_store),
        BundledRecipeLoader(ASSET_ROOT),
        project_index,
        acceptance_evidence_reader=acceptance_evidence_reader,
    )


def _build_registry(*, atlas_store: object, project_checkpoint: object) -> object:
    from .relay_runtime import ProductionRegistryResolver

    project_index = getattr(project_checkpoint, "project_index", None)
    if project_index is None:
        raise RuntimeConfigError("RUNTIME_DEPENDENCY_UNAVAILABLE")
    return ProductionRegistryResolver(project_index, atlas_store)


def _open_relay(
    *,
    config: RuntimeConfig,
    read_only: bool,
    capability_broker: object | None,
    integration_attestor: object | None,
) -> object:
    from .relay_runtime import (
        RelayCapabilitySecretProvider,
        RelayRuntime,
        open_relay_ro,
    )

    if read_only:
        return open_relay_ro(config.relay_database, scratch_root=config.scratch_root)
    store = _open_relay_store_rw(config)
    try:
        runtime = RelayRuntime.from_secret_provider(
            store,
            capability_secret_provider=RelayCapabilitySecretProvider(
                config.relay_capability_key
            ),
            capability_broker=cast("CapabilityBroker | None", capability_broker),
            integration_proof_resolver=cast(
                "IntegrationProofResolver | None", integration_attestor
            ),
        )
    except Exception:
        store.close()
        raise
    return _OwnedAdapter(value=runtime, closer=store)


def _open_relay_store_rw(config: RuntimeConfig) -> RelayStore:
    """Validate first, then open an existing Relay schema without migration."""

    from devkit_relay.store import RelayStore

    from .relay_runtime import RelayRuntimeError, open_relay_ro

    verified = open_relay_ro(config.relay_database, scratch_root=config.scratch_root)
    verified.close()
    connection: sqlite3.Connection | None = None
    try:
        database = Path(config.relay_database).absolute()
        connection = sqlite3.connect(
            database.as_uri() + "?mode=rw",
            uri=True,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        store = RelayStore.__new__(RelayStore)
        store._database = str(database)
        store._connection = connection
        store._assert_schema_compatible()
        store._assert_schema_shape()
        connection = None
        return store
    except sqlite3.Error as error:
        if connection is not None:
            connection.close()
        raise RelayRuntimeError("RELAY_STORAGE_ERROR") from error
    except Exception:
        if connection is not None:
            connection.close()
        raise


DEFAULT_RUNTIME_ADAPTER_FACTORIES = RuntimeAdapterFactories(
    open_project_checkpoint=_open_project_checkpoint,
    open_atlas_store=_open_atlas_store,
    open_continuity=_open_continuity,
    build_atlas=_build_atlas,
    build_registry=_build_registry,
    open_relay=_open_relay,
)
