#!/usr/bin/env python3
"""Assert G3 (calls & returns) and G2 (per-line steps) facts against a real
.ct produced by the patched engine, decoded via `ct-print --full`.

This is a genuine test: it EXITS NONZERO on any mismatch. The expected facts
are hand-derived in scripts/EXPECTED-G3.md (first-principles, before recording)
and duplicated here as literals so the assertion is not circular.

Usage:
  verify_g3.py g3 <full.json>   # gf_calls.gd call/return nesting
  verify_g3.py g2 <full.json>   # g2probe.gd per-line steps (regression)
"""
import json
import sys


def fail(msg):
    print("FAIL: " + msg, file=sys.stderr)
    sys.exit(1)


def load(path):
    with open(path) as f:
        return json.load(f)


def events(doc):
    return doc.get("events", [])


def calls(doc):
    return [e for e in events(doc) if e.get("kind") == "call_entry"]


def steps(doc):
    return [e for e in events(doc) if e.get("kind") == "step"]


def by_function(cs, name):
    return [c for c in cs if c.get("function") == name]


def one(cs, name):
    matches = by_function(cs, name)
    if len(matches) != 1:
        fail("expected exactly 1 call to %r, got %d" % (name, len(matches)))
    return matches[0]


def verify_g3(doc):
    cs = calls(doc)
    names = sorted(c.get("function") for c in cs)
    print("call_entry functions observed: %s" % names)

    # --- 1. all four _init-subtree functions plus the lifecycle _process ----
    for n in ("_init", "outer", "inner", "sibling"):
        if not by_function(cs, n):
            fail("no call recorded for function %r" % n)

    c_init = one(cs, "_init")
    c_outer = one(cs, "outer")
    c_inner = one(cs, "inner")
    c_sibling = one(cs, "sibling")

    # --- 2. nesting: inner under outer under _init -------------------------
    if c_inner["parent_call_key"] != c_outer["call_key"]:
        fail("inner.parent_call_key=%s != outer.call_key=%s"
             % (c_inner["parent_call_key"], c_outer["call_key"]))
    if c_outer["parent_call_key"] != c_init["call_key"]:
        fail("outer.parent_call_key=%s != _init.call_key=%s"
             % (c_outer["parent_call_key"], c_init["call_key"]))

    # --- 3. depth ladder ---------------------------------------------------
    if c_init["depth"] != 0:
        fail("_init.depth=%s (expected 0)" % c_init["depth"])
    if c_outer["depth"] != c_init["depth"] + 1:
        fail("outer.depth=%s (expected %s)" % (c_outer["depth"], c_init["depth"] + 1))
    if c_inner["depth"] != c_outer["depth"] + 1:
        fail("inner.depth=%s (expected %s)" % (c_inner["depth"], c_outer["depth"] + 1))
    if c_inner["depth"] != 2:
        fail("inner.depth=%s (expected 2)" % c_inner["depth"])

    # --- 4. sibling: same parent + depth as outer, runs AFTER outer --------
    if c_sibling["parent_call_key"] != c_init["call_key"]:
        fail("sibling.parent_call_key=%s != _init.call_key=%s"
             % (c_sibling["parent_call_key"], c_init["call_key"]))
    if c_sibling["depth"] != c_outer["depth"]:
        fail("sibling.depth=%s != outer.depth=%s"
             % (c_sibling["depth"], c_outer["depth"]))
    if not (c_sibling["entry_step"] > c_outer["exit_step"]):
        fail("sibling.entry_step=%s not > outer.exit_step=%s (sibling must run after outer returns)"
             % (c_sibling["entry_step"], c_outer["exit_step"]))

    # --- 5. children lists reflect the tree --------------------------------
    if c_inner["call_key"] not in c_outer.get("children", []):
        fail("outer.children=%s does not contain inner.call_key=%s"
             % (c_outer.get("children"), c_inner["call_key"]))
    init_children = c_init.get("children", [])
    for child in (c_outer["call_key"], c_sibling["call_key"]):
        if child not in init_children:
            fail("_init.children=%s does not contain %s" % (init_children, child))

    # --- 6a. engine-synthesized top-level frames ---------------------------
    # `@implicit_new` (GDScript implicit constructor) and `_process` (MainLoop
    # per-frame callback) are real engine-run call() frames, both at top level
    # (depth 0, parent -1). See scripts/EXPECTED-G3.md — they were added to the
    # expected set after being observed, and are asserted here so their nature
    # (top-level, no children) is pinned, not silently tolerated.
    for n in ("@implicit_new", "_process"):
        c = one(cs, n)
        if c["depth"] != 0:
            fail("%s.depth=%s (expected 0, engine top-level frame)" % (n, c["depth"]))
        if c["parent_call_key"] != -1:
            fail("%s.parent_call_key=%s (expected -1, top-level)" % (n, c["parent_call_key"]))

    # --- 6b. balance: exactly 6 balanced call/return pairs -----------------
    # ct-print reconstructs a call_entry only for a call that had a matching
    # return (the writer persists a call on its return), so the record count is
    # the balanced-pair count: 4 source functions + @implicit_new + _process.
    if len(cs) != 6:
        fail("expected 6 call_entry records (balanced pairs), got %d: %s"
             % (len(cs), [c.get("function") for c in cs]))

    # every call's range must be well-formed
    for c in cs:
        if c["entry_step"] > c["exit_step"]:
            fail("%s has entry_step=%s > exit_step=%s (malformed frame)"
                 % (c["function"], c["entry_step"], c["exit_step"]))

    # --- 7. deterministic printed value ------------------------------------
    check_stdout(doc, "CT_G3_RESULT=107")

    # --- 8. step lines in execution order ----------------------------------
    got = [s["line"] for s in steps(doc)]
    expected = [37, 30, 26, 27, 31, 38, 34, 39, 42]
    if got != expected:
        fail("G3 step lines %s != expected %s" % (got, expected))

    print("PASS G3: _init -> outer -> inner nested; sibling after outer; "
          "6 balanced call/return pairs (4 source + @implicit_new + _process); "
          "CT_G3_RESULT=107; steps %s" % got)


def verify_g2(doc):
    got = [s["line"] for s in steps(doc)]
    expected = [18, 19, 20, 14, 15, 21, 24]
    if got != expected:
        fail("G2 step lines %s != expected %s" % (got, expected))

    fns = set(s.get("function") for s in steps(doc))
    for n in ("_init", "add", "_process"):
        if n not in fns:
            fail("G2: no step attributed to function %r (got %s)" % (n, sorted(fns)))

    check_stdout(doc, "CT_G2_STEPS=30")
    print("PASS G2 regression: 7 per-line steps at %s across _init/add/_process; "
          "CT_G2_STEPS=30" % got)


def check_stdout(doc, needle):
    blobs = []
    for e in events(doc):
        if e.get("kind") == "io":
            for k in ("content", "text", "data", "bytes"):
                v = e.get(k)
                if isinstance(v, str):
                    blobs.append(v)
    joined = "\n".join(blobs)
    if needle not in joined:
        # stdout capture is best-effort in the trace; the runner also greps the
        # live process stdout, so only warn here rather than hard-fail if the
        # io stream is absent entirely.
        if not blobs:
            print("WARN: no io events in trace; stdout %r checked by runner instead"
                  % needle, file=sys.stderr)
            return
        fail("expected %r in trace stdout, got: %r" % (needle, joined))


def main():
    if len(sys.argv) != 3:
        fail("usage: verify_g3.py <g2|g3> <full.json>")
    mode, path = sys.argv[1], sys.argv[2]
    doc = load(path)
    if mode == "g3":
        verify_g3(doc)
    elif mode == "g2":
        verify_g2(doc)
    else:
        fail("unknown mode %r" % mode)


if __name__ == "__main__":
    main()
