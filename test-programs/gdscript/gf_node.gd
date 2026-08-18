# CodeTracer GDScript recorder — GF11 (Node lifecycle callbacks) reference node.
#
# A plain `extends Node` that implements every engine-invoked lifecycle
# callback. It is driven through its lifecycle by the SceneTree driver
# `gf_node_main.gd` (add_child -> tick N frames -> remove_child -> quit).
#
# Every callback is engine-invoked, yet each still enters GDScriptFunction::call,
# so the existing G2 step / G3 call-return / G4 value hooks record each callback
# as its own FRAME with NO engine change. Each callback lives on its own line and
# performs a deterministic side effect (a member counter increment, captured by
# the GF8 member-write hook) so its multiplicity is checkable, and reads its
# argument into a STACK LOCAL (`var d := delta`, `var n := what`) so the G4
# assign hook captures the per-frame `delta` (Float) and the `_notification`
# `what` (Int) as a decoded value on that callback's step.
#
# See scripts/EXPECTED-GF11.md for the first-principles expected facts.

extends Node

var init_count := 0
var enter_count := 0
var ready_count := 0
var process_count := 0
var physics_count := 0
var exit_count := 0
var last_notif := 0

func _init() -> void:
	init_count += 1

func _enter_tree() -> void:
	enter_count += 1

func _ready() -> void:
	ready_count += 1

func _process(delta: float) -> void:
	var d := delta              # capture the per-frame delta (Float) as a local
	process_count += 1

func _physics_process(delta: float) -> void:
	var pd := delta             # capture the physics-tick delta (Float) as a local
	physics_count += 1

func _notification(what: int) -> void:
	var n := what               # capture the notification code (Int) as a local
	last_notif = n

func _exit_tree() -> void:
	exit_count += 1
