from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from gameforge.harness.project_context import ContextPolicy, build_project_context


def test_context_reads_only_explicit_editable_files(tmp_path: Path) -> None:
    scripts = tmp_path / "Assets" / "Scripts"
    scripts.mkdir(parents=True)
    approved = scripts / "Weapon.cs"
    approved.write_text("public class Weapon {}\n", encoding="utf-8")
    (scripts / "Secret.cs").write_text("do not include\n", encoding="utf-8")

    context = build_project_context(tmp_path, ("Assets/Scripts/Weapon.cs",))

    assert context.total_bytes == approved.stat().st_size
    assert [document.path for document in context.files] == ["Assets/Scripts/Weapon.cs"]
    assert context.files[0].content == "public class Weapon {}\n"
    assert len(context.files[0].sha256) == 64


def test_context_allows_declared_new_file_without_reading_it(tmp_path: Path) -> None:
    context = build_project_context(tmp_path, ("Assets/Scripts/NewWeapon.cs",))

    assert context.files == ()
    assert context.total_bytes == 0


def test_context_exposes_content_free_asset_manifest_and_references(tmp_path: Path) -> None:
    scene = tmp_path / "scenes" / "main.tscn"
    asset = tmp_path / "assets" / "smokelet.png"
    other = tmp_path / "assets" / "flamelet.png"
    scene.parent.mkdir(parents=True)
    asset.parent.mkdir(parents=True)
    scene.write_text(
        '[ext_resource path="res://assets/smokelet.png" type="Texture2D" id="1"]\n',
        encoding="utf-8",
    )
    asset.write_bytes(b"smoke")
    other.write_bytes(b"flame")

    context = build_project_context(tmp_path, ("scenes/main.tscn",))

    assert [record.path for record in context.assets] == [
        "assets/flamelet.png",
        "assets/smokelet.png",
    ]
    smoke = next(record for record in context.assets if record.path.endswith("smokelet.png"))
    assert smoke.kind == "image"
    assert smoke.referenced_by == ("scenes/main.tscn",)
    assert not hasattr(smoke, "content")
    assert context.asset_count_total == 2
    assert context.assets_truncated is False


def test_context_truncates_large_asset_manifest_instead_of_rejecting_project(
    tmp_path: Path,
) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    for index in range(4):
        (assets / f"image-{index}.png").write_bytes(str(index).encode())

    context = build_project_context(
        tmp_path,
        (),
        policy=ContextPolicy(maximum_assets=2),
    )

    assert [record.path for record in context.assets] == [
        "assets/image-0.png",
        "assets/image-1.png",
    ]
    assert context.asset_count_total == 4
    assert context.assets_truncated is True


@pytest.mark.parametrize(
    "path",
    ("../outside.cs", "/tmp/outside.cs", "Assets/texture.exe", ""),
)
def test_context_rejects_unsafe_or_unsupported_paths(tmp_path: Path, path: str) -> None:
    with pytest.raises(ValueError):
        build_project_context(tmp_path, (path,))


def test_context_rejects_symlinks_even_when_target_is_inside_project(tmp_path: Path) -> None:
    target = tmp_path / "Target.cs"
    target.write_text("class Target {}\n", encoding="utf-8")
    link = tmp_path / "Link.cs"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="symbolic link"):
        build_project_context(tmp_path, ("Link.cs",))


def test_project_context_keeps_binary_edit_scope_content_free(tmp_path: Path) -> None:
    material = tmp_path / "materials" / "effect.material"
    material.parent.mkdir(parents=True)
    material.write_bytes(b"RSCC\x00binary")

    context = build_project_context(tmp_path, ("materials/effect.material",))

    assert context.files == ()
    asset = next(item for item in context.assets if item.path == "materials/effect.material")
    assert asset.kind == "material"
    assert asset.sha256 is not None
    assert asset.integrity == "unchecked"


def test_project_context_keeps_explicit_image_edit_scope_content_free(tmp_path: Path) -> None:
    texture = tmp_path / "assets" / "texture.png"
    texture.parent.mkdir(parents=True)
    texture.write_bytes(b"\x89PNG\r\n\x1a\n")

    context = build_project_context(tmp_path, ("assets/texture.png",))

    assert context.files == ()
    asset = next(item for item in context.assets if item.path == "assets/texture.png")
    assert asset.kind == "image"
    assert asset.sha256 is not None
    assert asset.integrity == "invalid"
    assert asset.integrity_detail == "image decode failed: UnidentifiedImageError"


def test_project_context_reports_valid_image_integrity_without_exposing_bytes(
    tmp_path: Path,
) -> None:
    texture = tmp_path / "assets" / "texture.png"
    texture.parent.mkdir(parents=True)
    Image.new("RGBA", (2, 2), (10, 20, 30, 255)).save(texture)

    context = build_project_context(tmp_path, ("assets/texture.png",))

    assert context.files == ()
    asset = next(item for item in context.assets if item.path == "assets/texture.png")
    assert asset.integrity == "valid"
    assert asset.integrity_detail is None


def test_context_enforces_per_file_and_total_limits(tmp_path: Path) -> None:
    (tmp_path / "One.cs").write_text("12345", encoding="utf-8")
    (tmp_path / "Two.cs").write_text("67890", encoding="utf-8")

    with pytest.raises(ValueError, match="file exceeds"):
        build_project_context(
            tmp_path,
            ("One.cs",),
            policy=ContextPolicy(maximum_file_bytes=4),
        )
    with pytest.raises(ValueError, match="total limit"):
        build_project_context(
            tmp_path,
            ("One.cs", "Two.cs"),
            policy=ContextPolicy(maximum_total_bytes=9),
        )
