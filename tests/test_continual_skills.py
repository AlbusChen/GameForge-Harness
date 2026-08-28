from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from gameforge.harness.capabilities import CapabilityEffect, CapabilityRegistry
from gameforge.harness.continual import (
    ContinualHarnessStore,
    KnowledgeScope,
    KnowledgeType,
    ProposalStatus,
    ReplayOutcome,
)
from gameforge.harness.game_tasks import EngineEnvironment
from gameforge.harness.skills import (
    SkillCompatibility,
    SkillError,
    SkillManifest,
    SkillRegistry,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _manifest(*, reviewed: bool = True) -> SkillManifest:
    return SkillManifest(
        name="select_resource",
        version="1",
        description="Select a structured resource deterministically.",
        package_digest=_digest("package"),
        tests_digest=_digest("tests"),
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        effects=(CapabilityEffect.NONE,),
        compatibility=SkillCompatibility(
            engine_family="godot",
            engine_versions=("4.4",),
            target_platforms=("linux",),
            task_archetypes=("visual",),
        ),
        reviewed=reviewed,
    )


def test_only_reviewed_compatible_skills_become_capabilities() -> None:
    skills = SkillRegistry()
    with pytest.raises(SkillError, match="reviewed"):
        skills.install(_manifest(reviewed=False), lambda arguments: {})
    skills.install(_manifest(), lambda arguments: {"selected": True})
    registry = CapabilityRegistry()

    names = skills.register_compatible_capabilities(
        registry,
        engine_family="godot",
        environment=EngineEnvironment(engine_version="4.4", target_platform="linux"),
        task_archetype="visual",
    )

    assert names == ("skill.select_resource",)
    assert registry.get(names[0]).descriptor.digest()


def test_continual_store_requires_replay_before_promotion_and_supports_rollback(
    tmp_path: Path,
) -> None:
    path = tmp_path / "continual.jsonl"
    store = ContinualHarnessStore(path)
    proposal = store.propose(
        entry_type=KnowledgeType.MEMORY,
        scope=KnowledgeScope.PROJECT,
        content="Main is the project entry scene.",
        source_run_id="run-1",
        trigger_event_ids=("run-1:00000001",),
        expected_metric="success_rate",
        validation_corpus="project-replay-v1",
        project_id="project",
    )
    promoted = store.evaluate_and_promote(
        proposal.id,
        lambda item: ReplayOutcome(
            baseline_success_rate=0.5,
            candidate_success_rate=0.75,
            baseline_cost=2,
            candidate_cost=2,
            preservation_passed=True,
            replay_stable=True,
            evaluated_project_ids=("project",),
        ),
    )

    assert promoted.status is ProposalStatus.PROMOTED
    assert store.entries[proposal.entry.id].content.startswith("Main")
    store.rollback(proposal.id, reason="project configuration changed")
    assert proposal.entry.id not in store.entries
    reloaded = ContinualHarnessStore(path)
    assert reloaded.proposals[proposal.id].status is ProposalStatus.ROLLED_BACK


def test_benchmark_mode_rejects_global_promotion(tmp_path: Path) -> None:
    store = ContinualHarnessStore(tmp_path / "store.jsonl")
    proposal = store.propose(
        entry_type=KnowledgeType.PROMPT,
        scope=KnowledgeScope.GLOBAL,
        content="Inspect object identity before reviewing a screenshot.",
        source_run_id="run",
        trigger_event_ids=("run:1",),
        expected_metric="visual_pass_rate",
        validation_corpus="cross-project",
    )
    record = store.evaluate_and_promote(
        proposal.id,
        lambda item: ReplayOutcome(
            baseline_success_rate=0.5,
            candidate_success_rate=0.8,
            baseline_cost=2,
            candidate_cost=2,
            preservation_passed=True,
            replay_stable=True,
            evaluated_project_ids=("a", "b", "c"),
        ),
        benchmark_mode=True,
    )

    assert record.status is ProposalStatus.REJECTED
    assert "benchmark mode" in record.reason
