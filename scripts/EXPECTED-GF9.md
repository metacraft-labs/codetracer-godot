# EXPECTED — GF9: Signals (declare / emit / connect / disconnect)

First-principles facts, hand-derived BEFORE running the recorder, that
`scripts/verify_gf9.py` asserts against the real `.ct` produced by the patched
engine over `test-programs/gdscript/gf_signals.gd`, decoded via `ct-print --full`.

## No engine change (the load-bearing finding — like GF1/GF2/GF6/GF7)

GF9 adds **no recorder code**. A GDScript signal handler is an ordinary method.
`emit`, `connect`, and `disconnect` are NATIVE calls (they have no GDScript frame
of their own). But emitting a signal dispatches SYNCHRONOUSLY to each connected
handler, and every handler runs through `GDScriptFunction::call` — so the
existing hooks fire per handler invocation:

- **G3 call/return**: each handler invocation is a balanced call FRAME.
- **G2 per-line steps**: the handler body's lines emit steps.
- **G4/GF5 value capture**: emitted args, read into locals, are captured by NAME;
  the handler's return value is captured on its return event (GF5).
- **GF8 member write**: the accumulator `total += ...` inside a handler is an
  `ADDR_TYPE_MEMBER` write captured by name (incidental; not the GF9 teeth).

The recorder binary is byte-identical to GF8 (the G2/G3/G4/GF1..GF8 streams
re-verify green). GF9 is a COVERAGE milestone.

## The emit boundary (how the recorder represents it — honest)

There is **no "emit" frame**. The native `Signal::emit` → `Object::emit_signalp`
→ `Callable::callp` → `GDScriptFunction::call` chain runs entirely inside the
native call initiated from the emitter's `hit.emit(...)` OPCODE. None of those
native links is a GDScript frame, so when the handler's `enter_function` hook
fires, the recorder's frame stack still has the EMITTER frame (`_initialize`,
depth 0) on top. Therefore:

- Each handler frame's **parent is the emitter frame** (`_initialize`), and its
  **depth is 1** (emitter depth 0 + 1).
- The emit call site is just a **step in the emitter** (`_initialize` at the
  `hit.emit(...)` line). The handler frame(s) appear immediately AFTER that step
  and BEFORE the emitter's next step.
- Which emit triggered a handler is therefore recoverable as: the last
  `_initialize` step line seen before the handler's `call_entry`. That is exactly
  the emit line. This is how the verifier buckets handlers per emit.

## Source line numbers (from gf_signals.gd)

| line | statement                               |
|------|-----------------------------------------|
| 44   | `signal hit(dmg, kind)`                 |
| 50   | `func _on_hit_a(dmg, kind):`            |
| 51   | `var a_dmg := dmg`                      |
| 52   | `var a_kind := kind`                    |
| 53   | `total += a_dmg`  (member write)        |
| 54   | `return a_dmg + a_kind`                 |
| 58   | `func _on_hit_b(dmg, kind):`            |
| 59   | `var b_val := dmg * 10 + kind`          |
| 60   | `total += b_val`  (member write)        |
| 61   | `return b_val`                          |
| 66   | `hit.emit(1, 1)`   emit #1 (0 handlers) |
| 70   | `hit.connect(_on_hit_a)`                |
| 71   | `hit.emit(7, 2)`   emit #2             |
| 75   | `hit.connect(_on_hit_b)`                |
| 76   | `hit.emit(3, 4)`   emit #3             |
| 81   | `hit.disconnect(_on_hit_a)`             |
| 82   | `hit.emit(5, 6)`   emit #4             |
| 85   | `hit.disconnect(_on_hit_b)`             |
| 86   | `hit.emit(9, 9)`   emit #5 (0 handlers)|

## The recorded call tree (deterministic; entry order = call_key order)

```
@implicit_new                depth 0            source gf_signals.gd
_initialize                  depth 0  parent -1 source gf_signals.gd   <- EMITTER
  (step 66: emit #1)         -- NO handler frame (nothing connected)
  (step 70: connect A)
  (step 71: emit #2)
    _on_hit_a                depth 1  parent _initialize   a_dmg=7 a_kind=2 -> ret 9
  (step 75: connect B)
  (step 76: emit #3)
    _on_hit_a                depth 1  parent _initialize   a_dmg=3 a_kind=4 -> ret 7
    _on_hit_b                depth 1  parent _initialize   b_val=34         -> ret 34
  (step 81: disconnect A)
  (step 82: emit #4)
    _on_hit_b                depth 1  parent _initialize   b_val=56         -> ret 56
  (step 85: disconnect B)
  (step 86: emit #5)         -- NO handler frame (all disconnected)
_process                     depth 0  parent -1 source gf_signals.gd   -> true
```

## Asserted facts (the GF9 teeth)

- **Per-emit handler dispatch** (handler frames appear/disappear EXACTLY per
  connect/disconnect state), keyed by emit line:
  - emit #1 (line 66): **no** handler frame.
  - emit #2 (line 71): exactly `[_on_hit_a]`.
  - emit #3 (line 76): exactly `[_on_hit_a, _on_hit_b]` (both, in connect order).
  - emit #4 (line 82): exactly `[_on_hit_b]` — `_on_hit_a` is **ABSENT** (it was
    disconnected at line 81) while the still-connected `_on_hit_b` is present.
  - emit #5 (line 86): **no** handler frame (all handlers disconnected).
- **Frame counts**: exactly 2 `_on_hit_a` frames + 2 `_on_hit_b` frames = 4
  handler frames total.
- **Nesting**: every handler frame is depth 1, parent = the single `_initialize`
  frame (no intervening emit frame).
- **Emitted args captured** (via read-into-local, per frame invocation):
  - `_on_hit_a` #1 (emit #2): `a_dmg = 7`, `a_kind = 2`; return `9` (Int).
  - `_on_hit_a` #2 (emit #3): `a_dmg = 3`, `a_kind = 4`; return `7` (Int).
  - `_on_hit_b` #1 (emit #3): `b_val = 34`; return `34` (Int).
  - `_on_hit_b` #2 (emit #4): `b_val = 56`; return `56` (Int).
- **types table**: `[None, Int, Float, Bool, String, Variant]` — scalar-only
  (all captured values are Int; the `_process` return is Bool). No `Object`
  (no object local is captured; the signal object is never assigned to a local).
- **balance**: `#call_entry == #call_exit`.
- **checksum**: `CT_GF9_RESULT=100` (0 +7 +(3+34) +56 = 100, via the member
  `total`, exercised across exactly the connected emissions).

## `await signal` → GF10 (deferred, honest)

GF9 is SYNCHRONOUS emit/connect/disconnect only. `await <signal>` (a coroutine
that suspends until the signal fires) is milestone GF10 (async continuation),
not exercised here.

## Non-vacuity (tamper runs, each MUST be rejected)

- `argvalue`     — corrupt an emitted-arg capture (`_on_hit_a` #1 `a_dmg` 7→999);
  breaks the emitted-args assertion.
- `disconnected` — inject a spurious `_on_hit_a` handler frame into the
  post-disconnect emit #4 bucket; the verifier MUST reject it (proves the
  "disconnected handler is ABSENT" assertion is real, not vacuous).
- `dropframe`    — delete an `_on_hit_b` call_entry (+ its exit) from emit #3;
  breaks the "both handlers on a multi-connect" count.
- `retvalue`     — corrupt a handler return value (`_on_hit_b` #1 ret 34→999);
  breaks the captured-return assertion.
