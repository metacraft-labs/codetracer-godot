#!/usr/bin/env python3
"""Assert GF9 (signals: declare / emit / connect / disconnect) facts against a
real .ct produced by the patched engine, decoded via `ct-print --full`.

This is a genuine test: it EXITS NONZERO on any mismatch. The expected facts are
hand-derived in scripts/EXPECTED-GF9.md (first-principles, before recording) and
duplicated here as literals so the assertion is not circular.

The load-bearing findings (see EXPECTED-GF9.md for the full derivation):

  - GF9 needs NO engine change. A signal handler is an ordinary method; emitting
    a signal dispatches synchronously to each connected handler, each running
    through GDScriptFunction::call — so the existing G3 call/return, G2 step,
    G4/GF5 value, and GF8 member hooks fire per handler invocation.
  - The native emit/connect/disconnect calls have NO GDScript frame, so a handler
    frame's PARENT is the emitter frame (`_initialize`, depth 0) and its depth is
    1. There is no intervening "emit" frame; the emit call site is a step in the
    emitter, and the handler frame(s) appear right after it. Which emit triggered
    a handler is recovered as the emitter frame's last step line before the
    handler's call_entry — i.e. the emit line.

THE TEETH: handler frames must appear/disappear EXACTLY per connect/disconnect
state. The verifier buckets handler frames by emit line and asserts the exact
per-emit handler set (incl. the disconnected handler being ABSENT post-disconnect
and no handler frame when nothing is connected), with emitted args + returns
captured per invocation.

Usage:
  verify_gf9.py verify <full.json>          # assert all GF9 facts (exit 0 = pass)
  verify_gf9.py tamper <full.json> <mode>   # corrupt the doc, expect the same
                                            # assertions to FAIL. mode is one of
                                            # argvalue|disconnected|dropframe|
                                            # retvalue. exit 0 iff the tamper was
                                            # caught.
"""
import json
import sys


class VerifyError(Exception):
    pass


HANDLERS = {"_on_hit_a", "_on_hit_b"}

EXPECTED_TYPES = ["None", "Int", "Float", "Bool", "String", "Variant"]

# The five emits (source lines) and the EXACT handler set each must produce, in
# dispatch (connect) order. This is the connect/disconnect teeth.
EXPECTED_EMIT_BUCKETS = {
    66: [],                          # emit #1: nothing connected -> no handler
    71: ["_on_hit_a"],               # emit #2: one handler
    76: ["_on_hit_a", "_on_hit_b"],  # emit #3: both, in connect order
    82: ["_on_hit_b"],               # emit #4: _on_hit_a DISCONNECTED -> absent
    86: [],                          # emit #5: all disconnected -> no handler
}

# Per handler frame (entry order = call_key order): its triggering emit line, the
# emitted args captured by read-into-local, and the captured return value.
EXPECTED_FRAMES = [
    {"fn": "_on_hit_a", "emit": 71, "caps": {"a_dmg": ("Int", 7), "a_kind": ("Int", 2)}, "ret": ("Int", 9)},
    {"fn": "_on_hit_a", "emit": 76, "caps": {"a_dmg": ("Int", 3), "a_kind": ("Int", 4)}, "ret": ("Int", 7)},
    {"fn": "_on_hit_b", "emit": 76, "caps": {"b_val": ("Int", 34)}, "ret": ("Int", 34)},
    {"fn": "_on_hit_b", "emit": 82, "caps": {"b_val": ("Int", 56)}, "ret": ("Int", 56)},
]


def load(path):
    with open(path) as f:
        return json.load(f)


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


def build_frames(doc):
    """Walk events in order; reconstruct each frame invocation with its steps by
    maintaining an active-frame stack (steps carry no call_key, so each step is
    attributed to the top-of-stack frame). Returns (order, by_key) where each
    frame dict has: call_key, function, depth, parent, return_value, steps (list
    of {line, vars}), and emit_line (the parent frame's last step line before this
    frame's entry — for a handler that is the emit line)."""
    by_key = {}
    order = []
    stack = []
    events = doc.get("events", [])
    for e in events:
        k = e.get("kind")
        if k == "call_entry":
            key = e.get("call_key")
            parent = e.get("parent_call_key")
            emit_line = None
            pfr = by_key.get(parent)
            if pfr and pfr["steps"]:
                emit_line = pfr["steps"][-1]["line"]
            fr = {
                "call_key": key,
                "function": e.get("function"),
                "depth": e.get("depth"),
                "parent": parent,
                "return_value": None,
                "steps": [],
                "emit_line": emit_line,
            }
            by_key[key] = fr
            order.append(fr)
            stack.append(key)
        elif k == "call_exit":
            key = e.get("call_key")
            if key in by_key:
                by_key[key]["return_value"] = e.get("return_value", {})
            # pop robustly: the @implicit_new / _initialize top-levels can
            # interleave (implicit_new exits after _initialize's first step).
            if stack and stack[-1] == key:
                stack.pop()
            elif key in stack:
                stack.remove(key)
        elif k == "step":
            if stack:
                by_key[stack[-1]]["steps"].append(
                    {"line": e.get("line"), "vars": e.get("vars", [])})
    return order, by_key


def frame_capture(frame, varname):
    """Return the value node for `varname` captured in this frame invocation, or
    raise if it is not captured on exactly one step."""
    found = [v for s in frame["steps"] for v in s.get("vars", [])
             if v.get("varname") == varname]
    if not found:
        raise VerifyError(
            "frame %s (call_key=%s, emit=%s) does not capture %r (captured: %r)"
            % (frame["function"], frame["call_key"], frame["emit_line"], varname,
               [v.get("varname") for s in frame["steps"] for v in s.get("vars", [])]))
    if len(found) != 1:
        raise VerifyError(
            "frame %s (call_key=%s) captures %r %d times (expected 1)"
            % (frame["function"], frame["call_key"], varname, len(found)))
    return found[0]


def assert_facts(doc):
    order, by_key = build_frames(doc)

    # --- A. types table (scalar-only; no Object) ------------------------------
    types = doc.get("types")
    if types != EXPECTED_TYPES:
        raise VerifyError("types table %r != expected %r" % (types, EXPECTED_TYPES))

    # --- B. the single emitter frame `_initialize` ----------------------------
    inits = [f for f in order if f["function"] == "_initialize"]
    if len(inits) != 1:
        raise VerifyError("expected exactly 1 `_initialize` frame, got %d" % len(inits))
    init_key = inits[0]["call_key"]

    # --- C. handler frames + per-emit dispatch buckets (THE TEETH) ------------
    handler_frames = [f for f in order if f["function"] in HANDLERS]

    # every handler frame is depth 1, parented to the emitter (no emit frame).
    for f in handler_frames:
        if f["depth"] != 1:
            raise VerifyError(
                "handler %s (call_key=%s) depth=%s, expected 1 (child of emitter)"
                % (f["function"], f["call_key"], f["depth"]))
        if f["parent"] != init_key:
            raise VerifyError(
                "handler %s (call_key=%s) parent=%s, expected _initialize (%s)"
                % (f["function"], f["call_key"], f["parent"], init_key))

    # bucket handler frames by their triggering emit line.
    buckets = {}
    for f in handler_frames:
        buckets.setdefault(f["emit_line"], []).append(f["function"])

    # assert EXACTLY the expected per-emit handler set (order matters).
    for emit_line, expected_handlers in EXPECTED_EMIT_BUCKETS.items():
        got = buckets.get(emit_line, [])
        if got != expected_handlers:
            raise VerifyError(
                "emit@line %d dispatched %r, expected %r "
                "(connect/disconnect state not faithfully reflected)"
                % (emit_line, got, expected_handlers))

    # no handler frame may be bucketed to any line OTHER than the known emits.
    unexpected = set(buckets) - set(EXPECTED_EMIT_BUCKETS)
    if unexpected:
        raise VerifyError("handler frames dispatched from unexpected lines %r" % sorted(unexpected))

    # exact total + per-function counts.
    if len(handler_frames) != 4:
        raise VerifyError("expected 4 handler frames total, got %d" % len(handler_frames))
    n_a = sum(1 for f in handler_frames if f["function"] == "_on_hit_a")
    n_b = sum(1 for f in handler_frames if f["function"] == "_on_hit_b")
    if (n_a, n_b) != (2, 2):
        raise VerifyError("handler counts (_on_hit_a,_on_hit_b)=(%d,%d), expected (2,2)" % (n_a, n_b))

    # --- D. emitted args + return value captured, per frame invocation --------
    if len(handler_frames) != len(EXPECTED_FRAMES):
        raise VerifyError("handler-frame count %d != expected %d"
                          % (len(handler_frames), len(EXPECTED_FRAMES)))
    observed = []
    for frame, exp in zip(handler_frames, EXPECTED_FRAMES):
        if frame["function"] != exp["fn"]:
            raise VerifyError("frame order: got %s, expected %s" % (frame["function"], exp["fn"]))
        if frame["emit_line"] != exp["emit"]:
            raise VerifyError("%s frame emit_line=%s, expected %s"
                              % (frame["function"], frame["emit_line"], exp["emit"]))
        # emitted args (read into locals)
        for varname, (kind, val) in exp["caps"].items():
            v = frame_capture(frame, varname)
            gk = v.get("value", {}).get("kind")
            gv = value_scalar(v.get("value", {}))
            if gk != kind or gv != val:
                raise VerifyError(
                    "%s@emit%d capture %s=%r:%s, expected %r:%s"
                    % (frame["function"], frame["emit_line"], varname, gv, gk, val, kind))
        # captured return value (GF5)
        rv = frame["return_value"] or {}
        rk, rval = exp["ret"]
        if rv.get("kind") != rk or value_scalar(rv) != rval:
            raise VerifyError(
                "%s@emit%d return=%r:%s, expected %r:%s"
                % (frame["function"], frame["emit_line"], value_scalar(rv), rv.get("kind"), rval, rk))
        observed.append("%s@emit%d(%s)->%r"
                        % (frame["function"], frame["emit_line"],
                           ",".join("%s=%r" % (k, v[1]) for k, v in exp["caps"].items()),
                           exp["ret"][1]))

    # --- E. call/return balance ----------------------------------------------
    n_entry = sum(1 for e in doc.get("events", []) if e.get("kind") == "call_entry")
    n_exit = sum(1 for e in doc.get("events", []) if e.get("kind") == "call_exit")
    if n_entry != n_exit:
        raise VerifyError("unbalanced call/return: %d entry vs %d exit" % (n_entry, n_exit))

    return ("PASS GF9: per-emit handler dispatch reflects connect/disconnect state "
            "exactly — emit@66=[] (none connected), emit@71=[_on_hit_a], "
            "emit@76=[_on_hit_a,_on_hit_b] (both), emit@82=[_on_hit_b] "
            "(_on_hit_a ABSENT post-disconnect), emit@86=[] (all disconnected); "
            "4 handler frames, each depth-1 child of the emitter (_initialize) with "
            "no intervening emit frame; emitted args + returns captured per "
            "invocation [%s]; types=%s; call/return balanced."
            % ("; ".join(observed), types))


def tamper(doc, mode):
    """Corrupt the doc so assert_facts SHOULD fail, proving the check has teeth."""
    order, by_key = build_frames(doc)
    handler_frames = [f for f in order if f["function"] in HANDLERS]

    if mode == "argvalue":
        # corrupt an emitted-arg capture: _on_hit_a #1 (emit 71) a_dmg 7 -> 999.
        f0 = handler_frames[0]
        v = frame_capture(f0, "a_dmg")
        v["value"]["i"] = 999
    elif mode == "disconnected":
        # inject a SPURIOUS _on_hit_a frame into the post-disconnect emit #4
        # (line 82) — the disconnected handler must NOT be there.
        inits = [f for f in order if f["function"] == "_initialize"]
        init_key = inits[0]["call_key"]
        events = doc.get("events", [])
        idx = None
        for i, e in enumerate(events):
            if e.get("kind") == "step" and e.get("line") == 82:
                idx = i
                break
        if idx is None:
            raise VerifyError("tamper: could not find emit#4 step (line 82)")
        fake = [
            {"kind": "call_entry", "function": "_on_hit_a", "call_key": 99991,
             "depth": 1, "parent_call_key": init_key, "line": None},
            {"kind": "step", "function": "_on_hit_a", "line": 51,
             "vars": [{"varname": "a_dmg", "value": {"kind": "Int", "i": 5}}]},
            {"kind": "call_exit", "call_key": 99991, "return_value": {"kind": "Int", "i": 7}},
        ]
        events[idx + 1:idx + 1] = fake
    elif mode == "dropframe":
        # delete the first _on_hit_b frame (emit #3) — breaks the "both handlers
        # on a multi-connect" count/bucket.
        target = next(f for f in handler_frames if f["function"] == "_on_hit_b")
        key = target["call_key"]
        doc["events"] = [
            e for e in doc.get("events", [])
            if not (e.get("kind") == "call_entry" and e.get("call_key") == key)
            and not (e.get("kind") == "call_exit" and e.get("call_key") == key)
        ]
    elif mode == "retvalue":
        # corrupt a captured return value: _on_hit_b #1 (emit 76) ret 34 -> 999.
        target = next(f for f in handler_frames if f["function"] == "_on_hit_b")
        key = target["call_key"]
        for e in doc.get("events", []):
            if e.get("kind") == "call_exit" and e.get("call_key") == key:
                e["return_value"]["i"] = 999
                break
    else:
        print("unknown tamper mode %r" % mode, file=sys.stderr)
        sys.exit(2)


def main():
    if len(sys.argv) < 3:
        print("usage: verify_gf9.py <verify|tamper> <full.json> [mode]", file=sys.stderr)
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
            print("usage: verify_gf9.py tamper <full.json> "
                  "<argvalue|disconnected|dropframe|retvalue>", file=sys.stderr)
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
