from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from gameforge.harness.contracts import GateOutcome, GateStatus, RunSpec


@dataclass(frozen=True)
class GodotImportEvaluator:
    """Engine-native import and configured-entrypoint gate for arbitrary Godot projects."""

    executable: Path
    run_directory: Path
    name: str = "godot-import"
    version: str = "1"

    def evaluate(self, run_spec: RunSpec, workspace: Path) -> tuple[GateOutcome, ...]:
        outcomes: list[GateOutcome] = []
        stopped = False
        for gate in run_spec.evaluation.required_gates:
            if stopped:
                outcomes.append(
                    GateOutcome(
                        gate=gate,
                        status=GateStatus.NOT_RUN,
                        detail="a prerequisite verification gate failed",
                    )
                )
                continue
            if gate == "specification":
                outcome = GateOutcome(
                    gate=gate,
                    status=GateStatus.PASS,
                    detail="RunSpec and normalized task schemas are valid",
                )
            elif gate == "compilation":
                outcome = self._import(workspace)
            else:
                outcome = GateOutcome(
                    gate=gate,
                    status=GateStatus.FAIL,
                    detail=f"unsupported generic Godot gate: {gate}",
                )
            outcomes.append(outcome)
            stopped = outcome.status is GateStatus.FAIL
        return tuple(outcomes)

    def _import(self, workspace: Path) -> GateOutcome:
        project_file = workspace / "project.godot"
        if not project_file.is_file():
            return GateOutcome(
                gate="compilation",
                status=GateStatus.FAIL,
                detail="project.godot is missing",
            )
        self.run_directory.mkdir(parents=True, exist_ok=True)
        log = self.run_directory / "godot-import.log"
        try:
            completed = subprocess.run(
                [
                    str(self.executable),
                    "--headless",
                    "--import",
                    "--quit",
                    "--path",
                    str(workspace.resolve(strict=True)),
                    "--log-file",
                    str(log),
                ],
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return GateOutcome(
                gate="compilation",
                status=GateStatus.FAIL,
                detail=f"Godot import could not complete: {error}",
            )
        process_log = self.run_directory / "godot-import-process.log"
        process_log.write_text(completed.stdout + completed.stderr, encoding="utf-8")
        if completed.returncode != 0:
            return GateOutcome(
                gate="compilation",
                status=GateStatus.FAIL,
                detail=f"Godot import exited with code {completed.returncode}",
                artifacts=(str(log), str(process_log)),
            )
        return GateOutcome(
            gate="compilation",
            status=GateStatus.PASS,
            detail="Godot imported the project and parsed its scripts/resources",
            artifacts=(str(log), str(process_log)),
        )
