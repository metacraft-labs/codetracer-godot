# CodeTracer GDScript recorder — MT14 (real combined-trace substrate) demo.
#
# Deterministic, headless, no clock and no RNG: every printed marker below is
# derivable from the literals in SAMPLES alone, so the recording is comparable
# across runs and hosts.
#
#   CT_GDSCRIPT_TRACE=<dir> \
#     ct-mcr record --output <native.ct> -- \
#       bin/godot.linuxbsd.template_debug.x86_64 --headless \
#         --path test-programs/mt14 --script res://mt14_mixed.gd
#
# WHY THIS PROGRAM (and not one of the GT1 corpus programs).  MT14 is about the
# native<->VM boundary, so the demo has to make that boundary happen in BOTH
# directions and more than once:
#
#   * VM -> native.  `Array.duplicate`, `Array.sort_custom`, `Array.map`,
#     `Array.size`, `FileAccess.open/store_line/close` and `print` all compile
#     to OPCODE_CALL / method-bind — the VM leaves GDScript into engine C++.
#     `FileAccess` and `print` additionally make the engine issue real
#     open/write/close syscalls, so the accompanying NATIVE recording is not
#     an empty process.
#
#   * native -> VM.  This is the direction the GT1 corpus never exercised and
#     the one the crossing spans exist for.  `sort_custom` and `map` take a
#     Callable and the engine's own C++ implementation invokes it: every
#     `_by_descending` / `_double` invocation is a fresh `GDScriptFunction::call`
#     frame ENTERED FROM NATIVE CODE, so the recorder opens a crossing span
#     whose parent is native rather than another GDScript frame.
#
#   * VM -> VM.  `_summarise` is a plain gd->gd call, so the trace also carries
#     crossings nested inside another crossing, to contrast with the ones the
#     engine entered.
#
# Steps, calls/returns and values are all exercised: int, float, bool, String,
# Array, Dictionary and Vector2 locals are written on named stack slots, and the
# `for` loop gives the step stream repeated lines.
#
# DERIVED BY HAND FROM THE SOURCE (first principles, per test-programs/gdscript/CORPUS.md):
#   SAMPLES                     = [17, 4, 42, 8, 23, 15, 16]        (7 entries)
#   sorted descending           = [42, 23, 17, 16, 15, 8, 4]
#   doubled                     = [84, 46, 34, 32, 30, 16, 8]
#   total  = 84+46+34+32+30+16+8                                    = 250
#   biggest                     = 84
#   count                       = 7
#   mean   = 250 / 7            = 35.714285...  -> "%.4f" -> 35.7143
#   flag   = total > 100                                            = true
# so the program prints, in order:
#   CT_MT14_COUNT=7
#   CT_MT14_TOTAL=250
#   CT_MT14_BIGGEST=84
#   CT_MT14_MEAN=35.7143
#   CT_MT14_FLAG=true

extends MainLoop

const SAMPLES := [17, 4, 42, 8, 23, 15, 16]

# Invoked BY the engine's native sort — one native->VM crossing per comparison.
func _by_descending(a: int, b: int) -> bool:
	var left := a
	var right := b
	return left > right

# Invoked BY the engine's native Array.map — one native->VM crossing per element.
func _double(v: int) -> int:
	var doubled := v * 2
	return doubled

# A plain gd->gd call: its crossing span nests inside the caller's.
func _summarise(values: Array) -> Dictionary:
	var total := 0
	# Explicitly typed: an untyped Array's element has no static type, so `:=`
	# cannot infer one here.
	var biggest: int = values[0]
	for v in values:
		total += v
		if v > biggest:
			biggest = v
	var summary := {}
	summary["count"] = values.size()
	summary["total"] = total
	summary["biggest"] = biggest
	summary["mean"] = float(total) / float(values.size())
	return summary

func _init():
	var data: Array = SAMPLES.duplicate()
	data.sort_custom(_by_descending)
	var doubled: Array = data.map(_double)
	var summary: Dictionary = _summarise(doubled)
	var label := "mt14"
	var flag: bool = summary["total"] > 100
	var mean: float = summary["mean"]
	var point := Vector2(mean, float(summary["biggest"]))
	var file := FileAccess.open("user://mt14_out.txt", FileAccess.WRITE)
	if file != null:
		file.store_line("%s total=%d point=%s" % [label, summary["total"], str(point)])
		file.close()
	print("CT_MT14_COUNT=%d" % summary["count"])
	print("CT_MT14_TOTAL=%d" % summary["total"])
	print("CT_MT14_BIGGEST=%d" % summary["biggest"])
	print("CT_MT14_MEAN=%.4f" % mean)
	print("CT_MT14_FLAG=%s" % flag)

func _process(_delta):
	return true
