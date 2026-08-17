# G3 expected facts — hand-derived from source (first-principles, pre-recording)

These facts are derived by reading the reference programs, NOT by reading
recorder output. `scripts/verify_g3.py` asserts them against the real `.ct`
produced by the patched engine (decoded with `ct-print --full`). If the
recorder and these notes ever disagree, one of them is wrong — do not
regenerate this file from recorder output.

## `test-programs/gdscript/gf_calls.gd` — call/return nesting (G3)

Execution trace (entry order == call_key order; the multi-stream writer
allocates call_key monotonically at each `register_call`, and function_id is
interned by name in first-seen order).

Two frames are engine-synthesized, not written in the source, and were added
to this note AFTER observing them once — they are legitimate real GDScript
`GDScriptFunction::call()` invocations, not recorder artifacts:

- `@implicit_new` — GDScript's implicit constructor / member-initializer
  function. The engine runs it (as its own `call()` frame) when the script
  instance is created, before `_init`. It is a top-level (depth 0) leaf here.
- `_process` — the MainLoop per-frame callback; it returns `true` on the first
  frame to quit. Top-level (depth 0) leaf.

```
@implicit_new    call_key 0  fid 0  depth 0  parent -1   children []       (engine ctor)
_init            call_key 1  fid 1  depth 0  parent -1   children [2, 4]
  outer(2)       call_key 2  fid 2  depth 1  parent 1    children [3]
    inner(3)     call_key 3  fid 3  depth 2  parent 2    children []
  sibling()      call_key 4  fid 4  depth 1  parent 1    children []
_process(_delta) call_key 5  fid 5  depth 0  parent -1   children []       (engine frame)
```

The four SOURCE functions (`_init`, `outer`, `inner`, `sibling`) and their
nesting are what the milestone is about; the two engine frames sit at top
level and do not intrude on the `_init` subtree.

- `inner` is nested under `outer` (inner.parent_call_key == outer.call_key,
  inner.depth == outer.depth + 1).
- `outer` is nested under `_init` (outer.parent_call_key == _init.call_key,
  outer.depth == _init.depth + 1).
- `sibling` is a SIBLING of `outer`: same parent (`_init`), same depth (1),
  and it runs AFTER `outer` returns (sibling.entry_step > outer.exit_step).
- `inner.depth == 2 == _init.depth + 2`.
- `@implicit_new` and `_process` are top-level frames (depth 0, parent -1,
  no children); neither intrudes on the `_init` subtree.
- Exactly 6 `call_entry` records exist (4 source + `@implicit_new` +
  `_process`). Because the CTFS multi-stream writer persists a call only on its
  matching return, 6 call records == 6 balanced call/return pairs (no orphaned
  call, no unmatched return).

Deterministic value:

```
inner(3)  -> 3 + 1          = 4
outer(2)  -> inner(2+1)=4   -> 4 * 2 = 8   => x = 8
sibling() -> 99                            => y = 99
x + y = 107
```

so stdout contains `CT_G3_RESULT=107`.

Per-line step lines, in execution order (9 steps):

```
37, 30, 26, 27, 31, 38, 34, 39, 42
```

(37 = `x = outer(2)`; 30 = `a = inner(m+1)`; 26,27 = inner body; 31 = outer
`return a*2`; 38 = `y = sibling()`; 34 = sibling `return 99`; 39 = print;
42 = `_process` `return true`.)

## `test-programs/gdscript/g2probe.gd` — per-line steps regression (G2)

Step lines, in execution order (7 steps):

```
18, 19, 20, 14, 15, 21, 24
```

(18,19,20 = `_init` body up to the `add(x,y)` call site; 14,15 = `add` body;
21 = print; 24 = `_process` `return true`.) Functions seen on steps: `_init`,
`add`, `_process`. `z = 10 + 20 = 30`, so stdout contains `CT_G2_STEPS=30`.

The G2 regression check requires this exact ordered line sequence to still be
emitted after the G3 call/return hooks were added — proving the call hooks did
not perturb the step stream.
