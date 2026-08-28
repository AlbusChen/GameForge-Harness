from __future__ import annotations

import hashlib

import pytest

from gameforge.harness.events import InMemoryEventStore
from gameforge.harness.evidence import (
    EvidenceGraph,
    EvidenceSubjectKind,
    GateEvidenceStatus,
    ObservationKind,
    empty_digest,
)
from gameforge.harness.game_tasks import (
    AcceptanceDimension,
    AcceptanceRequirement,
    GameTaskSpec,
    PlaytestScenario,
    RequirementEnforcement,
)
from gameforge.harness.gate_planner import (
    GateDescriptor,
    GateExecutionResult,
    GateLevel,
    GatePlanner,
    GatePlannerError,
    GateRegistry,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def test_gate_planner_separates_public_hidden_and_required_evidence() -> None:
    events = InMemoryEventStore("run")
    evidence = EvidenceGraph(events=events)
    digest = empty_digest()
    revision = evidence.commit_project_revision(
        source_tree_hash=digest,
        diff_hash=digest,
        asset_graph_digest=digest,
    )
    task = GameTaskSpec(
        engine_profile="test",
        entry_points=("Main",),
        target_platforms=("test",),
        requirements=(
            AcceptanceRequirement(
                id="structure",
                dimension=AcceptanceDimension.STRUCTURE,
                enforcement=RequirementEnforcement.REQUIRED,
                evaluator_ref="structure-gate",
            ),
            AcceptanceRequirement(
                id="hidden",
                dimension=AcceptanceDimension.STRUCTURE,
                enforcement=RequirementEnforcement.REQUIRED,
                evaluator_ref="hidden-gate",
                model_visible=False,
            ),
        ),
    )
    registry = GateRegistry()
    registry.register(
        GateDescriptor(
            name="structure-gate",
            version="1",
            level=GateLevel.SOURCE,
            subject_kinds=(EvidenceSubjectKind.PROJECT_REVISION,),
            evaluator_digest=_digest("structure"),
        ),
        lambda requirement, kind, subject: GateExecutionResult(status=GateEvidenceStatus.PASS),
    )
    registry.register(
        GateDescriptor(
            name="hidden-gate",
            version="1",
            level=GateLevel.FINAL,
            subject_kinds=(EvidenceSubjectKind.PROJECT_REVISION,),
            model_visible=False,
            evaluator_digest=_digest("hidden"),
        ),
        lambda requirement, kind, subject: GateExecutionResult(status=GateEvidenceStatus.PASS),
    )
    planner = GatePlanner(
        game_task=task,
        environment_digest=digest,
        evidence=evidence,
        events=events,
        registry=registry,
    )

    assert [item["name"] for item in planner.public_descriptions()] == ["structure-gate"]
    planner.run_public(
        "structure",
        subject_kind=EvidenceSubjectKind.PROJECT_REVISION,
        subject_id=revision.id,
    )
    with pytest.raises(GatePlannerError, match="not model-visible"):
        planner.run_public(
            "hidden",
            subject_kind=EvidenceSubjectKind.PROJECT_REVISION,
            subject_id=revision.id,
        )
    denied = planner.finish_decision(
        no_unapproved_changes=True,
        budgets_within_limits=True,
        no_uncertain_mutation=True,
        engine_runtime_reconciled=True,
    )
    assert not denied.allowed
    assert "hidden" in denied.reasons[0]
    planner.run_host(
        "hidden",
        subject_kind=EvidenceSubjectKind.PROJECT_REVISION,
        subject_id=revision.id,
    )
    allowed = planner.finish_decision(
        no_unapproved_changes=True,
        budgets_within_limits=True,
        no_uncertain_mutation=True,
        engine_runtime_reconciled=True,
    )
    assert allowed.allowed
    assert len(allowed.evidence_ids) == 2


def test_public_gate_reuses_fresh_evidence_for_the_current_revision() -> None:
    events = InMemoryEventStore("run")
    evidence = EvidenceGraph(events=events)
    digest = empty_digest()
    revision = evidence.commit_project_revision(
        source_tree_hash=digest,
        diff_hash=digest,
        asset_graph_digest=digest,
    )
    task = GameTaskSpec(
        engine_profile="test",
        entry_points=("Main",),
        target_platforms=("test",),
        requirements=(
            AcceptanceRequirement(
                id="structure",
                dimension=AcceptanceDimension.STRUCTURE,
                enforcement=RequirementEnforcement.REQUIRED,
                evaluator_ref="structure-gate",
            ),
        ),
    )
    calls = 0

    def handler(requirement, kind, subject):
        nonlocal calls
        del requirement, kind, subject
        calls += 1
        return GateExecutionResult(status=GateEvidenceStatus.PASS)

    registry = GateRegistry()
    registry.register(
        GateDescriptor(
            name="structure-gate",
            version="1",
            level=GateLevel.SOURCE,
            subject_kinds=(EvidenceSubjectKind.PROJECT_REVISION,),
            evaluator_digest=_digest("structure"),
        ),
        handler,
    )
    planner = GatePlanner(
        game_task=task,
        environment_digest=digest,
        evidence=evidence,
        events=events,
        registry=registry,
    )

    first = planner.run_public(
        "structure",
        subject_kind=EvidenceSubjectKind.PROJECT_REVISION,
        subject_id=revision.id,
    )
    second = planner.run_public(
        "structure",
        subject_kind=EvidenceSubjectKind.PROJECT_REVISION,
        subject_id=revision.id,
    )

    assert second.id == first.id
    assert calls == 1
    assert events.events()[-1].payload["cached"] is True


def test_public_gate_reruns_failed_evidence_for_the_same_revision() -> None:
    events = InMemoryEventStore("run")
    evidence = EvidenceGraph(events=events)
    digest = empty_digest()
    revision = evidence.commit_project_revision(
        source_tree_hash=digest,
        diff_hash=digest,
        asset_graph_digest=digest,
    )
    task = GameTaskSpec(
        engine_profile="test",
        entry_points=("Main",),
        target_platforms=("test",),
        requirements=(
            AcceptanceRequirement(
                id="structure",
                dimension=AcceptanceDimension.STRUCTURE,
                enforcement=RequirementEnforcement.REQUIRED,
                evaluator_ref="structure-gate",
            ),
        ),
    )
    statuses = iter((GateEvidenceStatus.FAIL, GateEvidenceStatus.PASS))
    calls = 0

    def handler(requirement, kind, subject):
        nonlocal calls
        del requirement, kind, subject
        calls += 1
        return GateExecutionResult(status=next(statuses))

    registry = GateRegistry()
    registry.register(
        GateDescriptor(
            name="structure-gate",
            version="1",
            level=GateLevel.SOURCE,
            subject_kinds=(EvidenceSubjectKind.PROJECT_REVISION,),
            evaluator_digest=_digest("structure"),
        ),
        handler,
    )
    planner = GatePlanner(
        game_task=task,
        environment_digest=digest,
        evidence=evidence,
        events=events,
        registry=registry,
    )

    failed = planner.run_public(
        "structure",
        subject_kind=EvidenceSubjectKind.PROJECT_REVISION,
        subject_id=revision.id,
    )
    passed = planner.run_public(
        "structure",
        subject_kind=EvidenceSubjectKind.PROJECT_REVISION,
        subject_id=revision.id,
    )

    assert failed.status is GateEvidenceStatus.FAIL
    assert passed.status is GateEvidenceStatus.PASS
    assert passed.id != failed.id
    assert calls == 2


def test_gate_planner_requires_visual_evidence_from_a_replayable_capture() -> None:
    events = InMemoryEventStore("run")
    evidence = EvidenceGraph(events=events)
    digest = empty_digest()
    revision = evidence.commit_project_revision(
        source_tree_hash=digest,
        diff_hash=digest,
        asset_graph_digest=digest,
    )
    task = GameTaskSpec(
        engine_profile="test",
        entry_points=("Main",),
        target_platforms=("test",),
        requirements=(
            AcceptanceRequirement(
                id="visual",
                dimension=AcceptanceDimension.VISUAL,
                enforcement=RequirementEnforcement.REQUIRED,
                evaluator_ref="visual-gate",
                scenario_refs=("show-main",),
            ),
        ),
        playtest_scenarios=(PlaytestScenario(id="show-main", entry_point="Main"),),
    )
    planner = GatePlanner(
        game_task=task,
        environment_digest=digest,
        evidence=evidence,
        events=events,
        registry=GateRegistry(),
    )
    evidence.record_gate(
        requirement_id="visual",
        subject_kind=EvidenceSubjectKind.PROJECT_REVISION,
        subject_id=revision.id,
        status=GateEvidenceStatus.PASS,
        environment_digest=digest,
        evaluator_digest=digest,
    )

    wrong_subject = planner.finish_decision(
        no_unapproved_changes=True,
        budgets_within_limits=True,
        no_uncertain_mutation=True,
        engine_runtime_reconciled=True,
    )

    assert not wrong_subject.allowed
    assert "wrong game subject" in wrong_subject.reasons[0]

    imported = evidence.record_imported_revision(
        project_revision_id=revision.id,
        engine_version="test",
        plugin_digest=digest,
        import_settings_digest=digest,
        environment_digest=digest,
    )
    session = evidence.start_session(
        parent_kind=EvidenceSubjectKind.IMPORTED_REVISION,
        parent_id=imported.id,
        entry_point="Main",
        scenario_id="show-main",
        seed=0,
        fixed_timestep_seconds=1 / 60,
        environment_digest=digest,
    )
    evidence.bind_input_trace(session.id, digest)
    capture = evidence.record_observation(
        subject_kind=EvidenceSubjectKind.PLAY_SESSION,
        subject_id=session.id,
        kind=ObservationKind.SCREENSHOT,
        artifact_hashes=(digest,),
        environment_digest=digest,
        input_trace_hash=digest,
        camera="main",
        width=1280,
        height=720,
    )
    evidence.stop_session(session.id)
    evidence.record_gate(
        requirement_id="visual",
        subject_kind=EvidenceSubjectKind.OBSERVATION,
        subject_id=capture.id,
        status=GateEvidenceStatus.PASS,
        environment_digest=digest,
        evaluator_digest=digest,
    )

    accepted = planner.finish_decision(
        no_unapproved_changes=True,
        budgets_within_limits=True,
        no_uncertain_mutation=True,
        engine_runtime_reconciled=True,
    )

    assert accepted.allowed

    evidence.record_gate(
        requirement_id="visual",
        subject_kind=EvidenceSubjectKind.OBSERVATION,
        subject_id=capture.id,
        status=GateEvidenceStatus.FAIL,
        environment_digest=digest,
        evaluator_digest=digest,
    )
    superseded = planner.finish_decision(
        no_unapproved_changes=True,
        budgets_within_limits=True,
        no_uncertain_mutation=True,
        engine_runtime_reconciled=True,
    )

    assert not superseded.allowed
    assert "latest required evidence did not pass" in superseded.reasons[0]
