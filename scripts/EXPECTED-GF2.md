# GF2 expected facts — hand-derived from source (first-principles, pre-recording)

These facts are derived by reading the reference program plus the confirmed
GDScript step-emission model, NOT by reading recorder output.
`scripts/verify_gf2.py` asserts them against the real `.ct` produced by the
patched engine (decoded with `ct-print --full`). If the recorder and these notes
ever disagree, one of them is wrong — do not regenerate this file from recorder
output.

## Reader choice

Verification reads the trace through `ct-print --full` — the canonical Nim CTFS
decoder from `codetracer-trace-format-nim`, the same `TraceReader` decode path
`MaterializedReplaySession` (`codetracer/src/db-backend`) uses to present
steps + locals-at-a-step. Each `step` event carries `function`, `line`, and a
`vars[]` array of decoded `{varname, value}`.

## The step-emission model (confirmed empirically against the unchanged binary)

GF2 needs **no new VM hook**. Control flow is just per-line `OPCODE_LINE` steps
(G2); a match binding pattern (`var x`) binds a local via `OPCODE_ASSIGN` into a
named slot, captured by the G4 assign hook. The model, established with throwaway
probes against the G4/GF1-era `bin/godot.macos.template_debug.arm64` (byte
identical — GF2 changes only `test-programs/` and `scripts/`):

1. **Every executed statement line emits exactly one step**, in execution order,
   including `return`, `break`, `continue`, and `pass`.
2. **A `for`/`while` HEADER emits its step only once**, at loop entry. The loop
   back-edge re-enters the body, not the header line, so the header line does
   NOT repeat per iteration. The **body lines repeat once per iteration** — so
   the iteration count equals the multiplicity of a body line.
3. **`if`/`elif` condition headers emit a step** when evaluated. An **`else:`
   header emits NO step** — control falls straight into the else body (only the
   body line appears). Only the TAKEN branch's body lines appear; untaken bodies
   are absent. This is what makes branch selection observable.
4. **`match`**: the `match` header line emits a step; each pattern line that is
   TESTED emits a step (including the wildcard `_:` when it is reached and the
   matched arm); the matched arm's body lines emit steps. Patterns after the
   matched arm are never tested, so their lines are absent. A comma/alternative
   pattern (`1, 2, 3:`) is one line → one step. A binding pattern (`var x`,
   `[.. var x ..]`, `{"k": var x}`, `var x when guard`) captures the bound value
   AT THE PATTERN LINE (the assign into the named slot happens there).
5. The `_process` MainLoop callback runs once after `_init` and emits one step
   at its `return true` line; `@implicit_new` emits no step. `verify_gf2.py`
   excludes `_process`/`@`-frames — the control-flow logic under test is `_init`
   plus its helpers.
6. **ct-print mislabels the FIRST step's `function`** (frame-name attribution
   lags by one at frame entry): the first `_init` step is labelled `classify`.
   The LINE number is correct. GF2 therefore asserts the step **line** sequence,
   not function labels — and line numbers are authoritative because every source
   statement in `gf_control_flow.gd` occupies a globally-unique line.

## Slot aliasing → assert lines, not names (for control flow)

Locals declared in sibling branches of an `if`/`match` reuse a single stack slot,
so their captured NAME can alias (a probe showed an `elif` body's local reported
under a sibling branch's name). GF2 sidesteps this: each construct is a separate
helper function, and each `if`/`match` arm reassigns ONE local (e.g. `tag`, `r`)
rather than declaring a per-arm local. Branch/arm selection is asserted on the
globally-unique step LINE numbers. Match binding VALUES (`b`, `rest`, `first`,
`dv`, `g`) live in separate `match` statements, each owning its binding slot, so
those names are unambiguous and ARE asserted by name.

## `test-programs/gdscript/gf_control_flow.gd` — expected step sequence

Applying the model to the source (line numbers from the committed file).
Execution order: the `_init` call-site line first, then the callee's body.

| # | construct (input)              | `_init` call-site | callee body step lines |
|---|--------------------------------|-------------------|------------------------|
| 1 | `classify(-5)` — NEG arm       | 153 | 39, 40, **41**, 46 |
| 2 | `classify(0)` — ZERO arm       | 154 | 39, 40, 42, **43**, 46 |
| 3 | `classify(7)` — ELSE arm       | 155 | 39, 40, 42, **45**, 46 (no step for `else:` @44) |
| 4 | `sum_for()` — for + continue   | 157 | 50, 51, 52, 54, 52, 54, 52, **53**, 52, 54, 55 |
| 5 | `count_while()` — while + break| 158 | 59, 60, 61, 62, 61, 62, 61, 62, **63**, 64 |
| 6 | `noop()` — pass                | 159 | **68**, 69 |
| 7 | `m_literal(2)` — literal arm    | 161 | 73, 74, 75, **77**, 78, 81 |
| 8 | `m_literal(99)` — wildcard arm  | 162 | 73, 74, 75, 77, **79**, 80, 81 |
| 9 | `m_expression(100)` — `LIMIT:`  | 163 | 85, 86, **87**, 88, 91 |
|10 | `m_comma(3)` — `1, 2, 3:`       | 164 | 95, 96, **97**, 98, 101 |
|11 | `m_bind(77)` — `var b:`         | 165 | 105, 106, **107**, 108, 109 |
|12 | `m_array([1,2])` — `[1,var rest]`| 166 | 113, 114, **115**, 116, 119 |
|13 | `m_array_open([7,8,9])` — `[var first, ..]` | 167 | 123, 124, **125**, 126, 129 |
|14 | `m_dict({"key":42})` — `{"key": var dv}` | 168 | 133, 134, **135**, 136, 139 |
|15 | `m_guard(8)` — `var g when g > 5` | 169 | 143, 144, **145**, 146, 149 |
| — | checksum + print               | 170, 171 | — |

Total: **102 step lines** (excluding the trailing `_process` step).

### Control-flow evidence extracted from the sequence

- **if/elif/else branch selection** (rows 1–3): each call takes a different arm.
  NEG shows body 41 (not 43/45); ZERO shows 42 (elif tested) then 43 (not
  41/45); POS/else shows 42 (elif tested), then 45 directly (no step for the
  `else:` header 44, no body 41/43). The taken body appears; the untaken bodies
  do not.
- **for + continue** (row 4): `range(4)` ⇒ body line 52 appears **4 times** (4
  iterations). The `continue` at line 53 fires exactly once (i == 2), and the
  add at line 54 appears only **3 times** — the i == 2 pass is skipped. The `for`
  header 51 appears once.
- **while + break** (row 5): body line 61 appears **3 times** (w = 1, 2, 3); the
  `break` at 63 fires once at w == 3, truncating what would otherwise be ~100
  iterations. The `while` header 60 appears once.
- **pass** (row 6): line 68 (`pass`) appears once as a no-op step.
- **match dispatch** (rows 7–15): for every pattern kind the tested pattern line
  and the matched body appear; unmatched fall-through arms (`_:` bodies at 76,
  90, 100, 118, 128, 138, 148) never appear.

## Match binding values (captured at the pattern line)

| line | binding | value | kind |
|------|---------|-------|------|
| 107  | `b`     | 77    | Int  |
| 115  | `rest`  | 2     | Int  | (`[1, var rest]` on `[1, 2]` binds the element, not a sub-array) |
| 125  | `first` | 7     | Int  | (`[var first, ..]` binds element 0) |
| 135  | `dv`    | 42    | Int  |
| 145  | `g`     | 8     | Int  | (bound before the `when g > 5` guard is evaluated) |

## Deterministic checksum

```
classify: 1 + 2 + 3            = 6
sum_for:  0 + 1 + 3            = 4    (i == 2 skipped by continue)
count_while:                    = 3    (break at w == 3)
noop:                           = 0
m_literal(2):                   = 12
m_literal(99):                  = 19
m_expression(100):              = 100
m_comma(3):                     = 1
m_bind(77):                     = 77
m_array([1,2]):                 = 2
m_array_open([7,8,9]):          = 7
m_dict({"key":42}):             = 42
m_guard(8):                     = 8
------------------------------------------------
checksum                        = 281
```

So stdout contains `CT_GF2_RESULT=281`.

## Non-vacuity (tamper runs)

`verify_gf2.py tamper <mode>` corrupts the decoded doc and re-runs the
assertions, which MUST then fail (exit 0 iff the tamper was caught):

- **branch**: rewrite the taken elif body step (line 43, `classify(0)`) to the
  `if` body line 41 — simulates a DIFFERENT branch being taken. Caught by the
  ordered-sequence assertion (index 9 diverges).
- **iter**: delete one occurrence of the for-body line 52 — simulates 3
  iterations instead of 4. Caught by the sequence + occurrence-count assertion.
- **binding**: change the array binding `rest` from 2 to 999 at line 115. Caught
  by the binding-value assertion.

## Regression (must be unchanged by GF2 — recorder is byte-identical)

GF2 adds no recorder code, so the earlier fixtures must record identically:

- GF1 (`gf_typing.gd`): 30 captured values; `CT_GF1_RESULT=1222`.
- G4 (`gf_values.gd`): 8 captured values; `CT_G4_RESULT=47`.
- G3 (`gf_calls.gd`): `_init → outer → inner` nesting; steps
  `37, 30, 26, 27, 31, 38, 34, 39, 42`; `CT_G3_RESULT=107`.
- G2 (`g2probe.gd`): step lines `18, 19, 20, 14, 15, 21, 24`; `CT_G2_STEPS=30`.
