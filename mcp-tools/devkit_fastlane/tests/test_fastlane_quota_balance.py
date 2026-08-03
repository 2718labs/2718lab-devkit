from __future__ import annotations

import ast
import copy
import hashlib
import hmac
import importlib.util
import json
import socket
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "fastlane_quota_balance.py"

EVALUATION_TIME = "2026-08-01T15:10:00Z"
OBSERVED_TIME = "2026-08-01T15:09:00Z"
VALID_UNTIL = "2026-08-01T15:10:30Z"
KEY = b"quota-balance-test-key-v1"

POLICY = {
    "schema": "2718lab-devkit/fastlane-quota-balance-policy-v1",
    "version": 1,
    "main": {
        "normal_used_lt_ppm": 600000,
        "normal_slope_lt_ppm_300s": 5000,
        "warm_used_lt_ppm": 750000,
        "warm_slope_lt_ppm_300s": 10000,
        "tight_used_lt_ppm": 900000,
        "tight_slope_lt_ppm_300s": 20000,
        "critical_used_lt_ppm": 980000,
        "normal_target": 12,
        "warm_target": 10,
        "tight_target": 8,
        "critical_target": 6,
        "paused_target": 6,
    },
    "spark": {
        "usage_deadband_ppm": 50000,
        "slope_deadband_ppm_300s": 2500,
        "absolute_cap_ppm": 850000,
        "global_concurrency_cap": 1,
        "host_concurrency_cap": 1,
    },
}


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def hash_value(value: object) -> str:
    payload = (
        value if isinstance(value, bytes) else canonical_json(value).encode("utf-8")
    )
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def digest_text(value: str) -> str:
    return hash_value(value.encode("utf-8"))


def sign(value: object) -> str:
    return hmac.new(
        KEY, canonical_json(value).encode("utf-8"), hashlib.sha256
    ).hexdigest()


POLICY_HASH = hash_value(POLICY)
POLICY_V2_PATH = ROOT / "assets" / "fastlane-quota-balance-policy-v2.json"
KEY_ID = digest_text("quota-balance-test-key-id")
SOURCE_ID = digest_text("quota-balance-host-a")
PERIOD_ID = digest_text("quota-balance-period-a")
HOST_ID = digest_text("quota-balance-host-id")
LEASE_SET = digest_text("quota-balance-active-leases")
LEASE_SET_AFTER = digest_text("quota-balance-active-leases-after")


def load_quota_module():
    spec = importlib.util.spec_from_file_location("fastlane_quota_balance", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load quota module: {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["fastlane_quota_balance"] = module
    spec.loader.exec_module(module)
    return module


def make_snapshot(
    *,
    main_used: int = 500000,
    main_slope: int = 0,
    spark_used: int = 100000,
    spark_slope: int = 0,
    ledger_epoch: int = 7,
    global_main_active: int = 0,
    global_spark_active: int = 0,
    host_main_active: int = 0,
    host_spark_active: int = 0,
    host_main_cap: int = 3,
    host_spark_cap: int = 1,
    observed_at: str = OBSERVED_TIME,
    valid_until: str = VALID_UNTIL,
    snapshot_seq: int = 11,
    source_kind: str = "codex_host_usage_snapshot",
) -> dict[str, object]:
    snapshot: dict[str, object] = {
        "schema": "2718lab-devkit/host-quota-snapshot-v1",
        "source": {
            "kind": source_kind,
            "source_id_hash": SOURCE_ID,
            "key_id": KEY_ID,
        },
        "snapshot_seq": snapshot_seq,
        "observed_at_utc_z": observed_at,
        "valid_until_utc_z": valid_until,
        "sample_window_seconds": 300,
        "main": {
            "period_id_hash": PERIOD_ID,
            "used_ppm": main_used,
            "delta_ppm_300s": main_slope,
        },
        "spark": {
            "period_id_hash": PERIOD_ID,
            "used_ppm": spark_used,
            "delta_ppm_300s": spark_slope,
        },
        "capacity": {
            "ledger_epoch": ledger_epoch,
            "global_main_active": global_main_active,
            "global_spark_active": global_spark_active,
            "host_main_active": host_main_active,
            "host_spark_active": host_spark_active,
            "host_main_cap": host_main_cap,
            "host_spark_cap": host_spark_cap,
            "active_lease_set_hash": LEASE_SET,
        },
    }
    snapshot_hash = hash_value(snapshot)
    signed = {**snapshot, "snapshot_hash": snapshot_hash}
    return {
        **signed,
        "signature": {
            "algorithm": "hmac-sha256",
            "value": sign(signed),
        },
    }


def make_candidate(
    label: str,
    *,
    pool: str = "main",
    lane: str = "terra",
    model: str = "gpt-5.6-terra",
    effort: str = "high",
    slot: str = "slot-1",
    spark_binding: dict[str, str] | None = None,
) -> dict[str, object]:
    return {
        "candidate_id": digest_text(f"candidate-{label}"),
        "workflow_key": digest_text(f"workflow-{label}"),
        "task_key": digest_text(f"task-{label}"),
        "pool": pool,
        "scheduler_role": "execution",
        "route_lock": {
            "result_hash": digest_text(f"route-{label}"),
            "task_fingerprint": digest_text(f"fingerprint-{label}"),
            "lane": lane,
            "model": model,
            "effort": effort,
            "safety_floor_rank": 60,
        },
        "task_lease_epoch": 3,
        "assignment_epoch": 5,
        "assignment_token": digest_text(f"assignment-{label}"),
        "host_id_hash": HOST_ID,
        "local_slot_id": slot,
        "write_scope_hash": digest_text(f"scope-{label}"),
        "input_snapshot_id": digest_text(f"input-{label}"),
        "spark_binding": spark_binding,
    }


def lease_scope_binding(candidate: dict[str, object]) -> str:
    return hash_value(
        {
            "candidate_id": candidate["candidate_id"],
            "workflow_key": candidate["workflow_key"],
            "task_key": candidate["task_key"],
            "task_lease_epoch": candidate["task_lease_epoch"],
            "assignment_epoch": candidate["assignment_epoch"],
            "assignment_token": candidate["assignment_token"],
            "host_id_hash": candidate["host_id_hash"],
            "local_slot_id": candidate["local_slot_id"],
            "write_scope_hash": candidate["write_scope_hash"],
            "input_snapshot_id": candidate["input_snapshot_id"],
        }
    )


def request_hash(payload: dict[str, object]) -> str:
    source = {key: value for key, value in payload.items() if key != "request_hash"}
    source["candidates"] = sorted(
        source["candidates"],
        key=lambda item: item["candidate_id"],  # type: ignore[index]
    )
    source["receipts"] = sorted(
        source["receipts"],
        key=lambda item: item["receipt_hash"],  # type: ignore[index]
    )
    return hash_value(source)


def make_request(
    *,
    snapshot: dict[str, object] | None = None,
    candidates: list[dict[str, object]] | None = None,
    receipts: list[dict[str, object]] | None = None,
    prior_audit_hash: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": "2718lab-devkit/fastlane-quota-balance-request-v1",
        "policy_hash": POLICY_HASH,
        "snapshot": make_snapshot() if snapshot is None else snapshot,
        "candidates": [make_candidate("a")] if candidates is None else candidates,
        "receipts": [] if receipts is None else receipts,
        "prior_audit_hash": prior_audit_hash,
    }
    payload["request_hash"] = request_hash(payload)
    return payload


def policy_v2_hash() -> str:
    assert POLICY_V2_PATH.exists(), "the v2 quota policy asset is absent"
    return hash_value(json.loads(POLICY_V2_PATH.read_text(encoding="utf-8")))


def make_v2_normal_spark_alternate(label: str) -> dict[str, object]:
    candidate = make_candidate(
        label,
        pool="spark",
        lane="spark",
        model="gpt-5.3-codex-spark",
        effort="medium",
    )
    candidate["capability_binding_hash"] = digest_text(f"capability-{label}")
    candidate["context_binding_hash"] = digest_text(f"context-{label}")
    route_lock = candidate["route_lock"]
    assert isinstance(route_lock, dict)
    binding: dict[str, object] = {
        "schema": "2718lab-devkit/spark-alternate-binding-v1",
        "route": {
            "lane": route_lock["lane"],
            "model": route_lock["model"],
            "effort": route_lock["effort"],
        },
        "capability_hash": candidate["capability_binding_hash"],
        "task_hash": candidate["task_key"],
        "lease_epoch": candidate["task_lease_epoch"],
        "context_hash": candidate["context_binding_hash"],
        "scope_hash": candidate["write_scope_hash"],
    }
    binding["binding_hash"] = hash_value(binding)
    candidate["spark_binding"] = binding
    return candidate


def make_v2_static_spark_strike(label: str) -> dict[str, object]:
    candidate = make_candidate(
        label,
        pool="spark",
        lane="spark",
        model="gpt-5.3-codex-spark",
        effort="medium",
        spark_binding={
            "spark_proof_hash": digest_text(f"proof-{label}"),
            "parent_main_route_hash": digest_text(f"parent-route-{label}"),
            "parent_admission_id": digest_text(f"parent-admission-{label}"),
            "writer_handoff_hash": digest_text(f"writer-handoff-{label}"),
        },
    )
    candidate["capability_binding_hash"] = digest_text(f"capability-{label}")
    candidate["context_binding_hash"] = digest_text(f"context-{label}")
    return candidate


def make_v2_request(
    *,
    snapshot: dict[str, object] | None = None,
    candidates: list[dict[str, object]] | None = None,
    receipts: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": "2718lab-devkit/fastlane-quota-balance-request-v2",
        "policy_hash": policy_v2_hash(),
        "snapshot": make_snapshot() if snapshot is None else snapshot,
        "candidates": (
            [make_v2_normal_spark_alternate("default")]
            if candidates is None
            else candidates
        ),
        "receipts": [] if receipts is None else receipts,
        "prior_audit_hash": None,
    }
    payload["request_hash"] = request_hash(payload)
    return payload


def make_receipt(
    candidate: dict[str, object],
    snapshot: dict[str, object],
    decision_hash: str,
) -> dict[str, object]:
    capacity = snapshot["capacity"]
    assert isinstance(capacity, dict)
    route_lock = candidate["route_lock"]
    assert isinstance(route_lock, dict)
    receipt: dict[str, object] = {
        "schema": "2718lab-devkit/global-admission-receipt-v1",
        "admission_id": digest_text(str(candidate["candidate_id"]) + "-admission"),
        "candidate_id": candidate["candidate_id"],
        "decision_hash": decision_hash,
        "pool": candidate["pool"],
        "ledger_epoch_before": capacity["ledger_epoch"],
        "ledger_epoch_after": int(capacity["ledger_epoch"]) + 1,
        "active_lease_set_hash_before": capacity["active_lease_set_hash"],
        "active_lease_set_hash_after": LEASE_SET_AFTER,
        "route_result_hash": route_lock["result_hash"],
        "task_lease_epoch": candidate["task_lease_epoch"],
        "assignment_epoch": candidate["assignment_epoch"],
        "assignment_token": candidate["assignment_token"],
        "host_id_hash": candidate["host_id_hash"],
        "local_slot_id": candidate["local_slot_id"],
        "write_scope_hash": candidate["write_scope_hash"],
        "input_snapshot_id": candidate["input_snapshot_id"],
        "issued_at_utc_z": OBSERVED_TIME,
        "expires_at_utc_z": VALID_UNTIL,
        "prior_receipt_hash": None,
    }
    receipt_hash = hash_value(receipt)
    signed = {**receipt, "receipt_hash": receipt_hash}
    return {
        **signed,
        "signature": {
            "algorithm": "hmac-sha256",
            "key_id": KEY_ID,
            "value": sign(signed),
        },
    }


def coordinator_input(
    *,
    operation: str = "admission",
    intake_shape: str = "structured",
    sol_lock: dict[str, object] | None = None,
    **flags: bool,
) -> dict[str, object]:
    return {
        "schema": "2718lab-devkit/coordinator-kernel-input-v1",
        "profile": {
            "model": "gpt-5.6-luna",
            "effort": "max",
            "capability_attestation_hash": digest_text("luna-max-capability"),
        },
        "operation": operation,
        "intake_shape": intake_shape,
        "cross_contract_semantics": flags.get("cross_contract_semantics", False),
        "security_semantics": flags.get("security_semantics", False),
        "recovery_semantics": flags.get("recovery_semantics", False),
        "model_policy_exception": flags.get("model_policy_exception", False),
        "quota_policy_exception": flags.get("quota_policy_exception", False),
        "mechanical_binding_complete": flags.get("mechanical_binding_complete", True),
        "operation_input_hash": digest_text(f"operation-input-{operation}"),
        "sol_lock": sol_lock,
    }


def make_sol_lock(
    input_value: dict[str, object], *, expires_event_seq: int = 12
) -> dict[str, object]:
    lock: dict[str, object] = {
        "schema": "2718lab-devkit/sol-decision-lock-v1",
        "operation": input_value["operation"],
        "operation_input_hash": input_value["operation_input_hash"],
        "contract_set_hash": digest_text("contracts-v1"),
        "policy_set_hash": POLICY_HASH,
        "evidence_set_hash": digest_text("evidence-v1"),
        "decision": "approved",
        "reviewer_model": "gpt-5.6-sol",
        "reviewer_role": "independent_sol",
        "issued_event_seq": 10,
        "expires_event_seq": expires_event_seq,
        "predecessor_hash": None,
        "signature_receipt_hash": digest_text("sol-signature-receipt"),
    }
    lock["lock_hash"] = hash_value(lock)
    return lock


class QuotaBalanceTests(unittest.TestCase):
    def resolver(self, key_id: str) -> bytes | None:
        return KEY if key_id == KEY_ID else None

    def compile(
        self,
        quota,
        request: dict[str, object],
        *,
        route_hashes: set[str] | None = None,
        lease_bindings: set[str] | None = None,
    ) -> dict[str, object]:
        candidates = request.get("candidates", [])
        assert isinstance(candidates, list)
        if route_hashes is None:
            route_hashes = {
                candidate["route_lock"]["result_hash"]
                for candidate in candidates
                if isinstance(candidate, dict)
                and isinstance(candidate.get("route_lock"), dict)
            }
        if lease_bindings is None:
            lease_bindings = {
                lease_scope_binding(candidate)
                for candidate in candidates
                if isinstance(candidate, dict)
            }
        return quota.compile_quota_balance(
            request,
            trusted_key_resolver=self.resolver,
            evaluation_time_utc_z=EVALUATION_TIME,
            verified_route_result_hashes=route_hashes,
            verified_lease_scope_bindings=lease_bindings,
        )

    def compile_v2(
        self,
        quota,
        request: dict[str, object],
        *,
        route_hashes: set[str] | None = None,
        lease_bindings: set[str] | None = None,
        alternate_bindings: set[str] | None = None,
    ) -> dict[str, object]:
        candidates = request.get("candidates", [])
        assert isinstance(candidates, list)
        if route_hashes is None:
            route_hashes = {
                candidate["route_lock"]["result_hash"]
                for candidate in candidates
                if isinstance(candidate, dict)
                and isinstance(candidate.get("route_lock"), dict)
            }
        if lease_bindings is None:
            lease_bindings = {
                lease_scope_binding(candidate)
                for candidate in candidates
                if isinstance(candidate, dict)
            }
        if alternate_bindings is None:
            alternate_bindings = {
                binding["binding_hash"]
                for candidate in candidates
                if isinstance(candidate, dict)
                and isinstance(binding := candidate.get("spark_binding"), dict)
                and binding.get("schema") == "2718lab-devkit/spark-alternate-binding-v1"
            }
        return quota.compile_quota_balance_v2(
            request,
            trusted_key_resolver=self.resolver,
            evaluation_time_utc_z=EVALUATION_TIME,
            verified_route_result_hashes=route_hashes,
            verified_lease_scope_bindings=lease_bindings,
            verified_spark_alternate_binding_hashes=alternate_bindings,
        )

    def test_q01_tight_quota_preserves_the_routed_model_and_effort(self) -> None:
        quota = load_quota_module()
        candidate = make_candidate(
            "q01", model="gpt-5.6-sol", effort="xhigh", lane="sol"
        )
        original_route = copy.deepcopy(candidate["route_lock"])
        decision = self.compile(
            quota,
            make_request(
                snapshot=make_snapshot(main_used=750000), candidates=[candidate]
            ),
        )
        self.assertEqual(original_route, candidate["route_lock"])
        self.assertEqual(8, decision["global_main_target"])
        self.assertEqual([original_route["result_hash"]], decision["route_lock_hashes"])

    def test_q02_untrusted_usage_never_admits_new_work(self) -> None:
        quota = load_quota_module()
        cases: list[tuple[str, dict[str, object]]] = []
        cases.append(("missing", make_request(snapshot={})))
        stale = make_snapshot(
            observed_at="2026-08-01T15:00:00Z", valid_until="2026-08-01T15:02:00Z"
        )
        cases.append(("stale", make_request(snapshot=stale)))
        forged = make_snapshot()
        forged["signature"] = {"algorithm": "hmac-sha256", "value": "0" * 64}
        cases.append(("forged", make_request(snapshot=forged)))
        replayed = make_snapshot(snapshot_seq=0)
        cases.append(("replayed", make_request(snapshot=replayed)))
        foreign = make_snapshot(source_kind="foreign_usage_snapshot")
        cases.append(("foreign", make_request(snapshot=foreign)))
        for name, request in cases:
            with self.subTest(name=name):
                decision = self.compile(quota, request)
                self.assertEqual([], decision["main_proposal_ids"])
                self.assertEqual([], decision["spark_proposal_ids"])
                self.assertEqual([], decision["admitted_candidate_ids"])
        malformed = quota.compile_quota_balance(
            [],
            trusted_key_resolver=self.resolver,
            evaluation_time_utc_z=EVALUATION_TIME,
            verified_route_result_hashes=set(),
            verified_lease_scope_bindings=set(),
        )
        self.assertEqual([], malformed["main_proposal_ids"])
        self.assertEqual([], malformed["spark_proposal_ids"])
        self.assertEqual([], malformed["admitted_candidate_ids"])

    def test_q03_main_boundaries_and_slope_thresholds_are_integer_exact(self) -> None:
        quota = load_quota_module()
        cases = (
            (599999, 4999, "normal", 12),
            (600000, 0, "warm", 10),
            (0, 5000, "warm", 10),
            (750000, 0, "tight", 8),
            (0, 10000, "tight", 8),
            (900000, 0, "critical", 6),
            (0, 20000, "critical", 6),
            (980000, 0, "paused", 6),
        )
        for used, slope, pressure, target in cases:
            with self.subTest(used=used, slope=slope):
                decision = self.compile(
                    quota,
                    make_request(
                        snapshot=make_snapshot(main_used=used, main_slope=slope)
                    ),
                )
                self.assertEqual(pressure, decision["main_pressure"])
                self.assertEqual(target, decision["global_main_target"])
                if pressure == "paused":
                    self.assertEqual([], decision["main_proposal_ids"])

    def test_q04_host_three_slot_cap_is_not_the_global_target(self) -> None:
        quota = load_quota_module()
        candidates = [
            make_candidate(f"q04-{index}", slot=f"slot-{index}")
            for index in range(1, 5)
        ]
        decision = self.compile(
            quota,
            make_request(
                snapshot=make_snapshot(global_main_active=8, host_main_cap=3),
                candidates=candidates,
            ),
        )
        self.assertEqual(12, decision["global_main_target"])
        self.assertEqual(3, len(decision["main_proposal_ids"]))
        wide_host = self.compile(
            quota,
            make_request(
                snapshot=make_snapshot(global_main_active=8, host_main_cap=8),
                candidates=candidates,
            ),
        )
        self.assertEqual(3, len(wide_host["main_proposal_ids"]))

    def test_q05_spark_and_control_occupancy_do_not_consume_main_capacity(self) -> None:
        quota = load_quota_module()
        candidates = [
            make_candidate(f"q05-{index}", slot=f"slot-{index}")
            for index in range(1, 4)
        ]
        decision = self.compile(
            quota,
            make_request(
                snapshot=make_snapshot(
                    global_main_active=10,
                    global_spark_active=1,
                    host_spark_active=1,
                ),
                candidates=candidates,
            ),
        )
        self.assertEqual(2, len(decision["main_proposal_ids"]))
        self.assertNotIn("quota_global_capacity_exhausted", decision["reason_codes"])

    def test_q06_pool_shrink_holds_new_work_without_cancelling_existing_routes(
        self,
    ) -> None:
        quota = load_quota_module()
        candidate = make_candidate("q06")
        original = copy.deepcopy(candidate)
        decision = self.compile(
            quota,
            make_request(
                snapshot=make_snapshot(
                    main_used=900000, global_main_active=8, host_main_active=3
                ),
                candidates=[candidate],
            ),
        )
        self.assertEqual([], decision["main_proposal_ids"])
        self.assertEqual(original, candidate)
        self.assertNotIn("cancelled_candidate_ids", decision)

    def test_q07_spark_holds_inside_deadband_ahead_overshoot_or_cap(self) -> None:
        quota = load_quota_module()
        spark = make_candidate(
            "q07",
            pool="spark",
            lane="spark",
            model="gpt-5.3-codex-spark",
            effort="xhigh",
            spark_binding={
                "spark_proof_hash": digest_text("spark-proof"),
                "parent_main_route_hash": digest_text("parent-route"),
                "parent_admission_id": digest_text("parent-admission"),
                "writer_handoff_hash": digest_text("writer-handoff"),
            },
        )
        cases = (
            (
                "deadband",
                make_snapshot(main_used=500000, spark_used=450000),
                "spark_deadband_hold",
            ),
            (
                "ahead",
                make_snapshot(main_used=500000, spark_used=550000),
                "spark_deadband_hold",
            ),
            (
                "slope",
                make_snapshot(
                    main_used=600000, main_slope=0, spark_used=500000, spark_slope=3000
                ),
                "spark_slope_hold",
            ),
            (
                "cap",
                make_snapshot(
                    main_used=600000, spark_used=500000, global_spark_active=1
                ),
                "spark_cap_hold",
            ),
        )
        for name, snapshot, reason in cases:
            with self.subTest(name=name):
                decision = self.compile(
                    quota, make_request(snapshot=snapshot, candidates=[spark])
                )
                self.assertEqual([], decision["spark_proposal_ids"])
                self.assertIn(reason, decision["reason_codes"])

    def test_q07a_spark_follows_main_when_lag_is_significant(self) -> None:
        quota = load_quota_module()
        spark = make_candidate(
            "q07a",
            pool="spark",
            lane="spark",
            model="gpt-5.3-codex-spark",
            effort="xhigh",
            spark_binding={
                "spark_proof_hash": digest_text("spark-proof"),
                "parent_main_route_hash": digest_text("parent-route"),
                "parent_admission_id": digest_text("parent-admission"),
                "writer_handoff_hash": digest_text("writer-handoff"),
            },
        )
        decision = self.compile(
            quota,
            make_request(
                snapshot=make_snapshot(main_used=650000, spark_used=570000),
                candidates=[spark],
            ),
        )
        self.assertEqual([spark["candidate_id"]], decision["spark_proposal_ids"])
        self.assertIn("spark_follow_lag", decision["reason_codes"])

    def test_q08_ordinary_main_work_cannot_be_transformed_into_a_spark_strike(
        self,
    ) -> None:
        quota = load_quota_module()
        candidate = make_candidate("q08", pool="main", lane="terra")
        decision = self.compile(
            quota,
            make_request(
                snapshot=make_snapshot(main_used=600000, spark_used=100000),
                candidates=[candidate],
            ),
        )
        self.assertEqual([], decision["spark_proposal_ids"])
        self.assertEqual("terra", candidate["route_lock"]["lane"])

    def test_q09_spark_requires_exact_handoff_and_lease_scope_binding(self) -> None:
        quota = load_quota_module()
        spark = make_candidate(
            "q09",
            pool="spark",
            lane="spark",
            model="gpt-5.3-codex-spark",
            effort="xhigh",
            spark_binding={
                "spark_proof_hash": digest_text("proof"),
                "parent_main_route_hash": digest_text("parent-route"),
                "parent_admission_id": digest_text("parent-admission"),
                "writer_handoff_hash": "",
            },
        )
        decision = self.compile(
            quota,
            make_request(
                snapshot=make_snapshot(main_used=700000, spark_used=100000),
                candidates=[spark],
            ),
            lease_bindings=set(),
        )
        self.assertEqual([], decision["spark_proposal_ids"])
        self.assertIn("lease_scope_binding_invalid", decision["reason_codes"])

    def test_q10_only_a_matching_cas_receipt_wins_the_last_global_slot(self) -> None:
        quota = load_quota_module()
        first = make_candidate("q10-a", slot="slot-1")
        second = make_candidate("q10-b", slot="slot-2")
        snapshot = make_snapshot(global_main_active=11, host_main_cap=3)
        proposal = self.compile(
            quota, make_request(snapshot=snapshot, candidates=[first, second])
        )
        winner = make_receipt(first, snapshot, proposal["decision_hash"])
        request = make_request(
            snapshot=snapshot, candidates=[first, second], receipts=[winner]
        )
        decision = self.compile(quota, request)
        self.assertEqual([first["candidate_id"]], decision["admitted_candidate_ids"])
        self.assertIn(second["candidate_id"], decision["held_candidate_ids"])
        self.assertEqual("terra", second["route_lock"]["lane"])

    def test_q11_permutation_and_restart_replay_are_byte_identical(self) -> None:
        quota = load_quota_module()
        first = make_candidate("q11-a", slot="slot-1")
        second = make_candidate("q11-b", slot="slot-2")
        snapshot = make_snapshot(global_main_active=10)
        left = self.compile(
            quota, make_request(snapshot=snapshot, candidates=[first, second])
        )
        right = self.compile(
            quota, make_request(snapshot=snapshot, candidates=[second, first])
        )
        self.assertEqual(left["decision_hash"], right["decision_hash"])
        self.assertEqual(left["audit_event_hash"], right["audit_event_hash"])

    def test_q19_v2_normal_alternate_uses_spark_at_51_main_20_spark(self) -> None:
        quota = load_quota_module()
        alternate = make_v2_normal_spark_alternate("q19")

        decision = self.compile_v2(
            quota,
            make_v2_request(
                snapshot=make_snapshot(main_used=510000, spark_used=200000),
                candidates=[alternate],
            ),
        )

        self.assertEqual([alternate["candidate_id"]], decision["spark_proposal_ids"])
        self.assertIn("spark_alternate_headroom", decision["reason_codes"])

    def test_q20_v2_normal_alternate_holds_inside_deadband(self) -> None:
        quota = load_quota_module()
        alternate = make_v2_normal_spark_alternate("q20")

        decision = self.compile_v2(
            quota,
            make_v2_request(
                snapshot=make_snapshot(main_used=510000, spark_used=470000),
                candidates=[alternate],
            ),
        )

        self.assertEqual([], decision["spark_proposal_ids"])
        self.assertIn("spark_deadband_hold", decision["reason_codes"])

    def test_q21_v2_strike_prioritizes_over_normal_alternate_and_caps_one_card(
        self,
    ) -> None:
        quota = load_quota_module()
        normal = make_v2_normal_spark_alternate("a-normal")
        strike = make_v2_static_spark_strike("z-strike")

        decision = self.compile_v2(
            quota,
            make_v2_request(
                snapshot=make_snapshot(main_used=510000, spark_used=200000),
                candidates=[normal, strike],
            ),
        )

        self.assertEqual([strike["candidate_id"]], decision["spark_proposal_ids"])
        self.assertEqual(1, len(decision["spark_proposal_ids"]))
        self.assertIn("spark_static_priority", decision["reason_codes"])

    def test_q22_v2_tampered_normal_binding_cannot_propose_spark(self) -> None:
        quota = load_quota_module()
        alternate = make_v2_normal_spark_alternate("q22")
        binding = alternate["spark_binding"]
        assert isinstance(binding, dict)
        original_binding_hash = binding["binding_hash"]
        binding["context_hash"] = digest_text("tampered-context")

        decision = self.compile_v2(
            quota,
            make_v2_request(
                snapshot=make_snapshot(main_used=510000, spark_used=200000),
                candidates=[alternate],
            ),
            alternate_bindings={original_binding_hash},
        )

        self.assertEqual([], decision["spark_proposal_ids"])
        self.assertIn("spark_alternate_binding_invalid", decision["reason_codes"])

    def test_q23_v2_zero_host_spark_capacity_holds_static_and_normal_candidates(
        self,
    ) -> None:
        quota = load_quota_module()
        candidates = (
            make_v2_static_spark_strike("q23-static"),
            make_v2_normal_spark_alternate("q23-normal"),
        )

        for candidate in candidates:
            with self.subTest(candidate=candidate["candidate_id"]):
                decision = self.compile_v2(
                    quota,
                    make_v2_request(
                        snapshot=make_snapshot(
                            main_used=510000,
                            spark_used=200000,
                            host_spark_cap=0,
                            host_spark_active=0,
                        ),
                        candidates=[candidate],
                    ),
                )

                self.assertEqual([], decision["spark_proposal_ids"])
                self.assertIn("spark_cap_hold", decision["reason_codes"])

    def test_q12_quota_compiler_has_no_forbidden_side_effect_surface(self) -> None:
        quota = load_quota_module()
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        forbidden = {
            "subprocess",
            "socket",
            "requests",
            "urllib",
            "http",
            "webbrowser",
            "multiprocessing",
        }
        imports = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertFalse(imports & forbidden)
        with (
            mock.patch.object(
                socket, "create_connection", side_effect=AssertionError("network call")
            ),
            mock.patch.object(
                subprocess, "run", side_effect=AssertionError("subprocess call")
            ),
        ):
            decision = self.compile(quota, make_request())
        self.assertEqual("resolved", decision["status"])

    def test_q13_luna_max_is_an_out_of_pool_coordinator(self) -> None:
        quota = load_quota_module()
        candidate = make_candidate("q13")
        before = copy.deepcopy(candidate["route_lock"])
        kernel_input = coordinator_input()
        decision = quota.evaluate_coordinator_kernel(
            kernel_input,
            trusted_capability_hashes={
                kernel_input["profile"]["capability_attestation_hash"]
            },
            trusted_sol_lock_hashes=set(),
            event_seq=11,
        )
        self.assertEqual("MECHANICAL_ALLOWED", decision["status"])
        self.assertEqual(before, candidate["route_lock"])
        self.assertNotIn("luna", candidate["route_lock"].values())

    def test_q14_semantic_or_final_operations_require_a_sol_lock(self) -> None:
        quota = load_quota_module()
        cases = (
            coordinator_input(intake_shape="unstructured"),
            coordinator_input(cross_contract_semantics=True),
            coordinator_input(security_semantics=True),
            coordinator_input(recovery_semantics=True),
            coordinator_input(model_policy_exception=True),
            coordinator_input(quota_policy_exception=True),
            coordinator_input(operation="final_acceptance"),
        )
        for item in cases:
            with self.subTest(operation=item["operation"]):
                decision = quota.evaluate_coordinator_kernel(
                    item,
                    trusted_capability_hashes={
                        item["profile"]["capability_attestation_hash"]
                    },
                    trusted_sol_lock_hashes=set(),
                    event_seq=11,
                )
                self.assertEqual("NEEDS_SOL_LOCK", decision["status"])

    def test_q15_missing_stale_or_foreign_sol_locks_do_not_authorize_progress(
        self,
    ) -> None:
        quota = load_quota_module()
        base = coordinator_input(operation="final_acceptance")
        valid = make_sol_lock(base)
        stale = make_sol_lock(base, expires_event_seq=10)
        foreign = copy.deepcopy(valid)
        foreign["operation"] = "archive"
        foreign["lock_hash"] = hash_value(
            {key: value for key, value in foreign.items() if key != "lock_hash"}
        )
        for lock, trusted in (
            (None, set()),
            (stale, {stale["lock_hash"]}),
            (foreign, {foreign["lock_hash"]}),
        ):
            with self.subTest(lock=lock is not None):
                item = copy.deepcopy(base)
                item["sol_lock"] = lock
                decision = quota.evaluate_coordinator_kernel(
                    item,
                    trusted_capability_hashes={
                        item["profile"]["capability_attestation_hash"]
                    },
                    trusted_sol_lock_hashes=trusted,
                    event_seq=11,
                )
                self.assertEqual("NEEDS_SOL_LOCK", decision["status"])

    def test_q16_a_valid_sol_lock_authorizes_only_its_exact_operation(self) -> None:
        quota = load_quota_module()
        acceptance = coordinator_input(operation="final_acceptance")
        lock = make_sol_lock(acceptance)
        acceptance["sol_lock"] = lock
        allowed = quota.evaluate_coordinator_kernel(
            acceptance,
            trusted_capability_hashes={
                acceptance["profile"]["capability_attestation_hash"]
            },
            trusted_sol_lock_hashes={lock["lock_hash"]},
            event_seq=11,
        )
        adjacent = copy.deepcopy(acceptance)
        adjacent["operation"] = "archive"
        adjacent["operation_input_hash"] = digest_text("operation-input-archive")
        denied = quota.evaluate_coordinator_kernel(
            adjacent,
            trusted_capability_hashes={
                adjacent["profile"]["capability_attestation_hash"]
            },
            trusted_sol_lock_hashes={lock["lock_hash"]},
            event_seq=11,
        )
        self.assertEqual("MECHANICAL_ALLOWED", allowed["status"])
        self.assertEqual("NEEDS_SOL_LOCK", denied["status"])

    def test_q17_main_capacity_evidence_uses_only_main_pool_targets(self) -> None:
        quota = load_quota_module()
        cases = (
            (500_000, 0, 12),
            (650_000, 0, 10),
            (800_000, 0, 8),
            (950_000, 0, 6),
        )
        for main_used, main_slope, expected_target in cases:
            with self.subTest(target=expected_target):
                request = make_request(
                    snapshot=make_snapshot(
                        main_used=main_used,
                        main_slope=main_slope,
                        global_main_active=5,
                        global_spark_active=1,
                    ),
                    candidates=[],
                )
                evidence = quota.compile_main_capacity_evidence(
                    request,
                    trusted_key_resolver=self.resolver,
                    evaluation_time_utc_z=EVALUATION_TIME,
                    verified_route_result_hashes=(),
                    verified_lease_scope_bindings=(),
                )

                self.assertEqual("resolved", evidence["status"])
                self.assertEqual(expected_target, evidence["global_main_target"])
                self.assertEqual(expected_target - 5, evidence["global_main_free"])
                self.assertNotIn("global_spark_active", evidence)

    def test_q18_main_capacity_evidence_fails_closed_when_usage_is_stale_or_untrusted(
        self,
    ) -> None:
        quota = load_quota_module()
        stale = make_request(
            snapshot=make_snapshot(valid_until="2026-08-01T15:09:30Z"),
            candidates=[],
        )
        stale_evidence = quota.compile_main_capacity_evidence(
            stale,
            trusted_key_resolver=self.resolver,
            evaluation_time_utc_z=EVALUATION_TIME,
            verified_route_result_hashes=(),
            verified_lease_scope_bindings=(),
        )
        untrusted_evidence = quota.compile_main_capacity_evidence(
            make_request(candidates=[]),
            trusted_key_resolver=lambda _key_id: None,
            evaluation_time_utc_z=EVALUATION_TIME,
            verified_route_result_hashes=(),
            verified_lease_scope_bindings=(),
        )

        self.assertEqual("blocked", stale_evidence["status"])
        self.assertIn("quota_snapshot_stale", stale_evidence["reason_codes"])
        self.assertEqual("blocked", untrusted_evidence["status"])
        self.assertIn("quota_snapshot_untrusted", untrusted_evidence["reason_codes"])


if __name__ == "__main__":
    unittest.main()
