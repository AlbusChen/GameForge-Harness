from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from gameforge.harness.godot_broker import GodotHostBroker


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify safe movie capture through the fixed Godot Host broker."
    )
    parser.add_argument("--godot", type=Path, required=True)
    return parser.parse_args()


def _crash_reports() -> set[str]:
    reports = Path.home() / "Library" / "Logs" / "DiagnosticReports"
    return {str(path) for path in reports.glob("Godot-*.ips")}


def _client(
    broker: GodotHostBroker,
    project: Path,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *broker.client_arguments, *arguments],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )


def main() -> int:
    arguments = _arguments()
    before_reports = _crash_reports()
    with tempfile.TemporaryDirectory(prefix="gameforge-godot-movie-smoke-") as directory:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        (project / "project.godot").write_text(
            """[application]
config/name="Godot Movie Capture Smoke"
run/main_scene="res://main.tscn"

[display]
window/size/viewport_width=64
window/size/viewport_height=64
window/size/window_width_override=64
window/size/window_height_override=64

[rendering]
renderer/rendering_method="gl_compatibility"
renderer/rendering_method.mobile="gl_compatibility"
""",
            encoding="utf-8",
        )
        (project / "main.tscn").write_text(
            """[gd_scene format=3]

[node name="Main" type="Node2D"]

[node name="Background" type="ColorRect" parent="."]
offset_right = 64.0
offset_bottom = 64.0
color = Color(0.2, 0.4, 0.8, 1)
""",
            encoding="utf-8",
        )
        broker = GodotHostBroker(
            executable=arguments.godot,
            project_root=project,
            exchange_root=root / "exchange",
            lock_file=root / "godot.lock",
            timeout_seconds=30,
            queue_timeout_seconds=30,
        )
        with broker:
            incompatible = _client(
                broker,
                project,
                "--headless",
                "--path",
                ".",
                "--write-movie",
                "invalid.png",
                "--quit-after",
                "1",
            )
            captured = _client(
                broker,
                project,
                "--path",
                ".",
                "--write-movie",
                "capture.png",
                "--fixed-fps",
                "1",
                "--quit-after",
                "2",
                "--windowed",
                "--resolution",
                "64x64",
            )
        frames = sorted(path.name for path in project.glob("capture*.png"))
        new_reports = sorted(_crash_reports() - before_reports)
        payload = {
            "schema_version": 1,
            "fixed_executable": str(arguments.godot.resolve(strict=True)),
            "incompatible": {
                "return_code": incompatible.returncode,
                "stderr": incompatible.stderr,
            },
            "capture": {
                "return_code": captured.returncode,
                "stderr": captured.stderr,
                "frames": frames,
            },
            "broker_events": broker.events,
            "new_crash_reports": new_reports,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return (
            0
            if incompatible.returncode == 125
            and "movie capture requires rendered frames" in incompatible.stderr
            and captured.returncode == 0
            and len(frames) >= 2
            and not new_reports
            and len(broker.events) == 2
            and broker.events[0].get("rejection_reason")
            == "incompatible_rendering_options"
            and broker.events[1].get("return_code") == 0
            else 1
        )


if __name__ == "__main__":
    raise SystemExit(main())
