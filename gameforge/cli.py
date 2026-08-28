from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import yaml
from pydantic import ValidationError

from gameforge import __version__
from gameforge.adapters.llm import ScriptedControlLanguageModel
from gameforge.adapters.unity import DoctorReport, run_doctor
from gameforge.benchmark import run_benchmark
from gameforge.benchmarking.gamedevbench import (
    run_gamedevbench_direct_api_minimal_harness,
    run_gamedevbench_harness,
    run_gamedevbench_minimal_harness,
    run_gamedevbench_profile_minimal_harness,
)
from gameforge.benchmarking.gamedevbench_batch import (
    compare_gamedevbench_paired_results,
    run_gamedevbench_harness_batch,
)
from gameforge.benchmarking.project_tasks import (
    run_project_task_benchmark,
    run_project_task_control,
)
from gameforge.benchmarking.runner import run_heterogeneous_benchmark
from gameforge.config import (
    DEFAULT_MODEL_PROFILE,
    DEFAULT_SPEC,
    DEFAULT_UNITY_PROJECT,
    EXPECTED_UNITY_VERSION,
    ProjectPaths,
)
from gameforge.harness.acceptance import validate_acceptance
from gameforge.harness.contracts import EngineName, EvaluationSpec, HarnessProfile, RunStatus
from gameforge.harness.game_tasks import (
    EngineEnvironment,
    GameTaskSpec,
    RuntimeProtocol,
)
from gameforge.harness.game_workspace import run_open_game_workspace
from gameforge.harness.model_profiles import ModelProfile
from gameforge.harness.project_execution import execute_isolated_unity_project
from gameforge.harness.project_query import answer_project_question
from gameforge.harness.run_factory import create_run_spec, create_run_spec_v2
from gameforge.harness.task_sources import ChangeRequestTaskSource, CreateBriefTaskSource
from gameforge.harness.unity_open_workspace import run_open_unity_workspace
from gameforge.orchestrator.executor import run_baseline, run_mock
from gameforge.orchestrator.planner import create_deterministic_plan
from gameforge.orchestrator.repair_demo import run_repair_demo
from gameforge.scaffold import initialize_workspace
from gameforge.schemas.game_spec import GameSpec
from gameforge.tooling.contracts import TOOL_CONTRACTS


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gameforge")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    initialize = commands.add_parser(
        "init",
        help="Create a version-matched starter workspace from the installed package.",
    )
    initialize.add_argument("destination", type=Path)

    validate = commands.add_parser("validate", help="Validate a YAML GameSpec without Unity.")
    validate.add_argument("--spec", type=Path, default=DEFAULT_SPEC)

    plan = commands.add_parser("plan", help="Compile a validated GameSpec into a task DAG.")
    plan.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    plan.add_argument("--json", action="store_true")

    doctor = commands.add_parser(
        "doctor", help="Inspect local prerequisites without changing them."
    )
    doctor.add_argument("--project", type=Path, default=DEFAULT_UNITY_PROJECT)
    doctor.add_argument("--json", action="store_true")

    run = commands.add_parser("run", help="Execute the verification workflow.")
    run.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    run.add_argument("--project", type=Path, default=DEFAULT_UNITY_PROJECT)
    run.add_argument(
        "--mock",
        action="store_true",
        help="Validate orchestration only; never invoke Unity or an external model.",
    )

    baseline = commands.add_parser(
        "baseline",
        help=(
            "Run deterministic Unity compile, test, build, and smoke gates "
            "without an external model."
        ),
    )
    baseline.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    baseline.add_argument("--project", type=Path, default=DEFAULT_UNITY_PROJECT)

    repair_demo = commands.add_parser(
        "repair-demo",
        help=(
            "Inject one bounded C# fault, diagnose it from Unity evidence, repair it, "
            "and rerun every gate. Requires a clean Git worktree."
        ),
    )
    repair_demo.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    repair_demo.add_argument("--project", type=Path, default=DEFAULT_UNITY_PROJECT)

    benchmark = commands.add_parser(
        "benchmark",
        help="Run and retain every repetition of the deterministic Unity benchmark.",
    )
    benchmark.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    benchmark.add_argument("--project", type=Path, default=DEFAULT_UNITY_PROJECT)
    benchmark.add_argument("--runs", type=int, default=20)
    benchmark.add_argument(
        "--mode",
        choices=("repair-demo", "baseline"),
        default="repair-demo",
    )

    heterogeneous = commands.add_parser(
        "benchmark-heterogeneous",
        help="Run the versioned heterogeneous Unity task catalog in isolated workspaces.",
    )
    heterogeneous.add_argument(
        "--catalog",
        type=Path,
        default=Path("benchmarks/unity-heterogeneous-v1.yaml"),
    )
    heterogeneous.add_argument("--project", type=Path, default=DEFAULT_UNITY_PROJECT)
    heterogeneous.add_argument("--adapter", choices=("control", "model"), default="control")
    heterogeneous.add_argument("--model-profile", type=Path, default=DEFAULT_MODEL_PROFILE)
    heterogeneous.add_argument("--task", action="append", default=[])

    project_benchmark = commands.add_parser(
        "benchmark-project-control",
        help="Run versioned create/change tasks through the offline scripted control adapter.",
    )
    project_benchmark.add_argument(
        "--catalog",
        type=Path,
        default=Path("benchmarks/project-tasks-v1.yaml"),
    )
    project_benchmark.add_argument("--project", type=Path, default=DEFAULT_UNITY_PROJECT)
    project_benchmark.add_argument("--task", action="append", default=[])

    project_model_benchmark = commands.add_parser(
        "benchmark-project",
        help="Run ordinary create/change tasks through a configured model or scripted control.",
    )
    project_model_benchmark.add_argument(
        "--catalog",
        type=Path,
        default=Path("benchmarks/project-tasks-v1.yaml"),
    )
    project_model_benchmark.add_argument(
        "--project",
        type=Path,
        default=DEFAULT_UNITY_PROJECT,
    )
    project_model_benchmark.add_argument(
        "--adapter",
        choices=("control", "model"),
        default="model",
    )
    project_model_benchmark.add_argument(
        "--model-profile",
        type=Path,
        default=DEFAULT_MODEL_PROFILE,
    )
    project_model_benchmark.add_argument("--task", action="append", default=[])

    gamedevbench = commands.add_parser(
        "benchmark-gamedevbench",
        help=(
            "Run one task from the pinned external GameDevBench checkout through "
            "the augmented typed-tool harness and official evaluator."
        ),
    )
    gamedevbench.add_argument("--benchmark-root", type=Path, required=True)
    gamedevbench.add_argument("--task", required=True)
    gamedevbench.add_argument(
        "--editable-path",
        action="append",
        default=[],
        help=(
            "Approved project-relative text path; repeat as needed. When omitted, derive a "
            "bounded scope from public task files and instructions."
        ),
    )
    gamedevbench.add_argument("--agent-executable", type=Path, required=True)
    gamedevbench.add_argument("--model", required=True)
    gamedevbench.add_argument(
        "--runtime-protocol",
        choices=tuple(protocol.value for protocol in RuntimeProtocol),
        default=RuntimeProtocol.LEGACY_TOOL_V1.value,
    )
    _add_agent_model_options(gamedevbench)

    gamedevbench_direct = commands.add_parser(
        "benchmark-gamedevbench-direct-api",
        help=(
            "Run one pinned GameDevBench task through the minimal host-owned Responses "
            "API workspace loop and the official evaluator."
        ),
    )
    gamedevbench_direct.add_argument("--benchmark-root", type=Path, required=True)
    gamedevbench_direct.add_argument("--task", required=True)
    gamedevbench_direct.add_argument("--model-profile", type=Path, required=True)

    gamedevbench_minimal = commands.add_parser(
        "benchmark-gamedevbench-minimal",
        help=(
            "Run the same minimal-open workspace contract with a codex-subscription or "
            "openai-responses-api model profile."
        ),
    )
    gamedevbench_minimal.add_argument("--benchmark-root", type=Path, required=True)
    gamedevbench_minimal.add_argument("--task", required=True)
    gamedevbench_minimal.add_argument("--model-profile", type=Path, required=True)

    game_open = commands.add_parser(
        "run-game-workspace",
        help=(
            "Run one minimal-open model session in a disposable game project, select or "
            "detect its engine, then evaluate it independently."
        ),
    )
    game_open.add_argument("--project", type=Path, required=True)
    game_open.add_argument("--engine", choices=("auto", "unity", "godot"), default="auto")
    game_request = game_open.add_mutually_exclusive_group(required=True)
    game_request.add_argument("--request")
    game_request.add_argument("--request-file", type=Path)
    game_open.add_argument("--model-profile", type=Path, required=True)
    game_open.add_argument("--task-id", default="game-open-workspace")
    game_open.add_argument("--timeout-seconds", type=int, default=1800)

    unity_open = commands.add_parser(
        "run-unity-workspace",
        help=(
            "Compatibility alias for run-game-workspace --engine unity."
        ),
    )
    unity_open.add_argument("--project", type=Path, required=True)
    unity_request = unity_open.add_mutually_exclusive_group(required=True)
    unity_request.add_argument("--request")
    unity_request.add_argument("--request-file", type=Path)
    unity_open.add_argument("--model-profile", type=Path, required=True)
    unity_open.add_argument("--task-id", default="unity-open-workspace")
    unity_open.add_argument("--timeout-seconds", type=int, default=1800)

    gamedevbench_batch = commands.add_parser(
        "benchmark-gamedevbench-batch",
        help="Run a pre-registered GameDevBench task manifest with resumable progress.",
    )
    gamedevbench_batch.add_argument("--benchmark-root", type=Path, required=True)
    gamedevbench_batch.add_argument(
        "--manifest",
        type=Path,
        default=Path("benchmarks/gamedevbench-pilot20-v1.json"),
    )
    gamedevbench_batch.add_argument("--agent-executable", type=Path, required=True)
    gamedevbench_batch.add_argument("--model", required=True)
    gamedevbench_batch.add_argument(
        "--runtime-protocol",
        choices=tuple(protocol.value for protocol in RuntimeProtocol),
        default=RuntimeProtocol.LEGACY_TOOL_V1.value,
    )
    _add_agent_model_options(gamedevbench_batch)
    gamedevbench_batch.add_argument(
        "--batch-directory",
        type=Path,
        help="Existing or new directory under the project root; reuse it to resume.",
    )
    gamedevbench_batch.add_argument(
        "--retry-infrastructure-from",
        type=Path,
        help=(
            "Seed a new batch from a prior progress file and rerun only eligible failures that "
            "occurred before any model request."
        ),
    )

    gamedevbench_compare = commands.add_parser(
        "benchmark-gamedevbench-compare",
        help="Build paired statistics from official and Harness first-attempt result files.",
    )
    gamedevbench_compare.add_argument("--manifest", type=Path, required=True)
    gamedevbench_compare.add_argument("--official-results", type=Path, required=True)
    gamedevbench_compare.add_argument("--harness-progress", type=Path, required=True)
    gamedevbench_compare.add_argument("--out", type=Path, required=True)

    model_check = commands.add_parser(
        "model-check",
        help="Validate a model profile and report credential presence without making a request.",
    )
    model_check.add_argument("--profile", type=Path, default=DEFAULT_MODEL_PROFILE)
    model_check.add_argument("--json", action="store_true")

    prepare = commands.add_parser(
        "prepare",
        help="Normalize a create/change request into a model- and engine-neutral RunSpec.",
    )
    prepare.add_argument("--source", choices=("create", "change"), required=True)
    source_input = prepare.add_mutually_exclusive_group(required=True)
    source_input.add_argument("--brief", type=Path)
    source_input.add_argument("--request")
    prepare.add_argument("--project", type=Path, default=DEFAULT_UNITY_PROJECT)
    prepare.add_argument("--engine", choices=tuple(EngineName), default=EngineName.UNITY)
    prepare.add_argument("--engine-version", default=EXPECTED_UNITY_VERSION)
    prepare.add_argument("--model-profile", type=Path, default=DEFAULT_MODEL_PROFILE)
    prepare.add_argument(
        "--harness-profile",
        choices=tuple(HarnessProfile),
        default=HarnessProfile.PROJECT,
    )
    prepare.add_argument("--editable-path", action="append", default=[])
    prepare.add_argument("--acceptance", action="append", default=[])
    prepare.add_argument(
        "--runtime-protocol",
        choices=tuple(RuntimeProtocol),
        default=RuntimeProtocol.LEGACY_TOOL_V1,
    )
    prepare.add_argument(
        "--game-task",
        type=Path,
        help="Reviewed JSON/YAML GameTaskSpec; required for programmable-v1 prepare.",
    )
    prepare.add_argument("--target-platform", default="macos-standalone")
    prepare.add_argument("--out", type=Path)

    project_run = commands.add_parser(
        "project-run",
        help=("Run an approved create/change task in an isolated Unity workspace and verify it."),
    )
    project_run.add_argument("--source", choices=("create", "change"), required=True)
    project_input = project_run.add_mutually_exclusive_group(required=True)
    project_input.add_argument("--brief", type=Path)
    project_input.add_argument("--request")
    project_run.add_argument("--project", type=Path, default=DEFAULT_UNITY_PROJECT)
    project_run.add_argument("--model-profile", type=Path, default=DEFAULT_MODEL_PROFILE)
    project_run.add_argument(
        "--runtime-protocol",
        choices=tuple(RuntimeProtocol),
        default=RuntimeProtocol.LEGACY_TOOL_V1,
    )
    project_run.add_argument(
        "--game-task",
        type=Path,
        help=(
            "Optional reviewed JSON/YAML GameTaskSpec for programmable-v1; when omitted, "
            "legacy assertions and native gates are compiled into hidden required evidence."
        ),
    )
    project_run.add_argument("--target-platform", default="macos-standalone")
    project_run.add_argument("--editable-path", action="append", required=True)
    project_run.add_argument(
        "--assert",
        dest="acceptance",
        action="append",
        required=True,
        help=(
            "Independent assertion: file_exists:PATH, file_contains:PATH::TEXT, "
            "file_not_contains:PATH::TEXT, or file_sha256:PATH::HEX."
        ),
    )
    project_run.add_argument(
        "--gate",
        action="append",
        choices=("specification", "compilation", "structure", "gameplay", "build", "smoke"),
        default=[],
        help="Engine Gate to run in addition to task acceptance and preservation.",
    )
    project_run.add_argument(
        "--decision-script",
        type=Path,
        help="Replay an offline JSON decision sequence; this is a control, not an LLM score.",
    )
    project_run.add_argument(
        "--approve-mutations",
        action="store_true",
        help="Explicitly authorize mutation of the listed editable paths in the isolated copy.",
    )

    project_query = commands.add_parser(
        "project-query",
        help="Answer a project question from explicitly approved text files without mutation.",
    )
    project_query.add_argument("--question", required=True)
    project_query.add_argument("--project", type=Path, default=DEFAULT_UNITY_PROJECT)
    project_query.add_argument("--model-profile", type=Path, default=DEFAULT_MODEL_PROFILE)
    project_query.add_argument(
        "--path",
        action="append",
        required=True,
        help="Approved read-only project-relative UTF-8 path; repeat as needed.",
    )

    tools = commands.add_parser("tools", help="List the typed tool contract registry.")
    tools.add_argument("--json", action="store_true")
    return parser


def _add_agent_model_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--reasoning-effort",
        choices=("none", "low", "medium", "high", "xhigh", "max"),
        help="Pin reasoning effort so model comparisons do not inherit a changing default.",
    )
    parser.add_argument("--input-usd-per-million", type=float, default=0.0)
    parser.add_argument("--cached-input-usd-per-million", type=float, default=0.0)
    parser.add_argument("--output-usd-per-million", type=float, default=0.0)


def _root() -> Path:
    return Path.cwd().resolve()


def _print_doctor(report: DoctorReport) -> None:
    print(f"Doctor: {report.overall}")
    for check in report.checks:
        marker = {"PASS": "[PASS]", "WARN": "[WARN]", "FAIL": "[FAIL]"}[check.status]
        print(f"{marker} {check.id}: {check.detail}")


def _resolved(root: Path, path: Path) -> Path:
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _output_under_root(root: Path, path: Path) -> Path:
    output = _resolved(root, path)
    try:
        output.relative_to(root)
    except ValueError as error:
        raise ValueError("RunSpec output must remain under the project root") from error
    return output


def _load_game_task(path: Path) -> GameTaskSpec:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"GameTaskSpec is not a regular file: {path}")
    try:
        if path.suffix.lower() in {".yaml", ".yml"}:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        else:
            payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, yaml.YAMLError) as error:
        raise ValueError(f"cannot read GameTaskSpec: {path}") from error
    return GameTaskSpec.model_validate(payload)


def _execute(arguments: argparse.Namespace) -> int:
    root = _root()
    if arguments.command == "init":
        destination = _resolved(root, arguments.destination)
        copied = initialize_workspace(destination)
        print(f"Workspace initialized: {destination}")
        for path in copied:
            print(f"- {path.name}/")
        print("Next: cd into the workspace and run `gameforge doctor`.")
        return 0

    if arguments.command == "validate":
        spec = GameSpec.from_yaml((root / arguments.spec).resolve())
        print(f"GameSpec valid: {spec.game.id} ({len(spec.acceptance)} acceptance conditions)")
        return 0

    if arguments.command == "plan":
        spec = GameSpec.from_yaml((root / arguments.spec).resolve())
        plan = create_deterministic_plan(spec)
        if arguments.json:
            print(plan.model_dump_json(indent=2))
        else:
            for index, task in enumerate(plan.tasks, start=1):
                dependencies = ", ".join(task.dependencies) or "none"
                print(f"{index}. {task.id} [{task.kind}] depends on: {dependencies}")
        return 0

    if arguments.command == "doctor":
        paths = ProjectPaths.resolve(root, unity_project=arguments.project)
        report = run_doctor(root, paths.unity_project)
        if arguments.json:
            print(report.to_json())
        else:
            _print_doctor(report)
        return 0 if report.overall == "PASS" else 1

    if arguments.command == "run":
        paths = ProjectPaths.resolve(root, arguments.spec, arguments.project)
        if not arguments.mock:
            print(
                "Real execution is not enabled until the Unity baseline and "
                "AgentBridge gates pass. "
                "Use --mock to validate the offline control plane.",
                file=sys.stderr,
            )
            return 2
        result = run_mock(paths.spec, paths.unity_project, paths.runs)
        print("Run completed: MOCK_VALIDATED")
        print("Unity compile/play/build: NOT_RUN")
        print(f"Run directory: {result.run_directory}")
        print(f"Report: {result.run_directory / 'report.html'}")
        return 0

    if arguments.command == "baseline":
        paths = ProjectPaths.resolve(root, arguments.spec, arguments.project)
        result = run_baseline(root, paths.spec, paths.unity_project, paths.runs)
        print(f"Baseline completed: {result.summary['status']}")
        print(f"Run directory: {result.run_directory}")
        print(f"Report: {result.run_directory / 'report.html'}")
        return 0 if result.summary["status"] == "PASS" else 1

    if arguments.command == "repair-demo":
        paths = ProjectPaths.resolve(root, arguments.spec, arguments.project)
        result = run_repair_demo(root, paths.spec, paths.unity_project, paths.runs)
        print(f"Repair demo completed: {result.summary['status']}")
        print(f"Repair attempts: {result.summary['repair_attempts']}")
        print(f"Run directory: {result.run_directory}")
        print(f"Report: {result.run_directory / 'report.html'}")
        return 0 if result.summary["status"] == "PASS" else 1

    if arguments.command == "benchmark":
        paths = ProjectPaths.resolve(root, arguments.spec, arguments.project)
        result = run_benchmark(
            root,
            paths.spec,
            paths.unity_project,
            paths.runs,
            repetitions=arguments.runs,
            mode=arguments.mode,
        )
        print(f"Benchmark completed: {result.summary['runs_completed']} runs")
        print(f"End-to-end success rate: {result.summary['end_to_end_success_rate']}")
        print(f"Benchmark directory: {result.directory}")
        print(f"Report: {result.directory / 'report.html'}")
        return 0 if result.summary["end_to_end_success_rate"] == 1.0 else 1

    if arguments.command == "benchmark-heterogeneous":
        profile = ModelProfile.from_yaml(_resolved(root, arguments.model_profile))
        result = run_heterogeneous_benchmark(
            root=root,
            catalog_path=_resolved(root, arguments.catalog),
            template_project=_resolved(root, arguments.project),
            model_profile=profile,
            adapter_mode=arguments.adapter,
            task_ids=tuple(arguments.task),
        )
        print(f"Heterogeneous benchmark completed: {result.summary['tasks_completed']} tasks")
        print(f"End-to-end success rate: {result.summary['end_to_end_success_rate']}")
        print(f"Control result: {result.summary['control_result']}")
        print(f"Benchmark directory: {result.directory}")
        print(f"Report: {result.directory / 'report.html'}")
        return 0 if result.summary["end_to_end_success_rate"] == 1.0 else 1

    if arguments.command == "model-check":
        profile_path = _resolved(root, arguments.profile)
        profile = ModelProfile.from_yaml(profile_path)
        credential_configured = profile.credential_is_configured(root)
        isolation_runner_configured = profile.isolation_runner_is_configured(root)
        payload = {
            "status": (
                "PASS"
                if credential_configured and isolation_runner_configured
                else "MISSING_CREDENTIAL"
                if not credential_configured
                else "MISSING_ISOLATION_RUNNER"
            ),
            "provider": profile.provider.value,
            "model": profile.model,
            "api_key_env": profile.api_key_env,
            "credential_configured": credential_configured,
            "execution_profile": profile.execution_profile.value,
            "isolation_runner_configured": isolation_runner_configured,
            "profile_sha256": profile.fingerprint(),
        }
        if arguments.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"Model profile: {payload['status']}")
            print(f"Provider: {profile.provider.value}")
            print(f"Model: {profile.model}")
            print(f"Execution profile: {profile.execution_profile.value}")
            if profile.api_key_env:
                print(f"Credential environment: {profile.api_key_env}")
            if not isolation_runner_configured:
                print("Isolation runner: missing or not executable")
        return 0 if payload["status"] == "PASS" else 1

    if arguments.command == "benchmark-gamedevbench":
        protocol = RuntimeProtocol(arguments.runtime_protocol)
        common = {
            "root": root,
            "benchmark_root": _resolved(root, arguments.benchmark_root),
            "task_id": arguments.task,
            "agent_executable": _resolved(root, arguments.agent_executable),
            "model_name": arguments.model,
            "reasoning_effort": arguments.reasoning_effort,
            "input_usd_per_million": arguments.input_usd_per_million,
            "cached_input_usd_per_million": arguments.cached_input_usd_per_million,
            "output_usd_per_million": arguments.output_usd_per_million,
        }
        if protocol is RuntimeProtocol.MINIMAL_OPEN_V1:
            if arguments.editable_path:
                raise ValueError("minimal-open-v1 always exposes the complete public project")
            run = run_gamedevbench_minimal_harness(**common)
        else:
            run = run_gamedevbench_harness(
                **common,
                editable_paths=tuple(arguments.editable_path),
                runtime_protocol=protocol,
            )
        result = run.harness_run.result
        print(f"GameDevBench harness run completed: {result.status}")
        print(f"Official evaluator: {next(g.status for g in result.gates if g.gate == 'official')}")
        print(f"Tokens: {result.input_tokens} input, {result.output_tokens} output")
        print(f"Run directory: {run.directory}")
        return 0 if result.status is RunStatus.PASS else 1

    if arguments.command == "benchmark-gamedevbench-direct-api":
        model_profile = ModelProfile.from_yaml(_resolved(root, arguments.model_profile))
        run = run_gamedevbench_direct_api_minimal_harness(
            root=root,
            benchmark_root=_resolved(root, arguments.benchmark_root),
            task_id=arguments.task,
            model_profile=model_profile,
        )
        result = run.harness_run.result
        print(f"GameDevBench Direct API run completed: {result.status}")
        print(f"Official evaluator: {next(g.status for g in result.gates if g.gate == 'official')}")
        print(f"Tokens: {result.input_tokens} input, {result.output_tokens} output")
        print(f"Run directory: {run.directory}")
        return 0 if result.status is RunStatus.PASS else 1

    if arguments.command == "benchmark-gamedevbench-minimal":
        model_profile = ModelProfile.from_yaml(_resolved(root, arguments.model_profile))
        run = run_gamedevbench_profile_minimal_harness(
            root=root,
            benchmark_root=_resolved(root, arguments.benchmark_root),
            task_id=arguments.task,
            model_profile=model_profile,
        )
        result = run.harness_run.result
        print(f"GameDevBench minimal workspace run completed: {result.status}")
        print(f"Solver backend: {model_profile.provider.value}")
        print(f"Execution profile: {model_profile.execution_profile.value}")
        print(f"Official evaluator: {next(g.status for g in result.gates if g.gate == 'official')}")
        print(f"Tokens: {result.input_tokens} input, {result.output_tokens} output")
        print(f"Run directory: {run.directory}")
        return 0 if result.status is RunStatus.PASS else 1

    if arguments.command in {"run-game-workspace", "run-unity-workspace"}:
        model_profile = ModelProfile.from_yaml(_resolved(root, arguments.model_profile))
        request = arguments.request
        if arguments.request_file is not None:
            request_path = _resolved(root, arguments.request_file)
            if not request_path.is_file() or request_path.is_symlink():
                raise ValueError(f"request file is not a regular file: {request_path}")
            request = request_path.read_text(encoding="utf-8")
        assert isinstance(request, str)
        if arguments.command == "run-game-workspace":
            run = run_open_game_workspace(
                root=root,
                project=_resolved(root, arguments.project),
                request=request,
                model_profile=model_profile,
                engine=arguments.engine,
                task_id=arguments.task_id,
                timeout_seconds=arguments.timeout_seconds,
            )
        else:
            run = run_open_unity_workspace(
                root=root,
                project=_resolved(root, arguments.project),
                request=request,
                model_profile=model_profile,
                task_id=arguments.task_id,
                timeout_seconds=arguments.timeout_seconds,
            )
        result = run.harness_run.result
        print(f"Game workspace run completed: {result.status}")
        print(f"Engine: {run.engine}")
        print(f"Solver backend: {model_profile.provider.value}")
        print(f"Execution profile: {model_profile.execution_profile.value}")
        print(f"Disposable workspace: {run.workspace}")
        print(f"Run directory: {run.directory}")
        return 0 if result.status is RunStatus.PASS else 1

    if arguments.command == "benchmark-gamedevbench-batch":
        batch_directory = (
            _output_under_root(root, arguments.batch_directory)
            if arguments.batch_directory
            else None
        )
        run = run_gamedevbench_harness_batch(
            root=root,
            benchmark_root=_resolved(root, arguments.benchmark_root),
            manifest_path=_resolved(root, arguments.manifest),
            agent_executable=_resolved(root, arguments.agent_executable),
            model_name=arguments.model,
            reasoning_effort=arguments.reasoning_effort,
            input_usd_per_million=arguments.input_usd_per_million,
            cached_input_usd_per_million=arguments.cached_input_usd_per_million,
            output_usd_per_million=arguments.output_usd_per_million,
            batch_directory=batch_directory,
            retry_infrastructure_from=(
                _resolved(root, arguments.retry_infrastructure_from)
                if arguments.retry_infrastructure_from
                else None
            ),
            runtime_protocol=RuntimeProtocol(arguments.runtime_protocol),
        )
        print(f"GameDevBench batch completed: {run.summary['tasks_completed']} tasks")
        print(f"Pass rate: {run.summary['pass_rate']}")
        print(f"Batch directory: {run.directory}")
        return 0 if run.summary["tasks_completed"] == run.summary["tasks_total"] else 1

    if arguments.command == "benchmark-gamedevbench-compare":
        comparison = compare_gamedevbench_paired_results(
            manifest_path=_resolved(root, arguments.manifest),
            official_results_path=_resolved(root, arguments.official_results),
            harness_progress_path=_resolved(root, arguments.harness_progress),
            output_path=_output_under_root(root, arguments.out),
        )
        print(f"Paired tasks: {comparison['paired_tasks_evaluable']}")
        print(f"Official pass rate: {comparison['official_pass_rate']}")
        print(f"Harness pass rate: {comparison['harness_pass_rate']}")
        print(f"Comparison: {_output_under_root(root, arguments.out)}")
        return 0

    if arguments.command == "benchmark-project-control":
        result = run_project_task_control(
            root=root,
            catalog_path=_resolved(root, arguments.catalog),
            template_project=_resolved(root, arguments.project),
            task_ids=tuple(arguments.task),
        )
        print(f"Project control benchmark completed: {result.summary['tasks_completed']} tasks")
        print(f"End-to-end success rate: {result.summary['end_to_end_success_rate']}")
        print("Result type: deterministic scripted control (not an LLM score)")
        print(f"Benchmark directory: {result.directory}")
        print(f"Report: {result.directory / 'report.html'}")
        return 0 if result.summary["end_to_end_success_rate"] == 1.0 else 1

    if arguments.command == "benchmark-project":
        profile = (
            ModelProfile.from_yaml(_resolved(root, arguments.model_profile))
            if arguments.adapter == "model"
            else None
        )
        result = run_project_task_benchmark(
            root=root,
            catalog_path=_resolved(root, arguments.catalog),
            template_project=_resolved(root, arguments.project),
            adapter_mode=arguments.adapter,
            model_profile=profile,
            task_ids=tuple(arguments.task),
        )
        print(f"Project benchmark completed: {result.summary['tasks_completed']} tasks")
        print(f"Adapter mode: {result.summary['adapter_mode']}")
        print(f"End-to-end success rate: {result.summary['end_to_end_success_rate']}")
        print(f"Benchmark directory: {result.directory}")
        print(f"Report: {result.directory / 'report.html'}")
        return 0 if result.summary["end_to_end_success_rate"] == 1.0 else 1

    if arguments.command == "prepare":
        model_profile = ModelProfile.from_yaml(_resolved(root, arguments.model_profile))
        project_path = _resolved(root, arguments.project)
        acceptance = tuple(arguments.acceptance)
        editable_paths = tuple(arguments.editable_path)
        if arguments.source == "create":
            if arguments.brief is None or arguments.request is not None:
                raise ValueError("create source requires --brief and does not accept --request")
            task = CreateBriefTaskSource(
                _resolved(root, arguments.brief),
                acceptance=acceptance,
                editable_paths=editable_paths,
            ).load(project_path)
        else:
            if arguments.request is None or arguments.brief is not None:
                raise ValueError("change source requires --request and does not accept --brief")
            task = ChangeRequestTaskSource(
                arguments.request,
                acceptance=acceptance,
                editable_paths=editable_paths,
            ).load(project_path)
        protocol = RuntimeProtocol(arguments.runtime_protocol)
        if protocol is RuntimeProtocol.PROGRAMMABLE_V1:
            if arguments.game_task is None:
                raise ValueError("programmable-v1 prepare requires --game-task")
            game_task = _load_game_task(_resolved(root, arguments.game_task))
            run_spec = create_run_spec_v2(
                task,
                model_profile,
                engine=EngineName(arguments.engine),
                engine_version=arguments.engine_version,
                profile=HarnessProfile(arguments.harness_profile),
                game_task=game_task,
                engine_environment=EngineEnvironment(
                    engine_version=arguments.engine_version,
                    target_platform=arguments.target_platform,
                ),
            )
        else:
            if arguments.game_task is not None:
                raise ValueError("--game-task is only valid with programmable-v1")
            run_spec = create_run_spec(
                task,
                model_profile,
                engine=EngineName(arguments.engine),
                engine_version=arguments.engine_version,
                profile=HarnessProfile(arguments.harness_profile),
            )
        serialized = run_spec.model_dump_json(indent=2)
        if arguments.out:
            output = _output_under_root(root, arguments.out)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(serialized + "\n", encoding="utf-8")
            print(f"RunSpec written: {output}")
        else:
            print(serialized)
        return 0

    if arguments.command == "project-query":
        profile_path = _resolved(root, arguments.model_profile)
        model_profile = ModelProfile.from_yaml(profile_path)
        run = answer_project_question(
            root=root,
            project=_resolved(root, arguments.project),
            question=arguments.question,
            approved_paths=tuple(arguments.path),
            model=model_profile.build_adapter(root),
        )
        print(run.result.answer)
        for citation in run.result.citations:
            print(f"Evidence: {citation.path}:{citation.line_start}-{citation.line_end}")
        print(f"Project unchanged: {str(run.result.project_unchanged).lower()}")
        print(f"Run directory: {run.directory}")
        return 0

    if arguments.command == "project-run":
        if not arguments.approve_mutations:
            raise ValueError(
                "project-run requires --approve-mutations after reviewing every --editable-path"
            )
        acceptance = tuple(arguments.acceptance)
        validate_acceptance(acceptance)
        editable_paths = tuple(arguments.editable_path)
        profile_path = _resolved(root, arguments.model_profile)
        model_profile = ModelProfile.from_yaml(profile_path)
        project_path = _resolved(root, arguments.project)
        if arguments.source == "create":
            if arguments.brief is None or arguments.request is not None:
                raise ValueError("create source requires --brief and does not accept --request")
            task = CreateBriefTaskSource(
                _resolved(root, arguments.brief),
                acceptance=acceptance,
                editable_paths=editable_paths,
            ).load(project_path)
        else:
            if arguments.request is None or arguments.brief is not None:
                raise ValueError("change source requires --request and does not accept --brief")
            task = ChangeRequestTaskSource(
                arguments.request,
                acceptance=acceptance,
                editable_paths=editable_paths,
            ).load(project_path)

        engine_gates = tuple(arguments.gate) or (
            "specification",
            "compilation",
            "structure",
            "gameplay",
            "build",
            "smoke",
        )
        required_gates = tuple(
            dict.fromkeys((*engine_gates[:1], "task_acceptance", *engine_gates[1:], "preservation"))
        )
        evaluation = EvaluationSpec(required_gates=required_gates)
        protocol = RuntimeProtocol(arguments.runtime_protocol)
        if protocol is RuntimeProtocol.PROGRAMMABLE_V1:
            if arguments.game_task is None:
                raise ValueError("programmable-v1 project-run requires --game-task")
            game_task = _load_game_task(_resolved(root, arguments.game_task))
            run_spec = create_run_spec_v2(
                task,
                model_profile,
                engine=EngineName.UNITY,
                engine_version=EXPECTED_UNITY_VERSION,
                profile=HarnessProfile.PROJECT,
                game_task=game_task,
                engine_environment=EngineEnvironment(
                    engine_version=EXPECTED_UNITY_VERSION,
                    target_platform=arguments.target_platform,
                ),
                evaluation=evaluation,
            )
        else:
            if arguments.game_task is not None:
                raise ValueError("--game-task is only valid with programmable-v1")
            run_spec = create_run_spec(
                task,
                model_profile,
                engine=EngineName.UNITY,
                engine_version=EXPECTED_UNITY_VERSION,
                profile=HarnessProfile.PROJECT,
                evaluation=evaluation,
            )
        if arguments.decision_script:
            script_path = _resolved(root, arguments.decision_script)
            model = ScriptedControlLanguageModel.from_json(script_path)
            digest = hashlib.sha256(script_path.read_bytes()).hexdigest()
            scripted_model = run_spec.model.model_copy(
                update={
                    "provider": "scripted-control",
                    "model": script_path.stem,
                    "parameters": {
                        "deterministic_replay": True,
                        "decision_script_sha256": digest,
                    },
                    "profile_sha256": digest,
                }
            )
            run_spec = run_spec.model_copy(update={"model": scripted_model})
        else:
            model = model_profile.build_adapter(root)
        execution = execute_isolated_unity_project(root=root, run_spec=run_spec, model=model)
        result = execution.harness_run.result
        print(f"Project run completed: {result.status}")
        print(f"Isolated workspace: {execution.workspace}")
        print(f"Run directory: {execution.harness_run.directory}")
        print(f"Result: {execution.harness_run.directory / 'result.json'}")
        return 0 if result.status is RunStatus.PASS else 1

    if arguments.command == "tools":
        if arguments.json:
            print(
                json.dumps(
                    {
                        name: contract.model_dump(mode="json")
                        for name, contract in sorted(TOOL_CONTRACTS.items())
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            for name, contract in sorted(TOOL_CONTRACTS.items()):
                print(
                    f"{name}: {contract.implementation.value}; "
                    f"mutates={str(contract.mutates_project).lower()}; "
                    f"retryable={str(contract.retryable).lower()}"
                )
        return 0

    raise AssertionError(f"unhandled command: {arguments.command}")


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        return _execute(arguments)
    except (ValueError, ValidationError) as error:
        print(json.dumps({"error": "validation_failed", "detail": str(error)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
