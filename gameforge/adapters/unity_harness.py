from __future__ import annotations

import difflib
import hashlib
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from xml.etree import ElementTree

from gameforge.adapters.unity import UnityBatchGateway, UnityToolError
from gameforge.harness.contracts import GateOutcome, GateStatus, RunSpec
from gameforge.orchestrator.policy import PolicyViolation
from gameforge.orchestrator.repair_loop import apply_text_replacement
from gameforge.schemas.diagnostic import TextReplacement

_PUBLIC_TEXT_SUFFIXES = frozenset(
    {
        ".asmdef",
        ".cginc",
        ".cs",
        ".hlsl",
        ".json",
        ".md",
        ".shader",
        ".txt",
        ".uss",
        ".uxml",
        ".yaml",
        ".yml",
    }
)
_TEXT_PAGE_MAXIMUM_BYTES = 64 * 1024
_TEXT_BATCH_MAXIMUM_FILES = 16
_TEXT_BATCH_MAXIMUM_BYTES = 256 * 1024
_PROJECT_DIFF_MAXIMUM_BYTES = 128 * 1024


def _safe_relative_path(raw_path: str) -> Path:
    relative = Path(raw_path)
    if not raw_path or relative.is_absolute() or ".." in relative.parts:
        raise PolicyViolation(f"path must be project-relative and cannot traverse: {raw_path}")
    return relative


def _unity_result(result: object) -> dict[str, object]:
    payload = asdict(result)  # type: ignore[arg-type]
    return {
        "status": "success",
        "tool": payload["tool"],
        "return_code": payload["return_code"],
        "log_path": str(payload["log_path"]),
        "artifacts": [str(path) for path in payload["artifacts"]],
    }


@dataclass(frozen=True)
class UnityHarnessEngineAdapter:
    gateway: UnityBatchGateway
    editable_paths: tuple[str, ...] = ()
    name: str = "unity"
    version: str = "native-v1"
    _baseline_editable_text: tuple[tuple[str, str | None], ...] = field(
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        normalized = tuple(_safe_relative_path(path).as_posix() for path in self.editable_paths)
        if len(normalized) != len(set(normalized)):
            raise ValueError("editable paths must be unique")
        object.__setattr__(self, "editable_paths", normalized)
        baseline: list[tuple[str, str | None]] = []
        for path in normalized:
            target = self.gateway.project / path
            if target.is_file() and not target.is_symlink():
                baseline.append((path, target.read_text(encoding="utf-8")))
            else:
                baseline.append((path, None))
        object.__setattr__(self, "_baseline_editable_text", tuple(baseline))

    def available_tools(self) -> tuple[str, ...]:
        tools = [
            "health_check",
            "inspect_project",
            "read_text_file",
            "read_text_files",
            "review_project_changes",
            "wait_for_compilation",
            "run_edit_mode_tests",
            "run_play_mode_tests",
            "build_player",
            "launch_build_smoke_test",
        ]
        if self.editable_paths:
            tools.extend(("apply_code_patch", "create_script"))
        return tuple(tools)

    def automatic_validation_tool(self) -> str | None:
        return "wait_for_compilation" if self.editable_paths else None

    def completion_feedback_tools(self) -> tuple[str, ...]:
        return ()

    def workspace_text_suffixes(self) -> tuple[str, ...]:
        return tuple(sorted(_PUBLIC_TEXT_SUFFIXES | {".asset", ".prefab", ".unity"}))

    def invoke(self, tool_name: str, arguments: Mapping[str, object]) -> object:
        if tool_name == "health_check":
            return _unity_result(self.gateway.health_check())
        if tool_name == "inspect_project":
            result = self.gateway.health_check()
            return {
                **_unity_result(result),
                "project": {
                    "name": self.gateway.project.name,
                    "path": str(self.gateway.project),
                    "editable_paths": list(self.editable_paths),
                },
                "compile_errors": 0,
            }
        if tool_name == "read_text_file":
            return self._read_text_file(arguments)
        if tool_name == "read_text_files":
            return self._read_text_files(arguments)
        if tool_name == "review_project_changes":
            return self._review_project_changes()
        if tool_name == "wait_for_compilation":
            return _unity_result(self.gateway.health_check())
        if tool_name == "run_edit_mode_tests":
            return _unity_result(self.gateway.run_tests("EditMode"))
        if tool_name == "run_play_mode_tests":
            return _unity_result(self.gateway.run_tests("PlayMode"))
        if tool_name == "build_player":
            return _unity_result(self.gateway.build_macos())
        if tool_name == "launch_build_smoke_test":
            return _unity_result(self.gateway.launch_build_smoke_test())
        if tool_name == "apply_code_patch":
            return self._apply_code_patch(arguments)
        if tool_name == "create_script":
            return self._create_script(arguments)
        raise ValueError(f"unsupported Unity harness tool: {tool_name}")

    def _public_text_path(self, raw_path: object) -> tuple[Path, str]:
        if not isinstance(raw_path, str):
            raise ValueError("public text path must be a string")
        relative = _safe_relative_path(raw_path)
        if relative.suffix.lower() not in _PUBLIC_TEXT_SUFFIXES:
            raise PolicyViolation(f"unsupported public text suffix: {relative.suffix}")
        target = self.gateway.project / relative
        project = self.gateway.project.resolve(strict=True)
        if (
            not target.is_file()
            or target.is_symlink()
            or not target.resolve(strict=True).is_relative_to(project)
        ):
            raise ValueError(f"public text target does not exist: {relative.as_posix()}")
        return target, relative.as_posix()

    def _read_text_file(self, arguments: Mapping[str, object]) -> dict[str, object]:
        raw_cursor = arguments.get("cursor", 0)
        raw_limit = arguments.get("limit", _TEXT_PAGE_MAXIMUM_BYTES)
        if not isinstance(raw_cursor, int) or isinstance(raw_cursor, bool) or raw_cursor < 0:
            raise ValueError("read_text_file cursor must be a non-negative byte offset")
        if (
            not isinstance(raw_limit, int)
            or isinstance(raw_limit, bool)
            or not 4 <= raw_limit <= _TEXT_PAGE_MAXIMUM_BYTES
        ):
            raise ValueError(
                f"read_text_file limit must be between 4 and {_TEXT_PAGE_MAXIMUM_BYTES} bytes"
            )
        target, normalized = self._public_text_path(arguments.get("path"))
        payload = target.read_bytes()
        try:
            content = payload.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise ValueError(f"public text target is not UTF-8: {normalized}") from error
        encoded = content.encode("utf-8")
        cursor_clamped = raw_cursor > len(encoded)
        cursor = min(raw_cursor, len(encoded))
        try:
            encoded[:cursor].decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("read_text_file cursor must align to a UTF-8 boundary") from error
        end = min(len(encoded), cursor + raw_limit)
        while end > cursor:
            try:
                page = encoded[cursor:end].decode("utf-8")
                break
            except UnicodeDecodeError as error:
                end = cursor + error.start
        else:
            page = ""
        next_cursor = end if end < len(encoded) else None
        return {
            "status": "success",
            "path": normalized,
            "content": page,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "requested_cursor": raw_cursor,
            "cursor": cursor,
            "cursor_clamped": cursor_clamped,
            "next_cursor": next_cursor,
            "total_bytes": len(encoded),
            "coverage_complete": next_cursor is None,
        }

    def _read_text_files(self, arguments: Mapping[str, object]) -> dict[str, object]:
        raw_paths = arguments.get("paths")
        if not isinstance(raw_paths, list) or not 1 <= len(raw_paths) <= _TEXT_BATCH_MAXIMUM_FILES:
            raise ValueError(
                f"read_text_files requires between 1 and {_TEXT_BATCH_MAXIMUM_FILES} paths"
            )
        if not all(isinstance(path, str) for path in raw_paths):
            raise ValueError("read_text_files paths must be strings")
        per_file_limit = min(
            _TEXT_PAGE_MAXIMUM_BYTES,
            _TEXT_BATCH_MAXIMUM_BYTES // len(raw_paths),
        )
        files = [
            self._read_text_file({"path": path, "limit": per_file_limit}) for path in raw_paths
        ]
        total_bytes = sum(len(str(item["content"]).encode("utf-8")) for item in files)
        return {"status": "success", "files": files, "total_bytes": total_bytes}

    def _review_project_changes(self) -> dict[str, object]:
        files: list[dict[str, object]] = []
        changed_paths: list[str] = []
        used_bytes = 0
        truncated = False
        for path, before in self._baseline_editable_text:
            target = self.gateway.project / path
            after = target.read_text(encoding="utf-8") if target.is_file() else None
            if before == after:
                continue
            changed_paths.append(path)
            diff = "".join(
                difflib.unified_diff(
                    (before or "").splitlines(keepends=True),
                    (after or "").splitlines(keepends=True),
                    fromfile=f"before/{path}",
                    tofile=f"after/{path}",
                    n=3,
                )
            )
            encoded = diff.encode("utf-8")
            remaining = max(0, _PROJECT_DIFF_MAXIMUM_BYTES - used_bytes)
            if len(encoded) > remaining:
                diff = encoded[:remaining].decode("utf-8", errors="ignore")
                truncated = True
            used_bytes += len(diff.encode("utf-8"))
            files.append(
                {
                    "path": path,
                    "change": (
                        "created" if before is None else "deleted" if after is None else "modified"
                    ),
                    "diff": diff,
                }
            )
            if truncated:
                break
        return {
            "status": "success",
            "changed_paths": changed_paths,
            "files": files,
            "truncated": truncated,
        }

    def _approved_path(self, raw_path: object, *, suffix: str) -> tuple[Path, str]:
        if not isinstance(raw_path, str):
            raise ValueError("tool path must be a string")
        relative = _safe_relative_path(raw_path)
        normalized = relative.as_posix()
        if normalized not in self.editable_paths:
            raise PolicyViolation(f"path is not in the approved editable set: {raw_path}")
        if relative.suffix.lower() != suffix:
            raise PolicyViolation(f"path must use the {suffix} suffix: {raw_path}")
        return relative, normalized

    def _apply_code_patch(self, arguments: Mapping[str, object]) -> dict[str, object]:
        raw_path = arguments.get("path")
        expected = arguments.get("expected")
        replacement = arguments.get("replacement")
        if not isinstance(expected, str) or not expected:
            raise ValueError("apply_code_patch expected anchor must be a non-empty string")
        if not isinstance(replacement, str):
            raise ValueError("apply_code_patch replacement must be a string")
        relative, normalized = self._approved_path(raw_path, suffix=".cs")
        proposal = TextReplacement(
            path=normalized,
            expected=expected,
            replacement=replacement,
            rationale="Exact replacement requested through the typed harness tool.",
            evidence_sources=("approved_project_context",),
        )
        patch = apply_text_replacement(
            self.gateway.project,
            proposal,
            allowed_roots=(relative.parent,),
        )
        return {"status": "success", **patch.model_dump(mode="json")}

    def _create_script(self, arguments: Mapping[str, object]) -> dict[str, object]:
        raw_path = arguments.get("path")
        content = arguments.get("content")
        relative, normalized = self._approved_path(raw_path, suffix=".cs")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("create_script content must be non-empty UTF-8 text")
        if len(content.encode("utf-8")) > 64 * 1024:
            raise ValueError("create_script content exceeds 64 KiB")
        target = self.gateway.project / relative
        if target.exists() or target.is_symlink():
            raise PolicyViolation(
                f"create_script refuses to overwrite an existing path: {normalized}"
            )
        parent = target.parent
        parent.mkdir(parents=True, exist_ok=True)
        resolved_parent = parent.resolve(strict=True)
        project = self.gateway.project.resolve(strict=True)
        if not resolved_parent.is_relative_to(project):
            raise PolicyViolation(f"create_script path escapes the project: {normalized}")
        target.write_text(content.rstrip() + "\n", encoding="utf-8")
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        return {"status": "success", "path": normalized, "sha256": digest}


@dataclass(frozen=True)
class UnityNativeEvaluator:
    gateway: UnityBatchGateway
    name: str = "unity-native"
    version: str = "1"

    def evaluate(self, run_spec: RunSpec, workspace: Path) -> tuple[GateOutcome, ...]:
        if workspace.resolve() != self.gateway.project.resolve():
            raise ValueError("evaluator workspace does not match the Unity gateway project")

        if run_spec.task.metadata.get("regenerate_scene") is True:
            self.gateway.create_arena()

        outcomes: list[GateOutcome] = []
        stopped = False
        for gate in run_spec.evaluation.required_gates:
            if stopped:
                outcomes.append(
                    GateOutcome(
                        gate=gate,
                        status=GateStatus.NOT_RUN,
                        detail="a prerequisite verification gate failed",
                    )
                )
                continue
            try:
                outcome = self._evaluate_gate(gate)
            except (ElementTree.ParseError, OSError, UnityToolError, ValueError) as error:
                outcome = GateOutcome(gate=gate, status=GateStatus.FAIL, detail=str(error))
            outcomes.append(outcome)
            stopped = outcome.status is GateStatus.FAIL
        return tuple(outcomes)

    def _evaluate_gate(self, gate: str) -> GateOutcome:
        if gate == "specification":
            return GateOutcome(
                gate=gate,
                status=GateStatus.PASS,
                detail="RunSpec and normalized task schemas are valid",
            )
        if gate == "compilation":
            result = self.gateway.health_check()
            return self._passed(gate, result.artifacts, "Unity batch compilation passed")
        if gate == "structure":
            result = self.gateway.run_tests("EditMode")
            _require_test_suite_passed(result.artifacts[0])
            return self._passed(gate, result.artifacts, "Edit Mode suite passed")
        if gate == "gameplay":
            result = self.gateway.run_tests("PlayMode")
            _require_test_suite_passed(result.artifacts[0])
            return self._passed(gate, result.artifacts, "Play Mode suite passed")
        if gate == "build":
            result = self.gateway.build_macos()
            return self._passed(gate, result.artifacts, "macOS player build passed")
        if gate == "smoke":
            result = self.gateway.launch_build_smoke_test()
            return self._passed(gate, result.artifacts, "built player smoke test passed")
        raise ValueError(f"unsupported Unity evaluation gate: {gate}")

    @staticmethod
    def _passed(gate: str, artifacts: tuple[Path, ...], detail: str) -> GateOutcome:
        return GateOutcome(
            gate=gate,
            status=GateStatus.PASS,
            detail=detail,
            artifacts=tuple(str(path) for path in artifacts),
        )


@dataclass(frozen=True)
class UnityCompileEvaluator:
    """Engine-native compile gate for arbitrary Unity projects."""

    gateway: UnityBatchGateway
    name: str = "unity-compile"
    version: str = "1"

    def evaluate(self, run_spec: RunSpec, workspace: Path) -> tuple[GateOutcome, ...]:
        if workspace.resolve() != self.gateway.project.resolve():
            raise ValueError("evaluator workspace does not match the Unity project")
        outcomes: list[GateOutcome] = []
        stopped = False
        for gate in run_spec.evaluation.required_gates:
            if stopped:
                outcomes.append(
                    GateOutcome(
                        gate=gate,
                        status=GateStatus.NOT_RUN,
                        detail="a prerequisite verification gate failed",
                    )
                )
                continue
            try:
                if gate == "specification":
                    outcome = GateOutcome(
                        gate=gate,
                        status=GateStatus.PASS,
                        detail="RunSpec and normalized task schemas are valid",
                    )
                elif gate == "compilation":
                    result = self.gateway.compile_project()
                    outcome = GateOutcome(
                        gate=gate,
                        status=GateStatus.PASS,
                        detail="Unity imported the project and compiled scripts",
                        artifacts=(str(result.log_path),),
                    )
                else:
                    raise ValueError(f"unsupported generic Unity gate: {gate}")
            except (OSError, UnityToolError, ValueError) as error:
                outcome = GateOutcome(
                    gate=gate,
                    status=GateStatus.FAIL,
                    detail=str(error),
                )
            outcomes.append(outcome)
            stopped = outcome.status is GateStatus.FAIL
        return tuple(outcomes)


def _require_test_suite_passed(path: Path) -> None:
    root = ElementTree.parse(path).getroot()
    result = root.attrib.get("result", "").lower()
    failed = int(root.attrib.get("failed", "0"))
    if result not in {"passed", "pass"} or failed:
        raise ValueError(
            f"Unity test suite failed: result={result or 'unknown'}, failed={failed}; see {path}"
        )
