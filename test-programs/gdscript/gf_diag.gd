# CodeTracer GDScript recorder — GF13 (Diagnostics & String Formatting)
# reference program.
#
# Deterministic, headless. Run as a Godot main loop:
#   CT_GDSCRIPT_TRACE=<dir> godot --headless --script res://gf_diag.gd
#
# GDScript has NO exceptions (no try/catch to record). GF13 covers the three
# diagnostic / formatting features that remain in the GF-series:
#
#  1. STRING FORMATTING — each formatted result is assigned to a NAMED local and
#     is therefore captured as a String VALUE by the existing G4 path (no engine
#     change for formatting):
#       - `%` format operator:  "%d/%s/%.2f" % [i, s, f]  -> "7/a/3.14"
#       - String.format:        "{0} {1}".format([a, b])  -> "x y"
#       - raw string r"...":    r"a\nb"                    -> a \ n b  (literal
#                               backslash — NOT a newline; length 4)
#       - triple-quoted:        """line1<NL>line2"""       -> "line1\nline2"
#                               (embedded newline preserved; length 11)
#       - concatenation:        s + "-" + a                -> "a-x"
#
#  2. assert — a PASSING `assert(ok, "msg")` is a no-op STEP; execution
#     continues (proven by the later steps/print). `ok` is captured true.
#     (A FAILING assert HALTS the VM in a debug build — probed SEPARATELY by
#     gf_diag_assert_fail.gd, which legitimately ends the trace at the assert.)
#
#  3. push_warning / push_error — GDScript's diagnostic surface (native core
#     utility functions; NOT exceptions). Neither halts. The recorder records
#     each as an events.dat special event carrying its message (GF13 engine
#     hook): push_error -> io_kind ioError, push_warning -> io_kind ioStderr
#     (rendered by ct-print --full), each also tagged in the event metadata
#     ("ct-push-error"/"ct-push-warning") for a real event-log pane.
#
# Deterministic checksum (hand-derived; see scripts/EXPECTED-GF13.md):
#   pf.length()  = 8   ("7/a/3.14")
#   ff.length()  = 3   ("x y")
#   raw.length() = 4   (a \ n b)
#   tq.length()  = 11  ("line1"+NL+"line2")
#   cc.length()  = 3   ("a-x")
#   => 8 + 3 + 4 + 11 + 3 = 29  => prints "CT_GF13_RESULT=29"

extends MainLoop

func _init():
	var i := 7
	var s := "a"
	var f := 3.14159
	var pf := "%d/%s/%.2f" % [i, s, f]
	var a := "x"
	var b := "y"
	var ff := "{0} {1}".format([a, b])
	var raw := r"a\nb"
	var tq := """line1
line2"""
	var cc := s + "-" + a
	var ok := (i == 7)
	assert(ok, "i must be 7")
	push_warning("gf13 warning")
	push_error("gf13 error")
	var checksum := pf.length() + ff.length() + raw.length() + tq.length() + cc.length()
	print("CT_GF13_RESULT=%d" % checksum)

func _process(_delta):
	return true
