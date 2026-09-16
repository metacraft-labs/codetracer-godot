extends SceneTree

var frames := 0
var marker := ""
var stop_file := ""


func _initialize() -> void:
	marker = OS.get_environment("CT_HCR_WINDOWS_ATTACH_MARKER")
	stop_file = OS.get_environment("CT_HCR_WINDOWS_ATTACH_STOP")
	if marker.is_empty() or stop_file.is_empty():
		push_error("attach probe requires marker and stop-file environment paths")
		quit(2)


func _process(_delta: float) -> bool:
	frames += 1
	var file := FileAccess.open(marker, FileAccess.WRITE)
	if file:
		file.store_string(JSON.stringify({
			"pid": OS.get_process_id(),
			"frames": frames,
			"engineLive": true,
		}))
	if FileAccess.file_exists(stop_file):
		quit(0)
	return false
