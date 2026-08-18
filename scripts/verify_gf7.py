#!/usr/bin/env python3
"""Assert GF7 (classes / inheritance / super / inner classes / preload/load)
facts against a real .ct produced by the patched engine, decoded via
`ct-print --full`.

This is a genuine test: it EXITS NONZERO on any mismatch. The expected facts are
hand-derived in scripts/EXPECTED-GF7.md (first-principles, before recording) and
duplicated here as literals so the assertion is not circular.

The load-bearing findings (see EXPECTED-GF7.md for the full derivation):

  - GF7 adds NO recorder code: a class method, `super.method()`, `_init`,
    `_static_init`, and an inner-class method are all GDScriptFunctions, so the
    existing G2/G3/G4/GF5 hooks fire. The binary is byte-identical to GF6.
  - `ct-print --full` puts NO path on `call_entry`; a frame's SOURCE FILE is the
    `path` of the step at its `entry_step` (the frame's first step). That is how
    cross-FILE facts are asserted: a derived frame resolves to res://gf_dog.gd
    and the base frame it reaches via `super` resolves to res://gf_animal.gd.
  - A method frame's `function` is the bare method name (`speak`, `_init`,
    `label`, `size`); an override and the base it reaches via `super` share the
    label and are distinguished by source path + depth/parent nesting.
  - `call_entry.args` is EMPTY for GDScript; a ctor arg is observed by the body
    copying it into a named local (Animal._init: n->got_name; Dog._init: n->pup).

Usage:
  verify_gf7.py verify <full.json>          # assert all GF7 facts (exit 0 = pass)
  verify_gf7.py tamper <full.json> <mode>   # corrupt the doc, expect the same
                                            # assertions to FAIL. mode is one of
                                            # srcpath|missingsuper|ctorarg|
                                            # nesting. exit 0 iff caught.
"""
import json
import sys

ANIMAL = "res://gf_animal.gd"
DOG = "res://gf_dog.gd"
ZOO = "res://gf_zoo.gd"


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


def step_paths(doc):
    """step_index -> path."""
    m = {}
    for s in steps(doc):
        m[s.get("step_index")] = s.get("path")
    return m


def frame_source(doc_paths, entry):
    """A frame's source FILE = the path of the step at its entry_step."""
    return doc_paths.get(entry.get("entry_step"))


def captures_by_name(doc):
    """varname -> [value-node, ...] in step order (across all steps)."""
    m = {}
    for s in steps(doc):
        for v in s.get("vars", []):
            m.setdefault(v.get("varname"), []).append(v.get("value", {}))
    return m


def scalar_list(nodes, kind, field):
    out = []
    for n in nodes:
        if n.get("kind") != kind:
            out.append(("?" + str(n.get("kind"))))
        else:
            out.append(n.get(field))
    return out


def check_scalar(val, want_kind, want_value, path):
    got = val.get("kind")
    if got != want_kind:
        raise VerifyError("%s: kind %r != %r" % (path, got, want_kind))
    if want_kind == "Int":
        if val.get("i") != want_value:
            raise VerifyError("%s: Int %r != %r" % (path, val.get("i"), want_value))
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


def exit_by_key(doc):
    return {e.get("call_key"): e for e in call_exits(doc)}


def assert_facts(doc):
    paths = step_paths(doc)
    ces = call_entries(doc)
    exits = exit_by_key(doc)
    caps = captures_by_name(doc)

    def src(c):
        return frame_source(paths, c)

    # --- A. types table -------------------------------------------------------
    types = doc.get("types", [])
    want_types = ["None", "Int", "Float", "Bool", "String", "Variant", "Object"]
    if types != want_types:
        raise VerifyError("types table %r != %r" % (types, want_types))

    # --- B. all three files present as frame sources (cross-FILE trace) --------
    frame_srcs = {src(c) for c in ces}
    for f in (ANIMAL, DOG, ZOO):
        if f not in frame_srcs:
            raise VerifyError("file %r absent from frame sources %r" % (f, sorted(frame_srcs)))

    # --- C. the MAIN driver _init (gf_zoo) ------------------------------------
    main_inits = [c for c in ces
                  if c.get("function") == "_init" and c.get("depth") == 0
                  and c.get("parent_call_key") == -1 and src(c) == ZOO]
    if len(main_inits) != 1:
        raise VerifyError("expected exactly 1 top-level gf_zoo _init, got %d" % len(main_inits))
    main_key = main_inits[0]["call_key"]

    # --- D. super / inheritance across files: speak ---------------------------
    speaks = [c for c in ces if c.get("function") == "speak"]
    if len(speaks) != 4:
        raise VerifyError("expected 4 speak frames, got %d" % len(speaks))
    derived_speaks = [c for c in speaks if src(c) == DOG]
    base_speaks = [c for c in speaks if src(c) == ANIMAL]
    if len(derived_speaks) != 2:
        raise VerifyError("expected 2 DERIVED speak (gf_dog.gd), got %d" % len(derived_speaks))
    if len(base_speaks) != 2:
        raise VerifyError("expected 2 BASE speak (gf_animal.gd), got %d" % len(base_speaks))
    derived_speak_keys = set()
    for c in derived_speaks:
        if c.get("depth") != 1 or c.get("parent_call_key") != main_key:
            raise VerifyError("derived speak not depth1/parent=MAIN: depth=%s parent=%s"
                              % (c.get("depth"), c.get("parent_call_key")))
        derived_speak_keys.add(c["call_key"])
        check_scalar(exits[c["call_key"]].get("return_value", {}), "String", "...woof",
                     "derived speak return")
    used = set()
    for c in base_speaks:
        if c.get("depth") != 2:
            raise VerifyError("base speak not depth2: depth=%s" % c.get("depth"))
        p = c.get("parent_call_key")
        if p not in derived_speak_keys:
            raise VerifyError("base speak parent %s is not a derived speak %s"
                              % (p, sorted(derived_speak_keys)))
        if p in used:
            raise VerifyError("two base speaks share the same derived parent %s" % p)
        used.add(p)
        check_scalar(exits[c["call_key"]].get("return_value", {}), "String", "...",
                     "base speak return")

    # --- E. _init ctor chain across files -------------------------------------
    inits = [c for c in ces if c.get("function") == "_init"]
    derived_inits = [c for c in inits if src(c) == DOG]
    base_inits = [c for c in inits if src(c) == ANIMAL]
    if len(derived_inits) != 2:
        raise VerifyError("expected 2 DERIVED _init (gf_dog.gd), got %d" % len(derived_inits))
    if len(base_inits) != 2:
        raise VerifyError("expected 2 BASE _init (gf_animal.gd), got %d" % len(base_inits))
    derived_init_keys = set()
    for c in derived_inits:
        if c.get("depth") != 1 or c.get("parent_call_key") != main_key:
            raise VerifyError("derived _init not depth1/parent=MAIN: depth=%s parent=%s"
                              % (c.get("depth"), c.get("parent_call_key")))
        derived_init_keys.add(c["call_key"])
    used = set()
    for c in base_inits:
        if c.get("depth") != 2:
            raise VerifyError("base _init not depth2: depth=%s" % c.get("depth"))
        p = c.get("parent_call_key")
        if p not in derived_init_keys:
            raise VerifyError("base _init parent %s is not a derived _init %s"
                              % (p, sorted(derived_init_keys)))
        if p in used:
            raise VerifyError("two base _init share the same derived parent %s" % p)
        used.add(p)

    # --- F. ctor arg captured (cross-file propagation through super._init) -----
    got_name = scalar_list(caps.get("got_name", []), "String", "text")
    if got_name != ["rex", "spot"]:
        raise VerifyError("got_name (base ctor arg) %r != ['rex','spot']" % got_name)
    pup = scalar_list(caps.get("pup", []), "String", "text")
    if pup != ["rex", "spot"]:
        raise VerifyError("pup (derived ctor arg) %r != ['rex','spot']" % pup)
    # super return value flows back derived<-base.
    if scalar_list(caps.get("sound", []), "String", "text") != ["...", "..."]:
        raise VerifyError("sound (base body) %r != ['...','...']"
                          % scalar_list(caps.get("sound", []), "String", "text"))
    if scalar_list(caps.get("base_sound", []), "String", "text") != ["...", "..."]:
        raise VerifyError("base_sound (derived reads super) %r != ['...','...']"
                          % scalar_list(caps.get("base_sound", []), "String", "text"))
    if scalar_list(caps.get("out", []), "String", "text") != ["...woof", "...woof"]:
        raise VerifyError("out %r != ['...woof','...woof']"
                          % scalar_list(caps.get("out", []), "String", "text"))

    # --- G. inner-class method frames -----------------------------------------
    labels = [c for c in ces if c.get("function") == "label"]
    if len(labels) != 1:
        raise VerifyError("expected 1 inner-class label frame, got %d" % len(labels))
    lc = labels[0]
    if lc.get("depth") != 1 or src(lc) != ANIMAL:
        raise VerifyError("label frame not depth1/gf_animal.gd: depth=%s src=%s"
                          % (lc.get("depth"), src(lc)))
    check_scalar(exits[lc["call_key"]].get("return_value", {}), "String", "tag", "label return")
    if scalar_list(caps.get("made", []), "String", "text") != ["tag"]:
        raise VerifyError("inner Tag.label local made %r != ['tag']"
                          % scalar_list(caps.get("made", []), "String", "text"))

    sizes = [c for c in ces if c.get("function") == "size"]
    if len(sizes) != 1:
        raise VerifyError("expected 1 inner-class size frame, got %d" % len(sizes))
    sc = sizes[0]
    if sc.get("depth") != 1 or src(sc) != DOG:
        raise VerifyError("size frame not depth1/gf_dog.gd: depth=%s src=%s"
                          % (sc.get("depth"), src(sc)))
    check_scalar(exits[sc["call_key"]].get("return_value", {}), "Int", 3, "size return")
    if scalar_list(caps.get("s", []), "Int", "i") != [3]:
        raise VerifyError("inner Kennel.size local s %r != [3]"
                          % scalar_list(caps.get("s", []), "Int", "i"))

    # --- H. _static_init frame ------------------------------------------------
    statics = [c for c in ces if c.get("function") == "_static_init"]
    if len(statics) != 1:
        raise VerifyError("expected 1 _static_init frame, got %d" % len(statics))
    stc = statics[0]
    if src(stc) != DOG:
        raise VerifyError("_static_init source %r != gf_dog.gd" % src(stc))
    parent = next((c for c in ces if c.get("call_key") == stc.get("parent_call_key")), None)
    if parent is None or parent.get("function") != "@static_initializer":
        raise VerifyError("_static_init parent is not @static_initializer: %r"
                          % (parent.get("function") if parent else None))
    if scalar_list(caps.get("marker", []), "Int", "i") != [7]:
        raise VerifyError("_static_init local marker %r != [7]"
                          % scalar_list(caps.get("marker", []), "Int", "i"))

    # --- I. member/property values are GF8-deferred (absent as named locals) --
    for member in ("species", "v", "count"):
        if member in caps:
            raise VerifyError("member %r captured as a named local (should be GF8-deferred)"
                              % member)

    # --- J. balance -----------------------------------------------------------
    if len(call_entries(doc)) != len(call_exits(doc)):
        raise VerifyError("unbalanced call/return: %d entry vs %d exit"
                          % (len(call_entries(doc)), len(call_exits(doc))))

    return ("PASS GF7: cross-FILE call tree over {gf_animal.gd, gf_dog.gd, "
            "gf_zoo.gd}; super proven x2 (Dog.speak@gf_dog.gd depth1 -> "
            "Animal.speak@gf_animal.gd depth2, returns '...woof'->'...'); ctor "
            "chain x2 (Dog._init -> super Animal._init) with the arg propagated "
            "cross-file (got_name=pup=['rex','spot']); inner classes label@"
            "gf_animal.gd->'tag' and size@gf_dog.gd->3; _static_init@gf_dog.gd "
            "under @static_initializer (marker=7); members species/v/count "
            "GF8-deferred (absent); types [None,Int,Float,Bool,String,Variant,"
            "Object]; call/return balanced.")


# --- tamper -----------------------------------------------------------------
def _steps(doc):
    return steps(doc)


def _base_speak(doc):
    paths = step_paths(doc)
    for c in call_entries(doc):
        if c.get("function") == "speak" and paths.get(c.get("entry_step")) == ANIMAL:
            return c
    return None


def tamper(doc, mode):
    if mode == "srcpath":
        # Rewrite a base speak frame's source (its entry step's path) to gf_dog.gd,
        # simulating a mis-attributed cross-file frame.
        c = _base_speak(doc)
        if c is None:
            raise VerifyError("tamper(srcpath): no base speak")
        es = c.get("entry_step")
        for s in _steps(doc):
            if s.get("step_index") == es:
                s["path"] = DOG
                return
        raise VerifyError("tamper(srcpath): no step at entry_step %s" % es)
    if mode == "missingsuper":
        # Delete a depth-2 base speak call_entry (a missing super frame).
        c = _base_speak(doc)
        if c is None:
            raise VerifyError("tamper(missingsuper): no base speak")
        doc["events"] = [e for e in doc["events"] if e is not c]
        return
    if mode == "ctorarg":
        # Corrupt the first captured ctor arg (got_name "rex" -> "wrong").
        for s in _steps(doc):
            for v in s.get("vars", []):
                if v.get("varname") == "got_name":
                    v["value"]["text"] = "wrong"
                    return
        raise VerifyError("tamper(ctorarg): no got_name capture")
    if mode == "nesting":
        # Reparent a base speak (depth 2) up to the MAIN _init (breaks super nesting).
        main_key = next(c["call_key"] for c in call_entries(doc)
                        if c.get("function") == "_init" and c.get("depth") == 0)
        c = _base_speak(doc)
        if c is None:
            raise VerifyError("tamper(nesting): no base speak")
        c["depth"] = 1
        c["parent_call_key"] = main_key
        return
    print("unknown tamper mode %r" % mode, file=sys.stderr)
    sys.exit(2)


def main():
    if len(sys.argv) < 3:
        print("usage: verify_gf7.py <verify|tamper> <full.json> [mode]", file=sys.stderr)
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
            print("usage: verify_gf7.py tamper <full.json> <srcpath|missingsuper|ctorarg|nesting>",
                  file=sys.stderr)
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
