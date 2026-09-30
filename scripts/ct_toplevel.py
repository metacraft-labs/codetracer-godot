"""The `<toplevel>` root frame every recording opens, checked and set aside.

`trace_writer_start` opens a `<toplevel>` frame before any other call
(codetracer-trace-format-spec/trace-events.md, "Recorder Integration — Starting
a Recording" and "`<toplevel>` is the call tree's root and its id is fixed"):
function_id 0, call_key 0, depth 0, parent -1, entered at the entry step 0.
The engine's own frames therefore sit one level below it.

The corpus facts (scripts/EXPECTED-*.md) describe the ENGINE's frames, whose
nesting is what the GDScript program determines: `_init` is a top-level engine
frame, its callees are one below it, and so on. `reroot` asserts the root is
exactly what the spec requires and then removes it, so every verifier keeps
stating those facts in the program's own terms. A recording with no root, a
root that is not the first call, or a second root is refused here rather than
being reshaped into something that would pass.
"""

TOPLEVEL = "<toplevel>"


class RootError(Exception):
    pass


def reroot(doc):
    events = doc.get("events", [])
    entries = [e for e in events if e.get("kind") == "call_entry"]
    if not entries:
        raise RootError("no call_entry at all — the recording has no `<toplevel>` root")
    root = entries[0]
    expected = {
        "function": TOPLEVEL,
        "function_id": 0,
        "call_key": 0,
        "depth": 0,
        "parent_call_key": -1,
        "entry_step": 0,
    }
    for key, want in expected.items():
        if root.get(key) != want:
            raise RootError(
                "first call_entry %s=%r, the spec requires %r (root: %s)" % (key, root.get(key), want, root)
            )
    extra = [e for e in entries[1:] if e.get("function") == TOPLEVEL]
    if extra:
        raise RootError(
            "%d further `<toplevel>` frames after the root: %s" % (len(extra), [e.get("call_key") for e in extra])
        )

    out = []
    for e in events:
        kind = e.get("kind")
        if kind in ("call_entry", "call_exit") and e.get("call_key") == 0:
            continue  # the root itself
        if kind in ("call_entry", "call_exit"):
            e = dict(e)
            if isinstance(e.get("depth"), int):
                e["depth"] -= 1
            if e.get("parent_call_key") == 0:
                e["parent_call_key"] = -1
        elif kind == "step" and isinstance(e.get("depth"), int):
            e = dict(e)
            e["depth"] -= 1
        out.append(e)
    rerooted = dict(doc)
    rerooted["events"] = out
    return rerooted
