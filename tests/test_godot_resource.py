from __future__ import annotations

from pathlib import Path

import pytest

from gameforge.adapters.godot_resource import (
    GodotResourceDocument,
    ResourceMutationRequest,
)
from gameforge.benchmarking.gamedevbench import (
    GodotHarnessEngineAdapter,
    _synchronize_script_node_references,
)


def test_object_mutation_preserves_unrelated_scene_sections() -> None:
    source = (
        '[gd_scene load_steps=2 format=3]\n\n'
        '[ext_resource type="Texture2D" path="res://weapon.png" id="1"]\n\n'
        '[node name="Arena" type="Node2D"]\n'
        'position = Vector2(4, 8)\n\n'
        '[node name="Weapon" type="Sprite2D" parent="."]\n'
        'texture = ExtResource("1")\n'
        'damage = 10\n'
    )
    document = GodotResourceDocument.parse(source)

    changes = document.mutate(
        ResourceMutationRequest.model_validate(
            {
                "operations": [
                    {
                        "kind": "set_properties",
                        "selector": {
                            "section": "node",
                            "attributes": {"name": "Weapon", "parent": "."},
                        },
                        "properties": {"damage": "25"},
                    }
                ]
            }
        )
    )

    rendered = document.render()
    assert changes[0]["properties_before"] == {"damage": "10"}
    assert changes[0]["properties_after"] == {"damage": "25"}
    assert '[ext_resource type="Texture2D" path="res://weapon.png" id="1"]' in rendered
    assert 'position = Vector2(4, 8)' in rendered
    assert 'texture = ExtResource("1")' in rendered
    assert "damage = 25" in rendered
    assert all(assertion["passed"] for assertion in document.structural_assertions())


def test_object_mutation_handles_a_different_resource_shape() -> None:
    source = (
        '[gd_resource type="StyleBoxFlat" load_steps=2 format=3]\n\n'
        '[sub_resource type="Gradient" id="Gradient_health"]\n'
        'offsets = PackedFloat32Array(0, 1)\n'
        'colors = PackedColorArray(1, 0, 0, 1, 0, 1, 0, 1)\n\n'
        '[resource]\n'
        'bg_color = Color(0.1, 0.1, 0.1, 1)\n'
        'border_width_left = 2\n'
    )
    document = GodotResourceDocument.parse(source)

    document.mutate(
        ResourceMutationRequest.model_validate(
            {
                "operations": [
                    {
                        "kind": "set_properties",
                        "selector": {"section": "resource", "attributes": {}},
                        "properties": {
                            "bg_color": "Color(0.02, 0.02, 0.02, 1)",
                            "border_width_right": "2",
                        },
                    }
                ]
            }
        )
    )

    rendered = document.render()
    assert "offsets = PackedFloat32Array(0, 1)" in rendered
    assert "colors = PackedColorArray(1, 0, 0, 1, 0, 1, 0, 1)" in rendered
    assert "bg_color = Color(0.02, 0.02, 0.02, 1)" in rendered
    assert "border_width_left = 2" in rendered
    assert "border_width_right = 2" in rendered


def test_object_mutation_updates_header_attributes_without_rewriting_others() -> None:
    source = (
        '[gd_scene load_steps=3 format=3 uid="uid://arena"]\n\n'
        '[ext_resource type="Texture2D" path="res://arena.png" id="1"]\n\n'
        '[node name="Arena" type="Node2D"]\n'
    )
    document = GodotResourceDocument.parse(source)

    changes = document.mutate(
        ResourceMutationRequest.model_validate(
            {
                "operations": [
                    {
                        "kind": "set_attributes",
                        "selector": {
                            "section": "gd_scene",
                            "attributes": {"format": "3"},
                        },
                        "attributes": {"load_steps": 4},
                    }
                ]
            }
        )
    )

    rendered = document.render()
    assert changes[0]["attributes_before"] == {"load_steps": "3"}
    assert changes[0]["attributes_after"] == {"load_steps": "4"}
    assert '[gd_scene load_steps=4 format=3 uid="uid://arena"]' in rendered
    assert '[ext_resource type="Texture2D" path="res://arena.png" id="1"]' in rendered


def test_object_mutation_accepts_empty_section_body_and_primitive_values() -> None:
    document = GodotResourceDocument.parse(
        '[gd_scene load_steps=1 format=3]\n\n[node name="Root" type="Node2D"]\n'
    )

    document.mutate(
        ResourceMutationRequest.model_validate(
            {
                "operations": [
                    {
                        "kind": "append_section",
                        "section": "ext_resource",
                        "attributes": {
                            "type": "Texture2D",
                            "path": "res://icon.png",
                            "id": "1",
                        },
                        "properties": {},
                    },
                    {
                        "kind": "set_properties",
                        "selector": {
                            "section": "node",
                            "attributes": {"name": "Root"},
                        },
                        "properties": {"visible": True, "z_index": 4, "scale_ratio": 1.5},
                    },
                ]
            }
        )
    )

    rendered = document.render()
    assert '[ext_resource type="Texture2D" path="res://icon.png" id="1"]' in rendered
    assert rendered.index("[ext_resource") < rendered.index("[node")
    assert "visible = true" in rendered
    assert "z_index = 4" in rendered
    assert "scale_ratio = 1.5" in rendered
    assert all(assertion["passed"] for assertion in document.structural_assertions())


def test_append_node_preserves_validated_header_expressions_without_double_quoting() -> None:
    document = GodotResourceDocument.parse(
        "[gd_scene load_steps=2 format=3]\n\n"
        '[ext_resource type="PackedScene" path="res://child.tscn" id="1_child"]\n\n'
        '[node name="Root" type="Node"]\n'
    )

    document.mutate(
        ResourceMutationRequest.model_validate(
            {
                "operations": [
                    {
                        "kind": "append_section",
                        "section": "node",
                        "attributes": {
                            "name": "Child",
                            "parent": ".",
                            "instance": 'ExtResource("1_child")',
                            "groups": '["Actors", "Targets"]',
                        },
                        "properties": {"priority": 3},
                    }
                ]
            }
        )
    )

    rendered = document.render()
    assert 'instance=ExtResource("1_child")' in rendered
    assert 'instance="ExtResource' not in rendered
    assert 'groups=["Actors", "Targets"]' in rendered
    reparsed = GodotResourceDocument.parse(rendered)
    child = next(
        section
        for section in reparsed.sections
        if section.attributes.get("name") == "Child"
    )
    assert child.attributes["instance"] == 'ExtResource("1_child")'
    assert child.attributes["groups"] == '["Actors", "Targets"]'


def test_header_expression_allowlist_quotes_ambiguous_or_injected_strings() -> None:
    document = GodotResourceDocument.parse(
        '[gd_scene format=3]\n\n[node name="Root" type="Node"]\n'
    )

    document.mutate(
        ResourceMutationRequest.model_validate(
            {
                "operations": [
                    {
                        "kind": "set_attributes",
                        "selector": {"section": "node", "attributes": {"name": "Root"}},
                        "attributes": {
                            "name": 'ExtResource("not-an-instance") trailing',
                        },
                    }
                ]
            }
        )
    )

    rendered = document.render()
    assert 'name="ExtResource(\\"not-an-instance\\") trailing"' in rendered
    assert len(GodotResourceDocument.parse(rendered).sections) == 2


def test_node_reference_metadata_merges_without_quoting_packed_array() -> None:
    document = GodotResourceDocument.parse(
        "[gd_scene format=3]\n\n"
        '[node name="Root" type="Node"]\n\n'
        '[node name="Worker" type="Node" parent="." '
        'node_paths=PackedStringArray("existing")]\n'
        'existing = NodePath("..")\n'
        'debug_label = NodePath("../Label")\n'
    )
    section = next(
        section
        for section in document.sections
        if section.section == "node" and section.attributes.get("name") == "Worker"
    )
    change = document.ensure_node_reference_metadata(section, ("debug_label", "actor"))

    assert change is not None
    assert change["node_paths_before"] == ["existing"]
    assert change["node_paths_after"] == ["existing", "debug_label", "actor"]
    assert 'node_paths=PackedStringArray("existing", "debug_label", "actor")' in document.render()
    assert 'node_paths="PackedStringArray' not in document.render()


def test_script_export_sync_distinguishes_node_references_from_native_node_paths(
    tmp_path: Path,
) -> None:
    script = tmp_path / "scripts" / "worker.gd"
    script.parent.mkdir(parents=True)
    script.write_text(
        "extends Node\n"
        "@export var debug_label: Label\n"
        '@export_node_path("Node") var target_path: NodePath\n',
        encoding="utf-8",
    )
    document = GodotResourceDocument.parse(
        "[gd_scene load_steps=2 format=3]\n\n"
        '[ext_resource type="Script" path="res://scripts/worker.gd" id="1"]\n\n'
        '[node name="Worker" type="Node"]\n'
        'script = ExtResource("1")\n'
        'debug_label = NodePath("Label")\n'
        'target_path = NodePath("Target")\n'
    )

    changes = _synchronize_script_node_references(document, tmp_path)

    assert len(changes) == 1
    assert changes[0]["node_paths_after"] == ["debug_label"]
    assert 'node_paths=PackedStringArray("debug_label")' in document.render()
    assert 'node_paths=PackedStringArray("debug_label", "target_path")' not in document.render()


def test_adapter_reconciles_node_reference_metadata_after_general_write(
    tmp_path: Path,
) -> None:
    script = tmp_path / "scripts" / "worker.gd"
    scene = tmp_path / "scenes" / "main.tscn"
    script.parent.mkdir(parents=True)
    scene.parent.mkdir(parents=True)
    script.write_text(
        "extends Node\n@export var actor: Node\n@export var debug_label: Label\n",
        encoding="utf-8",
    )
    scene.write_text(
        "[gd_scene load_steps=2 format=3]\n\n"
        '[ext_resource type="Script" path="res://scripts/worker.gd" id="1"]\n\n'
        '[node name="Root" type="Node"]\n\n'
        '[node name="Worker" type="Node" parent="."]\n'
        'script = ExtResource("1")\n'
        'actor = NodePath("..")\n'
        'debug_label = NodePath("../Label")\n',
        encoding="utf-8",
    )
    adapter = GodotHarnessEngineAdapter(
        project=tmp_path,
        run_directory=tmp_path / "run",
        editable_paths=("scenes/main.tscn",),
        godot=tmp_path / "godot",
    )

    report = adapter.reconcile_project_changes(("scenes/main.tscn",))

    assert report["changed_resource_count"] == 1
    assert report["metadata_change_count"] == 1
    rendered = scene.read_text(encoding="utf-8")
    assert 'node_paths=PackedStringArray("actor", "debug_label")' in rendered


def test_resource_mutation_allows_large_bounded_engine_property_values() -> None:
    large_value = "[" + ("1," * 20_000) + "1]"

    request = ResourceMutationRequest.model_validate(
        {
            "operations": [
                {
                    "kind": "set_properties",
                    "selector": {
                        "section": "sub_resource",
                        "attributes": {"id": "Frames"},
                    },
                    "properties": {"animations": large_value},
                }
            ]
        }
    )

    assert request.operations[0].properties["animations"] == large_value
    with pytest.raises(ValueError, match="value exceeds"):
        ResourceMutationRequest.model_validate(
            {
                "operations": [
                    {
                        "kind": "set_properties",
                        "selector": {
                            "section": "sub_resource",
                            "attributes": {"id": "Frames"},
                        },
                        "properties": {"animations": "x" * (1024 * 1024 + 1)},
                    }
                ]
            }
        )


def test_resource_mutation_normalizes_multiline_property_expressions() -> None:
    document = GodotResourceDocument.parse(
        '[gd_scene format=3]\n\n[node name="Root" type="Node"]\nvalues = []\n'
    )

    document.mutate(
        ResourceMutationRequest.model_validate(
            {
                "operations": [
                    {
                        "kind": "set_properties",
                        "selector": {
                            "section": "node",
                            "attributes": {"name": "Root"},
                        },
                        "properties": {"values": "[\n1,\n2,\n3\n]"},
                    }
                ]
            }
        )
    )

    assert "values = [ 1, 2, 3 ]" in document.render()


def test_resource_assertions_reject_declarations_after_consumers() -> None:
    document = GodotResourceDocument.parse(
        '[gd_scene format=3]\n\n'
        '[sub_resource type="ShaderMaterial" id="Material"]\n'
        'shader = ExtResource("Shader")\n\n'
        '[ext_resource type="Shader" path="res://shader.gdshader" id="Shader"]\n'
    )

    ordering = next(
        assertion
        for assertion in document.structural_assertions()
        if assertion["id"] == "canonical_section_order"
    )

    assert ordering["passed"] is False


def test_object_mutation_orders_new_subresource_before_existing_consumer() -> None:
    document = GodotResourceDocument.parse(
        '[gd_scene load_steps=2 format=3]\n\n'
        '[sub_resource type="SphereMesh" id="Sphere"]\n\n'
        '[node name="Root" type="MeshInstance3D"]\n'
        'mesh = SubResource("Sphere")\n'
    )

    document.mutate(
        ResourceMutationRequest.model_validate(
            {
                "operations": [
                    {
                        "kind": "append_section",
                        "section": "sub_resource",
                        "attributes": {"type": "ShaderMaterial", "id": "Material"},
                        "properties": {},
                    },
                    {
                        "kind": "set_properties",
                        "selector": {
                            "section": "sub_resource",
                            "attributes": {"id": "Sphere"},
                        },
                        "properties": {"material": 'SubResource("Material")'},
                    },
                ]
            }
        )
    )

    rendered = document.render()
    assert rendered.index('id="Material"') < rendered.index('id="Sphere"')
    assert all(assertion["passed"] for assertion in document.structural_assertions())


def test_adapter_object_mutation_is_atomic_when_any_selector_fails(tmp_path: Path) -> None:
    scene = tmp_path / "scenes" / "main.tscn"
    scene.parent.mkdir(parents=True)
    original = (
        '[gd_scene format=3]\n\n'
        '[node name="Root" type="Node2D"]\n'
        'position = Vector2(0, 0)\n'
    )
    scene.write_text(original, encoding="utf-8")
    adapter = GodotHarnessEngineAdapter(
        project=tmp_path,
        run_directory=tmp_path / "run",
        editable_paths=("scenes/main.tscn",),
        godot=tmp_path / "godot",
    )

    with pytest.raises(ValueError, match="missing"):
        adapter.invoke(
            "mutate_resource_objects",
            {
                "path": "scenes/main.tscn",
                "operations": [
                    {
                        "kind": "set_properties",
                        "selector": {
                            "section": "node",
                            "attributes": {"name": "Root"},
                        },
                        "properties": {"position": "Vector2(5, 5)"},
                    },
                    {
                        "kind": "set_properties",
                        "selector": {
                            "section": "node",
                            "attributes": {"name": "Missing"},
                        },
                        "properties": {"visible": "true"},
                    },
                ],
            },
        )

    assert scene.read_text(encoding="utf-8") == original
