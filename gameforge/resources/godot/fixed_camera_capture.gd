extends SceneTree

const WIDTH := 1280
const HEIGHT := 720
const SETTLE_FRAMES := 3
const MAX_NODES := 512
const MAX_ANIMATIONS_PER_NODE := 64
const MAX_ANIMATION_FRAMES_PER_NODE := 512


func _initialize() -> void:
	root.size = Vector2i(WIDTH, HEIGHT)
	call_deferred("_capture")


func _capture() -> void:
	var scene_path := str(ProjectSettings.get_setting("application/run/main_scene", ""))
	if scene_path.is_empty():
		for candidate in ["res://scenes/main.tscn", "res://scenes/Main.tscn"]:
			if ResourceLoader.exists(candidate):
				scene_path = candidate
				break
	if scene_path.is_empty():
		push_error("FIXED_CAPTURE_MAIN_SCENE_MISSING")
		quit(2)
		return
	var packed := load(scene_path) as PackedScene
	if packed == null:
		push_error("FIXED_CAPTURE_MAIN_SCENE_LOAD_FAILED")
		quit(2)
		return
	var scene := packed.instantiate()
	root.add_child(scene)
	for _frame in range(SETTLE_FRAMES):
		await process_frame
	var arguments := OS.get_cmdline_user_args()
	if arguments.size() != 2:
		push_error("FIXED_CAPTURE_EXPECTED_IMAGE_AND_METADATA_PATHS")
		quit(2)
		return
	var image := root.get_texture().get_image()
	var error := image.save_png(arguments[0])
	var metadata_error := _save_metadata(arguments[1], scene, scene_path, image)
	print("FIXED_CAPTURE_SIZE=", image.get_width(), "x", image.get_height())
	print("FIXED_CAPTURE_RESULT=", error)
	print("FIXED_CAPTURE_METADATA_RESULT=", metadata_error)
	quit(0 if error == OK and metadata_error == OK else 2)


func _save_metadata(path: String, scene: Node, scene_path: String, image: Image) -> Error:
	var nodes: Array = [scene]
	nodes.append_array(scene.find_children("*", "", true, false))
	var records: Array[Dictionary] = []
	for index in range(min(nodes.size(), MAX_NODES)):
		var node := nodes[index] as Node
		if node == null:
			continue
		records.append(_node_record(scene, node))
	var visible_rect := root.get_visible_rect()
	var payload := {
		"schema_version": 1,
		"scene_path": scene_path,
		"capture_size": _vector2i(image.get_size()),
		"viewport": {
			"size": _vector2i(root.size),
			"visible_rect": _rect2(visible_rect),
			"content_scale_factor": root.content_scale_factor,
			"content_scale_mode": root.content_scale_mode,
			"content_scale_aspect": root.content_scale_aspect,
		},
		"settle_frames": SETTLE_FRAMES,
		"state_phase": "runtime_after_scene_scripts_and_settle_frames",
		"node_count_total": nodes.size(),
		"nodes_truncated": nodes.size() > MAX_NODES,
		"nodes": records,
		"coordinate_contract": {
			"local": "serialized node-local coordinates",
			"canvas": "CanvasItem global_transform coordinates before viewport/camera projection",
			"capture": "1280x720 viewport pixels after active CanvasItem/Camera2D transforms",
		},
	}
	var file := FileAccess.open(path, FileAccess.WRITE)
	if file == null:
		return FileAccess.get_open_error()
	file.store_string(JSON.stringify(payload, "\t"))
	file.close()
	return OK


func _node_record(scene: Node, node: Node) -> Dictionary:
	var relative_path := "." if node == scene else str(scene.get_path_to(node))
	var record := {
		"path": relative_path,
		"class": node.get_class(),
	}
	if node is CanvasItem:
		var canvas_item := node as CanvasItem
		record["visible"] = canvas_item.visible
		record["global_transform"] = _transform2d(canvas_item.get_global_transform())
	if node is Node2D:
		var node_2d := node as Node2D
		record["node_2d"] = {
			"position": _vector2(node_2d.position),
			"rotation": node_2d.rotation,
			"scale": _vector2(node_2d.scale),
			"global_position": _vector2(node_2d.global_position),
			"global_rotation": node_2d.global_rotation,
			"global_scale": _vector2(node_2d.global_scale),
		}
	if node is Control:
		var control := node as Control
		record["control"] = {
			"position": _vector2(control.position),
			"size": _vector2(control.size),
			"global_position": _vector2(control.global_position),
			"global_rect": _rect2(control.get_global_rect()),
		}
	if node is Sprite2D:
		record["sprite_2d"] = _sprite_record(node as Sprite2D)
	if node is AnimatedSprite2D:
		record["animated_sprite_2d"] = _animated_sprite_record(node as AnimatedSprite2D)
	if node is TextureRect:
		record["texture_control"] = {
			"slots": {"texture": _texture_record((node as TextureRect).texture)},
		}
	elif node is NinePatchRect:
		var nine_patch := node as NinePatchRect
		record["texture_control"] = {
			"slots": {"texture": _texture_record(nine_patch.texture)},
			"draw_center": nine_patch.draw_center,
			"patch_margins": {
				"left": nine_patch.get_patch_margin(SIDE_LEFT),
				"top": nine_patch.get_patch_margin(SIDE_TOP),
				"right": nine_patch.get_patch_margin(SIDE_RIGHT),
				"bottom": nine_patch.get_patch_margin(SIDE_BOTTOM),
			},
		}
	elif node is TextureButton:
		var texture_button := node as TextureButton
		record["texture_control"] = {
			"slots": {
				"normal": _texture_record(texture_button.texture_normal),
				"pressed": _texture_record(texture_button.texture_pressed),
				"hover": _texture_record(texture_button.texture_hover),
				"disabled": _texture_record(texture_button.texture_disabled),
				"focused": _texture_record(texture_button.texture_focused),
			},
		}
	if node is Polygon2D:
		record["polygon_2d"] = _polygon_record(node as Polygon2D)
	if node is Camera2D:
		var camera := node as Camera2D
		record["camera_2d"] = {
			"enabled": camera.enabled,
			"zoom": _vector2(camera.zoom),
			"screen_center_position": _vector2(camera.get_screen_center_position()),
		}
	return record


func _sprite_record(sprite: Sprite2D) -> Dictionary:
	var local_rect := sprite.get_rect()
	return {
		"texture": _texture_record(sprite.texture),
		"texture_path": sprite.texture.resource_path if sprite.texture != null else "",
		"texture_size": _vector2(sprite.texture.get_size() if sprite.texture != null else Vector2.ZERO),
		"centered": sprite.centered,
		"offset": _vector2(sprite.offset),
		"region_enabled": sprite.region_enabled,
		"region_rect": _rect2(sprite.region_rect),
		"hframes": sprite.hframes,
		"vframes": sprite.vframes,
		"frame": sprite.frame,
		"frame_coords": _vector2i(sprite.frame_coords),
		"local_rect": _rect2(local_rect),
		"canvas_quad": _transformed_rect(sprite.get_global_transform(), local_rect),
	}


func _animated_sprite_record(sprite: AnimatedSprite2D) -> Dictionary:
	var frames := sprite.sprite_frames
	if frames == null:
		return {
			"animation": str(sprite.animation),
			"frame": sprite.frame,
			"playing": sprite.is_playing(),
			"speed_scale": sprite.speed_scale,
			"sprite_frames": {"assigned": false},
		}
	var animations: Array[Dictionary] = []
	var animation_names := frames.get_animation_names()
	var frames_remaining := MAX_ANIMATION_FRAMES_PER_NODE
	for animation_index in range(min(animation_names.size(), MAX_ANIMATIONS_PER_NODE)):
		var animation_name: StringName = animation_names[animation_index]
		var frame_count := frames.get_frame_count(animation_name)
		var returned_count: int = min(frame_count, frames_remaining)
		var frame_records: Array[Dictionary] = []
		for frame_index in range(returned_count):
			frame_records.append({
				"index": frame_index,
				"duration": frames.get_frame_duration(animation_name, frame_index),
				"texture": _texture_record(frames.get_frame_texture(animation_name, frame_index)),
			})
		frames_remaining -= returned_count
		animations.append({
			"name": str(animation_name),
			"loop": frames.get_animation_loop(animation_name),
			"speed": frames.get_animation_speed(animation_name),
			"frame_count": frame_count,
			"frames_returned": returned_count,
			"frames_truncated": returned_count < frame_count,
			"frames": frame_records,
		})
		if frames_remaining <= 0:
			break
	return {
		"animation": str(sprite.animation),
		"autoplay": str(sprite.autoplay),
		"frame": sprite.frame,
		"frame_progress": sprite.frame_progress,
		"playing": sprite.is_playing(),
		"speed_scale": sprite.speed_scale,
		"sprite_frames": {
			"resource_path": frames.resource_path,
			"resource_name": frames.resource_name,
			"animation_count": animation_names.size(),
			"animations_returned": animations.size(),
			"animations_truncated": animations.size() < animation_names.size(),
			"frame_provenance_limit": MAX_ANIMATION_FRAMES_PER_NODE,
			"animations": animations,
		},
	}


func _texture_record(texture: Texture2D, depth: int = 0) -> Dictionary:
	if texture == null:
		return {"assigned": false}
	var record := {
		"assigned": true,
		"class": texture.get_class(),
		"resource_path": texture.resource_path,
		"resource_name": texture.resource_name,
		"size": _vector2(texture.get_size()),
		"origin": (
			"external_resource_path"
			if not texture.resource_path.is_empty()
			else "embedded_or_runtime_resource_without_external_path"
		),
	}
	if texture is AtlasTexture:
		var atlas_texture := texture as AtlasTexture
		record["atlas_texture"] = {
			"region": _rect2(atlas_texture.region),
			"margin": _rect2(atlas_texture.margin),
			"filter_clip": atlas_texture.filter_clip,
			"atlas": (
				_texture_record(atlas_texture.atlas, depth + 1)
				if depth < 2
				else {"truncated": true}
			),
		}
	return record


func _polygon_record(polygon: Polygon2D) -> Dictionary:
	var local_bounds := _points_bounds(polygon.polygon)
	var canvas_points: Array[Dictionary] = []
	for point in polygon.polygon:
		canvas_points.append(_vector2(polygon.get_global_transform() * point))
	return {
		"point_count": polygon.polygon.size(),
		"local_bounds": _rect2(local_bounds),
		"canvas_bounds": _dictionary_points_bounds(canvas_points),
	}


func _transformed_rect(transform: Transform2D, rect: Rect2) -> Array[Dictionary]:
	var points: Array[Dictionary] = []
	for point in [
		rect.position,
		rect.position + Vector2(rect.size.x, 0),
		rect.end,
		rect.position + Vector2(0, rect.size.y),
	]:
		points.append(_vector2(transform * point))
	return points


func _points_bounds(points: PackedVector2Array) -> Rect2:
	if points.is_empty():
		return Rect2()
	var minimum := points[0]
	var maximum := points[0]
	for point in points:
		minimum = minimum.min(point)
		maximum = maximum.max(point)
	return Rect2(minimum, maximum - minimum)


func _dictionary_points_bounds(points: Array[Dictionary]) -> Dictionary:
	if points.is_empty():
		return _rect2(Rect2())
	var minimum := Vector2(float(points[0]["x"]), float(points[0]["y"]))
	var maximum := minimum
	for item in points:
		var point := Vector2(float(item["x"]), float(item["y"]))
		minimum = minimum.min(point)
		maximum = maximum.max(point)
	return _rect2(Rect2(minimum, maximum - minimum))


func _vector2(value: Vector2) -> Dictionary:
	return {"x": value.x, "y": value.y}


func _vector2i(value: Vector2i) -> Dictionary:
	return {"x": value.x, "y": value.y}


func _rect2(value: Rect2) -> Dictionary:
	return {
		"x": value.position.x,
		"y": value.position.y,
		"width": value.size.x,
		"height": value.size.y,
	}


func _transform2d(value: Transform2D) -> Dictionary:
	return {
		"x_axis": _vector2(value.x),
		"y_axis": _vector2(value.y),
		"origin": _vector2(value.origin),
	}
