# CodeTracer GDScript recorder — GF7 (Classes / Inheritance / super / inner
# classes / preload/load) reference program — BASE class file.
#
# This is the base of a two-file class hierarchy (gf_animal.gd is the base,
# gf_dog.gd the cross-file derived subclass; gf_zoo.gd is the MainLoop driver).
# It is NOT run directly — it is preload()/load()-ed by the others. See
# scripts/EXPECTED-GF7.md for the hand-derived facts and the full derivation.
#
# Exercises (recorded via the existing G3 call/return + G4/GF5 value/return
# hooks; GF7 adds NO engine change — see EXPECTED-GF7.md):
#  - `class_name Animal` + `@abstract`: Animal is ABSTRACT, so it can never be
#    instantiated directly (`Animal.new()` would error). Only the concrete
#    subclass Dog (gf_dog.gd) is instantiated; the abstract base's `_init` and
#    `speak()` are reached across the file boundary via Dog's `super._init(n)` /
#    `super.speak()`.
#  - `func _init(n)`: the constructor copies its arg into a named local
#    (`got_name`) so the CTOR ARGUMENT is captured (call_entry.args is empty for
#    GDScript — args are observed via a read-into-local, the GF5/GF6 seam). The
#    `species = got_name` member write is a class MEMBER write (ADDR_TYPE_MEMBER)
#    and is DEFERRED to GF8 — it is intentionally NOT captured here.
#  - `func speak() -> String`: the base method reached via `super.speak()`; its
#    return value ("...") is captured on the return event (GF5).
#  - inner `class Tag`: an inner class whose method `label()` records as its own
#    call/return FRAME (named "label", source res://gf_animal.gd). Its `var v`
#    member is a MEMBER write → GF8-deferred (not asserted here).
#
# Note on `class_name`: the global class registry is not populated in one-shot
# headless `--script` runs ("Could not load global script cache"), so the
# cross-file references in gf_dog.gd / gf_zoo.gd resolve the base via
# preload()/path-based `extends` rather than the `Animal` global. The
# `class_name Animal` declaration still compiles and is faithful; it is simply
# not the resolution path in this headless mode.

@abstract
class_name Animal
extends RefCounted

var species: String  # member (ADDR_TYPE_MEMBER) — GF8-deferred, not captured

func _init(n: String) -> void:
	var got_name := n  # ctor ARG captured here (call_entry.args is empty)
	species = got_name  # member write — GF8-deferred

func speak() -> String:
	var sound := "..."  # base sound, reached via super.speak() from Dog
	return sound

func legs() -> int:
	var l := 4
	return l

class Tag:
	var v: int  # member — GF8-deferred
	func label() -> String:
		var made := "tag"
		return made
