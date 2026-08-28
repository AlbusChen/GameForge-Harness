from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from pydantic import Field, field_validator

from gameforge.harness.base import StrictModel
from gameforge.harness.capabilities import CapabilityDescriptor, CapabilityEffect
from gameforge.harness.project_scope import ProjectAccessScope


class CapabilityPolicyViolation(RuntimeError):
    pass


class CapabilityPolicyConfig(StrictModel):
    allowed_permissions: tuple[str, ...] = ()
    editable_paths: tuple[str, ...] = ()
    approval_grants: tuple[str, ...] = ()
    allow_binary_mutation: bool = False
    require_binary_provenance: bool = True
    allowed_license_ids: tuple[str, ...] = ()
    maximum_paths_per_call: int = Field(default=64, ge=1, le=10_000)

    @field_validator("editable_paths")
    @classmethod
    def paths_are_safe(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_safe_relative(path) for path in value)
        if len(normalized) != len(set(normalized)):
            raise ValueError("editable paths must be unique")
        return normalized


class ScopedCapabilityPolicy:
    def __init__(
        self,
        config: CapabilityPolicyConfig,
        *,
        project_scope: ProjectAccessScope | None = None,
    ) -> None:
        self.config = config
        self._permissions = frozenset(config.allowed_permissions)
        self._project_scope = project_scope or ProjectAccessScope(config.editable_paths)
        self._approval_grants = frozenset(config.approval_grants)

    def authorize(self, descriptor: CapabilityDescriptor, arguments: Mapping[str, object]) -> None:
        ordinary_permissions = {
            permission
            for permission in descriptor.required_permissions
            if not permission.startswith("approval:")
        }
        missing = sorted(ordinary_permissions - self._permissions)
        if missing:
            raise CapabilityPolicyViolation(f"missing capability permissions: {missing}")
        required_approvals = {
            permission.removeprefix("approval:")
            for permission in descriptor.required_permissions
            if permission.startswith("approval:")
        }
        unapproved = sorted(required_approvals - self._approval_grants)
        if unapproved:
            raise CapabilityPolicyViolation(f"missing explicit approvals: {unapproved}")
        binary_mutation = CapabilityEffect.BINARY_ASSET_WRITE in descriptor.effects
        if binary_mutation and not self.config.allow_binary_mutation:
            raise CapabilityPolicyViolation("binary asset mutation is not allowed")
        if not _writes_project(descriptor):
            return
        _validate_embedded_resource_references(arguments)
        paths = tuple(sorted(_extract_mutation_paths(arguments)))
        if len(paths) > self.config.maximum_paths_per_call:
            raise CapabilityPolicyViolation("capability touches too many project paths")
        for raw_path in paths:
            normalized = _safe_relative(raw_path)
            if not self._project_scope.is_writable(normalized):
                raise CapabilityPolicyViolation(
                    f"path is outside the approved editable scope: {normalized}"
                )
        if binary_mutation and self.config.require_binary_provenance:
            _validate_binary_provenance(
                arguments.get("provenance"),
                paths=paths,
                allowed_license_ids=self.config.allowed_license_ids,
            )


def _writes_project(descriptor: CapabilityDescriptor) -> bool:
    return bool(
        {
            CapabilityEffect.PROJECT_WRITE,
            CapabilityEffect.BINARY_ASSET_WRITE,
        }
        & set(descriptor.effects)
    )


def _extract_mutation_paths(arguments: Mapping[str, object]) -> set[str]:
    found: set[str] = set()
    for key in ("path", "scene", "asset_path"):
        value = arguments.get(key)
        if isinstance(value, str):
            found.add(value)
    paths = arguments.get("paths")
    if isinstance(paths, list | tuple):
        found.update(item for item in paths if isinstance(item, str))
    files = arguments.get("files")
    if isinstance(files, list | tuple):
        for item in files:
            if isinstance(item, Mapping) and isinstance(item.get("path"), str):
                found.add(item["path"])
    return found


def _validate_embedded_resource_references(value: object) -> None:
    if isinstance(value, Mapping):
        for child in value.values():
            _validate_embedded_resource_references(child)
    elif isinstance(value, list | tuple):
        for child in value:
            _validate_embedded_resource_references(child)
    elif isinstance(value, str) and value.startswith("res://"):
        _safe_relative(value.removeprefix("res://"))


def _safe_relative(raw_path: str) -> str:
    relative = Path(raw_path)
    if not raw_path or relative.is_absolute() or ".." in relative.parts:
        raise CapabilityPolicyViolation(f"unsafe project-relative path: {raw_path}")
    return relative.as_posix()


def _validate_binary_provenance(
    raw: object,
    *,
    paths: tuple[str, ...],
    allowed_license_ids: tuple[str, ...],
) -> None:
    if not isinstance(raw, Mapping):
        raise CapabilityPolicyViolation("binary mutation requires provenance metadata")
    if {"source", "license_id", "original_sha256"}.issubset(raw):
        if len(paths) > 1:
            raise CapabilityPolicyViolation(
                "multi-file binary mutation requires provenance keyed by project path"
            )
        records = (raw,)
    else:
        normalized_records = {_safe_relative(str(path)): record for path, record in raw.items()}
        missing = sorted(set(paths) - set(normalized_records))
        if missing:
            raise CapabilityPolicyViolation(
                f"binary mutation is missing provenance for paths: {missing}"
            )
        records = tuple(normalized_records[path] for path in paths)
    for record in records:
        if not isinstance(record, Mapping):
            raise CapabilityPolicyViolation("binary provenance entry must be an object")
        source = record.get("source")
        license_id = record.get("license_id")
        digest = record.get("original_sha256")
        if not isinstance(source, str) or not source:
            raise CapabilityPolicyViolation("binary provenance requires a source")
        if not isinstance(license_id, str) or not license_id:
            raise CapabilityPolicyViolation("binary provenance requires a license_id")
        if allowed_license_ids and license_id not in allowed_license_ids:
            raise CapabilityPolicyViolation(f"binary asset license is not allowed: {license_id}")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise CapabilityPolicyViolation(
                "binary provenance requires a lowercase SHA-256 original_sha256"
            )
