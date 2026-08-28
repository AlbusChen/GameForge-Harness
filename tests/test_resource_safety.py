from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from gameforge.harness.game_tasks import AssetPolicy
from gameforge.harness.resource_safety import (
    AssetProvenance,
    ProjectTransaction,
    ResourceSafetyError,
    build_asset_graph,
    compare_asset_graphs,
)


def test_asset_policy_requires_explicit_native_workspace_agent_authority() -> None:
    assert AssetPolicy().allow_native_workspace_agent is False


def test_project_transaction_preserves_reference_integrity_and_rolls_back(tmp_path: Path) -> None:
    (tmp_path / "script.gd").write_text("extends Node\n", encoding="utf-8")
    scene = tmp_path / "scene.tscn"
    scene.write_text(
        '[gd_scene]\n[ext_resource path="res://script.gd" type="Script" id="1"]\n',
        encoding="utf-8",
    )
    policy = AssetPolicy()
    transaction = ProjectTransaction(tmp_path, ("script.gd",), policy)
    transaction.stage_text("script.gd", "extends Node\nvar speed = 2\n")

    result = transaction.commit()

    assert result.changed_paths == ("script.gd",)
    assert not build_asset_graph(tmp_path).broken_references

    broken = ProjectTransaction(tmp_path, ("scene.tscn",), policy)
    broken.stage_text(
        "scene.tscn",
        '[gd_scene]\n[ext_resource path="res://missing.gd" type="Script" id="1"]\n',
    )
    original = scene.read_text(encoding="utf-8")
    with pytest.raises(ResourceSafetyError, match="broken references"):
        broken.commit()
    assert scene.read_text(encoding="utf-8") == original


def test_binary_asset_requires_policy_and_provenance(tmp_path: Path) -> None:
    payload = b"\x89PNG\r\n"
    provenance = AssetProvenance(
        source="user-upload",
        license_id="CC0-1.0",
        original_sha256=hashlib.sha256(payload).hexdigest(),
    )
    denied = ProjectTransaction(tmp_path, ("icon.png",), AssetPolicy())
    with pytest.raises(ResourceSafetyError, match="disabled"):
        denied.stage_binary("icon.png", payload, provenance)

    allowed = ProjectTransaction(
        tmp_path,
        ("icon.png",),
        AssetPolicy(
            allow_binary_mutation=True,
            allowed_license_ids=("CC0-1.0",),
        ),
    )
    allowed.stage_binary("icon.png", payload, provenance)
    result = allowed.commit()

    assert result.sha256_after["icon.png"] == hashlib.sha256(payload).hexdigest()


def test_engine_generated_godot_sidecars_do_not_fail_preservation(tmp_path: Path) -> None:
    scene = tmp_path / "scene.tscn"
    scene.write_text('[gd_scene format=3]\n[node name="Root" type="Node"]\n', encoding="utf-8")
    before = build_asset_graph(tmp_path)

    (tmp_path / "scene.tscn.uid").write_text("uid://generated\n", encoding="utf-8")
    (tmp_path / "image.png.import").write_text(
        '[deps]\nsource_file="res://image.png"\ndest_files=["res://.godot/imported/x.ctex"]\n',
        encoding="utf-8",
    )
    after = build_asset_graph(tmp_path)

    report = compare_asset_graphs(before, after, editable_paths=("scene.tscn",))

    assert report.passed
    assert not report.changed_protected_paths
    assert not report.new_broken_references
