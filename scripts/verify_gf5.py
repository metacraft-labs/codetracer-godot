#!/usr/bin/env python3
"""Assert GF5 (functions: default arguments, static functions, variadic builtins,
return typing + return VALUES) facts against a real .ct produced by the patched
engine, decoded via `ct-print --full`.

This is a genuine test: it EXITS NONZERO on any mismatch. The expected facts are
hand-derived in scripts/EXPECTED-GF5.md (first-principles, before recording) and
duplicated here as literals so the assertion is not circular.

The load-bearing assertions:

  - DEFAULT ARGS: `configure(a, b := 10, c := "x")` is called once WITHOUT the
    optional args (`configure(1)`) and once WITH them (`configure(2, 20, "yz")`).
    * In the DEFAULTED call the omitted params `b`/`c` are materialized by the
      default-argument bytecode via a real OPCODE_ASSIGN into the PARAM SLOT, so
      the value hook captures `b == 10` and `c == "x"` DIRECTLY on the param
      slots — captured exactly once (only the defaulted call runs that path).
    * A SUPPLIED arg is placed into the callee slot by the call prologue with no
      OPCODE_ASSIGN, so it is not re-captured on the param slot; its bound value
      is observed by copying it into a fresh local. `got_a`/`got_b`/`got_c` are
      those copies, captured in BOTH calls, proving the bound value each time:
      [1,2] / [10,20] / ["x","yz"] (defaulted call first, supplied call second).
  - STATIC FUNCTION: `static func mul(a, b)` records a normal nested call/return
    frame (depth 1, parent `_init`) with its args copied into `fa`/`fb` (6/7) and
    a captured return value 42.
  - RETURN VALUES (the GF5 recorder change): every return event carries the
    returned Variant. Asserted on `call_exit.return_value`:
    configure -> [12, 24] (Int); mul -> 42 (Int); area -> 12.56636 (Float);
    do_void -> None (a `-> void` function: retvalue is NIL, encoded as None, not
    the format's bare VoidReturnMarker); pick -> 42 (Int, UNTYPED return);
    _init -> None; _process -> Bool true; @implicit_new -> None.
  - CALL TREE: configure(x2)/mul/area/do_void/pick all nest under `_init`;
    @implicit_new and _process are engine top-level frames (depth 0, parent -1).
  - VARIADIC BUILTIN: `print("gf5", "variadic", total)` is a variadic BUILTIN
    call, recorded as a caller-frame STEP in `_init` (lines 69/70) — there is NO
    GDScript call_entry for `print`. GDScript 4 has NO user-defined varargs (a
    language N/A, see EXPECTED-GF5.md), so this is the only variadic surface.

Usage:
  verify_gf5.py verify <full.json>          # assert all GF5 facts (exit 0 = pass)
  verify_gf5.py tamper <full.json> <mode>   # corrupt the doc, expect the same
                                            # assertions to FAIL. mode is one of
                                            # default|return|staticargs|void.
                                            # exit 0 iff the tamper was caught.
"""
import json
import sys

FLOAT_TOL = 1e-6


class VerifyError(Exception):
    pass


# --- expected captured named-local values, in step order -------------------
# varname -> [(kind, expected_value), ...]. Hand-derived from
# test-programs/gdscript/gf_functions.gd (see EXPECTED-GF5.md).
EXPECTED_CAPTURES = {
    # DIRECT default materialization on the param slot (defaulted call only).
    "b": [("Int", 10)],
    "c": [("String", "x")],
    # Param copies, captured in BOTH calls: defaulted first, supplied second.
    "got_a": [("Int", 1), ("Int", 2)],
    "got_b": [("Int", 10), ("Int", 20)],
    "got_c": [("String", "x"), ("String", "yz")],
    # Static-function args (copied into named locals).
    "fa": [("Int", 6)],
    "fb": [("Int", 7)],
    # Typed-float param copy.
    "rad": [("Float", 2.0)],
    # void-function local.
    "touched": [("Int", 1)],
    # _init locals.
    "d1": [("Int", 12)],
    "d2": [("Int", 24)],
    "m": [("Int", 42)],
    "ar": [("Float", 12.56636)],
    "pk": [("Int", 42)],
    "total": [("Int", 120)],
}

# expected return values per function, grouped in call_key order.
EXPECTED_RETURNS = {
    "configure": [("Int", 12), ("Int", 24)],
    "mul": [("Int", 42)],
    "area": [("Float", 12.56636)],
    "do_void": [("None", None)],
    "pick": [("Int", 42)],
    "_init": [("None", None)],
    "_process": [("Bool", True)],
    "@implicit_new": [("None", None)],
}

# functions expected nested directly under _init (depth 1).
NESTED_UNDER_INIT = ["configure", "configure", "mul", "area", "do_void", "pick"]
# engine top-level frames (depth 0, parent -1).
TOP_LEVEL = ["@implicit_new", "_process"]

# the two variadic-builtin `print(...)` call-site lines in _init.
PRINT_STEP_LINES = [69, 70]


def load(path):
    with open(path) as f:
        return json.load(f)


def events(doc):
    return doc.get("events", [])


def steps(doc):
    return [e for e in events(doc) if e.get("kind") == "step"]


def call_entries(doc):
    return [e for e in events(doc) if e.get("kind") == "call_entry"]


def call_exits(doc):
    return [e for e in events(doc) if e.get("kind") == "call_exit"]


def scalar_str(kind, val):
    if kind == "Int":
        return "Int %r" % val.get("i")
    if kind == "Float":
        return "Float %r" % val.get("f")
    if kind == "String":
        return "String %r" % val.get("text")
    if kind == "Bool":
        return "Bool %r" % val.get("b")
    if kind == "None":
        return "None"
    return "%s?" % kind


def check_scalar(val, want_kind, want_value, path):
    got = val.get("kind")
    if got != want_kind:
        raise VerifyError("%s: kind %r != %r (%s)" % (path, got, want_kind, scalar_str(got, val)))
    if want_kind == "Int":
        if val.get("i") != want_value:
            raise VerifyError("%s: Int %r != %r" % (path, val.get("i"), want_value))
    elif want_kind == "Float":
        if abs(float(val.get("f")) - want_value) > FLOAT_TOL:
            raise VerifyError("%s: Float %r != %r" % (path, val.get("f"), want_value))
    elif want_kind == "String":
        if val.get("text") != want_value:
            raise VerifyError("%s: String %r != %r" % (path, val.get("text"), want_value))
    elif want_kind == "Bool":
        if bool(val.get("b")) != bool(want_value):
            raise VerifyError("%s: Bool %r != %r" % (path, val.get("b"), want_value))
    elif want_kind == "None":
        pass
    else:
        raise VerifyError("%s: unknown expected kind %r" % (path, want_kind))


def captures_by_name(doc):
    """varname -> [value-node, ...] in step order (across all steps)."""
    m = {}
    for s in steps(doc):
        for v in s.get("vars", []):
            m.setdefault(v.get("varname"), []).append(v.get("value", {}))
    return m


def returns_by_function(doc):
    """function -> [return_value-node, ...] in call_key order."""
    exits = sorted(call_exits(doc), key=lambda e: e.get("call_key", 0))
    m = {}
    for e in exits:
        m.setdefault(e.get("function"), []).append(e.get("return_value", {}))
    return m


def assert_facts(doc):
    observed = []

    # --- 1. captured named locals (defaults, copies, static args, ...) ------
    caps = captures_by_name(doc)
    for name, want_seq in EXPECTED_CAPTURES.items():
        got = caps.get(name, [])
        if len(got) != len(want_seq):
            raise VerifyError(
                "capture %r: got %d occurrences %s, expected %d %s"
                % (name, len(got),
                   [scalar_str(v.get("kind"), v) for v in got],
                   len(want_seq), [w[0] + " " + repr(w[1]) for w in want_seq]))
        for i, (want_kind, want_value) in enumerate(want_seq):
            check_scalar(got[i], want_kind, want_value, "%s[%d]" % (name, i))
        observed.append("%s=%s" % (name, [scalar_str(w[0], {"i": w[1], "f": w[1], "text": w[1], "b": w[1]}) for w in want_seq]))

    # --- 2. return values on call_exit events -------------------------------
    rets = returns_by_function(doc)
    for fn, want_seq in EXPECTED_RETURNS.items():
        got = rets.get(fn, [])
        if len(got) != len(want_seq):
            raise VerifyError(
                "returns for %r: got %d %s, expected %d"
                % (fn, len(got), [scalar_str(v.get("kind"), v) for v in got], len(want_seq)))
        for i, (want_kind, want_value) in enumerate(want_seq):
            check_scalar(got[i], want_kind, want_value, "return %s[%d]" % (fn, i))

    # --- 3. call tree: nesting under _init + top-level engine frames --------
    ces = call_entries(doc)
    inits = [c for c in ces if c.get("function") == "_init"]
    if len(inits) != 1:
        raise VerifyError("expected exactly 1 _init call_entry, got %d" % len(inits))
    init_key = inits[0]["call_key"]
    if inits[0].get("depth") != 0 or inits[0].get("parent_call_key") != -1:
        raise VerifyError("_init not a top-level frame: depth=%s parent=%s"
                          % (inits[0].get("depth"), inits[0].get("parent_call_key")))

    nested = [c for c in ces if c.get("parent_call_key") == init_key]
    nested_fns = sorted(c.get("function") for c in nested)
    if nested_fns != sorted(NESTED_UNDER_INIT):
        raise VerifyError("functions nested under _init %s != expected %s"
                          % (nested_fns, sorted(NESTED_UNDER_INIT)))
    for c in nested:
        if c.get("depth") != 1:
            raise VerifyError("%s nested under _init has depth %s (expected 1)"
                              % (c.get("function"), c.get("depth")))
        if c.get("entry_step") > c.get("exit_step"):
            raise VerifyError("%s malformed frame entry_step %s > exit_step %s"
                              % (c.get("function"), c.get("entry_step"), c.get("exit_step")))

    for fn in TOP_LEVEL:
        matches = [c for c in ces if c.get("function") == fn]
        if len(matches) != 1:
            raise VerifyError("expected exactly 1 %r frame, got %d" % (fn, len(matches)))
        c = matches[0]
        if c.get("depth") != 0 or c.get("parent_call_key") != -1:
            raise VerifyError("%s not top-level: depth=%s parent=%s"
                              % (fn, c.get("depth"), c.get("parent_call_key")))

    # balance: ct-print reconstructs a call_entry only for a returned call, so
    # every call_entry has a matching call_exit (the writer persists the call on
    # its return). Count them and require equality.
    if len(call_entries(doc)) != len(call_exits(doc)):
        raise VerifyError("unbalanced call/return: %d call_entry vs %d call_exit"
                          % (len(call_entries(doc)), len(call_exits(doc))))

    # --- 4. static-function frame present at depth 1 under _init ------------
    mul = [c for c in nested if c.get("function") == "mul"]
    if len(mul) != 1:
        raise VerifyError("static function mul not recorded as a single nested frame")

    # --- 5. variadic builtin: print is a caller-frame step, NOT a frame ------
    if any("print" in (c.get("function") or "") for c in ces):
        raise VerifyError("print recorded as a GDScript call_entry (should be a caller-frame native-call step)")
    init_step_lines = set(s.get("line") for s in steps(doc) if s.get("function") == "_init")
    for ln in PRINT_STEP_LINES:
        if ln not in init_step_lines:
            raise VerifyError("no _init step at variadic-builtin print line %d" % ln)

    # --- 6. types table: scalar-only, still the 6 base entries --------------
    types = doc.get("types", [])
    if types != ["None", "Int", "Float", "Bool", "String", "Variant"]:
        raise VerifyError("types table changed (return-value capture is scalar): %r" % types)

    return ("PASS GF5: defaults captured (b=10,c=\"x\" direct + got_[a,b,c] "
            "[1,2]/[10,20]/[x,yz]); static mul fa=6,fb=7 ret 42; return values "
            "configure[12,24] mul 42 area 12.56636(Float) do_void None(void) "
            "pick 42(untyped) _process Bool; call tree nested under _init; "
            "print variadic-builtin as caller-frame steps L69/70 (no print frame); "
            "types table 6 base entries. Captures: %s" % "; ".join(observed))


def tamper(doc, mode):
    """Corrupt the decoded doc so assert_facts SHOULD fail, proving teeth.

    - default:     wrong param default value (direct `b` default 10 -> 99).
    - return:      wrong return value (configure's first return 12 -> 99).
    - staticargs:  wrong static-call arg (mul's `fa` 6 -> 99).
    - void:        wrong void return (do_void None -> Int, i.e. not None).
    """
    if mode == "default":
        for s in steps(doc):
            for v in s.get("vars", []):
                if v.get("varname") == "b" and v.get("value", {}).get("i") == 10:
                    v["value"]["i"] = 99
                    return
        raise VerifyError("tamper(default): could not find b=10 to corrupt")
    elif mode == "return":
        for e in sorted(call_exits(doc), key=lambda e: e.get("call_key", 0)):
            if e.get("function") == "configure":
                e["return_value"]["i"] = 99
                return
        raise VerifyError("tamper(return): no configure call_exit")
    elif mode == "staticargs":
        for s in steps(doc):
            for v in s.get("vars", []):
                if v.get("varname") == "fa" and v.get("value", {}).get("i") == 6:
                    v["value"]["i"] = 99
                    return
        raise VerifyError("tamper(staticargs): could not find fa=6 to corrupt")
    elif mode == "void":
        for e in call_exits(doc):
            if e.get("function") == "do_void":
                e["return_value"] = {"kind": "Int", "i": 7, "type_id": 1}
                return
        raise VerifyError("tamper(void): no do_void call_exit")
    else:
        print("unknown tamper mode %r" % mode, file=sys.stderr)
        sys.exit(2)


def main():
    if len(sys.argv) < 3:
        print("usage: verify_gf5.py <verify|tamper> <full.json> [mode]", file=sys.stderr)
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
            print("usage: verify_gf5.py tamper <full.json> <default|return|staticargs|void>", file=sys.stderr)
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
