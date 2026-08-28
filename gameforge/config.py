from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

EXPECTED_UNITY_VERSION = "6000.3.20f1"
EXPECTED_GODOT_VERSION = "4.4.1.stable.official.49a5bc7b6"
DEFAULT_SPEC = Path("specs/arena_demo.yaml")
DEFAULT_UNITY_PROJECT = Path("unity/ArenaTemplate")
DEFAULT_MODEL_PROFILE = Path("config/model.mock.yaml")


@dataclass(frozen=True)
class ProjectPaths:
    root: Path
    spec: Path
    unity_project: Path
    runs: Path

    @classmethod
    def resolve(
        cls,
        root: Path,
        spec: Path = DEFAULT_SPEC,
        unity_project: Path = DEFAULT_UNITY_PROJECT,
    ) -> ProjectPaths:
        root = root.resolve()
        return cls(
            root=root,
            spec=(root / spec).resolve() if not spec.is_absolute() else spec.resolve(),
            unity_project=(root / unity_project).resolve()
            if not unity_project.is_absolute()
            else unity_project.resolve(),
            runs=(root / "runs").resolve(),
        )


def configured_unity_editor() -> Path | None:
    explicit = os.environ.get("UNITY_EDITOR_PATH")
    if explicit:
        return Path(explicit).expanduser().resolve()

    expected = Path(
        f"/Applications/Unity/Hub/Editor/{EXPECTED_UNITY_VERSION}/Unity.app/Contents/MacOS/Unity"
    )
    return expected if expected.is_file() else None


def configured_godot_editor() -> Path | None:
    explicit = os.environ.get("GODOT_EDITOR_PATH") or os.environ.get("GODOT_PATH")
    if explicit:
        return Path(explicit).expanduser().resolve()

    candidates = (
        Path("/private/tmp/godot-4.4.1/Godot.app/Contents/MacOS/Godot"),
        Path("/private/tmp/godot-4.4.1-bin/godot"),
    )
    return next((path for path in candidates if path.is_file()), None)


def llm_key_is_configured(root: Path) -> bool:
    if bool(os.environ.get("LLM_API_KEY")):
        return True

    local_env = root / ".env.local"
    if not local_env.is_file():
        return False

    # Only return presence. Callers must never print the value.
    try:
        for line in local_env.read_text(encoding="utf-8").splitlines():
            name, separator, value = line.partition("=")
            if separator and name.strip() == "LLM_API_KEY" and value.strip():
                return True
    except OSError:
        return False
    return False
