from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from gameforge.harness.preservation import IGNORED_DIRECTORY_NAMES, IGNORED_FILE_SUFFIXES

DEFAULT_ALLOWED_SUFFIXES = frozenset(
    {
        ".cfg",
        ".cs",
        ".gd",
        ".gdshader",
        ".glsl",
        ".godot",
        ".json",
        ".md",
        ".shader",
        ".tres",
        ".tscn",
        ".unity",
        ".yaml",
        ".yml",
    }
)

DEFAULT_ASSET_SUFFIXES = frozenset(
    {
        ".bmp",
        ".fbx",
        ".glb",
        ".gltf",
        ".jpeg",
        ".jpg",
        ".material",
        ".mp3",
        ".obj",
        ".ogg",
        ".otf",
        ".png",
        ".svg",
        ".ttf",
        ".wav",
        ".webp",
    }
)


class SourceDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    content: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    bytes: int = Field(ge=0)


class ProjectAsset(BaseModel):
    """Content-free project asset metadata safe to expose to an agent."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    suffix: str = Field(min_length=1)
    bytes: int = Field(ge=0)
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    referenced_by: tuple[str, ...] = ()
    integrity: Literal["valid", "invalid", "unchecked"] = "unchecked"
    integrity_detail: str | None = Field(default=None, max_length=200)


class ProjectContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_path: Path
    files: tuple[SourceDocument, ...] = ()
    assets: tuple[ProjectAsset, ...] = ()
    asset_count_total: int = Field(default=0, ge=0)
    assets_truncated: bool = False
    total_bytes: int = Field(default=0, ge=0)


@dataclass(frozen=True)
class ContextPolicy:
    allowed_suffixes: frozenset[str] = DEFAULT_ALLOWED_SUFFIXES
    maximum_files: int = 32
    maximum_file_bytes: int = 64 * 1024
    maximum_total_bytes: int = 256 * 1024
    asset_suffixes: frozenset[str] = DEFAULT_ASSET_SUFFIXES
    # Explicitly authorized binary asset paths belong to mutation scope, but their bytes are
    # never injected into the ordinary text context. They remain content-free manifest entries
    # and are bounded by the transaction, provenance, and preservation layers.
    editable_asset_suffixes: frozenset[str] = DEFAULT_ASSET_SUFFIXES
    maximum_assets: int = 256
    maximum_hashed_asset_bytes: int = 16 * 1024 * 1024


def build_project_context(
    project_path: Path,
    editable_paths: tuple[str, ...],
    *,
    policy: ContextPolicy | None = None,
) -> ProjectContext:
    active_policy = policy or ContextPolicy()
    project = project_path.resolve(strict=True)
    if not project.is_dir():
        raise ValueError(f"project path is not a directory: {project}")
    if len(editable_paths) > active_policy.maximum_files:
        raise ValueError(
            f"editable path count exceeds the {active_policy.maximum_files}-file context limit"
        )
    if len(editable_paths) != len(set(editable_paths)):
        raise ValueError("editable paths must be unique")

    documents: list[SourceDocument] = []
    total_bytes = 0
    for raw_path in editable_paths:
        relative = Path(raw_path)
        if not raw_path or relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"editable path must be a safe project-relative path: {raw_path}")
        suffix = relative.suffix.lower()
        if (
            suffix not in active_policy.allowed_suffixes
            and suffix not in active_policy.editable_asset_suffixes
        ):
            raise ValueError(f"editable path has an unsupported source suffix: {raw_path}")

        unresolved = project / relative
        if unresolved.is_symlink():
            raise ValueError(f"editable path cannot be a symbolic link: {raw_path}")
        if not unresolved.exists():
            continue
        target = unresolved.resolve(strict=True)
        if not target.is_relative_to(project):
            raise ValueError(f"editable path escapes the project root: {raw_path}")
        if not target.is_file():
            raise ValueError(f"editable path is not a file: {raw_path}")

        # Mutation scope is not a context-injection contract. Existing binary assets can be
        # approved for a typed engine capability while remaining content-free model metadata.
        if suffix in active_policy.editable_asset_suffixes:
            continue

        payload = target.read_bytes()
        if len(payload) > active_policy.maximum_file_bytes:
            raise ValueError(
                f"editable file exceeds the {active_policy.maximum_file_bytes}-byte limit: "
                f"{raw_path}"
            )
        total_bytes += len(payload)
        if total_bytes > active_policy.maximum_total_bytes:
            raise ValueError(
                "editable source context exceeds the "
                f"{active_policy.maximum_total_bytes}-byte total limit"
            )
        try:
            content = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError(f"editable file is not valid UTF-8: {raw_path}") from error
        documents.append(
            SourceDocument(
                path=relative.as_posix(),
                content=content,
                sha256=hashlib.sha256(payload).hexdigest(),
                bytes=len(payload),
            )
        )

    assets, asset_count_total = _build_asset_manifest(project, tuple(documents), active_policy)
    return ProjectContext(
        project_path=project,
        files=tuple(documents),
        assets=assets,
        asset_count_total=asset_count_total,
        assets_truncated=asset_count_total > len(assets),
        total_bytes=total_bytes,
    )


def _build_asset_manifest(
    project: Path,
    documents: tuple[SourceDocument, ...],
    policy: ContextPolicy,
) -> tuple[tuple[ProjectAsset, ...], int]:
    references: dict[str, set[str]] = {}
    suffix_pattern = "|".join(
        re.escape(suffix.removeprefix(".")) for suffix in sorted(policy.asset_suffixes)
    )
    reference_pattern = re.compile(
        rf"(?:res://)?[A-Za-z0-9_./ @+()-]+\.(?:{suffix_pattern})",
        re.IGNORECASE,
    )
    for document in documents:
        for match in reference_pattern.findall(document.content):
            normalized = match.removeprefix("res://").strip("\"' ")
            references.setdefault(normalized, set()).add(document.path)

    assets: list[ProjectAsset] = []
    asset_count_total = 0
    for directory, names, files in os.walk(project, topdown=True, followlinks=False):
        names[:] = sorted(
            name
            for name in names
            if name not in IGNORED_DIRECTORY_NAMES and not (Path(directory) / name).is_symlink()
        )
        for name in sorted(files):
            path = Path(directory) / name
            suffix = path.suffix.lower()
            if (
                path.is_symlink()
                or suffix in IGNORED_FILE_SUFFIXES
                or suffix not in policy.asset_suffixes
            ):
                continue
            asset_count_total += 1
            if len(assets) >= policy.maximum_assets:
                continue
            relative = path.relative_to(project).as_posix()
            size = path.stat().st_size
            digest = (
                hashlib.sha256(path.read_bytes()).hexdigest()
                if size <= policy.maximum_hashed_asset_bytes
                else None
            )
            integrity, integrity_detail = _asset_integrity(path, suffix)
            assets.append(
                ProjectAsset(
                    path=relative,
                    kind=_asset_kind(suffix),
                    suffix=suffix,
                    bytes=size,
                    sha256=digest,
                    referenced_by=tuple(sorted(references.get(relative, set()))),
                    integrity=integrity,
                    integrity_detail=integrity_detail,
                )
            )
    return tuple(assets), asset_count_total


def _asset_kind(suffix: str) -> str:
    if suffix in {".bmp", ".jpeg", ".jpg", ".png", ".svg", ".webp"}:
        return "image"
    if suffix in {".mp3", ".ogg", ".wav"}:
        return "audio"
    if suffix in {".fbx", ".glb", ".gltf", ".obj"}:
        return "model"
    if suffix in {".otf", ".ttf"}:
        return "font"
    if suffix == ".material":
        return "material"
    return "asset"


def _asset_integrity(
    path: Path, suffix: str
) -> tuple[Literal["valid", "invalid", "unchecked"], str | None]:
    """Expose passive decode health without injecting binary contents into model context."""
    if suffix not in {".bmp", ".jpeg", ".jpg", ".png", ".webp"}:
        return "unchecked", None
    try:
        with Image.open(path) as image:
            image.verify()
    except (OSError, SyntaxError, ValueError) as error:
        return "invalid", f"image decode failed: {type(error).__name__}"
    return "valid", None
