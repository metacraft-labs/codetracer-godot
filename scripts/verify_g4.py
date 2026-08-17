#!/usr/bin/env python3
"""Assert G4 (captured values) facts against a real .ct produced by the patched
engine, decoded via `ct-print --full`.

This is a genuine test: it EXITS NONZERO on any mismatch. The expected facts are
hand-derived in scripts/EXPECTED-G4.md (first-principles, before recording) and
duplicated here as literals so the assertion is not circular.

Every expected value is asserted on the step whose (function, line) match — not
merely "present somewhere" — which is what proves values.dat stays
parallel-indexed to steps.dat (the MaterializedReplaySession invariant).

Usage:
  verify_g4.py verify <full.json>          # assert all G4 facts (exit 0 = pass)
  verify_g4.py tamper <full.json> <mode>   # corrupt the doc, expect the same
                                           # assertions to FAIL. mode is one of
                                           # value|name|step. exit 0 iff the
                                           # tamper was caught (assertions
                                           # failed as required).
"""
import json
import sys


class VerifyError(Exception):
    pass


def load(path):
    with open(path) as f:
        return json.load(f)


def steps(doc):
    return [e for e in doc.get("events", []) if e.get("kind") == "step"]


# (function, source line, varname, value-kind, expected value)
# Hand-derived from test-programs/gdscript/gf_values.gd; see EXPECTED-G4.md.
EXPECTED = [
    ("scale", 30, "received", "Int", 15),
    ("scale", 31, "factor", "Int", 30),
    ("_init", 35, "i", "Int", 10),
    ("_init", 36, "f", "Float", 2.5),
    ("_init", 37, "b", "Bool", True),
    ("_init", 38, "s", "String", "hi"),
    ("_init", 39, "n", "None", None),
    ("_init", 40, "i", "Int", 15),
]

EXPECTED_TYPES = ["None", "Int", "Float", "Bool", "String", "Variant"]


def value_scalar(val):
    """Extract the comparable scalar out of a decoded ct-print value node."""
    kind = val.get("kind")
    if kind == "Int":
        return val.get("i")
    if kind == "Float":
        return val.get("f")
    if kind == "Bool":
        return val.get("b")
    if kind == "String":
        return val.get("text")
    if kind == "None":
        return None
    return val.get("value", val)


def find_step(sts, function, line):
    matches = [s for s in sts if s.get("function") == function and s.get("line") == line]
    if not matches:
        raise VerifyError(
            "no step for function=%r line=%r" % (function, line))
    if len(matches) != 1:
        raise VerifyError(
            "expected exactly 1 step for function=%r line=%r, got %d"
            % (function, line, len(matches)))
    return matches[0]


def assert_facts(doc):
    """Raise VerifyError on any mismatch. Returns a human summary on success."""
    sts = steps(doc)

    # Types table: None must be TypeId(0), scalars follow, Variant is the
    # GF4 fallback extension point.
    types = doc.get("types")
    if types != EXPECTED_TYPES:
        raise VerifyError("types table %r != expected %r" % (types, EXPECTED_TYPES))

    observed = []
    for (function, line, varname, kind, expected_val) in EXPECTED:
        st = find_step(sts, function, line)
        vars_here = st.get("vars", [])
        hit = [v for v in vars_here if v.get("varname") == varname]
        if not hit:
            raise VerifyError(
                "step %s:%d carries no variable %r (vars=%r)"
                % (function, line, varname,
                   [v.get("varname") for v in vars_here]))
        if len(hit) != 1:
            raise VerifyError(
                "step %s:%d carries %d copies of %r"
                % (function, line, len(hit), varname))
        v = hit[0]
        val = v.get("value", {})
        got_kind = val.get("kind")
        if got_kind != kind:
            raise VerifyError(
                "%s:%d %s has kind %r, expected %r"
                % (function, line, varname, got_kind, kind))
        got_val = value_scalar(val)
        if got_val != expected_val:
            raise VerifyError(
                "%s:%d %s = %r (kind %s), expected %r"
                % (function, line, varname, got_val, got_kind, expected_val))
        observed.append("%s:%d %s=%r:%s" % (function, line, varname, expected_val, kind))

    # Parallel-index integrity: a value must NOT leak onto a step that did not
    # write it. Assert that no step OTHER than line 35/40 in _init carries `i`,
    # and that the two `i` captures carry the right per-line values (10 vs 15).
    i_captures = []
    for s in sts:
        if s.get("function") != "_init":
            continue
        for v in s.get("vars", []):
            if v.get("varname") == "i":
                i_captures.append((s.get("line"), value_scalar(v.get("value", {}))))
    if sorted(i_captures) != [(35, 10), (40, 15)]:
        raise VerifyError(
            "`i` captures %r != expected [(35, 10), (40, 15)] "
            "(a value leaked onto the wrong step)" % (sorted(i_captures),))

    return ("PASS G4: %d captured values on their own steps; "
            "types=%s; i=10@35 & i=15@40 parallel-indexed. %s"
            % (len(EXPECTED), types, observed))


def tamper(doc, mode):
    """Corrupt the doc so assert_facts SHOULD fail, proving the check has teeth.

    - value: change i's captured 10 to 999
    - name:  rename i's varname to a temporary-looking name
    - step:  move i's captured value from line 35 onto line 36
    """
    sts = steps(doc)

    def i_at(line):
        st = next(s for s in sts if s.get("function") == "_init" and s.get("line") == line)
        return st

    if mode == "value":
        st = i_at(35)
        for v in st.get("vars", []):
            if v.get("varname") == "i":
                v["value"]["i"] = 999
    elif mode == "name":
        st = i_at(35)
        for v in st.get("vars", []):
            if v.get("varname") == "i":
                v["varname"] = "@tmp"
    elif mode == "step":
        src = i_at(35)
        dst = i_at(36)
        moved = [v for v in src.get("vars", []) if v.get("varname") == "i"]
        src["vars"] = [v for v in src.get("vars", []) if v.get("varname") != "i"]
        dst.setdefault("vars", []).extend(moved)
    else:
        print("unknown tamper mode %r" % mode, file=sys.stderr)
        sys.exit(2)


def main():
    if len(sys.argv) < 3:
        print("usage: verify_g4.py <verify|tamper> <full.json> [mode]", file=sys.stderr)
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
            print("usage: verify_g4.py tamper <full.json> <value|name|step>", file=sys.stderr)
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
