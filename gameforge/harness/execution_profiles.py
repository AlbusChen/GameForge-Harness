from __future__ import annotations

from enum import StrEnum


class ExecutionProfile(StrEnum):
    """Host execution boundary selected independently from the solver backend."""

    NATIVE_OPEN = "native-open"
    SUPERVISED_NATIVE = "supervised-native"
    STRONG_ISOLATED = "strong-isolated"


def codex_sandbox_for(profile: ExecutionProfile) -> str:
    if profile is ExecutionProfile.NATIVE_OPEN:
        return "danger-full-access"
    if profile is ExecutionProfile.SUPERVISED_NATIVE:
        return "workspace-write"
    raise ValueError(
        "strong-isolated requires an external isolated runner, not a local Codex sandbox"
    )
