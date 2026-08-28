from __future__ import annotations

import os
import re
import shlex
from collections.abc import Mapping
from pathlib import Path

_ENVIRONMENT_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def login_shell_tool_environment(
    tool_directory: Path,
    *,
    exported_variables: Mapping[str, str] | None = None,
    inherited_path: str | None = None,
) -> dict[str, str]:
    """Keep task-owned command shims first when child login shells rebuild PATH."""

    tools = tool_directory.resolve(strict=True)
    if not tools.is_dir():
        raise ValueError("tool directory must be a directory")
    exports = dict(exported_variables or {})
    invalid_names = sorted(
        name for name in exports if _ENVIRONMENT_NAME_PATTERN.fullmatch(name) is None
    )
    if invalid_names:
        raise ValueError(
            "exported variable names must be portable shell identifiers: "
            + ", ".join(invalid_names)
        )
    if any(not isinstance(value, str) for value in exports.values()):
        raise TypeError("exported variable values must be strings")
    shell_config = tools / "shell-config"
    shell_config.mkdir(mode=0o700, exist_ok=True)
    lines = [f'export PATH={shlex.quote(str(tools))}:"${{PATH:-}}"']
    lines.extend(f"export {name}={shlex.quote(value)}" for name, value in sorted(exports.items()))
    configuration = "\n".join(lines) + "\n"
    for name in (".zshenv", ".zprofile", "sh-env"):
        (shell_config / name).write_text(configuration, encoding="utf-8")
    base_path = os.environ.get("PATH", "") if inherited_path is None else inherited_path
    return {
        "PATH": f"{tools}{os.pathsep}{base_path}",
        **exports,
        "ZDOTDIR": str(shell_config),
        "BASH_ENV": str(shell_config / "sh-env"),
        "ENV": str(shell_config / "sh-env"),
    }
