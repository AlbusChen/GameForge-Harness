from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from gameforge.adapters.llm import AgentCliLanguageModel, LanguageModel, ModelError
from gameforge.harness.artifacts import ContentAddressedArtifactStore
from gameforge.harness.capabilities import (
    CapabilityBroker,
    CapabilityCallError,
    CapabilityDescriptor,
    CapabilityEffect,
    CapabilityPhase,
    CapabilityRegistry,
)
from gameforge.harness.contracts import (
    ArtifactRecord,
    GateOutcome,
    GateStatus,
    RunBudgets,
    RunResultV2,
    RunSpecV2,
    RunStatus,
)
from gameforge.harness.control_language import ControlLimits
from gameforge.harness.control_session import RestrictedControlSession
from gameforge.harness.engine_lifecycle import EngineLifecycleCoordinator
from gameforge.harness.errors import InfrastructureFailure
from gameforge.harness.events import EventKind, JsonlEventStore
from gameforge.harness.evidence import (
    EvidenceGraph,
    EvidenceSubjectKind,
    GateEvidenceStatus,
    ObservationKind,
    empty_digest,
    environment_digest,
)
from gameforge.harness.game_tasks import AcceptanceDimension
from gameforge.harness.gate_planner import (
    GateDescriptor,
    GateExecutionResult,
    GateLevel,
    GatePlanner,
    GateRegistry,
)
from gameforge.harness.interfaces import EngineAdapter, EvaluatorAdapter
from gameforge.harness.playtest import PlaytestRunner
from gameforge.harness.policy_engine import CapabilityPolicyConfig, ScopedCapabilityPolicy
from gameforge.harness.preservation import (
    ProjectSnapshot,
    compare_snapshots,
    snapshot_project,
)
from gameforge.harness.program_runtime import (
    ProgrammableAgentRuntime,
    ProgrammableRuntimeResult,
)
from gameforge.harness.program_state import ControlSnapshot, ProgramStateStore
from gameforge.harness.project_scope import ProjectAccessScope
from gameforge.harness.resource_safety import (
    ResourceSafetyReport,
    build_asset_graph,
    compare_asset_graphs,
)
from gameforge.harness.skills import SkillRegistry
from gameforge.harness.workspace_agent import (
    WorkspaceAgentExecutor,
    register_workspace_agent_capability,
)
from gameforge.harness.workspace_program import (
    WorkspaceProgramExecutor,
    register_workspace_program_capability,
)
from gameforge.plugins.legacy import LegacyEnginePlugin


@dataclass(frozen=True)
class _ProjectWriteTransaction:
    backup_directory: Path
    files: dict[str, Path | None]


@dataclass
class ProgrammableHarnessRunner:
    model: LanguageModel
    engine: EngineAdapter
    evaluator: EvaluatorAdapter
    skills: SkillRegistry | None = None

    def execute(self, run_spec: RunSpecV2, output_root: Path):
        from gameforge.harness.runner import HarnessRun, HarnessRunner

        started_at = datetime.now(UTC)
        run_directory = output_root / run_spec.run_id
        run_directory.mkdir(parents=True, exist_ok=False)
        _write_json(run_directory / "run-spec.json", run_spec.model_dump(mode="json"))
        events = JsonlEventStore(run_directory / "events.jsonl", run_spec.run_id)
        events.append(
            EventKind.RUN_STARTED,
            payload={
                "run_spec_version": run_spec.version,
                "runtime_protocol": run_spec.runtime_protocol.value,
            },
        )
        artifact_store = ContentAddressedArtifactStore(
            run_directory / "artifact-store",
            events=events,
        )
        runtime_result: ProgrammableRuntimeResult | None = None
        evidence = EvidenceGraph(events=events)
        gates: tuple[GateOutcome, ...] = tuple(
            GateOutcome(gate=name, status=GateStatus.NOT_RUN, detail="agent did not finish")
            for name in run_spec.evaluation.required_gates
        )
        status = RunStatus.BLOCKED
        failure_category: str | None = None
        failure_detail: str | None = None
        program_cells = 0
        required_evidence_ids: tuple[str, ...] = ()
        required_evidence_coverage = 0.0
        lineage_complete = False
        dimension_status: dict[str, str] = {}
        project_scope: ProjectAccessScope | None = None
        try:
            before_snapshot = snapshot_project(run_spec.task.project_path)
            before_graph = build_asset_graph(
                run_spec.task.project_path,
                generated_roots=run_spec.game_task.asset_policy.generated_roots,
            )
            _write_json(
                run_directory / "project-before.json",
                before_snapshot.model_dump(mode="json"),
            )
            _write_json(
                run_directory / "asset-graph-before.json",
                before_graph.model_dump(mode="json"),
            )
            initial_revision = evidence.commit_project_revision(
                source_tree_hash=_snapshot_digest(before_snapshot),
                diff_hash=empty_digest(),
                asset_graph_digest=before_graph.digest,
            )
            project_scope = ProjectAccessScope(
                run_spec.task.editable_paths,
                allow_text_scope_expansion=(
                    run_spec.game_task.asset_policy.allow_text_scope_expansion
                ),
                maximum_writable_paths=(run_spec.game_task.asset_policy.maximum_writable_paths),
            )
            bind_project_scope = getattr(self.engine, "bind_project_scope", None)
            if callable(bind_project_scope):
                bind_project_scope(project_scope)
            _write_json(run_directory / "output-manifest.json", project_scope.snapshot())
            plugin = LegacyEnginePlugin(self.engine)
            manifest = plugin.manifest()
            registry = CapabilityRegistry()
            plugin.register(registry)
            host_managed_executables = ("godot",) if self.engine.name.casefold() == "godot" else ()
            register_workspace_program_capability(
                registry,
                WorkspaceProgramExecutor(
                    project=run_spec.task.project_path,
                    scratch_root=run_directory / "workspace-program",
                    project_scope=project_scope,
                    host_managed_executables=host_managed_executables,
                ),
            )
            if (
                isinstance(self.model, AgentCliLanguageModel)
                and run_spec.game_task.asset_policy.allow_native_workspace_agent
            ):
                text_suffixes_provider = getattr(self.engine, "workspace_text_suffixes", None)
                new_text_suffixes = (
                    tuple(text_suffixes_provider())
                    if callable(text_suffixes_provider) and project_scope.allow_text_scope_expansion
                    else ()
                )
                register_workspace_agent_capability(
                    registry,
                    WorkspaceAgentExecutor(
                        project=run_spec.task.project_path,
                        scratch_root=run_directory / "workspace-agent",
                        project_scope=project_scope,
                        model=self.model,
                        task_instruction=run_spec.task.instruction,
                        engine_environment=run_spec.engine_environment.model_dump(mode="json"),
                        new_text_suffixes=new_text_suffixes,
                        host_managed_executables=host_managed_executables,
                    ),
                )
            if self.skills is not None:
                self.skills.register_compatible_capabilities(
                    registry,
                    engine_family=self.engine.name,
                    environment=run_spec.engine_environment,
                    task_archetype=str(
                        run_spec.task.metadata.get("route", run_spec.task.source.value)
                    ),
                )
            _verify_pins(run_spec, manifest, registry)
            _write_json(
                run_directory / "resolved-plugins.json",
                {
                    "plugin": manifest.model_dump(mode="json"),
                    "capability_digests": registry.digests(),
                },
            )
            allowed_permissions = tuple(
                sorted(
                    {
                        permission
                        for descriptor in registry.descriptors()
                        for permission in descriptor.required_permissions
                        if not permission.startswith("approval:")
                    }
                )
            )
            env_digest = environment_digest(run_spec.engine_environment.model_dump(mode="json"))
            automatic_validation = self.engine.automatic_validation_tool()
            automatic_validation_pending = False
            explicit_validation_output: object | None = None
            current_snapshot = before_snapshot
            current_graph = before_graph
            host_reconciliations: list[dict[str, object]] = []

            def on_completed(
                descriptor: CapabilityDescriptor,
                arguments: object,
                output: object,
            ) -> None:
                nonlocal automatic_validation_pending
                nonlocal current_snapshot, current_graph, explicit_validation_output
                del arguments
                if descriptor.name == automatic_validation:
                    automatic_validation_pending = False
                    explicit_validation_output = output
                    return
                if CapabilityEffect.PROJECT_WRITE not in descriptor.effects:
                    return
                provisional_snapshot = snapshot_project(run_spec.task.project_path)
                changed = _changed_paths(current_snapshot, provisional_snapshot)
                if not changed:
                    return
                reconcile = getattr(self.engine, "reconcile_project_changes", None)
                if callable(reconcile):
                    report = reconcile(changed)
                    if not isinstance(report, dict):
                        raise CapabilityCallError(
                            "engine reconciliation must return an audit object"
                        )
                    host_reconciliations.append(
                        {
                            "capability": descriptor.name,
                            "input_changed_paths": list(changed),
                            "report": report,
                        }
                    )
                after_snapshot = snapshot_project(run_spec.task.project_path)
                changed = _changed_paths(current_snapshot, after_snapshot)
                after_graph = build_asset_graph(
                    run_spec.task.project_path,
                    generated_roots=run_spec.game_task.asset_policy.generated_roots,
                )
                protected_identities = tuple(
                    node.identity
                    for node in before_graph.nodes
                    if node.identity is not None and node.path not in project_scope.writable_paths
                )
                resource_report = compare_asset_graphs(
                    current_graph,
                    after_graph,
                    editable_paths=project_scope.writable_paths,
                    protected_identities=protected_identities,
                )
                if resource_report.new_broken_references:
                    raise CapabilityCallError(
                        "project mutation introduced broken resource references"
                    )
                evidence.commit_project_revision(
                    source_tree_hash=_snapshot_digest(after_snapshot),
                    diff_hash=_diff_digest(current_snapshot, after_snapshot),
                    asset_graph_digest=after_graph.digest,
                    changed_paths=changed,
                )
                current_snapshot = after_snapshot
                current_graph = after_graph
                explicit_validation_output = None
                automatic_validation_pending = automatic_validation is not None

            def begin_capability_transaction(
                descriptor: CapabilityDescriptor,
                arguments: object,
            ) -> object:
                del arguments
                if not any(
                    effect
                    in {
                        CapabilityEffect.PROJECT_WRITE,
                        CapabilityEffect.BINARY_ASSET_WRITE,
                    }
                    for effect in descriptor.effects
                ):
                    return None
                return _capture_project_write_transaction(
                    run_spec.task.project_path,
                    project_scope.writable_paths,
                    backup_root=run_directory / "transaction-backups",
                )

            def commit_capability_transaction(
                descriptor: CapabilityDescriptor,
                arguments: object,
                transaction: object,
                output: object,
            ) -> None:
                del descriptor, arguments, output
                if isinstance(transaction, _ProjectWriteTransaction):
                    _discard_project_write_transaction(transaction)

            def rollback_capability_transaction(
                descriptor: CapabilityDescriptor,
                arguments: object,
                transaction: object,
                error: Exception,
            ) -> None:
                del descriptor, arguments, error
                if isinstance(transaction, _ProjectWriteTransaction):
                    newly_declared_paths = tuple(
                        path
                        for path in project_scope.writable_paths
                        if path not in transaction.files
                    )
                    _restore_project_write_transaction(
                        run_spec.task.project_path,
                        transaction,
                        additional_created_paths=newly_declared_paths,
                    )

            def validate_completed_cell() -> tuple[dict[str, object], ...]:
                nonlocal automatic_validation_pending, explicit_validation_output
                if automatic_validation is None:
                    return ()
                if explicit_validation_output is not None and not automatic_validation_pending:
                    validation_output = explicit_validation_output
                    explicit_validation_output = None
                    imported_id = lifecycle.current_imported_id
                    if imported_id is None:
                        return ()
                    return record_automatic_validation(
                        imported_id,
                        validation_output,
                        source="explicit",
                    )
                if not automatic_validation_pending:
                    return ()
                automatic_validation_pending = False
                try:
                    imported, validation_output = lifecycle.synchronize(
                        automatic_validation,
                        {},
                        artifact_hashes=(),
                    )
                except CapabilityCallError as error:
                    return (
                        {
                            "capability": "automatic_validation",
                            "value": {"status": "failed", "error": str(error)},
                            "cached": False,
                        },
                    )
                explicit_validation_output = None
                return record_automatic_validation(
                    imported.id,
                    validation_output,
                    source="automatic",
                )

            def record_automatic_validation(
                imported_id: str,
                validation_output: object,
                *,
                source: str,
            ) -> tuple[dict[str, object], ...]:
                validation_passed = _capability_output_passed(validation_output)
                validation_status = (
                    GateEvidenceStatus.PASS if validation_passed else GateEvidenceStatus.FAIL
                )
                gate_ids: list[str] = []
                for requirement in run_spec.game_task.requirements:
                    if requirement.evaluator_ref != automatic_validation:
                        continue
                    existing = planner.evidence_for_requirement(
                        requirement.id,
                        passing_only=False,
                    )
                    gate = (
                        existing
                        if existing is not None
                        and existing.subject_id == imported_id
                        and existing.status is validation_status
                        else evidence.record_gate(
                            requirement_id=requirement.id,
                            subject_kind=EvidenceSubjectKind.IMPORTED_REVISION,
                            subject_id=imported_id,
                            status=validation_status,
                            environment_digest=env_digest,
                            evaluator_digest=hashlib.sha256(
                                f"{manifest.digest()}:{automatic_validation}".encode()
                            ).hexdigest(),
                            artifact_hashes=_extract_hashes(validation_output),
                            detail=f"{source} validation: {automatic_validation}",
                        )
                    )
                    gate_ids.append(gate.id)
                return (
                    {
                        "capability": "automatic_validation",
                        "value": {
                            "status": "pass" if validation_passed else "fail",
                            "tool": automatic_validation,
                            "source": source,
                            "gate_evidence_ids": gate_ids,
                            "output": _bounded_validation_output(validation_output),
                        },
                        "cached": False,
                    },
                )

            policy = ScopedCapabilityPolicy(
                CapabilityPolicyConfig(
                    allowed_permissions=allowed_permissions,
                    editable_paths=run_spec.task.editable_paths,
                    allow_binary_mutation=run_spec.game_task.asset_policy.allow_binary_mutation,
                    require_binary_provenance=(run_spec.game_task.asset_policy.require_provenance),
                    allowed_license_ids=run_spec.game_task.asset_policy.allowed_license_ids,
                ),
                project_scope=project_scope,
            )
            broker = CapabilityBroker(
                run_id=run_spec.run_id,
                registry=registry,
                events=events,
                policy=policy,
                completion_hook=on_completed,
                begin_hook=begin_capability_transaction,
                commit_hook=commit_capability_transaction,
                rollback_hook=rollback_capability_transaction,
                maximum_calls=run_spec.budgets.max_tool_calls,
                maximum_observation_bytes=(
                    run_spec.game_task.evidence_policy.maximum_observation_bytes
                ),
            )
            lifecycle = EngineLifecycleCoordinator(
                broker=broker,
                evidence=evidence,
                events=events,
                engine_version=run_spec.engine_version,
                plugin_digest=manifest.digest(),
                import_settings_digest=env_digest,
                environment_digest=env_digest,
            )
            broker.prerequisites = lifecycle
            broker.invalidator = lifecycle.invalidate
            gate_registry = _public_gate_registry(
                run_spec,
                registry=registry,
                broker=broker,
                lifecycle=lifecycle,
                evaluator_digest=hashlib.sha256(
                    f"{self.engine.name}:{self.engine.version}".encode()
                ).hexdigest(),
            )
            planner = GatePlanner(
                game_task=run_spec.game_task,
                environment_digest=env_digest,
                evidence=evidence,
                events=events,
                registry=gate_registry,
                target_platform=run_spec.engine_environment.target_platform,
            )
            bindings_provider = getattr(self.engine, "playtest_bindings", None)
            playtest_runner = None
            if callable(bindings_provider):
                playtest_runner = PlaytestRunner(
                    broker=broker,
                    lifecycle=lifecycle,
                    evidence=evidence,
                    events=events,
                    artifacts=artifact_store,
                    bindings=bindings_provider(),
                )
            session = RestrictedControlSession(
                broker=broker,
                gates=planner,
                events=events,
                lifecycle=lifecycle,
                playtest_runner=playtest_runner,
                playtest_scenarios={
                    scenario.id: scenario for scenario in run_spec.game_task.playtest_scenarios
                },
                limits=_control_limits_for_run(run_spec.budgets),
                cell_completion_hook=validate_completed_cell,
            )
            runtime_result = ProgrammableAgentRuntime(
                model=self.model,
                broker=broker,
                gates=planner,
                session=session,
                events=events,
                budgets=run_spec.budgets,
            ).run(run_spec)
            program_cells = runtime_result.program_cells
            _write_json(run_directory / "agent-result.json", runtime_result.to_dict())
            if host_reconciliations:
                _write_json(
                    run_directory / "host-reconciliation.json",
                    {
                        "schema_version": 1,
                        "invocations": host_reconciliations,
                    },
                )
            if runtime_result.outcome == "success":
                after_snapshot = snapshot_project(run_spec.task.project_path)
                after_graph = build_asset_graph(
                    run_spec.task.project_path,
                    generated_roots=run_spec.game_task.asset_policy.generated_roots,
                )
                preservation = compare_snapshots(
                    before_snapshot,
                    after_snapshot,
                    project_scope.writable_paths,
                )
                protected_identities = tuple(
                    node.identity
                    for node in before_graph.nodes
                    if node.identity is not None and node.path not in project_scope.writable_paths
                )
                resource_report = compare_asset_graphs(
                    before_graph,
                    after_graph,
                    editable_paths=project_scope.writable_paths,
                    protected_identities=protected_identities,
                )
                _write_json(
                    run_directory / "preservation.json",
                    preservation.model_dump(mode="json"),
                )
                _write_json(
                    run_directory / "resource-safety.json",
                    resource_report.model_dump(mode="json"),
                )
                preservation_passed = preservation.passed and resource_report.passed
                preservation_gate = GateOutcome(
                    gate="preservation",
                    status=GateStatus.PASS if preservation_passed else GateStatus.FAIL,
                    detail=(
                        "path, identity, and reference preservation passed"
                        if preservation_passed
                        else _resource_failure_detail(preservation, resource_report)
                    ),
                    artifacts=("preservation.json", "resource-safety.json"),
                )
                try:
                    events.append(EventKind.EVALUATION_STARTED)
                    evaluator_spec = HarnessRunner._without_preservation_gate(run_spec)
                    evaluated = self.evaluator.evaluate(
                        evaluator_spec,
                        run_spec.task.project_path,
                    )
                    events.append(EventKind.EVALUATION_COMPLETED)
                except Exception as error:
                    gates = HarnessRunner._merge_preservation_gate(run_spec, (), preservation_gate)
                    status = RunStatus.BLOCKED
                    failure_category = f"evaluator_{type(error).__name__}"
                    failure_detail = str(error)
                else:
                    gates = HarnessRunner._merge_preservation_gate(
                        run_spec, evaluated, preservation_gate
                    )
                    _record_host_evidence(
                        run_spec,
                        gates,
                        evidence=evidence,
                        environment_digest_value=env_digest,
                        evaluator_name=self.evaluator.name,
                        evaluator_version=self.evaluator.version,
                    )
                    final = planner.finish_decision(
                        no_unapproved_changes=preservation_passed,
                        budgets_within_limits=True,
                        no_uncertain_mutation=True,
                        engine_runtime_reconciled=True,
                    )
                    required = run_spec.game_task.required_requirements()
                    recorded_required_evidence = tuple(
                        gate
                        for requirement in required
                        if (
                            gate := planner.evidence_for_requirement(
                                requirement.id,
                                passing_only=False,
                            )
                        )
                        is not None
                    )
                    required_evidence_ids = tuple(gate.id for gate in recorded_required_evidence)
                    required_evidence_coverage = (
                        len(recorded_required_evidence) / len(required) if required else 1.0
                    )
                    lineage_complete = required_evidence_coverage == 1.0
                    dimension_status = _dimension_status(run_spec, planner)
                    native_status = HarnessRunner._status_from_gates(run_spec, gates)
                    status = native_status if final.allowed else RunStatus.BLOCKED
                    if native_status is RunStatus.FAIL or _required_game_evidence_failed(
                        run_spec, planner
                    ):
                        status = RunStatus.FAIL
                    if status is RunStatus.FAIL:
                        failure_category = "evaluation"
                        failure_detail = "required verification failed"
                    elif status is RunStatus.BLOCKED:
                        failure_category = "evidence_incomplete"
                        failure_detail = "; ".join(final.reasons) or "evaluator inconclusive"
                _write_json(
                    run_directory / "gate-results.json",
                    [gate.model_dump(mode="json") for gate in gates],
                )
            else:
                failure_category = "agent_blocked"
                failure_detail = runtime_result.summary
            _write_json(
                run_directory / "evidence.json",
                evidence.snapshot().model_dump(mode="json"),
            )
            snapshot = ControlSnapshot(
                run_id=run_spec.run_id,
                last_event_sequence=len(events.events()),
                capability_digests=registry.digests(),
                plugin_digests={manifest.name: manifest.digest()},
                program=session.state,
                evidence=evidence.snapshot(),
            )
            ProgramStateStore(run_directory / "control-snapshot.json").save(snapshot)
            del initial_revision
        except (ModelError, OSError, ValueError, RuntimeError) as error:
            failure_category = type(error).__name__
            failure_detail = str(error)
            _write_json(
                run_directory / "failure.json",
                {"category": failure_category, "detail": failure_detail},
            )
        if project_scope is not None:
            _write_json(run_directory / "output-manifest.json", project_scope.snapshot())
        final_event = events.append(
            EventKind.RUN_COMPLETED
            if status in {RunStatus.PASS, RunStatus.FAIL}
            else EventKind.RUN_BLOCKED,
            payload={
                "status": status.value,
                "failure_category": failure_category,
                "failure_detail": failure_detail,
            },
        )
        del final_event
        _write_jsonl(
            run_directory / "trace.jsonl",
            tuple(event.model_dump(mode="json") for event in events.events()),
        )
        model_turns = runtime_result.turns if runtime_result else 0
        capability_calls_per_turn = (
            runtime_result.tool_calls / model_turns
            if runtime_result is not None and model_turns
            else 0.0
        )
        policy_violations = sum(
            1
            for event in events.events()
            if event.kind is EventKind.CAPABILITY_FAILED
            and event.payload.get("type") == "CapabilityPolicyViolation"
        )
        metrics = {
            "model_turns": model_turns,
            "program_cells": program_cells,
            "capability_calls": runtime_result.tool_calls if runtime_result else 0,
            "capability_calls_per_turn": capability_calls_per_turn,
            "prompt_bytes": runtime_result.prompt_bytes if runtime_result else 0,
            "required_evidence_coverage": required_evidence_coverage,
            "lineage_complete": lineage_complete,
            "dimension_status": dimension_status,
            "policy_violations": policy_violations,
        }
        _write_json(run_directory / "metrics.json", metrics)
        artifacts = _artifact_manifest(run_directory)
        result = RunResultV2(
            run_id=run_spec.run_id,
            status=status,
            started_at=started_at,
            ended_at=datetime.now(UTC),
            model=run_spec.model,
            gates=gates,
            artifacts=artifacts,
            input_tokens=runtime_result.input_tokens if runtime_result else 0,
            output_tokens=runtime_result.output_tokens if runtime_result else 0,
            cached_input_tokens=(runtime_result.cached_input_tokens if runtime_result else 0),
            reasoning_output_tokens=(
                runtime_result.reasoning_output_tokens if runtime_result else 0
            ),
            cost_usd=runtime_result.cost_usd if runtime_result else 0,
            tool_calls=runtime_result.tool_calls if runtime_result else 0,
            failure_category=failure_category,
            failure_detail=failure_detail,
            program_cells=program_cells,
            model_turns=model_turns,
            prompt_bytes=runtime_result.prompt_bytes if runtime_result else 0,
            capability_calls_per_turn=capability_calls_per_turn,
            evidence_subjects=_evidence_subject_count(evidence),
            required_evidence_ids=required_evidence_ids,
            required_evidence_coverage=required_evidence_coverage,
            lineage_complete=lineage_complete,
            dimension_status=dimension_status,
            policy_violations=policy_violations,
        )
        _write_json(run_directory / "result.json", result.model_dump(mode="json"))
        return HarnessRun(run_directory, result)


def _public_gate_registry(
    run_spec: RunSpecV2,
    *,
    registry: CapabilityRegistry,
    broker: CapabilityBroker,
    lifecycle: EngineLifecycleCoordinator,
    evaluator_digest: str,
) -> GateRegistry:
    gates = GateRegistry()
    capability_names = {descriptor.name for descriptor in registry.descriptors()}
    registered: set[str] = set()
    for requirement in run_spec.game_task.requirements:
        name = requirement.evaluator_ref
        if not requirement.model_visible or name not in capability_names or name in registered:
            continue
        descriptor = registry.get(name).descriptor

        def handler(requirement, subject_kind, subject_id, *, capability=name, item=descriptor):
            if item.phase is CapabilityPhase.ENGINE_SYNC:
                if subject_kind is not EvidenceSubjectKind.PROJECT_REVISION:
                    raise CapabilityCallError("engine sync gate requires a project revision")
                imported, output = lifecycle.synchronize(
                    capability,
                    requirement.evaluator_arguments,
                    artifact_hashes=(),
                )
                result_subject_kind = EvidenceSubjectKind.IMPORTED_REVISION
                result_subject_id = imported.id
            elif item.phase is CapabilityPhase.OBSERVE:
                output = broker.invoke(capability, requirement.evaluator_arguments).output
                result_subject_kind = subject_kind
                result_subject_id = subject_id
            else:
                raise CapabilityCallError(
                    "public gate capability must be observe or engine_sync phase"
                )
            return GateExecutionResult(
                status=(
                    GateEvidenceStatus.PASS
                    if _capability_output_passed(output)
                    else GateEvidenceStatus.FAIL
                ),
                detail=f"public capability gate {capability} completed",
                artifact_hashes=_extract_hashes(output),
                subject_kind=result_subject_kind,
                subject_id=result_subject_id,
            )

        gates.register(
            GateDescriptor(
                name=name,
                version=descriptor.version,
                level=(
                    GateLevel.ENGINE_SYNC
                    if descriptor.phase is CapabilityPhase.ENGINE_SYNC
                    else GateLevel.SOURCE
                ),
                subject_kinds=(EvidenceSubjectKind.PROJECT_REVISION,),
                model_visible=True,
                evaluator_digest=evaluator_digest,
            ),
            handler,
        )
        registered.add(name)
    return gates


def _record_host_evidence(
    run_spec: RunSpecV2,
    gates: tuple[GateOutcome, ...],
    *,
    evidence: EvidenceGraph,
    environment_digest_value: str,
    evaluator_name: str,
    evaluator_version: str,
) -> None:
    by_name = {gate.gate: gate for gate in gates}
    if evidence.current_project_id is None:
        return
    for requirement in run_spec.game_task.requirements:
        if evidence.fresh_passing_gate(requirement.id, environment_digest_value) is not None:
            continue
        outcome = by_name.get(requirement.evaluator_ref)
        if outcome is None:
            continue
        if requirement.dimension is AcceptanceDimension.BEHAVIOR:
            _record_host_behavior_observation(
                run_spec,
                requirement_id=requirement.id,
                outcome=outcome,
                evidence=evidence,
                environment_digest_value=environment_digest_value,
                evaluator_name=evaluator_name,
                evaluator_version=evaluator_version,
            )
        subject = _host_evidence_subject(requirement.dimension, evidence)
        if subject is None:
            continue
        subject_kind, subject_id = subject
        evidence.record_gate(
            requirement_id=requirement.id,
            subject_kind=subject_kind,
            subject_id=subject_id,
            status=GateEvidenceStatus(outcome.status.value),
            environment_digest=environment_digest_value,
            evaluator_digest=hashlib.sha256(
                f"{evaluator_name}:{evaluator_version}:{outcome.gate}".encode()
            ).hexdigest(),
            artifact_hashes=tuple(
                value for value in (_hash_artifact(path) for path in outcome.artifacts) if value
            ),
            detail=outcome.detail,
        )


def _record_host_behavior_observation(
    run_spec: RunSpecV2,
    *,
    requirement_id: str,
    outcome: GateOutcome,
    evidence: EvidenceGraph,
    environment_digest_value: str,
    evaluator_name: str,
    evaluator_version: str,
) -> None:
    """Bind a host evaluator execution log to a typed no-input play session."""
    artifact_hashes = tuple(
        value for value in (_hash_artifact(path) for path in outcome.artifacts) if value
    )
    imported = tuple(
        item
        for item in evidence.imported_revisions.values()
        if evidence.subject_is_fresh(EvidenceSubjectKind.IMPORTED_REVISION, item.id)
    )
    if not artifact_hashes or not imported:
        return
    invocation = {
        "evaluator": evaluator_name,
        "version": evaluator_version,
        "requirement_id": requirement_id,
        "seed": run_spec.evaluation.seed,
        "input": "none",
    }
    input_trace_hash = hashlib.sha256(
        json.dumps(invocation, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    session = evidence.start_session(
        parent_kind=EvidenceSubjectKind.IMPORTED_REVISION,
        parent_id=imported[-1].id,
        entry_point=f"host-evaluator:{evaluator_name}",
        seed=run_spec.evaluation.seed,
        fixed_timestep_seconds=None,
        environment_digest=environment_digest_value,
    )
    evidence.bind_input_trace(session.id, input_trace_hash)
    evidence.record_observation(
        subject_kind=EvidenceSubjectKind.PLAY_SESSION,
        subject_id=session.id,
        kind=ObservationKind.LOG,
        artifact_hashes=artifact_hashes,
        environment_digest=environment_digest_value,
        input_trace_hash=input_trace_hash,
        metadata={
            "source": "host_evaluator",
            "evaluator": evaluator_name,
            "requirement_id": requirement_id,
        },
    )
    evidence.stop_session(session.id)


def _host_evidence_subject(dimension: AcceptanceDimension, evidence: EvidenceGraph):
    current = evidence.current_project_id
    if current is None:
        return None
    current_observations = tuple(
        item
        for item in evidence.observations.values()
        if evidence.subject_is_fresh(EvidenceSubjectKind.OBSERVATION, item.id)
    )
    observation_kinds = {
        AcceptanceDimension.BEHAVIOR: {
            "structured",
            "log",
            "input_trace",
        },
        AcceptanceDimension.VISUAL: {"screenshot", "video"},
        AcceptanceDimension.AUDIO: {"audio"},
        AcceptanceDimension.PERFORMANCE: {"performance"},
    }
    if dimension in observation_kinds:
        accepted = observation_kinds[dimension]
        matching = tuple(item for item in current_observations if item.kind.value in accepted)
        if matching:
            return EvidenceSubjectKind.OBSERVATION, matching[-1].id
        if dimension is AcceptanceDimension.BEHAVIOR:
            sessions = tuple(
                item
                for item in evidence.sessions.values()
                if evidence.subject_is_fresh(EvidenceSubjectKind.PLAY_SESSION, item.id)
            )
            if sessions:
                return EvidenceSubjectKind.PLAY_SESSION, sessions[-1].id
        return None
    if dimension is AcceptanceDimension.BUILD:
        builds = tuple(
            item
            for item in evidence.builds.values()
            if evidence.subject_is_fresh(EvidenceSubjectKind.BUILD_ARTIFACT, item.id)
        )
        return (EvidenceSubjectKind.BUILD_ARTIFACT, builds[-1].id) if builds else None
    if dimension is AcceptanceDimension.STRUCTURE:
        imports = tuple(
            item
            for item in evidence.imported_revisions.values()
            if evidence.subject_is_fresh(EvidenceSubjectKind.IMPORTED_REVISION, item.id)
        )
        if imports:
            return EvidenceSubjectKind.IMPORTED_REVISION, imports[-1].id
    return EvidenceSubjectKind.PROJECT_REVISION, current


def _verify_pins(run_spec: RunSpecV2, manifest, registry: CapabilityRegistry) -> None:
    if run_spec.plugins:
        matching = tuple(pin for pin in run_spec.plugins if pin.name == manifest.name)
        if len(matching) != 1:
            raise ValueError("RunSpec plugin pins do not identify the active engine plugin")
        pin = matching[0]
        if pin.version != manifest.version or pin.digest != manifest.digest():
            raise ValueError("engine plugin pin does not match the loaded implementation")
        if pin.capabilities and set(pin.capabilities) != set(manifest.capability_digests):
            raise ValueError("engine plugin capability pin does not match the loaded manifest")
    actual = registry.digests()
    mismatched = sorted(
        name for name, digest in run_spec.capability_digests.items() if actual.get(name) != digest
    )
    if mismatched:
        raise ValueError(f"capability digests do not match RunSpec: {mismatched}")


def _snapshot_digest(snapshot: ProjectSnapshot) -> str:
    return hashlib.sha256(
        json.dumps(snapshot.files, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _diff_digest(before: ProjectSnapshot, after: ProjectSnapshot) -> str:
    changed = {
        path: {"before": before.files.get(path), "after": after.files.get(path)}
        for path in _changed_paths(before, after)
    }
    return hashlib.sha256(
        json.dumps(changed, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _capture_project_write_transaction(
    project: Path,
    writable_paths: tuple[str, ...],
    *,
    backup_root: Path | None = None,
) -> _ProjectWriteTransaction:
    """Stream the authorized revision to disk before one model-selected write call."""
    root = project.resolve(strict=True)
    if backup_root is not None:
        backup_root.mkdir(parents=True, exist_ok=True)
    backup_directory = Path(
        tempfile.mkdtemp(
            prefix="gameforge-transaction-",
            dir=backup_root,
        )
    )
    paths = set(writable_paths)
    paths.update(f"{path}.meta" for path in writable_paths)
    paths.update(f"{path}.uid" for path in writable_paths)
    files: dict[str, Path | None] = {}
    try:
        for index, raw_path in enumerate(sorted(paths)):
            target = _safe_transaction_target(root, raw_path)
            if not target.exists():
                files[raw_path] = None
                continue
            if not target.is_file() or target.is_symlink():
                raise InfrastructureFailure(
                    f"transaction target is not an ordinary file: {raw_path}"
                )
            backup = backup_directory / f"{index:08d}.backup"
            with target.open("rb") as source, backup.open("xb") as destination:
                shutil.copyfileobj(source, destination, length=1024 * 1024)
                destination.flush()
                os.fsync(destination.fileno())
            files[raw_path] = backup
    except Exception:
        shutil.rmtree(backup_directory, ignore_errors=True)
        raise
    return _ProjectWriteTransaction(
        backup_directory=backup_directory,
        files=files,
    )


def _restore_project_write_transaction(
    project: Path,
    transaction: _ProjectWriteTransaction,
    *,
    additional_created_paths: tuple[str, ...] = (),
) -> None:
    root = project.resolve(strict=True)
    for raw_path in additional_created_paths:
        if raw_path in transaction.files:
            continue
        target = _safe_transaction_target(root, raw_path)
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise InfrastructureFailure(f"transaction rollback target changed type: {raw_path}")
        target.unlink(missing_ok=True)
    for raw_path, backup in transaction.files.items():
        target = _safe_transaction_target(root, raw_path)
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise InfrastructureFailure(f"transaction rollback target changed type: {raw_path}")
        if backup is None:
            target.unlink(missing_ok=True)
            continue
        if not backup.is_file() or backup.is_symlink():
            raise InfrastructureFailure(f"transaction backup is unavailable: {raw_path}")
        current = root
        for part in Path(raw_path).parts[:-1]:
            current /= part
            if current.is_symlink():
                raise InfrastructureFailure(
                    f"transaction rollback path crosses a symlink: {raw_path}"
                )
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix=f".{target.name}.",
            dir=target.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            with backup.open("rb") as source:
                shutil.copyfileobj(source, stream, length=1024 * 1024)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    _discard_project_write_transaction(transaction)


def _discard_project_write_transaction(transaction: _ProjectWriteTransaction) -> None:
    """Release a completed snapshot; rollback failures intentionally retain it."""
    shutil.rmtree(transaction.backup_directory, ignore_errors=True)


def _safe_transaction_target(root: Path, raw_path: str) -> Path:
    relative = Path(raw_path)
    if not raw_path or relative.is_absolute() or ".." in relative.parts:
        raise InfrastructureFailure(f"unsafe transaction path: {raw_path}")
    target = root / relative
    if not target.resolve(strict=False).is_relative_to(root):
        raise InfrastructureFailure(f"transaction path escapes project: {raw_path}")
    return target


def _control_limits_for_run(budgets: RunBudgets) -> ControlLimits:
    seconds_per_turn = budgets.wall_seconds / budgets.max_turns
    return ControlLimits(
        maximum_steps=100_000,
        maximum_loop_iterations=4_096,
        maximum_wall_seconds=min(300.0, max(60.0, seconds_per_turn * 2.0)),
    )


def _changed_paths(before: ProjectSnapshot, after: ProjectSnapshot) -> tuple[str, ...]:
    return tuple(
        sorted(
            path
            for path in set(before.files) | set(after.files)
            if before.files.get(path) != after.files.get(path)
        )
    )


def _capability_output_passed(output: object) -> bool:
    if not isinstance(output, dict):
        return True
    if output.get("compiled") is False or output.get("accepted") is False:
        return False
    status = str(output.get("status", output.get("result", "success"))).lower()
    return status not in {"fail", "failed", "error", "inconclusive", "not_run"}


def _bounded_validation_output(output: object) -> object:
    """Expose actionable lifecycle diagnostics without copying unbounded logs into context."""
    encoded = json.dumps(output, sort_keys=True)
    maximum_bytes = 8 * 1024
    if len(encoded.encode("utf-8")) <= maximum_bytes:
        return output
    if not isinstance(output, dict):
        return {
            "truncated": True,
            "type": type(output).__name__,
            "bytes": len(encoded.encode("utf-8")),
        }
    keys = (
        "status",
        "result",
        "compiled",
        "accepted",
        "error",
        "diagnostics",
        "import_return_code",
        "resource_return_code",
        "startup_return_code",
        "startup_status",
    )
    projection: dict[str, object] = {
        "truncated": True,
        "bytes": len(encoded.encode("utf-8")),
        "available_keys": sorted(str(key) for key in output),
    }
    for key in keys:
        value = output.get(key)
        if key == "diagnostics" and isinstance(value, list):
            projection[key] = [str(item)[:1000] for item in value[:8]]
        elif value is not None:
            projection[key] = value
    return projection


def _extract_hashes(value: object) -> tuple[str, ...]:
    hashes: set[str] = set()
    if isinstance(value, dict):
        for child in value.values():
            hashes.update(_extract_hashes(child))
    elif isinstance(value, list | tuple):
        for child in value:
            hashes.update(_extract_hashes(child))
    elif (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    ):
        hashes.add(value)
    return tuple(sorted(hashes))


def _hash_artifact(raw_path: str) -> str | None:
    path = Path(raw_path)
    if not path.is_file() or path.is_symlink():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _resource_failure_detail(preservation, resource_report: ResourceSafetyReport) -> str:
    details: list[str] = []
    if preservation.unrelated_changed_paths:
        details.append("unrelated paths: " + ", ".join(preservation.unrelated_changed_paths))
    if resource_report.changed_protected_paths:
        details.append("protected resources: " + ", ".join(resource_report.changed_protected_paths))
    if resource_report.missing_protected_identities:
        details.append(
            "missing identities: " + ", ".join(resource_report.missing_protected_identities)
        )
    if resource_report.new_broken_references:
        details.append("new broken resource references")
    return "; ".join(details) or "resource preservation failed"


def _artifact_manifest(run_directory: Path) -> tuple[ArtifactRecord, ...]:
    artifacts: list[ArtifactRecord] = []
    for path in sorted(run_directory.rglob("*")):
        if not path.is_file() or path.name == "result.json":
            continue
        content = path.read_bytes()
        artifacts.append(
            ArtifactRecord(
                path=str(path.relative_to(run_directory)),
                kind=path.suffix.lstrip(".") or "file",
                sha256=hashlib.sha256(content).hexdigest(),
                bytes=len(content),
            )
        )
    return tuple(artifacts)


def _evidence_subject_count(evidence: EvidenceGraph) -> int:
    return sum(
        len(items)
        for items in (
            evidence.project_revisions,
            evidence.imported_revisions,
            evidence.builds,
            evidence.sessions,
            evidence.observations,
            evidence.gates,
        )
    )


def _required_game_evidence_failed(run_spec: RunSpecV2, planner: GatePlanner) -> bool:
    for requirement in run_spec.game_task.required_requirements():
        gate = planner.evidence_for_requirement(requirement.id, passing_only=False)
        if gate is not None and gate.status is GateEvidenceStatus.FAIL:
            return True
    return False


def _dimension_status(run_spec: RunSpecV2, planner: GatePlanner) -> dict[str, str]:
    statuses: dict[str, list[str]] = {}
    for requirement in run_spec.game_task.requirements:
        gate = planner.evidence_for_requirement(requirement.id, passing_only=False)
        status = gate.status.value if gate is not None else "MISSING"
        statuses.setdefault(requirement.dimension.value, []).append(status)
    priority = {"FAIL": 0, "MISSING": 1, "NOT_RUN": 2, "PASS": 3}
    return {
        dimension: min(values, key=lambda item: priority.get(item, 1))
        for dimension, values in sorted(statuses.items())
    }


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, records: tuple[dict[str, object], ...]) -> None:
    with path.open("w", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, sort_keys=True, default=str) + "\n")
