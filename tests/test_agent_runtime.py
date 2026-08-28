from __future__ import annotations

import json
from pathlib import Path

import pytest

from gameforge.adapters.llm import ModelResponse, ModelUsage
from gameforge.harness.contracts import RunBudgets
from gameforge.harness.project_context import build_project_context
from gameforge.harness.runtime import AgentRuntime, RegisteredToolExecutor
from gameforge.harness.task_sources import ChangeRequestTaskSource


class ScriptedModel:
    def __init__(self, decisions: list[dict[str, object]]) -> None:
        self._decisions = iter(decisions)

    def complete(self, *, system: str, prompt: str) -> ModelResponse:
        assert "verification-first" in system
        assert "available_tools" in prompt
        return ModelResponse(
            text=json.dumps(next(self._decisions)),
            usage=ModelUsage(input_tokens=10, output_tokens=5, cost_usd=0.001),
            provider="scripted",
            model="test-model",
        )


def test_agent_runtime_executes_only_registered_typed_tools(tmp_path: Path) -> None:
    task = ChangeRequestTaskSource("Inspect the project.").load(tmp_path)
    model = ScriptedModel(
        [
            {
                "kind": "tool",
                "tool": "health_check",
                "arguments": {},
                "rationale": "Confirm the engine bridge is healthy.",
                "evidence_sources": [],
            },
            {
                "kind": "finish",
                "outcome": "success",
                "summary": "The bridge is healthy.",
                "rationale": "The typed health check returned success.",
            },
        ]
    )
    tools = RegisteredToolExecutor({"health_check": lambda arguments: {"status": "healthy"}})

    result = AgentRuntime(model, tools, RunBudgets(max_cost_usd=1)).run(task)

    assert result.outcome == "success"
    assert result.tool_calls == 1
    assert result.turns == 2
    assert result.cost_usd == 0.002
    assert result.trace[1]["status"] == "success"


def test_agent_runtime_can_finish_after_one_successful_bounded_tool(tmp_path: Path) -> None:
    task = ChangeRequestTaskSource("Apply one complete bounded change.").load(tmp_path)
    model = ScriptedModel(
        [
            {
                "kind": "tool",
                "tool": "health_check",
                "arguments": {},
                "rationale": "One tool call fully completes this synthetic task.",
                "finish_after_success": True,
                "summary_after_success": "The bounded tool completed; evaluate independently.",
            }
        ]
    )
    tools = RegisteredToolExecutor({"health_check": lambda arguments: {"status": "healthy"}})

    result = AgentRuntime(model, tools, RunBudgets(max_cost_usd=1)).run(task)

    assert result.outcome == "success"
    assert result.turns == 1
    assert result.tool_calls == 1
    assert result.cost_usd == 0.001
    assert result.trace[-1]["event"] == "provisional_finish_after_tool"


def test_agent_runtime_includes_only_bounded_project_context(tmp_path: Path) -> None:
    source = tmp_path / "Weapon.cs"
    source.write_text("class Weapon {}\n", encoding="utf-8")
    task = ChangeRequestTaskSource(
        "Inspect the approved file.", editable_paths=("Weapon.cs",)
    ).load(tmp_path)
    seen_prompt = ""

    class ContextModel:
        def complete(self, *, system: str, prompt: str) -> ModelResponse:
            nonlocal seen_prompt
            del system
            seen_prompt = prompt
            return ModelResponse(
                text=json.dumps(
                    {
                        "kind": "finish",
                        "outcome": "success",
                        "summary": "Inspected.",
                        "rationale": "The approved context was available.",
                    }
                ),
                usage=ModelUsage(1, 1, 0),
                provider="scripted",
                model="test-model",
            )

    context = build_project_context(tmp_path, task.editable_paths)
    AgentRuntime(ContextModel(), RegisteredToolExecutor({}), RunBudgets()).run(task, context)

    payload = json.loads(seen_prompt)
    assert payload["project_context"]["files"][0]["path"] == "Weapon.cs"
    assert payload["project_context"]["files"][0]["content"] == "class Weapon {}\n"


def test_unknown_tool_is_an_observation_not_arbitrary_execution(tmp_path: Path) -> None:
    task = ChangeRequestTaskSource("Inspect the project.").load(tmp_path)
    model = ScriptedModel(
        [
            {
                "kind": "tool",
                "tool": "shell",
                "arguments": {"command": "anything"},
                "rationale": "Try an unavailable tool.",
            },
            {
                "kind": "finish",
                "outcome": "blocked",
                "summary": "No approved inspection tool is available.",
                "rationale": "The requested tool was rejected.",
            },
        ]
    )
    tools = RegisteredToolExecutor({})

    result = AgentRuntime(model, tools, RunBudgets(max_cost_usd=1)).run(task)

    assert result.outcome == "blocked"
    assert result.tool_calls == 1
    assert result.trace[1]["status"] == "error"
    assert "not registered" in str(result.trace[1]["error"])


def test_tool_call_budget_blocks_before_invocation(tmp_path: Path) -> None:
    invoked = False

    def handler(arguments: object) -> object:
        nonlocal invoked
        invoked = True
        return arguments

    task = ChangeRequestTaskSource("Inspect the project.").load(tmp_path)
    model = ScriptedModel(
        [
            {
                "kind": "tool",
                "tool": "health_check",
                "arguments": {},
                "rationale": "Inspect first.",
            }
        ]
    )
    tools = RegisteredToolExecutor({"health_check": handler})

    result = AgentRuntime(model, tools, RunBudgets(max_tool_calls=0, max_cost_usd=1)).run(task)

    assert result.outcome == "blocked"
    assert result.summary == "tool-call budget exceeded"
    assert not invoked


def test_agent_runtime_automatically_validates_a_mutation(tmp_path: Path) -> None:
    task = ChangeRequestTaskSource(
        "Update the approved scene.", editable_paths=("scene.tscn",)
    ).load(tmp_path)
    model = ScriptedModel(
        [
            {
                "kind": "tool",
                "tool": "write_text_file",
                "arguments": {"path": "scene.tscn", "content": "updated"},
                "rationale": "Apply the complete bounded change.",
                "finish_after_success": True,
                "summary_after_success": "The scene is ready for independent evaluation.",
            }
        ]
    )
    tools = RegisteredToolExecutor(
        {
            "write_text_file": lambda arguments: {
                "status": "success",
                "path": arguments["path"],
            },
            "import_project": lambda arguments: {"status": "success"},
        }
    )

    result = AgentRuntime(
        model,
        tools,
        RunBudgets(max_tool_calls=4, max_cost_usd=1),
        validation_tool="import_project",
        max_validation_repairs=1,
    ).run(task)

    assert result.outcome == "success"
    assert result.tool_calls == 2
    assert result.validation_attempts == 1
    assert result.repair_attempts == 0
    assert result.trace[-2]["event"] == "automatic_validation"


def test_agent_runtime_allows_one_scoped_repair_after_failed_validation(
    tmp_path: Path,
) -> None:
    task = ChangeRequestTaskSource(
        "Update the approved scene.", editable_paths=("scene.tscn",)
    ).load(tmp_path)
    model = ScriptedModel(
        [
            {
                "kind": "tool",
                "tool": "write_text_file",
                "arguments": {"path": "scene.tscn", "content": "broken"},
                "rationale": "Apply the requested scene change.",
                "finish_after_success": True,
                "summary_after_success": "The scene is provisionally complete.",
            },
            {
                "kind": "tool",
                "tool": "replace_text",
                "arguments": {
                    "path": "scene.tscn",
                    "expected": "broken",
                    "replacement": "fixed",
                },
                "rationale": "Repair the exact parser fault reported by public validation.",
                "finish_after_success": True,
                "summary_after_success": "The scoped repair now validates.",
            },
        ]
    )
    validations = iter(({"status": "failed"}, {"status": "success"}))
    tools = RegisteredToolExecutor(
        {
            "write_text_file": lambda arguments: {
                "status": "success",
                "path": arguments["path"],
            },
            "replace_text": lambda arguments: {
                "status": "success",
                "path": arguments["path"],
            },
            "import_project": lambda arguments: next(validations),
        }
    )

    result = AgentRuntime(
        model,
        tools,
        RunBudgets(max_turns=3, max_tool_calls=6, max_cost_usd=1),
        validation_tool="import_project",
        max_validation_repairs=1,
    ).run(task)

    assert result.outcome == "success"
    assert result.validation_attempts == 2
    assert result.repair_attempts == 1
    assert result.tool_calls == 4


def test_completion_feedback_can_repair_any_approved_path_and_counts_aux_usage(
    tmp_path: Path,
) -> None:
    task = ChangeRequestTaskSource(
        "Update a scene and its approved helper script.",
        editable_paths=("scene.tscn", "helper.gd"),
    ).load(tmp_path)
    model = ScriptedModel(
        [
            {
                "kind": "tool",
                "tool": "write_text_file",
                "arguments": {"path": "scene.tscn", "content": "first"},
                "rationale": "Apply the initial scene change.",
                "finish_after_success": True,
                "summary_after_success": "The candidate is ready for feedback.",
            },
            {
                "kind": "tool",
                "tool": "write_text_file",
                "arguments": {"path": "helper.gd", "content": "repair"},
                "rationale": "Use the approved helper path to address the feedback.",
                "finish_after_success": True,
                "summary_after_success": "The approved multi-file repair is complete.",
            },
        ]
    )
    feedback = iter(
        (
            {
                "status": "failed",
                "summary": "visible mismatch",
                "_harness_usage": {
                    "provider": "reviewer",
                    "model": "test-reviewer",
                    "input_tokens": 3,
                    "output_tokens": 2,
                    "cost_usd": 0.2,
                },
            },
            {"status": "success", "summary": "visible request is satisfied"},
        )
    )
    tools = RegisteredToolExecutor(
        {
            "write_text_file": lambda arguments: {
                "status": "success",
                "path": arguments["path"],
            },
            "import_project": lambda arguments: {"status": "success"},
            "review_visual_change": lambda arguments: next(feedback),
        }
    )

    result = AgentRuntime(
        model,
        tools,
        RunBudgets(max_turns=3, max_tool_calls=8, max_cost_usd=1),
        validation_tool="import_project",
        completion_feedback_tools=("review_visual_change",),
        max_validation_repairs=1,
    ).run(task)

    assert result.outcome == "success"
    assert result.repair_attempts == 1
    assert result.tool_calls == 6
    assert result.input_tokens == 23
    assert result.output_tokens == 12
    assert result.cost_usd == pytest.approx(0.202)
    assert sum(event["event"] == "automatic_feedback" for event in result.trace) == 2


def test_completion_feedback_stops_repairs_when_candidate_does_not_improve(
    tmp_path: Path,
) -> None:
    task = ChangeRequestTaskSource(
        "Improve the approved scene.", editable_paths=("scene.tscn",)
    ).load(tmp_path)
    model = ScriptedModel(
        [
            {
                "kind": "tool",
                "tool": "write_text_file",
                "arguments": {"path": "scene.tscn", "content": "candidate-1"},
                "rationale": "Create the first candidate.",
                "finish_after_success": True,
                "summary_after_success": "The first candidate is ready.",
            },
            {
                "kind": "tool",
                "tool": "write_text_file",
                "arguments": {"path": "scene.tscn", "content": "candidate-2"},
                "rationale": "Apply one feedback-guided repair.",
                "finish_after_success": True,
                "summary_after_success": "The repaired candidate is ready.",
            },
        ]
    )
    feedback = iter(
        (
            {
                "status": "failed",
                "progress_status": "not_applicable",
                "summary": "first candidate misses the target",
            },
            {
                "status": "failed",
                "progress_status": "unchanged",
                "summary": "second candidate did not improve",
            },
        )
    )
    tools = RegisteredToolExecutor(
        {
            "write_text_file": lambda arguments: {
                "status": "success",
                "path": arguments["path"],
            },
            "import_project": lambda arguments: {"status": "success"},
            "review_visual_change": lambda arguments: next(feedback),
        }
    )

    result = AgentRuntime(
        model,
        tools,
        RunBudgets(max_turns=4, max_tool_calls=10, max_cost_usd=1),
        validation_tool="import_project",
        completion_feedback_tools=("review_visual_change",),
        max_validation_repairs=2,
    ).run(task)

    assert result.outcome == "success"
    assert result.repair_attempts == 1
    assert result.tool_calls == 6
    stopped = [
        event for event in result.trace if event["event"] == "advisory_feedback_no_progress"
    ]
    assert stopped[0]["progress_status"] == "unchanged"
