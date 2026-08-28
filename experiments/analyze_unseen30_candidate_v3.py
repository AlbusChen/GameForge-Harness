#!/usr/bin/env python3
"""Analyze the global-budget, batched-discovery Programmable Harness candidate."""

from __future__ import annotations

import experiments.analyze_unseen30_candidate_v2 as analysis


def main() -> None:
    analysis.CANDIDATE = "programmable-open-global-v3"
    analysis.CONDITIONS = (
        "official-default",
        "legacy-tool-v1",
        "programmable-v1",
        analysis.CANDIDATE,
    )
    analysis.main()


if __name__ == "__main__":
    main()
