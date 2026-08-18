#!/usr/bin/env python3
"""Assert GF8 (properties get/set & annotations @export/@onready, incl.
MEMBER-write capture) facts against a real .ct produced by the patched engine,
decoded via `ct-print --full`.

This is a genuine test: it EXITS NONZERO on any mismatch. The expected facts are
hand-derived in scripts/EXPECTED-GF8.md (first-principles, before recording) and
duplicated here as literals so the assertion is not circular.

The load-bearing findings (see EXPECTED-GF8.md for the full derivation):

  - GF8 captures writes into class-MEMBER slots (ADDR_TYPE_MEMBER) and static-var
    slots — the gap G4/GF1/GF3/GF7 deferred. Member writes are resolved to their
    declared NAME (GDScript::debug_get_member_by_index /
    debug_get_static_var_by_index) and land on the current step exactly like a
    stack local, so values.dat stays parallel-indexed to steps.dat.
  - @export defaults and @onready assignments are member initializers the
    compiler emits as OPCODE_ASSIGN into a MEMBER address -> captured by name.
  - A property `temp` with inline get/set records its accessors as FRAMES named
    @temp_getter / @temp_setter; the setter body's backing write `_t = ...` is a
    MEMBER write captured INSIDE the setter frame; the getter's `return _t` is
    captured as the return value (GF5).

Usage:
  verify_gf8.py verify <full.json>          # assert all GF8 facts (exit 0 = pass)
  verify_gf8.py tamper <full.json> <mode>   # corrupt the doc, expect the same
                                            # assertions to FAIL. mode is one of
                                            # value|membername|missingsetter.
                                            # exit 0 iff the tamper was caught.
"""
import json
import sys


class VerifyError(Exception):
    pass


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


def exit_by_key(doc):
    return {e.get("call_key"): e for e in call_exits(doc)}


# (function, source line, varname, value-kind, expected value).
# Hand-derived from test-programs/gdscript/gf_props.gd; see EXPECTED-GF8.md.
# Every entry is a MEMBER or static-var write (or the getter result read into a
# local) — the writes G4/GF1/GF7 deferred to GF8.
EXPECTED = [
    # @export defaults + backing-field initializer (member initializers run in
    # the implicit initializer as OPCODE_ASSIGN into a MEMBER slot).
    ("@implicit_new", 58, "hp", "Int", 100),
    ("@implicit_new", 59, "level", "Int", 3),
    ("@implicit_new", 62, "_t", "Float", 0.0),
    # plain member write + static-var mutation in _init.
    ("_init", 74, "x", "Int", 5),
    ("_init", 75, "total", "Int", 7),
    # property setter frame: incoming value copied to `got` (proves v==150.0),
    # then the BACKING member `_t` written (clamped) — both INSIDE @temp_setter.
    ("@temp_setter", 70, "got", "Float", 150.0),
    ("@temp_setter", 71, "_t", "Float", 100.0),
    # @onready assignment (runs in @implicit_ready when the node enters the tree).
    ("@implicit_ready", 64, "ready_mark", "Int", 42),
    # getter result read into a local (stack) — the getter frame ran.
    ("run", 80, "read_back", "Float", 100.0),
]

EXPECTED_TYPES = ["None", "Int", "Float", "Bool", "String", "Variant", "Object"]


def value_scalar(val):
    kind = val.get("kind")
    if kind == "Int":
        return val.get("i")
    if kind == "Float":
        return val.get("f")
    if kind == "Bool":
        return bool(val.get("b"))
    if kind == "String":
        return val.get("text")
    if kind == "None":
        return None
    return val.get("value", val)


def steps_at(sts, function, line):
    return [s for s in sts if s.get("function") == function and s.get("line") == line]


def find_capture(sts, function, line, varname):
    """Return the value node for `varname` captured on the step at (function,
    line). Requires EXACTLY ONE step at (function, line) that carries `varname`
    (a distinct frame may re-step the same source line without the capture — e.g.
    the outer script's @implicit_new walks the inner class body with no vars — so
    match on the var-carrying step, not merely (function, line))."""
    at = steps_at(sts, function, line)
    if not at:
        raise VerifyError("no step for function=%r line=%r" % (function, line))
    carrying = [(s, v) for s in at for v in s.get("vars", []) if v.get("varname") == varname]
    if not carrying:
        raise VerifyError(
            "no step %s:%d carries variable %r (vars at that line: %r)"
            % (function, line, varname,
               [[v.get("varname") for v in s.get("vars", [])] for s in at]))
    if len(carrying) != 1:
        raise VerifyError(
            "%d steps at %s:%d carry %r (expected exactly 1)"
            % (len(carrying), function, line, varname))
    return carrying[0][1]


def assert_facts(doc):
    sts = steps(doc)
    ces = call_entries(doc)
    exits = exit_by_key(doc)

    # --- A. types table (Object present: the Gadget node local is captured) ---
    types = doc.get("types")
    if types != EXPECTED_TYPES:
        raise VerifyError("types table %r != expected %r" % (types, EXPECTED_TYPES))

    # --- B. member / static / property captures, each on its own step ---------
    observed = []
    for (function, line, varname, kind, expected_val) in EXPECTED:
        v = find_capture(sts, function, line, varname)
        val = v.get("value", {})
        got_kind = val.get("kind")
        if got_kind != kind:
            raise VerifyError(
                "%s:%d %s has kind %r, expected %r" % (function, line, varname, got_kind, kind))
        got_val = value_scalar(val)
        if got_val != expected_val:
            raise VerifyError(
                "%s:%d %s = %r (kind %s), expected %r"
                % (function, line, varname, got_val, got_kind, expected_val))
        observed.append("%s=%r:%s" % (varname, expected_val, kind))

    # --- C. static-var initializer path: total==0 on @static_initializer ------
    static_total = [v for s in sts if s.get("function") == "@static_initializer"
                    for v in s.get("vars", []) if v.get("varname") == "total"]
    if not static_total:
        raise VerifyError("no @static_initializer step captured `total` (static-var init path)")
    if value_scalar(static_total[0].get("value", {})) != 0:
        raise VerifyError(
            "static-var init total != 0 (got %r)" % value_scalar(static_total[0].get("value", {})))

    # --- D. the `run` frame (parent of the property accessor frames) ----------
    runs = [c for c in ces if c.get("function") == "run"]
    if len(runs) != 1:
        raise VerifyError("expected exactly 1 `run` frame, got %d" % len(runs))
    run_key = runs[0]["call_key"]

    # --- E. property SETTER frame (@temp_setter): child of run, void return ---
    setters = [c for c in ces if c.get("function") == "@temp_setter"]
    if len(setters) != 1:
        raise VerifyError("expected exactly 1 @temp_setter frame, got %d" % len(setters))
    sc = setters[0]
    if sc.get("depth") != 2 or sc.get("parent_call_key") != run_key:
        raise VerifyError(
            "@temp_setter not depth2/child-of-run: depth=%s parent=%s (run=%s)"
            % (sc.get("depth"), sc.get("parent_call_key"), run_key))
    setter_ret = exits[sc["call_key"]].get("return_value", {})
    if setter_ret.get("kind") != "None":
        raise VerifyError("@temp_setter return kind %r != None" % setter_ret.get("kind"))

    # --- F. property GETTER frame (@temp_getter): child of run, returns 100.0 --
    getters = [c for c in ces if c.get("function") == "@temp_getter"]
    if len(getters) != 1:
        raise VerifyError("expected exactly 1 @temp_getter frame, got %d" % len(getters))
    gc = getters[0]
    if gc.get("depth") != 2 or gc.get("parent_call_key") != run_key:
        raise VerifyError(
            "@temp_getter not depth2/child-of-run: depth=%s parent=%s (run=%s)"
            % (gc.get("depth"), gc.get("parent_call_key"), run_key))
    getter_ret = exits[gc["call_key"]].get("return_value", {})
    if getter_ret.get("kind") != "Float" or getter_ret.get("f") != 100.0:
        raise VerifyError(
            "@temp_getter return %r/%r != Float/100.0"
            % (getter_ret.get("kind"), getter_ret.get("f")))

    # --- G. the backing member write _t=100.0 is INSIDE the setter frame ------
    # The EXPECTED entry (@temp_setter, 71, _t)=100.0 is matched by find_capture
    # ONLY on a step whose function is @temp_setter, so the backing member write
    # is proven to live inside the setter frame (not merely at that source line).

    # --- H. call/return balance ----------------------------------------------
    if len(call_entries(doc)) != len(call_exits(doc)):
        raise VerifyError(
            "unbalanced call/return: %d entry vs %d exit"
            % (len(call_entries(doc)), len(call_exits(doc))))

    return ("PASS GF8: %d member/static/property values captured by NAME on their "
            "own steps (hp=100,level=3 @export; _t=0.0 init; x=5 plain member; "
            "total 0->7 static var; got=150.0/_t=100.0 in @temp_setter backing "
            "write; ready_mark=42 @onready in @implicit_ready; read_back=100.0 via "
            "getter); property accessors are FRAMES (@temp_setter void / "
            "@temp_getter -> 100.0), both children of run; types=%s; call/return "
            "balanced. %s" % (len(EXPECTED), types, observed))


def tamper(doc, mode):
    """Corrupt the doc so assert_facts SHOULD fail, proving the check has teeth."""
    sts = steps(doc)

    def var_at(function, line, varname):
        """The value node for `varname` on the var-carrying step at (function,
        line) — the outer-script frame may re-step the same line with no vars."""
        for s in sts:
            if s.get("function") == function and s.get("line") == line:
                for v in s.get("vars", []):
                    if v.get("varname") == varname:
                        return v
        raise VerifyError("tamper: no %s captured at %s:%d" % (varname, function, line))

    if mode == "value":
        # wrong MEMBER value: flip @export hp from 100 to 999.
        var_at("@implicit_new", 58, "hp")["value"]["i"] = 999
    elif mode == "membername":
        # wrong MEMBER name: rename the plain member write `x` -> `notx`.
        var_at("_init", 74, "x")["varname"] = "notx"
    elif mode == "missingsetter":
        # drop the @temp_setter call_entry + its call_exit.
        setter_keys = {c.get("call_key") for c in call_entries(doc)
                       if c.get("function") == "@temp_setter"}
        doc["events"] = [
            e for e in doc.get("events", [])
            if not (e.get("kind") == "call_entry" and e.get("function") == "@temp_setter")
            and not (e.get("kind") == "call_exit" and e.get("call_key") in setter_keys)
        ]
    else:
        print("unknown tamper mode %r" % mode, file=sys.stderr)
        sys.exit(2)


def main():
    if len(sys.argv) < 3:
        print("usage: verify_gf8.py <verify|tamper> <full.json> [mode]", file=sys.stderr)
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
            print("usage: verify_gf8.py tamper <full.json> <value|membername|missingsetter>",
                  file=sys.stderr)
            sys.exit(2)
        mode = sys.argv[3]
        tamper(doc, mode)
        try:
            assert_facts(doc)
        except VerifyError as e:
            print("OK: tamper(%s) correctly rejected: %s" % (mode, e))
            sys.exit(0)
        print("FAIL: tamper(%s) slipped through the verifier (assertions still passed)"
              % mode, file=sys.stderr)
        sys.exit(1)

    print("unknown command %r" % cmd, file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
