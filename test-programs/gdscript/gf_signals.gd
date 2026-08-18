# CodeTracer GDScript recorder — GF9 (Signals: declare / emit / connect /
# disconnect) reference program.
#
# Deterministic, headless. Run as a Godot main loop:
#   CT_GDSCRIPT_TRACE=<dir> godot --headless --script res://gf_signals.gd
#
# `--script` requires the entry to inherit SceneTree / MainLoop. SceneTree
# extends MainLoop extends Object, so `self` owns signals: a `signal` can be
# declared, connected, emitted, and disconnected on the SceneTree instance.
#
# WHAT GF9 PROVES (no engine change — a COVERAGE milestone, like GF1/GF2/GF6/GF7):
# A GDScript signal handler is an ORDINARY method. Emitting a signal dispatches
# SYNCHRONOUSLY to each connected handler, and each handler runs through
# GDScriptFunction::call — so the existing hooks fire per handler invocation:
#   - G3 call/return: each handler invocation is a balanced call FRAME.
#   - G2 per-line steps: the handler body's lines emit steps.
#   - G4/GF5 value capture: emitted args, read into locals, are captured by NAME;
#     the handler's return value is captured on its return event (GF5).
# The native emit/connect/disconnect calls have NO GDScript frame of their own,
# so the handler frame's PARENT is the emitter's frame (`_initialize`, depth 0),
# making handler frames depth-1 children of the emitter. There is no intervening
# "emit" frame; the emit call site is just a step in the emitter, and the handler
# frame(s) appear immediately after that step and before the emitter's next step.
#
# GDScript params are not captured directly (same read-into-local seam as
# GF5/GF7), so each handler copies its emitted args into locals on distinct lines
# to make the emitted ARG VALUES observable in the trace.
#
# The TEETH: handler frames must appear/disappear EXACTLY per connect/disconnect
# state — proven by driving five emits (0 / 1 / 2 / 1-after-disconnect / 0-after-
# disconnect-all connected handlers) and asserting which handler frame(s) each
# emit produced, with captured args + returns — NOT merely that a trace exists.
#
# Deterministic checksum (hand-derived; see scripts/EXPECTED-GF9.md):
#   emit(1,1) no connections     -> total unchanged = 0
#   emit(7,2) _on_hit_a          -> total += 7            = 7
#   emit(3,4) _on_hit_a,_on_hit_b-> total += 3 (=10) then += 34 (b_val) = 44
#   emit(5,6) _on_hit_b only     -> total += 56 (b_val=56) = 100
#   emit(9,9) no connections     -> total unchanged = 100
#   => prints "CT_GF9_RESULT=100"

extends SceneTree

signal hit(dmg, kind)                 # GF9: a user-declared signal with 2 params

var total := 0                        # accumulator for the deterministic checksum

# Handler A — reads BOTH emitted args into locals on their own lines (so the
# emitted arg values are observable), mutates `total`, returns their sum.
func _on_hit_a(dmg, kind):            # line 50
	var a_dmg = dmg                   # line 51: capture emitted arg 1 by name
	var a_kind = kind                 # line 52: capture emitted arg 2 by name
	total += a_dmg                    # line 53
	return a_dmg + a_kind             # line 54: return value captured by GF5

# Handler B — distinct line numbers + a distinct local so its frame identity and
# captured value are crisp and cannot be confused with handler A's.
func _on_hit_b(dmg, kind):            # line 58
	var b_val = dmg * 10 + kind       # line 59: single distinct local
	total += b_val                    # line 60
	return b_val                      # line 61: return value captured by GF5

func _initialize() -> void:
	# 1. EMIT WITH NO CONNECTIONS: no handler is connected yet, so this emit
	#    must produce NO handler frame at all.
	hit.emit(1, 1)                    # line 66: emit #1 (0 handlers)

	# 2. DECLARE+CONNECT+EMIT: connect one handler, emit -> exactly one handler
	#    frame with the emitted args captured and its return recorded.
	hit.connect(_on_hit_a)            # line 70: native connect (no GDScript frame)
	hit.emit(7, 2)                    # line 71: emit #2 -> _on_hit_a(7,2), ret 9

	# 3. MULTIPLE HANDLERS: connect a SECOND handler, emit -> BOTH handler frames
	#    appear on a single emit, in connection order (_on_hit_a then _on_hit_b).
	hit.connect(_on_hit_b)            # line 75: native connect
	hit.emit(3, 4)                    # line 76: emit #3 -> _on_hit_a(3,4) ret 7,
	                                  #          then _on_hit_b(3,4) b_val 34 ret 34

	# 4. DISCONNECT ONE (the teeth): disconnect _on_hit_a, emit -> ONLY the still-
	#    connected _on_hit_b runs; the disconnected _on_hit_a frame is ABSENT.
	hit.disconnect(_on_hit_a)         # line 81: native disconnect
	hit.emit(5, 6)                    # line 82: emit #4 -> _on_hit_b(5,6) b_val 56

	# 5. DISCONNECT ALL: disconnect the last handler, emit -> NO handler frame.
	hit.disconnect(_on_hit_b)         # line 85: native disconnect
	hit.emit(9, 9)                    # line 86: emit #5 (0 handlers)

	print("CT_GF9_RESULT=%d" % total)
	quit()

func _process(_delta: float) -> bool:
	return true
