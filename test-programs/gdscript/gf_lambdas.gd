# CodeTracer GDScript recorder — GF6 (Lambdas & Closures / local capture)
# reference program.
#
# Deterministic, headless. Run as a Godot main loop:
#   CT_GDSCRIPT_TRACE=<dir> godot --headless --script res://gf_lambdas.gd
#
# Exercises GF6 (NO engine change — see EXPECTED-GF6.md):
#  - a lambda that CAPTURES an outer local (`base`) and takes a PARAM (`x`),
#    invoked via `.call()` — records as a nested call/return frame (G3) named
#    "<anonymous lambda>" under _init, with its return value captured (GF5);
#  - the lambda's param and captured value are READABLE at lambda execution:
#    the body reads `x` -> `seen_x` and `base` -> `seen_base` (both prologue-
#    placed leading args, observed via a read/copy — the same seam GF5 used for
#    supplied arguments; see EXPECTED-GF6.md) and returns their sum;
#  - CAPTURE-BY-VALUE: after the lambda is created, the outer `base` is mutated
#    (10 -> 999). Re-invoking the lambda STILL returns 15 and `seen_base` STILL
#    reads 10 (captures are snapshotted BY VALUE at OPCODE_CREATE_LAMBDA), while
#    the outer local `base` is separately captured as [10, 999];
#  - a lambda STORED IN A VAR is value-captured as a Callable (GF4 shallow
#    Struct "Callable" {method:"<anonymous lambda>"});
#  - a NESTED lambda (a lambda created inside a lambda) capturing from TWO scopes
#    (`a` from _init transitively, `b` from the outer lambda) nests at depth 2.
#
# Lambda params are typed and every read-copy is used, so the program compiles
# with no warnings (the engine treats inference-from-Variant / unused-variable
# warnings as errors). `.call()` returns Variant, so its results are assigned to
# explicitly-typed locals.
#
# Deterministic checksum (hand-derived, see EXPECTED-GF6.md):
#   add.call(5)      -> seen_x(5) + seen_base(10)          = 15
#   add.call(5)      -> seen_x(5) + seen_base(captured 10) = 15  (base now 999)
#   doubler.call(21) -> 21 * 2                             = 42
#   outer.call(7)    -> inner: 7 + seen_a(100) + seen_b(200) = 307
#   total = 15 + 15 + 42 + 307 = 379  => prints "CT_GF6_RESULT=379"

extends MainLoop

func _init():
	# 1. Lambda capturing an outer local (base) + taking a param (x).
	var base := 10
	var add := func(x: int) -> int:
		var seen_x := x
		var seen_base := base
		return seen_x + seen_base
	var r1: int = add.call(5)

	# 2. Capture-by-value: mutate the outer local AFTER the lambda was created.
	#    The lambda snapshotted base==10 at creation, so it STILL returns 15.
	base = 999
	var r2: int = add.call(5)

	# 3. Lambda stored in a var — value-captured as a Callable.
	var doubler := func(n: int) -> int: return n * 2
	var r3: int = doubler.call(21)

	# 4. Nested lambda capturing from two scopes (a from _init, b from outer).
	var a := 100
	var outer := func(y: int) -> int:
		var b := 200
		var inner := func(z: int) -> int:
			var seen_a := a
			var seen_b := b
			return z + seen_a + seen_b
		var ires: int = inner.call(y)
		return ires
	var rn: int = outer.call(7)

	var total := r1 + r2 + r3 + rn
	print("CT_GF6_RESULT=%d" % total)

func _process(_delta):
	return true
