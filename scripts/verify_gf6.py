#!/usr/bin/env python3
"""Assert GF6 (lambdas & closures / local capture) facts against a real .ct
produced by the patched engine, decoded via `ct-print --full`.

This is a genuine test: it EXITS NONZERO on any mismatch. The expected facts are
hand-derived in scripts/EXPECTED-GF6.md (first-principles, before recording) and
duplicated here as literals so the assertion is not circular.

The load-bearing findings (see EXPECTED-GF6.md for the full derivation):

  - A lambda is its own GDScriptFunction, and a lambda `.call(...)` invocation
    goes through `GDScriptFunction::call` (GDScriptLambdaCallable::call ->
    function->call, gdscript_lambda_callable.cpp:92/120). So the existing G2
    step, G3 call/return, and GF5 return-value hooks all fire: a lambda records
    as a nested call/return FRAME named "<anonymous lambda>"
    (gdscript_compiler.cpp:2308) with its return value captured.
  - The lambda's PARAMS and its CAPTURED locals are BOTH named stack slots:
    captures are injected as LEADING PARAMETERS of the lambda function
    (gdscript_analyzer.cpp resolve_pending_lambda_bodies), so both go through
    add_parameter -> add_stack_identifier and are resolvable via
    debug_get_stack_member_state (the G4 slot->name table). BUT, being function
    arguments, they are placed by the call PROLOGUE, not via OPCODE_ASSIGN — so
    (exactly like GF5's supplied arguments) they are not re-captured on their own
    slot. Their values are observed at lambda execution by the body reading them
    into fresh named locals (`seen_x` <- param x; `seen_base` <- capture base).
  - CAPTURE-BY-VALUE: OPCODE_CREATE_LAMBDA snapshots captures by value
    (gdscript_vm.cpp:2744 `captures.write[i] = *arg`). So after the outer `base`
    is mutated 10 -> 999, the lambda STILL returns 15 and `seen_base` STILL reads
    10 on the second call, while the outer local `base` is captured as [10, 999].
  - A lambda stored in a var is value-captured as a Callable (GF4 shallow
    Struct "Callable" {method: "<anonymous lambda>"}).
  - A nested lambda (created inside a lambda) capturing from two scopes nests at
    depth 2 under the outer lambda.

Usage:
  verify_gf6.py verify <full.json>          # assert all GF6 facts (exit 0 = pass)
  verify_gf6.py tamper <full.json> <mode>   # corrupt the doc, expect the same
                                            # assertions to FAIL. mode is one of
                                            # param|captured|capturebyvalue|
                                            # return|nesting. exit 0 iff caught.
"""
import json
import sys

LAMBDA = "<anonymous lambda>"


class VerifyError(Exception):
    pass


# --- expected captured SCALAR named-local values, in step order ------------
# varname -> [(kind, expected_value), ...]. Hand-derived from
# test-programs/gdscript/gf_lambdas.gd (see EXPECTED-GF6.md).
EXPECTED_CAPTURES = {
    # _init locals.
    "base": [("Int", 10), ("Int", 999)],  # created 10, then mutated to 999
    "r1": [("Int", 15)],
    "r2": [("Int", 15)],  # capture-by-value: still 15 after base -> 999
    "r3": [("Int", 42)],
    "a": [("Int", 100)],
    "rn": [("Int", 307)],
    "total": [("Int", 379)],
    # add lambda body locals (called TWICE): param + captured outer local.
    "seen_x": [("Int", 5), ("Int", 5)],       # the PARAM x, read at execution
    "seen_base": [("Int", 10), ("Int", 10)],  # the CAPTURE base — 10 BOTH times
    # outer lambda body locals.
    "b": [("Int", 200)],
    # inner (nested) lambda body locals: captures from two scopes.
    "seen_a": [("Int", 100)],  # a, captured transitively from _init
    "seen_b": [("Int", 200)],  # b, captured from the outer lambda
}

# Lambdas stored in vars: value-captured as a Callable struct. varname -> count.
EXPECTED_CALLABLES = {"add": 1, "doubler": 1, "outer": 1, "inner": 1}

# expected return values per function, in call_key order.
#   <anonymous lambda> exits in call_key (entry) order:
#     add#1=15, add#2=15, doubler=42, outer=307, inner=307
EXPECTED_RETURNS = {
    LAMBDA: [("Int", 15), ("Int", 15), ("Int", 42), ("Int", 307), ("Int", 307)],
    "_init": [("None", None)],
    "_process": [("Bool", True)],
    "@implicit_new": [("None", None)],
}


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
    if kind == "Struct":
        return "Struct"
    return "%s?" % kind


def check_scalar(val, want_kind, want_value, path):
    got = val.get("kind")
    if got != want_kind:
        raise VerifyError("%s: kind %r != %r (%s)" % (path, got, want_kind, scalar_str(got, val)))
    if want_kind == "Int":
        if val.get("i") != want_value:
            raise VerifyError("%s: Int %r != %r" % (path, val.get("i"), want_value))
    elif want_kind == "Float":
        if abs(float(val.get("f")) - want_value) > 1e-6:
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


def type_name_of(doc, node, path):
    tid = node.get("type_id")
    types = doc.get("types", [])
    if not isinstance(tid, int) or tid < 0 or tid >= len(types):
        raise VerifyError("%s: type_id %r out of range (types len %d)" % (path, tid, len(types)))
    return types[tid]


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
    caps = captures_by_name(doc)

    # --- 1. captured scalar named locals: params, captures, capture-by-value --
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

    # capture-by-value teeth (explicit): the outer local `base` mutates 10->999,
    # but the lambda's captured `seen_base` stays 10 on BOTH invocations.
    if [v.get("i") for v in caps.get("base", [])] != [10, 999]:
        raise VerifyError("outer local base not [10, 999]: %r" % [v.get("i") for v in caps.get("base", [])])
    if [v.get("i") for v in caps.get("seen_base", [])] != [10, 10]:
        raise VerifyError("capture-by-value broken: seen_base %r != [10, 10]"
                          % [v.get("i") for v in caps.get("seen_base", [])])

    # --- 2. lambdas stored in vars are value-captured as Callable structs -----
    for name, want_count in EXPECTED_CALLABLES.items():
        got = caps.get(name, [])
        if len(got) != want_count:
            raise VerifyError("Callable var %r: got %d occurrences, expected %d"
                              % (name, len(got), want_count))
        node = got[0]
        if node.get("kind") != "Struct":
            raise VerifyError("Callable var %r: kind %r != Struct" % (name, node.get("kind")))
        tn = type_name_of(doc, node, "callable %s" % name)
        if tn != "Callable":
            raise VerifyError("Callable var %r: type_name %r != 'Callable'" % (name, tn))
        fields = node.get("field_values")
        if not isinstance(fields, list) or len(fields) != 1:
            raise VerifyError("Callable var %r: expected 1 field (method), got %r" % (name, fields))
        check_scalar(fields[0], "String", LAMBDA, "callable %s.method" % name)

    # --- 3. return values on call_exit events (incl. every lambda return) -----
    rets = returns_by_function(doc)
    for fn, want_seq in EXPECTED_RETURNS.items():
        got = rets.get(fn, [])
        if len(got) != len(want_seq):
            raise VerifyError(
                "returns for %r: got %d %s, expected %d"
                % (fn, len(got), [scalar_str(v.get("kind"), v) for v in got], len(want_seq)))
        for i, (want_kind, want_value) in enumerate(want_seq):
            check_scalar(got[i], want_kind, want_value, "return %s[%d]" % (fn, i))

    # --- 4. lambda call/return FRAMES + nesting -------------------------------
    ces = call_entries(doc)
    inits = [c for c in ces if c.get("function") == "_init"]
    if len(inits) != 1:
        raise VerifyError("expected exactly 1 _init call_entry, got %d" % len(inits))
    init = inits[0]
    init_key = init["call_key"]
    if init.get("depth") != 0 or init.get("parent_call_key") != -1:
        raise VerifyError("_init not top-level: depth=%s parent=%s"
                          % (init.get("depth"), init.get("parent_call_key")))

    lambdas = [c for c in ces if c.get("function") == LAMBDA]
    if len(lambdas) != 5:
        raise VerifyError("expected 5 <anonymous lambda> frames, got %d" % len(lambdas))
    for c in lambdas:
        if c.get("entry_step") > c.get("exit_step"):
            raise VerifyError("lambda frame malformed: entry_step %s > exit_step %s"
                              % (c.get("entry_step"), c.get("exit_step")))

    # 4 lambdas nest directly under _init (depth 1): add(x2), doubler, outer.
    depth1 = [c for c in lambdas if c.get("parent_call_key") == init_key]
    if len(depth1) != 4:
        raise VerifyError("expected 4 lambda frames directly under _init, got %d" % len(depth1))
    for c in depth1:
        if c.get("depth") != 1:
            raise VerifyError("lambda under _init has depth %s (expected 1)" % c.get("depth"))

    # 1 lambda nests at depth 2 (inner), parented to a depth-1 lambda (outer).
    depth2 = [c for c in lambdas if c.get("depth") == 2]
    if len(depth2) != 1:
        raise VerifyError("expected exactly 1 nested (depth-2) lambda, got %d" % len(depth2))
    inner = depth2[0]
    depth1_keys = {c["call_key"] for c in depth1}
    if inner.get("parent_call_key") not in depth1_keys:
        raise VerifyError("nested lambda's parent %s is not a depth-1 lambda %s"
                          % (inner.get("parent_call_key"), sorted(depth1_keys)))

    # the nested (inner) frame returns 307 and its parent (outer) returns 307.
    exit_by_key = {e.get("call_key"): e for e in call_exits(doc)}
    inner_exit = exit_by_key.get(inner["call_key"])
    if inner_exit is None:
        raise VerifyError("nested lambda has no matching call_exit")
    check_scalar(inner_exit.get("return_value", {}), "Int", 307, "inner lambda return")
    outer_exit = exit_by_key.get(inner["parent_call_key"])
    if outer_exit is None:
        raise VerifyError("outer lambda has no matching call_exit")
    check_scalar(outer_exit.get("return_value", {}), "Int", 307, "outer lambda return")

    # engine top-level frames.
    for fn in ("@implicit_new", "_process"):
        matches = [c for c in ces if c.get("function") == fn]
        if len(matches) != 1:
            raise VerifyError("expected exactly 1 %r frame, got %d" % (fn, len(matches)))
        c = matches[0]
        if c.get("depth") != 0 or c.get("parent_call_key") != -1:
            raise VerifyError("%s not top-level: depth=%s parent=%s"
                              % (fn, c.get("depth"), c.get("parent_call_key")))

    # balance: every call_entry has a matching call_exit.
    if len(call_entries(doc)) != len(call_exits(doc)):
        raise VerifyError("unbalanced call/return: %d call_entry vs %d call_exit"
                          % (len(call_entries(doc)), len(call_exits(doc))))

    # --- 5. types table: scalars + the lazily-interned Callable struct --------
    types = doc.get("types", [])
    want_types = ["None", "Int", "Float", "Bool", "String", "Variant", "Callable"]
    if types != want_types:
        raise VerifyError("types table %r != %r" % (types, want_types))

    return ("PASS GF6: lambda frames (5 <anonymous lambda>: 4 under _init depth1, "
            "1 nested depth2 under the outer lambda) with returns [15,15,42,307,307]; "
            "param x readable at execution (seen_x=5x2), captured base readable "
            "(seen_base=10x2); CAPTURE-BY-VALUE proven (outer base [10,999] but "
            "seen_base [10,10]); lambdas value-captured as Callable{method="
            "'<anonymous lambda>'} (add/doubler/outer/inner); nested lambda captures "
            "two scopes (seen_a=100, seen_b=200 -> 307); types [None,Int,Float,Bool,"
            "String,Variant,Callable].")


def _nth_var(doc, varname, n, pred=None):
    """Return the n-th (0-based) value-node for `varname` across steps, or None."""
    seen = 0
    for s in steps(doc):
        for v in s.get("vars", []):
            if v.get("varname") == varname:
                val = v.get("value", {})
                if pred is None or pred(val):
                    if seen == n:
                        return val
                    seen += 1
    return None


def tamper(doc, mode):
    """Corrupt the decoded doc so assert_facts SHOULD fail, proving teeth.

    - param:          wrong param captured value (seen_x 5 -> 99).
    - captured:       wrong captured outer value (first seen_base 10 -> 99).
    - capturebyvalue: simulate capture-by-REFERENCE (2nd seen_base 10 -> 999);
                      expected [10,10] so must be rejected.
    - return:         wrong lambda return (first <anonymous lambda> 15 -> 99).
    - nesting:        reparent the nested (depth-2) lambda up to _init (depth 1),
                      breaking the depth-2 nesting assertion.
    """
    if mode == "param":
        val = _nth_var(doc, "seen_x", 0)
        if val is None:
            raise VerifyError("tamper(param): no seen_x")
        val["i"] = 99
        return
    if mode == "captured":
        val = _nth_var(doc, "seen_base", 0)
        if val is None:
            raise VerifyError("tamper(captured): no seen_base")
        val["i"] = 99
        return
    if mode == "capturebyvalue":
        val = _nth_var(doc, "seen_base", 1)
        if val is None:
            raise VerifyError("tamper(capturebyvalue): no 2nd seen_base")
        val["i"] = 999  # as if the capture tracked the outer mutation
        return
    if mode == "return":
        for e in sorted(call_exits(doc), key=lambda e: e.get("call_key", 0)):
            if e.get("function") == LAMBDA:
                e["return_value"]["i"] = 99
                return
        raise VerifyError("tamper(return): no lambda call_exit")
    if mode == "nesting":
        ces = call_entries(doc)
        init_key = next(c["call_key"] for c in ces if c.get("function") == "_init")
        for c in ces:
            if c.get("function") == LAMBDA and c.get("depth") == 2:
                c["depth"] = 1
                c["parent_call_key"] = init_key
                return
        raise VerifyError("tamper(nesting): no depth-2 lambda")
    print("unknown tamper mode %r" % mode, file=sys.stderr)
    sys.exit(2)


def main():
    if len(sys.argv) < 3:
        print("usage: verify_gf6.py <verify|tamper> <full.json> [mode]", file=sys.stderr)
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
            print("usage: verify_gf6.py tamper <full.json> <param|captured|capturebyvalue|return|nesting>", file=sys.stderr)
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
