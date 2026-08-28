from __future__ import annotations

from pathlib import Path

import pytest

from gameforge.harness.acceptance import FileAssertion, evaluate_acceptance
from gameforge.harness.contracts import GateStatus


def test_file_acceptance_assertions_are_independent_and_machine_readable(tmp_path: Path) -> None:
    source = tmp_path / "Assets" / "Scripts" / "Weapon.cs"
    source.parent.mkdir(parents=True)
    source.write_text("damage = 12;\n", encoding="utf-8")

    outcome = evaluate_acceptance(
        (
            "file_exists:Assets/Scripts/Weapon.cs",
            "file_contains:Assets/Scripts/Weapon.cs::damage = 12;",
            "file_not_contains:Assets/Scripts/Weapon.cs::damage = 10;",
        ),
        tmp_path,
    )

    assert outcome.status is GateStatus.PASS
    assert "Weapon.cs" in outcome.detail


def test_file_acceptance_rejects_traversal_and_missing_expectation(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cannot traverse"):
        FileAssertion.parse("file_exists:../secret")
    with pytest.raises(ValueError, match="requires a non-empty value"):
        FileAssertion.parse("file_contains:Game.cs")

    outcome = evaluate_acceptance(("file_exists:Missing.cs",), tmp_path)
    assert outcome.status is GateStatus.FAIL
