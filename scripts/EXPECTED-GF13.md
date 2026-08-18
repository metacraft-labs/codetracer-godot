# EXPECTED — GF13: Diagnostics & String Formatting (assert / push_error / push_warning / format)

First-principles facts, hand-derived BEFORE running the recorder, that
`scripts/verify_gf13.py` asserts against the real `.ct` produced by the patched
engine over `test-programs/gdscript/gf_diag.gd`, decoded via `ct-print --full`.
This is the LAST GF-series milestone.

GDScript has **no exceptions** — there is no try/catch, and none is invented.
GF13 covers the three remaining features: string formatting, `assert`, and the
`push_error` / `push_warning` diagnostic pair.

## Engine change (the honest scope)

- **String formatting: NO engine change.** `"%d/%s/%.2f" % [...]`,
  `"{0} {1}".format([...])`, raw `r"..."`, triple-quoted `"""..."""`, and `+`
  concatenation all evaluate to a String that lands in a NAMED local via
  `OPCODE_ASSIGN` — captured by the existing G4 String path. A COVERAGE result.
- **assert: NO engine change.** An `assert(cond, msg)` statement occupies its own
  source line, so its `OPCODE_LINE` already records it as an ordinary step. A
  passing assert is a no-op step (execution continues, proven by the later
  steps). A FAILING assert halts the VM (`OPCODE_BREAK`) in a debug build —
  probed separately (see below).
- **push_error / push_warning: ONE small engine hook**, confined to
  `modules/gdscript/`. These are native CORE Variant utility functions
  (`variant_utility.cpp`), vararg, so the compiler emits them as
  `OPCODE_CALL_UTILITY` (never the validated form — vararg forbids it). A hook in
  that opcode calls `gdscript_ct_trace_utility_diagnostic(function, args, argc)`;
  when `function` is `push_error`/`push_warning` it records the diagnostic as an
  **events.dat special event** (the SAME channel GF10's async markers use — NOT
  an exception model) carrying the joined message. Every other utility call
  (`print`, `str`, ...) is ignored, so a program that calls no diagnostics is
  byte-identical to GF12.

## Representation of push_error / push_warning (honest)

Recorded as trace EVENTS (option (a)), not caller-frame steps. The recorder
maps `push_error` -> `FFI_EVENT_ERROR` and `push_warning` ->
`FFI_EVENT_TRACE_LOG_EVENT`; the multi-stream writer's `toIOEventKind` then
renders them, via `ct-print --full`, as two DISTINCT `kind=="io"` events:

| field      | push_warning         | push_error         |
|------------|----------------------|--------------------|
| `io_kind`  | `ioStderr`           | `ioError`          |
| `text`     | `gf13 warning`       | `gf13 error`       |
| `metadata` | `ct-push-warning`*   | `ct-push-error`*   |

The message is the `content` (surfaced as `text`), matching the engine's own
`join_string` of the args. `push_error` uses the real error io kind; a warning
has no dedicated wire kind so it rides the neutral trace-log kind (rendered
`ioStderr`, which is also where Godot's own `WARN_PRINT` writes).

*The recorder ALSO writes a level tag into the event metadata for a real
event-log pane, but `ct-print --full` does NOT surface multi-stream io metadata
(GF10's async markers documented the same), so the verifier keys off
`io_kind` + `text` — both of which ct-print DOES surface, and which already
distinguish warning from error unambiguously.

## Source line numbers (from gf_diag.gd, `_init`)

| line | statement                        | captured named local  | value                         |
|------|----------------------------------|-----------------------|-------------------------------|
| 44   | `var i := 7`                     | `i`   (Int)           | `7`                           |
| 45   | `var s := "a"`                   | `s`   (String)        | `"a"`                         |
| 46   | `var f := 3.14159`               | `f`   (Float)         | `3.14159`                     |
| 47   | `var pf := "%d/%s/%.2f" % [...]` | `pf`  (String)        | `"7/a/3.14"`  (len 8)         |
| 48   | `var a := "x"`                   | `a`   (String)        | `"x"`                         |
| 49   | `var b := "y"`                   | `b`   (String)        | `"y"`                         |
| 50   | `var ff := "{0} {1}".format(...)`| `ff`  (String)        | `"x y"`       (len 3)         |
| 51   | `var raw := r"a\nb"`             | `raw` (String)        | `a` `\` `n` `b` (literal `\`, len 4) |
| 52   | `var tq := """line1<NL>line2"""` | `tq`  (String)        | `"line1\nline2"` (real NL, len 11) |
| 54   | `var cc := s + "-" + a`          | `cc`  (String)        | `"a-x"`       (len 3)         |
| 55   | `var ok := (i == 7)`             | `ok`  (Bool)          | `true`                        |
| 56   | `assert(ok, "i must be 7")`      | — (no-op STEP)        | passing → execution continues |
| 57   | `push_warning("gf13 warning")`   | — (STEP + io event)   | warning event, msg `gf13 warning` |
| 58   | `push_error("gf13 error")`       | — (STEP + io event)   | error event,   msg `gf13 error`   |
| 59   | `var checksum := ...`            | `checksum` (Int)      | `29`                          |
| 60   | `print(...)`                     | —                     | prints `CT_GF13_RESULT=29`    |

Deterministic checksum: `8 + 3 + 4 + 11 + 3 = 29` → stdout `CT_GF13_RESULT=29`.

## The teeth (what verify_gf13.py asserts — not "a trace exists")

1. **Exact formatted-string values**: `pf=="7/a/3.14"`, `ff=="x y"`,
   `raw=="a\\nb"` (literal backslash, 4 chars), `tq=="line1\nline2"` (embedded
   newline, 11 chars), `cc=="a-x"`, each captured as a `String` on the step at
   its own source line; plus `i==7`, `ok==true`.
2. **assert is a recorded step**: a step exists at line 56 AND steps at the
   later lines 57/58/59/60 exist (a passing assert did not halt).
3. **push_warning / push_error recorded as events**: exactly one io event with
   `io_kind=="ioStderr"` and `text=="gf13 warning"`; exactly one with
   `io_kind=="ioError"` and `text=="gf13 error"`.
4. **Types table unchanged (scalar-only)**: `[None, Int, Float, Bool, String, Variant]`
   (None at TypeId 0). The format arrays `[i, s, f]` / `[a, b]` are temporaries,
   never captured, so no Array type is registered.

Non-vacuity is proven by tamper runs (wrong formatted string / missing assert
step / wrong-or-missing push message), each of which the verifier MUST reject.

## Failing-assert probe (separate run — gf_diag_assert_fail.gd)

| line | statement                     | recorded? |
|------|-------------------------------|-----------|
| 19   | `var x := 1`                  | step yes  |
| 20   | `assert(x == 2, "x must be 2")`| step yes (then HALT) |
| 21   | `print("SHOULD_NOT_REACH")`   | NO step (never executed) |

The failing assert aborts the CURRENT frame (`_init`): `SHOULD_NOT_REACH`
(line 21) never prints and never records a step. The recorder flushes what it
recorded up to the halt (atexit close). Observed recorded step lines
`[19, 20, 24]` — the assert-line step (20) is present, line 21 is absent, and
line 24 is the `_process` frame: the engine's MainLoop still ticks `_process`
once (it returns `true`, ending the loop) AFTER the aborted `_init`. That is the
honest engine behavior — a failing assert halts the running function, not the
whole process — and the trace legitimately shows no `_init` step past line 20.
