#!/usr/bin/env python3
"""Assert GF1 (static typing, inference, operators, constants, enums) facts
against a real .ct produced by the patched engine, decoded via
`ct-print --full`.

This is a genuine test: it EXITS NONZERO on any mismatch. The expected facts are
hand-derived in scripts/EXPECTED-GF1.md (first-principles, before recording) and
duplicated here as literals so the assertion is not circular.

Every expected value is asserted on the step whose (function, line) match — not
merely "present somewhere" — which is what proves values.dat stays
parallel-indexed to steps.dat (the MaterializedReplaySession invariant).

Usage:
  verify_gf1.py verify <full.json>          # assert all GF1 facts (exit 0 = pass)
  verify_gf1.py tamper <full.json> <mode>   # corrupt the doc, expect the same
                                            # assertions to FAIL. mode is one of
                                            # value|kind|step. exit 0 iff the
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
# Hand-derived from test-programs/gdscript/gf_typing.gd; see EXPECTED-GF1.md.
# NOTE: `counter` (static var) is intentionally ABSENT — its capture is GF8
# (member writes), not GF1.
EXPECTED = [
    # typed / inferred / untyped locals
    ("_init", 37, "a", "Int", 5),
    ("_init", 38, "b", "Float", 2.5),
    ("_init", 39, "c", "String", "s"),
    # runtime seeds (make the operator opcodes actually execute)
    ("_init", 42, "s7", "Int", 7),
    ("_init", 43, "s2", "Int", 2),
    ("_init", 44, "s12", "Int", 12),
    ("_init", 45, "s10", "Int", 10),
    ("_init", 46, "s1", "Int", 1),
    ("_init", 47, "sf7", "Float", 7.0),
    # arithmetic incl. power and integer/float division
    ("_init", 50, "pw", "Int", 1024),
    ("_init", 51, "idiv", "Int", 3),
    ("_init", 52, "fdiv", "Float", 3.5),
    ("_init", 53, "md", "Int", 1),
    # bitwise & | ^ ~ << >>
    ("_init", 56, "band", "Int", 8),
    ("_init", 57, "bor", "Int", 14),
    ("_init", 58, "bxor", "Int", 6),
    ("_init", 59, "bnot", "Int", -13),
    ("_init", 60, "shl", "Int", 16),
    ("_init", 61, "shr", "Int", 3),
    # comparison
    ("_init", 64, "cmp", "Bool", True),
    # logical and / or / not
    ("_init", 67, "land", "Bool", True),
    ("_init", 68, "lor", "Bool", True),
    ("_init", 69, "lnot", "Bool", True),
    # ternary
    ("_init", 72, "tern", "Int", 100),
    # type / identity operators is / as / in
    ("_init", 75, "ris", "Bool", True),
    ("_init", 76, "ras", "Float", 7.0),
    ("_init", 77, "rin", "Bool", True),
    # constant
    ("_init", 80, "d", "Int", 42),
    # enums (named + unnamed)
    ("_init", 83, "e", "Int", 5),
    ("_init", 84, "f", "Int", 1),
]

EXPECTED_TYPES = ["None", "Int", "Float", "Bool", "String", "Variant"]

# static-var write deferred to GF8 — assert it is NOT captured as a named local.
DEFERRED_ABSENT = ["counter"]


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
        raise VerifyError("no step for function=%r line=%r" % (function, line))
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
        observed.append("%s=%r:%s" % (varname, expected_val, kind))

    # GF8 boundary: the static var must NOT appear as a captured named local
    # anywhere in the trace.
    for s in sts:
        for v in s.get("vars", []):
            if v.get("varname") in DEFERRED_ABSENT:
                raise VerifyError(
                    "static var %r WAS captured at %s:%s — GF1 must defer member "
                    "writes to GF8" % (v.get("varname"), s.get("function"), s.get("line")))

    return ("PASS GF1: %d captured values on their own steps "
            "(typing/operators/const/enums); types=%s; static var deferred to "
            "GF8 (absent as expected). %s"
            % (len(EXPECTED), types, observed))


def tamper(doc, mode):
    """Corrupt the doc so assert_facts SHOULD fail, proving the check has teeth.

    - value: change pw's captured 1024 to 999 (wrong value)
    - kind:  change ris's Bool kind to Int (wrong type-kind)
    - step:  move the `as` result (ras) from line 76 onto line 75 (wrong step)
    """
    sts = steps(doc)

    def step_at(line):
        return next(s for s in sts if s.get("function") == "_init" and s.get("line") == line)

    if mode == "value":
        st = step_at(50)
        for v in st.get("vars", []):
            if v.get("varname") == "pw":
                v["value"]["i"] = 999
    elif mode == "kind":
        st = step_at(75)
        for v in st.get("vars", []):
            if v.get("varname") == "ris":
                v["value"]["kind"] = "Int"
                v["value"]["i"] = 1
    elif mode == "step":
        src = step_at(76)
        dst = step_at(75)
        moved = [v for v in src.get("vars", []) if v.get("varname") == "ras"]
        src["vars"] = [v for v in src.get("vars", []) if v.get("varname") != "ras"]
        dst.setdefault("vars", []).extend(moved)
    else:
        print("unknown tamper mode %r" % mode, file=sys.stderr)
        sys.exit(2)


def main():
    if len(sys.argv) < 3:
        print("usage: verify_gf1.py <verify|tamper> <full.json> [mode]", file=sys.stderr)
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
            print("usage: verify_gf1.py tamper <full.json> <value|kind|step>", file=sys.stderr)
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
