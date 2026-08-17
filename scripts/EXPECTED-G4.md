# G4 expected facts — hand-derived from source (first-principles, pre-recording)

These facts are derived by reading the reference program, NOT by reading
recorder output. `scripts/verify_g4.py` asserts them against the real `.ct`
produced by the patched engine (decoded with `ct-print --full`). If the
recorder and these notes ever disagree, one of them is wrong — do not
regenerate this file from recorder output.

## Reader choice

Verification reads the trace through `ct-print --full` — the canonical Nim
CTFS decoder from `codetracer-trace-format-nim`. Its `--full` mode decodes each
step's captured variables from CBOR into structured JSON: every `step` event
carries a `vars[]` array of `{varname, type_id, type_name, value}`, where
`value` is the decoded value (`{"kind":"Int","i":10,...}`,
`{"kind":"None",...}`, etc.). This is the SAME `TraceReader` decode path
`MaterializedReplaySession` (`codetracer/src/db-backend`) uses to present
locals-at-a-step, so asserting on `ct-print --full`'s `vars[]` tests the exact
consumer view of the value stream. `MaterializedReplaySession` is not driven
directly because ct-print already surfaces the decoded name+value+type per step
that G4 must prove; a bespoke db-backend harness would add no assertion power.

## `test-programs/gdscript/gf_values.gd` — captured values (G4)

G4 scope: the common scalar/String types — int, float, bool, String, null —
captured on WRITTEN named stack slots. Each `var x := <literal>` (and the
reassignment `i = i + 5`) is a NON-call assignment: the OPCODE_LINE for that
source line fires first (registering the step), then the assign/operator opcode
writes the slot, so the captured value attaches to the step AT THAT LINE — the
values.dat ↔ steps.dat parallel index. Compiler temporaries carry no
source-level name in `GDScriptFunction::stack_debug` (or an `@`-prefixed one)
and are skipped.

Expected captured variables, each attached to the step at its own source line:

```
function  line  varname    value.kind  value        type_name
--------  ----  ---------  ----------  -----------  ---------
scale     30    received   Int         15           Int
scale     31    factor     Int         30           Int        (the ARGUMENT, by name)
_init     35    i          Int         10           Int
_init     36    f          Float       2.5          Float
_init     37    b          Bool        true         Bool
_init     38    s          String      "hi"         String
_init     39    n          None        (null)       None
_init     40    i          Int         15           Int        (reassignment i = i + 5)
```

Derivation:

- `var i := 10` / `var f := 2.5` / `var b := true` / `var s := "hi"` /
  `var n = null` — each writes the literal to that local's stack slot; the
  Variant type at the write is INT / FLOAT / BOOL / STRING / NIL, mapped to
  trace values Int / Float / Bool / String / None.
- `i = i + 5` — the `+` operator (OPCODE_OPERATOR / OPCODE_OPERATOR_VALIDATED)
  and/or the following typed assign writes 15 back into `i`'s slot at line 40.
  So `i` is captured twice: 10 at line 35, 15 at line 40 (both correct; the
  reassignment is a distinct step-attached value).
- `scale(i)` is called with `i == 15`. Inside `scale`:
  - `var received = factor` mirrors the passed argument value → `received = 15`,
    proving the argument flowed in.
  - `factor = factor * 2` reassigns the ARGUMENT slot → `factor = 30`, captured
    BY ITS SOURCE NAME `factor` (function parameters are registered in
    `stack_debug` via `add_parameter`→`add_local`, so they resolve to a name
    exactly like locals).

Value of `r = scale(15) = 30` is verified through the deterministic stdout
checksum, NOT as a per-step captured variable: `r` is written by the call's
return path (OPCODE_CALL_RETURN), which — like the native-call-site opcodes
deferred in G3 — is outside the assign/operator write opcodes G4 hooks. The
loader still shows a value for `r` (30) at line 42 because the writer attaches
the return value to the then-current step; asserting `r`'s exact step is left to
a later milestone. G4 asserts only the assign/operator-written locals above.

Deterministic checksum: `i + int(f) + r = 15 + 2 + 30 = 47`, so stdout contains
`CT_G4_RESULT=47`.

## Parallel-index (values ↔ steps) integrity

Every expected value above must be attached to the step whose `function` AND
`line` match the row — not merely "present somewhere". `verify_g4.py` asserts
each `(function, line, varname, kind, value)` tuple on the correct step and
fails if a value is missing, on the wrong step, under the wrong name, or with
the wrong value/kind. Three tamper runs (wrong value / wrong name / wrong step)
prove the verifier rejects each corruption (exit nonzero), so the assertion is
not vacuous.

## Types table

The recorder registers the None type FIRST so it occupies `TypeId(0)` (the CTFS
`NONE_TYPE_ID` invariant), then Int, Float, Bool, String, and a `Variant`
fallback (Raw, the GF4 extension point). Expected `types`:

```
None, Int, Float, Bool, String, Variant
```

## Regression (must be unchanged by value capture)

- G2 (`g2probe.gd`): step lines `18, 19, 20, 14, 15, 21, 24`; `CT_G2_STEPS=30`.
- G3 (`gf_calls.gd`): `_init → outer → inner` nesting, `sibling` after `outer`,
  6 balanced call/return pairs, steps `37, 30, 26, 27, 31, 38, 34, 39, 42`;
  `CT_G3_RESULT=107`.

Value capture must not perturb the step or call streams: the value hooks fire
strictly between an OPCODE_LINE step and the next, and register only
variable-value records (values.dat), which do not advance the step index.
