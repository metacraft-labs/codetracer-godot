# CodeTracer GDScript recorder — GF1 (Static Typing, Inference, Operators,
# Constants & Enums) reference program.
#
# Deterministic, headless. Run as a Godot main loop:
#   CT_GDSCRIPT_TRACE=<dir> godot --headless --script res://gf_typing.gd
#
# Every construct writes its result to a NAMED local via `var name = <expr>`
# (or `name = <expr>`). The GDScript codegen routes such a declaration through
# write_assign / write_assign_with_conversion, i.e. an OPCODE_ASSIGN* into the
# named slot (the operator / is / as / in / ternary sub-result lands in a
# compiler temporary first). Those ASSIGN opcodes are already hooked by G4, so
# each named local is captured with its correct value AND type kind — no new VM
# hook is required for GF1. See scripts/EXPECTED-GF1.md for the first-principles
# derivation.
#
# Runtime "seed" locals (s7, s2, s12, s10, s1, sf7) feed the operators so the
# operator opcodes actually EXECUTE at runtime (a fully-literal expression like
# `2 ** 10` would be constant-folded at compile time; using a runtime operand
# forces OPCODE_OPERATOR / OPCODE_OPERATOR_VALIDATED to run).
#
# NOTE (honest boundary): `static var` writes are instance/class MEMBER writes,
# not stack-slot writes; capturing them is milestone GF8 (member writes). The
# `static var counter` below is present to show it does not break the trace, but
# its capture is NOT asserted by GF1 (see EXPECTED-GF1.md).

extends MainLoop

const K = 42

enum Dir { UP, DOWN = 5, LEFT }   # named enum: UP=0, DOWN=5, LEFT=6
enum { A, B }                     # unnamed enum: A=0, B=1

static var counter := 0           # GF8-deferred member write (not asserted)

func _init():
	# 1. typed / inferred / untyped locals
	var a: int = 5                # typed int            -> a = 5    (Int)
	var b := 2.5                  # inferred float       -> b = 2.5  (Float)
	var c = "s"                   # untyped String       -> c = "s"  (String)

	# runtime seeds (prevent constant folding so operator opcodes execute)
	var s7 := 7
	var s2 := 2
	var s12 := 12
	var s10 := 10
	var s1 := 1
	var sf7 := 7.0

	# 2a. arithmetic (incl. power and integer/float division)
	var pw = s2 ** 10             # power                -> pw = 1024 (Int)
	var idiv = s7 / s2            # integer division     -> idiv = 3  (Int)
	var fdiv = sf7 / s2           # float division       -> fdiv = 3.5 (Float)
	var md = s7 % s2             # modulo               -> md = 1    (Int)

	# 2b. bitwise (& | ^ ~ << >>)
	var band = s12 & s10         # bitwise and          -> band = 8  (Int)
	var bor = s12 | s10          # bitwise or           -> bor = 14  (Int)
	var bxor = s12 ^ s10         # bitwise xor          -> bxor = 6  (Int)
	var bnot = ~s12              # bitwise not (unary)  -> bnot = -13 (Int)
	var shl = s1 << 4            # left shift           -> shl = 16  (Int)
	var shr = s12 >> 2           # right shift          -> shr = 3   (Int)

	# 2c. comparison
	var cmp = s7 > s2            # comparison           -> cmp = true (Bool)

	# 2d. logical (and / or / not)
	var land = (s7 > s2) and (s2 > s1)   # logical and  -> land = true (Bool)
	var lor = (s7 < s2) or (s2 > s1)     # logical or   -> lor = true  (Bool)
	var lnot = not (s7 < s2)             # logical not  -> lnot = true (Bool)

	# 2e. ternary
	var tern = 100 if s7 > s2 else 200   # ternary      -> tern = 100 (Int)

	# 2f. type / identity operators (is / as / in)
	var ris = s7 is int          # is                   -> ris = true (Bool)
	var ras = s7 as float        # as                   -> ras = 7.0  (Float)
	var rin = s2 in [1, 2, 3]    # in                   -> rin = true (Bool)

	# 3. constant
	var d = K                    # const read           -> d = 42    (Int)

	# 4. enums (named + unnamed)
	var e = Dir.DOWN             # named enum member    -> e = 5     (Int)
	var f = B                    # unnamed enum member  -> f = 1     (Int)

	# GF8-deferred member write (present, not asserted)
	counter += 1

	# Deterministic checksum over the captured Int results.
	var checksum = pw + idiv + md + band + bor + bxor + bnot + shl + shr \
		+ tern + d + e + f + int(b) + int(fdiv) + int(ras)
	print("CT_GF1_RESULT=%d" % checksum)

func _process(_delta):
	return true
