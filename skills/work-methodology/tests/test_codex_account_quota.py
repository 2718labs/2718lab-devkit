from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "codex_account_quota.py"
BALANCE_SCRIPT = ROOT / "scripts" / "fastlane_quota_balance.py"


def load_provider():
    spec = importlib.util.spec_from_file_location("codex_account_quota", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load quota provider: {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["codex_account_quota"] = module
    spec.loader.exec_module(module)
    return module


def load_balance():
    spec = importlib.util.spec_from_file_location("fastlane_quota_balance", BALANCE_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load quota balance: {BALANCE_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["fastlane_quota_balance"] = module
    spec.loader.exec_module(module)
    return module


CAPACITY = {
    "ledger_epoch": 7,
    "global_main_active": 1,
    "global_spark_active": 0,
    "host_main_active": 1,
    "host_spark_active": 0,
    "host_main_cap": 3,
    "host_spark_cap": 1,
    "active_lease_set_hash": "sha256:" + "a" * 64,
}


def fake_server_code(
    *,
    spark_label: str = "GPT-5.3-Codex-Spark",
    nested_account: bool = False,
    main_used: int = 50,
    spark_used: int = 16,
) -> str:
    payload = {
        "account": {
            "type": "chatgpt",
            "planType": "pro",
            "requiresOpenaiAuth": True,
        },
        "limits": {
            "rateLimits": {
                "limitId": "codex",
                "primary": {
                    "usedPercent": main_used,
                    "windowDurationMins": 10080,
                    "resetsAt": 1786162042,
                },
                "secondary": None,
                "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
                "planType": "pro",
            },
            "rateLimitsByLimitId": {
                "codex": {
                    "limitId": "codex",
                    "primary": {
                        "usedPercent": main_used,
                        "windowDurationMins": 10080,
                        "resetsAt": 1786162042,
                    },
                    "secondary": None,
                    "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
                    "planType": "pro",
                },
                "codex_bengalfox": {
                    "limitId": "codex_bengalfox",
                    "limitName": spark_label,
                    "primary": {
                        "usedPercent": spark_used,
                        "windowDurationMins": 10080,
                        "resetsAt": 1786171853,
                    },
                    "secondary": None,
                    "credits": None,
                    "planType": "pro",
                },
            },
        },
    }
    if nested_account:
        payload["account"] = {
            "account": payload["account"],
            "requiresOpenaiAuth": True,
        }
    encoded = json.dumps(payload, separators=(",", ":"))
    return f"""
import json, sys
payload = json.loads({encoded!r})
for raw in sys.stdin:
    message = json.loads(raw)
    method = message.get("method")
    if method == "initialize":
        print(json.dumps({{"id": message["id"], "result": {{"userAgent": "fake"}}}}), flush=True)
    elif method == "account/read":
        print(json.dumps({{"id": message["id"], "result": payload["account"]}}), flush=True)
    elif method == "account/rateLimits/read":
        print(json.dumps({{"id": message["id"], "result": payload["limits"]}}), flush=True)
"""


class CodexAccountQuotaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="quota-provider-", dir="D:/bun/tmp/codex"))

    def tearDown(self) -> None:
        for path in sorted(self.temp.rglob("*"), reverse=True):
            if path.is_file() or path.is_symlink():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        self.temp.rmdir()

    def provider(self, *, spark_label: str = "GPT-5.3-Codex-Spark"):
        module = load_provider()
        return module.CodexQuotaProvider(
            command=[sys.executable, "-c", fake_server_code(spark_label=spark_label)],
            state_path=self.temp / "sample-cache.json",
            now=lambda: 1786100000.0,
        )

    def test_reads_official_jsonl_and_builds_verifiable_main_and_spark_snapshot(self) -> None:
        provider = self.provider()
        evidence = provider.read(capacity=CAPACITY)

        self.assertEqual("pro", evidence.plan_type)
        self.assertEqual(500000, evidence.snapshot["main"]["used_ppm"])
        self.assertEqual(160000, evidence.snapshot["spark"]["used_ppm"])
        self.assertEqual("codex", evidence.main_limit_id)
        self.assertEqual("codex_bengalfox", evidence.spark_limit_id)
        self.assertTrue(evidence.key_resolver(evidence.key_id))

        balance = load_balance()
        verified, _ = balance._verified_snapshot(
            evidence.snapshot,
            trusted_key_resolver=evidence.key_resolver,
            evaluation_time_utc_z=evidence.snapshot["observed_at_utc_z"],
        )
        self.assertEqual(evidence.snapshot, verified)

    def test_spark_label_must_be_exact(self) -> None:
        module = load_provider()
        provider = module.CodexQuotaProvider(
            command=[sys.executable, "-c", fake_server_code(spark_label="not-spark")],
            state_path=self.temp / "sample-cache.json",
            now=lambda: 1786100000.0,
        )
        with self.assertRaises(module.CodexQuotaError):
            provider.read(capacity=CAPACITY)

    def test_accepts_current_nested_account_read_shape(self) -> None:
        module = load_provider()
        provider = module.CodexQuotaProvider(
            command=[sys.executable, "-c", fake_server_code(nested_account=True)],
            state_path=self.temp / "nested-cache.json",
            now=lambda: 1786100000.0,
        )
        evidence = provider.read(capacity=CAPACITY)
        self.assertEqual("pro", evidence.plan_type)

    def test_attach_snapshot_rehashes_request_without_exposing_key(self) -> None:
        module = load_provider()
        provider = self.provider()
        evidence = provider.read(capacity=CAPACITY)
        balance = load_balance()
        policy = balance._policy()
        request = {
            "schema": "2718lab-devkit/fastlane-quota-balance-request-v1",
            "policy_hash": balance._hash(policy),
            "candidates": [],
            "receipts": [],
            "snapshot": evidence.snapshot,
            "request_hash": "sha256:" + "0" * 64,
        }
        attached = module.attach_snapshot(request, evidence)
        self.assertEqual(evidence.snapshot, attached["snapshot"])
        self.assertEqual(balance._normalized_request_hash(attached), attached["request_hash"])
        self.assertNotIn("key", attached)
        self.assertNotIn("secret", attached)

    def test_second_sample_records_same_period_usage_slope(self) -> None:
        module = load_provider()
        first = module.CodexQuotaProvider(
            command=[sys.executable, "-c", fake_server_code(main_used=50)],
            state_path=self.temp / "slope-cache.json",
            now=lambda: 1786100000.0,
        ).read(capacity=CAPACITY)
        second = module.CodexQuotaProvider(
            command=[sys.executable, "-c", fake_server_code(main_used=51)],
            state_path=self.temp / "slope-cache.json",
            now=lambda: 1786100300.0,
        ).read(capacity=CAPACITY)
        self.assertEqual(0, first.snapshot["main"]["delta_ppm_300s"])
        self.assertEqual(10000, second.snapshot["main"]["delta_ppm_300s"])

    def test_state_cache_accepts_explicit_user_configured_g_drive_path(self) -> None:
        module = load_provider()
        module.CodexQuotaProvider(state_path=Path("G:/2718lab-devkit/quota-cache.json"))

    def test_state_cache_rejects_unapproved_c_drive_temp_path(self) -> None:
        module = load_provider()
        with self.assertRaises(module.CodexQuotaError):
            module.CodexQuotaProvider(state_path=Path("C:/codex-quota-cache.json"))


if __name__ == "__main__":
    unittest.main()
