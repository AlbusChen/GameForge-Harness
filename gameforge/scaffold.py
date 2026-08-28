from __future__ import annotations

import shutil
from importlib import resources
from pathlib import Path

SCAFFOLD_DIRECTORIES = ("benchmarks", "config", "specs", "unity")


def initialize_workspace(destination: Path) -> tuple[Path, ...]:
    """Copy the bundled, version-matched starter workspace into a new directory."""

    target = destination.resolve()
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ValueError(f"initialization destination must be absent or empty: {target}")
    target.mkdir(parents=True, exist_ok=True)

    resource_root = resources.files("gameforge.resources")
    if not all(resource_root.joinpath(name).is_dir() for name in SCAFFOLD_DIRECTORIES):
        source_checkout = Path(__file__).resolve().parent.parent
        if all((source_checkout / name).is_dir() for name in SCAFFOLD_DIRECTORIES):
            resource_root = source_checkout
    copied: list[Path] = []
    for name in SCAFFOLD_DIRECTORIES:
        source = resource_root.joinpath(name)
        if not source.is_dir():
            raise RuntimeError(f"installed package is missing scaffold resource: {name}")
        output = target / name
        shutil.copytree(source, output)
        copied.append(output)
    return tuple(copied)
