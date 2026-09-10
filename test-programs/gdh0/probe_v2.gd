# GDH0 probe — VERSION 2.  The POST-reload source.
#
# scripts/record-and-verify-gdh0.sh copies this file to `probe.gd` in a
# scratch project, runs it headless under CT_GDSCRIPT_TRACE, and part-way
# through the run overwrites `probe.gd` with probe_v2.gd and asks the engine
# to reload it over `core:reload_scripts` on the `--remote-debug` peer.
#
# Every printed token is deterministic: no clock, no RNG, no host-dependent
# value.  `GDH0_TICK=<n> ` carries a TRAILING SPACE so that a driver looking
# for tick 8 cannot match tick 18 — the same rule EXPECTED-HCR1.md's marker
# uses.
#
# The tick delay exists so an external driver has a real window in which to
# overwrite the file and deliver the reload request.  Without it this program
# finishes 24 ticks in ~0.1 s and the reload lands after the run is over —
# measured, on this host, before the delay was added.
extends MainLoop

const TICK_MS := 100
const TICKS := 24

var tick := 0

func _initialize() -> void:
	print("GDH0_BEGIN")

# ==== v2's insertion ======================================================
# Seventeen lines added ABOVE the function below, and NOTHING else moved.
# Everything from here down is shifted by exactly this block's height.
#
# The shift is the point.  v1 is 40 lines long; every line v2 executes after
# the reload lies in 45..57, so a post-reload step's line number does not
# exist in v1's text AT ALL.  A consumer that resolves the recorded path to
# the one source view the container carries is not merely showing the wrong
# line — it is being asked for a line the file it has does not reach.
#
# The block is inert on purpose (comments only) so the shift is purely
# positional and no new behaviour is confounded with it.
#
# The block's HEIGHT is measured by the harness from these two files, by
# diffing them.  It is never written into a verifier, so a fixture edit
# cannot silently disagree with an assertion.
# ==========================================================================
func probe(n: int) -> void:
	print("GDH0_V2_A tick=", n)
	print("GDH0_V2_B tick=", n)
	print("GDH0_V2_C tick=", n)

func _process(_delta: float) -> bool:
	tick += 1
	probe(tick)
	print("GDH0_TICK=", tick, " ")
	OS.delay_msec(TICK_MS)
	return tick >= TICKS

func _finalize() -> void:
	print("GDH0_END")
