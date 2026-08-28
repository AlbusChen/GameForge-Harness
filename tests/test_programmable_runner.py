from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from gameforge.adapters.llm import ModelResponse, ModelUsage
from gameforge.harness.contracts import (
    EngineName,
    EvaluationSpec,
    GateOutcome,
    GateStatus,
    HarnessProfile,
    RunStatus,
)
from gameforge.harness.game_tasks import (
    AcceptanceDimension,
    AcceptanceRequirement,
    EngineEnvironment,
    GameTaskSpec,
    PluginPin,
    RequirementEnforcement,
)
from gameforge.harness.model_profiles import ModelProfile
from gameforge.harness.programmable_runner import (
    _capture_project_write_transaction,
    _discard_project_write_transaction,
    _restore_project_write_transaction,
)
from gameforge.harness.run_factory import create_run_spec_v2
from gameforge.harness.runner import HarnessRunner
from gameforge.harness.task_sources import ChangeRequestTaskSource


def test_project_write_transaction_restores_modified_and_created_files(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    scene = project / "scene.tscn"
    created = project / "new.gd"
    scene.write_text("before\n", encoding="utf-8")
    transaction = _capture_project_write_transaction(
        project,
        ("scene.tscn", "new.gd"),
    )
    scene.write_text("broken\n", encoding="utf-8")
    created.write_text("created\n", encoding="utf-8")

    _restore_project_write_transaction(project, transaction)

    assert scene.read_text(encoding="utf-8") == "before\n"
    assert not created.exists()


def test_project_write_transaction_removes_newly_declared_file_on_rollback(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    scene = project / "scene.tscn"
    scene.write_text("before\n", encoding="utf-8")
    transaction = _capture_project_write_transaction(project, ("scene.tscn",))
    generated = project / "scripts" / "new_feature.gd"
    generated.parent.mkdir()
    generated.write_text("extends Node\n", encoding="utf-8")

    _restore_project_write_transaction(
        project,
        transaction,
        additional_created_paths=("scripts/new_feature.gd",),
    )

    assert scene.read_text(encoding="utf-8") == "before\n"
    assert not generated.exists()


def test_project_write_transaction_streams_large_file_and_discards_on_commit(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    asset = project / "large.bmp"
    payload = b"large-asset-block" * (3 * 1024 * 1024)
    asset.write_bytes(payload)
    transaction = _capture_project_write_transaction(
        project,
        ("large.bmp",),
        backup_root=tmp_path / "transactions",
    )

    backup = transaction.files["large.bmp"]
    assert transaction.backup_directory.is_dir()
    assert backup is not None
    assert backup.stat().st_size == len(payload)

    _discard_project_write_transaction(transaction)

    assert not transaction.backup_directory.exists()


def test_project_write_transaction_restores_large_file_from_disk(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    asset = project / "large.bmp"
    payload = b"large-asset-block" * (3 * 1024 * 1024)
    asset.write_bytes(payload)
    transaction = _capture_project_write_transaction(
        project,
        ("large.bmp",),
        backup_root=tmp_path / "transactions",
    )
    asset.write_bytes(b"broken")

    _restore_project_write_transaction(project, transaction)

    assert asset.read_bytes() == payload
    assert not transaction.backup_directory.exists()


class ProgrammableModel:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *, system: str, prompt: str) -> ModelResponse:
        assert "hidden-native" not in prompt
        assert "programmable" in system
        assert "run_workspace_program" in system
        assert "await forge.call" in system
        assert "control_api" in prompt
        assert "Capabilities are not global functions." in prompt
        self.calls += 1
        decision = (
            {
                "kind": "cell",
                "code": "project = await forge.call('inspect_project')",
                "rationale": "Inspect bounded project state.",
            }
            if self.calls == 1
            else {
                "kind": "finish",
                "outcome": "success",
                "summary": "The project was inspected.",
                "evidence": [],
                "rationale": "Independent evaluation can now run.",
            }
        )
        return ModelResponse(
            text=json.dumps(decision),
            usage=ModelUsage(10, 5, 0.001),
            provider="scripted",
            model="test",
        )


class RecoveringDecisionModel:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *, system: str, prompt: str) -> ModelResponse:
        del system, prompt
        self.calls += 1
        text = (
            "not a decision"
            if self.calls == 1
            else json.dumps(
                {
                    "kind": "finish",
                    "outcome": "success",
                    "summary": "Recovered with a valid decision.",
                    "evidence": [],
                    "rationale": "The prior schema error was corrected.",
                }
            )
        )
        return ModelResponse(
            text=text,
            usage=ModelUsage(10, 5, 0.001),
            provider="scripted",
            model="recovering-test",
        )


class ProgrammableEngine:
    name = "godot"
    version = "test-v2"

    def available_tools(self) -> tuple[str, ...]:
        return ("inspect_project",)

    def automatic_validation_tool(self) -> str | None:
        return None

    def completion_feedback_tools(self) -> tuple[str, ...]:
        return ()

    def invoke(self, tool_name: str, arguments: object) -> object:
        assert tool_name == "inspect_project"
        assert arguments == {}
        return {"project": "fixture", "status": "success"}


class NativeEvaluator:
    name = "native"
    version = "2"

    def evaluate(self, run_spec: object, workspace: Path) -> tuple[GateOutcome, ...]:
        assert workspace.is_dir()
        return tuple(
            GateOutcome(gate=gate, status=GateStatus.PASS)
            for gate in run_spec.evaluation.required_gates  # type: ignore[attr-defined]
        )


class FailingNativeEvaluator(NativeEvaluator):
    def evaluate(self, run_spec: object, workspace: Path) -> tuple[GateOutcome, ...]:
        assert workspace.is_dir()
        return tuple(
            GateOutcome(gate=gate, status=GateStatus.FAIL, detail="fixture failure")
            for gate in run_spec.evaluation.required_gates  # type: ignore[attr-defined]
        )


class BehaviorEvaluator:
    name = "official-fixture"
    version = "1"

    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path

    def evaluate(self, run_spec: object, workspace: Path) -> tuple[GateOutcome, ...]:
        del run_spec
        assert workspace.is_dir()
        self.log_path.write_text("fixture behavior passed\n", encoding="utf-8")
        return (
            GateOutcome(
                gate="official",
                status=GateStatus.PASS,
                artifacts=(str(self.log_path),),
            ),
        )


class MutatingProgrammableModel:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *, system: str, prompt: str) -> ModelResponse:
        del system
        payload = json.loads(prompt)
        if self.calls:
            assert payload["public_requirement_evidence"][0]["status"] == "PASS"
            automatic = next(
                item
                for item in reversed(payload["observations"])
                if item.get("capability") == "automatic_validation"
            )
            assert automatic["value"]["output"]["diagnostics"] == ["fixture diagnostic"]
        self.calls += 1
        decision = (
            {
                "kind": "cell",
                "code": (
                    "change = await forge.call("
                    "'write_text_file', path='scene.tscn', "
                    "content='[gd_scene]\\n')"
                ),
                "rationale": "Apply the approved project mutation.",
            }
            if self.calls == 1
            else {
                "kind": "finish",
                "outcome": "success",
                "summary": "The imported scene is ready.",
                "evidence": [],
                "rationale": "Automatic import evidence is fresh.",
            }
        )
        return ModelResponse(
            text=json.dumps(decision),
            usage=ModelUsage(10, 5, 0.001),
            provider="scripted",
            model="test",
        )


class WorkspaceProgramModel:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *, system: str, prompt: str) -> ModelResponse:
        assert "run_workspace_program" in system
        payload = json.loads(prompt)
        capability_names = {item["name"] for item in payload["capabilities"]}
        assert "run_workspace_program" in capability_names
        if self.calls:
            assert payload["public_requirement_evidence"][0]["status"] == "PASS"
        self.calls += 1
        program = (
            "from pathlib import Path\n"
            "Path('scene.tscn').write_text('[gd_scene]\\n', encoding='utf-8')\n"
        )
        decision = (
            {
                "kind": "workspace",
                "source": program,
                "paths": ["scene.tscn"],
                "timeout_seconds": 30,
                "rationale": "Use the general workspace environment to author the scene.",
            }
            if self.calls == 1
            else {
                "kind": "finish",
                "outcome": "success",
                "summary": "The scene was authored and imported.",
                "evidence": [],
                "rationale": "Current-revision public evidence passes.",
            }
        )
        return ModelResponse(
            text=json.dumps(decision),
            usage=ModelUsage(10, 5, 0.001),
            provider="scripted",
            model="workspace-program-test",
        )


class ExplicitSyncModel:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *, system: str, prompt: str) -> ModelResponse:
        del system, prompt
        self.calls += 1
        decision = (
            {
                "kind": "cell",
                "code": (
                    "change = await forge.call('write_text_file', path='scene.tscn', "
                    "content='[gd_scene]\\n')\n"
                    "sync = await forge.engine.sync('import_project')"
                ),
                "rationale": "Write and explicitly validate the current revision.",
            }
            if self.calls == 1
            else {
                "kind": "finish",
                "outcome": "success",
                "summary": "The explicitly validated scene is ready.",
                "evidence": [],
                "rationale": "Fresh public evidence is available.",
            }
        )
        return ModelResponse(
            text=json.dumps(decision),
            usage=ModelUsage(10, 5, 0.001),
            provider="scripted",
            model="explicit-sync-test",
        )


class RecoveringExplicitSyncModel:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *, system: str, prompt: str) -> ModelResponse:
        del system
        payload = json.loads(prompt)
        self.calls += 1
        if self.calls == 1:
            decision = {
                "kind": "cell",
                "code": (
                    "change = await forge.call('write_text_file', path='scene.tscn', "
                    "content='[gd_scene]\\n')"
                ),
                "rationale": "Write the current revision and let automatic validation run.",
            }
        elif self.calls == 2:
            assert payload["public_requirement_evidence"][0]["status"] == "FAIL"
            decision = {
                "kind": "cell",
                "code": "sync = await forge.engine.sync('import_project')",
                "rationale": "Retry the transiently failed validator on the same revision.",
            }
        else:
            assert payload["public_requirement_evidence"][0]["status"] == "PASS"
            decision = {
                "kind": "finish",
                "outcome": "success",
                "summary": "The retried validation now passes.",
                "evidence": [],
                "rationale": "Fresh passing evidence supersedes the transient failure.",
            }
        return ModelResponse(
            text=json.dumps(decision),
            usage=ModelUsage(10, 5, 0.001),
            provider="scripted",
            model="recovering-explicit-sync-test",
        )


class MutatingEngine(ProgrammableEngine):
    def __init__(self, project: Path) -> None:
        self.project = project
        self.import_calls = 0

    def available_tools(self) -> tuple[str, ...]:
        return ("write_text_file", "import_project")

    def automatic_validation_tool(self) -> str | None:
        return "import_project"

    def invoke(self, tool_name: str, arguments: object) -> object:
        if tool_name == "write_text_file":
            assert isinstance(arguments, dict)
            target = self.project / str(arguments["path"])
            target.write_text(str(arguments["content"]), encoding="utf-8")
            return {"status": "success", "path": str(arguments["path"])}
        if tool_name == "import_project":
            self.import_calls += 1
            return {
                "status": "success",
                "return_code": 0,
                "log_path": "import.log",
                "diagnostics": ["fixture diagnostic"],
            }
        raise AssertionError(tool_name)


class TransientValidationEngine(MutatingEngine):
    def invoke(self, tool_name: str, arguments: object) -> object:
        if tool_name != "import_project":
            return super().invoke(tool_name, arguments)
        self.import_calls += 1
        if self.import_calls == 1:
            return {
                "status": "failed",
                "return_code": 124,
                "log_path": "import.log",
                "diagnostics": ["transient timeout"],
            }
        return {
            "status": "success",
            "return_code": 0,
            "log_path": "import.log",
            "diagnostics": [],
        }


class ReconcilingMutatingEngine(MutatingEngine):
    def __init__(self, project: Path) -> None:
        super().__init__(project)
        self.reconcile_calls: list[tuple[str, ...]] = []

    def reconcile_project_changes(
        self,
        changed_paths: tuple[str, ...],
    ) -> dict[str, object]:
        self.reconcile_calls.append(changed_paths)
        scene = self.project / "scene.tscn"
        scene.write_text(
            scene.read_text(encoding="utf-8") + "# host-reconciled\n",
            encoding="utf-8",
        )
        return {
            "schema_version": 1,
            "status": "success",
            "changed_resource_count": 1,
        }


def _game_task() -> GameTaskSpec:
    return GameTaskSpec(
        engine_profile="godot-test",
        entry_points=("Main",),
        target_platforms=("test",),
        requirements=(
            AcceptanceRequirement(
                id="hidden-native",
                dimension=AcceptanceDimension.STRUCTURE,
                enforcement=RequirementEnforcement.REQUIRED,
                evaluator_ref="specification",
                model_visible=False,
            ),
        ),
    )


def _spec(project: Path, *, plugins: tuple[PluginPin, ...] = ()):
    task = ChangeRequestTaskSource("Inspect the game project.").load(project)
    return create_run_spec_v2(
        task,
        ModelProfile(provider="mock", model="test"),
        engine=EngineName.GODOT,
        engine_version="4.4",
        profile=HarnessProfile.PROJECT,
        game_task=_game_task(),
        engine_environment=EngineEnvironment(engine_version="4.4", target_platform="test"),
        plugins=plugins,
        now=datetime(2026, 8, 9, tzinfo=UTC),
    )


def test_harness_runner_dispatches_v2_and_persists_typed_evidence(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    model = ProgrammableModel()

    run = HarnessRunner(model, ProgrammableEngine(), NativeEvaluator()).execute(
        _spec(project), tmp_path / "runs"
    )

    assert run.result.version == 2
    assert run.result.status is RunStatus.PASS
    assert run.result.tool_calls == 1
    assert run.result.program_cells == 1  # type: ignore[attr-defined]
    assert run.result.required_evidence_ids  # type: ignore[attr-defined]
    assert (run.directory / "events.jsonl").is_file()
    assert (run.directory / "evidence.json").is_file()
    assert (run.directory / "control-snapshot.json").is_file()
    assert (run.directory / "resource-safety.json").is_file()
    assert (run.directory / "metrics.json").is_file()
    assert run.result.model_turns == 2  # type: ignore[attr-defined]
    assert run.result.prompt_bytes > 0  # type: ignore[attr-defined]
    assert run.result.required_evidence_coverage == 1  # type: ignore[attr-defined]
    assert run.result.lineage_complete  # type: ignore[attr-defined]
    assert model.calls == 2


def test_failed_required_gate_still_has_complete_evidence_lineage(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    run = HarnessRunner(
        ProgrammableModel(), ProgrammableEngine(), FailingNativeEvaluator()
    ).execute(_spec(project), tmp_path / "runs")

    assert run.result.status is RunStatus.FAIL
    assert run.result.required_evidence_coverage == 1  # type: ignore[attr-defined]
    assert run.result.lineage_complete  # type: ignore[attr-defined]
    assert len(run.result.required_evidence_ids) == 1  # type: ignore[attr-defined]


def test_v2_runtime_recovers_from_invalid_model_decision_and_keeps_usage(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    model = RecoveringDecisionModel()

    run = HarnessRunner(model, ProgrammableEngine(), NativeEvaluator()).execute(
        _spec(project), tmp_path / "runs"
    )

    agent_result = json.loads((run.directory / "agent-result.json").read_text(encoding="utf-8"))
    assert run.result.status is RunStatus.PASS
    assert run.result.model_turns == 2  # type: ignore[attr-defined]
    assert run.result.input_tokens == 20
    assert run.result.output_tokens == 10
    assert agent_result["trace"][0]["event"] == "model_program_decision_error"
    assert model.calls == 2


def test_v2_runner_blocks_before_model_when_plugin_pin_mismatches(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    model = ProgrammableModel()
    pin = PluginPin(name="godot", version="test-v2", digest="f" * 64)

    run = HarnessRunner(model, ProgrammableEngine(), NativeEvaluator()).execute(
        _spec(project, plugins=(pin,)), tmp_path / "runs"
    )

    assert run.result.status is RunStatus.BLOCKED
    assert run.result.failure_category == "ValueError"
    assert "plugin pin" in str(run.result.failure_detail)
    assert model.calls == 0


def test_v2_mutation_automatically_syncs_engine_and_records_fresh_gate(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task = ChangeRequestTaskSource(
        "Create the approved scene.", editable_paths=("scene.tscn",)
    ).load(project)
    game_task = GameTaskSpec(
        engine_profile="godot-test",
        entry_points=("scene.tscn",),
        target_platforms=("test",),
        requirements=(
            AcceptanceRequirement(
                id="import",
                dimension=AcceptanceDimension.STRUCTURE,
                enforcement=RequirementEnforcement.REQUIRED,
                evaluator_ref="import_project",
            ),
        ),
    )
    run_spec = create_run_spec_v2(
        task,
        ModelProfile(provider="mock", model="test"),
        engine=EngineName.GODOT,
        engine_version="4.4",
        profile=HarnessProfile.PROJECT,
        game_task=game_task,
        engine_environment=EngineEnvironment(engine_version="4.4", target_platform="test"),
        now=datetime(2026, 8, 9, tzinfo=UTC),
    )

    run = HarnessRunner(
        MutatingProgrammableModel(), MutatingEngine(project), NativeEvaluator()
    ).execute(run_spec, tmp_path / "runs")

    evidence = json.loads((run.directory / "evidence.json").read_text(encoding="utf-8"))
    assert run.result.status is RunStatus.PASS
    assert run.result.tool_calls == 2
    assert len(evidence["project_revisions"]) == 2
    assert len(evidence["imported_revisions"]) == 1
    assert evidence["gates"][0]["requirement_id"] == "import"


def test_v2_general_workspace_program_commits_and_enters_validation_lifecycle(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task = ChangeRequestTaskSource(
        "Create the approved scene.", editable_paths=("scene.tscn",)
    ).load(project)
    game_task = GameTaskSpec(
        engine_profile="godot-test",
        entry_points=("scene.tscn",),
        target_platforms=("test",),
        requirements=(
            AcceptanceRequirement(
                id="import",
                dimension=AcceptanceDimension.STRUCTURE,
                enforcement=RequirementEnforcement.REQUIRED,
                evaluator_ref="import_project",
            ),
        ),
    )
    run_spec = create_run_spec_v2(
        task,
        ModelProfile(provider="mock", model="test"),
        engine=EngineName.GODOT,
        engine_version="4.4",
        profile=HarnessProfile.PROJECT,
        game_task=game_task,
        engine_environment=EngineEnvironment(engine_version="4.4", target_platform="test"),
        now=datetime(2026, 8, 9, tzinfo=UTC),
    )
    engine = MutatingEngine(project)

    run = HarnessRunner(WorkspaceProgramModel(), engine, NativeEvaluator()).execute(
        run_spec, tmp_path / "runs"
    )

    assert run.result.status is RunStatus.PASS
    assert run.result.tool_calls == 2
    assert engine.import_calls == 1
    assert (project / "scene.tscn").read_text(encoding="utf-8") == "[gd_scene]\n"


def test_host_reconciliation_covers_general_workspace_writes_before_validation(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task = ChangeRequestTaskSource(
        "Create the approved scene.", editable_paths=("scene.tscn",)
    ).load(project)
    game_task = GameTaskSpec(
        engine_profile="godot-test",
        entry_points=("scene.tscn",),
        target_platforms=("test",),
        requirements=(
            AcceptanceRequirement(
                id="import",
                dimension=AcceptanceDimension.STRUCTURE,
                enforcement=RequirementEnforcement.REQUIRED,
                evaluator_ref="import_project",
            ),
        ),
    )
    run_spec = create_run_spec_v2(
        task,
        ModelProfile(provider="mock", model="test"),
        engine=EngineName.GODOT,
        engine_version="4.4",
        profile=HarnessProfile.PROJECT,
        game_task=game_task,
        engine_environment=EngineEnvironment(engine_version="4.4", target_platform="test"),
        now=datetime(2026, 8, 9, tzinfo=UTC),
    )
    engine = ReconcilingMutatingEngine(project)

    run = HarnessRunner(WorkspaceProgramModel(), engine, NativeEvaluator()).execute(
        run_spec, tmp_path / "runs"
    )

    assert run.result.status is RunStatus.PASS
    assert engine.reconcile_calls == [("scene.tscn",)]
    assert engine.import_calls == 1
    assert (project / "scene.tscn").read_text(encoding="utf-8").endswith("# host-reconciled\n")
    audit = json.loads((run.directory / "host-reconciliation.json").read_text(encoding="utf-8"))
    assert audit["invocations"][0]["capability"] == "run_workspace_program"


def test_explicit_sync_satisfies_pending_automatic_validation_without_duplicate(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task = ChangeRequestTaskSource(
        "Create the approved scene.", editable_paths=("scene.tscn",)
    ).load(project)
    game_task = GameTaskSpec(
        engine_profile="godot-test",
        entry_points=("scene.tscn",),
        target_platforms=("test",),
        requirements=(
            AcceptanceRequirement(
                id="import",
                dimension=AcceptanceDimension.STRUCTURE,
                enforcement=RequirementEnforcement.REQUIRED,
                evaluator_ref="import_project",
            ),
        ),
    )
    run_spec = create_run_spec_v2(
        task,
        ModelProfile(provider="mock", model="test"),
        engine=EngineName.GODOT,
        engine_version="4.4",
        profile=HarnessProfile.PROJECT,
        game_task=game_task,
        engine_environment=EngineEnvironment(engine_version="4.4", target_platform="test"),
        now=datetime(2026, 8, 9, tzinfo=UTC),
    )
    engine = MutatingEngine(project)

    run = HarnessRunner(ExplicitSyncModel(), engine, NativeEvaluator()).execute(
        run_spec, tmp_path / "runs"
    )

    assert run.result.status is RunStatus.PASS
    assert run.result.tool_calls == 2
    assert engine.import_calls == 1
    assert run.result.lineage_complete  # type: ignore[attr-defined]


def test_explicit_sync_supersedes_failed_validation_on_the_same_revision(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task = ChangeRequestTaskSource(
        "Create the approved scene.", editable_paths=("scene.tscn",)
    ).load(project)
    game_task = GameTaskSpec(
        engine_profile="godot-test",
        entry_points=("scene.tscn",),
        target_platforms=("test",),
        requirements=(
            AcceptanceRequirement(
                id="import",
                dimension=AcceptanceDimension.STRUCTURE,
                enforcement=RequirementEnforcement.REQUIRED,
                evaluator_ref="import_project",
            ),
        ),
    )
    run_spec = create_run_spec_v2(
        task,
        ModelProfile(provider="mock", model="test"),
        engine=EngineName.GODOT,
        engine_version="4.4",
        profile=HarnessProfile.PROJECT,
        game_task=game_task,
        engine_environment=EngineEnvironment(engine_version="4.4", target_platform="test"),
        now=datetime(2026, 8, 9, tzinfo=UTC),
    )
    engine = TransientValidationEngine(project)

    run = HarnessRunner(RecoveringExplicitSyncModel(), engine, NativeEvaluator()).execute(
        run_spec, tmp_path / "runs"
    )

    evidence = json.loads((run.directory / "evidence.json").read_text(encoding="utf-8"))
    assert run.result.status is RunStatus.PASS
    assert engine.import_calls == 2
    assert [gate["status"] for gate in evidence["gates"]] == ["FAIL", "PASS"]


def test_v2_host_behavior_evaluator_records_session_log_and_lineage(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    task = ChangeRequestTaskSource(
        "Create the approved scene.", editable_paths=("scene.tscn",)
    ).load(project)
    game_task = GameTaskSpec(
        engine_profile="godot-test",
        entry_points=("scene.tscn",),
        target_platforms=("test",),
        requirements=(
            AcceptanceRequirement(
                id="import",
                dimension=AcceptanceDimension.STRUCTURE,
                enforcement=RequirementEnforcement.REQUIRED,
                evaluator_ref="import_project",
            ),
            AcceptanceRequirement(
                id="official-behavior",
                dimension=AcceptanceDimension.BEHAVIOR,
                enforcement=RequirementEnforcement.REQUIRED,
                evaluator_ref="official",
                model_visible=False,
            ),
        ),
    )
    run_spec = create_run_spec_v2(
        task,
        ModelProfile(provider="mock", model="test"),
        engine=EngineName.GODOT,
        engine_version="4.4",
        profile=HarnessProfile.PROJECT,
        game_task=game_task,
        engine_environment=EngineEnvironment(engine_version="4.4", target_platform="test"),
        evaluation=EvaluationSpec(required_gates=("official", "preservation")),
        now=datetime(2026, 8, 9, tzinfo=UTC),
    )

    run = HarnessRunner(
        MutatingProgrammableModel(),
        MutatingEngine(project),
        BehaviorEvaluator(tmp_path / "official.log"),
    ).execute(run_spec, tmp_path / "runs")

    evidence = json.loads((run.directory / "evidence.json").read_text(encoding="utf-8"))
    assert run.result.status is RunStatus.PASS
    assert run.result.lineage_complete  # type: ignore[attr-defined]
    assert run.result.dimension_status["behavior"] == "PASS"  # type: ignore[attr-defined]
    assert evidence["sessions"][0]["state"] == "stopped"
    assert evidence["observations"][0]["kind"] == "log"
    assert evidence["observations"][0]["input_trace_hash"]
