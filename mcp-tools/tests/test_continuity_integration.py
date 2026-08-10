"""Private UoW continuity fencing contracts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

import devkit_runtime.uow as uow_module
from devkit_atlas.models import AtlasError
from devkit_atlas.service import AcceptedAtlasProjectionRequest
from devkit_atlas.store import StoreConflictError
from devkit_continuity.cas import ContinuityCasError
from devkit_continuity.models import ContinuityAttempt, ContinuityKey, ContinuityPointer
from devkit_continuity.store import ContinuityStoreError
from devkit_runtime.config import RuntimeConfig
from devkit_runtime.uow import (
    _UNSET,
    RuntimeAdapterFactories,
    RuntimeUnitOfWork,
    ToolResultAdapter,
)
from orchestrator.models import AtlasOutboxState

_HASH = "sha256:" + "a" * 64
_VIEW = "sha256:" + "b" * 64
_OTHER_VIEW = "sha256:" + "c" * 64
_RECEIPT = "sha256:" + "d" * 64


@dataclass
class _Outbox:
    acceptance_id: str
    ingestion_key: str
    payload_hash: str
    state: AtlasOutboxState = AtlasOutboxState.PENDING
    retry_count: int = 0
    quarantine_count: int = 0


class _Orchestrator:
    def __init__(self, outbox: _Outbox) -> None:
        self.outbox = outbox
        self.unrelated = _Outbox("other-acceptance", "other-ingestion", _HASH)
        self.fail_mark_once = False
        self.lookup_error: Exception | None = None

    def atlas_outbox_for_acceptance(self, acceptance_id: str) -> _Outbox | None:
        if self.lookup_error is not None:
            raise self.lookup_error
        return self.outbox if acceptance_id == self.outbox.acceptance_id else None

    def mark_atlas_outbox_projected(self, ingestion_key: str) -> _Outbox:
        assert ingestion_key == self.outbox.ingestion_key
        if self.fail_mark_once:
            self.fail_mark_once = False
            raise RuntimeError("mark interrupted")
        self.outbox.state = AtlasOutboxState.PROJECTED
        return self.outbox

    def mark_atlas_outbox_retry(self, ingestion_key: str, **_kwargs: object) -> _Outbox:
        assert ingestion_key == self.outbox.ingestion_key
        self.outbox.retry_count += 1
        return self.outbox

    def mark_atlas_outbox_quarantined(
        self, ingestion_key: str, **_kwargs: object
    ) -> _Outbox:
        assert ingestion_key == self.outbox.ingestion_key
        self.outbox.quarantine_count += 1
        self.outbox.state = AtlasOutboxState.QUARANTINED
        return self.outbox


class _Continuity:
    def __init__(self, key: ContinuityKey) -> None:
        self.store = self
        self.current = ContinuityAttempt(key, 1, "claimed", None, None)
        self.pointer: ContinuityPointer | None = None
        self.freeze_calls = 0
        self.publish_calls = 0
        self.freeze_error: Exception | None = None
        self.freeze_race: str | None = None
        self.typed_view_calls = 0
        self.race_mode: str | None = None
        self.publish_error: Exception | None = None
        self.replay_enabled = False
        self.replay_candidate_error: Exception | None = None
        self.replay_materialize_calls = 0
        self.replay_request: object | None = None
        self.replay_evidence: object | None = None
        self.replay_extraction: object | None = None

    def find_replay_candidate(
        self,
        workflow_id: str,
        code_task_id: str,
        acceptance_id: str,
        ingestion_key: str,
    ) -> ContinuityKey | None:
        if self.replay_candidate_error is not None:
            raise self.replay_candidate_error
        if not self.replay_enabled:
            return None
        assert (
            workflow_id,
            code_task_id,
            acceptance_id,
            ingestion_key,
        ) == (
            self.current.key.workflow_id,
            self.current.key.code_task_id,
            self.current.key.acceptance_id,
            self.current.key.ingestion_key,
        )
        return self.current.key

    def materialize_replay(self, key: ContinuityKey) -> object:
        assert self.replay_enabled and key == self.current.key
        self.replay_materialize_calls += 1
        assert self.replay_request is not None
        assert self.replay_evidence is not None
        assert self.replay_extraction is not None
        return SimpleNamespace(
            attempt=self.current,
            view=SimpleNamespace(view_id=_VIEW),
            request=self.replay_request,
            evidence=self.replay_evidence,
            extraction=self.replay_extraction,
        )

    def claim_or_reuse(self, key: ContinuityKey) -> ContinuityAttempt:
        assert key == self.current.key
        return self.current

    def freeze(self, attempt: ContinuityAttempt, _request: object, _evidence: object) -> object:
        self.freeze_calls += 1
        if self.freeze_race == "frozen":
            self.freeze_race = None
            self.current = ContinuityAttempt(
                attempt.key, attempt.fence_epoch, "frozen", _VIEW, _RECEIPT
            )
            raise ContinuityStoreError("CONTINUITY_STATE_CONFLICT")
        if self.freeze_race == "frozen_then_published":
            if self.freeze_calls == 1:
                self.current = ContinuityAttempt(
                    attempt.key, attempt.fence_epoch, "frozen", _VIEW, _RECEIPT
                )
                raise ContinuityStoreError("CONTINUITY_STATE_CONFLICT")
            if self.freeze_calls == 2:
                self.freeze_race = None
                self._publish(_VIEW)
                raise ContinuityStoreError("CONTINUITY_STATE_CONFLICT")
        if self.freeze_race == "published":
            self.freeze_race = None
            self._publish(_VIEW)
            raise ContinuityStoreError("CONTINUITY_STATE_CONFLICT")
        if self.freeze_race == "published_different":
            self.freeze_race = None
            self._publish(_OTHER_VIEW)
            raise ContinuityStoreError("CONTINUITY_STATE_CONFLICT")
        assert attempt == self.current
        if self.freeze_error is not None:
            raise self.freeze_error
        if self.current.state == "claimed":
            self.current = ContinuityAttempt(
                attempt.key, attempt.fence_epoch, "frozen", _VIEW, _RECEIPT
            )
        return SimpleNamespace(view_id=_VIEW)

    def _typed_view(self, key: ContinuityKey, _request: object, _evidence: object) -> object:
        assert key == self.current.key
        self.typed_view_calls += 1
        return SimpleNamespace(view_id=_VIEW)

    def publish(self, attempt: ContinuityAttempt, view: object) -> ContinuityPointer:
        assert attempt.key == self.current.key
        assert view.view_id == _VIEW
        self.publish_calls += 1
        if self.publish_error is not None:
            error = self.publish_error
            self.publish_error = None
            raise error
        if self.race_mode == "same":
            self._publish(_VIEW)
            raise ContinuityStoreError("CONTINUITY_STATE_CONFLICT")
        if self.race_mode == "different":
            self._publish(_OTHER_VIEW)
            raise ContinuityStoreError("CONTINUITY_STATE_CONFLICT")
        self._publish(_VIEW)
        assert self.pointer is not None
        return self.pointer

    def _publish(self, view_id: str) -> None:
        self.current = ContinuityAttempt(
            self.current.key,
            self.current.fence_epoch,
            "published",
            view_id,
            _RECEIPT,
        )
        self.pointer = ContinuityPointer(
            self.current.key.workflow_id,
            self.current.key.code_task_id,
            self.current.key.code_task_version,
            view_id,
            1,
            self.current.fence_epoch,
        )

    def current_attempt(self, key: ContinuityKey) -> ContinuityAttempt | None:
        return self.current if key == self.current.key else None

    def pointer_for(self, key: ContinuityKey) -> ContinuityPointer | None:
        return self.pointer if key == self.current.key else None


class _Atlas:
    def __init__(self, request: AcceptedAtlasProjectionRequest) -> None:
        self.prepared = SimpleNamespace(request=request, evidence=object())
        self.prepare_calls = 0
        self.project_calls = 0
        self.prepare_error: AtlasError | None = None
        self.project_error: Exception | None = None
        self.forbid_live_prepare = False
        self.replay_prepare_calls = 0

    def accept(self, *_args: object) -> object:
        raise AssertionError("UoW must not re-enter public Atlas.accept after freeze")

    def _prepare_accepted_projection(self, *identifiers: str) -> object:
        if self.forbid_live_prepare:
            raise AssertionError("replay must not open the live evidence reader")
        assert identifiers == (
            self.prepared.request.workflow_id,
            self.prepared.request.code_task_id,
            self.prepared.request.acceptance_id,
            self.prepared.request.ingestion_key,
        )
        self.prepare_calls += 1
        if self.prepare_error is not None:
            raise self.prepare_error
        return self.prepared

    def _prepare_projection_from_request(self, request: object, evidence: object) -> object:
        self.replay_prepare_calls += 1
        assert request is self.prepared.request
        assert evidence is self.prepared.evidence
        return self.prepared

    def _project_prepared_acceptance(self, prepared: object) -> object:
        assert prepared is self.prepared
        self.project_calls += 1
        if self.project_error is not None:
            raise self.project_error
        return {"projection": self.project_calls}


def _request() -> AcceptedAtlasProjectionRequest:
    return AcceptedAtlasProjectionRequest.create(
        workflow_id="workflow",
        code_task_id="code-task",
        code_task_version=1,
        input_snapshot_id="input",
        output_snapshot_id="output",
        indexed_diff_hash=_HASH,
        intent_id="intent",
        language="python",
        framework="",
        checkpoint_id="checkpoint",
        checkpoint_hash=_HASH,
        output_query_trace_id="query",
        verification_artifact_hashes=(_HASH,),
        execution_receipt_ids=(_HASH,),
    )


def _uow(tmp_path: Path) -> tuple[RuntimeUnitOfWork, _Atlas, _Continuity, _Orchestrator]:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    config = RuntimeConfig.load(
        environ={"PLUGIN_DATA": str(tmp_path / "data"), "CODEX_TASK_TEMP": str(scratch)}
    )
    request = _request()
    key = ContinuityKey(
        request.workflow_id,
        request.code_task_id,
        request.code_task_version,
        request.acceptance_id,
        request.ingestion_key,
        request.payload_hash,
        request.evidence_binding_hash,
    )
    atlas = _Atlas(request)
    continuity = _Continuity(key)
    continuity.replay_request = request
    continuity.replay_extraction = object()
    atlas.prepared.extraction = continuity.replay_extraction
    continuity.replay_evidence = atlas.prepared.evidence
    outbox = _Outbox(request.acceptance_id, request.ingestion_key, request.payload_hash)
    orchestrator = _Orchestrator(outbox)
    factories = RuntimeAdapterFactories(
        open_project_checkpoint=lambda **_kwargs: object(),
        open_atlas_store=lambda **_kwargs: object(),
        open_continuity=lambda **_kwargs: continuity,
        build_atlas=lambda **_kwargs: atlas,
        build_registry=lambda **_kwargs: object(),
        open_relay=lambda **_kwargs: object(),
    )
    uow = RuntimeUnitOfWork(
        config=config,
        read_only=False,
        factories=factories,
        capability_broker=None,
        integration_attestor=None,
        tool_results=ToolResultAdapter(),
    )
    uow._atlas = atlas  # noqa: SLF001 - isolate UoW orchestration only
    uow._orchestrator_store = orchestrator  # noqa: SLF001 - isolated outbox seam
    return uow, atlas, continuity, orchestrator


def test_same_view_publish_race_recovers_before_generic_conflict_quarantine(
    tmp_path: Path,
) -> None:
    uow, atlas, continuity, orchestrator = _uow(tmp_path)
    continuity.race_mode = "same"
    request = atlas.prepared.request

    projection = uow.accept_atlas(
        request.workflow_id,
        request.code_task_id,
        request.acceptance_id,
        request.ingestion_key,
    )

    assert projection == {"projection": 1}
    assert continuity.current.state == "published"
    assert orchestrator.outbox.state is AtlasOutboxState.PROJECTED
    assert orchestrator.outbox.quarantine_count == 0


def test_matching_outbox_opens_reader_free_authority_when_not_yet_initialized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uow, atlas, _continuity, orchestrator = _uow(tmp_path)
    request = atlas.prepared.request
    uow._orchestrator_store = _UNSET  # noqa: SLF001 - exercise lazy authority seam

    monkeypatch.setattr(uow_module, "_open_orchestrator_store_rw", lambda _config: orchestrator)

    def fail_reader() -> object:
        raise AssertionError("outbox lookup must not construct a live reader")

    uow._atlas_acceptance_evidence_reader = fail_reader  # type: ignore[method-assign]  # noqa: SLF001

    assert uow._matching_atlas_outbox(  # noqa: SLF001 - exact private contract
        request.acceptance_id, request.ingestion_key
    ) is orchestrator.outbox


def test_different_view_publish_race_quarantines_the_exact_pending_outbox(
    tmp_path: Path,
) -> None:
    uow, atlas, continuity, orchestrator = _uow(tmp_path)
    continuity.race_mode = "different"
    request = atlas.prepared.request

    with pytest.raises(AtlasError) as raised:
        uow.accept_atlas(
            request.workflow_id,
            request.code_task_id,
            request.acceptance_id,
            request.ingestion_key,
        )

    assert raised.value.code == "ATLAS_EVIDENCE_CONFLICT"
    assert orchestrator.outbox.state is AtlasOutboxState.QUARANTINED
    assert orchestrator.outbox.quarantine_count == 1


def test_freeze_availability_failure_leaves_atlas_unprojected_and_outbox_pending(
    tmp_path: Path,
) -> None:
    uow, atlas, continuity, orchestrator = _uow(tmp_path)
    continuity.freeze_error = ContinuityCasError("CONTINUITY_CAS_UNAVAILABLE")
    request = atlas.prepared.request

    with pytest.raises(AtlasError) as raised:
        uow.accept_atlas(
            request.workflow_id,
            request.code_task_id,
            request.acceptance_id,
            request.ingestion_key,
        )

    assert raised.value.code == "ATLAS_EVIDENCE_UNAVAILABLE"
    assert atlas.project_calls == 0
    assert orchestrator.outbox.state is AtlasOutboxState.PENDING
    assert orchestrator.outbox.retry_count == 1


def test_private_atlas_evidence_failure_is_mapped_before_outbox_retry(
    tmp_path: Path,
) -> None:
    uow, atlas, _continuity, orchestrator = _uow(tmp_path)
    atlas.prepare_error = AtlasError("acceptance_evidence_unavailable")
    request = atlas.prepared.request

    with pytest.raises(AtlasError) as raised:
        uow.accept_atlas(
            request.workflow_id,
            request.code_task_id,
            request.acceptance_id,
            request.ingestion_key,
        )

    assert raised.value.code == "ATLAS_EVIDENCE_UNAVAILABLE"
    assert orchestrator.outbox.state is AtlasOutboxState.PENDING
    assert orchestrator.outbox.retry_count == 1


def test_outbox_lookup_error_cannot_replace_a_mapped_atlas_failure(
    tmp_path: Path,
) -> None:
    uow, atlas, _continuity, orchestrator = _uow(tmp_path)
    atlas.prepare_error = AtlasError("acceptance_evidence_unavailable")
    orchestrator.lookup_error = RuntimeError("outbox lookup probe")
    request = atlas.prepared.request

    with pytest.raises(AtlasError) as raised:
        uow.accept_atlas(
            request.workflow_id,
            request.code_task_id,
            request.acceptance_id,
            request.ingestion_key,
        )

    assert raised.value.code == "ATLAS_EVIDENCE_UNAVAILABLE"
    assert "probe" not in str(raised.value)


def test_same_fence_freeze_race_reuses_the_deterministic_frozen_view(
    tmp_path: Path,
) -> None:
    uow, atlas, continuity, orchestrator = _uow(tmp_path)
    continuity.freeze_race = "frozen"
    request = atlas.prepared.request

    projection = uow.accept_atlas(
        request.workflow_id,
        request.code_task_id,
        request.acceptance_id,
        request.ingestion_key,
    )

    assert projection == {"projection": 1}
    assert continuity.freeze_calls == 2
    assert continuity.current.state == "published"
    assert orchestrator.outbox.state is AtlasOutboxState.PROJECTED
    assert orchestrator.outbox.quarantine_count == 0


def test_same_fence_published_freeze_race_requires_matching_view_and_pointer(
    tmp_path: Path,
) -> None:
    uow, atlas, continuity, orchestrator = _uow(tmp_path)
    continuity.freeze_race = "published"
    request = atlas.prepared.request

    projection = uow.accept_atlas(
        request.workflow_id,
        request.code_task_id,
        request.acceptance_id,
        request.ingestion_key,
    )

    assert projection == {"projection": 1}
    assert continuity.typed_view_calls == 1
    assert continuity.publish_calls == 0
    assert continuity.current.state == "published"
    assert orchestrator.outbox.state is AtlasOutboxState.PROJECTED
    assert orchestrator.outbox.quarantine_count == 0


def test_same_fence_second_freeze_race_recovers_one_published_view(
    tmp_path: Path,
) -> None:
    uow, atlas, continuity, orchestrator = _uow(tmp_path)
    continuity.freeze_race = "frozen_then_published"
    request = atlas.prepared.request

    projection = uow.accept_atlas(
        request.workflow_id,
        request.code_task_id,
        request.acceptance_id,
        request.ingestion_key,
    )

    assert projection == {"projection": 1}
    assert continuity.freeze_calls == 2
    assert continuity.typed_view_calls == 1
    assert continuity.publish_calls == 0
    assert continuity.current.state == "published"
    assert orchestrator.outbox.state is AtlasOutboxState.PROJECTED
    assert orchestrator.outbox.quarantine_count == 0


def test_different_view_published_freeze_race_quarantines_the_exact_outbox(
    tmp_path: Path,
) -> None:
    uow, atlas, continuity, orchestrator = _uow(tmp_path)
    continuity.freeze_race = "published_different"
    request = atlas.prepared.request

    with pytest.raises(AtlasError) as raised:
        uow.accept_atlas(
            request.workflow_id,
            request.code_task_id,
            request.acceptance_id,
            request.ingestion_key,
        )

    assert raised.value.code == "ATLAS_EVIDENCE_CONFLICT"
    assert atlas.project_calls == 0
    assert orchestrator.outbox.state is AtlasOutboxState.QUARANTINED
    assert orchestrator.outbox.quarantine_count == 1


def test_prepared_atlas_store_conflict_is_quarantined_as_a_public_conflict(
    tmp_path: Path,
) -> None:
    uow, atlas, _continuity, orchestrator = _uow(tmp_path)
    atlas.project_error = StoreConflictError("private store mismatch")
    request = atlas.prepared.request

    with pytest.raises(AtlasError) as raised:
        uow.accept_atlas(
            request.workflow_id,
            request.code_task_id,
            request.acceptance_id,
            request.ingestion_key,
        )

    assert raised.value.code == "ATLAS_EVIDENCE_CONFLICT"
    assert "private" not in str(raised.value)
    assert orchestrator.outbox.state is AtlasOutboxState.QUARANTINED
    assert orchestrator.outbox.quarantine_count == 1


def test_publish_availability_failure_keeps_the_frozen_view_for_retry(
    tmp_path: Path,
) -> None:
    uow, atlas, continuity, orchestrator = _uow(tmp_path)
    continuity.publish_error = ContinuityCasError("CONTINUITY_CAS_UNAVAILABLE")
    request = atlas.prepared.request

    with pytest.raises(AtlasError) as raised:
        uow.accept_atlas(
            request.workflow_id,
            request.code_task_id,
            request.acceptance_id,
            request.ingestion_key,
        )
    assert raised.value.code == "ATLAS_EVIDENCE_UNAVAILABLE"
    assert continuity.current.state == "frozen"
    assert atlas.project_calls == 1
    assert orchestrator.outbox.state is AtlasOutboxState.PENDING

    projection = uow.accept_atlas(
        request.workflow_id,
        request.code_task_id,
        request.acceptance_id,
        request.ingestion_key,
    )
    assert projection == {"projection": 2}
    assert continuity.current.state == "published"
    assert continuity.freeze_calls == 2
    assert orchestrator.outbox.state is AtlasOutboxState.PROJECTED


def test_published_retry_repairs_only_its_matching_pending_outbox(
    tmp_path: Path,
) -> None:
    uow, atlas, continuity, orchestrator = _uow(tmp_path)
    orchestrator.fail_mark_once = True
    request = atlas.prepared.request

    with pytest.raises(AtlasError) as raised:
        uow.accept_atlas(
            request.workflow_id,
            request.code_task_id,
            request.acceptance_id,
            request.ingestion_key,
        )
    assert raised.value.code == "ATLAS_EVIDENCE_UNAVAILABLE"
    assert continuity.current.state == "published"
    assert orchestrator.outbox.state is AtlasOutboxState.PENDING

    projection = uow.accept_atlas(
        request.workflow_id,
        request.code_task_id,
        request.acceptance_id,
        request.ingestion_key,
    )
    assert projection == {"projection": 2}
    assert continuity.freeze_calls == 1
    assert continuity.publish_calls == 1
    assert orchestrator.outbox.state is AtlasOutboxState.PROJECTED


def test_same_acceptance_reuses_one_published_view_without_touching_other_pending_work(
    tmp_path: Path,
) -> None:
    uow, atlas, continuity, orchestrator = _uow(tmp_path)
    request = atlas.prepared.request

    first = uow.accept_atlas(
        request.workflow_id,
        request.code_task_id,
        request.acceptance_id,
        request.ingestion_key,
    )
    second = uow.accept_atlas(
        request.workflow_id,
        request.code_task_id,
        request.acceptance_id,
        request.ingestion_key,
    )

    assert first == {"projection": 1}
    assert second == {"projection": 2}
    assert continuity.current.state == "published"
    assert continuity.current.view_id == _VIEW
    assert continuity.freeze_calls == 1
    assert continuity.publish_calls == 1
    assert orchestrator.outbox.state is AtlasOutboxState.PROJECTED
    assert orchestrator.unrelated.state is AtlasOutboxState.PENDING


def test_published_replay_precedes_live_evidence_and_never_republishes(
    tmp_path: Path,
) -> None:
    uow, atlas, continuity, orchestrator = _uow(tmp_path)
    continuity.replay_enabled = True
    continuity._publish(_VIEW)  # noqa: SLF001 - model completed durable replay state
    atlas.forbid_live_prepare = True
    request = atlas.prepared.request

    projection = uow.accept_atlas(
        request.workflow_id,
        request.code_task_id,
        request.acceptance_id,
        request.ingestion_key,
    )

    assert projection == {"projection": 1}
    assert atlas.prepare_calls == 0
    assert atlas.replay_prepare_calls == 1
    assert continuity.replay_materialize_calls == 1
    assert continuity.freeze_calls == 0
    assert continuity.publish_calls == 0
    assert orchestrator.outbox.state is AtlasOutboxState.PROJECTED


def test_published_replay_rejects_pointer_mismatch_before_projection(
    tmp_path: Path,
) -> None:
    uow, atlas, continuity, orchestrator = _uow(tmp_path)
    continuity.replay_enabled = True
    continuity._publish(_VIEW)  # noqa: SLF001 - model completed durable replay state
    assert continuity.pointer is not None
    continuity.pointer = ContinuityPointer(
        continuity.pointer.workflow_id,
        continuity.pointer.code_task_id,
        continuity.pointer.code_task_version,
        _OTHER_VIEW,
        continuity.pointer.pointer_version,
        continuity.pointer.fence_epoch,
    )
    atlas.forbid_live_prepare = True
    request = atlas.prepared.request

    with pytest.raises(AtlasError) as raised:
        uow.accept_atlas(
            request.workflow_id,
            request.code_task_id,
            request.acceptance_id,
            request.ingestion_key,
        )

    assert raised.value.code == "ATLAS_EVIDENCE_CONFLICT"
    assert atlas.prepare_calls == 0
    assert atlas.project_calls == 0
    assert continuity.publish_calls == 0
    assert orchestrator.outbox.state is AtlasOutboxState.QUARANTINED


def test_frozen_replay_projects_then_publishes_the_same_fence(
    tmp_path: Path,
) -> None:
    uow, atlas, continuity, orchestrator = _uow(tmp_path)
    continuity.replay_enabled = True
    continuity.current = ContinuityAttempt(
        continuity.current.key, 1, "frozen", _VIEW, _RECEIPT
    )
    atlas.forbid_live_prepare = True
    request = atlas.prepared.request

    projection = uow.accept_atlas(
        request.workflow_id,
        request.code_task_id,
        request.acceptance_id,
        request.ingestion_key,
    )

    assert projection == {"projection": 1}
    assert atlas.prepare_calls == 0
    assert continuity.replay_materialize_calls == 2
    assert continuity.freeze_calls == 0
    assert continuity.publish_calls == 1
    assert continuity.current.state == "published"
    assert orchestrator.outbox.state is AtlasOutboxState.PROJECTED


def test_post_pointer_pending_replay_repairs_only_the_matching_outbox(
    tmp_path: Path,
) -> None:
    uow, atlas, continuity, orchestrator = _uow(tmp_path)
    continuity.replay_enabled = True
    continuity._publish(_VIEW)  # noqa: SLF001 - post-pointer interruption fixture
    atlas.forbid_live_prepare = True
    orchestrator.fail_mark_once = True
    request = atlas.prepared.request

    with pytest.raises(AtlasError) as raised:
        uow.accept_atlas(
            request.workflow_id,
            request.code_task_id,
            request.acceptance_id,
            request.ingestion_key,
        )

    assert raised.value.code == "ATLAS_EVIDENCE_UNAVAILABLE"
    assert atlas.prepare_calls == 0
    assert continuity.publish_calls == 0
    assert orchestrator.outbox.state is AtlasOutboxState.PENDING

    projection = uow.accept_atlas(
        request.workflow_id,
        request.code_task_id,
        request.acceptance_id,
        request.ingestion_key,
    )

    assert projection == {"projection": 2}
    assert continuity.publish_calls == 0
    assert orchestrator.outbox.state is AtlasOutboxState.PROJECTED


@pytest.mark.parametrize(
    "error_code",
    ("CONTINUITY_REPLAY_AMBIGUOUS", "CONTINUITY_REPLAY_CONFLICT"),
)
def test_invalid_replay_candidate_cannot_fall_back_to_live_evidence(
    tmp_path: Path, error_code: str
) -> None:
    uow, atlas, continuity, orchestrator = _uow(tmp_path)
    continuity.replay_candidate_error = ContinuityStoreError(error_code)
    atlas.forbid_live_prepare = True
    request = atlas.prepared.request

    with pytest.raises(AtlasError) as raised:
        uow.accept_atlas(
            request.workflow_id,
            request.code_task_id,
            request.acceptance_id,
            request.ingestion_key,
        )

    assert raised.value.code == "ATLAS_EVIDENCE_CONFLICT"
    assert atlas.prepare_calls == 0
    assert orchestrator.outbox.state is AtlasOutboxState.QUARANTINED


def test_unprepared_replay_candidate_is_unavailable_without_live_evidence(
    tmp_path: Path,
) -> None:
    uow, atlas, continuity, orchestrator = _uow(tmp_path)
    continuity.replay_candidate_error = ContinuityStoreError(
        "CONTINUITY_STORE_UNPREPARED"
    )
    atlas.forbid_live_prepare = True
    request = atlas.prepared.request

    with pytest.raises(AtlasError) as raised:
        uow.accept_atlas(
            request.workflow_id,
            request.code_task_id,
            request.acceptance_id,
            request.ingestion_key,
        )

    assert raised.value.code == "ATLAS_EVIDENCE_UNAVAILABLE"
    assert atlas.prepare_calls == 0
    assert atlas.project_calls == 0
    assert continuity.publish_calls == 0
    assert orchestrator.outbox.state is AtlasOutboxState.PENDING
    assert orchestrator.outbox.retry_count == 1
