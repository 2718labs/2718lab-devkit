"""Read and sign the local Codex app-server account quota snapshot.

The Codex app-server is the host-owned source of account/rate-limit truth.  This
module intentionally speaks only its documented JSONL stdio protocol; it never
opens ``auth.json``, cookies, or a private HTTP endpoint.  A provider instance
keeps the HMAC key in memory, so a snapshot can be verified by the same host
process that produced it without placing credentials on disk.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import queue
import secrets
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

_HASH_PREFIX = "sha256:"
_SNAPSHOT_SCHEMA = "2718lab-devkit/host-quota-snapshot-v1"
_SOURCE_KIND = "codex_host_usage_snapshot"
_SPARK_LIMIT_NAME = "GPT-5.3-Codex-Spark"
_SAMPLE_WINDOW_SECONDS = 300
_SNAPSHOT_TTL_SECONDS = 120
_MAX_JSONL_LINE_BYTES = 128 * 1024
_MAX_MESSAGES = 64
_CACHE_SCHEMA = "2718lab-devkit/codex-quota-sample-cache-v1"
_STATE_ROOT = "d:\\bun\\tmp\\codex\\"


class CodexQuotaError(RuntimeError):
    """The host quota source did not produce a trusted snapshot."""


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _hash(value: object) -> str:
    return _HASH_PREFIX + hashlib.sha256(
        _canonical_json(value).encode("utf-8")
    ).hexdigest()


def _hash_bytes(value: bytes) -> str:
    return _HASH_PREFIX + hashlib.sha256(value).hexdigest()


def _is_hash(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == len(_HASH_PREFIX) + 64
        and value.startswith(_HASH_PREFIX)
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _utc_z(epoch_seconds: float) -> str:
    return (
        datetime.fromtimestamp(epoch_seconds, tz=UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CodexQuotaError(f"{field} must be an object")
    return value


def _bounded_int(value: object, *, field: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise CodexQuotaError(f"{field} is outside its allowed range")
    return value


def _percent_to_ppm(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CodexQuotaError(f"{field} is not a percentage")
    numeric = float(value)
    if not math.isfinite(numeric) or not 0 <= numeric <= 100:
        raise CodexQuotaError(f"{field} is outside 0..100")
    return max(0, min(1_000_000, round(numeric * 10_000)))


def _default_command() -> list[str]:
    executable = shutil.which("codex.cmd") or shutil.which("codex")
    if executable is None:
        raise CodexQuotaError("codex executable is unavailable")
    return [executable, "app-server", "--stdio"]


def _validate_state_path(value: Path | None) -> Path | None:
    if value is None:
        raw = os.environ.get("CODEX_TASK_TEMP")
        if not raw:
            return None
        value = Path(raw) / "codex-quota-sample-cache.json"
    path = value.expanduser().resolve(strict=False)
    normalized = str(path).replace("/", "\\").lower()
    if os.name == "nt" and not normalized.startswith(_STATE_ROOT):
        raise CodexQuotaError("quota state path must be under the D task root")
    return path


def _validate_capacity(capacity: Mapping[str, Any]) -> dict[str, Any]:
    expected = {
        "ledger_epoch",
        "global_main_active",
        "global_spark_active",
        "host_main_active",
        "host_spark_active",
        "host_main_cap",
        "host_spark_cap",
        "active_lease_set_hash",
    }
    if set(capacity) != expected:
        raise CodexQuotaError("quota capacity fields are invalid")
    result = dict(capacity)
    _bounded_int(result["ledger_epoch"], field="ledger_epoch", minimum=0, maximum=2**63 - 1)
    _bounded_int(result["global_main_active"], field="global_main_active", minimum=0, maximum=12)
    _bounded_int(result["global_spark_active"], field="global_spark_active", minimum=0, maximum=1)
    _bounded_int(result["host_main_active"], field="host_main_active", minimum=0, maximum=8)
    _bounded_int(result["host_spark_active"], field="host_spark_active", minimum=0, maximum=1)
    _bounded_int(result["host_main_cap"], field="host_main_cap", minimum=0, maximum=8)
    _bounded_int(result["host_spark_cap"], field="host_spark_cap", minimum=0, maximum=1)
    if not _is_hash(result["active_lease_set_hash"]):
        raise CodexQuotaError("active lease set hash is invalid")
    return result


class _JsonlSession:
    """Small bounded JSONL RPC client with no shell and no secret logging."""

    def __init__(self, command: Sequence[str], *, timeout_seconds: float) -> None:
        if not command or any(not isinstance(item, str) or not item for item in command):
            raise CodexQuotaError("app-server command is invalid")
        if not 0.1 <= timeout_seconds <= 30:
            raise CodexQuotaError("app-server timeout is invalid")
        self._command = list(command)
        self._timeout_seconds = timeout_seconds
        self._process: subprocess.Popen[bytes] | None = None
        self._messages: queue.Queue[object] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._next_id = 1

    def __enter__(self) -> Self:
        try:
            self._process = subprocess.Popen(
                self._command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                shell=False,
                env=os.environ.copy(),
            )
        except (OSError, ValueError) as error:
            raise CodexQuotaError("app-server could not be started") from error
        assert self._process.stdout is not None

        def read_lines() -> None:
            assert self._process is not None
            assert self._process.stdout is not None
            try:
                for line in self._process.stdout:
                    self._messages.put(line)
            finally:
                self._messages.put(None)

        self._reader = threading.Thread(target=read_lines, daemon=True)
        self._reader.start()
        return self

    def __exit__(self, *_: object) -> None:
        process = self._process
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            process.terminate()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def _send(self, message: Mapping[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise CodexQuotaError("app-server session is not running")
        encoded = (_canonical_json(message) + "\n").encode("utf-8")
        try:
            process.stdin.write(encoded)
            process.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise CodexQuotaError("app-server protocol write failed") from error

    def request(self, method: str, params: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        request: dict[str, Any] = {"method": method, "id": request_id}
        if params is not None:
            request["params"] = dict(params)
        self._send(request)
        deadline = time.monotonic() + self._timeout_seconds
        for _ in range(_MAX_MESSAGES):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexQuotaError("app-server protocol timed out")
            try:
                raw = self._messages.get(timeout=remaining)
            except queue.Empty as error:
                raise CodexQuotaError("app-server protocol timed out") from error
            if raw is None:
                raise CodexQuotaError("app-server closed before response")
            if not isinstance(raw, bytes) or len(raw) > _MAX_JSONL_LINE_BYTES:
                raise CodexQuotaError("app-server response is oversized")
            try:
                message = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise CodexQuotaError("app-server response is not JSONL") from error
            if not isinstance(message, Mapping):
                raise CodexQuotaError("app-server response is not an object")
            if "id" not in message:
                continue
            if message.get("id") != request_id:
                raise CodexQuotaError("app-server response id is mismatched")
            if "error" in message:
                raise CodexQuotaError(f"app-server rejected {method}")
            return _mapping(message.get("result"), f"app-server {method} result")
        raise CodexQuotaError("app-server response limit exceeded")


@dataclass(frozen=True)
class QuotaSnapshotEvidence:
    """A signed snapshot and its in-memory verifier, never a serialized secret."""

    snapshot: Mapping[str, Any]
    key_id: str
    _key: bytes
    plan_type: str
    main_limit_id: str
    spark_limit_id: str

    def key_resolver(self, key_id: str) -> bytes | None:
        if key_id == self.key_id:
            return self._key
        return None


class CodexQuotaProvider:
    """Fetch official app-server limits and produce a host-trusted snapshot."""

    def __init__(
        self,
        *,
        command: Sequence[str] | None = None,
        executable: str | None = None,
        timeout_seconds: float = 8.0,
        state_path: Path | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        if command is not None and executable is not None:
            raise CodexQuotaError("command and executable are mutually exclusive")
        if command is None:
            if executable is not None:
                command = [executable, "app-server", "--stdio"]
            else:
                command = _default_command()
        self._command = list(command)
        self._timeout_seconds = timeout_seconds
        self._state_path = _validate_state_path(state_path)
        self._now = now or time.time

    def _read_cache(self) -> Mapping[str, Any] | None:
        if self._state_path is None:
            return None
        try:
            if self._state_path.stat().st_size > 16 * 1024:
                return None
            value = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(value, Mapping) or value.get("schema") != _CACHE_SCHEMA:
            return None
        return value

    def _write_cache(self, value: Mapping[str, Any]) -> None:
        if self._state_path is None:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._state_path.with_name(
                f"{self._state_path.name}.{secrets.token_hex(6)}.tmp"
            )
            temporary.write_text(_canonical_json(value), encoding="utf-8")
            temporary.replace(self._state_path)
        except OSError as error:
            raise CodexQuotaError("quota sample cache could not be written") from error

    def _sample_delta(
        self,
        *,
        pool_name: str,
        period_id_hash: str,
        used_ppm: int,
        now_epoch: float,
        previous: Mapping[str, Any] | None,
    ) -> int:
        if previous is None:
            return 0
        raw = previous.get(pool_name)
        if not isinstance(raw, Mapping):
            return 0
        if raw.get("period_id_hash") != period_id_hash:
            return 0
        old_used = raw.get("used_ppm")
        old_at = raw.get("observed_at_epoch")
        if type(old_used) is not int or type(old_at) not in {int, float}:
            return 0
        age = now_epoch - float(old_at)
        if not 1 <= age <= 900:
            return 0
        increase = max(0, used_ppm - old_used)
        return max(0, min(1_000_000, round(increase * _SAMPLE_WINDOW_SECONDS / age)))

    @staticmethod
    def _pool_entry(
        limits: Mapping[str, Any], *, limit_id: str, limit_name: str | None = None
    ) -> tuple[str, Mapping[str, Any]]:
        by_id = _mapping(limits.get("rateLimitsByLimitId"), "rateLimitsByLimitId")
        matches: list[tuple[str, Mapping[str, Any]]] = []
        for key, raw in by_id.items():
            if not isinstance(key, str) or not isinstance(raw, Mapping):
                raise CodexQuotaError("rate limit map entry is invalid")
            if (limit_name is None and key == limit_id) or (
                limit_name is not None and raw.get("limitName") == limit_name
            ):
                matches.append((key, raw))
        if len(matches) != 1:
            raise CodexQuotaError("required Codex rate-limit pool is unavailable")
        actual_id, entry = matches[0]
        if entry.get("limitId") != actual_id:
            raise CodexQuotaError("rate-limit identity is mismatched")
        return actual_id, entry

    @staticmethod
    def _pool_values(entry: Mapping[str, Any], *, field: str) -> tuple[int, int, int]:
        primary = _mapping(entry.get("primary"), f"{field}.primary")
        used_ppm = _percent_to_ppm(primary.get("usedPercent"), field=f"{field}.usedPercent")
        window = _bounded_int(
            primary.get("windowDurationMins"),
            field=f"{field}.windowDurationMins",
            minimum=1,
            maximum=1_051_200,
        )
        resets_at = _bounded_int(
            primary.get("resetsAt"),
            field=f"{field}.resetsAt",
            minimum=1,
            maximum=2**63 - 1,
        )
        return used_ppm, window, resets_at

    def read(self, *, capacity: Mapping[str, Any]) -> QuotaSnapshotEvidence:
        capacity_value = _validate_capacity(capacity)
        now_epoch = float(self._now())
        if not math.isfinite(now_epoch) or now_epoch <= 0:
            raise CodexQuotaError("quota observation time is invalid")
        with _JsonlSession(self._command, timeout_seconds=self._timeout_seconds) as session:
            initialize = session.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "2718lab_devkit_quota",
                        "title": "2718Lab DevKit quota source",
                        "version": "1.0.0",
                    }
                },
            )
            if not isinstance(initialize, Mapping):
                raise CodexQuotaError("app-server initialize result is invalid")
            session._send({"method": "initialized", "params": {}})
            account = session.request("account/read", {"refreshToken": False})
            limits = session.request("account/rateLimits/read")

        account_details = account.get("account")
        if isinstance(account_details, Mapping):
            account = account_details
        if account.get("type") != "chatgpt":
            raise CodexQuotaError("Codex account is not ChatGPT-managed")
        plan_type = account.get("planType")
        if not isinstance(plan_type, str) or not plan_type:
            raise CodexQuotaError("Codex account plan is unavailable")

        main_limit_id, main_entry = self._pool_entry(limits, limit_id="codex")
        spark_limit_id, spark_entry = self._pool_entry(
            limits, limit_id="", limit_name=_SPARK_LIMIT_NAME
        )
        main_used, main_window, main_reset = self._pool_values(main_entry, field="main")
        spark_used, spark_window, spark_reset = self._pool_values(spark_entry, field="spark")
        main_period = _hash(
            {"limit_id": main_limit_id, "window_duration_mins": main_window, "resets_at": main_reset}
        )
        spark_period = _hash(
            {"limit_id": spark_limit_id, "window_duration_mins": spark_window, "resets_at": spark_reset}
        )
        previous = self._read_cache()
        main_delta = self._sample_delta(
            pool_name="main",
            period_id_hash=main_period,
            used_ppm=main_used,
            now_epoch=now_epoch,
            previous=previous,
        )
        spark_delta = self._sample_delta(
            pool_name="spark",
            period_id_hash=spark_period,
            used_ppm=spark_used,
            now_epoch=now_epoch,
            previous=previous,
        )
        previous_seq = previous.get("snapshot_seq") if isinstance(previous, Mapping) else None
        snapshot_seq = int(now_epoch * 1000)
        if type(previous_seq) is int:
            snapshot_seq = max(snapshot_seq, previous_seq + 1)
        source = {
            "kind": _SOURCE_KIND,
            "source_id_hash": _hash("codex-app-server-account-rate-limits"),
        }
        key = secrets.token_bytes(32)
        key_id = _hash_bytes(key)
        source["key_id"] = key_id
        observed = _utc_z(now_epoch)
        valid_until = _utc_z(now_epoch + _SNAPSHOT_TTL_SECONDS)
        unsigned: dict[str, Any] = {
            "schema": _SNAPSHOT_SCHEMA,
            "source": source,
            "snapshot_seq": snapshot_seq,
            "observed_at_utc_z": observed,
            "valid_until_utc_z": valid_until,
            "sample_window_seconds": _SAMPLE_WINDOW_SECONDS,
            "main": {
                "period_id_hash": main_period,
                "used_ppm": main_used,
                "delta_ppm_300s": main_delta,
            },
            "spark": {
                "period_id_hash": spark_period,
                "used_ppm": spark_used,
                "delta_ppm_300s": spark_delta,
            },
            "capacity": capacity_value,
        }
        snapshot_hash = _hash(unsigned)
        signed = {**unsigned, "snapshot_hash": snapshot_hash}
        signature = hmac.new(
            key, _canonical_json(signed).encode("utf-8"), hashlib.sha256
        ).hexdigest()
        snapshot = {
            **signed,
            "signature": {"algorithm": "hmac-sha256", "value": signature},
        }
        self._write_cache(
            {
                "schema": _CACHE_SCHEMA,
                "snapshot_seq": snapshot_seq,
                "main": {
                    "period_id_hash": main_period,
                    "used_ppm": main_used,
                    "observed_at_epoch": now_epoch,
                },
                "spark": {
                    "period_id_hash": spark_period,
                    "used_ppm": spark_used,
                    "observed_at_epoch": now_epoch,
                },
            }
        )
        return QuotaSnapshotEvidence(
            snapshot=snapshot,
            key_id=key_id,
            _key=key,
            plan_type=plan_type,
            main_limit_id=main_limit_id,
            spark_limit_id=spark_limit_id,
        )


def _normalized_request_hash(request: Mapping[str, Any]) -> str:
    body = {key: value for key, value in request.items() if key != "request_hash"}
    for field, key in (("candidates", "candidate_id"), ("receipts", "receipt_hash")):
        values = body.get(field)
        if isinstance(values, list):
            body[field] = sorted(
                values,
                key=lambda value: str(value.get(key, "")) if isinstance(value, Mapping) else "",
            )
    return _hash(body)


def attach_snapshot(
    quota_request: Mapping[str, Any], evidence: QuotaSnapshotEvidence
) -> dict[str, Any]:
    """Bind a fresh snapshot to a request and recompute its exact request hash."""

    request = dict(quota_request)
    request["snapshot"] = dict(evidence.snapshot)
    request["request_hash"] = _normalized_request_hash(request)
    return request


__all__ = [
    "CodexQuotaError",
    "CodexQuotaProvider",
    "QuotaSnapshotEvidence",
    "attach_snapshot",
]
