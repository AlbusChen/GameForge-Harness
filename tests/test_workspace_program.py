from __future__ import annotations

import time
from pathlib import Path

import pytest

from gameforge.harness.project_scope import ProjectAccessScope
from gameforge.harness.workspace_program import WorkspaceProgramExecutor


def _executor(
    tmp_path: Path,
    *,
    writable: tuple[str, ...],
    host_managed_executables: tuple[str, ...] = (),
) -> WorkspaceProgramExecutor:
    project = tmp_path / "project"
    project.mkdir()
    return WorkspaceProgramExecutor(
        project=project,
        scratch_root=tmp_path / "runs" / "workspace-program",
        project_scope=ProjectAccessScope(
            writable,
            allow_text_scope_expansion=True,
        ),
        host_managed_executables=host_managed_executables,
    )


def test_workspace_program_runs_full_python_with_analysis_libraries(tmp_path: Path) -> None:
    executor = _executor(tmp_path, writable=("scene.tscn",))
    (executor.project / "scene.tscn").write_text("[gd_scene]\n", encoding="utf-8")

    result = executor.invoke(
        {
            "source": (
                "from pathlib import Path\n"
                "from PIL import Image\n"
                "print(Path('scene.tscn').read_text().strip())\n"
                "print(Image.new('RGB', (3, 2)).size)\n"
            ),
            "paths": [],
            "timeout_seconds": 30,
        }
    )

    assert result["status"] == "success"
    assert result["committed_paths"] == []
    assert "[gd_scene]" in result["stdout"]
    assert "(3, 2)" in result["stdout"]


def test_workspace_program_blocks_host_managed_godot_process(tmp_path: Path) -> None:
    executor = _executor(
        tmp_path,
        writable=(),
        host_managed_executables=("godot",),
    )

    result = executor.invoke(
        {
            "source": (
                "import subprocess\n"
                "completed = subprocess.run(\n"
                "    ['godot', '--version'], capture_output=True, text=True, check=False\n"
                ")\n"
                "print(completed.returncode)\n"
                "print(completed.stderr)\n"
            ),
            "paths": [],
            "timeout_seconds": 30,
        }
    )

    assert result["status"] == "success"
    assert result["host_managed_process_blocked"] is True
    assert "GAMEFORGE_HOST_MANAGED_PROCESS" in result["stdout"]
    assert "Do not retry" in result["guidance"]


def test_workspace_program_commits_only_declared_paths(tmp_path: Path) -> None:
    executor = _executor(tmp_path, writable=("scene.tscn",))
    scene = executor.project / "scene.tscn"
    scene.write_text("before\n", encoding="utf-8")

    result = executor.invoke(
        {
            "source": "from pathlib import Path\nPath('scene.tscn').write_text('after\\n')\n",
            "paths": ["scene.tscn"],
            "timeout_seconds": 30,
        }
    )

    assert result["status"] == "success"
    assert result["committed_paths"] == ["scene.tscn"]
    assert scene.read_text(encoding="utf-8") == "after\n"


def test_workspace_program_can_generate_a_declared_binary_asset(tmp_path: Path) -> None:
    executor = _executor(tmp_path, writable=("generated/icon.png",))

    result = executor.invoke(
        {
            "source": (
                "from pathlib import Path\n"
                "from PIL import Image\n"
                "Path('generated').mkdir()\n"
                "Image.new('RGBA', (4, 3), (20, 40, 60, 255)).save('generated/icon.png')\n"
            ),
            "paths": ["generated/icon.png"],
            "timeout_seconds": 30,
        }
    )

    assert result["status"] == "success"
    assert result["committed_paths"] == ["generated/icon.png"]
    assert (executor.project / "generated/icon.png").read_bytes().startswith(b"\x89PNG")
    assert result["binary_change_lineage"][0]["provenance"] == (
        "task-authorized-model-generated-asset"
    )
    lineage = executor.scratch_root / result["binary_change_lineage_artifact"]
    assert lineage.is_file()
    assert "generated/icon.png" in lineage.read_text(encoding="utf-8")


def test_workspace_program_rejects_undeclared_changes_without_committing(tmp_path: Path) -> None:
    executor = _executor(tmp_path, writable=("scene.tscn", "other.gd"))
    scene = executor.project / "scene.tscn"
    other = executor.project / "other.gd"
    scene.write_text("scene-before\n", encoding="utf-8")
    other.write_text("other-before\n", encoding="utf-8")

    result = executor.invoke(
        {
            "source": (
                "from pathlib import Path\n"
                "Path('scene.tscn').write_text('scene-after\\n')\n"
                "Path('other.gd').write_text('other-after\\n')\n"
            ),
            "paths": ["scene.tscn"],
            "timeout_seconds": 30,
        }
    )

    assert result["status"] == "rejected"
    assert result["undeclared_paths"] == ["other.gd"]
    assert scene.read_text(encoding="utf-8") == "scene-before\n"
    assert other.read_text(encoding="utf-8") == "other-before\n"


def test_workspace_program_cannot_read_host_sibling(tmp_path: Path) -> None:
    executor = _executor(tmp_path, writable=("scene.tscn",))
    (executor.project / "scene.tscn").write_text("public\n", encoding="utf-8")
    secret = tmp_path / "hidden-evaluator.txt"
    secret.write_text("secret-value\n", encoding="utf-8")

    result = executor.invoke(
        {
            "source": (
                "from pathlib import Path\n"
                f"target = Path({str(secret)!r})\n"
                "try:\n"
                "    print(target.read_text())\n"
                "except OSError as error:\n"
                "    print('DENIED', type(error).__name__)\n"
            ),
            "paths": [],
            "timeout_seconds": 30,
        }
    )

    assert result["status"] == "success"
    assert "DENIED" in result["stdout"]
    assert "secret-value" not in result["stdout"]


def test_workspace_program_requires_runtime_write_authorization(tmp_path: Path) -> None:
    executor = _executor(tmp_path, writable=("scene.tscn",))
    (executor.project / "scene.tscn").write_text("public\n", encoding="utf-8")

    with pytest.raises(ValueError, match="not writable"):
        executor.invoke(
            {
                "source": "print('no mutation')\n",
                "paths": ["undeclared.gd"],
                "timeout_seconds": 30,
            }
        )


def test_workspace_program_timeout_is_structured_and_commits_nothing(
    tmp_path: Path,
) -> None:
    executor = _executor(tmp_path, writable=("scene.tscn",))
    scene = executor.project / "scene.tscn"
    scene.write_text("before\n", encoding="utf-8")

    started = time.monotonic()
    result = executor.invoke(
        {
            "source": (
                "from pathlib import Path\n"
                "import subprocess, sys, time\n"
                "Path('scene.tscn').write_text('uncommitted\\n')\n"
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                "time.sleep(30)\n"
            ),
            "paths": ["scene.tscn"],
            "timeout_seconds": 1,
        }
    )

    assert time.monotonic() - started < 8
    assert result["status"] == "timeout"
    assert result["timed_out"] is True
    assert result["committed_paths"] == []
    assert scene.read_text(encoding="utf-8") == "before\n"


def test_workspace_validator_persists_and_compares_current_revisions(tmp_path: Path) -> None:
    executor = _executor(tmp_path, writable=("scene.tscn",))
    scene = executor.project / "scene.tscn"
    scene.write_text("before\n", encoding="utf-8")

    saved = executor.save_validator(
        {
            "name": "scene-check",
            "source": (
                "from pathlib import Path\n"
                "value = Path('scene.tscn').read_text().strip()\n"
                "print({'value': value, 'passed': value == 'after'})\n"
            ),
        }
    )
    first = executor.run_validator({"name": "scene-check", "timeout_seconds": 30})
    scene.write_text("after\n", encoding="utf-8")
    second = executor.run_validator({"name": "scene-check", "timeout_seconds": 30})
    listed = executor.list_validators({})

    assert saved["status"] == "success"
    assert first["status"] == "success"
    assert "'passed': False" in first["stdout"]
    assert first["comparison"]["previous_run"] is False
    assert second["status"] == "success"
    assert "'passed': True" in second["stdout"]
    assert second["comparison"] == {
        "previous_run": True,
        "project_changed": True,
        "status_changed": False,
        "stdout_changed": True,
    }
    assert listed["validators"][0]["run_count"] == 2


def test_workspace_validator_cannot_mutate_candidate_project(tmp_path: Path) -> None:
    executor = _executor(tmp_path, writable=("scene.tscn",))
    scene = executor.project / "scene.tscn"
    scene.write_text("before\n", encoding="utf-8")
    executor.save_validator(
        {
            "name": "mutation-probe",
            "source": "from pathlib import Path\nPath('scene.tscn').write_text('changed\\n')\n",
        }
    )

    result = executor.run_validator({"name": "mutation-probe"})

    assert result["status"] == "rejected"
    assert result["committed_paths"] == []
    assert scene.read_text(encoding="utf-8") == "before\n"
