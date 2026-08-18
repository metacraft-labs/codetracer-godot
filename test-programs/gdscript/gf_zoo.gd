# CodeTracer GDScript recorder — GF7 (Classes / Inheritance / super / inner
# classes / preload/load) reference program — MAIN DRIVER.
#
# Deterministic, headless. Run as a Godot main loop:
#   CT_GDSCRIPT_TRACE=<dir> godot --headless --script res://gf_zoo.gd
#
# `--script` requires the entry script to inherit from MainLoop / SceneTree
# (main/main.cpp: "Can't load the script ... as it doesn't inherit from
# SceneTree or MainLoop"), so THIS file is the runnable MainLoop; it drives the
# two-file class hierarchy in gf_animal.gd (base) + gf_dog.gd (derived).
#
# Exercises GF7 end to end (NO engine change — see scripts/EXPECTED-GF7.md):
#  - preload (COMPILE-TIME): `const DogScript = preload("res://gf_dog.gd")`;
#    `DogScript.new("rex")` instantiates Dog and drives Dog._init -> super
#    Animal._init (cross-file ctor chain) and Dog.speak -> super Animal.speak
#    (cross-file super). Both base frames resolve to res://gf_animal.gd.
#  - load (RUNTIME): `load("res://gf_dog.gd")` then `.new("spot")` drives the
#    SAME cross-file chains from a runtime-resolved script — asserting the
#    loaded script's method calls are traced with the correct per-frame source.
#  - inner classes: `AnimalScript.Tag.new().label()` (inner class of the BASE
#    file, frame source res://gf_animal.gd) and `DogScript.Kennel.new().size()`
#    (inner class of the DERIVED file, frame source res://gf_dog.gd).
#
# Deterministic checksum (hand-derived, see scripts/EXPECTED-GF7.md):
#   speak() = super.speak() ("...") + "woof" = "...woof" (7 chars)
#   s1 (preload Dog.speak) = "...woof" -> 7
#   s3 (load    Dog.speak) = "...woof" -> 7
#   lbl (Tag.label)        = "tag"     -> 3
#   kn  (Kennel.size)      = 3
#   total = 7 + 7 + 3 + 3 = 20  => prints "CT_GF7_RESULT=20"

extends MainLoop

const AnimalScript = preload("res://gf_animal.gd")
const DogScript = preload("res://gf_dog.gd")

func _init():
	# preload path: instantiate the concrete Dog via the compile-time-preloaded
	# script; Dog._init chains to Animal._init, Dog.speak to Animal.speak.
	var d1 = DogScript.new("rex")
	var s1: String = d1.speak()

	# load path: the SAME hierarchy resolved at RUNTIME via load().
	var DogDyn = load("res://gf_dog.gd")
	var d3 = DogDyn.new("spot")
	var s3: String = d3.speak()

	# inner class in the BASE file (Tag.label — frame source gf_animal.gd).
	var t = AnimalScript.Tag.new()
	var lbl: String = t.label()

	# inner class in the DERIVED file (Kennel.size — frame source gf_dog.gd).
	var k = DogScript.Kennel.new()
	var kn: int = k.size()

	var total = s1.length() + s3.length() + lbl.length() + kn
	print("CT_GF7_RESULT=%d" % total)

func _process(_delta):
	return true
