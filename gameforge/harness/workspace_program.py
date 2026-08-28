from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import sysconfig
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from gameforge.harness.capabilities import (
    CapabilityDescriptor,
    CapabilityEffect,
    CapabilityPhase,
    CapabilityRegistry,
    ConcurrencyMode,
    CostClass,
    Determinism,
    Idempotency,
    RetryPolicy,
)
from gameforge.harness.preservation import IGNORED_DIRECTORY_NAMES, snapshot_project
from gameforge.harness.project_context import DEFAULT_ASSET_SUFFIXES
from gameforge.harness.project_scope import ProjectAccessScope
from gameforge.harness.shell_environment import login_shell_tool_environment

_MAXIMUM_SOURCE_BYTES = 512 * 1024
_MAXIMUM_OUTPUT_BYTES = 32 * 1024
_MAXIMUM_COMMIT_BYTES_PER_FILE = 8 * 1024 * 1024
_MAXIMUM_PROGRAM_SECONDS = 180
_PROGRAM_TERMINATION_GRACE_SECONDS = 3
_MAXIMUM_VALIDATORS = 16
_VALIDATOR_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_EXECUTABLE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}$")
_HOST_MANAGED_PROCESS_MARKER = "GAMEFORGE_HOST_MANAGED_PROCESS"


@dataclass(frozen=True)
class _ProgramProcessResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


@dataclass(frozen=True)
class WorkspaceProgramExecutor:
    """Run model-authored Python in a hermetic copy, then commit declared files."""

    project: Path
    scratch_root: Path
    project_scope: ProjectAccessScope
    python_executable: Path = Path(sys.executable)
    sandbox_executable: Path = Path("/usr/bin/sandbox-exec")
    host_managed_executables: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        project = self.project.resolve(strict=True)
        if not project.is_dir():
            raise ValueError("workspace-program project must be a directory")
        python = self.python_executable.resolve(strict=True)
        if not python.is_file():
            raise ValueError("workspace-program Python executable is unavailable")
        sandbox = self.sandbox_executable.resolve(strict=True)
        if not sandbox.is_file():
            raise ValueError("workspace-program requires the macOS sandbox-exec boundary")
        host_managed = _validated_executable_names(self.host_managed_executables)
        object.__setattr__(self, "project", project)
        object.__setattr__(self, "python_executable", python)
        object.__setattr__(self, "sandbox_executable", sandbox)
        object.__setattr__(self, "host_managed_executables", host_managed)

    def invoke(self, arguments: Mapping[str, object]) -> dict[str, object]:
        source = arguments.get("source")
        raw_paths = arguments.get("paths")
        timeout_seconds = arguments.get("timeout_seconds", 120)
        if not isinstance(source, str) or not source.strip():
            raise ValueError("run_workspace_program requires non-empty Python source")
        if len(source.encode("utf-8")) > _MAXIMUM_SOURCE_BYTES:
            raise ValueError("workspace program exceeds the 512 KiB source limit")
        if not isinstance(raw_paths, list) or not all(isinstance(path, str) for path in raw_paths):
            raise ValueError("run_workspace_program paths must be a list of strings")
        if len(raw_paths) > 64 or len(raw_paths) != len(set(raw_paths)):
            raise ValueError("run_workspace_program accepts at most 64 unique paths")
        if (
            not isinstance(timeout_seconds, int)
            or isinstance(timeout_seconds, bool)
            or not 1 <= timeout_seconds <= _MAXIMUM_PROGRAM_SECONDS
        ):
            raise ValueError("workspace program timeout_seconds must be between 1 and 180")

        paths = tuple(_safe_relative(path) for path in raw_paths)
        for path in paths:
            if not self.project_scope.is_writable(path):
                raise ValueError(f"workspace program path is not writable: {path}")

        return self._execute(source=source, paths=paths, timeout_seconds=timeout_seconds)

    def save_validator(self, arguments: Mapping[str, object]) -> dict[str, object]:
        """Persist a model-authored public validator outside the candidate project."""
        name = _validator_name(arguments.get("name"))
        source = arguments.get("source")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("save_workspace_validator requires non-empty Python source")
        if len(source.encode("utf-8")) > _MAXIMUM_SOURCE_BYTES:
            raise ValueError("workspace validator exceeds the 512 KiB source limit")
        try:
            compile(source, f"<workspace-validator:{name}>", "exec")
        except SyntaxError as error:
            raise ValueError(f"workspace validator is not valid Python: {error.msg}") from error
        directory = self._validator_directory()
        existing = tuple(directory.glob("*.py"))
        target = directory / f"{name}.py"
        if not target.exists() and len(existing) >= _MAXIMUM_VALIDATORS:
            raise ValueError(f"at most {_MAXIMUM_VALIDATORS} workspace validators may be saved")
        target.write_text(source, encoding="utf-8")
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        return {
            "status": "success",
            "name": name,
            "source_sha256": digest,
            "saved_validator_count": len(tuple(directory.glob("*.py"))),
            "guidance": (
                "Run this validator after relevant revisions with run_workspace_validator; "
                "compare its structured output rather than treating its assumptions as truth."
            ),
        }

    def list_validators(self, arguments: Mapping[str, object]) -> dict[str, object]:
        if arguments:
            raise ValueError("list_workspace_validators accepts no arguments")
        directory = self._validator_directory()
        validators = []
        for path in sorted(directory.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            validators.append(
                {
                    "name": path.stem,
                    "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                    "source_bytes": len(source.encode("utf-8")),
                    "run_count": len(tuple(directory.glob(f"{path.stem}.run-*.json"))),
                }
            )
        return {"status": "success", "validators": validators}

    def run_validator(self, arguments: Mapping[str, object]) -> dict[str, object]:
        """Rerun a saved validator against a fresh copy of the current public revision."""
        name = _validator_name(arguments.get("name"))
        timeout_seconds = arguments.get("timeout_seconds", 60)
        if (
            not isinstance(timeout_seconds, int)
            or isinstance(timeout_seconds, bool)
            or not 1 <= timeout_seconds <= _MAXIMUM_PROGRAM_SECONDS
        ):
            raise ValueError("workspace validator timeout_seconds must be between 1 and 180")
        directory = self._validator_directory()
        target = directory / f"{name}.py"
        if not target.is_file():
            raise ValueError(f"workspace validator is not saved: {name}")
        source = target.read_text(encoding="utf-8")
        result = self._execute(source=source, paths=(), timeout_seconds=timeout_seconds)
        project_revision_sha256 = _snapshot_digest(snapshot_project(self.project).files)
        previous_paths = sorted(directory.glob(f"{name}.run-*.json"))
        previous = (
            json.loads(previous_paths[-1].read_text(encoding="utf-8")) if previous_paths else None
        )
        result.update(
            {
                "validator_name": name,
                "project_revision_sha256": project_revision_sha256,
                "comparison": {
                    "previous_run": previous is not None,
                    "project_changed": (
                        previous.get("project_revision_sha256") != project_revision_sha256
                        if isinstance(previous, dict)
                        else None
                    ),
                    "status_changed": (
                        previous.get("status") != result.get("status")
                        if isinstance(previous, dict)
                        else None
                    ),
                    "stdout_changed": (
                        previous.get("stdout_sha256")
                        != hashlib.sha256(str(result.get("stdout", "")).encode()).hexdigest()
                        if isinstance(previous, dict)
                        else None
                    ),
                },
            }
        )
        ordinal = len(previous_paths) + 1
        history = {
            "status": result.get("status"),
            "project_revision_sha256": project_revision_sha256,
            "source_sha256": result.get("source_sha256"),
            "stdout_sha256": hashlib.sha256(str(result.get("stdout", "")).encode()).hexdigest(),
            "return_code": result.get("return_code"),
            "timed_out": result.get("timed_out"),
        }
        (directory / f"{name}.run-{ordinal:03d}.json").write_text(
            json.dumps(history, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return result

    def _validator_directory(self) -> Path:
        directory = self.scratch_root / "validators"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _write_binary_change_lineage(self, records: list[dict[str, object]]) -> str:
        self.scratch_root.mkdir(parents=True, exist_ok=True)
        for index in range(1, 10_000):
            target = self.scratch_root / f"binary-lineage-{index:04d}.json"
            try:
                with target.open("x", encoding="utf-8") as stream:
                    json.dump(
                        {"schema_version": 1, "changes": records},
                        stream,
                        indent=2,
                        sort_keys=True,
                    )
                    stream.write("\n")
                return target.name
            except FileExistsError:
                continue
        raise RuntimeError("workspace-program binary-lineage namespace is exhausted")

    def _execute(
        self,
        *,
        source: str,
        paths: tuple[str, ...],
        timeout_seconds: int,
    ) -> dict[str, object]:
        self.scratch_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="cell-", dir=self.scratch_root
        ) as temporary_directory:
            temporary = Path(temporary_directory).resolve(strict=True)
            staging = temporary / "project"
            program_path = temporary / "program.py"
            runtime_scratch = temporary / "scratch"
            runtime_scratch.mkdir()
            shim_directory = _install_host_managed_process_shims(
                runtime_scratch,
                self.host_managed_executables,
            )
            _copy_public_project(self.project, staging)
            program_path.write_text(source, encoding="utf-8")
            before = snapshot_project(staging)
            profile = _sandbox_profile(
                staging=staging,
                temporary=temporary,
                python_executable=self.python_executable,
                host_managed_executables=self.host_managed_executables,
            )
            started = time.monotonic()
            completed = _run_workspace_process(
                [
                    str(self.sandbox_executable),
                    "-p",
                    profile,
                    str(self.python_executable),
                    "-B",
                    str(program_path),
                ],
                cwd=staging,
                env=_program_environment(
                    runtime_scratch,
                    shim_directory=shim_directory,
                    host_managed_executables=self.host_managed_executables,
                ),
                timeout_seconds=timeout_seconds,
            )
            elapsed_seconds = round(time.monotonic() - started, 3)
            after = snapshot_project(staging)
            changed_paths = _changed_paths(before.files, after.files)
            binary_change_lineage = [
                {
                    "path": path,
                    "provenance": (
                        "task-authorized-public-asset-transformation"
                        if path in before.files
                        else "task-authorized-model-generated-asset"
                    ),
                    "sha256_before": before.files.get(path),
                    "sha256_after": after.files.get(path),
                }
                for path in changed_paths
                if Path(path).suffix.lower() in DEFAULT_ASSET_SUFFIXES
            ]
            stdout, stdout_truncated = _bounded_text(completed.stdout)
            stderr, stderr_truncated = _bounded_text(completed.stderr)
            host_managed_process_blocked = _HOST_MANAGED_PROCESS_MARKER in (
                (completed.stdout or "") + (completed.stderr or "")
            )
            common = {
                "return_code": completed.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "stdout_truncated": stdout_truncated,
                "stderr_truncated": stderr_truncated,
                "changed_paths": list(changed_paths),
                "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                "python": self.python_executable.name,
                "network": "denied",
                "filesystem": "staged public project plus private scratch",
                "timed_out": completed.timed_out,
                "elapsed_seconds": elapsed_seconds,
                "declared_timeout_seconds": timeout_seconds,
                "cost_signal": (
                    "timeout: change process strategy before rerunning"
                    if completed.timed_out
                    else "slow: consider a narrower probe"
                    if elapsed_seconds >= min(30, timeout_seconds * 0.75)
                    else "normal"
                ),
                "host_managed_process_blocked": host_managed_process_blocked,
                "guidance": (
                    "Do not retry the blocked engine executable by name, alias, or absolute "
                    "path. Complete source-level work and return control; the Host runs the "
                    "engine validation lifecycle."
                    if host_managed_process_blocked
                    else None
                ),
                "binary_change_lineage": binary_change_lineage,
                "binary_change_lineage_artifact": (
                    self._write_binary_change_lineage(binary_change_lineage)
                    if binary_change_lineage
                    else None
                ),
            }
            if completed.timed_out:
                return {
                    "status": "timeout",
                    "reason": "workspace program exceeded its declared timeout",
                    "committed_paths": [],
                    **common,
                }
            if completed.returncode != 0:
                return {
                    "status": "failed",
                    "committed_paths": [],
                    **common,
                }

            allowed = _allowed_paths(paths)
            undeclared = tuple(path for path in changed_paths if path not in allowed)
            if undeclared:
                return {
                    "status": "rejected",
                    "reason": "program changed undeclared project paths",
                    "undeclared_paths": list(undeclared),
                    "committed_paths": [],
                    **common,
                }
            invalid = _invalid_commit_paths(staging, changed_paths)
            if invalid:
                return {
                    "status": "rejected",
                    "reason": "program produced a deletion, symlink, or oversized file",
                    "invalid_paths": list(invalid),
                    "committed_paths": [],
                    **common,
                }

            _commit_text_paths(self.project, staging, changed_paths)
            return {
                "status": "success",
                "committed_paths": list(changed_paths),
                **common,
            }


def register_workspace_program_capability(
    registry: CapabilityRegistry,
    executor: WorkspaceProgramExecutor,
) -> None:
    descriptor = CapabilityDescriptor(
        name="run_workspace_program",
        version="workspace-program-v3",
        purpose=(
            "Run arbitrary full Python in a hermetic copy of the complete public project. "
            "The program may freely read project text and binary assets, use installed pure "
            "analysis libraries such as Pillow, create private scratch data, and invoke permitted "
            "local processes inside the same filesystem/network sandbox. Engine executables "
            "designated Host-managed are blocked here and run later by the Host lifecycle. Only "
            "ordinary files listed "
            "in paths are transactionally committed; use an empty list for read-only analysis. "
            "Declare additional safe output paths through declare_output_manifest first."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": _MAXIMUM_SOURCE_BYTES,
                    "description": "Complete Python program executed with the project as cwd.",
                },
                "paths": {
                    "type": "array",
                    "minItems": 0,
                    "maxItems": 64,
                    "uniqueItems": True,
                    "items": {"type": "string"},
                    "description": "Declared project-relative ordinary files eligible for commit.",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAXIMUM_PROGRAM_SECONDS,
                },
            },
            "required": ["source", "paths"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        effects=(CapabilityEffect.PROJECT_READ, CapabilityEffect.PROJECT_WRITE),
        phase=CapabilityPhase.AUTHOR,
        required_permissions=("project:read", "project:write"),
        invalidates=("import", "build", "playtest", "capture", "gate"),
        retry_policy=RetryPolicy(maximum_attempts=1),
        timeout_seconds=_MAXIMUM_PROGRAM_SECONDS,
        idempotency=Idempotency.NON_IDEMPOTENT,
        concurrency=ConcurrencyMode.SERIAL_PROJECT,
        cost_class=CostClass.CHEAP,
        determinism=Determinism.BEST_EFFORT,
        evidence_outputs=("program_output", "project_revision"),
        affected_scopes=("hermetic public-project copy", "declared text paths"),
    )
    registry.register(descriptor, executor.invoke)
    registry.register(
        CapabilityDescriptor(
            name="save_workspace_validator",
            version="workspace-validator-v1",
            purpose=(
                "Save or update a named model-authored Python validator as run evidence, outside "
                "the candidate project. This is an optional reusable verification affordance, "
                "not a prescribed test format and not an independent correctness oracle."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": 64},
                    "source": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": _MAXIMUM_SOURCE_BYTES,
                    },
                },
                "required": ["name", "source"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            effects=(CapabilityEffect.ARTIFACT_WRITE,),
            phase=CapabilityPhase.AUTHOR,
            required_permissions=("artifact:write",),
            retry_policy=RetryPolicy(maximum_attempts=1),
            timeout_seconds=10,
            idempotency=Idempotency.NON_IDEMPOTENT,
            concurrency=ConcurrencyMode.SERIAL_PROJECT,
            cost_class=CostClass.CHEAP,
            determinism=Determinism.DETERMINISTIC,
            evidence_outputs=("validator_definition",),
            affected_scopes=("run evidence directory",),
        ),
        executor.save_validator,
    )
    registry.register(
        CapabilityDescriptor(
            name="list_workspace_validators",
            version="workspace-validator-v1",
            purpose="List the model-authored validators saved in this run and their run counts.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            output_schema={"type": "object"},
            effects=(CapabilityEffect.NONE,),
            phase=CapabilityPhase.OBSERVE,
            required_permissions=(),
            retry_policy=RetryPolicy(maximum_attempts=1),
            timeout_seconds=10,
            idempotency=Idempotency.PURE,
            concurrency=ConcurrencyMode.PARALLEL_READ,
            cost_class=CostClass.CHEAP,
            determinism=Determinism.DETERMINISTIC,
            evidence_outputs=("validator_inventory",),
            affected_scopes=("run evidence directory",),
        ),
        executor.list_validators,
    )
    registry.register(
        CapabilityDescriptor(
            name="run_workspace_validator",
            version="workspace-validator-v1",
            purpose=(
                "Run a previously saved model-authored Python validator in a fresh hermetic copy "
                "of the current complete public project. It cannot commit project changes and "
                "reports comparison with the validator's preceding run."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": 64},
                    "timeout_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": _MAXIMUM_PROGRAM_SECONDS,
                    },
                },
                "required": ["name"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            effects=(CapabilityEffect.PROJECT_READ, CapabilityEffect.ARTIFACT_WRITE),
            phase=CapabilityPhase.OBSERVE,
            required_permissions=("project:read", "artifact:write"),
            retry_policy=RetryPolicy(maximum_attempts=1),
            timeout_seconds=_MAXIMUM_PROGRAM_SECONDS,
            idempotency=Idempotency.NON_IDEMPOTENT,
            concurrency=ConcurrencyMode.SERIAL_PROJECT,
            cost_class=CostClass.CHEAP,
            determinism=Determinism.BEST_EFFORT,
            evidence_outputs=("validator_run", "project_revision"),
            affected_scopes=("hermetic public-project copy", "run evidence directory"),
        ),
        executor.run_validator,
    )


def _copy_public_project(source: Path, destination: Path) -> None:
    ignored = set(IGNORED_DIRECTORY_NAMES) | {"artifact-store"}

    def ignore(_directory: str, names: list[str]) -> set[str]:
        return {name for name in names if name in ignored}

    shutil.copytree(source, destination, symlinks=True, ignore=ignore)


def _sandbox_profile(
    *,
    staging: Path,
    temporary: Path,
    python_executable: Path,
    host_managed_executables: tuple[str, ...] = (),
) -> str:
    readable_roots = {
        Path("/Applications"),
        Path("/Library"),
        Path("/System"),
        Path("/bin"),
        Path("/opt/homebrew"),
        Path("/private/var/db/timezone"),
        Path("/sbin"),
        Path("/usr"),
        Path(sys.base_prefix).resolve(),
        Path(sys.prefix).resolve(),
        python_executable.parent,
        staging,
        temporary,
    }
    read_subpaths = "\n".join(
        f'    (subpath "{_scheme_path(path)}")' for path in sorted(readable_roots)
    )
    ancestors = {
        ancestor for path in readable_roots for ancestor in path.resolve(strict=False).parents
    }
    read_ancestors = "\n".join(
        f'    (literal "{_scheme_path(path)}")' for path in sorted(ancestors)
    )
    blocked_paths = _host_managed_executable_paths(host_managed_executables)
    blocked_processes = (
        "(deny process-exec\n"
        + "\n".join(f'    (literal "{_scheme_path(path)}")' for path in blocked_paths)
        + ")\n"
        if blocked_paths
        else ""
    )
    return (
        "(version 1)\n"
        "(deny default)\n"
        "(allow process*)\n"
        "(allow signal (target self))\n"
        "(allow sysctl-read)\n"
        "(allow mach-lookup)\n"
        "(allow ipc-posix*)\n"
        "(allow file-read*\n"
        f"{read_subpaths}\n"
        f"{read_ancestors}\n"
        '    (literal "/dev/null")\n'
        '    (literal "/dev/urandom"))\n'
        "(allow file-write*\n"
        f'    (subpath "{_scheme_path(staging)}")\n'
        f'    (subpath "{_scheme_path(temporary)}")\n'
        '    (literal "/dev/null"))\n'
        f"{blocked_processes}"
        "(deny network*)\n"
    )


def _program_environment(
    scratch: Path,
    *,
    shim_directory: Path | None = None,
    host_managed_executables: tuple[str, ...] = (),
) -> dict[str, str]:
    path = "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    environment = {
        "HOME": str(scratch),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": path,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": sysconfig.get_paths()["purelib"],
        "TMPDIR": str(scratch),
        "GAMEFORGE_SCRATCH": str(scratch),
    }
    if shim_directory is not None:
        exports = {"GAMEFORGE_HOST_MANAGED_EXECUTABLES": ",".join(host_managed_executables)}
        if "godot" in host_managed_executables:
            exports["GODOT"] = str(shim_directory / "godot")
        environment.update(
            login_shell_tool_environment(
                shim_directory,
                exported_variables=exports,
                inherited_path=path,
            )
        )
    return environment


def _validated_executable_names(names: tuple[str, ...]) -> tuple[str, ...]:
    normalized = tuple(dict.fromkeys(names))
    if any(_EXECUTABLE_NAME_PATTERN.fullmatch(name) is None for name in normalized):
        raise ValueError("host-managed executable names must be safe basenames")
    return normalized


def _host_managed_executable_paths(names: tuple[str, ...]) -> tuple[Path, ...]:
    paths: set[Path] = set()
    for name in _validated_executable_names(names):
        discovered = shutil.which(name)
        if discovered is None:
            continue
        path = Path(discovered)
        paths.add(path)
        paths.add(path.resolve(strict=False))
    return tuple(sorted(paths))


def _install_host_managed_process_shims(
    root: Path,
    names: tuple[str, ...],
) -> Path | None:
    if not names:
        return None
    validated = _validated_executable_names(names)
    directory = root / "host-managed-bin"
    directory.mkdir(parents=True, exist_ok=True)
    for name in validated:
        target = directory / name
        target.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' '{_HOST_MANAGED_PROCESS_MARKER}: {name} is executed by the "
            "supervising Host; return control for engine validation.' >&2\n"
            "exit 69\n",
            encoding="utf-8",
        )
        target.chmod(0o755)
    return directory


def _run_workspace_process(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: int,
) -> _ProgramProcessResult:
    """Run a workspace program and terminate its whole descendant group on timeout."""
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=os.name == "posix",
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
        return _ProgramProcessResult(process.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        _terminate_workspace_process(process, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=_PROGRAM_TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            _terminate_workspace_process(process, signal.SIGKILL)
            stdout, stderr = process.communicate()
        return _ProgramProcessResult(
            process.returncode,
            stdout,
            stderr,
            timed_out=True,
        )


def _terminate_workspace_process(
    process: subprocess.Popen[str],
    signal_number: signal.Signals,
) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal_number)
        elif signal_number is signal.SIGTERM:
            process.terminate()
        else:
            process.kill()
    except ProcessLookupError:
        return


def _safe_relative(raw_path: str) -> str:
    relative = Path(raw_path)
    if not raw_path or relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"unsafe project-relative path: {raw_path}")
    return relative.as_posix()


def _validator_name(value: object) -> str:
    if not isinstance(value, str) or _VALIDATOR_NAME_PATTERN.fullmatch(value) is None:
        raise ValueError(
            "workspace validator name must be 1-64 letters, digits, underscores, or hyphens"
        )
    return value


def _snapshot_digest(files: dict[str, str]) -> str:
    encoded = json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _allowed_paths(paths: tuple[str, ...]) -> frozenset[str]:
    allowed = set(paths)
    allowed.update(f"{path}.meta" for path in paths)
    return frozenset(allowed)


def _changed_paths(before: dict[str, str], after: dict[str, str]) -> tuple[str, ...]:
    return tuple(
        sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))
    )


def _invalid_commit_paths(staging: Path, paths: tuple[str, ...]) -> tuple[str, ...]:
    invalid: list[str] = []
    root = staging.resolve(strict=True)
    for path in paths:
        target = staging / path
        if (
            not target.is_file()
            or target.is_symlink()
            or not target.resolve(strict=True).is_relative_to(root)
            or target.stat().st_size > _MAXIMUM_COMMIT_BYTES_PER_FILE
        ):
            invalid.append(path)
            continue
    return tuple(invalid)


def _commit_text_paths(project: Path, staging: Path, paths: tuple[str, ...]) -> None:
    root = project.resolve(strict=True)
    originals: dict[str, bytes | None] = {}
    committed: list[str] = []
    try:
        for path in paths:
            source = staging / path
            destination = project / path
            _validate_destination(root, destination)
            originals[path] = destination.read_bytes() if destination.is_file() else None
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                prefix=f".{destination.name}.",
                dir=destination.parent,
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                stream.write(source.read_bytes())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            committed.append(path)
    except Exception:
        for path in reversed(committed):
            destination = project / path
            original = originals[path]
            if original is None:
                destination.unlink(missing_ok=True)
            else:
                destination.write_bytes(original)
        raise


def _validate_destination(root: Path, destination: Path) -> None:
    relative = destination.relative_to(root)
    current = root
    for part in relative.parts[:-1]:
        current /= part
        if current.is_symlink():
            raise ValueError(f"workspace program destination crosses a symlink: {relative}")
    if destination.is_symlink() or not destination.resolve(strict=False).is_relative_to(root):
        raise ValueError(f"workspace program destination escapes the project: {relative}")


def _bounded_text(value: str) -> tuple[str, bool]:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= _MAXIMUM_OUTPUT_BYTES:
        return value, False
    return encoded[:_MAXIMUM_OUTPUT_BYTES].decode("utf-8", errors="ignore"), True


def _scheme_path(path: Path) -> str:
    return str(path.resolve(strict=False)).replace("\\", "\\\\").replace('"', '\\"')
