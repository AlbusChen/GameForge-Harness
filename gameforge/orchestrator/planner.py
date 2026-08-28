from __future__ import annotations

from gameforge.schemas.game_spec import GameSpec
from gameforge.schemas.task import Task, TaskDAG, TaskKind


def create_deterministic_plan(spec: GameSpec) -> TaskDAG:
    acceptance_ids = [condition.id for condition in spec.acceptance]
    return TaskDAG(
        tasks=[
            Task(
                id="inspect_project",
                kind=TaskKind.INSPECT,
                description="Inspect Unity version, project structure, scene, and compile status.",
            ),
            Task(
                id="implement_arena",
                kind=TaskKind.IMPLEMENT,
                description="Create the deterministic top-down arena from the validated GameSpec.",
                dependencies=["inspect_project"],
                acceptance_ids=acceptance_ids,
            ),
            Task(
                id="compile_scripts",
                kind=TaskKind.COMPILE,
                description="Compile C# scripts and reject Console errors.",
                dependencies=["implement_arena"],
            ),
            Task(
                id="verify_structure",
                kind=TaskKind.STRUCTURE_TEST,
                description="Verify required objects, components, UI, and serialized references.",
                dependencies=["compile_scripts"],
            ),
            Task(
                id="verify_gameplay",
                kind=TaskKind.PLAY_TEST,
                description="Run deterministic Play Mode acceptance commands and capture state.",
                dependencies=["verify_structure"],
                acceptance_ids=acceptance_ids,
            ),
            Task(
                id="build_macos",
                kind=TaskKind.BUILD,
                description="Build the macOS standalone player from a clean verified state.",
                dependencies=["verify_gameplay"],
            ),
            Task(
                id="smoke_test_build",
                kind=TaskKind.BUILD,
                description="Launch the built app and verify it reaches the arena scene.",
                dependencies=["build_macos"],
            ),
            Task(
                id="write_report",
                kind=TaskKind.REPORT,
                description="Write trace, diff, test, build, timing, and cost evidence.",
                dependencies=["smoke_test_build"],
            ),
        ]
    )
