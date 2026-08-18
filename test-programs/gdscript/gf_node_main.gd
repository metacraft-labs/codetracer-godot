# CodeTracer GDScript recorder — GF11 (Node lifecycle callbacks) driver.
#
# Deterministic, headless. Run as a Godot main loop:
#   CT_GDSCRIPT_TRACE=<dir> godot --headless --script res://gf_node_main.gd
#
# `--script` requires the entry to inherit SceneTree / MainLoop. This driver
# instantiates the lifecycle Node (gf_node.gd), adds it under get_root(), lets
# the real headless SceneTree TICK the node (driving `_process` /
# `_physics_process` / the per-frame `_notification`s), then removes it and quits
# — so the FULL lifecycle fires for real, with no mocks.
#
# DETERMINISM: the loop quits the instant the NODE's own `_process` counter
# reaches TARGET_PROCESS, so the node's `_process` fires EXACTLY TARGET_PROCESS
# times regardless of host speed (host timing only affects how many idle/physics
# ticks the SceneTree runs, never the node's process count once it is the quit
# trigger). `_physics_process` is time-driven by the headless main loop (a
# variable number of fixed-timestep ticks accumulates per idle frame), so its
# multiplicity is host-dependent — the recorder captures every tick that fires,
# and the verifier asserts it fired >= 1 time (documented in EXPECTED-GF11.md).

extends SceneTree

const GfNode = preload("res://gf_node.gd")
const TARGET_PROCESS := 3

var node

func _initialize() -> void:
	node = GfNode.new()             # _init fires here (construction)
	get_root().add_child(node)      # NOTIFICATION_PARENTED now; _enter_tree + _ready on the first tick

func _process(_delta: float) -> bool:
	if node.process_count >= TARGET_PROCESS:
		get_root().remove_child(node)   # _exit_tree fires here
		var total: int = node.init_count + node.enter_count + node.ready_count + node.process_count + node.exit_count
		print("CT_GF11_PROC=%d" % node.process_count)
		print("CT_GF11_PHYS=%d" % node.physics_count)
		print("CT_GF11_RESULT=%d" % total)
		node.free()
		quit()
	return false
