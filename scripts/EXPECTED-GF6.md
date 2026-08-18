# GF6 expected facts — hand-derived from source (first-principles, pre-recording)

These facts are derived by reading the reference program
(`test-programs/gdscript/gf_lambdas.gd`) plus the recorder contract
(`modules/gdscript/gdscript_ct_trace.cpp` + the G2/G3/G4/GF5 hook points in
`gdscript_vm.cpp`) and the Godot lambda runtime, NOT by reading recorder output.
`scripts/verify_gf6.py` asserts them against the real `.ct` produced by the
patched engine (decoded with `ct-print --full`, the same `TraceReader` decode
path `MaterializedReplaySession` uses to present steps, locals-at-a-step, and
call/return frames). If the recorder and these notes ever disagree, one of them
is wrong — do not regenerate this file from recorder output.

## GF6 adds NO recorder code (the load-bearing finding, like GF1/GF2)

A GDScript lambda `func(x): ...` compiles to its **own** `GDScriptFunction`
(the compiler names it `<anonymous lambda>`, `gdscript_compiler.cpp:2308`) that
is wrapped in a `Callable` — a `GDScriptLambdaCallable`
(`gdscript_lambda_callable.cpp`). The runtime facts that make GF6 a pure
coverage milestone:

1. **A lambda invocation goes through `GDScriptFunction::call`.**
   `GDScriptLambdaCallable::call` (`gdscript_lambda_callable.cpp:92`) forwards to
   `function->call(nullptr, args, total_argcount, ...)` (line 120, or line 149
   for the no-capture branch), where `function` is the lambda's
   `GDScriptFunction*`. So the existing hooks all fire for a lambda frame:
   - the G2 per-line step hook (`gdscript_vm.cpp:3928`),
   - the G3 call-entry hook (`gdscript_vm.cpp:666`,
     `gdscript_ct_trace_call(name, source, _initial_line)` — `name` is the
     function's `name` field = `<anonymous lambda>`),
   - the G3+GF5 return hook on both exit paths (`:4025` / `:4049`,
     `gdscript_ct_trace_return(retvalue)`).
   So a lambda `.call(...)` records as a **nested call/return FRAME** named
   `<anonymous lambda>` with its **return value** captured — no new hook.

2. **The lambda's params AND its captured locals are named stack slots.**
   During analysis, `resolve_pending_lambda_bodies`
   (`gdscript_analyzer.cpp`) injects the captured identifiers as **leading
   parameters** of the lambda function ("Add captures as extra parameters at the
   beginning"). So both the captures and the declared params flow through
   `add_parameter -> add_local -> add_stack_identifier`
   (`gdscript_byte_codegen.cpp:35/45`) and land in `stack_debug`, resolvable by
   `debug_get_stack_member_state` — the exact G4 slot->name table
   `gdscript_ct_trace_assign` uses. The lambda's captures are prepended to the
   call args in `GDScriptLambdaCallable::call` (`args[0..captures-1]` =
   captures, `args[captures..]` = params, lines 101-118), matching the leading-
   parameter layout.

3. **Params/captures are prologue-placed, not `OPCODE_ASSIGN`ed** — so, exactly
   like GF5's *supplied* arguments, they are not re-captured on their own slot;
   they are read into fresh named locals at lambda execution to observe them.
   The lambda body does `var seen_x := x` (the PARAM) and
   `var seen_base := base` (the CAPTURE), each an `OPCODE_ASSIGN` the G4 value
   hook captures by name.

4. **Capture is BY VALUE.** `OPCODE_CREATE_LAMBDA` snapshots each capture with
   `captures.write[i] = *arg` (`gdscript_vm.cpp:2700`) into the
   `GDScriptLambdaCallable`'s `Vector<Variant> captures`
   (`gdscript_lambda_callable.cpp:153`). Mutating the outer local afterwards does
   NOT change the snapshot. The fixture proves this: after `base = 999`, the
   lambda still returns 15 and `seen_base` still reads 10.

Empirically GF6 therefore uses the **unchanged GF5-era binary**
(`bin/godot.macos.template_debug.arm64`); the recorder sources are byte-identical
and the G2/G3/G4/GF1..GF5 regressions all still pass.

## The reference program (`test-programs/gdscript/gf_lambdas.gd`)

| construct | code | fact |
| --------- | ---- | ---- |
| capture + param lambda | `var add := func(x): ... return x + base` | frame `<anonymous lambda>` under `_init`, returns `15` |
| capture-by-value | `base = 999; add.call(5)` | STILL `15`; `seen_base` STILL `10` |
| lambda in a var | `var doubler := func(n): return n * 2` | `doubler` captured as Callable; `doubler.call(21)` -> `42` |
| nested lambda, two scopes | `outer` -> `inner` capturing `a` (from `_init`) + `b` (from `outer`) | `inner` frame at depth 2 under `outer`, returns `307` |

Execution / call_key (entry) order of the 5 lambda frames:
`add#1`, `add#2`, `doubler`, `outer`, `inner` (`inner` entered while `outer` is
on the stack, so `inner.call_key > outer.call_key`).

## Captured named locals (`verify_gf6.py EXPECTED_CAPTURES`), in step order

- `base` = [Int 10, Int 999] — the outer local: created 10, then mutated to 999.
- `r1` = [Int 15], `r2` = [Int 15] — `r2` proves capture-by-value (base was 999).
- `r3` = [Int 42], `a` = [Int 100], `rn` = [Int 307], `total` = [Int 379].
- `seen_x` = [Int 5, Int 5] — the lambda PARAM `x`, read at execution (add x2).
- `seen_base` = [Int 10, Int 10] — the CAPTURE `base`, read at execution.
  **10 on BOTH calls** even though the outer `base` became 999 -> capture-by-value.
- `b` = [Int 200] — the outer lambda's local.
- `seen_a` = [Int 100] — `a`, captured transitively from `_init` into `inner`.
- `seen_b` = [Int 200] — `b`, captured from the `outer` lambda into `inner`.

The lambda param slot `x` and the lambda capture slot `base` are NOT captured
under their own names (prologue-placed, no assign) — they surface via the
`seen_*` reads above, the same way GF5's supplied args surface via `got_*`.

## Lambdas stored in vars -> Callable (`verify_gf6.py EXPECTED_CALLABLES`)

`add`, `doubler`, `outer`, `inner` are each value-captured (on their `var … :=
func…` assign) as the GF4 shallow `Struct "Callable"` with a single field
`method` = String `<anonymous lambda>` (`GDScriptLambdaCallable::get_method()`
returns `function->get_name()`; `gdscript_ct_trace.cpp gct_callable`). This is
faithful, not overclaimed: the recorder emits the Callable's method name, not a
deep dump of the lambda body or its captured environment.

## Return values (`verify_gf6.py EXPECTED_RETURNS`, on `call_exit`)

- `<anonymous lambda>` -> [Int 15, Int 15, Int 42, Int 307, Int 307]
  (call_key order: add#1, add#2, doubler, outer, inner).
- `_init` -> [None], `@implicit_new` -> [None] (no return).
- `_process` -> [Bool true].

## Call tree (nesting)

- `_init`: depth 0, parent -1.
- 4 `<anonymous lambda>` frames at depth 1 with `parent_call_key == _init`:
  `add` (×2), `doubler`, `outer`.
- 1 `<anonymous lambda>` frame at depth 2 (`inner`), `parent_call_key` = the
  `outer` frame's `call_key` (a depth-1 lambda). `inner` returns 307 and its
  parent `outer` also returns 307 (the nested-lambda teeth).
- `@implicit_new`, `_process`: engine top-level frames (depth 0, parent -1).
- Every `call_entry` has a matching `call_exit` (balanced).

## Types table

The scalars are interned eagerly (`[None, Int, Float, Bool, String, Variant]`,
None at TypeId 0). The first Callable value (`var add := func…`) lazily interns
the `Callable` struct type. No Float/Array/Dictionary value is captured, but the
Float scalar type is always registered eagerly. So the table is exactly
`[None, Int, Float, Bool, String, Variant, Callable]`.

## Determinism

`add.call(5)=15`, `add.call(5)=15` (capture-by-value), `doubler.call(21)=42`,
`outer.call(7)` -> `inner`: `7 + a(100) + b(200) = 307`. `total = 15 + 15 + 42 +
307 = 379`; the program prints `CT_GF6_RESULT=379`.

## Tamper runs (prove the verifier is not vacuous)

`verify_gf6.py tamper <full.json> <mode>` corrupts the decoded doc and requires
the same assertions to then FAIL:

- `param` — a lambda PARAM captured value (`seen_x` 5 -> 99).
- `captured` — a CAPTURED outer value (first `seen_base` 10 -> 99).
- `capturebyvalue` — simulate capture-by-REFERENCE (2nd `seen_base` 10 -> 999);
  expected `[10,10]`, so it must be rejected — this is the teeth on the
  capture-by-value semantics specifically.
- `return` — a lambda return value (first `<anonymous lambda>` 15 -> 99).
- `nesting` — reparent the nested (depth-2) lambda up to `_init` (depth 1),
  breaking the depth-2 nesting assertion.

Each MUST be rejected; the runner fails if any tamper slips through.
