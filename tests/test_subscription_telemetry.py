from __future__ import annotations

import json

from gameforge.telemetry.subscription import _sanitize_snapshot, quota_delta


def test_subscription_snapshot_keeps_quota_but_drops_account_identifiers() -> None:
    snapshot = _sanitize_snapshot(
        {
            1: {
                "result": {
                    "rateLimitsByLimitId": {
                        "raw-secret-id": {
                            "limitName": "standard",
                            "planType": "pro",
                            "primary": {
                                "usedPercent": 46,
                                "windowDurationMins": 10080,
                                "resetsAt": 1_786_636_918,
                            },
                            "credits": {"balance": "not-retained"},
                        }
                    }
                }
            },
            2: {
                "result": {
                    "summary": {"lifetimeTokens": 1234, "email": "not-retained"},
                    "dailyUsageBuckets": [
                        {"startDate": "2026-08-08", "tokens": 321, "requests": []}
                    ],
                }
            },
        },
        captured_at="2026-08-08T00:00:00+00:00",
    )

    encoded = json.dumps(snapshot, sort_keys=True)
    assert snapshot["status"] == "available"
    assert snapshot["buckets"][0]["window_kind"] == "weekly"  # type: ignore[index]
    assert snapshot["buckets"][0]["used_percent"] == 46.0  # type: ignore[index]
    assert snapshot["usage_summary"]["latest_daily_tokens"] == 321  # type: ignore[index]
    assert "raw-secret-id" not in encoded
    assert "not-retained" not in encoded
    assert "email" not in encoded


def test_subscription_quota_delta_reports_weekly_percentage_points() -> None:
    before = {
        "captured_at": "before",
        "buckets": [
            {
                "window_kind": "weekly",
                "used_percent": 46.0,
                "window_duration_minutes": 10080,
                "resets_at": 123,
            }
        ],
    }
    after = {
        "captured_at": "after",
        "buckets": [
            {
                "window_kind": "weekly",
                "used_percent": 48.0,
                "window_duration_minutes": 10080,
                "resets_at": 123,
            }
        ],
    }

    delta = quota_delta(before, after)

    assert delta["status"] == "available"
    assert delta["weekly_delta_percentage_points"] == 2.0
    assert delta["weekly_window_minutes"] == 10080


def test_subscription_quota_delta_ignores_reordered_model_specific_bucket() -> None:
    main = {
        "label": None,
        "window_kind": "weekly",
        "used_percent": 46.0,
        "window_duration_minutes": 10080,
        "resets_at": 123,
    }
    model_specific = {
        "label": "separate-model-limit",
        "window_kind": "weekly",
        "used_percent": 0.0,
        "window_duration_minutes": 10080,
        "resets_at": 456,
    }

    delta = quota_delta(
        {"captured_at": "before", "buckets": [main, model_specific]},
        {"captured_at": "after", "buckets": [model_specific, main]},
    )

    assert delta["weekly_delta_percentage_points"] == 0.0
    assert delta["weekly_resets_at"] == 123
