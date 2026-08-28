extends SceneTree

## Independent showcase driver for generated Godot projects.
##
## The driver runs outside the solver and consumes a frozen JSON input trace. Godot's
## MovieWriter records every rendered frame while this script injects normal engine
## InputEvent objects. The generated project is never edited in place.

const MAX_NODES := 1024
const MAX_STATE_RECORDS := 256

var _frame_index := 0
var _action_records: Array[Dictionary] = []


func _initialize() -> void:
	call_deferred("_record_showcase")


func _record_showcase() -> void:
	var args := OS.get_cmdline_user_args()
	if args.size() != 4:
		push_error("SHOWCASE_EXPECTED_TRACE_EVIDENCE_INITIAL_FINAL")
		quit(2)
		return
	var trace_path := args[0]
	var evidence_path := args[1]
	var initial_path := args[2]
	var final_path := args[3]
	var trace := _load_json_object(trace_path)
	if trace.is_empty():
		push_error("SHOWCASE_TRACE_INVALID")
		quit(2)
		return
	var width := int(trace.get("width", 1280))
	var height := int(trace.get("height", 720))
	root.size = Vector2i(width, height)

	var scene_path := str(ProjectSettings.get_setting("application/run/main_scene", ""))
	if scene_path.is_empty() or not ResourceLoader.exists(scene_path):
		push_error("SHOWCASE_MAIN_SCENE_MISSING")
		quit(2)
		return
	var packed := load(scene_path) as PackedScene
	if packed == null:
		push_error("SHOWCASE_MAIN_SCENE_LOAD_FAILED")
		quit(2)
		return
	var scene := packed.instantiate()
	root.add_child(scene)

	await _wait_frames(int(trace.get("settle_frames", 30)))
	var before := _snapshot(scene)
	var initial_error := root.get_texture().get_image().save_png(initial_path)
	var actions: Array = trace.get("actions", [])
	for index in range(actions.size()):
		var action_value = actions[index]
		if typeof(action_value) != TYPE_DICTIONARY:
			push_error("SHOWCASE_ACTION_NOT_OBJECT_%d" % index)
			quit(2)
			return
		var action: Dictionary = action_value
		var started_frame := _frame_index
		var action_error := await _perform_action(action)
		_action_records.append({
			"index": index,
			"type": str(action.get("type", "")),
			"started_frame": started_frame,
			"finished_frame": _frame_index,
			"status": "PASS" if action_error.is_empty() else "ERROR",
			"error": action_error,
		})
		if not action_error.is_empty():
			push_error("SHOWCASE_ACTION_FAILED_%d_%s" % [index, action_error])
			quit(2)
			return

	await _wait_frames(int(trace.get("tail_frames", 90)))
	var after := _snapshot(scene)
	var final_error := root.get_texture().get_image().save_png(final_path)
	var payload := {
		"schema_version": 1,
		"status": "PASS",
		"trace_path": trace_path,
		"showcase_id": str(trace.get("showcase_id", "")),
		"scene_path": scene_path,
		"capture_size": {"x": width, "y": height},
		"rendered_frames": _frame_index,
		"movie_fps": int(trace.get("fps", 30)),
		"before": before,
		"after": after,
		"actions": _action_records,
		"initial_capture_error": initial_error,
		"final_capture_error": final_error,
	}
	var metadata_error := _write_json(evidence_path, payload)
	print("SHOWCASE_STATUS=PASS")
	print("SHOWCASE_RENDERED_FRAMES=", _frame_index)
	print("SHOWCASE_ACTION_COUNT=", _action_records.size())
	quit(0 if initial_error == OK and final_error == OK and metadata_error == OK else 2)


func _perform_action(action: Dictionary) -> String:
	var action_type := str(action.get("type", ""))
	match action_type:
		"wait":
			await _wait_frames(int(action.get("frames", 1)))
		"key":
			var key_names := _string_array(action.get("keys", [action.get("key", "")]))
			if key_names.is_empty():
				return "missing_key"
			var keycodes: Array[Key] = []
			for key_name in key_names:
				var keycode := _keycode(key_name)
				if keycode == KEY_NONE:
					return "unknown_key_%s" % key_name
				keycodes.append(keycode)
			for keycode in keycodes:
				_send_key(keycode, true)
			await _wait_frames(max(1, int(action.get("frames", 2))))
			for keycode in keycodes:
				_send_key(keycode, false)
			await _wait_frames(max(1, int(action.get("release_frames", 1))))
		"mouse_move":
			_send_mouse_motion(Vector2(float(action.get("x", 0)), float(action.get("y", 0))))
			await _wait_frames(max(1, int(action.get("frames", 1))))
		"mouse_click":
			var position := Vector2(float(action.get("x", 0)), float(action.get("y", 0)))
			_send_mouse_motion(position)
			_send_mouse_button(position, true)
			await _wait_frames(max(1, int(action.get("frames", 2))))
			_send_mouse_button(position, false)
			await _wait_frames(max(1, int(action.get("release_frames", 2))))
		_:
			return "unknown_type_%s" % action_type
	return ""


func _wait_frames(frames: int) -> void:
	for _unused in range(max(0, frames)):
		await process_frame
		_frame_index += 1


func _send_key(keycode: Key, pressed: bool) -> void:
	var event := InputEventKey.new()
	event.keycode = keycode
	event.physical_keycode = keycode
	event.pressed = pressed
	event.echo = false
	Input.parse_input_event(event)


func _send_mouse_motion(position: Vector2) -> void:
	var event := InputEventMouseMotion.new()
	event.position = position
	event.global_position = position
	Input.parse_input_event(event)


func _send_mouse_button(position: Vector2, pressed: bool) -> void:
	var event := InputEventMouseButton.new()
	event.position = position
	event.global_position = position
	event.button_index = MOUSE_BUTTON_LEFT
	event.pressed = pressed
	event.button_mask = MOUSE_BUTTON_MASK_LEFT if pressed else 0
	Input.parse_input_event(event)


func _keycode(name: String) -> Key:
	var normalized := name.strip_edges().to_upper()
	var named := {
		"SPACE": KEY_SPACE,
		"ENTER": KEY_ENTER,
		"RETURN": KEY_ENTER,
		"ESC": KEY_ESCAPE,
		"ESCAPE": KEY_ESCAPE,
		"SHIFT": KEY_SHIFT,
		"LEFT": KEY_LEFT,
		"RIGHT": KEY_RIGHT,
		"UP": KEY_UP,
		"DOWN": KEY_DOWN,
		"SLASH": KEY_SLASH,
		"/": KEY_SLASH,
	}
	if named.has(normalized):
		return named[normalized]
	if normalized.length() == 1:
		return OS.find_keycode_from_string(normalized)
	return KEY_NONE


func _string_array(value) -> Array[String]:
	var output: Array[String] = []
	if typeof(value) != TYPE_ARRAY:
		return output
	for item in value:
		var text := str(item)
		if not text.is_empty():
			output.append(text)
	return output


func _load_json_object(path: String) -> Dictionary:
	var file := FileAccess.open(path, FileAccess.READ)
	if file == null:
		return {}
	var parsed = JSON.parse_string(file.get_as_text())
	file.close()
	return parsed if typeof(parsed) == TYPE_DICTIONARY else {}


func _write_json(path: String, payload: Dictionary) -> Error:
	var file := FileAccess.open(path, FileAccess.WRITE)
	if file == null:
		return FileAccess.get_open_error()
	file.store_string(JSON.stringify(payload, "\t"))
	file.close()
	return OK


func _snapshot(scene: Node) -> Dictionary:
	var nodes: Array[Node] = [scene]
	nodes.append_array(scene.find_children("*", "", true, false))
	var class_counts: Dictionary = {}
	var visible_canvas_items := 0
	var state_records: Array[Dictionary] = []
	for index in range(min(nodes.size(), MAX_NODES)):
		var node := nodes[index]
		var node_class := node.get_class()
		class_counts[node_class] = int(class_counts.get(node_class, 0)) + 1
		if node is CanvasItem and (node as CanvasItem).is_visible_in_tree():
			visible_canvas_items += 1
		var record: Dictionary = {}
		if node is Node2D:
			var node_2d := node as Node2D
			record = {
				"path": str(scene.get_path_to(node)),
				"class": node_class,
				"position": {"x": node_2d.global_position.x, "y": node_2d.global_position.y},
				"rotation": node_2d.global_rotation,
			}
		elif node is Control:
			var control := node as Control
			record = {
				"path": str(scene.get_path_to(node)),
				"class": node_class,
				"position": {"x": control.global_position.x, "y": control.global_position.y},
				"size": {"x": control.size.x, "y": control.size.y},
			}
		if node is Label:
			record["text"] = (node as Label).text
		elif node is RichTextLabel:
			record["text"] = (node as RichTextLabel).text
		elif node is Button:
			record["text"] = (node as Button).text
		if not record.is_empty():
			state_records.append(record)
			if state_records.size() >= MAX_STATE_RECORDS:
				break
	return {
		"node_count": nodes.size(),
		"nodes_truncated": nodes.size() > MAX_NODES,
		"class_counts": class_counts,
		"visible_canvas_items": visible_canvas_items,
		"state_records": state_records,
	}
