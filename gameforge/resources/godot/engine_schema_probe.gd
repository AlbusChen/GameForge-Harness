extends SceneTree

const MAXIMUM_ITEMS := 256


func _initialize() -> void:
	var arguments := OS.get_cmdline_user_args()
	if arguments.size() != 2:
		_fail("expected request and output paths")
		return
	var request: Variant = JSON.parse_string(FileAccess.get_file_as_string(arguments[0]))
	if typeof(request) != TYPE_DICTIONARY:
		_fail("request is not an object")
		return
	var requested_class: String = str(request.get("class_name", ""))
	if requested_class.is_empty() or not ClassDB.class_exists(requested_class):
		_fail("engine class does not exist: " + requested_class)
		return
	var include_inherited: bool = bool(request.get("include_inherited", true))
	var name_filter: String = str(request.get("name_filter", "")).to_lower()
	var properties: Array = _filter_named(
		ClassDB.class_get_property_list(requested_class, not include_inherited),
		name_filter
	)
	var methods: Array = _filter_named(
		ClassDB.class_get_method_list(requested_class, not include_inherited),
		name_filter
	)
	var signals: Array = _filter_named(
		ClassDB.class_get_signal_list(requested_class, not include_inherited),
		name_filter
	)
	var payload := {
		"schema_version": 1,
		"class_name": requested_class,
		"parent_class": ClassDB.get_parent_class(requested_class),
		"include_inherited": include_inherited,
		"name_filter": name_filter,
		"properties_total": properties.size(),
		"methods_total": methods.size(),
		"signals_total": signals.size(),
		"properties_truncated": properties.size() > MAXIMUM_ITEMS,
		"methods_truncated": methods.size() > MAXIMUM_ITEMS,
		"signals_truncated": signals.size() > MAXIMUM_ITEMS,
		"properties": _sanitize_array(properties.slice(0, MAXIMUM_ITEMS)),
		"methods": _sanitize_array(methods.slice(0, MAXIMUM_ITEMS)),
		"signals": _sanitize_array(signals.slice(0, MAXIMUM_ITEMS)),
	}
	var output := FileAccess.open(str(arguments[1]), FileAccess.WRITE)
	if output == null:
		_fail("could not open output path")
		return
	output.store_string(JSON.stringify(payload, "\t"))
	output.close()
	print("GAMEFORGE_ENGINE_SCHEMA_OK")
	quit(0)


func _filter_named(items: Array, name_filter: String) -> Array:
	if name_filter.is_empty():
		return items
	var selected: Array = []
	for item in items:
		if typeof(item) != TYPE_DICTIONARY:
			continue
		if name_filter in str(item.get("name", "")).to_lower():
			selected.append(item)
	return selected


func _sanitize_array(items: Array) -> Array:
	var result: Array = []
	for item in items:
		result.append(_sanitize(item))
	return result


func _sanitize(value):
	match typeof(value):
		TYPE_NIL, TYPE_BOOL, TYPE_INT, TYPE_FLOAT, TYPE_STRING:
			return value
		TYPE_ARRAY:
			var result: Array = []
			for item in value:
				result.append(_sanitize(item))
			return result
		TYPE_DICTIONARY:
			var result := {}
			for key in value:
				result[str(key)] = _sanitize(value[key])
			return result
		_:
			return str(value)


func _fail(message: String) -> void:
	push_error("GAMEFORGE_ENGINE_SCHEMA_FAILED: " + message)
	quit(2)
