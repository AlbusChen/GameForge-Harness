from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gameforge.adapters.llm import CostLedger, ModelResponse, ModelUsage
from gameforge.harness.capabilities import (
    CapabilityBroker,
    CapabilityDescriptor,
    CapabilityEffect,
    CapabilityPhase,
    CapabilityRegistry,
    ConcurrencyMode,
    Idempotency,
)
from gameforge.harness.contracts import EngineName, HarnessProfile, RunBudgets
from gameforge.harness.control_language import (
    ControlLimits,
    ControlProgramError,
    SafeInterpreter,
    host_function,
    namespace,
)
from gameforge.harness.control_session import RestrictedControlSession
from gameforge.harness.engine_lifecycle import EngineLifecycleCoordinator
from gameforge.harness.errors import InfrastructureFailure
from gameforge.harness.events import InMemoryEventStore
from gameforge.harness.evidence import (
    EvidenceGraph,
    EvidenceSubjectKind,
    GateEvidenceStatus,
    empty_digest,
)
from gameforge.harness.game_tasks import (
    AcceptanceDimension,
    AcceptanceRequirement,
    EngineEnvironment,
    GameTaskSpec,
    RequirementEnforcement,
)
from gameforge.harness.gate_planner import (
    GateDescriptor,
    GateExecutionResult,
    GateLevel,
    GatePlanner,
    GateRegistry,
)
from gameforge.harness.model_profiles import ModelProfile
from gameforge.harness.program_runtime import (
    ProgramDecision,
    ProgrammableAgentRuntime,
    _account_auxiliary_usage,
    _bounded_cell_result,
    _bounded_program_state,
)
from gameforge.harness.programmable_runner import _control_limits_for_run
from gameforge.harness.run_factory import create_run_spec_v2
from gameforge.harness.task_sources import ChangeRequestTaskSource


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _read_descriptor() -> CapabilityDescriptor:
    return CapabilityDescriptor(
        name="read",
        version="1",
        purpose="bounded read",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        effects=(CapabilityEffect.PROJECT_READ,),
        phase=CapabilityPhase.OBSERVE,
        idempotency=Idempotency.PURE,
        concurrency=ConcurrencyMode.PARALLEL_READ,
    )


def _session() -> tuple[RestrictedControlSession, EvidenceGraph, str]:
    registry = CapabilityRegistry()
    registry.register(_read_descriptor(), lambda arguments: {"path": arguments["path"], "n": 2})
    registry.register(
        CapabilityDescriptor(
            name="sync",
            version="1",
            purpose="engine sync",
            input_schema={"type": "object", "additionalProperties": False},
            output_schema={"type": "object"},
            effects=(CapabilityEffect.ENGINE_STATE,),
            phase=CapabilityPhase.ENGINE_SYNC,
            concurrency=ConcurrencyMode.EXCLUSIVE_ENGINE,
        ),
        lambda arguments: {
            "status": "failed",
            "diagnostics": ["Scene instance is missing"],
        },
    )
    registry.register(
        CapabilityDescriptor(
            name="capture",
            version="1",
            purpose="bounded advisory capture",
            input_schema={"type": "object", "additionalProperties": False},
            output_schema={"type": "object"},
            effects=(CapabilityEffect.ARTIFACT_WRITE,),
            phase=CapabilityPhase.CAPTURE,
            concurrency=ConcurrencyMode.EXCLUSIVE_ENGINE,
        ),
        lambda arguments: {"status": "success", "verdict": "uncertain"},
    )
    events = InMemoryEventStore("run")
    evidence = EvidenceGraph(events=events)
    digest = empty_digest()
    revision = evidence.commit_project_revision(
        source_tree_hash=digest,
        diff_hash=digest,
        asset_graph_digest=digest,
    )
    broker = CapabilityBroker(run_id="run", registry=registry, events=events)
    task = GameTaskSpec(
        engine_profile="test",
        entry_points=("Main",),
        target_platforms=("test",),
        requirements=(
            AcceptanceRequirement(
                id="structure",
                dimension=AcceptanceDimension.STRUCTURE,
                enforcement=RequirementEnforcement.REQUIRED,
                evaluator_ref="structure",
            ),
        ),
    )
    gates = GateRegistry()
    gates.register(
        GateDescriptor(
            name="structure",
            version="1",
            level=GateLevel.SOURCE,
            subject_kinds=(EvidenceSubjectKind.PROJECT_REVISION,),
            evaluator_digest=_digest("gate"),
        ),
        lambda requirement, kind, subject: GateExecutionResult(status=GateEvidenceStatus.PASS),
    )
    planner = GatePlanner(
        game_task=task,
        environment_digest=digest,
        evidence=evidence,
        events=events,
        registry=gates,
    )
    return (
        RestrictedControlSession(
            broker=broker,
            gates=planner,
            events=events,
            limits=ControlLimits(maximum_loop_iterations=20),
        ),
        evidence,
        revision.id,
    )


def test_restricted_control_session_persists_functions_state_and_runs_public_gate() -> None:
    session, _, revision_id = _session()
    first = session.execute_cell(
        """
def total(values):
    result = 0
    for value in values:
        result += value
    return result

reads = await forge.parallel([
    {"name": "read", "arguments": {"path": "one"}},
    {"name": "read", "arguments": {"path": "two"}},
])
score = total([item["n"] for item in reads])
await forge.state.set("score", score)
"""
    )
    second = session.execute_cell(
        f"""
score = await forge.state.get("score", 0) + 1
gate = await forge.gates.run(
    "structure",
    subject_kind="project_revision",
    subject_id="{revision_id}",
)
proposal = await forge.finish.propose(summary="ready", evidence=[gate["id"]])
"""
    )

    assert first.execution.variables["score"] == 4
    assert second.execution.variables["score"] == 5
    assert session.state.functions["total"].startswith("def total")
    assert second.finish_proposal is not None
    assert second.finish_proposal.public_decision.allowed


def test_restricted_control_accepts_immutable_json_literal_aliases() -> None:
    session, _, _ = _session()

    result = session.execute_cell("payload = {'enabled': true, 'disabled': false, 'missing': null}")

    assert result.execution.variables["payload"] == {
        "enabled": True,
        "disabled": False,
        "missing": None,
    }
    with pytest.raises(ControlProgramError, match="assignment identifier"):
        session.execute_cell("false = 1")


def test_control_program_cannot_catch_or_repair_infrastructure_failure() -> None:
    def fail() -> None:
        raise InfrastructureFailure("engine crashed")

    interpreter = SafeInterpreter(
        namespaces={
            "host": namespace(
                "host",
                {"fail": host_function("host.fail", fail)},
            )
        }
    )

    with pytest.raises(InfrastructureFailure, match="engine crashed"):
        interpreter.execute(
            "try:\n    await host.fail()\nexcept Exception:\n    recovered = True\n"
        )

    assert "recovered" not in interpreter.variables


@pytest.mark.parametrize(
    "source, message",
    [
        ("import os", "statement is not allowed"),
        ("value = open('secret')", "unknown identifier"),
        ("value = forge.__dict__", "private"),
        ("value = [x for x in range(100)]", "range exceeds"),
    ],
)
def test_restricted_control_rejects_unsafe_or_unbounded_code(source: str, message: str) -> None:
    session, _, _ = _session()
    with pytest.raises(ControlProgramError, match=message):
        session.execute_cell(source)


def test_failed_cell_rolls_back_program_variables_but_keeps_audited_calls() -> None:
    session, _, _ = _session()
    session.execute_cell("value = 1")
    with pytest.raises(ControlProgramError):
        session.execute_cell("seen = await forge.call('read', path='one')\nvalue = open('no')")

    assert session.state.variables == {"value": 1}
    assert session.broker.call_count == 1
    assert session.last_cell_observations[0]["capability"] == "read"


def test_cell_completion_hook_runs_once_after_a_successful_composite_cell() -> None:
    session, _, _ = _session()
    completions = 0

    def complete() -> tuple[dict[str, object], ...]:
        nonlocal completions
        completions += 1
        return ({"capability": "automatic_validation", "value": {"status": "pass"}},)

    session.cell_completion_hook = complete
    result = session.execute_cell(
        "one = await forge.call('read', path='one')\ntwo = await forge.call('read', path='two')"
    )

    assert completions == 1
    assert len(result.observations) == 3
    assert result.observations[-1]["capability"] == "automatic_validation"


def test_direct_capability_decision_uses_the_same_cell_lifecycle() -> None:
    session, _, _ = _session()
    completions = 0

    def complete() -> tuple[dict[str, object], ...]:
        nonlocal completions
        completions += 1
        return ({"capability": "automatic_validation", "value": {"status": "pass"}},)

    session.cell_completion_hook = complete
    result = session.execute_direct_capability("read", {"path": "scene.tscn"})

    assert result.cell_number == 1
    assert result.execution.steps == 0
    assert result.observations[0]["capability"] == "read"
    assert result.observations[-1]["capability"] == "automatic_validation"
    assert completions == 1


def test_first_class_capability_dispatches_capture_without_control_language() -> None:
    session, _, _ = _session()

    result = session.execute_registered_capability("capture", {})

    assert result.cell_number == 1
    assert result.observations[0]["capability"] == "forge.capture.run:capture"
    assert result.observations[0]["value"] == {
        "status": "success",
        "verdict": "uncertain",
    }


def test_engine_sync_result_is_persisted_as_a_cell_observation() -> None:
    session, evidence, _ = _session()
    digest = empty_digest()
    session.lifecycle = EngineLifecycleCoordinator(
        broker=session.broker,
        evidence=evidence,
        events=session.events,
        engine_version="4.7",
        plugin_digest=digest,
        import_settings_digest=digest,
        environment_digest=digest,
    )

    result = session.execute_cell("sync_result = await forge.engine.sync('sync')")

    observation = result.observations[0]
    assert observation["capability"] == "forge.engine.sync:sync"
    assert observation["value"]["output"]["diagnostics"] == ["Scene instance is missing"]


def test_capture_capability_has_an_explicit_advisory_namespace() -> None:
    session, _, _ = _session()

    result = session.execute_cell("visual = await forge.capture.run('capture')")

    assert result.execution.variables["visual"] == {
        "status": "success",
        "verdict": "uncertain",
    }
    assert result.observations[0]["capability"] == "forge.capture.run:capture"
    with pytest.raises(ControlProgramError, match="forge.capture.run"):
        session.execute_cell("visual = await forge.call('capture')")


def test_bounded_program_state_keeps_small_recent_diagnostics() -> None:
    variables = {
        "large_inspection": {"content": "x" * (20 * 1024)},
        "sync_result": {
            "output": {
                "status": "failed",
                "diagnostics": ["Scene instance is missing"],
            }
        },
        "second_large_inspection": {"content": "y" * (20 * 1024)},
    }

    bounded = _bounded_program_state(variables)

    assert bounded["sync_result"] == variables["sync_result"]
    assert bounded["__projection__"]["truncated"] is True
    assert len(json.dumps(bounded, sort_keys=True).encode("utf-8")) <= 32 * 1024


@pytest.mark.parametrize("diagnostic_position", ["first", "last"])
@pytest.mark.parametrize("variable_count", [512, 2_048])
def test_bounded_program_state_is_hard_bounded_and_retains_diagnostics(
    variable_count: int,
    diagnostic_position: str,
) -> None:
    variables: dict[str, object] = {}
    diagnostic = {
        "status": "failed",
        "diagnostics": ["Parse Error: STATE_SENTINEL"],
    }
    if diagnostic_position == "first":
        variables["import_result"] = diagnostic
    for index in range(variable_count - 1):
        variables[f"value_{index:05d}"] = {
            "status": "success",
            "value": index,
            "label": f"bounded-value-{index}",
        }
    if diagnostic_position == "last":
        variables["import_result"] = diagnostic

    bounded = _bounded_program_state(variables)
    encoded = json.dumps(bounded, sort_keys=True).encode("utf-8")

    assert len(encoded) <= 32 * 1024
    assert b"STATE_SENTINEL" in encoded
    assert bounded["__projection__"]["omitted_count"] > 0


def test_control_language_supports_common_model_generated_text_operations() -> None:
    session, _, _ = _session()

    result = session.execute_cell(
        "pairs = [['one', 1], ['two', 2]]\n"
        "labels = [('%s-%02d' % [name, value]).zfill(8) for name, value in pairs]\n"
        "counts = ['banana'.count('a'), 'banana'.index('n')]\n"
        "lines = 'one\\ntwo'.splitlines()"
    )

    assert result.execution.variables["labels"] == ["00one-01", "00two-02"]
    assert result.execution.variables["counts"] == [3, 2]
    assert result.execution.variables["lines"] == ["one", "two"]


def test_control_language_supports_bounded_python_mapping_and_loop_patterns() -> None:
    session, _, _ = _session()

    result = session.execute_cell(
        "resource_ids = {}\n"
        "for family, direction, frame in [['idle', 0, 0], ['run', 1, 2]]:\n"
        "    resource_ids[(family, direction, frame)] = family + str(frame)\n"
        "idle = resource_ids[('idle', 0, 0)]\n"
        "run = resource_ids.get(('run', 1, 2))\n"
        "present = ('idle', 0, 0) in resource_ids\n"
        "decoded_keys = resource_ids.keys()\n"
        "mask = 6 & 2\n"
        "all_positive = all(value > 0 for value in [1, 2, 3])\n"
        "counter = 0\n"
        "while counter < 3:\n"
        "    counter += 1\n"
        "ordered = [3, 1, 2]\n"
        "ordered.sort()\n"
        "numbered = enumerate(['a', 'b'], 4)\n"
        "types_ok = [isinstance(resource_ids, dict), isinstance(idle, (str, list))]"
    )

    assert result.execution.variables["idle"] == "idle0"
    assert result.execution.variables["run"] == "run2"
    assert result.execution.variables["present"] is True
    assert result.execution.variables["decoded_keys"] == [
        ["idle", 0, 0],
        ["run", 1, 2],
    ]
    assert result.execution.variables["mask"] == 2
    assert result.execution.variables["all_positive"] is True
    assert result.execution.variables["counter"] == 3
    assert result.execution.variables["ordered"] == [1, 2, 3]
    assert result.execution.variables["numbered"] == [[4, "a"], [5, "b"]]
    assert result.execution.variables["types_ok"] == [True, True]


def test_control_language_bounds_while_loops() -> None:
    session, _, _ = _session()

    with pytest.raises(ControlProgramError, match="while loop iteration limit exceeded"):
        session.execute_cell("while True:\n    pass")


def test_program_decision_accepts_harmless_json_wrappers_and_metadata() -> None:
    decision = ProgramDecision.from_json(
        '```json\n{"code":"value = 1","presentation_note":"continue"}\n```'
    )

    assert decision.kind == "cell"
    assert decision.code == "value = 1"
    assert decision.rationale == "Execute the supplied control decision."

    cell_with_finish_noise = ProgramDecision.from_json(
        '{"kind":"cell","code":"value = 2","rationale":null,'
        '"outcome":"success","summary":"not yet","evidence":["ignored"]}'
    )
    assert cell_with_finish_noise.code == "value = 2"
    assert cell_with_finish_noise.outcome is None
    assert cell_with_finish_noise.evidence == ()

    finish_with_null_evidence = ProgramDecision.from_json(
        '{"kind":"finish","outcome":"success","summary":"done",'
        '"rationale":"complete","evidence":null,"code":null}'
    )
    assert finish_with_null_evidence.evidence == ()

    prose_wrapped = ProgramDecision.from_json(
        'Here is the requested decision:\n{"code":"value = 3"}\nThis is ready to run.'
    )
    assert prose_wrapped.kind == "cell"
    assert prose_wrapped.code == "value = 3"

    workspace = ProgramDecision.from_json(
        '{"source":"from pathlib import Path\\nprint(Path.cwd())",'
        '"paths":[],"rationale":"inspect freely"}'
    )
    assert workspace.kind == "workspace"
    assert workspace.timeout_seconds == 120
    assert workspace.paths == ()

    capability = ProgramDecision.from_json(
        json.dumps(
            {
                "kind": "capability",
                "capability": "capture",
                "arguments": {},
                "rationale": "Inspect the current rendered candidate.",
            }
        )
    )
    assert capability.kind == "capability"
    assert capability.capability == "capture"
    assert capability.arguments == {}


def test_control_language_exposes_public_json_keys_as_attributes() -> None:
    session, _, _ = _session()

    result = session.execute_cell(
        "payload = {'value': 3, 'label': 'Alpha'}\n"
        "answer = payload.value + len(payload.label.lower())"
    )

    assert result.execution.variables["answer"] == 8


def test_control_language_print_returns_a_bounded_cell_result() -> None:
    session, _, _ = _session()

    result = session.execute_cell("print({'status': 'ok'}, 3, sep=' | ', end='')")

    assert result.execution.value == "{'status': 'ok'} | 3"


def test_control_language_print_rejects_unbounded_output() -> None:
    session, _, _ = _session()

    with pytest.raises(ControlProgramError, match="print output exceeds"):
        session.execute_cell("print('x' * 40000)")


def test_cell_result_projection_is_hard_bounded() -> None:
    result = _bounded_cell_result({"payload": "x" * (40 * 1024)})

    assert result["truncated"] is True
    assert result["original_json_bytes"] > 32 * 1024


def test_program_runtime_accounts_and_hides_capability_model_usage() -> None:
    ledger = CostLedger(maximum_usd=1)
    observations = (
        {
            "capability": "model-backed-tool",
            "value": {
                "status": "success",
                "_harness_usage": {
                    "provider": "nested",
                    "model": "test",
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "cached_input_tokens": 60,
                    "reasoning_output_tokens": 8,
                    "cost_usd": 0.01,
                },
            },
        },
    )

    sanitized = _account_auxiliary_usage(observations, ledger)

    assert sanitized[0]["value"] == {"status": "success"}
    assert ledger.input_tokens == 100
    assert ledger.output_tokens == 20
    assert ledger.cached_input_tokens == 60
    assert ledger.reasoning_output_tokens == 8
    assert ledger.total_usd == 0.01


def test_program_decision_still_rejects_multiple_json_objects() -> None:
    with pytest.raises(Exception, match="program decision is invalid"):
        ProgramDecision.from_json(
            '{"kind":"cell","code":"value = 1","rationale":"one"}'
            '{"kind":"finish","outcome":"success","summary":"two",'
            '"rationale":"two"}'
        )


def test_program_decision_exposes_optional_native_agent_without_a_fixed_route() -> None:
    decision = ProgramDecision.from_json(
        json.dumps(
            {
                "kind": "agent",
                "focus": "Inspect, implement, and verify with whatever local tools are useful.",
                "timeout_seconds": 600,
                "rationale": "Sustained tool continuity is useful for this project.",
            }
        )
    )

    assert decision.kind == "agent"
    assert decision.timeout_seconds == 600
    assert decision.focus is not None


def test_workspace_decision_retains_180_second_bound() -> None:
    with pytest.raises(Exception, match="program decision is invalid"):
        ProgramDecision.from_json(
            json.dumps(
                {
                    "kind": "workspace",
                    "source": "print('bounded')",
                    "paths": [],
                    "timeout_seconds": 600,
                    "rationale": "This should use the native agent decision instead.",
                }
            )
        )


def test_control_cell_wall_budget_scales_with_run_budget() -> None:
    limits = _control_limits_for_run(RunBudgets(wall_seconds=1800, max_turns=18, max_tool_calls=40))

    assert limits.maximum_wall_seconds == 200


class ScriptedProgramModel:
    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[dict[str, object]] = []

    def complete(self, *, system: str, prompt: str) -> ModelResponse:
        assert "programmable Harness" in system
        assert "run_workspace_program" in system
        assert "game_task" in prompt
        self.prompts.append(json.loads(prompt))
        self.calls += 1
        decision = (
            {
                "kind": "cell",
                "code": ("result = await forge.call('read', path='scene.tscn')\nprint(result)"),
                "rationale": "Inspect through a typed capability.",
            }
            if self.calls == 1
            else {
                "kind": "finish",
                "outcome": "success",
                "summary": "Public work is complete; evaluate independently.",
                "evidence": [],
                "rationale": "Only the hidden evaluator remains.",
            }
        )
        return ModelResponse(
            text=json.dumps(decision),
            usage=ModelUsage(10, 5, 0.001),
            provider="scripted",
            model="test",
        )


def test_programmable_runtime_does_not_offer_infrastructure_failure_to_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _, _ = _session()
    model = ScriptedProgramModel()
    task = ChangeRequestTaskSource("Inspect and finish.").load(tmp_path)
    run_spec = create_run_spec_v2(
        task,
        ModelProfile(provider="mock", model="test"),
        engine=EngineName.GODOT,
        engine_version="4.4",
        profile=HarnessProfile.PROJECT,
        game_task=session.gates.game_task,
        engine_environment=EngineEnvironment(engine_version="4.4", target_platform="test"),
        budgets=RunBudgets(max_turns=3, max_tool_calls=3, max_cost_usd=1),
        now=datetime(2026, 8, 9, tzinfo=UTC),
    )

    def fail_cell(source: str) -> None:
        del source
        raise InfrastructureFailure("engine crashed")

    monkeypatch.setattr(session, "execute_cell", fail_cell)

    with pytest.raises(InfrastructureFailure, match="engine crashed"):
        ProgrammableAgentRuntime(
            model=model,
            broker=session.broker,
            gates=session.gates,
            session=session,
            events=session.events,
            budgets=run_spec.budgets,
        ).run(run_spec)

    assert model.calls == 1


def test_programmable_agent_runtime_uses_public_projection_and_provisional_finish(
    tmp_path: Path,
) -> None:
    registry = CapabilityRegistry()
    registry.register(_read_descriptor(), lambda arguments: {"path": arguments["path"]})
    events = InMemoryEventStore("run")
    evidence = EvidenceGraph(events=events)
    digest = empty_digest()
    evidence.commit_project_revision(
        source_tree_hash=digest,
        diff_hash=digest,
        asset_graph_digest=digest,
    )
    broker = CapabilityBroker(run_id="run", registry=registry, events=events)
    game_task = GameTaskSpec(
        engine_profile="test",
        entry_points=("Main",),
        target_platforms=("test",),
        requirements=(
            AcceptanceRequirement(
                id="hidden",
                dimension=AcceptanceDimension.BEHAVIOR,
                enforcement=RequirementEnforcement.REQUIRED,
                evaluator_ref="hidden-evaluator",
                model_visible=False,
            ),
        ),
    )
    planner = GatePlanner(
        game_task=game_task,
        environment_digest=digest,
        evidence=evidence,
        events=events,
        registry=GateRegistry(),
    )
    session = RestrictedControlSession(broker=broker, gates=planner, events=events)
    task = ChangeRequestTaskSource("Inspect and finish.").load(tmp_path)
    run_spec = create_run_spec_v2(
        task,
        ModelProfile(provider="mock", model="test"),
        engine=EngineName.GODOT,
        engine_version="4.4",
        profile=HarnessProfile.PROJECT,
        game_task=game_task,
        engine_environment=EngineEnvironment(engine_version="4.4", target_platform="test"),
        budgets=RunBudgets(max_turns=3, max_tool_calls=3, max_cost_usd=1),
        now=datetime(2026, 8, 9, tzinfo=UTC),
    )

    model = ScriptedProgramModel()
    result = ProgrammableAgentRuntime(
        model=model,
        broker=broker,
        gates=planner,
        session=session,
        events=events,
        budgets=run_spec.budgets,
    ).run(run_spec)

    assert result.outcome == "success"
    assert result.program_cells == 1
    assert result.tool_calls == 1
    assert result.turns == 2
    assert model.prompts[1]["observations"][-1] == {
        "kind": "cell_result",
        "value": "{'path': 'scene.tscn'}\n",
    }
    assert "registered capabilities" in model.prompts[0]["visual_grounding"]["guidance"]
    assert model.prompts[0]["budgets"]["turns_remaining_including_this_response"] == 3
    assert model.prompts[1]["budgets"]["turns_remaining_including_this_response"] == 2
