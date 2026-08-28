from __future__ import annotations

import hashlib
import json
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from gameforge.adapters.llm import AgentCliLanguageModel, ModelError
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
from gameforge.harness.preservation import snapshot_project
from gameforge.harness.project_context import DEFAULT_ASSET_SUFFIXES
from gameforge.harness.project_scope import ProjectAccessScope
from gameforge.harness.shell_environment import login_shell_tool_environment
from gameforge.harness.workspace_program import (
    _allowed_paths,
    _bounded_text,
    _changed_paths,
    _commit_text_paths,
    _copy_public_project,
    _install_host_managed_process_shims,
    _invalid_commit_paths,
    _validated_executable_names,
)

_MAXIMUM_FOCUS_BYTES = 8 * 1024
_MAXIMUM_AGENT_SECONDS = 900


@dataclass(frozen=True)
class WorkspaceAgentExecutor:
    """Let the model use native coding tools in a staged copy, then audit the diff."""

    project: Path
    scratch_root: Path
    project_scope: ProjectAccessScope
    model: AgentCliLanguageModel
    task_instruction: str
    engine_environment: dict[str, object]
    new_text_suffixes: tuple[str, ...] = ()
    host_managed_executables: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        project = self.project.resolve(strict=True)
        if not project.is_dir():
            raise ValueError("workspace-agent project must be a directory")
        if not self.task_instruction.strip():
            raise ValueError("workspace-agent task instruction must be non-empty")
        normalized_suffixes = tuple(sorted(set(self.new_text_suffixes)))
        if any(
            not suffix.startswith(".") or suffix != suffix.lower() for suffix in normalized_suffixes
        ):
            raise ValueError("workspace-agent new-text suffixes must be lowercase extensions")
        host_managed = _validated_executable_names(self.host_managed_executables)
        object.__setattr__(self, "project", project)
        object.__setattr__(self, "new_text_suffixes", normalized_suffixes)
        object.__setattr__(self, "host_managed_executables", host_managed)

    def invoke(self, arguments: Mapping[str, object]) -> dict[str, object]:
        focus = arguments.get("focus", "")
        timeout_seconds = arguments.get("timeout_seconds", 600)
        if not isinstance(focus, str):
            raise ValueError("run_workspace_agent focus must be a string")
        if len(focus.encode("utf-8")) > _MAXIMUM_FOCUS_BYTES:
            raise ValueError("run_workspace_agent focus exceeds 8 KiB")
        if (
            not isinstance(timeout_seconds, int)
            or isinstance(timeout_seconds, bool)
            or not 1 <= timeout_seconds <= _MAXIMUM_AGENT_SECONDS
        ):
            raise ValueError("run_workspace_agent timeout_seconds must be between 1 and 900")
        return self._execute(focus=focus, timeout_seconds=timeout_seconds)

    def _execute(self, *, focus: str, timeout_seconds: int) -> dict[str, object]:
        self.scratch_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="agent-", dir=self.scratch_root
        ) as temporary_directory:
            temporary = Path(temporary_directory).resolve(strict=True)
            staging = temporary / "project"
            _copy_public_project(self.project, staging)
            before = snapshot_project(staging)
            prompt = self._prompt(focus)
            shim_directory = _install_host_managed_process_shims(
                self.scratch_root,
                self.host_managed_executables,
            )
            environment_overrides: dict[str, str] = {}
            if shim_directory is not None:
                exports = {
                    "GAMEFORGE_HOST_MANAGED_EXECUTABLES": ",".join(self.host_managed_executables)
                }
                if "godot" in self.host_managed_executables:
                    exports["GODOT"] = str(shim_directory / "godot")
                environment_overrides = login_shell_tool_environment(
                    shim_directory,
                    exported_variables=exports,
                )
            started = time.monotonic()
            response = None
            error: ModelError | None = None
            try:
                response = self.model.run_workspace_agent(
                    project=staging,
                    prompt=prompt,
                    timeout_seconds=timeout_seconds,
                    environment_overrides=environment_overrides,
                )
            except ModelError as caught:
                error = caught
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
            final_message, final_message_truncated = _bounded_text(
                response.text if response is not None else ""
            )
            common: dict[str, object] = {
                "changed_paths": list(changed_paths),
                "committed_paths": [],
                "elapsed_seconds": elapsed_seconds,
                "declared_timeout_seconds": timeout_seconds,
                "timed_out": error is not None and error.code == "timeout",
                "agent_final_message": final_message,
                "agent_final_message_truncated": final_message_truncated,
                "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                "filesystem": (
                    "native agent tools run in a disposable public-project copy; only the "
                    "Host-authorized diff can be committed"
                ),
                "tool_network": "Codex workspace-write sandbox policy",
                "read_confidentiality": (
                    "not certified by this optional native-agent capability; use the hermetic "
                    "workspace program when host-read isolation is required"
                ),
                "cost_signal": (
                    "timeout: change strategy before rerunning"
                    if error is not None and error.code == "timeout"
                    else "slow: consider a narrower delegated objective"
                    if elapsed_seconds >= min(120, timeout_seconds * 0.75)
                    else "normal"
                ),
                "binary_change_lineage": binary_change_lineage,
                "binary_change_lineage_artifact": (
                    self._write_binary_change_lineage(binary_change_lineage)
                    if binary_change_lineage
                    else None
                ),
            }
            if response is not None:
                audit_artifact = self._write_tool_audit(response.tool_audit)
                common["agent_tool_audit"] = {
                    "artifact": audit_artifact,
                    "event_count": len(response.tool_audit),
                    "content_policy": (
                        "command content and command output omitted; hashes, exit states, and "
                        "redacted file-change paths retained"
                    ),
                }
                common["_harness_usage"] = {
                    "provider": response.provider,
                    "model": response.model,
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                    "cached_input_tokens": response.usage.cached_input_tokens,
                    "reasoning_output_tokens": response.usage.reasoning_output_tokens,
                    "cost_usd": response.usage.cost_usd,
                }
            if error is not None:
                return {
                    "status": "timeout" if error.code == "timeout" else "failed",
                    "reason": str(error),
                    "error_code": error.code,
                    **common,
                }

            allowed = _allowed_paths(self.project_scope.writable_paths)
            undeclared = tuple(path for path in changed_paths if path not in allowed)
            auto_declared = tuple(
                path
                for path in undeclared
                if not (self.project / path).exists()
                and Path(path).suffix.lower() in self.new_text_suffixes
            )
            rejected_undeclared = tuple(path for path in undeclared if path not in auto_declared)
            if rejected_undeclared:
                return {
                    "status": "rejected",
                    "reason": (
                        "workspace agent modified an undeclared existing path or created an "
                        "unsupported output type"
                    ),
                    "undeclared_paths": list(rejected_undeclared),
                    **common,
                }
            invalid = _invalid_commit_paths(staging, changed_paths)
            if invalid:
                return {
                    "status": "rejected",
                    "reason": "workspace agent produced a deletion, symlink, or oversized file",
                    "invalid_paths": list(invalid),
                    **common,
                }

            if auto_declared:
                expanded_count = len(set(self.project_scope.writable_paths) | set(auto_declared))
                if (
                    not self.project_scope.allow_text_scope_expansion
                    or expanded_count > self.project_scope.maximum_writable_paths
                ):
                    return {
                        "status": "rejected",
                        "reason": "workspace agent new-text outputs exceed Host scope policy",
                        "undeclared_paths": list(auto_declared),
                        **common,
                    }
            _commit_text_paths(self.project, staging, changed_paths)
            declaration = (
                self.project_scope.declare_output_manifest(
                    auto_declared,
                    reason="native workspace agent created audited project text outputs",
                )
                if auto_declared
                else None
            )
            return {
                "status": "success",
                **common,
                "committed_paths": list(changed_paths),
                "scope_declaration": declaration,
            }

    def _write_tool_audit(self, records: tuple[dict[str, object], ...]) -> str:
        self.scratch_root.mkdir(parents=True, exist_ok=True)
        for index in range(1, 10_000):
            target = self.scratch_root / f"trajectory-{index:04d}.json"
            try:
                with target.open("x", encoding="utf-8") as stream:
                    json.dump(
                        {"schema_version": 1, "events": records},
                        stream,
                        indent=2,
                        sort_keys=True,
                    )
                    stream.write("\n")
                return target.name
            except FileExistsError:
                continue
        raise RuntimeError("workspace-agent audit artifact namespace is exhausted")

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
        raise RuntimeError("workspace-agent binary-lineage namespace is exhausted")

    def _prompt(self, focus: str) -> str:
        payload = {
            "task": self.task_instruction,
            "optional_delegation_focus": focus or None,
            "engine_environment": self.engine_environment,
            "host_authorized_writable_paths": list(self.project_scope.writable_paths),
            "host_auto_declarable_new_text_suffixes": list(self.new_text_suffixes),
            "host_authorized_binary_paths": [
                path
                for path in self.project_scope.writable_paths
                if Path(path).suffix.lower() in DEFAULT_ASSET_SUFFIXES
            ],
            "host_managed_native_executables": list(self.host_managed_executables),
        }
        return (
            "You are the implementation worker inside a disposable staged copy of the complete "
            "public project. Solve the public request below. You may choose and use native local "
            "coding, shell, engine, image-analysis, and testing tools in any sequence; there is "
            "no required workflow. Inspect the project rather than guessing. Hidden evaluators "
            "are unavailable. Do not read or act on paths outside the current project. Change "
            "only files needed for the request. Existing files outside "
            "host_authorized_writable_paths remain immutable. You may create necessary new "
            "ordinary text files whose suffix appears in "
            "host_auto_declarable_new_text_suffixes; the Host will audit and declare them after "
            "the staged diff passes all bounds. You may repair, transform, or generate binary "
            "assets only at paths explicitly listed in host_authorized_binary_paths; their "
            "before/after hashes and derivation class are retained as lineage. Verify the result "
            "with whatever public checks are useful, then return a concise summary. Native "
            "executables listed in host_managed_native_executables are reserved for the "
            "supervising Host: do not launch them by command name, alias, environment-resolved "
            "path, or absolute path. Do source/static checks and return control; the Host runs "
            "engine import and runtime validation after the audited diff.\n\n"
            + json.dumps(payload, indent=2, sort_keys=True)
        )


def register_workspace_agent_capability(
    registry: CapabilityRegistry,
    executor: WorkspaceAgentExecutor,
) -> None:
    registry.register(
        CapabilityDescriptor(
            name="run_workspace_agent",
            version="workspace-agent-v2",
            purpose=(
                "Optionally delegate sustained multi-step project work to the same model in a "
                "native coding/tool session over a disposable complete public-project copy. The "
                "worker chooses its own exploration, editing, engine, image-analysis, and testing "
                "strategy. The Host then rejects out-of-scope/deleted/symlinked/oversized changes "
                "or transactionally commits the authorized diff. When task policy allows scope "
                "expansion, the Host may audit and declare bounded new engine text files; it "
                "never auto-authorizes changes to existing files. This is an alternative to "
                "single-operation capabilities and full-Python workspace programs, not a "
                "required route. Its native sandbox certifies write containment, not host-read "
                "confidentiality; use run_workspace_program when strict read isolation matters."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "focus": {
                        "type": "string",
                        "maxLength": _MAXIMUM_FOCUS_BYTES,
                        "description": (
                            "Optional objective or context chosen by the supervising model; the "
                            "unchanged public task is always supplied by the Host."
                        ),
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": _MAXIMUM_AGENT_SECONDS,
                    },
                },
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            effects=(CapabilityEffect.PROJECT_READ, CapabilityEffect.PROJECT_WRITE),
            phase=CapabilityPhase.AUTHOR,
            required_permissions=("project:read", "project:write"),
            invalidates=("import", "build", "playtest", "capture", "gate"),
            retry_policy=RetryPolicy(maximum_attempts=1),
            timeout_seconds=_MAXIMUM_AGENT_SECONDS,
            idempotency=Idempotency.NON_IDEMPOTENT,
            concurrency=ConcurrencyMode.SERIAL_PROJECT,
            cost_class=CostClass.JUDGE,
            determinism=Determinism.BEST_EFFORT,
            evidence_outputs=("agent_trajectory", "project_revision"),
            affected_scopes=(
                "disposable complete public-project copy",
                "Host-authorized project diff",
            ),
        ),
        executor.invoke,
    )
