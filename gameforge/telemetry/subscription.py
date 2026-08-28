from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def capture_subscription_snapshot(
    executable: Path,
    *,
    timeout_seconds: float = 20,
) -> dict[str, object]:
    """Read account quota telemetry without exposing account identity or credentials."""
    captured_at = datetime.now(UTC).isoformat()
    try:
        raw = _read_account_telemetry(executable, timeout_seconds=timeout_seconds)
        return _sanitize_snapshot(raw, captured_at=captured_at)
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as error:
        return {
            "schema_version": 1,
            "captured_at": captured_at,
            "status": "unavailable",
            "error_category": type(error).__name__,
        }


def quota_delta(
    before: dict[str, object],
    after: dict[str, object],
) -> dict[str, object]:
    before_weekly = _weekly_bucket(before)
    after_weekly = _weekly_bucket(after)
    before_percent = _number(before_weekly.get("used_percent")) if before_weekly else None
    after_percent = _number(after_weekly.get("used_percent")) if after_weekly else None
    delta = (
        round(after_percent - before_percent, 4)
        if before_percent is not None and after_percent is not None
        else None
    )
    return {
        "schema_version": 1,
        "status": "available" if delta is not None else "unavailable",
        "before_captured_at": before.get("captured_at"),
        "after_captured_at": after.get("captured_at"),
        "weekly_used_percent_before": before_percent,
        "weekly_used_percent_after": after_percent,
        "weekly_delta_percentage_points": delta,
        "weekly_window_minutes": (
            after_weekly.get("window_duration_minutes") if after_weekly else None
        ),
        "weekly_resets_at": after_weekly.get("resets_at") if after_weekly else None,
        "precision_note": (
            "The service reports integer-rounded percent usage. The delta includes only activity "
            "between these two automated snapshots, but can include other concurrent account use."
        ),
    }


def _read_account_telemetry(
    executable: Path,
    *,
    timeout_seconds: float,
) -> dict[int, dict[str, Any]]:
    resolved = executable.resolve(strict=True)
    process = subprocess.Popen(
        [str(resolved), "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_sanitized_environment(),
        start_new_session=os.name == "posix",
    )
    assert process.stdin is not None
    assert process.stdout is not None
    messages = (
        {
            "method": "initialize",
            "id": 0,
            "params": {
                "clientInfo": {
                    "name": "gameforge_harness",
                    "title": "GameForge Harness",
                    "version": "0.1.0",
                }
            },
        },
        {"method": "initialized", "params": {}},
        {"method": "account/rateLimits/read", "id": 1},
        {"method": "account/usage/read", "id": 2},
    )
    for message in messages:
        process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
    process.stdin.flush()

    responses: dict[int, dict[str, Any]] = {}
    deadline = time.monotonic() + timeout_seconds
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        while time.monotonic() < deadline and not {0, 1, 2}.issubset(responses):
            remaining = max(0.0, deadline - time.monotonic())
            if not selector.select(timeout=min(1.0, remaining)):
                if process.poll() is not None:
                    break
                continue
            line = process.stdout.readline()
            if not line:
                break
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            identifier = payload.get("id") if isinstance(payload, dict) else None
            if isinstance(identifier, int) and identifier in {0, 1, 2}:
                if "error" in payload:
                    raise RuntimeError(f"account telemetry request {identifier} failed")
                responses[identifier] = payload
    finally:
        selector.close()
        _terminate(process)
    if not {0, 1, 2}.issubset(responses):
        raise TimeoutError("account telemetry responses were incomplete")
    return responses


def _sanitize_snapshot(
    responses: dict[int, dict[str, Any]],
    *,
    captured_at: str,
) -> dict[str, object]:
    rate_result = responses[1].get("result", {})
    usage_result = responses[2].get("result", {})
    raw_buckets = rate_result.get("rateLimitsByLimitId")
    if not isinstance(raw_buckets, dict):
        fallback = rate_result.get("rateLimits")
        raw_buckets = {"primary": fallback} if isinstance(fallback, dict) else {}

    buckets: list[dict[str, object]] = []
    for raw in raw_buckets.values():
        if not isinstance(raw, dict):
            continue
        window = raw.get("primary")
        if not isinstance(window, dict):
            continue
        duration = _integer(window.get("windowDurationMins"))
        buckets.append(
            {
                "label": _safe_label(raw.get("limitName")),
                "used_percent": _number(window.get("usedPercent")),
                "window_duration_minutes": duration,
                "window_kind": "weekly" if duration == 7 * 24 * 60 else "other",
                "resets_at": _integer(window.get("resetsAt")),
                "plan_type": _safe_label(raw.get("planType")),
                "limit_reached": raw.get("rateLimitReachedType") is not None,
            }
        )
    buckets.sort(
        key=lambda bucket: (
            bucket.get("label") is not None,
            str(bucket.get("label") or ""),
            int(bucket.get("window_duration_minutes") or 0),
        )
    )
    for index, bucket in enumerate(buckets, start=1):
        bucket["bucket"] = f"limit-{index}"

    summary = usage_result.get("summary")
    daily = usage_result.get("dailyUsageBuckets")
    latest_daily = daily[-1] if isinstance(daily, list) and daily else {}
    return {
        "schema_version": 1,
        "captured_at": captured_at,
        "status": "available" if buckets else "unavailable",
        "buckets": buckets,
        "usage_summary": {
            "lifetime_tokens": _integer(summary.get("lifetimeTokens"))
            if isinstance(summary, dict)
            else None,
            "latest_daily_date": _safe_label(latest_daily.get("startDate"))
            if isinstance(latest_daily, dict)
            else None,
            "latest_daily_tokens": _integer(latest_daily.get("tokens"))
            if isinstance(latest_daily, dict)
            else None,
        },
        "privacy": (
            "Account identity, raw bucket identifiers, credentials, and reset-credit details are "
            "not retained."
        ),
    }


def _weekly_bucket(snapshot: dict[str, object]) -> dict[str, object] | None:
    buckets = snapshot.get("buckets")
    if not isinstance(buckets, list):
        return None
    weekly = [
        bucket
        for bucket in buckets
        if isinstance(bucket, dict) and bucket.get("window_kind") == "weekly"
    ]
    if not weekly:
        return None
    return next((bucket for bucket in weekly if bucket.get("label") is None), weekly[0])


def _terminate(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    else:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
        else:
            process.kill()
        process.wait(timeout=5)


def _sanitized_environment() -> dict[str, str]:
    def keep(name: str) -> bool:
        upper = name.upper()
        if upper in {"GH_TOKEN", "GITHUB_TOKEN"}:
            return False
        return not upper.endswith(("_API_KEY", "_ACCESS_TOKEN"))

    return {name: value for name, value in os.environ.items() if keep(name)}


def _safe_label(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value[:120]


def _integer(value: object) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None
