#!/usr/bin/env python3
"""Run the seven-case open-request suite on the final v4 Harness."""

from __future__ import annotations

import experiments.run_open_requests_candidate_v3 as coordinator


def main() -> int:
    coordinator.CONDITION = "programmable-open-global-diagnostic-v4"
    return coordinator.main()


if __name__ == "__main__":
    raise SystemExit(main())
