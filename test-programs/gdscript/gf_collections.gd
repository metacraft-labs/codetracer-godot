# CodeTracer GDScript recorder — GF3 (Collections) reference program.
#
# Deterministic, headless. Run as a Godot main loop:
#   CT_GDSCRIPT_TRACE=<dir> godot --headless --script res://gf_collections.gd
#
# Exercises the GF3 structured-collection encoder: untyped Array, typed
# Array[int], the scalar Packed*Array family, untyped Dictionary, typed
# Dictionary[String, int], one level of nesting (array-of-arrays and a dict with
# an array value), and a mutation (append) whose post-mutation value is captured
# by reassigning the array into a fresh named local.
#
# Each `var name := <collection>` declaration is an OPCODE_ASSIGN (or the typed
# variant) into a named stack slot, captured by the G4 assign hook at that line;
# GF3 replaces the old Raw fallback with real Sequence / Tuple CBOR structure.
#
# Named locals and their expected captured STRUCTURE (see EXPECTED-GF3.md):
#   line 35  a_untyped   -> Sequence[Int 1, String "two", Float 3.0]
#   line 36  a_typed     -> Sequence[Int 10, Int 20, Int 30]        (Array[int])
#   line 37  p_byte      -> Sequence[Int 1, Int 2, Int 255]         (PackedByteArray)
#   line 38  p_i32       -> Sequence[Int 100, Int 200, Int 300]     (PackedInt32Array)
#   line 39  p_i64       -> Sequence[Int 1000, Int 2000]            (PackedInt64Array)
#   line 40  p_f32       -> Sequence[Float 1.5, Float 2.5]          (PackedFloat32Array)
#   line 41  p_f64       -> Sequence[Float 3.5, Float 4.5]          (PackedFloat64Array)
#   line 42  p_str       -> Sequence[String "x", String "y", String "z"] (PackedStringArray)
#   line 43  d_untyped   -> Sequence[Tuple("a",Int 1), Tuple("b",Int 2)]
#   line 44  d_typed     -> Sequence[Tuple("x",Int 10), Tuple("y",Int 20)] (Dictionary[String,int])
#   line 45  nested      -> Sequence[Sequence[Int 1,Int 2], Sequence[Int 3,Int 4]]
#   line 46  d_with_arr  -> Sequence[Tuple("nums", Sequence[Int 7,Int 8,Int 9])]
#   line 48  mut         -> Sequence[Int 1, Int 2, Int 3]           (pre-mutation)
#   line 50  mut_after   -> Sequence[Int 1, Int 2, Int 3, Int 4]    (post-append)

extends MainLoop

func _init():
	var a_untyped := [1, "two", 3.0]
	var a_typed: Array[int] = [10, 20, 30]
	var p_byte := PackedByteArray([1, 2, 255])
	var p_i32 := PackedInt32Array([100, 200, 300])
	var p_i64 := PackedInt64Array([1000, 2000])
	var p_f32 := PackedFloat32Array([1.5, 2.5])
	var p_f64 := PackedFloat64Array([3.5, 4.5])
	var p_str := PackedStringArray(["x", "y", "z"])
	var d_untyped := {"a": 1, "b": 2}
	var d_typed: Dictionary[String, int] = {"x": 10, "y": 20}
	var nested := [[1, 2], [3, 4]]
	var d_with_arr := {"nums": [7, 8, 9]}

	var mut := [1, 2, 3]
	mut.append(4)
	var mut_after := mut

	var checksum = a_untyped.size() \
		+ a_typed[0] + a_typed[1] + a_typed[2] \
		+ p_byte[2] \
		+ p_i32[0] \
		+ p_i64.size() \
		+ p_f32.size() \
		+ p_f64.size() \
		+ p_str.size() \
		+ d_untyped["a"] + d_untyped["b"] \
		+ d_typed["x"] + d_typed["y"] \
		+ nested[0][0] + nested[1][1] \
		+ d_with_arr["nums"][0] \
		+ mut_after.size()
	print("CT_GF3_RESULT=%d" % checksum)

func _process(_delta):
	return true
