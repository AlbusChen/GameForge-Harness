from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from pydantic import Field, model_validator

from gameforge.harness.base import StrictModel
from gameforge.harness.evidence import EvidenceSnapshot


class ProgramState(StrictModel):
    version: int = 1
    cell_number: int = Field(default=0, ge=0)
    variables: dict[str, object] = Field(default_factory=dict)
    functions: dict[str, str] = Field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def state_is_bounded_json(self) -> ProgramState:
        try:
            encoded = json.dumps(
                {"variables": self.variables, "functions": self.functions, "notes": self.notes},
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise ValueError("program state must be JSON serializable") from error
        if len(encoded) > 4 * 1024 * 1024:
            raise ValueError("program state exceeds 4 MiB")
        for name in (*self.variables, *self.functions):
            if not name.isidentifier() or name.startswith("_"):
                raise ValueError(f"unsafe program state identifier: {name}")
        return self


class ControlSnapshot(StrictModel):
    run_id: str = Field(min_length=1)
    last_event_sequence: int = Field(ge=0)
    capability_digests: dict[str, str]
    plugin_digests: dict[str, str]
    program: ProgramState
    evidence: EvidenceSnapshot

    def assert_compatible(
        self,
        *,
        run_id: str,
        capability_digests: dict[str, str],
        plugin_digests: dict[str, str],
    ) -> None:
        if self.run_id != run_id:
            raise ProgramStateError("snapshot belongs to a different run")
        if self.capability_digests != capability_digests:
            raise ProgramStateError("capability digests changed since snapshot")
        if self.plugin_digests != plugin_digests:
            raise ProgramStateError("plugin digests changed since snapshot")


class ProgramStateError(RuntimeError):
    pass


class ProgramStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def save(self, snapshot: ControlSnapshot) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=self.path.parent,
                mode="w",
                encoding="utf-8",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                stream.write(snapshot.model_dump_json(indent=2) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.path)
        except OSError as error:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise ProgramStateError(f"failed to save program snapshot: {self.path}") from error

    def load(self) -> ControlSnapshot:
        if not self.path.is_file() or self.path.is_symlink():
            raise ProgramStateError(f"program snapshot is missing: {self.path}")
        try:
            return ControlSnapshot.model_validate_json(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ProgramStateError(f"invalid program snapshot: {self.path}") from error
