from __future__ import annotations

import argparse
import json
import shlex
import sys
import tempfile
from pathlib import Path

from gameforge.harness.godot_host_execution import (
    GodotHostExecutionBridge,
    host_execution_client_environment,
)

from gameforge.adapters.llm import AgentCliLanguageModel


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Smoke-test minimal host Godot execution from a real model sandbox."
    )
    parser.add_argument("--agent", type=Path, required=True)
    parser.add_argument("--godot", type=Path, required=True)
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--reasoning-effort", default="medium")
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    with tempfile.TemporaryDirectory(prefix="minimal-godot-host-execution-smoke-") as directory:
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
        bridge = GodotHostExecutionBridge(
            executable=arguments.godot,
            project_root=project,
            exchange_root=tools / "exchange",
            timeout_seconds=60,
        )
        with bridge:
            client_command = " ".join(
                shlex.quote(part) for part in (sys.executable, *bridge.client_arguments)
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
                    "Do not modify files. Run `godot --headless --version` exactly once, then "
                    "report the complete version string and stop."
                ),
                timeout_seconds=180,
                environment_overrides=host_execution_client_environment(tools, wrapper),
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
            "host_execution_events": bridge.events,
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
            and len(bridge.events) == 1
            and bridge.events[0].get("return_code") == 0
            and expected_version in response.text
            else 1
        )


if __name__ == "__main__":
    raise SystemExit(main())
