# GF5 expected facts — hand-derived from source (first-principles, pre-recording)

These facts are derived by reading the reference program
(`test-programs/gdscript/gf_functions.gd`) plus the recorder contract
(`modules/gdscript/gdscript_ct_trace.cpp` + the G3/G4 hook points in
`gdscript_vm.cpp`), NOT by reading recorder output. `scripts/verify_gf5.py`
asserts them against the real `.ct` produced by the patched engine (decoded with
`ct-print --full`, the same `TraceReader` decode path `MaterializedReplaySession`
uses to present steps, locals-at-a-step, and call/return frames). If the recorder
and these notes ever disagree, one of them is wrong — do not regenerate this file
from recorder output.

## The GF5 recorder change: return VALUES

Through GF4, `gdscript_ct_trace_return()` emitted a bare `register_return()` (the
call/return pair carried no value; ct-print showed a `Void` return marker). GF5
extends it to `gdscript_ct_trace_return(const Variant &return_value)`: the VM's
`retvalue` (the Variant the `OPCODE_RETURN*` opcode stored, or a
default-constructed `NIL` for a `-> void` / fall-off-the-end function) is encoded
with the SAME recursive `ct_value_*` encoder G4/GF3/GF4 use for locals and
attached to the return record via `trace_writer_register_return_cbor`.

This is **additional data on the existing return event**. `register_return_cbor`
routes through the SAME `MultiStreamTraceWriter.registerReturn` the bare
`register_return` does (just with value bytes), so it does NOT add, remove, or
reorder any call/return record. The G3 nesting + balanced-pair invariant is
therefore UNCHANGED — proven by the G3 regression still passing (6 balanced pairs
in `gf_calls.gd`, unchanged nesting) and the G2 step stream
`[18,19,20,14,15,21,24]` unchanged. Returns live in the call stream, not
`values.dat`, so there is no parallel-index constraint (the return hook does not
require `g_ct_started`).

Both `GDScriptFunction::call` exit paths (the normal / yield-suspend exit at
`gdscript_vm.cpp:~4025` and the await-resume completion exit at `~4049`) pass the
same `retvalue`, so the value is captured regardless of which exit fires.

## `-> void` / no-return -> None (documented decision)

A `-> void` function, or any function that falls off the end without a `return`,
leaves `retvalue` a default-constructed `Variant` (`NIL`). GF5 encodes that as a
real **None** value node (`kind "None"`, `type_id 0`) — consistent with G4's
`null -> None` scalar handling — NOT the format's bare one-byte `VoidReturnMarker`.
So `do_void() -> void` records `return_value = None`, and `_init` (no return) and
`@implicit_new` also record `None`.

## Default arguments — how they are captured

GDScript compiles optional parameters into a **default-argument bytecode block**
emitted at the top of the function (`start_parameters` /
`write_assign_default_parameter`, `gdscript_compiler.cpp:2438` +
`gdscript_byte_codegen.cpp:1017`). `write_assign_default_parameter` emits a plain
`write_assign` — i.e. an `OPCODE_ASSIGN` (already hooked by G4's
`CT_TRACE_ASSIGN`) into the **parameter's own stack slot**.

- **Omitted (defaulted) arg:** when a caller supplies fewer args than the
  parameter count, the VM sets `defarg = _argument_count - p_argcount` and, via
  `OPCODE_JUMP_TO_DEF_ARGUMENT`, jumps into that block, which runs the default
  expression and `OPCODE_ASSIGN`s the result to the param slot. The value hook
  sees this assign and captures the parameter **directly, by name, with its
  default value**.
- **Supplied arg:** the argument is placed straight into the callee's stack slot
  by the call prologue (`memnew_placement`, `gdscript_vm.cpp:~583`) with NO
  `OPCODE_ASSIGN`, and the default block is skipped. So a supplied arg is NOT
  re-captured on the param slot; its bound value is observed by reading/copying
  it into a fresh local.

**Line attribution quirk (documented honestly):** the default-argument block runs
BEFORE the callee executes its first `OPCODE_LINE`, so the VM's `line` still holds
the caller's call-site line. In this fixture the defaulted-call default
materialization therefore attaches `b`/`c` to a step at **line 62** (the
`var d1 = configure(1)` call site) attributed to the `configure` frame, rather
than to a line inside `configure`'s body. This does not affect correctness of the
captured NAME/VALUE (`b=10`, `c="x"`); the verifier asserts the captured values,
not the line of the default-materialization step.

The fixture copies each param into a fresh local (`got_a`/`got_b`/`got_c`) so the
BOUND value (default or supplied) is captured uniformly in BOTH calls.

## User-defined variadic functions: language N/A (NOT a recorder gap)

**GDScript 4 has NO user-defined variadic functions.** A GDScript `func` has a
fixed parameter list (with optional trailing defaults, covered above); there is
no `*args` / rest-parameter syntax for user code. Only some engine BUILT-IN /
native methods are variadic (e.g. `print`, `printerr`, `str`, `Callable.call`).

This is a **language N/A**, not a recorder limitation: there is nothing for the
recorder to record because the construct does not exist in the language. We do
NOT fabricate a user vararg function. Instead the fixture exercises the only
variadic surface GDScript has — a variadic BUILT-IN call, `print("gf5",
"variadic", total)` — and shows it is recorded as a **caller-frame step** (an
`OPCODE_CALL` to a native method) in `_init`, NOT as a GDScript call/return frame:
there is no `call_entry` for `print`. The recorder deliberately does not hook
`OPCODE_CALL*` native call sites (the G3 scope note), so a native/builtin call
surfaces only via the per-line step in the caller — which is exactly what a
variadic builtin should look like.

## The reference program

`test-programs/gdscript/gf_functions.gd` (line numbers are load-bearing for the
step attribution but the verifier asserts on captured VALUES + call/return
structure, not on body line numbers, so it is robust to fixture edits above the
functions):

| function                         | kind             | return type | called as                | return value |
| -------------------------------- | ---------------- | ----------- | ------------------------ | ------------ |
| `configure(a, b := 10, c := "x")`| defaults         | `-> int`    | `configure(1)`           | `12` (Int)   |
| `configure(...)`                 | defaults         | `-> int`    | `configure(2, 20, "yz")` | `24` (Int)   |
| `mul(a, b)`                      | **static**       | `-> int`    | `mul(6, 7)`              | `42` (Int)   |
| `area(r: float)`                 | typed float ret  | `-> float`  | `area(2.0)`              | `12.56636` (Float) |
| `do_void()`                      | **void**         | `-> void`   | `do_void()`              | `None`       |
| `pick(flag)`                     | **untyped** ret  | (none)      | `pick(true)`             | `42` (Int)   |
| `_init()`                        | ctor             | (none)      | engine                   | `None`       |
| `_process(_delta)`               | lifecycle        | (none)      | engine                   | `true` (Bool)|
| `@implicit_new`                  | engine-synth     | —           | engine                   | `None`       |

## Captured named locals (verify_gf5.py `EXPECTED_CAPTURES`)

In step order across the whole trace:

- `b` = [Int 10] — **direct default materialization** on the param slot (defaulted
  call only; the supplied call places its arg via the prologue with no assign).
- `c` = [String "x"] — likewise.
- `got_a` = [Int 1, Int 2] — param copy: defaulted call, then supplied call.
- `got_b` = [Int 10, Int 20] — proves the default (10) then the supplied (20).
- `got_c` = [String "x", String "yz"].
- `fa` = [Int 6], `fb` = [Int 7] — the **static** function `mul`'s args.
- `rad` = [Float 2.0] — `area`'s typed-float param copy.
- `touched` = [Int 1] — a local in the `-> void` function.
- `d1` = [Int 12], `d2` = [Int 24], `m` = [Int 42], `ar` = [Float 12.56636],
  `pk` = [Int 42], `total` = [Int 120] — `_init` locals.

## Return values (verify_gf5.py `EXPECTED_RETURNS`, on `call_exit`)

- `configure` -> [Int 12, Int 24] (call_key order = defaulted then supplied)
- `mul` -> [Int 42]  (static function's return value)
- `area` -> [Float 12.56636]  (typed float return)
- `do_void` -> [None]  (`-> void`: retvalue NIL encoded as None)
- `pick` -> [Int 42]  (UNTYPED return)
- `_init` -> [None], `@implicit_new` -> [None]  (no return)
- `_process` -> [Bool true]

## Call tree

`configure` (×2), `mul`, `area`, `do_void`, `pick` all nest directly under
`_init` (depth 1, `parent_call_key == _init.call_key`). `@implicit_new` and
`_process` are engine top-level frames (depth 0, parent -1). Every `call_entry`
has a matching `call_exit` (balanced), and the static function `mul` appears as a
single ordinary nested frame like any instance method.

## Determinism

`configure(1)=12`, `configure(2,20,"yz")=24`, `mul(6,7)=42`, `pick(true)=42` ->
`total = 12 + 24 + 42 + 42 = 120`; the program prints `CT_GF5_RESULT=120` (and a
variadic `print("gf5", "variadic", 120)` -> `gf5variadic120`).

## Types table

All captured values and all return values in this fixture are scalars
(Int/Float/String/Bool/None), so the `types` table stays exactly the 6 base
entries `[None, Int, Float, Bool, String, Variant]` (None at TypeId 0) — no
struct/collection type is interned. This is asserted, and it confirms
return-value capture did not eagerly register extra types.

## Tamper runs (prove the verifier is not vacuous)

`verify_gf5.py tamper <full.json> <mode>` corrupts the decoded doc and requires
the same assertions to then FAIL:

- `default` — the direct default capture `b` 10 -> 99 (wrong param default value).
- `return` — `configure`'s first return 12 -> 99 (wrong return value).
- `staticargs` — the static `mul`'s `fa` 6 -> 99 (wrong static-call arg).
- `void` — `do_void`'s return None -> Int (wrong void return).

Each MUST be rejected; the runner fails if any tamper slips through.
