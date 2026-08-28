#!/usr/bin/env python3
"""Run the frozen GameCraft family15 pilot through the unified minimal-open Harness.

The solver runs in a disposable /private/tmp workspace so the public task cannot
accidentally discover this repository's open rubric files through its parent directory.
The generated project is then preserved under runs/ and evaluated by the pinned official
Linux replay container. The container uses the official stub judge only to materialize
build/replay/frame evidence; subscription-backed rubric scoring is a separate step.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "benchmarks/gamecraft-bench-family15-v1.json"
DEFAULT_VERSION = ROOT
DEFAULT_BENCHMARK = ROOT / "third_party/gamecraft-bench"
DEFAULT_EXPERIMENT = ROOT / "runs/experiments/gamecraft-bench-family15-unified-native-open-run1"
DEFAULT_AGENT = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
DEFAULT_GODOT = ROOT / "third_party/runtimes/godot-4.6.2/Godot.app/Contents/MacOS/Godot"
DEFAULT_LIVE_ROOT = Path("/private/tmp/gamecraft-bench-family15-live")
DEFAULT_IMAGE = "gamecraft-evaluator-arm:a433475"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--version", type=Path, default=DEFAULT_VERSION)
    parser.add_argument("--benchmark-root", type=Path, default=DEFAULT_BENCHMARK)
    parser.add_argument("--experiment", type=Path, default=DEFAULT_EXPERIMENT)
    parser.add_argument("--agent", type=Path, default=DEFAULT_AGENT)
    parser.add_argument("--godot", type=Path, default=DEFAULT_GODOT)
    parser.add_argument("--live-root", type=Path, default=DEFAULT_LIVE_ROOT)
    parser.add_argument("--evaluator-image", default=DEFAULT_IMAGE)
    parser.add_argument("--colima-profile", default="gamecraft-arm")
    parser.add_argument("--max-new-attempts", type=int)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--skip-official-evaluation", action="store_true")
    return parser.parse_args()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _freeze_json(path: Path, payload: object) -> None:
    canonical = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != canonical:
            raise RuntimeError(f"frozen protocol differs: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical, encoding="utf-8")


def _load_runtime(source: Path) -> dict[str, Any]:
    sys.path.insert(0, str(source))
    from gameforge.harness.contracts import (  # type: ignore[import-not-found]
        EngineName,
        EvaluationSpec,
        HarnessProfile,
        NormalizedTask,
        RunBudgets,
        TaskSourceKind,
    )
    from gameforge.harness.engine_registry import ENGINE_REGISTRY  # type: ignore[import-not-found]
    from gameforge.harness.engine_tools import write_engine_receipt  # type: ignore[import-not-found]
    from gameforge.harness.execution_profiles import ExecutionProfile  # type: ignore[import-not-found]
    from gameforge.harness.game_tasks import (  # type: ignore[import-not-found]
        AcceptanceDimension,
        AcceptanceRequirement,
        AssetPolicy,
        EngineEnvironment,
        GameTaskSpec,
        RequirementEnforcement,
        RuntimeProtocol,
    )
    from gameforge.harness.game_workspace import _prompt  # type: ignore[import-not-found]
    from gameforge.harness.minimal_workspace import MinimalWorkspaceHarnessRunner  # type: ignore[import-not-found]
    from gameforge.harness.model_profiles import ModelProfile, ModelProvider  # type: ignore[import-not-found]
    from gameforge.harness.run_factory import create_run_spec_v2  # type: ignore[import-not-found]

    return locals()


def _validate_manifest(manifest: dict[str, Any], benchmark: Path) -> list[dict[str, Any]]:
    tasks = list(manifest["tasks"])
    if len(tasks) != 15 or len({item["family"] for item in tasks}) != 15:
        raise ValueError("family15 must contain exactly one task from every family")
    if [item["ordinal"] for item in tasks] != list(range(1, 16)):
        raise ValueError("family15 ordinals must be contiguous")
    for item in tasks:
        task = benchmark / "tasks" / item["task_id"]
        instruction = task / "instruction.md"
        rubric = task / "tests/rubric.json"
        if _sha256(instruction) != item["instruction_sha256"]:
            raise ValueError(f"instruction hash changed: {item['task_id']}")
        if _sha256(rubric) != item["rubric_sha256"]:
            raise ValueError(f"rubric hash changed: {item['task_id']}")
    return tasks


def _clone_public_resources(source: Path, destination: Path) -> None:
    if destination.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        ["cp", "-cR", str(source), str(destination)],
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"APFS asset clone failed: {completed.stderr.strip()}")


def _prepare_live(
    *, live: Path, benchmark: Path, kenney: Path, screenshot_wrapper: Path
) -> tuple[Path, Path]:
    if live.exists():
        raise RuntimeError(f"live attempt already exists: {live}")
    workspace = live / "workspace"
    resources = live / "public-resources"
    workspace.mkdir(parents=True)
    resources.mkdir()
    _clone_public_resources(kenney, resources / "library")
    (resources / "library-oga").mkdir()
    tools = resources / "tools"
    tools.mkdir()
    shutil.copy2(benchmark / "tools/screenshot.gd", tools / "screenshot.gd")
    shutil.copy2(benchmark / "tools/godot_command_line.md", tools / "godot_command_line.md")
    shutil.copy2(screenshot_wrapper, tools / "screenshot.sh")
    (tools / "screenshot.sh").chmod(0o755)
    # Symlinks are intentionally outside the project snapshot and Godot import scan.
    (workspace / ".gamecraft-public-resources").symlink_to(resources, target_is_directory=True)
    return workspace, resources


def _adapt_instruction(instruction: str, workspace: Path, resources: Path) -> str:
    mapped = instruction
    replacements = (
        ("/workspace/assets/library-oga/", f"{resources / 'library-oga'}/"),
        ("/workspace/assets/library/", f"{resources / 'library'}/"),
        ("/workspace/tools/screenshot.sh", str(resources / "tools/screenshot.sh")),
        ("/workspace/tools/godot_command_line.md", str(resources / "tools/godot_command_line.md")),
        ("/workspace/game/", f"{workspace}/"),
        ("/workspace/game", str(workspace)),
    )
    for old, new in replacements:
        mapped = mapped.replace(old, new)
    note = (
        "ENVIRONMENT PATH MAPPING (transport only): canonical GameCraft paths have been "
        "mechanically mapped to the absolute paths in this disposable workspace. The Kenney "
        "library and public tools are available at the mapped paths. The OpenGameArt directory "
        "is present but empty on this host. Keep the complete Godot project and demo_outputs in "
        "the current workspace.\n\n"
    )
    return note + mapped


def _godot_version(executable: Path) -> str:
    completed = subprocess.run(
        [str(executable), "--version"], capture_output=True, text=True, timeout=30, check=True
    )
    return completed.stdout.strip().splitlines()[0]


def _evaluator_image_metadata(profile: str, image: str) -> dict[str, str]:
    completed = subprocess.run(
        [
            "colima", "ssh", "-p", profile, "--", "sudo", "docker", "image", "inspect",
            image, "--format", "{{.Id}} {{.Architecture}} {{.Os}}",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    values = completed.stdout.strip().split()
    if len(values) != 3 or not values[0].startswith("sha256:"):
        raise RuntimeError(f"unexpected evaluator image metadata: {completed.stdout!r}")
    return {"id": values[0], "architecture": values[1], "os": values[2]}


def _run_solver(
    *, runtime: dict[str, Any], live: Path, workspace: Path, instruction: str,
    task: dict[str, Any], manifest: dict[str, Any], source: Path, agent: Path,
    godot: Path,
) -> dict[str, Any]:
    EngineName = runtime["EngineName"]
    engine = runtime["ENGINE_REGISTRY"][EngineName.GODOT]
    profile = runtime["ModelProfile"](
        provider=runtime["ModelProvider"].CODEX_SUBSCRIPTION,
        model=manifest["solver"]["model"],
        executable=agent,
        reasoning_effort=manifest["solver"]["reasoning_effort"],
        execution_profile=runtime["ExecutionProfile"].NATIVE_OPEN,
        timeout_seconds=3600,
        max_output_tokens=1200,
    )
    normalized = runtime["NormalizedTask"](
        id=f"gamecraft-{task['ordinal']:02d}-{task['task_id']}",
        source=runtime["TaskSourceKind"].BENCHMARK,
        instruction=instruction,
        project_path=workspace,
        acceptance=("complete launchable Godot project with replayable demo traces",),
        editable_paths=(),
        benchmark_name="GameCraft-Bench",
        benchmark_version=manifest["benchmark"]["commit"],
        source_sha256=runtime["NormalizedTask"].source_hash(
            {"task_id": task["task_id"], "instruction_sha256": task["instruction_sha256"]}
        ),
        metadata={
            "workspace_authority": "empty-project-create",
            "canonical_path_mapping": "transport-only",
            "hidden_rubric_visible": False,
        },
    )
    game_task = runtime["GameTaskSpec"](
        engine_profile="godot-open-workspace",
        entry_points=("project.godot",),
        target_platforms=("editor",),
        requirements=(
            runtime["AcceptanceRequirement"](
                id="compilation",
                dimension=runtime["AcceptanceDimension"].BUILD,
                enforcement=runtime["RequirementEnforcement"].REQUIRED,
                evaluator_ref=engine.evaluator_ref,
            ),
        ),
        asset_policy=runtime["AssetPolicy"](
            allow_binary_mutation=True,
            allow_text_scope_expansion=True,
            allow_native_workspace_agent=True,
            maximum_writable_paths=10_000,
        ),
    )
    version = _godot_version(godot)
    now = datetime.now(UTC)
    run_spec = runtime["create_run_spec_v2"](
        normalized,
        profile,
        engine=EngineName.GODOT,
        engine_version=version,
        profile=runtime["HarnessProfile"].PROJECT,
        game_task=game_task,
        engine_environment=runtime["EngineEnvironment"](
            engine_version=version, target_platform="editor"
        ),
        runtime_protocol=runtime["RuntimeProtocol"].MINIMAL_OPEN_V1,
        budgets=runtime["RunBudgets"](
            wall_seconds=int(manifest["solver"]["timeout_seconds"]),
            max_turns=1,
            max_tool_calls=1000,
            max_repairs=0,
            max_cost_usd=100,
        ),
        evaluation=runtime["EvaluationSpec"](
            required_gates=("specification", "compilation"),
            evaluator_version=engine.evaluator_version,
        ),
        now=now,
    ).model_copy(update={"run_id": f"gamecraft-family15-{task['ordinal']:02d}-{task['task_id']}"})
    output_root = live / "harness"
    prompt = runtime["_prompt"](instruction, "godot", "native-open")
    started = time.monotonic()
    with engine.tool_session(
        profile=profile.execution_profile,
        workspace=workspace,
        executable=godot,
        timeout_seconds=1800,
    ) as engine_session:
        model = profile.build_adapter(
            ROOT,
            trusted_read_roots=(source, Path(sys.executable).resolve(strict=True).parents[1]),
            host_runtime_roots=(engine_session.root,) if engine_session.root else (),
        )
        evaluator = engine.evaluator(
            workspace=workspace,
            run_directory=output_root / run_spec.run_id,
            executable=godot,
        )
        run = runtime["MinimalWorkspaceHarnessRunner"](
            model=model,
            evaluator=evaluator,
            prompt=prompt,
            timeout_seconds=float(manifest["solver"]["timeout_seconds"]),
            environment_overrides=engine_session.environment,
        ).execute(run_spec, output_root)
        runtime["write_engine_receipt"](run.directory / "engine-runtime.json", engine_session)
    return {
        "run_directory": str(run.directory),
        "status": run.result.status.value,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "input_tokens": run.result.input_tokens,
        "cached_input_tokens": run.result.cached_input_tokens,
        "output_tokens": run.result.output_tokens,
        "reasoning_output_tokens": run.result.reasoning_output_tokens,
        "tool_calls": run.result.tool_calls,
        "gates": [gate.model_dump(mode="json") for gate in run.result.gates],
    }


def _preserve_live(live: Path, attempt: Path) -> None:
    attempt.mkdir(parents=True, exist_ok=False)
    shutil.copytree(
        live / "workspace",
        attempt / "workspace",
        symlinks=False,
        ignore=lambda _directory, names: {
            name for name in names if name == ".gamecraft-public-resources"
        },
    )
    shutil.copytree(live / "harness", attempt / "harness")


def _run_official_evidence(
    *, workspace: Path, rubric: Path, output: Path, profile: str, image: str
) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=False)
    command = [
        "colima", "ssh", "-p", profile, "--", "sudo", "docker", "run", "--rm",
        "--platform", "linux/arm64",
        "-v", f"{workspace}:/workspace/game",
        "-v", f"{rubric}:/tests/rubric.json:ro",
        "-v", f"{output}:/logs/verifier",
        image,
        "python3", "-m", "gamecraft_bench.verifier",
        "--project", "/workspace/game",
        "--rubric", "/tests/rubric.json",
        "--output", "/logs/verifier",
        "--judge", "stub",
        "--judge-model", "1.0",
    ]
    started = time.monotonic()
    completed = subprocess.run(
        command, capture_output=True, text=True, timeout=2400, check=False
    )
    (output / "container.stdout.log").write_text(completed.stdout, encoding="utf-8")
    (output / "container.stderr.log").write_text(completed.stderr, encoding="utf-8")
    breakdown_path = output / "breakdown.json"
    if not breakdown_path.is_file():
        raise RuntimeError(
            f"official evaluator produced no breakdown (rc={completed.returncode}): "
            f"{completed.stderr[-1000:]}"
        )
    breakdown = json.loads(breakdown_path.read_text(encoding="utf-8"))
    return {
        "container_return_code": completed.returncode,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "build_ok": bool(breakdown["build_ok"]),
        "replayed_demo_count": len(breakdown["demos"]),
        "errors": breakdown["errors"],
        "stub_reward_not_a_score": breakdown["reward"],
    }


def _progress(experiment: Path, total: int) -> None:
    receipts = sorted((experiment / "receipts").glob("*.json"))
    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in receipts]
    _write_json(
        experiment / "progress.json",
        {
            "schema_version": 1,
            "completed": len(payloads),
            "total": total,
            "solver_passes": sum(item.get("solver", {}).get("status") == "PASS" for item in payloads),
            "official_build_passes": sum(
                item.get("official_evidence", {}).get("build_ok") is True for item in payloads
            ),
            "infrastructure_failures": sum("infrastructure_error" in item for item in payloads),
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )


def main() -> int:
    arguments = _arguments()
    if arguments.max_new_attempts is not None and arguments.max_new_attempts < 0:
        raise ValueError("max-new-attempts must be nonnegative")
    manifest_path = arguments.manifest.resolve(strict=True)
    version = arguments.version.resolve(strict=True)
    source_candidate = version / "source"
    source = (source_candidate if source_candidate.is_dir() else version).resolve(strict=True)
    benchmark = arguments.benchmark_root.resolve(strict=True)
    experiment = arguments.experiment.resolve()
    agent = arguments.agent.resolve(strict=True)
    godot = arguments.godot.resolve(strict=True)
    live_root = arguments.live_root.resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    tasks = _validate_manifest(manifest, benchmark)
    metadata_path = version / "VERSION.json"
    if not metadata_path.is_file():
        metadata_path = version / "RELEASE.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    repo_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=benchmark, capture_output=True, text=True, check=True
    ).stdout.strip()
    if repo_commit != manifest["benchmark"]["commit"]:
        raise ValueError(f"benchmark commit differs: {repo_commit}")
    evaluator_image = _evaluator_image_metadata(
        arguments.colima_profile, arguments.evaluator_image
    )
    protocol = {
        "schema_version": 1,
        "manifest": str(manifest_path),
        "manifest_sha256": _sha256(manifest_path),
        "runner": str(Path(__file__).resolve()),
        "runner_sha256": _sha256(Path(__file__).resolve()),
        "harness_version": metadata["version"],
        "harness_tree_sha256": metadata["runtime_tree_sha256"],
        "benchmark_commit": repo_commit,
        "agent": str(agent),
        "agent_sha256": _sha256(agent),
        "godot": str(godot),
        "godot_sha256": _sha256(godot),
        "godot_version": _godot_version(godot),
        "evaluator_image": arguments.evaluator_image,
        "evaluator_image_id": evaluator_image["id"],
        "evaluator_image_architecture": evaluator_image["architecture"],
        "evaluator_image_os": evaluator_image["os"],
        "evaluator_godot_version": "4.6.2.stable.official.71f334935",
        "evaluator_godot_sha256": "34a1ce46e58920ad4db010c0dab5a564b1c096e5a6f8e2a61eb46d802518f501",
        "colima_profile": arguments.colima_profile,
        "solver": manifest["solver"],
        "evaluation": manifest["evaluation"],
        "tasks": tasks,
    }
    experiment.mkdir(parents=True, exist_ok=True)
    _freeze_json(experiment / "protocol-freeze.json", protocol)
    _progress(experiment, len(tasks))
    if arguments.prepare_only:
        print(experiment / "protocol-freeze.json")
        return 0

    runtime = _load_runtime(source)
    completed = {
        int(json.loads(path.read_text(encoding="utf-8"))["ordinal"])
        for path in (experiment / "receipts").glob("*.json")
    }
    pending = [task for task in tasks if int(task["ordinal"]) not in completed]
    if arguments.max_new_attempts is not None:
        pending = pending[: arguments.max_new_attempts]
    kenney = (benchmark / "assets/library").resolve(strict=True)
    screenshot_wrapper = (
        ROOT / "experiments/gamecraft_bench/macos_screenshot.sh"
    ).resolve(strict=True)
    live_root.mkdir(parents=True, exist_ok=True)
    for task in pending:
        ordinal = int(task["ordinal"])
        task_id = str(task["task_id"])
        attempt_id = f"{ordinal:02d}-{task_id}"
        live = live_root / attempt_id
        preserved = experiment / "attempts" / attempt_id
        receipt: dict[str, Any] = {
            "schema_version": 1,
            "ordinal": ordinal,
            "task_id": task_id,
            "family": task["family"],
            "started_at": datetime.now(UTC).isoformat(),
        }
        try:
            workspace, resources = _prepare_live(
                live=live,
                benchmark=benchmark,
                kenney=kenney,
                screenshot_wrapper=screenshot_wrapper,
            )
            raw_instruction = (
                benchmark / "tasks" / task_id / "instruction.md"
            ).read_text(encoding="utf-8")
            instruction = _adapt_instruction(raw_instruction, workspace, resources)
            (live / "adapted-instruction.md").write_text(instruction, encoding="utf-8")
            receipt["solver"] = _run_solver(
                runtime=runtime,
                live=live,
                workspace=workspace,
                instruction=instruction,
                task=task,
                manifest=manifest,
                source=source,
                agent=agent,
                godot=godot,
            )
            _preserve_live(live, preserved)
            shutil.copy2(live / "adapted-instruction.md", preserved / "adapted-instruction.md")
            if not arguments.skip_official_evaluation:
                receipt["official_evidence"] = _run_official_evidence(
                    workspace=(preserved / "workspace").resolve(strict=True),
                    rubric=(benchmark / "tasks" / task_id / "tests/rubric.json").resolve(strict=True),
                    output=preserved / "official-evidence",
                    profile=arguments.colima_profile,
                    image=arguments.evaluator_image,
                )
        except Exception as error:
            receipt["infrastructure_error"] = f"{type(error).__name__}: {error}"
        receipt["ended_at"] = datetime.now(UTC).isoformat()
        _write_json(experiment / "receipts" / f"{ordinal:02d}.json", receipt)
        _progress(experiment, len(tasks))
        status = receipt.get("official_evidence", {}).get(
            "build_ok", receipt.get("solver", {}).get("status", "INFRASTRUCTURE_ERROR")
        )
        print(f"[{ordinal:02d}/15] {task_id}: {status}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
