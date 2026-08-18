#!/usr/bin/env python3
"""Assert GF4 (full Variant builtin type surface: math / struct / handle types)
facts against a real .ct produced by the patched engine, decoded via
`ct-print --full`.

This is a genuine test: it EXITS NONZERO on any mismatch. The expected facts are
hand-derived in scripts/EXPECTED-GF4.md (first-principles, before recording) and
duplicated here as literals so the assertion is not circular.

The load-bearing assertion is on the STRUCTURED value of each captured local:

  - Math/struct types (Vector2/2i/3/3i/4/4i, Rect2/2i, Plane, Quaternion, AABB,
    Basis, Transform2D/3D, Projection, Color) decode to a `Struct` whose
    `field_values` (positional, in the Godot type's canonical field order) carry
    the constructed values, and whose registered type NAME (resolved via the
    root `types` table from the value node's own `type_id`) is the Godot type
    (e.g. "Vector3"). Nested struct fields (Rect2.position, Transform3D.basis,
    ...) decode to nested `Struct`s and are checked recursively.
  - Handle types: StringName/NodePath -> `String`; RID/Callable/Signal/Object
    -> shallow `Struct` (RID{id}, Callable{method}, Signal{name},
    Object{class,id}); null -> `None`.
  - Packed struct-arrays (PackedVector2/3Array, PackedColorArray) -> `Sequence`
    (type name "Array") of the matching struct element.

The per-var `type_name` that ct-print prints for a cbor-registered value is
`None` (register_variable_cbor stores type_id 0 because the real type is inside
the CBOR); the authoritative type identity is therefore the VALUE NODE's own
`type_id`, resolved against the root `types` array — which this verifier does.

Usage:
  verify_gf4.py verify <full.json>          # assert all GF4 facts (exit 0 = pass)
  verify_gf4.py tamper <full.json> <mode>   # corrupt the doc, expect the same
                                            # assertions to FAIL. mode is one of
                                            # fieldval|kind|typename|shape.
                                            # exit 0 iff the tamper was caught.
"""
import json
import sys

FLOAT_TOL = 1e-6


class VerifyError(Exception):
    pass


# --- Expected-value DSL ----------------------------------------------------
# A value is one of:
#   ("Int", n) ("Float", x) ("String", s) ("Bool", b) ("None",)
#   ("AnyInt",)                 -> any Int (value not asserted; e.g. instance id)
#   ("Struct", type_name, [v])  -> Struct with those field_values, in order, and
#                                  the given registered type name
#   ("Seq", type_name, [v])     -> Sequence (type_name usually "Array")
def I(n):
    return ("Int", n)


def F(x):
    return ("Float", x)


def S(s):
    return ("String", s)


def N():
    return ("None",)


def AnyI():
    return ("AnyInt",)


def St(type_name, fields):
    return ("Struct", type_name, fields)


def SeqT(type_name, elems):
    return ("Seq", type_name, elems)


# (line, varname, expected-value-structure) — hand-derived from
# test-programs/gdscript/gf_variant_types.gd (see EXPECTED-GF4.md).
V2 = lambda a, b: St("Vector2", [F(a), F(b)])
V2I = lambda a, b: St("Vector2i", [I(a), I(b)])
V3 = lambda a, b, c: St("Vector3", [F(a), F(b), F(c)])
V4 = lambda a, b, c, d: St("Vector4", [F(a), F(b), F(c), F(d)])

EXPECTED = [
    (28, "v2", V2(1.5, 2.5)),
    (29, "v2i", V2I(3, 4)),
    (30, "v3", V3(1.5, 2.5, 3.5)),
    (31, "v3i", St("Vector3i", [I(5), I(6), I(7)])),
    (32, "v4", V4(1.0, 2.0, 3.0, 4.0)),
    (33, "v4i", St("Vector4i", [I(8), I(9), I(10), I(11)])),
    (35, "r2", St("Rect2", [V2(1.0, 2.0), V2(3.0, 4.0)])),
    (36, "r2i", St("Rect2i", [V2I(5, 6), V2I(7, 8)])),
    (37, "col", St("Color", [F(0.1), F(0.2), F(0.3), F(1.0)])),
    (38, "pl", St("Plane", [V3(0.0, 1.0, 0.0), F(5.0)])),
    (39, "q", St("Quaternion", [F(0.0), F(0.0), F(0.0), F(1.0)])),
    (40, "ab", St("AABB", [V3(1.0, 2.0, 3.0), V3(4.0, 5.0, 6.0)])),
    (41, "bs", St("Basis", [V3(2.0, 0.0, 0.0), V3(0.0, 3.0, 0.0), V3(0.0, 0.0, 4.0)])),
    (42, "t2", St("Transform2D", [V2(1.0, 0.0), V2(0.0, 1.0), V2(9.0, 10.0)])),
    (43, "t3", St("Transform3D",
                  [St("Basis", [V3(1.0, 0.0, 0.0), V3(0.0, 1.0, 0.0), V3(0.0, 0.0, 1.0)]),
                   V3(7.0, 8.0, 9.0)])),
    (44, "proj", St("Projection",
                    [V4(1.0, 0.0, 0.0, 0.0), V4(0.0, 1.0, 0.0, 0.0),
                     V4(0.0, 0.0, 1.0, 0.0), V4(0.0, 0.0, 0.0, 1.0)])),
    (46, "sname", S("foo")),
    (47, "npath", S("a/b")),
    (48, "rid", St("RID", [I(0)])),
    (49, "callable", St("Callable", [S("my_method")])),
    (50, "sig", St("Signal", [S("my_signal")])),
    (51, "obj", St("Object", [S("RefCounted"), AnyI()])),
    (52, "nil_val", N()),
    (54, "pv2", SeqT("Array", [V2(1.0, 2.0), V2(3.0, 4.0)])),
    (55, "pv3", SeqT("Array", [V3(1.0, 2.0, 3.0)])),
    (56, "pcol", SeqT("Array", [St("Color", [F(1.0), F(0.0), F(0.0), F(1.0)])])),
]


def load(path):
    with open(path) as f:
        return json.load(f)


def all_steps(doc):
    return [e for e in doc.get("events", []) if e.get("kind") == "step"]


def step_at_unique_line(sts, line):
    matches = [s for s in sts if s.get("line") == line]
    if not matches:
        raise VerifyError("no step at line %d" % line)
    if len(matches) != 1:
        raise VerifyError("expected exactly 1 step at line %d, got %d" % (line, len(matches)))
    return matches[0]


def type_name_of(doc, node, path):
    """Resolve a value node's registered type name via the root `types` table."""
    tid = node.get("type_id")
    if tid is None:
        raise VerifyError("%s: value node has no type_id" % path)
    types = doc.get("types", [])
    if not isinstance(tid, int) or tid < 0 or tid >= len(types):
        raise VerifyError("%s: type_id %r out of range (types len %d)" % (path, tid, len(types)))
    return types[tid]


def check_value(doc, node, expected, path):
    """Recursively assert `node` (a ct-print value JSON) matches `expected`."""
    kind = expected[0]
    got_kind = node.get("kind")
    if kind == "Int":
        if got_kind != "Int":
            raise VerifyError("%s: kind %r != Int" % (path, got_kind))
        if node.get("i") != expected[1]:
            raise VerifyError("%s: Int %r != %r" % (path, node.get("i"), expected[1]))
    elif kind == "AnyInt":
        if got_kind != "Int":
            raise VerifyError("%s: kind %r != Int (AnyInt)" % (path, got_kind))
    elif kind == "Float":
        if got_kind != "Float":
            raise VerifyError("%s: kind %r != Float" % (path, got_kind))
        if abs(float(node.get("f")) - expected[1]) > FLOAT_TOL:
            raise VerifyError("%s: Float %r != %r" % (path, node.get("f"), expected[1]))
    elif kind == "String":
        if got_kind != "String":
            raise VerifyError("%s: kind %r != String" % (path, got_kind))
        if node.get("text") != expected[1]:
            raise VerifyError("%s: String %r != %r" % (path, node.get("text"), expected[1]))
    elif kind == "None":
        if got_kind != "None":
            raise VerifyError("%s: kind %r != None" % (path, got_kind))
    elif kind == "Struct":
        want_type, want_fields = expected[1], expected[2]
        if got_kind != "Struct":
            raise VerifyError("%s: kind %r != Struct" % (path, got_kind))
        tn = type_name_of(doc, node, path)
        if tn != want_type:
            raise VerifyError("%s: struct type_name %r != %r" % (path, tn, want_type))
        fields = node.get("field_values")
        if not isinstance(fields, list):
            raise VerifyError("%s: Struct has no field_values array" % path)
        if len(fields) != len(want_fields):
            raise VerifyError(
                "%s: Struct %s field count %d != %d"
                % (path, want_type, len(fields), len(want_fields)))
        for i, (child, want_child) in enumerate(zip(fields, want_fields)):
            check_value(doc, child, want_child, "%s.%d" % (path, i))
    elif kind == "Seq":
        want_type, want_elems = expected[1], expected[2]
        if got_kind != "Sequence":
            raise VerifyError("%s: kind %r != Sequence" % (path, got_kind))
        tn = type_name_of(doc, node, path)
        if tn != want_type:
            raise VerifyError("%s: sequence type_name %r != %r" % (path, tn, want_type))
        elems = node.get("elements")
        if not isinstance(elems, list):
            raise VerifyError("%s: Sequence has no elements array" % path)
        if len(elems) != len(want_elems):
            raise VerifyError(
                "%s: Sequence length %d != %d" % (path, len(elems), len(want_elems)))
        for i, (child, want_child) in enumerate(zip(elems, want_elems)):
            check_value(doc, child, want_child, "%s[%d]" % (path, i))
    else:
        raise VerifyError("%s: unknown expected kind %r" % (path, kind))


def find_var(step, varname):
    hits = [v for v in step.get("vars", []) if v.get("varname") == varname]
    if not hits:
        raise VerifyError(
            "step line %d carries no var %r (vars=%r)"
            % (step.get("line"), varname, [v.get("varname") for v in step.get("vars", [])]))
    if len(hits) != 1:
        raise VerifyError("step line %d carries %d copies of var %r" % (step.get("line"), len(hits), varname))
    return hits[0]


def assert_types_table_has(doc, names):
    types = doc.get("types", [])
    for n in names:
        if n not in types:
            raise VerifyError("types table missing %r (types=%r)" % (n, types))


def assert_facts(doc):
    sts = all_steps(doc)
    observed = []
    for (line, varname, expected) in EXPECTED:
        st = step_at_unique_line(sts, line)
        v = find_var(st, varname)
        check_value(doc, v.get("value", {}), expected, "%s@%d" % (varname, line))
        observed.append("%s@%d" % (varname, line))

    # The base scalar types must still lead the table (None at TypeId 0), and
    # the new per-Godot-type Struct types must be interned (lazily, so scalar-only
    # traces keep exactly the 6 base entries — asserted by the GF3/G4 regressions).
    types = doc.get("types", [])
    if types[:6] != ["None", "Int", "Float", "Bool", "String", "Variant"]:
        raise VerifyError("base types table changed: %r" % types[:6])
    assert_types_table_has(doc, [
        "Vector2", "Vector2i", "Vector3", "Vector3i", "Vector4", "Vector4i",
        "Rect2", "Rect2i", "Color", "Plane", "Quaternion", "AABB", "Basis",
        "Transform2D", "Transform3D", "Projection", "RID", "Callable", "Signal",
        "Object"])

    # Human-legible sub-facts (subsumed by the recursive check, restated so the
    # evidence is concrete in the log).
    sub = []
    v3 = find_var(step_at_unique_line(sts, 30), "v3")["value"]
    sub.append("v3=Struct Vector3{%.1f,%.1f,%.1f}" % (
        v3["field_values"][0]["f"], v3["field_values"][1]["f"], v3["field_values"][2]["f"]))
    col = find_var(step_at_unique_line(sts, 37), "col")["value"]
    sub.append("col.a=%.1f" % col["field_values"][3]["f"])
    t3 = find_var(step_at_unique_line(sts, 43), "t3")["value"]
    sub.append("t3.origin.z=%.1f (nested Basis+Vector3)" % t3["field_values"][1]["field_values"][2]["f"])
    obj = find_var(step_at_unique_line(sts, 51), "obj")["value"]
    sub.append("obj.class=%s" % obj["field_values"][0]["text"])
    pv2 = find_var(step_at_unique_line(sts, 54), "pv2")["value"]
    sub.append("pv2=Seq[Vector2{%.1f,%.1f}, Vector2{%.1f,%.1f}]" % (
        pv2["elements"][0]["field_values"][0]["f"], pv2["elements"][0]["field_values"][1]["f"],
        pv2["elements"][1]["field_values"][0]["f"], pv2["elements"][1]["field_values"][1]["f"]))

    return ("PASS GF4: %d structured Variant types verified (%s); sub-facts: %s"
            % (len(EXPECTED), ", ".join(observed), "; ".join(sub)))


def tamper(doc, mode):
    """Corrupt the decoded doc so assert_facts SHOULD fail, proving teeth.

    - fieldval: change v3.x Float 1.5 -> 9.9 (wrong field value).
    - kind:     turn v3 from a Struct into an Int (wrong kind).
    - typename: rename v3's registered type "Vector3" -> "Bogus" (wrong type).
    - shape:    flatten r2.position from a Struct to an Int (wrong nested shape).
    """
    sts = all_steps(doc)

    def val_at(line, name):
        return find_var(step_at_unique_line(sts, line), name)["value"]

    if mode == "fieldval":
        val_at(30, "v3")["field_values"][0]["f"] = 9.9
    elif mode == "kind":
        node = val_at(30, "v3")
        node.clear()
        node["kind"] = "Int"
        node["i"] = 7
        node["type_id"] = 1
    elif mode == "typename":
        node = val_at(30, "v3")
        tid = node["type_id"]
        doc["types"][tid] = "Bogus"
    elif mode == "shape":
        node = val_at(35, "r2")["field_values"][0]
        node.clear()
        node["kind"] = "Int"
        node["i"] = 1
        node["type_id"] = 1
    else:
        print("unknown tamper mode %r" % mode, file=sys.stderr)
        sys.exit(2)


def main():
    if len(sys.argv) < 3:
        print("usage: verify_gf4.py <verify|tamper> <full.json> [mode]", file=sys.stderr)
        sys.exit(2)
    cmd, path = sys.argv[1], sys.argv[2]
    doc = load(path)

    if cmd == "verify":
        try:
            print(assert_facts(doc))
        except VerifyError as e:
            print("FAIL: " + str(e), file=sys.stderr)
            sys.exit(1)
        sys.exit(0)

    if cmd == "tamper":
        if len(sys.argv) != 4:
            print("usage: verify_gf4.py tamper <full.json> <fieldval|kind|typename|shape>", file=sys.stderr)
            sys.exit(2)
        mode = sys.argv[3]
        tamper(doc, mode)
        try:
            assert_facts(doc)
        except VerifyError as e:
            print("OK: tamper(%s) correctly rejected: %s" % (mode, e))
            sys.exit(0)
        print("FAIL: tamper(%s) slipped through the verifier" % mode, file=sys.stderr)
        sys.exit(1)

    print("unknown command %r" % cmd, file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
