from __future__ import annotations

import argparse
import json
import shlex
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

from gameforge.harness.pinned_godot_transport import (
    TransparentPinnedGodotTransport,
    pinned_godot_client_environment,
)

import gameforge.adapters.llm as llm_adapter
from gameforge.adapters.llm import AgentCliLanguageModel
from gameforge.benchmarking.gamedevbench import _isolated_godot_runtime


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Smoke-test transparent pinned Godot transport from a real model sandbox."
    )
    parser.add_argument("--agent", type=Path, required=True)
    parser.add_argument("--godot", type=Path, required=True)
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--reasoning-effort", default="medium")
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    original_agent_process = llm_adapter._run_agent_cli_process

    def diagnostic_agent_process(*args, **kwargs):
        completed = original_agent_process(*args, **kwargs)
        if completed.returncode != 0:
            print("AGENT_STDOUT_BEGIN", file=sys.stderr)
            print(completed.stdout, file=sys.stderr)
            print("AGENT_STDOUT_END", file=sys.stderr)
            print("AGENT_STDERR_BEGIN", file=sys.stderr)
            print(completed.stderr, file=sys.stderr)
            print("AGENT_STDERR_END", file=sys.stderr)
        return completed

    llm_adapter._run_agent_cli_process = diagnostic_agent_process
    with ExitStack() as stack:
        directory = stack.enter_context(
            tempfile.TemporaryDirectory(prefix="transparent-pinned-godot-smoke-")
        )
        runtime = stack.enter_context(_isolated_godot_runtime(arguments.godot))
        root = Path(directory)
        project = root / "project"
        tools = root / "tools"
        project.mkdir()
        tools.mkdir()
        project_file = project / "project.godot"
        project_file.write_text(
            '[application]\nconfig/name="Minimal Host Execution Smoke"\n',
            encoding="utf-8",
        )
        before = project_file.read_bytes()
        transport = TransparentPinnedGodotTransport(
            executable=runtime.executable,
            workspace_root=project,
            scratch_root=tools / "scratch",
            exchange_root=tools / "exchange",
            timeout_seconds=60,
            additional_cwd_roots=(Path("/private/tmp"), Path(tempfile.gettempdir())),
        )
        with transport:
            client_command = " ".join(
                shlex.quote(part) for part in (sys.executable, *transport.client_arguments)
            )
            wrapper = tools / "godot"
            wrapper.write_text(f'#!/bin/sh\nexec {client_command} "$@"\n', encoding="utf-8")
            wrapper.chmod(0o755)
            model = AgentCliLanguageModel(
                executable=arguments.agent,
                model=arguments.model,
                reasoning_effort=arguments.reasoning_effort,
                timeout_seconds=180,
                maximum_startup_retries=0,
            )
            response = model.run_workspace_agent(
                project=project,
                prompt=(
                    "Do not modify the workspace. Execute exactly one terminal command. In that "
                    "single command, create a temporary directory with `mktemp -d`, write a "
                    "minimal `project.godot` inside it, run `godot --headless --path \"$tmpdir\" "
                    "--editor --quit`, then run `godot4 --headless --version`. Report the complete "
                    "version string and stop."
                ),
                timeout_seconds=180,
                environment_overrides=pinned_godot_client_environment(
                    tools,
                    wrapper,
                    transport.scratch_root,
                ),
            )
        command_events = [
            event for event in response.tool_audit if event.get("kind") == "command_execution"
        ]
        payload = {
            "schema_version": 1,
            "model": response.model,
            "transport_attempts": response.transport_attempts,
            "final_message": response.text,
            "command_exit_codes": [event.get("exit_code") for event in command_events],
            "model_visible_exit_134_events": sum(
                event.get("exit_code") == 134 for event in command_events
            ),
            "transport_events": transport.events,
            "project_unchanged": project_file.read_bytes() == before,
        }
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        expected_version = "4.4.1.stable.official.49a5bc7b6"
        return (
            0
            if payload["model_visible_exit_134_events"] == 0
            and payload["project_unchanged"]
            and len(command_events) == 1
            and command_events[0].get("exit_code") == 0
            and len(transport.events) == 2
            and all(event.get("return_code") == 0 for event in transport.events)
            and not any(event.get("rejected") for event in transport.events)
            and expected_version in response.text
            else 1
        )


if __name__ == "__main__":
    raise SystemExit(main())
