# CodeTracer GDScript recorder — GF5 (Functions: defaults, static, variadic,
# return typing) reference program.
#
# Deterministic, headless. Run as a Godot main loop:
#   CT_GDSCRIPT_TRACE=<dir> godot --headless --script res://gf_functions.gd
#
# Exercises GF5:
#  - default arguments (configure) called WITH and WITHOUT the optional args;
#    each param is copied into a fresh named local so the BOUND value (default
#    or supplied) is captured in BOTH calls (a supplied arg is placed directly
#    into the callee slot by the call prologue — no OPCODE_ASSIGN — so it is
#    only observable via a read/copy; an OMITTED arg is materialized by the
#    default-argument bytecode via a real OPCODE_ASSIGN that the value hook
#    sees on the param slot itself);
#  - a static function (mul) recorded as a normal nested call/return frame with
#    its args copied into named locals;
#  - typed `-> int` / `-> float`, `-> void`, and untyped returns, with the
#    return VALUE captured on each return event;
#  - a variadic BUILTIN call (print with 3 args) recorded as a caller-frame
#    step (GDScript 4 has NO user-defined varargs — that is a language N/A,
#    documented in EXPECTED-GF5.md, not a recorder gap).
#
# Deterministic checksum (hand-derived, see EXPECTED-GF5.md):
#   configure(1)          -> got_a=1,  got_b=10, got_c="x"  -> 1+10+1  = 12
#   configure(2,20,"yz")  -> got_a=2,  got_b=20, got_c="yz" -> 2+20+2  = 24
#   mul(6, 7)             -> 42
#   pick(true)            -> 42
#   sum = 12 + 24 + 42 + 42 = 120  => prints "CT_GF5_RESULT=120"

extends MainLoop

# Default arguments: b (int) and c (String) are optional with defaults.
func configure(a, b := 10, c := "x") -> int:
	var got_a = a
	var got_b = b
	var got_c = c
	return got_a + got_b + got_c.length()

# Static function: still a normal GDScriptFunction call/return frame.
static func mul(a, b) -> int:
	var fa = a
	var fb = b
	return fa * fb

# Typed float return.
func area(r: float) -> float:
	var rad = r
	return 3.14159 * rad * rad

# void return: retvalue is NIL -> captured as None.
func do_void() -> void:
	var touched = 1
	return

# Untyped return (no return-type annotation).
func pick(flag):
	if flag:
		return 42
	return "none"

func _init():
	var d1 = configure(1)
	var d2 = configure(2, 20, "yz")
	var m = mul(6, 7)
	var ar = area(2.0)
	do_void()
	var pk = pick(true)
	var total = d1 + d2 + m + pk
	print("gf5", "variadic", total)
	print("CT_GF5_RESULT=%d" % total)

func _process(_delta):
	return true
