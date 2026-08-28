from __future__ import annotations

from datetime import UTC, datetime

from gameforge import __version__
from gameforge.harness.contracts import (
    EngineName,
    EvaluationSpec,
    HarnessProfile,
    NormalizedTask,
    RunBudgets,
    RunSpec,
    RunSpecV2,
)
from gameforge.harness.game_tasks import EngineEnvironment, GameTaskSpec, PluginPin, RuntimeProtocol
from gameforge.harness.model_profiles import ModelProfile


def create_run_spec(
    task: NormalizedTask,
    model: ModelProfile,
    *,
    engine: EngineName,
    engine_version: str,
    profile: HarnessProfile,
    budgets: RunBudgets | None = None,
    evaluation: EvaluationSpec | None = None,
    now: datetime | None = None,
) -> RunSpec:
    timestamp = now or datetime.now(UTC)
    run_id = f"{timestamp.strftime('%Y%m%dT%H%M%SZ')}-{task.id}"
    return RunSpec(
        run_id=run_id,
        created_at=timestamp,
        engine=engine,
        engine_version=engine_version,
        harness_version=__version__,
        profile=profile,
        task=task,
        model=model.to_run_spec(),
        budgets=budgets or RunBudgets(),
        evaluation=evaluation or EvaluationSpec(),
    )


def create_run_spec_v2(
    task: NormalizedTask,
    model: ModelProfile,
    *,
    engine: EngineName,
    engine_version: str,
    profile: HarnessProfile,
    game_task: GameTaskSpec,
    engine_environment: EngineEnvironment,
    runtime_protocol: RuntimeProtocol = RuntimeProtocol.PROGRAMMABLE_V1,
    plugins: tuple[PluginPin, ...] = (),
    capability_digests: dict[str, str] | None = None,
    budgets: RunBudgets | None = None,
    evaluation: EvaluationSpec | None = None,
    now: datetime | None = None,
) -> RunSpecV2:
    timestamp = now or datetime.now(UTC)
    run_id = f"{timestamp.strftime('%Y%m%dT%H%M%SZ')}-{task.id}"
    return RunSpecV2(
        run_id=run_id,
        created_at=timestamp,
        engine=engine,
        engine_version=engine_version,
        harness_version=__version__,
        profile=profile,
        task=task,
        model=model.to_run_spec(),
        budgets=budgets or RunBudgets(),
        evaluation=evaluation or EvaluationSpec(),
        game_task=game_task,
        engine_environment=engine_environment,
        runtime_protocol=runtime_protocol,
        plugins=plugins,
        capability_digests=capability_digests or {},
    )
