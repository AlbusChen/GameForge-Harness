from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

_PROTOCOL_VERSION = 1
_POLL_SECONDS = 0.01
_TERMINATION_GRACE_SECONDS = 3.0


def _write_json_atomic(path: Path, payload: object) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _normalized_return_code(return_code: int) -> int:
    return 128 - return_code if return_code < 0 else return_code


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    else:  # pragma: no cover - supported release hosts are POSIX.
        process.terminate()
    try:
        process.wait(timeout=_TERMINATION_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
    else:  # pragma: no cover
        process.kill()
    process.wait(timeout=_TERMINATION_GRACE_SECONDS)


def _host_environment_without_credentials() -> dict[str, str]:
    blocked = {"GH_TOKEN", "GITHUB_TOKEN", "LLM_API_KEY", "OPENAI_API_KEY"}
    return {
        name: value
        for name, value in os.environ.items()
        if name.upper() not in blocked and not name.upper().endswith(("_API_KEY", "_ACCESS_TOKEN"))
    }


@dataclass
class SupervisedNativeTransport:
    """Transparent broker for one pinned Host executable.

    The model retains complete argv freedom. The broker changes only the process
    boundary: it fixes the executable, confines cwd to disposable roots, removes
    unrelated credentials, enforces one timeout, and records content-free audit
    metadata. It applies no workflow policy, argument rewriting, or retry.
    """

    executable: Path
    workspace_root: Path
    scratch_root: Path
    exchange_root: Path
    timeout_seconds: float
    additional_cwd_roots: tuple[Path, ...] = ()
    _token: str = field(init=False, repr=False)
    _stop: threading.Event = field(init=False, repr=False)
    _cwd_roots: tuple[tuple[str, Path], ...] = field(init=False, repr=False)
    _thread: threading.Thread | None = field(init=False, default=None, repr=False)
    _active_process: subprocess.Popen[bytes] | None = field(init=False, default=None, repr=False)
    _active_lock: threading.Lock = field(init=False, default_factory=threading.Lock, repr=False)
    _events: list[dict[str, object]] = field(init=False, default_factory=list, repr=False)
    _events_lock: threading.Lock = field(init=False, default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        executable = self.executable.resolve(strict=True)
        workspace = self.workspace_root.resolve(strict=True)
        scratch = self.scratch_root.resolve(strict=False)
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ValueError(f"supervised executable is unavailable: {executable}")
        if not workspace.is_dir():
            raise ValueError(f"supervised workspace is not a directory: {workspace}")
        if self.timeout_seconds <= 0:
            raise ValueError("supervised execution timeout must be positive")
        roots: list[tuple[str, Path]] = [("workspace", workspace), ("scratch", scratch)]
        for index, root in enumerate(self.additional_cwd_roots, start=1):
            resolved = root.resolve(strict=True)
            if not resolved.is_dir():
                raise ValueError(f"temporary cwd root is not a directory: {resolved}")
            if any(resolved == existing for _, existing in roots):
                continue
            roots.append((f"temporary-{index}", resolved))
        object.__setattr__(self, "executable", executable)
        object.__setattr__(self, "workspace_root", workspace)
        object.__setattr__(self, "scratch_root", scratch)
        object.__setattr__(self, "exchange_root", self.exchange_root.resolve())
        object.__setattr__(self, "_cwd_roots", tuple(roots))
        object.__setattr__(self, "_token", secrets.token_hex(32))
        object.__setattr__(self, "_stop", threading.Event())

    @property
    def client_arguments(self) -> tuple[str, ...]:
        return (
            str(Path(__file__).resolve(strict=True)),
            "client",
            "--exchange-root",
            str(self.exchange_root),
            "--token",
            self._token,
            "--timeout-seconds",
            str(self.timeout_seconds + 15.0),
            "--",
        )

    @property
    def events(self) -> tuple[dict[str, object], ...]:
        with self._events_lock:
            return tuple(dict(event) for event in self._events)

    def __enter__(self) -> SupervisedNativeTransport:
        self.scratch_root.mkdir(mode=0o700, parents=True, exist_ok=False)
        self.exchange_root.mkdir(parents=True, exist_ok=False)
        self.exchange_root.chmod(0o700)
        for name in ("requests", "processing", "responses"):
            directory = self.exchange_root / name
            directory.mkdir()
            directory.chmod(0o700)
        thread = threading.Thread(
            target=self._serve,
            name="gameforge-supervised-native",
            daemon=True,
        )
        object.__setattr__(self, "_thread", thread)
        thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        with self._active_lock:
            active = self._active_process
        if active is not None:
            _terminate_process(active)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=_TERMINATION_GRACE_SECONDS + 1.0)
            if thread.is_alive():
                raise RuntimeError("supervised Host execution bridge did not stop")

    def _serve(self) -> None:
        requests = self.exchange_root / "requests"
        processing = self.exchange_root / "processing"
        while not self._stop.is_set():
            request_path = next(
                (
                    path
                    for path in sorted(requests.glob("*.json"))
                    if path.is_file() and len(path.stem) == 32
                ),
                None,
            )
            if request_path is None:
                self._stop.wait(_POLL_SECONDS)
                continue
            claimed = processing / request_path.name
            try:
                request_path.replace(claimed)
            except FileNotFoundError:
                continue
            self._handle_request(claimed)

    def _handle_request(self, request_path: Path) -> None:
        request_id = request_path.stem
        response = self.exchange_root / "responses" / request_id
        response.mkdir(mode=0o700)
        stdout_path = response / "stdout.bin"
        stderr_path = response / "stderr.bin"
        try:
            payload = json.loads(request_path.read_text(encoding="utf-8"))
            arguments, cwd = self._validated_request(payload)
            result = self._run(arguments, cwd, stdout_path, stderr_path)
        except Exception as error:
            stdout_path.write_bytes(b"")
            stderr_path.write_text(
                f"supervised Host execution rejected request: {error}\n",
                encoding="utf-8",
            )
            result = {
                "schema_version": _PROTOCOL_VERSION,
                "return_code": 125,
                "timed_out": False,
                "error": f"{type(error).__name__}: {error}",
            }
            self._record_event((), None, result, rejected=True)
        _write_json_atomic(response / "result.json", result)

    def _validated_request(self, payload: object) -> tuple[tuple[str, ...], Path]:
        if not isinstance(payload, dict):
            raise ValueError("request must be a JSON object")
        if payload.get("schema_version") != _PROTOCOL_VERSION:
            raise ValueError("unsupported supervised execution protocol")
        if not secrets.compare_digest(str(payload.get("token", "")), self._token):
            raise PermissionError("invalid supervised execution token")
        raw_arguments = payload.get("arguments")
        if not isinstance(raw_arguments, list) or not all(
            isinstance(item, str) for item in raw_arguments
        ):
            raise ValueError("arguments must be a list of strings")
        if len(raw_arguments) > 256 or sum(len(item) for item in raw_arguments) > 64 * 1024:
            raise ValueError("arguments exceed the supervised transport limit")
        raw_cwd = payload.get("cwd")
        if not isinstance(raw_cwd, str):
            raise ValueError("cwd must be a string")
        cwd = Path(raw_cwd).resolve(strict=True)
        if not cwd.is_dir() or self._describe_cwd(cwd)[0] == "outside-authority":
            raise PermissionError("cwd must remain inside a disposable authorized root")
        return tuple(raw_arguments), cwd

    def _describe_cwd(self, cwd: Path) -> tuple[str, str | None]:
        for scope, root in self._cwd_roots:
            if _within(cwd, root):
                return scope, cwd.relative_to(root).as_posix()
        return "outside-authority", None

    def _run(
        self,
        arguments: tuple[str, ...],
        cwd: Path,
        stdout_path: Path,
        stderr_path: Path,
    ) -> dict[str, object]:
        started = time.monotonic()
        with stdout_path.open("wb") as stdout_stream, stderr_path.open("wb") as stderr_stream:
            process = subprocess.Popen(
                [str(self.executable), *arguments],
                cwd=cwd,
                env=_host_environment_without_credentials(),
                stdout=stdout_stream,
                stderr=stderr_stream,
                start_new_session=os.name == "posix",
            )
            with self._active_lock:
                self._active_process = process
            timed_out = False
            try:
                try:
                    raw_return_code = process.wait(timeout=self.timeout_seconds)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    _terminate_process(process)
                    raw_return_code = 124
            finally:
                with self._active_lock:
                    self._active_process = None
        return_code = _normalized_return_code(raw_return_code)
        result = {
            "schema_version": _PROTOCOL_VERSION,
            "return_code": return_code,
            "timed_out": timed_out,
            "error": None,
            "execution_seconds": round(time.monotonic() - started, 3),
            "attempt_count": 1,
        }
        self._record_event(arguments, cwd, result, rejected=False)
        return result

    def _record_event(
        self,
        arguments: tuple[str, ...],
        cwd: Path | None,
        result: dict[str, object],
        *,
        rejected: bool,
    ) -> None:
        canonical = json.dumps(arguments, ensure_ascii=False, separators=(",", ":")).encode()
        scope, relative_cwd = self._describe_cwd(cwd) if cwd is not None else ("invalid", None)
        event = {
            "schema_version": _PROTOCOL_VERSION,
            "arguments_sha256": hashlib.sha256(canonical).hexdigest(),
            "argument_count": len(arguments),
            "cwd_scope": scope,
            "cwd": relative_cwd,
            "return_code": result.get("return_code"),
            "timed_out": bool(result.get("timed_out", False)),
            "rejected": rejected,
            "execution_seconds": result.get("execution_seconds", 0.0),
            "attempt_count": result.get("attempt_count", 0),
        }
        with self._events_lock:
            self._events.append(event)


def run_client(
    *,
    exchange_root: Path,
    token: str,
    arguments: Sequence[str],
    timeout_seconds: float,
) -> int:
    root = exchange_root.resolve(strict=True)
    request_id = uuid.uuid4().hex
    _write_json_atomic(
        root / "requests" / f"{request_id}.json",
        {
            "schema_version": _PROTOCOL_VERSION,
            "token": token,
            "cwd": str(Path.cwd().resolve(strict=True)),
            "arguments": list(arguments),
        },
    )
    result_path = root / "responses" / request_id / "result.json"
    deadline = time.monotonic() + timeout_seconds
    while not result_path.is_file():
        if time.monotonic() >= deadline:
            print("supervised Host execution response timed out", file=sys.stderr)
            return 124
        time.sleep(_POLL_SECONDS)
    response = result_path.parent
    sys.stdout.buffer.write((response / "stdout.bin").read_bytes())
    sys.stdout.buffer.flush()
    sys.stderr.buffer.write((response / "stderr.bin").read_bytes())
    sys.stderr.buffer.flush()
    result = json.loads(result_path.read_text(encoding="utf-8"))
    return_code = result.get("return_code")
    if not isinstance(return_code, int) or not 0 <= return_code <= 255:
        print("supervised Host execution returned an invalid exit code", file=sys.stderr)
        return 125
    return return_code


def _arguments(raw: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GameForge supervised Host execution client")
    subparsers = parser.add_subparsers(dest="command", required=True)
    client = subparsers.add_parser("client")
    client.add_argument("--exchange-root", type=Path, required=True)
    client.add_argument("--token", required=True)
    client.add_argument("--timeout-seconds", type=float, required=True)
    client.add_argument("arguments", nargs=argparse.REMAINDER)
    parsed = parser.parse_args(raw)
    if parsed.arguments[:1] == ["--"]:
        parsed.arguments = parsed.arguments[1:]
    return parsed


def main(raw: Sequence[str] | None = None) -> int:
    parsed = _arguments(raw)
    return run_client(
        exchange_root=parsed.exchange_root,
        token=parsed.token,
        arguments=parsed.arguments,
        timeout_seconds=parsed.timeout_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())
