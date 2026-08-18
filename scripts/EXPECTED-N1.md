# EXPECTED — N1 (Nested-Trace Join Keys + Correlation Record)

First-principles facts for `test-programs/gdscript/n1_nested.gd`, hand-derived by
reasoning about the program before looking at recorder output. Asserted by
`scripts/verify_n1.py` (see the header there for the full teeth) and the runner
`scripts/record-and-verify-n1.sh`.

## Program

```gdscript
extends MainLoop
func helper(n):
	var a = []          # untyped Array
	a.append(n)         # native-call
	a.append(n + 1)     # native-call
	return a.size()     # native-call
func _init():
	var x = 10
	var c = helper(x)
	var total = x + c   # 12
	print("CT_N1_RESULT=%d" % total)
func _process(_delta):
	return true
```

Deterministic result: `helper(10)` builds `[10, 11]`, `size() == 2`, so
`total = 10 + 2 = 12` → **`CT_N1_RESULT=12`**.

## Two recording modes

### Standalone (no MCR context) — the corpus regression (`verify_n1.py standalone`)

Recorded with NO `CT_MCR_*` set and not under `ct-mcr`. The join-key emission is
**inert**: the program records its steps normally but emits **ZERO** join events.
The trace is byte-identical to a pre-N1 recording. This is what the GT1 corpus
row asserts (steps > 0 and join count == 0).

### Under a controlled MCR context (`verify_n1.py verify` / `record-and-verify-n1.sh`)

Recorded with `CT_MCR_GEID=1000 CT_MCR_TICK=500000` (the shim standing in for the
`ct-mcr` live-GEID interface). The recorder tags call-entry/exit and native-call
boundaries with `(GEID, tick)` join keys, emitted as `events.dat` special events
whose content is `ct-nested-join:gdscript geid=… tick=… step=… site=… thread=…`
(wire contract: `codetracer-trace-format-spec/nested-trace-correlation.md`).

Expected join **sites** (order = emission order; geids monotonic non-decreasing):

- `_init` runs `var x = 10` (first step), then `helper(x)`:
  - **call-enter** join for `helper` (a step already exists, so it is emitted;
    `_init`'s own call-enter is before the first step and is correctly skipped),
  - three **native-call** joins inside `helper` (`a.append(n)`, `a.append(n+1)`,
    `a.size()`),
  - **call-exit** join for `helper`,
  - **call-exit** join for `_init`.
- So: ≥1 join at EACH of the three sites (`call-enter`, `call-exit`,
  `native-call`), every join well-formed (integer geid/tick/step, valid site, a
  real step index), and geid monotonic non-decreasing.

## Resolution (against a SYNTHETIC native trace)

The synthetic parent native trace is a controlled `geid.idx` stand-in over
`[base_geid, base_geid + 4096)` (built independently in `verify_n1.py` from the
known base). Per the correlation record §3:

- **nested→native**: every join's `geid` resolves into that native index.
- **native→nested**: a native geid resolves to the join with the greatest
  `geid ≤ g'` (exact hit for a recorded coordinate; `≤` rule between coordinates).

## Non-vacuity (tamper — each MUST be rejected)

- `geid`: a join geid outside the native index (`base + 1e9`) is **unresolvable**.
- `step`: a join claiming a step past the trace is **ill-formed**.

## Honest scope

The engine + `.ct` + `ct-print` are real (no mocks). The **native trace** is the
sole synthetic fixture — it stands in for the N2 MCR constellation (a full MCR
run of the patched Godot needs the Linux substrate and a real `ct-mcr`
recording). The `ct_mcr_now` live-GEID interface exists today
(`codetracer-native-recorder` `ct_interpose/.../trace_context.nim`); N1 drives
the same emission path through the `CT_MCR_GEID/CT_MCR_TICK` shim so the mechanism
is real and testable standalone.
