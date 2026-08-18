#!/usr/bin/env python3
"""Assert GF3 (collections: Array / typed Array / Packed*Array / Dictionary /
typed Dictionary, incl. one level of nesting + a mutation) facts against a real
.ct produced by the patched engine, decoded via `ct-print --full`.

This is a genuine test: it EXITS NONZERO on any mismatch. The expected facts are
hand-derived in scripts/EXPECTED-GF3.md (first-principles, before recording) and
duplicated here as literals so the assertion is not circular.

The load-bearing assertion is on the STRUCTURED value of each captured
collection local: its kind (Sequence / Tuple), element COUNT, and each element's
value + type, recursively — the exact CBOR the writer's streaming encoder
emitted and that MaterializedReplaySession decodes through the same TraceReader
path `ct-print --full` uses. So this asserts the decoded compound structure, not
merely that a trace exists.

  Array / Packed*Array -> Sequence of elements.
  Dictionary           -> Sequence of key/value Tuples (the Python/Ruby dict
                          pattern) — Tuple[key, value] per entry.

Usage:
  verify_gf3.py verify <full.json>          # assert all GF3 facts (exit 0 = pass)
  verify_gf3.py tamper <full.json> <mode>   # corrupt the doc, expect the same
                                            # assertions to FAIL. mode is one of
                                            # elem|length|dictkey|dictval|nesting.
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
#   ("Seq", [v, ...])          -> Sequence with those elements, in order
#   ("Tuple", [v, ...])        -> Tuple with those elements, in order
def I(n):
    return ("Int", n)


def F(x):
    return ("Float", x)


def S(s):
    return ("String", s)


def Seq(elems):
    return ("Seq", elems)


def Tup(elems):
    return ("Tuple", elems)


def Pair(k, v):
    return ("Tuple", [k, v])


# (line, varname, expected-value-structure) — hand-derived from
# test-programs/gdscript/gf_collections.gd (see EXPECTED-GF3.md).
EXPECTED = [
    (35, "a_untyped", Seq([I(1), S("two"), F(3.0)])),
    (36, "a_typed", Seq([I(10), I(20), I(30)])),
    (37, "p_byte", Seq([I(1), I(2), I(255)])),
    (38, "p_i32", Seq([I(100), I(200), I(300)])),
    (39, "p_i64", Seq([I(1000), I(2000)])),
    (40, "p_f32", Seq([F(1.5), F(2.5)])),
    (41, "p_f64", Seq([F(3.5), F(4.5)])),
    (42, "p_str", Seq([S("x"), S("y"), S("z")])),
    (43, "d_untyped", Seq([Pair(S("a"), I(1)), Pair(S("b"), I(2))])),
    (44, "d_typed", Seq([Pair(S("x"), I(10)), Pair(S("y"), I(20))])),
    (45, "nested", Seq([Seq([I(1), I(2)]), Seq([I(3), I(4)])])),
    (46, "d_with_arr", Seq([Pair(S("nums"), Seq([I(7), I(8), I(9)]))])),
    (48, "mut", Seq([I(1), I(2), I(3)])),
    (50, "mut_after", Seq([I(1), I(2), I(3), I(4)])),  # post-append: length 3 -> 4
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


def check_value(node, expected, path):
    """Recursively assert `node` (a ct-print value JSON) matches `expected`."""
    kind = expected[0]
    got_kind = node.get("kind")
    if kind == "Int":
        if got_kind != "Int":
            raise VerifyError("%s: kind %r != Int" % (path, got_kind))
        if node.get("i") != expected[1]:
            raise VerifyError("%s: Int %r != %r" % (path, node.get("i"), expected[1]))
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
    elif kind == "Bool":
        if got_kind != "Bool":
            raise VerifyError("%s: kind %r != Bool" % (path, got_kind))
        if bool(node.get("b")) != expected[1]:
            raise VerifyError("%s: Bool %r != %r" % (path, node.get("b"), expected[1]))
    elif kind == "None":
        if got_kind != "None":
            raise VerifyError("%s: kind %r != None" % (path, got_kind))
    elif kind in ("Seq", "Tuple"):
        want_kind = "Sequence" if kind == "Seq" else "Tuple"
        if got_kind != want_kind:
            raise VerifyError("%s: kind %r != %s" % (path, got_kind, want_kind))
        elems = node.get("elements")
        if not isinstance(elems, list):
            raise VerifyError("%s: %s has no elements array" % (path, want_kind))
        want = expected[1]
        if len(elems) != len(want):
            raise VerifyError(
                "%s: %s length %d != %d (elements=%r)"
                % (path, want_kind, len(elems), len(want),
                   [e.get("kind") for e in elems]))
        for i, (child, want_child) in enumerate(zip(elems, want)):
            check_value(child, want_child, "%s[%d]" % (path, i))
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


def assert_facts(doc):
    sts = all_steps(doc)
    observed = []
    for (line, varname, expected) in EXPECTED:
        st = step_at_unique_line(sts, line)
        v = find_var(st, varname)
        check_value(v.get("value", {}), expected, "%s@%d" % (varname, line))
        observed.append("%s@%d" % (varname, line))

    # Explicit, human-legible sub-facts (subsumed by the recursive check above,
    # asserted separately so the evidence is concrete in the log).
    sub = []
    # append mutation is visible: mut length 3 -> mut_after length 4.
    mut = find_var(step_at_unique_line(sts, 48), "mut")["value"]
    mut_after = find_var(step_at_unique_line(sts, 50), "mut_after")["value"]
    if len(mut.get("elements", [])) != 3 or len(mut_after.get("elements", [])) != 4:
        raise VerifyError("append mutation not reflected: len(mut)=%d len(mut_after)=%d"
                          % (len(mut.get("elements", [])), len(mut_after.get("elements", []))))
    if mut_after["elements"][3].get("i") != 4:
        raise VerifyError("appended element != 4: %r" % mut_after["elements"][3])
    sub.append("append 3->4 (last=4)")
    # PackedByteArray element 255 present.
    pbyte = find_var(step_at_unique_line(sts, 37), "p_byte")["value"]
    if pbyte["elements"][2].get("i") != 255:
        raise VerifyError("PackedByteArray[2] != 255: %r" % pbyte["elements"][2])
    sub.append("p_byte[2]=255")
    # dict as key/value tuples: first pair ("a", 1).
    dun = find_var(step_at_unique_line(sts, 43), "d_untyped")["value"]
    p0 = dun["elements"][0]
    if p0.get("kind") != "Tuple" or p0["elements"][0].get("text") != "a" or p0["elements"][1].get("i") != 1:
        raise VerifyError("d_untyped first pair != (a,1): %r" % p0)
    sub.append('d_untyped[0]=("a",1)')
    # nested array-of-arrays: nested[1][1] == 4.
    nested = find_var(step_at_unique_line(sts, 45), "nested")["value"]
    if nested["elements"][1]["elements"][1].get("i") != 4:
        raise VerifyError("nested[1][1] != 4: %r" % nested)
    sub.append("nested[1][1]=4")

    return ("PASS GF3: %d structured collections verified (%s); sub-facts: %s"
            % (len(EXPECTED), ", ".join(observed), "; ".join(sub)))


def tamper(doc, mode):
    """Corrupt the decoded doc so assert_facts SHOULD fail, proving teeth.

    - elem:    change a_untyped[0] from Int 1 to Int 999 (wrong element value).
    - length:  drop a_typed's last element (wrong length 3 -> 2).
    - dictkey: change d_untyped first pair key "a" -> "z" (wrong dict key).
    - dictval: change d_untyped first pair value 1 -> 999 (wrong dict value).
    - nesting: flatten nested[0] from a Sequence to an Int (wrong nesting shape).
    """
    sts = all_steps(doc)

    def val_at(line, name):
        return find_var(step_at_unique_line(sts, line), name)["value"]

    if mode == "elem":
        val_at(35, "a_untyped")["elements"][0]["i"] = 999
    elif mode == "length":
        val_at(36, "a_typed")["elements"].pop()
    elif mode == "dictkey":
        val_at(43, "d_untyped")["elements"][0]["elements"][0]["text"] = "z"
    elif mode == "dictval":
        val_at(43, "d_untyped")["elements"][0]["elements"][1]["i"] = 999
    elif mode == "nesting":
        node = val_at(45, "nested")["elements"][0]
        node.clear()
        node["kind"] = "Int"
        node["i"] = 12
    else:
        print("unknown tamper mode %r" % mode, file=sys.stderr)
        sys.exit(2)


def main():
    if len(sys.argv) < 3:
        print("usage: verify_gf3.py <verify|tamper> <full.json> [mode]", file=sys.stderr)
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
            print("usage: verify_gf3.py tamper <full.json> <elem|length|dictkey|dictval|nesting>", file=sys.stderr)
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
