from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


class ProjectScopeError(RuntimeError):
    pass


@dataclass
class ProjectAccessScope:
    """Runtime write scope, independent from project-wide read-only discovery."""

    initial_writable_paths: tuple[str, ...]
    allow_text_scope_expansion: bool = False
    maximum_writable_paths: int = 64
    _writable_paths: set[str] = field(init=False, repr=False)
    _declarations: list[dict[str, object]] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        normalized = tuple(_safe_relative(path) for path in self.initial_writable_paths)
        if len(normalized) != len(set(normalized)):
            raise ValueError("initial writable paths must be unique")
        if len(normalized) > self.maximum_writable_paths:
            raise ValueError("initial writable paths exceed the configured scope limit")
        self.initial_writable_paths = normalized
        self._writable_paths = set(normalized)

    @property
    def writable_paths(self) -> tuple[str, ...]:
        return tuple(sorted(self._writable_paths))

    @property
    def declarations(self) -> tuple[dict[str, object], ...]:
        return tuple(self._declarations)

    def is_writable(self, path: str) -> bool:
        return _safe_relative(path) in self._writable_paths

    def declare_output_manifest(
        self,
        paths: tuple[str, ...],
        *,
        reason: str,
    ) -> dict[str, object]:
        if not self.allow_text_scope_expansion:
            raise ProjectScopeError("task policy does not allow writable scope expansion")
        if not paths:
            raise ProjectScopeError("output manifest must declare at least one path")
        if not reason.strip():
            raise ProjectScopeError("output manifest requires a non-empty reason")
        normalized = tuple(_safe_relative(path) for path in paths)
        if len(normalized) != len(set(normalized)):
            raise ProjectScopeError("output manifest paths must be unique")
        expanded = self._writable_paths | set(normalized)
        if len(expanded) > self.maximum_writable_paths:
            raise ProjectScopeError(
                f"output manifest exceeds the {self.maximum_writable_paths}-path scope limit"
            )
        added = tuple(sorted(set(normalized) - self._writable_paths))
        self._writable_paths = expanded
        declaration = {
            "sequence": len(self._declarations) + 1,
            "reason": reason.strip(),
            "requested_paths": list(normalized),
            "added_paths": list(added),
            "writable_paths": list(self.writable_paths),
        }
        self._declarations.append(declaration)
        return declaration

    def snapshot(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "read_scope": "public-project-text",
            "initial_writable_paths": list(self.initial_writable_paths),
            "writable_paths": list(self.writable_paths),
            "scope_expansion_enabled": self.allow_text_scope_expansion,
            "maximum_writable_paths": self.maximum_writable_paths,
            "declarations": list(self.declarations),
        }


def _safe_relative(raw_path: str) -> str:
    relative = Path(raw_path)
    if not raw_path or relative.is_absolute() or ".." in relative.parts:
        raise ProjectScopeError(f"unsafe project-relative path: {raw_path}")
    return relative.as_posix()
