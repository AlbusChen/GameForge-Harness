from __future__ import annotations

import hashlib
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from gameforge.orchestrator.policy import PolicyViolation, ToolPolicy
from gameforge.schemas.diagnostic import PatchResult, TextReplacement

MAX_PATCHABLE_BYTES = 512 * 1024


@dataclass
class RepairBudget:
    maximum: int
    max_seconds: float = 900.0
    max_cost_usd: float = 0.0
    attempts: int = 0
    cost_usd: float = 0.0
    started_at: float = field(default_factory=time.monotonic)

    def consume(self, *, cost_usd: float = 0.0) -> int:
        if self.attempts >= self.maximum:
            raise RuntimeError("repair loop budget exhausted")
        if time.monotonic() - self.started_at > self.max_seconds:
            raise RuntimeError("repair loop time budget exhausted")
        if self.cost_usd + cost_usd > self.max_cost_usd:
            raise RuntimeError("repair loop cost budget exhausted")
        self.attempts += 1
        self.cost_usd += cost_usd
        return self.attempts


def apply_text_replacement(
    project_root: Path,
    proposal: TextReplacement,
    *,
    allowed_roots: tuple[Path, ...],
) -> PatchResult:
    relative = Path(proposal.path)
    if relative.is_absolute() or ".." in relative.parts:
        raise PolicyViolation(f"patch path must be a safe relative path: {proposal.path}")

    policy = ToolPolicy(project_root)
    target = policy.require_project_path(project_root / relative)
    resolved_roots = tuple(
        policy.require_project_path(project_root / root) for root in allowed_roots
    )
    if not any(target.is_relative_to(root) for root in resolved_roots):
        raise PolicyViolation(f"patch path is outside approved roots: {proposal.path}")
    if target.suffix != ".cs":
        raise PolicyViolation(f"only C# source patches are allowed: {proposal.path}")
    if not target.is_file():
        raise FileNotFoundError(target)

    before = target.read_text(encoding="utf-8")
    before_bytes = before.encode("utf-8")
    if len(before_bytes) > MAX_PATCHABLE_BYTES:
        raise PolicyViolation(f"patch target exceeds {MAX_PATCHABLE_BYTES} bytes")
    replacements = before.count(proposal.expected)
    if replacements != 1:
        raise ValueError(
            f"expected patch anchor exactly once in {proposal.path}, found {replacements}"
        )

    after = before.replace(proposal.expected, proposal.replacement, 1)
    after_bytes = after.encode("utf-8")
    if len(after_bytes) > MAX_PATCHABLE_BYTES:
        raise PolicyViolation(f"patched file exceeds {MAX_PATCHABLE_BYTES} bytes")

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            stream.write(after)
            stream.flush()
            os.fsync(stream.fileno())
            temporary_path = Path(stream.name)
        temporary_path.chmod(target.stat().st_mode)
        os.replace(temporary_path, target)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    return PatchResult(
        path=proposal.path,
        replacements=replacements,
        bytes_before=len(before_bytes),
        bytes_after=len(after_bytes),
        sha256_before=hashlib.sha256(before_bytes).hexdigest(),
        sha256_after=hashlib.sha256(after_bytes).hexdigest(),
    )
