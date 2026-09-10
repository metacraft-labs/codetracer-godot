# GDH0 probe — VERSION 1.  The PRE-reload source.
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

func probe(n: int) -> void:
	print("GDH0_V1_A tick=", n)
	print("GDH0_V1_B tick=", n)
	print("GDH0_V1_C tick=", n)

func _process(_delta: float) -> bool:
	tick += 1
	probe(tick)
	print("GDH0_TICK=", tick, " ")
	OS.delay_msec(TICK_MS)
	return tick >= TICKS

func _finalize() -> void:
	print("GDH0_END")
