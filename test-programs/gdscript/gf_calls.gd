# CodeTracer GDScript recorder — G3 (Calls & Returns) reference program.
#
# Deterministic, headless. Run as a Godot main loop:
#   CT_GDSCRIPT_TRACE=<dir> godot --headless --script res://gf_calls.gd
#
# The call graph the recorder must reconstruct (entry order = call_key order):
#
#   _init                    (root, depth 0)
#     └─ outer(2)            (depth 1)   -> call_key after _init
#          └─ inner(3)       (depth 2)   -> nested under outer
#     └─ sibling()           (depth 1)   -> AFTER outer returns (a sibling)
#
# After _init returns, the engine calls _process once; it returns true so the
# main loop quits immediately. _process is a second top-level (depth 0) frame
# with no children — it does not touch the _init subtree the test asserts on.
#
# Deterministic value (hand-derived, see scripts/EXPECTED-G3.md):
#   inner(3)  -> 3 + 1        = 4
#   outer(2)  -> inner(2+1)=4 -> 4 * 2 = 8      => x = 8
#   sibling() -> 99                             => y = 99
#   x + y = 107   => prints "CT_G3_RESULT=107"

extends MainLoop

func inner(n):
	var r = n + 1
	return r

func outer(m):
	var a = inner(m + 1)
	return a * 2

func sibling():
	return 99

func _init():
	var x = outer(2)
	var y = sibling()
	print("CT_G3_RESULT=%d" % (x + y))

func _process(_delta):
	return true
