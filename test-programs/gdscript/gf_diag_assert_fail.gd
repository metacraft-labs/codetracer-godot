# CodeTracer GDScript recorder — GF13 FAILING-ASSERT probe.
#
# A SEPARATE run from gf_diag.gd. GDScript has no exceptions, but a FAILING
# `assert(cond)` in a DEBUG build aborts the current execution (the VM hits
# OPCODE_BREAK). This program deliberately fails its assert to document, with a
# real recording, that:
#   - the trace legitimately ENDS at the assert line (the recorder flushes what
#     it recorded up to the halt via its atexit close);
#   - the statement AFTER the assert ("SHOULD_NOT_REACH") never records a step.
#
# It is NOT part of the passing gf_diag.gd program (which must run to
# completion). See scripts/record-and-verify-gf13.sh for the probe assertions.
#
#   CT_GDSCRIPT_TRACE=<dir> godot --headless --script res://gf_diag_assert_fail.gd

extends MainLoop

func _init():
	var x := 1
	assert(x == 2, "x must be 2")
	print("SHOULD_NOT_REACH")

func _process(_delta):
	return true
