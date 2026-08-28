from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import Field

from gameforge.harness.base import StrictModel
from gameforge.harness.game_tasks import AssetPolicy
from gameforge.harness.preservation import IGNORED_DIRECTORY_NAMES

_UNITY_GUID_PATTERN = re.compile(r"\bguid:\s*([0-9a-fA-F]{32})\b")
_GODOT_UID_PATTERN = re.compile(r'\buid\s*=\s*"([^"]+)"')
_GODOT_PATH_PATTERN = re.compile(r'\bpath\s*=\s*"res://([^"]+)"')
_TEXT_SUFFIXES = frozenset(
    {
        ".asmdef",
        ".cfg",
        ".cs",
        ".gd",
        ".godot",
        ".import",
        ".json",
        ".meta",
        ".prefab",
        ".shader",
        ".tres",
        ".tscn",
        ".unity",
        ".yaml",
        ".yml",
    }
)
_ENGINE_GENERATED_SIDECAR_SUFFIXES = frozenset({".import", ".uid"})


class AssetKind(StrEnum):
    TEXT = "text"
    BINARY = "binary"
    GENERATED = "generated"


class ReferenceKind(StrEnum):
    UNITY_GUID = "unity_guid"
    GODOT_UID = "godot_uid"
    GODOT_PATH = "godot_path"


class AssetProvenance(StrictModel):
    source: str = Field(min_length=1)
    license_id: str = Field(min_length=1)
    original_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    attribution: str | None = None


class AssetNode(StrictModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    bytes: int = Field(ge=0)
    kind: AssetKind
    identity: str | None = None


class ResourceReference(StrictModel):
    source_path: str
    target: str
    kind: ReferenceKind
    resolved_path: str | None = None


class AssetGraphSnapshot(StrictModel):
    project_path: Path
    nodes: tuple[AssetNode, ...]
    references: tuple[ResourceReference, ...]
    broken_references: tuple[ResourceReference, ...]
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    def node_by_path(self) -> dict[str, AssetNode]:
        return {node.path: node for node in self.nodes}


class ResourceSafetyReport(StrictModel):
    passed: bool
    changed_protected_paths: tuple[str, ...] = ()
    missing_protected_identities: tuple[str, ...] = ()
    new_broken_references: tuple[ResourceReference, ...] = ()


class ProjectTransactionResult(StrictModel):
    changed_paths: tuple[str, ...]
    sha256_before: dict[str, str | None]
    sha256_after: dict[str, str]
    provenance: dict[str, AssetProvenance] = Field(default_factory=dict)
    asset_graph_digest: str


class ResourceSafetyError(RuntimeError):
    pass


def build_asset_graph(
    project_path: Path,
    *,
    generated_roots: tuple[str, ...] = (),
    maximum_files: int = 50_000,
    maximum_text_bytes: int = 2 * 1024 * 1024,
) -> AssetGraphSnapshot:
    root = project_path.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("asset graph root must be a directory")
    normalized_generated = tuple(_safe_relative(path) for path in generated_roots)
    nodes: list[AssetNode] = []
    text: dict[str, str] = {}
    unity_guid_paths: dict[str, str] = {}
    godot_uid_paths: dict[str, str] = {}
    for directory, names, files in os.walk(root, topdown=True, followlinks=False):
        names[:] = sorted(
            name
            for name in names
            if name not in IGNORED_DIRECTORY_NAMES and not (Path(directory) / name).is_symlink()
        )
        for name in sorted(files):
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            payload = path.read_bytes()
            generated = any(
                relative == prefix or relative.startswith(f"{prefix}/")
                for prefix in normalized_generated
            ) or _is_generated_sidecar(relative)
            kind = (
                AssetKind.GENERATED
                if generated
                else (AssetKind.TEXT if _is_text(path, payload) else AssetKind.BINARY)
            )
            identity: str | None = None
            decoded: str | None = None
            if kind is not AssetKind.BINARY and len(payload) <= maximum_text_bytes:
                try:
                    decoded = payload.decode("utf-8")
                except UnicodeDecodeError:
                    decoded = None
                if decoded is not None and kind is not AssetKind.GENERATED:
                    text[relative] = decoded
                    if relative.endswith(".meta"):
                        own_guid = _first_match(_UNITY_GUID_PATTERN, decoded)
                        if own_guid is not None:
                            asset_path = relative.removesuffix(".meta")
                            unity_guid_paths[own_guid.lower()] = asset_path
                            identity = f"unity-guid:{own_guid.lower()}"
                    godot_uid = _first_match(_GODOT_UID_PATTERN, decoded)
                    if godot_uid is not None:
                        godot_uid_paths[godot_uid] = relative
                        identity = f"godot-uid:{godot_uid}"
            nodes.append(
                AssetNode(
                    path=relative,
                    sha256=hashlib.sha256(payload).hexdigest(),
                    bytes=len(payload),
                    kind=kind,
                    identity=identity,
                )
            )
            if len(nodes) > maximum_files:
                raise ResourceSafetyError("asset graph exceeds maximum file count")

    references: list[ResourceReference] = []
    broken: list[ResourceReference] = []
    node_paths = {node.path for node in nodes}
    for source_path, content in text.items():
        own_meta_guid = (
            _first_match(_UNITY_GUID_PATTERN, content) if source_path.endswith(".meta") else None
        )
        for match in _UNITY_GUID_PATTERN.finditer(content):
            guid = match.group(1).lower()
            if own_meta_guid is not None and guid == own_meta_guid.lower():
                continue
            reference = ResourceReference(
                source_path=source_path,
                target=guid,
                kind=ReferenceKind.UNITY_GUID,
                resolved_path=unity_guid_paths.get(guid),
            )
            references.append(reference)
            if reference.resolved_path is None:
                broken.append(reference)
        for match in _GODOT_PATH_PATTERN.finditer(content):
            target = Path(match.group(1)).as_posix()
            reference = ResourceReference(
                source_path=source_path,
                target=target,
                kind=ReferenceKind.GODOT_PATH,
                resolved_path=target if target in node_paths else None,
            )
            references.append(reference)
            if reference.resolved_path is None:
                broken.append(reference)
        for match in _GODOT_UID_PATTERN.finditer(content):
            uid = match.group(1)
            if godot_uid_paths.get(uid) == source_path:
                continue
            reference = ResourceReference(
                source_path=source_path,
                target=uid,
                kind=ReferenceKind.GODOT_UID,
                resolved_path=godot_uid_paths.get(uid),
            )
            references.append(reference)
            if reference.resolved_path is None:
                broken.append(reference)
    digest_payload = {
        "nodes": [node.model_dump(mode="json") for node in nodes],
        "references": [reference.model_dump(mode="json") for reference in references],
    }
    digest = hashlib.sha256(
        json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return AssetGraphSnapshot(
        project_path=root,
        nodes=tuple(nodes),
        references=tuple(references),
        broken_references=tuple(broken),
        digest=digest,
    )


def compare_asset_graphs(
    before: AssetGraphSnapshot,
    after: AssetGraphSnapshot,
    *,
    editable_paths: tuple[str, ...],
    protected_identities: tuple[str, ...] = (),
) -> ResourceSafetyReport:
    if before.project_path != after.project_path:
        raise ValueError("asset graphs refer to different projects")
    editable_values = {_safe_relative(path) for path in editable_paths}
    editable = frozenset(
        editable_values | {f"{path}.meta" for path in editable_values if not path.endswith(".meta")}
    )
    before_nodes = before.node_by_path()
    after_nodes = after.node_by_path()
    changed_protected = tuple(
        sorted(
            path
            for path in set(before_nodes) | set(after_nodes)
            if path not in editable
            and not _is_generated_sidecar(path)
            and (
                before_nodes.get(path) is None
                or after_nodes.get(path) is None
                or before_nodes[path].sha256 != after_nodes[path].sha256
            )
        )
    )
    after_identities = {node.identity for node in after.nodes if node.identity is not None}
    missing_identities = tuple(
        sorted(identity for identity in protected_identities if identity not in after_identities)
    )
    before_broken = {
        (item.source_path, item.kind.value, item.target) for item in before.broken_references
    }
    new_broken = tuple(
        item
        for item in after.broken_references
        if (item.source_path, item.kind.value, item.target) not in before_broken
    )
    return ResourceSafetyReport(
        passed=not changed_protected and not missing_identities and not new_broken,
        changed_protected_paths=changed_protected,
        missing_protected_identities=missing_identities,
        new_broken_references=new_broken,
    )


def _is_generated_sidecar(path: str) -> bool:
    return Path(path).suffix.lower() in _ENGINE_GENERATED_SIDECAR_SUFFIXES


@dataclass
class ProjectTransaction:
    project: Path
    editable_paths: tuple[str, ...]
    asset_policy: AssetPolicy

    def __post_init__(self) -> None:
        self.project = self.project.resolve(strict=True)
        self._editable = frozenset(_safe_relative(path) for path in self.editable_paths)
        self._staged: dict[str, tuple[bytes, AssetProvenance | None]] = {}

    def stage_text(self, path: str, content: str) -> None:
        self._stage(path, content.encode("utf-8"), provenance=None, binary=False)

    def stage_binary(self, path: str, content: bytes, provenance: AssetProvenance) -> None:
        self._stage(path, content, provenance=provenance, binary=True)

    def commit(self) -> ProjectTransactionResult:
        if not self._staged:
            raise ResourceSafetyError("project transaction has no staged changes")
        before_graph = build_asset_graph(
            self.project, generated_roots=self.asset_policy.generated_roots
        )
        backups: dict[str, bytes | None] = {}
        temporary_paths: list[Path] = []
        try:
            for relative, (payload, _) in self._staged.items():
                target = self._target(relative)
                backups[relative] = target.read_bytes() if target.exists() else None
                target.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                    temporary_paths.append(Path(stream.name))
            for relative, temporary in zip(self._staged, temporary_paths, strict=True):
                temporary.replace(self._target(relative))
            after_graph = build_asset_graph(
                self.project, generated_roots=self.asset_policy.generated_roots
            )
            report = compare_asset_graphs(
                before_graph,
                after_graph,
                editable_paths=self.editable_paths,
            )
            if report.new_broken_references:
                raise ResourceSafetyError("project transaction introduced broken references")
        except Exception:
            self._restore(backups)
            for temporary in temporary_paths:
                temporary.unlink(missing_ok=True)
            raise
        before_hashes = {
            path: None if payload is None else hashlib.sha256(payload).hexdigest()
            for path, payload in backups.items()
        }
        after_hashes = {
            path: hashlib.sha256(payload).hexdigest() for path, (payload, _) in self._staged.items()
        }
        result = ProjectTransactionResult(
            changed_paths=tuple(sorted(self._staged)),
            sha256_before=before_hashes,
            sha256_after=after_hashes,
            provenance={
                path: provenance
                for path, (_, provenance) in self._staged.items()
                if provenance is not None
            },
            asset_graph_digest=after_graph.digest,
        )
        self._staged.clear()
        return result

    def _stage(
        self,
        path: str,
        payload: bytes,
        *,
        provenance: AssetProvenance | None,
        binary: bool,
    ) -> None:
        normalized = _safe_relative(path)
        if normalized not in self._editable:
            raise ResourceSafetyError(f"path is outside transaction scope: {normalized}")
        if normalized in self._staged:
            raise ResourceSafetyError(f"path is already staged: {normalized}")
        if binary:
            if not self.asset_policy.allow_binary_mutation:
                raise ResourceSafetyError("binary mutation is disabled by asset policy")
            if self.asset_policy.require_provenance and provenance is None:
                raise ResourceSafetyError("binary mutation requires provenance")
            if (
                provenance is not None
                and self.asset_policy.allowed_license_ids
                and provenance.license_id not in self.asset_policy.allowed_license_ids
            ):
                raise ResourceSafetyError("binary asset license is not allowed")
        self._staged[normalized] = (payload, provenance)

    def _target(self, relative: str) -> Path:
        target = self.project / relative
        if target.is_symlink():
            raise ResourceSafetyError(f"transaction refuses symbolic link: {relative}")
        resolved_parent = target.parent.resolve(strict=target.parent.exists())
        if not resolved_parent.is_relative_to(self.project):
            raise ResourceSafetyError(f"transaction path escapes project: {relative}")
        return target

    def _restore(self, backups: dict[str, bytes | None]) -> None:
        for relative, payload in backups.items():
            target = self._target(relative)
            if payload is None:
                target.unlink(missing_ok=True)
            else:
                target.write_bytes(payload)


def _first_match(pattern: re.Pattern[str], value: str) -> str | None:
    match = pattern.search(value)
    return match.group(1) if match is not None else None


def _is_text(path: Path, payload: bytes) -> bool:
    if path.suffix.lower() in _TEXT_SUFFIXES:
        return True
    if b"\x00" in payload[:4096]:
        return False
    try:
        payload[:4096].decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _safe_relative(path: str) -> str:
    relative = Path(path)
    if not path or relative.is_absolute() or ".." in relative.parts:
        raise ResourceSafetyError(f"unsafe project-relative path: {path}")
    return relative.as_posix()
