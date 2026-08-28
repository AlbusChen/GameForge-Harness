from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - the benchmark runtime is macOS/POSIX.
    fcntl = None  # type: ignore[assignment]


def run_with_process_mutex(
    *,
    lock_file: Path,
    executable: Path,
    arguments: tuple[str, ...],
    crash_attempts: int = 1,
    private_home_root: Path | None = None,
) -> int:
    """Run an unrestricted child command while holding a cross-process mutex."""

    resolved_executable = executable.resolve(strict=True)
    if not resolved_executable.is_file():
        raise ValueError(f"queued executable is not a file: {resolved_executable}")
    if not 1 <= crash_attempts <= 3:
        raise ValueError("crash_attempts must be between one and three")
    resolved_lock = lock_file.resolve()
    resolved_lock.parent.mkdir(parents=True, exist_ok=True)
    resolved_home_root = private_home_root.resolve() if private_home_root else None
    if resolved_home_root is not None:
        resolved_home_root.mkdir(parents=True, exist_ok=True)
    with resolved_lock.open("a+b") as stream:
        if fcntl is not None:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            for attempt in range(1, crash_attempts + 1):
                if resolved_home_root is None:
                    completed = subprocess.run(
                        [str(resolved_executable), *arguments],
                        check=False,
                    )
                else:
                    with tempfile.TemporaryDirectory(
                        prefix="process-", dir=resolved_home_root
                    ) as private_home:
                        environment = os.environ.copy()
                        environment["HOME"] = private_home
                        environment["CFFIXED_USER_HOME"] = private_home
                        completed = subprocess.run(
                            [str(resolved_executable), *arguments],
                            check=False,
                            env=environment,
                        )
                if completed.returncode not in {-11, -6, 134, 139}:
                    return completed.returncode
                if attempt == crash_attempts:
                    return (
                        128 - completed.returncode
                        if completed.returncode < 0
                        else completed.returncode
                    )
            raise AssertionError("unreachable process retry state")
        finally:
            if fcntl is not None:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _arguments(raw: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one command while holding a shared process mutex."
    )
    parser.add_argument("--lock-file", type=Path, required=True)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--crash-attempts", type=int, default=1)
    parser.add_argument("--private-home-root", type=Path)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    parsed = parser.parse_args(raw)
    if parsed.arguments[:1] == ["--"]:
        parsed.arguments = parsed.arguments[1:]
    return parsed


def main(raw: list[str] | None = None) -> int:
    parsed = _arguments(raw)
    return run_with_process_mutex(
        lock_file=parsed.lock_file,
        executable=parsed.executable,
        arguments=tuple(parsed.arguments),
        crash_attempts=parsed.crash_attempts,
        private_home_root=parsed.private_home_root,
    )


if __name__ == "__main__":
    raise SystemExit(main())
