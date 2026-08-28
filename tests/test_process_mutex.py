from __future__ import annotations

import sys
import textwrap
from pathlib import Path

from gameforge.harness.process_mutex import run_with_process_mutex


def test_process_mutex_runs_child_with_unrestricted_arguments(tmp_path: Path) -> None:
    output = tmp_path / "output.txt"

    return_code = run_with_process_mutex(
        lock_file=tmp_path / "engine.lock",
        executable=Path(sys.executable),
        arguments=(
            "-c",
            (f"from pathlib import Path; Path({str(output)!r}).write_text('ok\\n')"),
        ),
    )

    assert return_code == 0
    assert output.read_text(encoding="utf-8") == "ok\n"


def test_process_mutex_retries_crash_with_fresh_private_home(tmp_path: Path) -> None:
    counter = tmp_path / "counter.txt"
    homes = tmp_path / "homes.txt"
    source = textwrap.dedent(
        f"""
        import os
        from pathlib import Path

        counter = Path({str(counter)!r})
        homes = Path({str(homes)!r})
        attempt = int(counter.read_text()) + 1 if counter.exists() else 1
        counter.write_text(str(attempt))
        with homes.open("a") as stream:
            stream.write(os.environ["CFFIXED_USER_HOME"] + "\\n")
        if attempt == 1:
            os._exit(134)
        """
    )

    return_code = run_with_process_mutex(
        lock_file=tmp_path / "engine.lock",
        executable=Path(sys.executable),
        arguments=("-c", source),
        crash_attempts=2,
        private_home_root=tmp_path / "private-homes",
    )

    recorded_homes = homes.read_text(encoding="utf-8").splitlines()
    assert return_code == 0
    assert counter.read_text(encoding="utf-8") == "2"
    assert len(recorded_homes) == 2
    assert recorded_homes[0] != recorded_homes[1]
