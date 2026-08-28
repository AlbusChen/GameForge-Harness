from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from gameforge.adapters.unity import UnityToolResult
from gameforge.adapters.unity_harness import UnityHarnessEngineAdapter, UnityNativeEvaluator
from gameforge.harness.contracts import (
    EngineName,
    EvaluationSpec,
    GateStatus,
    HarnessProfile,
)
from gameforge.harness.model_profiles import ModelProfile
from gameforge.harness.run_factory import create_run_spec
from gameforge.harness.task_sources import ChangeRequestTaskSource
from gameforge.orchestrator.policy import PolicyViolation


@dataclass
class FakeGateway:
    project: Path
    run_directory: Path

    def _result(self, tool: str, artifact: Path | None = None) -> UnityToolResult:
        log = self.run_directory / f"{tool}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("ok\n", encoding="utf-8")
        artifacts = (artifact,) if artifact is not None else ()
        return UnityToolResult(tool, 0, log, artifacts)

    def health_check(self) -> UnityToolResult:
        artifact = self.run_directory / "health.json"
        artifact.write_text("{}\n", encoding="utf-8")
        return self._result("health_check", artifact)

    def run_tests(self, platform: str) -> UnityToolResult:
        artifact = self.run_directory / f"{platform}.xml"
        artifact.write_text('<test-run result="Passed" failed="0" />\n', encoding="utf-8")
        return self._result(f"run_{platform}", artifact)

    def build_macos(self) -> UnityToolResult:
        artifact = self.run_directory / "Arena.app"
        artifact.mkdir(exist_ok=True)
        return self._result("build_macos", artifact)

    def launch_build_smoke_test(self) -> UnityToolResult:
        artifact = self.run_directory / "smoke.json"
        artifact.write_text("{}\n", encoding="utf-8")
        return self._result("smoke", artifact)


def test_unity_adapter_patches_only_exact_approved_file(tmp_path: Path) -> None:
    project = tmp_path / "project"
    source_dir = project / "Assets" / "Scripts"
    source_dir.mkdir(parents=True)
    approved = source_dir / "Weapon.cs"
    approved.write_text("damage = 10;\n", encoding="utf-8")
    other = source_dir / "Enemy.cs"
    other.write_text("health = 20;\n", encoding="utf-8")
    gateway = FakeGateway(project, tmp_path / "run")
    gateway.run_directory.mkdir()
    adapter = UnityHarnessEngineAdapter(
        gateway,  # type: ignore[arg-type]
        editable_paths=("Assets/Scripts/Weapon.cs",),
    )

    result = adapter.invoke(
        "apply_code_patch",
        {"path": "Assets/Scripts/Weapon.cs", "expected": "10", "replacement": "12"},
    )

    assert result["status"] == "success"  # type: ignore[index]
    assert approved.read_text(encoding="utf-8") == "damage = 12;\n"
    assert other.read_text(encoding="utf-8") == "health = 20;\n"
    with pytest.raises(PolicyViolation, match="approved editable set"):
        adapter.invoke(
            "apply_code_patch",
            {"path": "Assets/Scripts/Enemy.cs", "expected": "20", "replacement": "1"},
        )


def test_unity_adapter_creates_only_new_approved_csharp_file(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "Assets" / "Scripts").mkdir(parents=True)
    gateway = FakeGateway(project, tmp_path / "run")
    gateway.run_directory.mkdir()
    adapter = UnityHarnessEngineAdapter(
        gateway,  # type: ignore[arg-type]
        editable_paths=("Assets/Scripts/NewFeature.cs",),
    )

    result = adapter.invoke(
        "create_script",
        {
            "path": "Assets/Scripts/NewFeature.cs",
            "content": "namespace Example { public sealed class NewFeature {} }",
        },
    )

    assert result["status"] == "success"  # type: ignore[index]
    assert (project / "Assets" / "Scripts" / "NewFeature.cs").is_file()
    with pytest.raises(PolicyViolation, match="refuses to overwrite"):
        adapter.invoke(
            "create_script",
            {"path": "Assets/Scripts/NewFeature.cs", "content": "replacement"},
        )


def test_unity_adapter_exposes_bounded_public_reads_and_change_review(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    source_dir = project / "Assets" / "Scripts"
    source_dir.mkdir(parents=True)
    approved = source_dir / "Weapon.cs"
    approved.write_text("damage = 10;\n", encoding="utf-8")
    public = source_dir / "Enemy.cs"
    public.write_text("health = 20;\n", encoding="utf-8")
    gateway = FakeGateway(project, tmp_path / "run")
    gateway.run_directory.mkdir()
    adapter = UnityHarnessEngineAdapter(
        gateway,  # type: ignore[arg-type]
        editable_paths=("Assets/Scripts/Weapon.cs",),
    )

    assert {
        "read_text_file",
        "read_text_files",
        "review_project_changes",
    }.issubset(adapter.available_tools())
    assert {".cs", ".prefab", ".unity", ".uxml"}.issubset(
        adapter.workspace_text_suffixes()
    )
    single = adapter.invoke("read_text_file", {"path": "Assets/Scripts/Enemy.cs"})
    assert single["content"] == "health = 20;\n"  # type: ignore[index]
    batch = adapter.invoke(
        "read_text_files",
        {"paths": ["Assets/Scripts/Weapon.cs", "Assets/Scripts/Enemy.cs"]},
    )
    assert len(batch["files"]) == 2  # type: ignore[arg-type]

    adapter.invoke(
        "apply_code_patch",
        {"path": "Assets/Scripts/Weapon.cs", "expected": "10", "replacement": "12"},
    )
    review = adapter.invoke("review_project_changes", {})
    assert review["changed_paths"] == ["Assets/Scripts/Weapon.cs"]  # type: ignore[index]
    assert "-damage = 10;" in review["files"][0]["diff"]  # type: ignore[index]
    assert "+damage = 12;" in review["files"][0]["diff"]  # type: ignore[index]

    with pytest.raises(PolicyViolation, match="cannot traverse"):
        adapter.invoke("read_text_file", {"path": "../secret.txt"})


def test_unity_native_evaluator_runs_independent_required_gates(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    gateway = FakeGateway(project, run_directory)
    task = ChangeRequestTaskSource("Verify the project.").load(project)
    run_spec = create_run_spec(
        task,
        ModelProfile(provider="mock", model="test"),
        engine=EngineName.UNITY,
        engine_version="test",
        profile=HarnessProfile.PROJECT,
        evaluation=EvaluationSpec(
            required_gates=(
                "specification",
                "compilation",
                "structure",
                "gameplay",
                "build",
                "smoke",
            )
        ),
    )

    outcomes = UnityNativeEvaluator(gateway).evaluate(run_spec, project)  # type: ignore[arg-type]

    assert [outcome.gate for outcome in outcomes] == list(run_spec.evaluation.required_gates)
    assert all(outcome.status is GateStatus.PASS for outcome in outcomes)
