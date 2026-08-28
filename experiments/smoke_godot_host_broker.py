from __future__ import annotations

import argparse
import json
import shlex
import sys
import tempfile
from pathlib import Path

from gameforge.adapters.llm import AgentCliLanguageModel
from gameforge.harness.godot_broker import GodotHostBroker, broker_client_environment


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Smoke-test Host Godot access from a real workspace-write agent session."
    )
    parser.add_argument("--agent", type=Path, required=True)
    parser.add_argument("--godot", type=Path, required=True)
    parser.add_argument(
        "--direct-godot",
        type=Path,
        default=Path("/Applications/Godot.app/Contents/MacOS/Godot"),
    )
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--reasoning-effort", default="medium")
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    with tempfile.TemporaryDirectory(prefix="gameforge-godot-broker-smoke-") as directory:
        root = Path(directory)
        project = root / "project"
        tools = root / "tools"
        project.mkdir()
        tools.mkdir()
        project_file = project / "project.godot"
        project_file.write_text(
            '[application]\nconfig/name="Godot Broker Smoke"\n', encoding="utf-8"
        )
        before = project_file.read_bytes()
        broker = GodotHostBroker(
            executable=arguments.godot,
            project_root=project,
            exchange_root=tools / "broker",
            lock_file=Path(tempfile.gettempdir()) / "gameforge-godot-process-v1.lock",
            timeout_seconds=30,
        )
        with broker:
            client_command = " ".join(
                shlex.quote(part) for part in (sys.executable, *broker.client_arguments)
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
                pre_tool_use_hook_command=(
                    sys.executable,
                    "-m",
                    "gameforge.harness.native_tool_policy",
                    "godot",
                ),
            )
            response = model.run_workspace_agent(
                project=project,
                prompt=(
                    "Test the fixed Godot boundary without modifying files. First attempt "
                    f"`{arguments.direct_godot} --headless --version` exactly once and "
                    "observe the policy result. Then run `godot --headless --version` "
                    "exactly once. Report which call was blocked and the version returned "
                    "by the allowed call. Do not try another route around the policy."
                ),
                timeout_seconds=180,
                environment_overrides=broker_client_environment(tools, wrapper),
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
            "exit_134_events": sum(event.get("exit_code") == 134 for event in command_events),
            "broker_events": broker.events,
            "project_unchanged": project_file.read_bytes() == before,
            "usage": {
                "input_tokens": response.usage.input_tokens,
                "cached_input_tokens": response.usage.cached_input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        expected_version = "4.4.1.stable.official.49a5bc7b6"
        return (
            0
            if payload["exit_134_events"] == 0
            and payload["project_unchanged"]
            and len(command_events) == 1
            and command_events[0].get("exit_code") == 0
            and "blocked" in response.text.lower()
            and len(broker.events) == 1
            and broker.events[0].get("return_code") == 0
            and expected_version in response.text
            else 1
        )


if __name__ == "__main__":
    raise SystemExit(main())
