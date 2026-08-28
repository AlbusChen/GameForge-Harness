from pathlib import Path

import pytest

from gameforge.orchestrator.policy import PolicyViolation
from gameforge.orchestrator.repair_loop import apply_text_replacement
from gameforge.schemas.diagnostic import TextReplacement


def _proposal(path: str, expected: str = "before") -> TextReplacement:
    return TextReplacement(
        path=path,
        expected=expected,
        replacement="after",
        rationale="A compiler diagnostic identifies the exact broken token.",
        evidence_sources=("compile.log",),
    )


def test_scoped_patch_replaces_one_exact_anchor(tmp_path: Path) -> None:
    scripts = tmp_path / "unity" / "Assets" / "Scripts"
    scripts.mkdir(parents=True)
    source = scripts / "Example.cs"
    source.write_text("before\n", encoding="utf-8")

    result = apply_text_replacement(
        tmp_path,
        _proposal("unity/Assets/Scripts/Example.cs"),
        allowed_roots=(Path("unity/Assets/Scripts"),),
    )

    assert source.read_text(encoding="utf-8") == "after\n"
    assert result.replacements == 1
    assert result.sha256_before != result.sha256_after


def test_scoped_patch_rejects_path_escape(tmp_path: Path) -> None:
    with pytest.raises(PolicyViolation):
        apply_text_replacement(
            tmp_path,
            _proposal("../outside.cs"),
            allowed_roots=(Path("unity/Assets/Scripts"),),
        )


def test_scoped_patch_rejects_ambiguous_anchor(tmp_path: Path) -> None:
    scripts = tmp_path / "unity" / "Assets" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "Example.cs").write_text("before before\n", encoding="utf-8")

    with pytest.raises(ValueError, match="exactly once"):
        apply_text_replacement(
            tmp_path,
            _proposal("unity/Assets/Scripts/Example.cs"),
            allowed_roots=(Path("unity/Assets/Scripts"),),
        )
