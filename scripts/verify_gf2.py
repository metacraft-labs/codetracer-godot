#!/usr/bin/env python3
"""Assert GF2 (control flow & match, all pattern kinds) facts against a real .ct
produced by the patched engine, decoded via `ct-print --full`.

This is a genuine test: it EXITS NONZERO on any mismatch. The expected facts are
hand-derived in scripts/EXPECTED-GF2.md (first-principles, before recording) and
duplicated here as literals so the assertion is not circular.

The load-bearing assertion is on the EXACT recorded step-line SEQUENCE (order +
multiplicity). Because every source statement in gf_control_flow.gd occupies a
globally-unique line, the ordered list of step line numbers fully determines the
control flow that ran: which if/elif/else branch was taken (the taken branch's
body line appears, untaken bodies do NOT), how many times each loop body ran
(iteration count = occurrences of the body line), how break/continue/pass alter
the stream, and which match arm was dispatched for every pattern kind.

Function labels are intentionally NOT asserted for the step sequence: ct-print
mislabels the FIRST step of the trace (the frame-name attribution lags by one at
frame entry — the line number is correct, only the function label is off), so we
assert on line numbers, which are authoritative and unique per statement. The
_process MainLoop callback's trailing step is excluded (it is not part of the
control-flow logic under test).

Match binding VALUES (b, rest, first, dv, g) are asserted by (line, varname,
value) — each is captured at its match PATTERN line via the G4 assign hook.

Usage:
  verify_gf2.py verify <full.json>          # assert all GF2 facts (exit 0 = pass)
  verify_gf2.py tamper <full.json> <mode>   # corrupt the doc, expect the same
                                            # assertions to FAIL. mode is one of
                                            # branch|iter|binding. exit 0 iff the
                                            # tamper was caught.
"""
import json
import sys


class VerifyError(Exception):
    pass


def load(path):
    with open(path) as f:
        return json.load(f)


def all_steps(doc):
    return [e for e in doc.get("events", []) if e.get("kind") == "step"]


def control_flow_steps(doc):
    """Step events that belong to the user control-flow logic (_init + helpers).

    Excludes the _process MainLoop callback and any engine-synthesized
    @-prefixed frame; keeps every user step (including the first, whose function
    LABEL ct-print mislabels but whose LINE is correct)."""
    out = []
    for s in all_steps(doc):
        fn = s.get("function") or ""
        if fn == "_process" or fn.startswith("@"):
            continue
        out.append(s)
    return out


# The exact ordered step-line sequence, hand-derived from
# test-programs/gdscript/gf_control_flow.gd (see EXPECTED-GF2.md). Blocks are
# grouped by the construct that produced them; execution order is call-site line
# first, then the callee's body.
EXPECTED_LINES = (
    # if/elif/else — all three arms
    [153] + [39, 40, 41, 46]              # classify(-5): NEG arm (line 41)
    + [154] + [39, 40, 42, 43, 46]        # classify(0):  ZERO arm (42 elif, 43)
    + [155] + [39, 40, 42, 45, 46]        # classify(7):  ELSE arm (45; no `else:` step)
    # for + continue: body 52  x4 (4 iters), 54 x3 (skip i==2), 53 x1 (continue)
    + [157] + [50, 51, 52, 54, 52, 54, 52, 53, 52, 54, 55]
    # while + break: body 61 x3 / 62 x3, 63 x1 (break), header 60 once
    + [158] + [59, 60, 61, 62, 61, 62, 61, 62, 63, 64]
    # pass no-op
    + [159] + [68, 69]
    # match literal arm (m_literal(2) -> `2:` at 77, body 78)
    + [161] + [73, 74, 75, 77, 78, 81]
    # match wildcard arm (m_literal(99) -> `_:` at 79, body 80)
    + [162] + [73, 74, 75, 77, 79, 80, 81]
    # match expression pattern (LIMIT at 87, body 88)
    + [163] + [85, 86, 87, 88, 91]
    # match comma/alternative (1,2,3 at 97, body 98)
    + [164] + [95, 96, 97, 98, 101]
    # match plain binding (var b at 107 binds 77, body 108)
    + [165] + [105, 106, 107, 108, 109]
    # match array binding ([1, var rest] at 115 binds 2, body 116)
    + [166] + [113, 114, 115, 116, 119]
    # match open-ended array ([var first, ..] at 125 binds 7, body 126)
    + [167] + [123, 124, 125, 126, 129]
    # match dictionary binding ({"key": var dv} at 135 binds 42, body 136)
    + [168] + [133, 134, 135, 136, 139]
    # match guard (var g when g > 5 at 145 binds 8, body 146)
    + [169] + [143, 144, 145, 146, 149]
    # checksum + print
    + [170, 171]
)

# Match binding values, each captured at its match PATTERN line.
# (line, varname, value-kind, value)
EXPECTED_BINDINGS = [
    (107, "b", "Int", 77),
    (115, "rest", "Int", 2),
    (125, "first", "Int", 7),
    (135, "dv", "Int", 42),
    (145, "g", "Int", 8),
]

# Legible sub-facts (subsumed by the full-sequence match, asserted separately so
# the evidence is explicit). line -> exact occurrence count in the step stream.
EXPECTED_COUNTS = {
    52: 4,   # for-body first stmt: 4 iterations (range(4))
    54: 3,   # add stmt: skipped once by `continue` (i == 2)
    53: 1,   # `continue` executed exactly once
    61: 3,   # while-body: 3 iterations before `break`
    63: 1,   # `break` executed exactly once
    60: 1,   # while HEADER emits its step once (not per iteration)
    51: 1,   # for HEADER emits its step once
}

# Lines whose presence proves a specific branch/arm was TAKEN.
TAKEN_BODY_LINES = [41, 43, 45,           # if / elif / else bodies
                    78, 80, 88, 98, 108,  # match: literal, wildcard, expr, comma, bind
                    116, 126, 136, 146]   # match: array, open-array, dict, guard
# Lines that must be ABSENT — untaken arm bodies for the inputs we drove.
ABSENT_BODY_LINES = [76,   # m_literal `1:` body (never selected: inputs 2 and 99)
                     90,   # m_expression `_:` body (LIMIT matched)
                     100,  # m_comma `_:` body (1,2,3 matched)
                     118,  # m_array `_:` body ([1,2] matched)
                     128,  # m_array_open `_:` body (matched)
                     138,  # m_dict `_:` body (matched)
                     148]  # m_guard `_:` body (guard matched)


def value_scalar(val):
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


def step_at_unique_line(sts, line):
    matches = [s for s in sts if s.get("line") == line]
    if not matches:
        raise VerifyError("no step at line %d" % line)
    if len(matches) != 1:
        raise VerifyError("expected exactly 1 step at line %d, got %d" % (line, len(matches)))
    return matches[0]


def assert_facts(doc):
    sts = control_flow_steps(doc)
    got_lines = [s.get("line") for s in sts]

    # --- 1. THE load-bearing assertion: exact ordered step-line sequence -----
    if got_lines != EXPECTED_LINES:
        # Produce a focused diff of the first divergence.
        n = min(len(got_lines), len(EXPECTED_LINES))
        idx = next((i for i in range(n) if got_lines[i] != EXPECTED_LINES[i]), n)
        ctx_lo = max(0, idx - 3)
        raise VerifyError(
            "step-line sequence mismatch at index %d\n  expected[%d:]=%r\n  got     [%d:]=%r\n"
            "  (len expected=%d got=%d)"
            % (idx, ctx_lo, EXPECTED_LINES[ctx_lo:idx + 6], ctx_lo,
               got_lines[ctx_lo:idx + 6], len(EXPECTED_LINES), len(got_lines)))

    # --- 2. explicit occurrence counts (loops / break / continue / headers) --
    for line, want in EXPECTED_COUNTS.items():
        have = got_lines.count(line)
        if have != want:
            raise VerifyError(
                "line %d occurs %d times, expected %d (loop/break/continue evidence)"
                % (line, have, want))

    # --- 3. branch/arm selection: taken bodies present, untaken absent -------
    line_set = set(got_lines)
    for line in TAKEN_BODY_LINES:
        if line not in line_set:
            raise VerifyError("taken-branch body line %d is ABSENT (branch not recorded)" % line)
    for line in ABSENT_BODY_LINES:
        if line in line_set:
            raise VerifyError("untaken-arm body line %d is PRESENT (wrong arm recorded)" % line)

    # --- 4. match binding values captured at their pattern lines -------------
    observed = []
    for (line, varname, kind, expected_val) in EXPECTED_BINDINGS:
        st = step_at_unique_line(sts, line)
        hit = [v for v in st.get("vars", []) if v.get("varname") == varname]
        if not hit:
            raise VerifyError(
                "match pattern line %d carries no binding %r (vars=%r)"
                % (line, varname, [v.get("varname") for v in st.get("vars", [])]))
        if len(hit) != 1:
            raise VerifyError("line %d carries %d copies of binding %r" % (line, len(hit), varname))
        val = hit[0].get("value", {})
        got_kind = val.get("kind")
        if got_kind != kind:
            raise VerifyError("binding %s@%d kind %r != %r" % (varname, line, got_kind, kind))
        got_val = value_scalar(val)
        if got_val != expected_val:
            raise VerifyError(
                "binding %s@%d = %r (%s), expected %r" % (varname, line, got_val, got_kind, expected_val))
        observed.append("%s@%d=%r:%s" % (varname, line, expected_val, kind))

    return ("PASS GF2: %d control-flow step lines match exactly; "
            "if/elif/else branch selection proven; for-body x4 (continue skips 1), "
            "while-body x3 (break truncates); match dispatch for literal/wildcard/"
            "expression/comma/binding/array/open-array/dictionary/guard; "
            "bindings %s" % (len(EXPECTED_LINES), observed))


def tamper(doc, mode):
    """Corrupt the doc so assert_facts SHOULD fail, proving the check has teeth.

    - branch:  rewrite the taken elif-body step (line 43, classify(0)) to the
               if-body line 41 — simulates a DIFFERENT branch being taken. The
               ordered-sequence assertion must catch the stale expectation.
    - iter:    delete one occurrence of the for-body line 52 — simulates a wrong
               iteration count (3 instead of 4). Sequence + count must catch it.
    - binding: change the array binding `rest` from 2 to 999 at line 115.
    """
    sts = all_steps(doc)
    if mode == "branch":
        for s in sts:
            if s.get("line") == 43:   # unique: classify(0) elif body
                s["line"] = 41        # pretend the `if` arm ran instead
                break
    elif mode == "iter":
        # remove the first for-body step (line 52) => only 3 iterations recorded
        for i, s in enumerate(sts):
            if s.get("line") == 52:
                doc_events = doc["events"]
                doc_events.remove(s)
                break
    elif mode == "binding":
        for s in sts:
            if s.get("line") == 115:
                for v in s.get("vars", []):
                    if v.get("varname") == "rest":
                        v["value"]["i"] = 999
                break
    else:
        print("unknown tamper mode %r" % mode, file=sys.stderr)
        sys.exit(2)


def main():
    if len(sys.argv) < 3:
        print("usage: verify_gf2.py <verify|tamper> <full.json> [mode]", file=sys.stderr)
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
            print("usage: verify_gf2.py tamper <full.json> <branch|iter|binding>", file=sys.stderr)
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
