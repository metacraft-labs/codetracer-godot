# CodeTracer GDScript recorder — G2 (per-line steps) regression probe.
#
# Deterministic, headless. Exercises per-line OPCODE_LINE steps across a call
# into add() and back, so the G3 call/return hooks can be proven not to have
# regressed the step stream.
#
#   CT_GDSCRIPT_TRACE=<dir> godot --headless --script res://g2probe.gd
#
# Prints "CT_G2_STEPS=30".

extends MainLoop

func add(a, b):
	var s = a + b
	return s

func _init():
	var x = 10
	var y = 20
	var z = add(x, y)
	print("CT_G2_STEPS=%d" % z)

func _process(_delta):
	return true
