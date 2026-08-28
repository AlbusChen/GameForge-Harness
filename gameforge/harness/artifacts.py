from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from pydantic import Field

from gameforge.harness.base import StrictModel
from gameforge.harness.events import EventKind, EventStore


class ArtifactHandle(StrictModel):
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    kind: str = Field(min_length=1)
    bytes: int = Field(ge=0)
    relative_path: str = Field(min_length=1)
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class ArtifactStoreError(RuntimeError):
    pass


class ContentAddressedArtifactStore:
    def __init__(
        self,
        root: Path,
        *,
        events: EventStore | None = None,
        maximum_bytes: int = 512 * 1024 * 1024,
    ) -> None:
        if maximum_bytes < 1:
            raise ValueError("artifact maximum_bytes must be positive")
        self.root = root
        self.objects = root / "objects"
        self.objects.mkdir(parents=True, exist_ok=True)
        self.events = events
        self.maximum_bytes = maximum_bytes

    def put_bytes(
        self,
        payload: bytes,
        *,
        kind: str,
        metadata: dict[str, str | int | float | bool | None] | None = None,
    ) -> ArtifactHandle:
        if not kind:
            raise ValueError("artifact kind must be non-empty")
        if len(payload) > self.maximum_bytes:
            raise ArtifactStoreError("artifact exceeds configured byte limit")
        digest = hashlib.sha256(payload).hexdigest()
        relative = Path("objects") / digest[:2] / digest
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if not target.is_file() or target.is_symlink():
                raise ArtifactStoreError("artifact object path is not a regular file")
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise ArtifactStoreError("artifact object hash mismatch")
        else:
            temporary_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
                    temporary_path = Path(stream.name)
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary_path.replace(target)
            except OSError as error:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
                raise ArtifactStoreError("failed to persist artifact") from error
        handle = ArtifactHandle(
            sha256=digest,
            kind=kind,
            bytes=len(payload),
            relative_path=relative.as_posix(),
            metadata=metadata or {},
        )
        if self.events is not None:
            self.events.append(
                EventKind.ARTIFACT_RECORDED,
                subject_id=digest,
                payload=handle.model_dump(mode="json"),
            )
        return handle

    def put_json(
        self,
        payload: object,
        *,
        kind: str = "json",
        metadata: dict[str, str | int | float | bool | None] | None = None,
    ) -> ArtifactHandle:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return self.put_bytes(encoded, kind=kind, metadata=metadata)

    def put_file(
        self,
        path: Path,
        *,
        kind: str,
        metadata: dict[str, str | int | float | bool | None] | None = None,
    ) -> ArtifactHandle:
        if not path.is_file() or path.is_symlink():
            raise ArtifactStoreError(f"artifact source is not a regular file: {path}")
        size = path.stat().st_size
        if size > self.maximum_bytes:
            raise ArtifactStoreError("artifact exceeds configured byte limit")
        return self.put_bytes(path.read_bytes(), kind=kind, metadata=metadata)

    def read_bytes(self, handle: ArtifactHandle) -> bytes:
        target = self.root / handle.relative_path
        resolved_root = self.root.resolve(strict=True)
        if not target.is_file() or target.is_symlink():
            raise ArtifactStoreError("artifact object is missing")
        resolved = target.resolve(strict=True)
        if not resolved.is_relative_to(resolved_root):
            raise ArtifactStoreError("artifact path escapes the store")
        payload = resolved.read_bytes()
        if len(payload) != handle.bytes or hashlib.sha256(payload).hexdigest() != handle.sha256:
            raise ArtifactStoreError("artifact object failed integrity verification")
        return payload
