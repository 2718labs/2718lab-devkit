"""Relay v3 application service and capability authority boundary."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from typing import Any

from .canonical import canonical_hash
from .evidence import CapabilitySigner, RelayCapabilityError
from .proofs import (
    IntegrationExpectation,
    IntegrationProofError,
    IntegrationProofReservation,
    IntegrationProofResolver,
    validate_integration_proof,
)
from .store import RelayStore, RelayStoreError

_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_WORKSPACE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SCOPE_PATH = re.compile(r"^[A-Za-z]:")


class RelayError(RuntimeError):
    """Stable Relay domain failure for the MCP adapter to envelope."""

    def __init__(self, code: str, message: str = "relay request rejected") -> None:
        self.code = code
        super().__init__(code if message == "relay request rejected" else message)


class RelayService:
    """Validate Relay inputs and authorize all lifecycle state transitions.

    The service owns no host integration.  `relay_start` returns inert host
    actions, `relay_status` reads durable state, workers use `relay_handoff`,
    and Sol alone uses `relay_integrate` with a separate HMAC scope.
    """

    _PLAN_FIELDS = frozenset(
        {
            "schema",
            "workflow_id",
            "workspace_binding",
            "base_commit",
            "capacity",
            "runtime_policy_id",
            "tasks",
            "dependencies",
            "conflicts",
            "queues",
            "plan_hash",
        }
    )
    _BINDING_FIELDS = frozenset(
        {"workspace_id", "input_snapshot_id", "atlas_packet_ids"}
    )
    _TASK_FIELDS = frozenset(
        {
            "task_id",
            "kind",
            "title",
            "objective",
            "priority",
            "dependencies",
            "write_scope",
            "route",
            "constraints",
            "acceptance_criteria",
            "atlas_packet_ids",
            "required_evidence",
            "prewarm_for_task_id",
            "retry_policy",
        }
    )
    _ROUTE_FIELDS = frozenset({"route_class", "model", "reasoning_effort"})
    _SCOPE_FIELDS = frozenset({"path", "kind"})
    _CONSTRAINT_FIELDS = frozenset({"code", "detail"})
    _CRITERION_FIELDS = frozenset({"criterion_id", "description"})
    _EVIDENCE_FIELDS = frozenset({"kind", "selector"})
    _RETRY_FIELDS = frozenset({"max_attempts", "retryable_codes"})
    _DEPENDENCY_FIELDS = frozenset({"from_task_id", "kind", "to_task_id"})
    _CONFLICT_FIELDS = frozenset({"from_task_id", "kind", "to_task_id"})
    _QUEUE_FIELDS = frozenset(
        {
            "prepared_prewarms",
            "ready",
            "running_slots",
            "review_integration",
            "terminal",
        }
    )
    _ROUTES = {
        "terra_high": ("gpt-5.6-terra", "high"),
        "terra_max": ("gpt-5.6-terra", "max"),
        "sol_high": ("gpt-5.6-sol", "high"),
        "sol_ultra": ("gpt-5.6-sol", "ultra"),
    }
    _KINDS = frozenset({"implementation", "verification", "review", "prewarm"})
    _WORKER_ACTIONS = frozenset(
        {"bind_endpoint", "heartbeat", "evidence", "terminal", "candidate_handoff"}
    )
    _SOL_ACTIONS = frozenset(
        {"review", "rebase", "reject", "integrate", "approve_readonly"}
    )
    _RECOVERY_ACTIONS = frozenset({"stale_recovery", "interruption_recovery"})
    _MAX_TASKS = 64
    _MAX_DEPENDENCY_EDGES = 1_520
    _MAX_CONFLICT_EDGES = 2_016
    _MAX_TEXT = 2_048

    def __init__(
        self,
        store: RelayStore,
        *,
        capability_secret: bytes | str,
        integration_proof_resolver: IntegrationProofResolver | None = None,
    ) -> None:
        self._store = store
        self._capabilities = CapabilitySigner(capability_secret)
        self._integration_proofs = integration_proof_resolver

    def issue_worker_capability(
        self,
        *,
        workflow_id: str,
        task_id: str,
        action: str,
        epoch: int,
        endpoint: str,
    ) -> str:
        """Issue a host-delivered worker token; it is never stored or returned by status."""

        try:
            return self._capabilities.issue(
                workflow_id=workflow_id,
                task_id=task_id,
                action=action,
                epoch=epoch,
                endpoint=endpoint,
                scope="worker",
            )
        except ValueError as error:
            raise RelayError("RELAY_REQUEST_INVALID") from error

    def issue_sol_capability(
        self,
        *,
        workflow_id: str,
        task_id: str,
        action: str,
        epoch: int,
        endpoint: str,
    ) -> str:
        """Issue a separate Sol-only token for review and integration decisions."""

        try:
            return self._capabilities.issue(
                workflow_id=workflow_id,
                task_id=task_id,
                action=action,
                epoch=epoch,
                endpoint=endpoint,
                scope="sol",
            )
        except ValueError as error:
            raise RelayError("RELAY_REQUEST_INVALID") from error

    def start(self, request: Mapping[str, Any]) -> dict[str, object]:
        """Apply one exact `relay_start` create or refill request."""

        if type(request) is not dict or type(request.get("mode")) is not str:
            raise RelayError("RELAY_REQUEST_INVALID")
        if request["mode"] == "create":
            if set(request) != {"mode", "plan", "idempotency_key"}:
                raise RelayError("RELAY_REQUEST_INVALID")
            return self.start_create(
                request["plan"], idempotency_key=request["idempotency_key"]
            )
        if request["mode"] == "refill":
            if set(request) != {
                "mode",
                "workflow_id",
                "refill_directive_id",
                "expected_schedule_version",
                "idempotency_key",
            }:
                raise RelayError("RELAY_REQUEST_INVALID")
            return self.start_refill(
                request["workflow_id"],
                request["refill_directive_id"],
                expected_schedule_version=request["expected_schedule_version"],
                idempotency_key=request["idempotency_key"],
            )
        raise RelayError("RELAY_REQUEST_INVALID")

    def start_create(
        self, plan: object, *, idempotency_key: object
    ) -> dict[str, object]:
        """Persist one canonical compiler result and return host actions."""

        if type(idempotency_key) is not str:
            raise RelayError("RELAY_REQUEST_INVALID")
        return self._call(
            self._store.start_create,
            self._validated_plan(plan),
            idempotency_key=idempotency_key,
        )

    def start_refill(
        self,
        workflow_id: object,
        directive_id: object,
        *,
        expected_schedule_version: object,
        idempotency_key: object,
    ) -> dict[str, object]:
        """Consume only a status-issued current refill directive."""

        workflow = self._identifier(workflow_id)
        directive = self._identifier(directive_id)
        if (
            type(expected_schedule_version) is not int
            or expected_schedule_version < 0
            or type(idempotency_key) is not str
        ):
            raise RelayError("RELAY_REQUEST_INVALID")
        return self._call(
            self._store.start_refill,
            workflow,
            directive,
            expected_schedule_version=expected_schedule_version,
            idempotency_key=idempotency_key,
        )

    def recover(self, request: Mapping[str, Any]) -> dict[str, object]:
        """Reissue a stalled or interrupted lease from its exact predecessor."""

        fields = self._lifecycle_fields(request, expected_scope="sol")
        action = fields["action"]
        if action not in self._RECOVERY_ACTIONS:
            raise RelayError("RELAY_CAPABILITY_SCOPE")
        base = {
            "workflow_id",
            "task_id",
            "action",
            "epoch",
            "endpoint",
            "expected_task_version",
            "capability",
        }
        self._exact_request_fields(
            request, base | {"predecessor_action_id", "predecessor_lease_id"}
        )
        recovery_fields = {
            key: value
            for key, value in fields.items()
            if key not in {"action", "endpoint"}
        }
        return self._call(
            self._store.recover_lease,
            recovery_kind=action,
            predecessor_action_id=self._identifier(request["predecessor_action_id"]),
            predecessor_lease_id=self._identifier(request["predecessor_lease_id"]),
            **recovery_fields,
        )

    def status(self, workflow_id: object) -> dict[str, object]:
        """Read status only; it does not create directives, leases, or actions."""

        return self._call(self._store.status, self._identifier(workflow_id))

    def handoff(self, request: Mapping[str, Any]) -> dict[str, object]:
        """Apply only a worker-scoped endpoint, heartbeat, evidence, or handoff event."""

        fields = self._lifecycle_fields(request, expected_scope="worker")
        action = fields["action"]
        if action not in self._WORKER_ACTIONS:
            raise RelayError("RELAY_CAPABILITY_SCOPE")
        base = {
            "workflow_id",
            "task_id",
            "action",
            "epoch",
            "endpoint",
            "expected_task_version",
            "capability",
        }
        if action in {"bind_endpoint", "heartbeat"}:
            self._exact_request_fields(request, base)
            operation = (
                self._store.bind_endpoint
                if action == "bind_endpoint"
                else self._store.heartbeat
            )
            return self._call(operation, **fields)
        if action == "evidence":
            self._exact_request_fields(request, base | {"evidence"})
            return self._call(
                self._store.record_evidence, evidence=request["evidence"], **fields
            )
        if action == "terminal":
            self._exact_request_fields(request, base | {"outcome"})
            if type(request["outcome"]) is not str:
                raise RelayError("RELAY_REQUEST_INVALID")
            return self._call(
                self._store.worker_terminal, outcome=request["outcome"], **fields
            )
        self._exact_request_fields(request, base | {"candidate"})
        return self._call(
            self._store.candidate_handoff, candidate=request["candidate"], **fields
        )

    def integrate(self, request: Mapping[str, Any]) -> dict[str, object]:
        """Apply one Sol-scoped review, rebase, rejection, or integration mutation."""

        fields = self._lifecycle_fields(request, expected_scope="sol")
        sol_fields = {key: value for key, value in fields.items() if key != "endpoint"}
        action = fields["action"]
        if action not in self._SOL_ACTIONS:
            raise RelayError("RELAY_CAPABILITY_SCOPE")
        base = {
            "workflow_id",
            "task_id",
            "action",
            "epoch",
            "endpoint",
            "expected_task_version",
            "capability",
        }
        if action == "approve_readonly":
            self._exact_request_fields(request, base)
            return self._call(self._store.approve_readonly, **sol_fields)
        if action == "review":
            self._exact_request_fields(
                request, base | {"candidate_id", "review_digest"}
            )
            return self._call(
                self._store.review_candidate,
                candidate_id=self._identifier(request["candidate_id"]),
                review_digest=self._digest(request["review_digest"]),
                **sol_fields,
            )
        if action == "rebase":
            self._exact_request_fields(
                request,
                base
                | {
                    "candidate_id",
                    "base_commit",
                    "head_commit",
                    "diff_hash",
                    "evidence_hashes",
                },
            )
            return self._call(
                self._store.rebase_candidate,
                candidate_id=self._identifier(request["candidate_id"]),
                base_commit=self._commit(request["base_commit"]),
                head_commit=self._commit(request["head_commit"]),
                diff_hash=self._digest(request["diff_hash"]),
                evidence_hashes=self._digest_list(request["evidence_hashes"]),
                **sol_fields,
            )
        if action == "reject":
            self._exact_request_fields(request, base | {"candidate_id"})
            return self._call(
                self._store.reject_candidate,
                candidate_id=self._identifier(request["candidate_id"]),
                **sol_fields,
            )
        if "integration_proof_id" not in request:
            raise RelayError("RELAY_INTEGRATION_PROOF_REQUIRED")
        self._exact_request_fields(
            request,
            base | {"candidate_id", "integration_proof_id"},
        )
        candidate_id = self._identifier(request["candidate_id"])
        proof_id = self._proof_id(request["integration_proof_id"])
        if self._integration_proofs is None:
            raise RelayError("RELAY_INTEGRATION_ATTESTOR_UNAVAILABLE")
        expectation = self._proof_call(
            self._store.integration_expectation,
            candidate_id=candidate_id,
            proof_id=proof_id,
            **sol_fields,
        )
        try:
            reservation = self._integration_proofs.reserve(proof_id, expectation)
        except IntegrationProofError as error:
            raise RelayError(error.code) from None
        except Exception:
            raise RelayError("RELAY_INTEGRATION_ATTESTOR_UNAVAILABLE") from None

        try:
            receipt = reservation.receipt
            validate_integration_proof(proof_id, expectation, receipt)
            result = self._call(
                self._store.integrate_candidate,
                candidate_id=candidate_id,
                proof_id=proof_id,
                expectation=expectation,
                receipt=receipt,
                **sol_fields,
            )
        except IntegrationProofError as error:
            self._release_proof_reservation(reservation)
            raise RelayError(error.code) from None
        except RelayError:
            self._release_proof_reservation(reservation)
            raise
        except Exception:
            self._release_proof_reservation(reservation)
            raise RelayError("RELAY_INTEGRATION_ATTESTOR_UNAVAILABLE") from None
        try:
            reservation.consume()
        except IntegrationProofError as error:
            self._release_proof_reservation(reservation)
            raise RelayError(error.code) from None
        except Exception:
            self._release_proof_reservation(reservation)
            raise RelayError("RELAY_INTEGRATION_ATTESTOR_UNAVAILABLE") from None
        return result

    def _lifecycle_fields(
        self, request: Mapping[str, Any], *, expected_scope: str
    ) -> dict[str, object]:
        if type(request) is not dict:
            raise RelayError("RELAY_REQUEST_INVALID")
        try:
            workflow_id = self._identifier(request.get("workflow_id"))
            task_id = self._identifier(request.get("task_id"))
            action = self._identifier(request.get("action"))
            epoch = request.get("epoch")
            endpoint = request.get("endpoint")
            expected_task_version = request.get("expected_task_version")
            if (
                type(epoch) is not int
                or epoch < 1
                or type(endpoint) is not str
                or not endpoint
                or len(endpoint) > 256
                or type(expected_task_version) is not int
                or expected_task_version < 1
            ):
                raise RelayError("RELAY_REQUEST_INVALID")
            self._capabilities.verify(
                request.get("capability"),
                workflow_id=workflow_id,
                task_id=task_id,
                action=action,
                epoch=epoch,
                endpoint=endpoint,
                scope=expected_scope,
            )
            return {
                "workflow_id": workflow_id,
                "task_id": task_id,
                "epoch": epoch,
                "endpoint": endpoint,
                "expected_task_version": expected_task_version,
                "action": action,
            }
        except RelayCapabilityError as error:
            raise RelayError(error.code) from error
        except (TypeError, ValueError) as error:
            raise RelayError("RELAY_REQUEST_INVALID") from error

    def _validated_plan(self, value: object) -> dict[str, Any]:
        if type(value) is not dict or set(value) != self._PLAN_FIELDS:
            raise RelayError("RELAY_PLAN_INVALID")
        if value["schema"] != "2718lab-devkit/relay-plan-v1":
            raise RelayError("RELAY_PLAN_INVALID")
        workflow_id = self._identifier(value["workflow_id"], plan=True)
        binding = self._validated_binding(value["workspace_binding"])
        base_commit = self._commit(value["base_commit"], plan=True)
        capacity = value["capacity"]
        if type(capacity) is not int or not 1 <= capacity <= 3:
            raise RelayError("RELAY_PLAN_INVALID")
        if value["runtime_policy_id"] != "2718lab-devkit/relay-runtime-policy-v1":
            raise RelayError("RELAY_PLAN_INVALID")
        tasks = self._validated_tasks(value["tasks"])
        task_ids = [task["task_id"] for task in tasks]
        if task_ids != sorted(task_ids) or len(task_ids) != len(set(task_ids)):
            raise RelayError("RELAY_PLAN_INVALID")
        self._validate_task_relations(tasks)
        dependency_edges = self._validated_edges(
            value["dependencies"],
            task_ids,
            "depends_on",
            maximum=self._MAX_DEPENDENCY_EDGES,
        )
        expected_dependencies = [
            {
                "from_task_id": task["task_id"],
                "kind": "depends_on",
                "to_task_id": dependency,
            }
            for task in tasks
            for dependency in task["dependencies"]
        ]
        if dependency_edges != expected_dependencies:
            raise RelayError("RELAY_PLAN_INVALID")
        conflict_edges = self._validated_edges(
            value["conflicts"],
            task_ids,
            "write_scope_conflict",
            maximum=self._MAX_CONFLICT_EDGES,
        )
        expected_conflicts = self._compiler_conflicts(tasks)
        if conflict_edges != expected_conflicts:
            raise RelayError("RELAY_PLAN_INVALID")
        if binding["atlas_packet_ids"] != sorted(
            {packet for task in tasks for packet in task["atlas_packet_ids"]}
        ):
            raise RelayError("RELAY_PLAN_INVALID")
        self._validated_queues(value["queues"], tasks, expected_conflicts)
        body = {key: value[key] for key in self._PLAN_FIELDS if key != "plan_hash"}
        if value["plan_hash"] != canonical_hash(body):
            raise RelayError("RELAY_PLAN_INVALID")
        return {
            **body,
            "workflow_id": workflow_id,
            "workspace_binding": binding,
            "base_commit": base_commit,
            "tasks": tasks,
            "plan_hash": value["plan_hash"],
        }

    def _validated_binding(self, value: object) -> dict[str, object]:
        if type(value) is not dict or set(value) != self._BINDING_FIELDS:
            raise RelayError("RELAY_PLAN_INVALID")
        workspace_id = self._workspace_id(value["workspace_id"], plan=True)
        snapshot = self._digest(value["input_snapshot_id"], plan=True)
        packets = self._digest_list(value["atlas_packet_ids"], plan=True)
        return {
            "workspace_id": workspace_id,
            "input_snapshot_id": snapshot,
            "atlas_packet_ids": packets,
        }

    def _validated_tasks(self, value: object) -> list[dict[str, Any]]:
        if type(value) is not list or not 1 <= len(value) <= self._MAX_TASKS:
            raise RelayError("RELAY_PLAN_INVALID")
        return [self._validated_task(item) for item in value]

    def _validated_task(self, value: object) -> dict[str, Any]:
        if type(value) is not dict or set(value) != self._TASK_FIELDS:
            raise RelayError("RELAY_PLAN_INVALID")
        task_id = self._identifier(value["task_id"], plan=True)
        kind = value["kind"]
        priority = value["priority"]
        if (
            type(kind) is not str
            or kind not in self._KINDS
            or type(priority) is not int
            or not 1 <= priority <= 100
        ):
            raise RelayError("RELAY_PLAN_INVALID")
        title = self._text(value["title"], plan=True, maximum=256)
        objective = self._text(value["objective"], plan=True)
        dependencies = self._identifier_list(value["dependencies"], plan=True)
        scopes = self._validated_scopes(value["write_scope"])
        if kind == "implementation" and not scopes:
            raise RelayError("RELAY_PLAN_INVALID")
        if kind != "implementation" and scopes:
            raise RelayError("RELAY_PLAN_INVALID")
        route = self._validated_route(value["route"])
        constraints = self._validated_pairs(
            value["constraints"], self._CONSTRAINT_FIELDS, "code", "detail"
        )
        criteria = self._validated_pairs(
            value["acceptance_criteria"],
            self._CRITERION_FIELDS,
            "criterion_id",
            "description",
        )
        packets = self._digest_list(value["atlas_packet_ids"], plan=True)
        evidence = self._validated_pairs(
            value["required_evidence"], self._EVIDENCE_FIELDS, "kind", "selector"
        )
        target = value["prewarm_for_task_id"]
        if kind == "prewarm":
            if dependencies or target is None:
                raise RelayError("RELAY_PLAN_INVALID")
            target = self._identifier(target, plan=True)
        elif target is not None:
            raise RelayError("RELAY_PLAN_INVALID")
        retry = value["retry_policy"]
        if type(retry) is not dict or set(retry) != self._RETRY_FIELDS:
            raise RelayError("RELAY_PLAN_INVALID")
        max_attempts = retry["max_attempts"]
        if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
            raise RelayError("RELAY_PLAN_INVALID")
        retry_codes = self._identifier_list(retry["retryable_codes"], plan=True)
        return {
            "task_id": task_id,
            "kind": kind,
            "title": title,
            "objective": objective,
            "priority": priority,
            "dependencies": dependencies,
            "write_scope": scopes,
            "route": route,
            "constraints": constraints,
            "acceptance_criteria": criteria,
            "atlas_packet_ids": packets,
            "required_evidence": evidence,
            "prewarm_for_task_id": target,
            "retry_policy": {
                "max_attempts": max_attempts,
                "retryable_codes": retry_codes,
            },
        }

    def _validated_scopes(self, value: object) -> list[dict[str, str]]:
        if type(value) is not list or len(value) > 32:
            raise RelayError("RELAY_PLAN_INVALID")
        scopes: list[dict[str, str]] = []
        for item in value:
            if type(item) is not dict or set(item) != self._SCOPE_FIELDS:
                raise RelayError("RELAY_PLAN_INVALID")
            path = item["path"]
            kind = item["kind"]
            try:
                path_utf8 = path.encode("utf-8", errors="strict")
            except (AttributeError, UnicodeError):
                raise RelayError("RELAY_PLAN_INVALID") from None
            if (
                type(path) is not str
                or not path
                or len(path) > self._MAX_TEXT
                or len(path_utf8) > self._MAX_TEXT
                or unicodedata.normalize("NFC", path) != path
                or path.startswith(("/", "~"))
                or _SCOPE_PATH.match(path) is not None
                or "\\" in path
                or "\x00" in path
                or any(
                    ord(character) < 32 or ord(character) == 127 for character in path
                )
                or any(part in {"", ".", ".."} for part in path.split("/"))
                or type(kind) is not str
                or kind not in {"file", "tree"}
            ):
                raise RelayError("RELAY_PLAN_INVALID")
            scopes.append({"path": path, "kind": kind})
        if scopes != sorted(scopes, key=lambda item: (item["path"], item["kind"])):
            raise RelayError("RELAY_PLAN_INVALID")
        if len({(item["path"], item["kind"]) for item in scopes}) != len(scopes):
            raise RelayError("RELAY_PLAN_INVALID")
        if len({item["path"].casefold() for item in scopes}) != len(scopes):
            raise RelayError("RELAY_PLAN_INVALID")
        return scopes

    def _validated_route(self, value: object) -> dict[str, str]:
        if type(value) is not dict or set(value) != self._ROUTE_FIELDS:
            raise RelayError("RELAY_PLAN_INVALID")
        route_class = value["route_class"]
        if type(route_class) is not str or route_class not in self._ROUTES:
            raise RelayError("RELAY_PLAN_INVALID")
        model, effort = self._ROUTES[route_class]
        if value["model"] != model or value["reasoning_effort"] != effort:
            raise RelayError("RELAY_PLAN_INVALID")
        return {"route_class": route_class, "model": model, "reasoning_effort": effort}

    def _validated_pairs(
        self,
        value: object,
        fields: frozenset[str],
        identifier_key: str,
        text_key: str,
    ) -> list[dict[str, str]]:
        if type(value) is not list or len(value) > 32:
            raise RelayError("RELAY_PLAN_INVALID")
        pairs: list[dict[str, str]] = []
        for item in value:
            if type(item) is not dict or set(item) != fields:
                raise RelayError("RELAY_PLAN_INVALID")
            pairs.append(
                {
                    identifier_key: self._identifier(item[identifier_key], plan=True),
                    text_key: self._text(item[text_key], plan=True),
                }
            )
        if pairs != sorted(
            pairs, key=lambda item: (item[identifier_key], item[text_key])
        ):
            raise RelayError("RELAY_PLAN_INVALID")
        if len({(item[identifier_key], item[text_key]) for item in pairs}) != len(
            pairs
        ):
            raise RelayError("RELAY_PLAN_INVALID")
        return pairs

    def _validated_edges(
        self,
        value: object,
        task_ids: list[str],
        required_kind: str,
        *,
        maximum: int,
    ) -> list[dict[str, str]]:
        if type(value) is not list or len(value) > maximum:
            raise RelayError("RELAY_PLAN_INVALID")
        entries: list[dict[str, str]] = []
        known = set(task_ids)
        for item in value:
            if type(item) is not dict or set(item) != self._DEPENDENCY_FIELDS:
                raise RelayError("RELAY_PLAN_INVALID")
            source = self._identifier(item["from_task_id"], plan=True)
            target = self._identifier(item["to_task_id"], plan=True)
            if (
                item["kind"] != required_kind
                or source not in known
                or target not in known
            ):
                raise RelayError("RELAY_PLAN_INVALID")
            entries.append(
                {
                    "from_task_id": source,
                    "kind": required_kind,
                    "to_task_id": target,
                }
            )
        if entries != sorted(
            entries,
            key=lambda item: (item["from_task_id"], item["to_task_id"]),
        ) or len(
            {(item["from_task_id"], item["to_task_id"]) for item in entries}
        ) != len(entries):
            raise RelayError("RELAY_PLAN_INVALID")
        return entries

    def _validated_queues(
        self,
        value: object,
        tasks: list[dict[str, Any]],
        conflicts: list[dict[str, str]],
    ) -> None:
        if type(value) is not dict or set(value) != self._QUEUE_FIELDS:
            raise RelayError("RELAY_PLAN_INVALID")
        for name in self._QUEUE_FIELDS:
            queue = value[name]
            if type(queue) is not list or any(type(item) is not str for item in queue):
                raise RelayError("RELAY_PLAN_INVALID")
            if name in {"running_slots", "review_integration", "terminal"} and queue:
                raise RelayError("RELAY_PLAN_INVALID")
        prewarms = sorted(
            (task for task in tasks if task["kind"] == "prewarm"),
            key=lambda task: (-task["priority"], task["task_id"]),
        )
        candidates = [
            task
            for task in tasks
            if task["kind"] != "prewarm" and not task["dependencies"]
        ]
        candidate_ids = {task["task_id"] for task in candidates}
        withheld = {
            edge["to_task_id"]
            for edge in conflicts
            if edge["from_task_id"] in candidate_ids
            and edge["to_task_id"] in candidate_ids
        }
        ready = sorted(
            (task for task in candidates if task["task_id"] not in withheld),
            key=lambda task: (-task["priority"], task["task_id"]),
        )
        expected = {
            "prepared_prewarms": [task["task_id"] for task in prewarms],
            "ready": [task["task_id"] for task in ready],
            "running_slots": [],
            "review_integration": [],
            "terminal": [],
        }
        if value != expected:
            raise RelayError("RELAY_PLAN_INVALID")

    def _validate_task_relations(self, tasks: list[dict[str, Any]]) -> None:
        task_by_id = {task["task_id"]: task for task in tasks}
        prewarm_ids = {task["task_id"] for task in tasks if task["kind"] == "prewarm"}
        for task in tasks:
            task_id = task["task_id"]
            dependencies = task["dependencies"]
            if (
                task_id in dependencies
                or not set(dependencies) <= set(task_by_id)
                or set(dependencies) & prewarm_ids
            ):
                raise RelayError("RELAY_PLAN_INVALID")
            target = task["prewarm_for_task_id"]
            if target is not None and (
                target not in task_by_id or task_by_id[target]["kind"] == "prewarm"
            ):
                raise RelayError("RELAY_PLAN_INVALID")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise RelayError("RELAY_PLAN_INVALID")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency in task_by_id[task_id]["dependencies"]:
                visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in task_by_id:
            visit(task_id)

    def _compiler_conflicts(self, tasks: list[dict[str, Any]]) -> list[dict[str, str]]:
        dependencies = {task["task_id"]: task["dependencies"] for task in tasks}
        ancestor_cache: dict[str, frozenset[str]] = {}

        def ancestors(task_id: str) -> frozenset[str]:
            if task_id not in ancestor_cache:
                direct = dependencies[task_id]
                ancestor_cache[task_id] = frozenset(
                    {
                        *direct,
                        *(ancestor for item in direct for ancestor in ancestors(item)),
                    }
                )
            return ancestor_cache[task_id]

        writers = [task for task in tasks if task["kind"] == "implementation"]
        conflicts: list[dict[str, str]] = []
        for index, left in enumerate(writers):
            for right in writers[index + 1 :]:
                if (
                    left["task_id"] in ancestors(right["task_id"])
                    or right["task_id"] in ancestors(left["task_id"])
                    or not any(
                        self._scopes_overlap(left_scope, right_scope)
                        for left_scope in left["write_scope"]
                        for right_scope in right["write_scope"]
                    )
                ):
                    continue
                if left["priority"] != right["priority"]:
                    blocker, blocked = (
                        (left, right)
                        if left["priority"] > right["priority"]
                        else (right, left)
                    )
                else:
                    blocker, blocked = (
                        (left, right)
                        if left["task_id"] < right["task_id"]
                        else (right, left)
                    )
                conflicts.append(
                    {
                        "from_task_id": blocker["task_id"],
                        "kind": "write_scope_conflict",
                        "to_task_id": blocked["task_id"],
                    }
                )
        return sorted(
            conflicts,
            key=lambda item: (item["from_task_id"], item["to_task_id"]),
        )

    @staticmethod
    def _scopes_overlap(left: Mapping[str, str], right: Mapping[str, str]) -> bool:
        if left["path"] == right["path"]:
            return True
        if left["kind"] == "tree" and right["path"].startswith(left["path"] + "/"):
            return True
        return right["kind"] == "tree" and left["path"].startswith(right["path"] + "/")

    def _identifier(self, value: object, *, plan: bool = False) -> str:
        if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
            raise RelayError("RELAY_PLAN_INVALID" if plan else "RELAY_REQUEST_INVALID")
        return value

    def _workspace_id(self, value: object, *, plan: bool = False) -> str:
        if type(value) is not str or _WORKSPACE_ID.fullmatch(value) is None:
            raise RelayError("RELAY_PLAN_INVALID" if plan else "RELAY_REQUEST_INVALID")
        return value

    def _commit(self, value: object, *, plan: bool = False) -> str:
        if type(value) is not str or _COMMIT.fullmatch(value) is None:
            raise RelayError("RELAY_PLAN_INVALID" if plan else "RELAY_REQUEST_INVALID")
        return value

    def _digest(self, value: object, *, plan: bool = False) -> str:
        if type(value) is not str or _DIGEST.fullmatch(value) is None:
            raise RelayError("RELAY_PLAN_INVALID" if plan else "RELAY_REQUEST_INVALID")
        return value

    def _digest_list(self, value: object, *, plan: bool = False) -> list[str]:
        if type(value) is not list or len(value) > 32:
            raise RelayError("RELAY_PLAN_INVALID" if plan else "RELAY_REQUEST_INVALID")
        values = [self._digest(item, plan=plan) for item in value]
        if values != sorted(values) or len(values) != len(set(values)):
            raise RelayError("RELAY_PLAN_INVALID" if plan else "RELAY_REQUEST_INVALID")
        return values

    def _identifier_list(self, value: object, *, plan: bool = False) -> list[str]:
        if type(value) is not list or len(value) > 32:
            raise RelayError("RELAY_PLAN_INVALID" if plan else "RELAY_REQUEST_INVALID")
        values = [self._identifier(item, plan=plan) for item in value]
        if values != sorted(values) or len(values) != len(set(values)):
            raise RelayError("RELAY_PLAN_INVALID" if plan else "RELAY_REQUEST_INVALID")
        return values

    def _text(
        self, value: object, *, plan: bool = False, maximum: int | None = None
    ) -> str:
        limit = self._MAX_TEXT if maximum is None else maximum
        if (
            type(value) is not str
            or not value
            or value != value.strip()
            or len(value) > limit
            or "\r" in value
            or "\n" in value
        ):
            raise RelayError("RELAY_PLAN_INVALID" if plan else "RELAY_REQUEST_INVALID")
        return value

    @staticmethod
    def _exact_request_fields(request: Mapping[str, Any], expected: set[str]) -> None:
        if set(request) != expected:
            raise RelayError("RELAY_REQUEST_INVALID")

    @staticmethod
    def _call(operation: Any, *args: Any, **kwargs: Any) -> dict[str, object]:
        kwargs.pop("action", None)
        try:
            return operation(*args, **kwargs)
        except RelayStoreError as error:
            raise RelayError(error.code) from error
        except KeyError as error:
            raise RelayError("RELAY_REQUEST_INVALID") from error

    @staticmethod
    def _proof_call(
        operation: Any, *args: Any, **kwargs: Any
    ) -> IntegrationExpectation:
        kwargs.pop("action", None)
        try:
            result = operation(*args, **kwargs)
        except RelayStoreError as error:
            raise RelayError(error.code) from error
        except KeyError as error:
            raise RelayError("RELAY_REQUEST_INVALID") from error
        if type(result) is not IntegrationExpectation:
            raise RelayError("RELAY_INTEGRATION_PROOF_CORRUPT")
        return result

    @staticmethod
    def _release_proof_reservation(
        reservation: IntegrationProofReservation,
    ) -> None:
        try:
            reservation.release()
        except Exception:
            pass

    @staticmethod
    def _proof_id(value: object) -> str:
        if type(value) is not str or _DIGEST.fullmatch(value) is None:
            raise RelayError("RELAY_INTEGRATION_PROOF_INVALID")
        return value
