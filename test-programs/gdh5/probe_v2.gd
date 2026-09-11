# GDH5 probe — VERSION 2. The POST-reload source.
#
# Used by scripts/record-and-verify-gdh5.sh for two gates:
#
#   * gdh5_in_process_reload_matches_the_remote_debugger_path — the same
#     fixture is reloaded twice, once from the recorder's in-process poll point
#     and once with `core:reload_scripts` over `--remote-debug`, and the two
#     must agree about what got reloaded and what the program then printed.
#     Godot's own supported path is the oracle.
#   * gdh5_unpreserved_state_is_reported — `counter` is a STATIC variable, and
#     `_initialize` sets it to a NON-DEFAULT value. That matters: after the
#     reload it will read as the default again, and "it is now the default" is
#     only evidence of a reset if the fixture is known to have moved it off the
#     default first (the milestone's anti_vacuity).
#
# Every printed token is deterministic: no clock, no RNG, no host-dependent
# value. `GDH5_TICK=<n> ` carries a TRAILING SPACE so a driver looking for
# tick 8 cannot match tick 18.
extends MainLoop

const TICK_MS := 100
const TICKS := 24
const STATIC_SEED := 4242

static var counter := 0

var tick := 0

func _initialize() -> void:
	counter = STATIC_SEED
	print("GDH5_BEGIN")
	print("GDH5_STATIC_AT_BEGIN=", counter, " ")

func probe(n: int) -> void:
	print("GDH5_V2_A tick=", n)
	print("GDH5_V2_B tick=", n)
	print("GDH5_V2_C tick=", n)

func _process(_delta: float) -> bool:
	tick += 1
	probe(tick)
	print("GDH5_TICK=", tick, " ")
	print("GDH5_STATIC=", counter, " ")
	OS.delay_msec(TICK_MS)
	return tick >= TICKS

func _finalize() -> void:
	print("GDH5_END")
