# CodeTracer GDScript recorder — GF2 (Control Flow & match, all pattern kinds)
# reference program.
#
# Deterministic, headless. Run as a Godot main loop:
#   CT_GDSCRIPT_TRACE=<dir> godot --headless --script res://gf_control_flow.gd
#
# GF2 proves that the STEP SEQUENCE recorded by the patched engine faithfully
# reflects control flow: which if/elif/else branch was taken, how many times a
# loop body ran, how break/continue/pass alter the body's step stream, and which
# match arm was dispatched (for every pattern kind) — plus the bound values of
# match binding patterns.
#
# GF2 needs NO new VM hook (a COVERAGE milestone, like GF1):
#   - control flow is just per-line OPCODE_LINE steps (G2). Each executed
#     statement line emits exactly one step; a for/while HEADER emits its step
#     once at loop entry (the back-edge re-enters the body, not the header line);
#     an `else:` header emits NO step (control falls straight into the else
#     body); if/elif condition headers, `return`, `break`, `continue`, and `pass`
#     each emit one step when executed.
#   - a match binding pattern (`var x`) binds a local via an OPCODE_ASSIGN into
#     the named slot, captured at the PATTERN line by the G4 assign hook; the
#     match header + each TESTED pattern line + the matched arm's body each emit
#     a step, so arm dispatch is fully observable.
# Confirmed empirically against the G4/GF1-era binary (unchanged) — see
# scripts/EXPECTED-GF2.md for the first-principles derivation of every step.
#
# Each construct lives in its OWN helper function so branch bodies do not share a
# stack slot (sibling-branch locals alias to one slot); the taken-branch
# assertion is therefore made on the globally-unique step LINE numbers, not on
# variable names. Match binding VALUES (b, rest, first, dv, g) are asserted by
# name because each match statement owns its binding slot.

extends MainLoop

const LIMIT = 100

# --- if / elif / else: single `tag` reassigned per branch (no slot aliasing) ---
func classify(n):
	var tag = 0
	if n < 0:
		tag = 1
	elif n == 0:
		tag = 2
	else:
		tag = 3
	return tag

# --- for loop + continue (skips i == 2) + implicit iteration count ---
func sum_for():
	var acc = 0
	for i in range(4):
		if i == 2:
			continue
		acc = acc + i
	return acc

# --- while loop + break (exits at w == 3, long before w < 100 fails) ---
func count_while():
	var w = 0
	while w < 100:
		w = w + 1
		if w == 3:
			break
	return w

# --- pass as a no-op statement ---
func noop():
	pass
	return 0

# --- match: literal arm / wildcard arm (driven by two inputs) ---
func m_literal(x):
	var r = 0
	match x:
		1:
			r = 11
		2:
			r = 12
		_:
			r = 19
	return r

# --- match: expression pattern (constant expression LIMIT) ---
func m_expression(x):
	var r = 0
	match x:
		LIMIT:
			r = 100
		_:
			r = 0
	return r

# --- match: comma / alternative pattern ---
func m_comma(x):
	var r = 0
	match x:
		1, 2, 3:
			r = 1
		_:
			r = 0
	return r

# --- match: plain binding pattern (var b) — matches anything, binds the value ---
func m_bind(x):
	var r = 0
	match x:
		var b:
			r = b
	return r

# --- match: array pattern with binding ([1, var rest]) ---
func m_array(a):
	var r = 0
	match a:
		[1, var rest]:
			r = rest
		_:
			r = -1
	return r

# --- match: open-ended array pattern ([var first, ..]) ---
func m_array_open(a):
	var r = 0
	match a:
		[var first, ..]:
			r = first
		_:
			r = -1
	return r

# --- match: dictionary pattern with binding ({"key": var dv}) ---
func m_dict(d):
	var r = 0
	match d:
		{"key": var dv}:
			r = dv
		_:
			r = -1
	return r

# --- match: binding pattern with a `when` guard (var g when g > 5) ---
func m_guard(x):
	var r = 0
	match x:
		var g when g > 5:
			r = g
		_:
			r = -1
	return r

func _init():
	# if/elif/else — drive all three arms
	var t_neg = classify(-5)
	var t_zero = classify(0)
	var t_pos = classify(7)
	# loops
	var s = sum_for()
	var w = count_while()
	var z = noop()
	# match — every pattern kind, each driven to the arm named
	var ml = m_literal(2)
	var mw = m_literal(99)
	var me = m_expression(100)
	var mc = m_comma(3)
	var mb = m_bind(77)
	var ma = m_array([1, 2])
	var mo = m_array_open([7, 8, 9])
	var md = m_dict({"key": 42})
	var mg = m_guard(8)
	var checksum = t_neg + t_zero + t_pos + s + w + z + ml + mw + me + mc + mb + ma + mo + md + mg
	print("CT_GF2_RESULT=%d" % checksum)

func _process(_delta):
	return true
