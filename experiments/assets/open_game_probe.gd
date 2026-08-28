extends SceneTree

const WIDTH := 1280
const HEIGHT := 720
const SETTLE_FRAMES := 12
const INPUT_FRAMES := 120
const MAX_NODES := 1024


func _initialize() -> void:
	root.size = Vector2i(WIDTH, HEIGHT)
	call_deferred("_probe")


func _probe() -> void:
	var args := OS.get_cmdline_user_args()
	if args.size() != 3:
		push_error("OPEN_PROBE_EXPECTED_INITIAL_FINAL_METADATA")
		quit(2)
		return
	var scene_path := str(ProjectSettings.get_setting("application/run/main_scene", ""))
	if scene_path.is_empty() or not ResourceLoader.exists(scene_path):
		push_error("OPEN_PROBE_MAIN_SCENE_MISSING")
		quit(2)
		return
	var packed := load(scene_path) as PackedScene
	if packed == null:
		push_error("OPEN_PROBE_MAIN_SCENE_LOAD_FAILED")
		quit(2)
		return
	var scene := packed.instantiate()
	root.add_child(scene)
	for _frame in range(SETTLE_FRAMES):
		await process_frame
	var before := _snapshot(scene)
	var initial_error := root.get_texture().get_image().save_png(args[0])
	await _exercise_inputs()
	var after := _snapshot(scene)
	var final_error := root.get_texture().get_image().save_png(args[1])
	var payload := {
		"schema_version": 1,
		"scene_path": scene_path,
		"capture_size": {"x": WIDTH, "y": HEIGHT},
		"settle_frames": SETTLE_FRAMES,
		"input_frames": INPUT_FRAMES,
		"before": before,
		"after": after,
	}
	var file := FileAccess.open(args[2], FileAccess.WRITE)
	var metadata_error := FileAccess.get_open_error() if file == null else OK
	if file != null:
		file.store_string(JSON.stringify(payload, "\t"))
		file.close()
	print("OPEN_PROBE_INITIAL_RESULT=", initial_error)
	print("OPEN_PROBE_FINAL_RESULT=", final_error)
	print("OPEN_PROBE_METADATA_RESULT=", metadata_error)
	quit(0 if initial_error == OK and final_error == OK and metadata_error == OK else 2)


func _exercise_inputs() -> void:
	await _tap_key(KEY_ENTER, KEY_ENTER)
	await _tap_key(KEY_SPACE, KEY_SPACE)
	await _mouse_click(Vector2(WIDTH * 0.5, HEIGHT * 0.5))
	await _mouse_click(Vector2(WIDTH * 0.25, HEIGHT * 0.65))
	await _hold_key(KEY_D, KEY_D, 24)
	await _hold_key(KEY_RIGHT, KEY_RIGHT, 24)
	await _hold_key(KEY_W, KEY_W, 18)
	await _hold_key(KEY_UP, KEY_UP, 18)
	await _tap_key(KEY_SPACE, KEY_SPACE)
	for _frame in range(INPUT_FRAMES):
		await process_frame


func _tap_key(keycode: Key, physical: Key) -> void:
	await _hold_key(keycode, physical, 2)


func _hold_key(keycode: Key, physical: Key, frames: int) -> void:
	var pressed := InputEventKey.new()
	pressed.keycode = keycode
	pressed.physical_keycode = physical
	pressed.pressed = true
	Input.parse_input_event(pressed)
	for _frame in range(frames):
		await process_frame
	var released := pressed.duplicate()
	released.pressed = false
	Input.parse_input_event(released)
	await process_frame


func _mouse_click(position: Vector2) -> void:
	var motion := InputEventMouseMotion.new()
	motion.position = position
	motion.global_position = position
	Input.parse_input_event(motion)
	var pressed := InputEventMouseButton.new()
	pressed.position = position
	pressed.global_position = position
	pressed.button_index = MOUSE_BUTTON_LEFT
	pressed.pressed = true
	Input.parse_input_event(pressed)
	await process_frame
	var released := pressed.duplicate()
	released.pressed = false
	Input.parse_input_event(released)
	for _frame in range(3):
		await process_frame


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
		if node is Node2D:
			var node_2d := node as Node2D
			state_records.append({
				"path": str(scene.get_path_to(node)),
				"class": node_class,
				"position": {"x": node_2d.global_position.x, "y": node_2d.global_position.y},
				"rotation": node_2d.global_rotation,
			})
		elif node is Control:
			var control := node as Control
			state_records.append({
				"path": str(scene.get_path_to(node)),
				"class": node_class,
				"position": {"x": control.global_position.x, "y": control.global_position.y},
				"size": {"x": control.size.x, "y": control.size.y},
			})
		if state_records.size() >= 256:
			break
	return {
		"node_count": nodes.size(),
		"nodes_truncated": nodes.size() > MAX_NODES,
		"class_counts": class_counts,
		"visible_canvas_items": visible_canvas_items,
		"state_records": state_records,
	}
