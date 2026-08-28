#!/usr/bin/env python3
"""Run the global-budget, batched-discovery Programmable Harness candidate."""

from __future__ import annotations

import sys

import experiments.run_unseen30_candidate_v2 as coordinator


def main() -> int:
    coordinator.CANDIDATE_CONDITION = "programmable-open-global-v3"
    return coordinator.main()


if __name__ == "__main__":
    sys.exit(main())
