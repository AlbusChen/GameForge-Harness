from __future__ import annotations

from collections import Counter
from pathlib import Path

from gameforge.benchmarking.heterogeneous import (
    FailureClass,
    HeterogeneousBenchmarkAdapter,
    TaskCatalog,
)

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "benchmarks" / "unity-heterogeneous-v1.yaml"
PROJECT = ROOT / "unity" / "ArenaTemplate"


def test_catalog_has_twenty_balanced_unique_tasks_with_valid_anchors() -> None:
    catalog = TaskCatalog.from_yaml(CATALOG)

    assert len(catalog.tasks) == 20
    assert len({task.id for task in catalog.tasks}) == 20
    assert Counter(task.failure_class for task in catalog.tasks) == {
        FailureClass.COMPILATION: 5,
        FailureClass.STRUCTURE: 5,
        FailureClass.GAMEPLAY: 5,
        FailureClass.BUILD_LAUNCH: 5,
    }
    catalog.validate_template(PROJECT)


def test_public_normalized_task_does_not_leak_private_fault_fixture() -> None:
    catalog = TaskCatalog.from_yaml(CATALOG)
    task = catalog.task("gameplay-weapon-damage")

    normalized = task.to_normalized_task(
        PROJECT,
        benchmark_name=catalog.name,
        benchmark_version=catalog.version,
    )
    serialized = normalized.model_dump_json()

    assert normalized.metadata["failure_class"] == "gameplay"
    assert normalized.editable_paths == ("Assets/Scripts/WeaponController.cs",)
    assert task.fixture.expected not in serialized
    assert task.fixture.replacement not in serialized


def test_adapter_injects_fixture_only_into_isolated_workspace(tmp_path: Path) -> None:
    catalog = TaskCatalog.from_yaml(CATALOG)
    adapter = HeterogeneousBenchmarkAdapter(catalog, PROJECT)
    workspace = tmp_path / "workspace"
    task = adapter.load_task("gameplay-weapon-damage", workspace)

    adapter.prepare_workspace(task, workspace)

    fixture = catalog.task(task.id).fixture
    mutated = (workspace / fixture.path).read_text(encoding="utf-8")
    original = (PROJECT / fixture.path).read_text(encoding="utf-8")
    assert fixture.replacement in mutated
    assert fixture.expected not in mutated
    assert fixture.expected in original
    assert not (workspace / "Library").exists()
