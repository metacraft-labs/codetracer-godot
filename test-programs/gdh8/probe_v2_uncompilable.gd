# GDH8 probe — VERSION 1.  The source the process STARTS with (generation 1).
#
# GDH-M8 grades FAILURE semantics (design §8.1), so this fixture is shaped
# differently from GDH-M6's and the difference is the point.
#
# THE TOKEN.  Every probe line prints exactly one token:
#
#     GDH8|it=<iteration>|L=<line>|
#
# `<line>` is the 1-based line number of the `print` statement THAT EMITTED IT.
# GDScript has no __LINE__, so the number is written by hand — and
# `verify_gdh8.py` re-derives every one of them by scanning this file and
# REFUSES to run if a literal disagrees with the line it sits on.  The trailing
# `|` is not decoration: `L=5` must not match `L=51`.
#
# THERE IS NO `v=` TAG, and that is deliberate.  GDH-M6's fixture prepends its
# new lines ABOVE everything, so each version's whole line space is disjoint
# from the others' and a version tag is meaningful.  This one is APPEND-ONLY:
# `probe_v2_ok.gd` is this file BYTE FOR BYTE plus three lines added at the very
# end of `extra()`, which is the last function in the file.  So under v2 the
# lines below still execute, at the same numbers, and a `v=` tag on them would
# say "1" while v2 was running.  What identifies the running version instead is
# the set of lines that FIRE: v1 emits {52, 53} per tick and v2 emits
# {52, 53, 71, 72, 73}.  The three high lines are V2-ONLY and their presence in
# stdout is the harness's evidence that a reload was applied — which is exactly
# what `gdh8_digest_mismatch_is_refused_before_anything_is_touched`'s falsifier
# has to be killed by ("go red by observing v2's tokens in stdout, i.e. by the
# engine having reloaded, not merely by the absence of an error message").
#
# THE APPEND-ONLY SHAPE IS ALSO THE REMEDY GDH-M7 NAMED.  That milestone's
# `gdh7_a_breakpoint_binds_to_every_version` is NOT RUN because the GDH-M6
# fixture has no line that executes in two versions — its three versions'
# executed line sets are pairwise disjoint.  Here every line of v1 is an
# executed line of v2 as well, so a breakpoint on (say) line 52 binds and hits
# in both.  This fixture is written to be able to serve that gate; running it is
# GDH-M7's job, not this one's.
#
# Every printed value is deterministic: no clock, no RNG, no host-dependent
# value.  The tick delay exists so the driver has a real window in which to
# deliver each notification.
extends MainLoop

const TICK_MS := 60
const TICKS := 30

var tick := 0

func _initialize() -> void:
	print("GDH8_BEGIN")

func probe(n: int) -> void:
	print("GDH8|it=", n, "|L=52|")
	print("GDH8|it=", n, "|L=53|")

func _process(_delta: float) -> bool:
	tick += 1
	probe(tick)
	extra(tick)
	print("GDH8_TICK=", tick, " ")
	OS.delay_msec(TICK_MS)
	return tick >= TICKS

func _finalize() -> void:
	print("GDH8_END")

# THE APPEND POINT.  `extra()` is the LAST function in the file on purpose:
# v2 is this file plus lines added here and nowhere else, so every line above
# keeps its number across the reload.
func extra(n: int) -> void:
	pass
	print("GDH8|it=", n, "|L=71|")
	print("GDH8|it=", n, "|L=72|")
	print("GDH8|it=", n, "|L=73|")

# THE DEFECT THIS FIXTURE EXISTS FOR — GDH-M8b.  It PARSES.  It ANALYZES.  The
# COMPILER refuses it.
#
# `probe_v2_bad.gd`'s defect dies in `GDScriptParser::parse` (gdscript.cpp:816),
# which is why GDH-M8's pre-check can refuse it without installing anything: the
# parser and the analyzer both run on a stack-local `GDScriptParser`.
# `GDScriptCompiler` cannot be run that way — `compile()` writes into a
# `GDScript` — so a file that survives both and fails the compiler is the one
# shape the pre-check is structurally unable to see.  Measured against the STOCK
# engine before it was relied on:
#
#   $ godot.linuxbsd.template_debug.x86_64 --headless --path <proj> \
#       --script res://probe.gd
#   SCRIPT ERROR: Compile Error: Must use 'gdh8_compiler_only_defect' instead
#                 of 'self.gdh8_compiler_only_defect' in getter.
#             at: GDScript::reload (res://probe.gd:112)
#
# "Compile Error", not "Parse Error" — Godot prints the first at
# gdscript.cpp:856 (after `ERR_COMPILATION_FAILED` at :862) and the second at
# :827 and :842.  `verify_gdh8.py` re-measures exactly that distinction before
# it records anything, so this claim is the engine's and not this comment's.
#
# The construct: inside the getter of `gdh8_compiler_only_defect`, read the
# property through `self`.  The analyzer resolves it (it is a real property of a
# real type); the code generator refuses it at gdscript_compiler.cpp:802-806,
# because `MI->value.getter == codegen.function_name` makes it an infinite
# recursion the compiler will not emit.
#
# APPEND-ONLY, like every other file here: this is `probe_v2_ok.gd` byte for
# byte plus these lines, so the L=71/72/73 probes above are still v2's own and
# still lie past the whole of v1.  If the reload were wrongly applied, stdout
# would carry them; if it were applied and left broken, the MainLoop would stop
# being called and the program would never print GDH8_END.  Both are visible.
var gdh8_compiler_only_defect: int = 0:
	get = gdh8_read_the_property

func gdh8_read_the_property() -> int:
	return self.gdh8_compiler_only_defect
