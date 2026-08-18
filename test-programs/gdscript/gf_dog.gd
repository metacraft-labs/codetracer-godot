# CodeTracer GDScript recorder — GF7 (Classes / Inheritance / super / inner
# classes / preload/load) reference program — DERIVED class file.
#
# gf_dog.gd is the CROSS-FILE derived subclass: its top-level class `Dog`
# `extends` the base in the SEPARATE file gf_animal.gd. It is NOT run directly —
# it is preload()/load()-ed by the gf_zoo.gd MainLoop driver. See
# scripts/EXPECTED-GF7.md.
#
# Exercises (recorded via the existing hooks; NO engine change):
#  - INHERITANCE + `super` ACROSS FILES: `Dog._init(n)` calls `super._init(n)`
#    (reaching Animal._init in gf_animal.gd) and `Dog.speak()` calls
#    `super.speak()` (reaching Animal.speak in gf_animal.gd). Each produces a
#    nested call tree whose DERIVED frame's source is res://gf_dog.gd and whose
#    BASE frame (reached via super) is res://gf_animal.gd — the cross-file
#    super nesting GF7's verification asserts.
#  - the ctor arg `n` is copied into `pup` so it is captured in the DERIVED
#    frame too (proving the arg flows through super._init across files: the base
#    frame captures the SAME value as `got_name`).
#  - `_static_init`: a static initializer that runs at class load; it records as
#    a `_static_init` frame (under the engine-synthesized `@static_initializer`),
#    source res://gf_dog.gd. Its `marker` local is captured.
#  - inner `class Kennel`: an inner class whose method `size()` records as its
#    own frame (named "size", source res://gf_dog.gd).
#
# `extends` uses a path-based reference (not the `Animal` global) because the
# global class registry is unavailable in one-shot headless runs (see
# gf_animal.gd header).

class_name Dog
extends "res://gf_animal.gd"

static func _static_init() -> void:
	var marker := 7
	marker = marker

func _init(n: String) -> void:
	super._init(n)  # cross-file ctor chain: reaches Animal._init (gf_animal.gd)
	var pup := n  # ctor arg captured in the DERIVED frame too
	pup = pup

func speak() -> String:
	var base_sound := super.speak()  # cross-file super: reaches Animal.speak
	var out := base_sound + "woof"
	return out

class Kennel:
	var count: int  # member — GF8-deferred
	func size() -> int:
		var s := 3
		return s
