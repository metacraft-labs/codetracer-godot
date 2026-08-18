# CodeTracer GDScript recorder — GF4 (Full Variant builtin type surface:
# math / struct / handle types) reference program.
#
# Deterministic, headless. Run as a Godot main loop:
#   CT_GDSCRIPT_TRACE=<dir> godot --headless --script res://gf_variant_types.gd
#
# Exercises the GF4 encoder: every remaining math/struct Variant type is
# captured as a named-field Struct (one registered Struct type per Godot type),
# the handle/string types (StringName, NodePath, RID, Callable, Signal, Object)
# map per the design table, null maps to None, and the packed struct-arrays
# (PackedVector2/3Array, PackedColorArray — the GF3 Raw deferrals) become
# Sequences of the matching struct element.
#
# Each `var name := <expr>` is an OPCODE_ASSIGN into a named stack slot, captured
# by the G4 assign hook at that source line. Known field values are chosen so the
# verifier (scripts/verify_gf4.py) can assert each structure + field value from
# first principles (see scripts/EXPECTED-GF4.md).

extends MainLoop

signal my_signal(x)

func my_method():
	pass

func _init():
	# --- float/int vector structs ---
	var v2 := Vector2(1.5, 2.5)
	var v2i := Vector2i(3, 4)
	var v3 := Vector3(1.5, 2.5, 3.5)
	var v3i := Vector3i(5, 6, 7)
	var v4 := Vector4(1.0, 2.0, 3.0, 4.0)
	var v4i := Vector4i(8, 9, 10, 11)
	# --- composite structs (fields are themselves structs) ---
	var r2 := Rect2(1.0, 2.0, 3.0, 4.0)
	var r2i := Rect2i(5, 6, 7, 8)
	var col := Color(0.1, 0.2, 0.3, 1.0)
	var pl := Plane(0.0, 1.0, 0.0, 5.0)
	var q := Quaternion(0.0, 0.0, 0.0, 1.0)
	var ab := AABB(Vector3(1, 2, 3), Vector3(4, 5, 6))
	var bs := Basis(Vector3(2, 0, 0), Vector3(0, 3, 0), Vector3(0, 0, 4))
	var t2 := Transform2D(0.0, Vector2(9, 10))
	var t3 := Transform3D(Basis(), Vector3(7, 8, 9))
	var proj := Projection()
	# --- handle / string types ---
	var sname := &"foo"
	var npath := ^"a/b"
	var rid := RID()
	var callable := Callable(self, "my_method")
	var sig := Signal(self, "my_signal")
	var obj := RefCounted.new()
	var nil_val = null
	# --- packed struct-arrays (GF3 Raw deferrals, now Sequences of structs) ---
	var pv2 := PackedVector2Array([Vector2(1, 2), Vector2(3, 4)])
	var pv3 := PackedVector3Array([Vector3(1, 2, 3)])
	var pcol := PackedColorArray([Color(1, 0, 0, 1)])

	var checksum := v2i.x + v2i.y \
		+ v3i.x + v3i.y + v3i.z \
		+ v4i.x + v4i.y + v4i.z + v4i.w \
		+ int(r2.size.x) + int(r2.size.y) \
		+ r2i.position.x + r2i.position.y \
		+ int(pl.d) \
		+ int(q.w) \
		+ int(ab.size.x) \
		+ int(bs.x.x) \
		+ int(t2.origin.x) + int(t2.origin.y) \
		+ int(t3.origin.z) \
		+ int(proj.w.w) \
		+ String(sname).length() \
		+ int(rid.get_id()) \
		+ pv2.size() + pv3.size() + pcol.size()
	print("CT_GF4_RESULT=%d" % checksum)

func _process(_delta):
	return true
