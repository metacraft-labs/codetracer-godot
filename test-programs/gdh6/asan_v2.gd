# GDH-M6 ASan fixture — VERSION 2 — the content the reload installs.
#
# `gdh6_a_timed_out_deferral_does_not_outlive_its_request` needs one thing the
# gate fixtures cannot give it: TIME.  The race it drives is "the waiter's
# bound expires while the safe point is still applying", so the program has to
# still be running when the safe point finishes — and probe_v1.gd runs 30 ticks
# at 60 ms, i.e. 1.8 s in total, which is shorter than the hold the gate needs
# in order to make the timeout deterministic.  Measured: with a 2 s bound and a
# 9 s hold the program ended first and the apply never completed, so the gate
# reported a failure that was the FIXTURE's and not the subject's.
#
# This fixture therefore runs long enough for the whole race to play out with
# margin, and carries no probe tokens at all: nothing here is compared against
# a trace.  The only thing the driver reads from it is `GDH6_TICK=<n> ` (with
# the trailing space, so tick 6 cannot match tick 16).
extends MainLoop

const TICK_MS := 50
const TICKS := 600      # 30 s of headroom

var tick := 0

func _initialize() -> void:
	print("GDH6_BEGIN")

func work(n: int) -> int:
	return n * 3

func _process(_delta: float) -> bool:
	tick += 1
	var v := work(tick)
	print("GDH6_TICK=", tick, " ")
	print("GDH6_ASAN_V2=", v, " ")
	OS.delay_msec(TICK_MS)
	return tick >= TICKS

func _finalize() -> void:
	print("GDH6_END")
