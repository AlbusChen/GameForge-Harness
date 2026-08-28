from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from gameforge.harness.contracts import GateOutcome, GateStatus, RunSpec
from gameforge.harness.interfaces import EvaluatorAdapter

_KINDS = frozenset({"file_exists", "file_contains", "file_not_contains", "file_sha256"})


@dataclass(frozen=True)
class FileAssertion:
    kind: str
    path: str
    expected: str | None = None

    @classmethod
    def parse(cls, expression: str) -> FileAssertion:
        kind, separator, remainder = expression.partition(":")
        if not separator or kind not in _KINDS:
            raise ValueError(
                "acceptance must use file_exists:PATH, file_contains:PATH::TEXT, "
                "file_not_contains:PATH::TEXT, or file_sha256:PATH::HEX"
            )
        raw_path, value_separator, expected = remainder.partition("::")
        path = _safe_relative(raw_path)
        if kind == "file_exists":
            if value_separator:
                raise ValueError("file_exists does not accept an expected value")
            return cls(kind=kind, path=path)
        if not value_separator or not expected:
            raise ValueError(f"{kind} requires a non-empty value after ::")
        if kind == "file_sha256" and (
            len(expected) != 64
            or any(character not in "0123456789abcdef" for character in expected)
        ):
            raise ValueError("file_sha256 requires a lowercase 64-character SHA-256 value")
        return cls(kind=kind, path=path, expected=expected)

    def evaluate(self, workspace: Path) -> tuple[bool, str]:
        root = workspace.resolve(strict=True)
        target = root / self.path
        if target.is_symlink():
            return False, f"{self.path}: symbolic links are not accepted"
        if self.kind == "file_exists":
            passed = target.is_file()
            return passed, f"{self.path}: {'exists' if passed else 'missing'}"
        if not target.is_file():
            return False, f"{self.path}: missing"
        resolved = target.resolve(strict=True)
        if not resolved.is_relative_to(root):
            return False, f"{self.path}: escapes workspace"
        payload = resolved.read_bytes()
        if self.kind == "file_sha256":
            actual = hashlib.sha256(payload).hexdigest()
            passed = actual == self.expected
            return passed, f"{self.path}: sha256={actual}"
        try:
            content = payload.decode("utf-8")
        except UnicodeDecodeError:
            return False, f"{self.path}: is not UTF-8 text"
        assert self.expected is not None
        contains = self.expected in content
        passed = contains if self.kind == "file_contains" else not contains
        expectation = "contains" if self.kind == "file_contains" else "does not contain"
        return passed, f"{self.path}: {expectation} approved text = {str(passed).lower()}"


def validate_acceptance(expressions: tuple[str, ...]) -> tuple[FileAssertion, ...]:
    if not expressions:
        raise ValueError("project execution requires at least one independent acceptance assertion")
    return tuple(FileAssertion.parse(expression) for expression in expressions)


def evaluate_acceptance(expressions: tuple[str, ...], workspace: Path) -> GateOutcome:
    assertions = validate_acceptance(expressions)
    details: list[str] = []
    passed = True
    for assertion in assertions:
        assertion_passed, detail = assertion.evaluate(workspace)
        passed = passed and assertion_passed
        details.append(detail)
    return GateOutcome(
        gate="task_acceptance",
        status=GateStatus.PASS if passed else GateStatus.FAIL,
        detail="; ".join(details),
    )


@dataclass(frozen=True)
class AcceptanceEvaluator:
    downstream: EvaluatorAdapter
    name: str = "task-acceptance"
    version: str = "1"

    def evaluate(self, run_spec: RunSpec, workspace: Path) -> tuple[GateOutcome, ...]:
        delegated_gates = tuple(
            gate for gate in run_spec.evaluation.required_gates if gate != "task_acceptance"
        )
        delegated_evaluation = run_spec.evaluation.model_copy(
            update={"required_gates": delegated_gates}
        )
        delegated_spec = run_spec.model_copy(update={"evaluation": delegated_evaluation})
        downstream = {
            outcome.gate: outcome for outcome in self.downstream.evaluate(delegated_spec, workspace)
        }
        outcomes: list[GateOutcome] = []
        for gate in run_spec.evaluation.required_gates:
            if gate == "task_acceptance":
                outcomes.append(evaluate_acceptance(run_spec.task.acceptance, workspace))
            else:
                outcomes.append(
                    downstream.get(
                        gate,
                        GateOutcome(
                            gate=gate,
                            status=GateStatus.NOT_RUN,
                            detail="downstream evaluator did not return the required gate",
                        ),
                    )
                )
        return tuple(outcomes)


def _safe_relative(raw_path: str) -> str:
    relative = Path(raw_path)
    if not raw_path or relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"assertion path must be project-relative and cannot traverse: {raw_path}")
    return relative.as_posix()
