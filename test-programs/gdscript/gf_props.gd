# CodeTracer GDScript recorder — GF8 (Properties get/set & Annotations
# @export / @onready, incl. MEMBER-write capture) reference program.
#
# Deterministic, headless. Run as a Godot main loop:
#   CT_GDSCRIPT_TRACE=<dir> godot --headless --script res://gf_props.gd
#
# `--script` requires the entry to inherit SceneTree / MainLoop. This file is a
# SceneTree so it owns a `root` Window: it can add a Node child and thereby
# drive the @onready assignment (which the engine emits into `@implicit_ready`,
# reached only when a Node enters the tree — see gdscript_compiler.cpp).
#
# What GF8 closes (the class-MEMBER write gap deferred by G4/GF1/GF3/GF7):
# every write below lands in an instance-member or static-var slot, NOT a stack
# slot, so before GF8 none of them were captured. GF8 resolves the member index
# to its declared NAME (GDScript::debug_get_member_by_index /
# debug_get_static_var_by_index) and captures the written value on the current
# step, exactly like a stack local.
#
# Constructs exercised (all on the inner Gadget class):
#  - @export var hp: int = 100          — member initializer runs in the implicit
#    initializer as an OPCODE_ASSIGN into an ADDR_TYPE_MEMBER slot -> hp==100.
#  - @export_range(0, 10) var level: int = 3  — same path -> level==3.
#  - var x: int; x = 5 in _init         — plain member write (ADDR_TYPE_MEMBER).
#  - var _t: float = 0.0                — backing field; initializer -> _t==0.0.
#  - static var total := 0; total = total + 7  — static-var write via
#    OPCODE_SET_STATIC_VARIABLE -> total==7 (the static initializer also writes
#    total==0 in @static_initializer).
#  - @onready var ready_mark := 42      — assigned in @implicit_ready. Node
#    readiness is DEFERRED: root.add_child(g) schedules _ready, which the
#    SceneTree runs AFTER _initialize() returns (observed: the @implicit_ready
#    frame lands after run()). So ready_mark is 0 while run() executes and is
#    only assigned to 42 later — GF8 still CAPTURES ready_mark==42 on the
#    @implicit_ready frame (the @onready deliverable), it is just not read by
#    run(). (Driving _ready before the compute would need an extra idle frame;
#    the capture — the thing GF8 asserts — is present regardless.)
#  - computed property `temp: float` with get:/set(v):  — assigning `temp =
#    150.0` compiles to a CALL to the setter frame `@temp_setter`; its body
#    copies the incoming value into `got` (got==150.0, proving v==150 — GDScript
#    params are not captured directly, same read-into-local seam as GF7) and
#    writes the BACKING member `_t = clamp(got, 0.0, 100.0)` -> _t==100.0
#    (ADDR_TYPE_MEMBER write inside the setter). Reading `temp` compiles to a
#    CALL to the getter frame `@temp_getter`, whose return value is _t==100.0
#    (captured on the return event by GF5).
#
# Deterministic checksum (hand-derived, see scripts/EXPECTED-GF8.md):
#   temp = 150.0 -> setter clamps _t to 100.0
#   read_back    = temp (getter) = 100.0
#   sum = int(100.0) + hp(100) + x(5) + level(3) = 208  (ready_mark not yet set)
#   Gadget.total = 0 + 7 = 7
#   => prints "CT_GF8_RESULT=208" and "CT_GF8_TOTAL=7"

extends SceneTree

class Gadget:
	extends Node

	@export_group("stats")
	@export var hp: int = 100                 # @export member default -> hp==100
	@export_range(0, 10) var level: int = 3   # @export_range default  -> level==3

	var x: int                                # plain member (written in _init)
	var _t: float = 0.0                       # backing field for `temp`
	static var total := 0                     # static var (0, then 7)
	@onready var ready_mark := 42             # @onready (assigned in _ready)

	var temp: float:                          # computed property (get/set frames)
		get:
			return _t                         # getter body -> returns backing _t
		set(v):
			var got := v                      # capture incoming setter value -> got==150.0
			_t = clamp(got, 0.0, 100.0)       # setter body -> backing member write

	func _init() -> void:
		x = 5                                 # plain member write -> x==5
		total = total + 7                     # static-var write   -> total==7

	func run() -> int:
		temp = 150.0                          # invokes @temp_setter (got==150.0)
		var read_back := temp                 # invokes @temp_getter (returns 100.0)
		var sum := int(read_back) + hp + x + level
		return sum

	# GF8 follow-up: exercise the three NAMED member-write opcodes DIRECTLY so the
	# committed test locks them in (previously only manually verified by the
	# reviewer). Each named-member write is captured by NAME on its own step:
	#  - OPCODE_SET_NAMED           — in-place write on an UNTYPED base (`uv.x`);
	#    the base has no static type, so codegen falls back to the name-carrying
	#    opcode. Captured as `x == 9.0`.
	#  - OPCODE_SET_NAMED_VALIDATED — in-place write on a TYPED base (`tv.y`);
	#    the base's Vector2 static type has a validated setter for `y`, so codegen
	#    emits the validated variant (no StringName operand — the name is
	#    recovered from the DEBUG setter_names table). This is the GF8 gap that
	#    was previously SILENTLY DROPPED. Captured as `y == 8.0`.
	#  - OPCODE_SET_MEMBER          — a native self property write (`name = ...`
	#    resolves to Node.name). Captured as `name == "gadget1"`.
	func member_ops() -> void:
		var uv = Vector2(1.0, 2.0)            # untyped local (Variant) base
		uv.x = 9.0                            # OPCODE_SET_NAMED           -> x == 9.0
		var tv: Vector2 = Vector2(3.0, 4.0)   # typed local base
		tv.y = 8.0                            # OPCODE_SET_NAMED_VALIDATED -> y == 8.0
		name = "gadget1"                      # OPCODE_SET_MEMBER (native Node.name)


func _initialize() -> void:
	var g := Gadget.new()      # implicit init: hp=100, level=3, _t=0.0; _init: x=5, total=7
	root.add_child(g)          # schedules _ready -> @implicit_ready (ready_mark=42) runs later
	var r := g.run()           # property setter/getter frames + backing writes
	g.member_ops()             # GF8 follow-up: drive SET_NAMED / SET_NAMED_VALIDATED / SET_MEMBER
	print("CT_GF8_RESULT=%d" % r)
	print("CT_GF8_TOTAL=%d" % Gadget.total)
	g.queue_free()
	quit()


func _process(_delta: float) -> bool:
	return true
