from __future__ import annotations

from pathlib import Path

from gameforge.adapters.llm import ModelResponse, ModelUsage
from gameforge.harness.capabilities import CapabilityRegistry
from gameforge.harness.project_scope import ProjectAccessScope
from gameforge.harness.workspace_agent import (
    WorkspaceAgentExecutor,
    register_workspace_agent_capability,
)


class _EditingModel:
    def __init__(self, changes: dict[str, str | bytes]) -> None:
        self.changes = changes
        self.prompts: list[str] = []
        self.environment_overrides: list[dict[str, str]] = []

    def run_workspace_agent(
        self,
        *,
        project: Path,
        prompt: str,
        timeout_seconds: float,
        environment_overrides: dict[str, str] | None = None,
    ) -> ModelResponse:
        del timeout_seconds
        self.prompts.append(prompt)
        self.environment_overrides.append(dict(environment_overrides or {}))
        for relative, content in self.changes.items():
            target = project / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                target.write_bytes(content)
            else:
                target.write_text(content, encoding="utf-8")
        return ModelResponse(
            "implemented and verified",
            ModelUsage(
                input_tokens=120,
                output_tokens=20,
                cost_usd=0.01,
                cached_input_tokens=80,
                reasoning_output_tokens=7,
            ),
            provider="fake-workspace-agent",
            model="fake-model",
            tool_audit=(
                {
                    "kind": "file_change",
                    "changes": [{"path": "scene.tscn", "within_project": True}],
                },
            ),
        )


def _executor(
    tmp_path: Path,
    *,
    writable: tuple[str, ...],
    changes: dict[str, str | bytes],
) -> tuple[WorkspaceAgentExecutor, _EditingModel]:
    project = tmp_path / "project"
    project.mkdir()
    model = _EditingModel(changes)
    executor = WorkspaceAgentExecutor(
        project=project,
        scratch_root=tmp_path / "run" / "workspace-agent",
        project_scope=ProjectAccessScope(writable, allow_text_scope_expansion=True),
        model=model,  # type: ignore[arg-type]
        task_instruction="Update the public scene and verify it.",
        engine_environment={"engine_version": "test"},
        new_text_suffixes=(".gd", ".tscn"),
        host_managed_executables=("godot",),
    )
    return executor, model


def test_workspace_agent_commits_only_host_authorized_staged_diff(tmp_path: Path) -> None:
    executor, model = _executor(
        tmp_path,
        writable=("scene.tscn",),
        changes={"scene.tscn": "after\n"},
    )
    scene = executor.project / "scene.tscn"
    scene.write_text("before\n", encoding="utf-8")

    result = executor.invoke({"focus": "Use any useful local tools", "timeout_seconds": 30})

    assert result["status"] == "success"
    assert result["committed_paths"] == ["scene.tscn"]
    assert scene.read_text(encoding="utf-8") == "after\n"
    assert "there is no required workflow" in model.prompts[0]
    assert "do not launch them" in model.prompts[0]
    environment = model.environment_overrides[0]
    assert environment["GAMEFORGE_HOST_MANAGED_EXECUTABLES"] == "godot"
    assert Path(environment["GODOT"]).is_file()
    assert environment["PATH"].startswith(str(Path(environment["GODOT"]).parent))
    assert result["_harness_usage"]["input_tokens"] == 120
    assert result["agent_tool_audit"]["event_count"] == 1
    audit = executor.scratch_root / result["agent_tool_audit"]["artifact"]
    assert audit.is_file()
    assert "scene.tscn" in audit.read_text(encoding="utf-8")


def test_workspace_agent_rejects_complete_diff_after_out_of_scope_write(
    tmp_path: Path,
) -> None:
    executor, _model = _executor(
        tmp_path,
        writable=("scene.tscn",),
        changes={"scene.tscn": "after\n", "unrelated.gd": "bad\n"},
    )
    scene = executor.project / "scene.tscn"
    scene.write_text("before\n", encoding="utf-8")
    unrelated = executor.project / "unrelated.gd"
    unrelated.write_text("existing\n", encoding="utf-8")

    result = executor.invoke({})

    assert result["status"] == "rejected"
    assert result["undeclared_paths"] == ["unrelated.gd"]
    assert scene.read_text(encoding="utf-8") == "before\n"
    assert unrelated.read_text(encoding="utf-8") == "existing\n"


def test_workspace_agent_auto_declares_new_bounded_text_path(tmp_path: Path) -> None:
    executor, _model = _executor(
        tmp_path,
        writable=("scene.tscn",),
        changes={"scripts/new_feature.gd": "extends Node\n"},
    )
    (executor.project / "scene.tscn").write_text("before\n", encoding="utf-8")

    result = executor.invoke({})

    assert result["status"] == "success"
    assert result["committed_paths"] == ["scripts/new_feature.gd"]
    assert result["scope_declaration"]["added_paths"] == ["scripts/new_feature.gd"]
    assert executor.project_scope.is_writable("scripts/new_feature.gd")
    assert (executor.project / "scripts/new_feature.gd").read_text(encoding="utf-8") == (
        "extends Node\n"
    )


def test_workspace_agent_does_not_auto_declare_new_binary_path(tmp_path: Path) -> None:
    executor, _model = _executor(
        tmp_path,
        writable=("scene.tscn",),
        changes={"assets/new_texture.png": "not-a-real-image"},
    )
    (executor.project / "scene.tscn").write_text("before\n", encoding="utf-8")

    result = executor.invoke({})

    assert result["status"] == "rejected"
    assert result["undeclared_paths"] == ["assets/new_texture.png"]
    assert not (executor.project / "assets/new_texture.png").exists()


def test_workspace_agent_commits_explicit_binary_asset_with_lineage(tmp_path: Path) -> None:
    executor, model = _executor(
        tmp_path,
        writable=("assets/item_icon.png",),
        changes={"assets/item_icon.png": b"\x89PNG\r\n\x1a\nrepaired"},
    )
    texture = executor.project / "assets" / "item_icon.png"
    texture.parent.mkdir(parents=True)
    texture.write_bytes(b"broken-png")

    result = executor.invoke({})

    assert result["status"] == "success"
    assert result["committed_paths"] == ["assets/item_icon.png"]
    assert texture.read_bytes().startswith(b"\x89PNG")
    assert result["binary_change_lineage"][0]["provenance"] == (
        "task-authorized-public-asset-transformation"
    )
    lineage = executor.scratch_root / result["binary_change_lineage_artifact"]
    assert lineage.is_file()
    assert "assets/item_icon.png" in lineage.read_text(encoding="utf-8")
    assert "assets/item_icon.png" in model.prompts[0]


def test_workspace_agent_capability_is_optional_and_discloses_read_boundary(
    tmp_path: Path,
) -> None:
    executor, _model = _executor(tmp_path, writable=(), changes={})
    registry = CapabilityRegistry()

    register_workspace_agent_capability(registry, executor)

    descriptor = registry.get("run_workspace_agent").descriptor
    assert descriptor.phase.value == "author"
    assert descriptor.effects[1].value == "project_write"
    assert "not host-read confidentiality" in descriptor.purpose
