# GF1 expected facts — hand-derived from source (first-principles, pre-recording)

These facts are derived by reading the reference program and the GDScript
codegen, NOT by reading recorder output. `scripts/verify_gf1.py` asserts them
against the real `.ct` produced by the patched engine (decoded with
`ct-print --full`). If the recorder and these notes ever disagree, one of them
is wrong — do not regenerate this file from recorder output.

## Reader choice

Same as G4: verification reads the trace through `ct-print --full`, the
canonical Nim CTFS decoder from `codetracer-trace-format-nim`, whose per-step
`vars[]` (`{varname, type_id, type_name, value}`) is the SAME `TraceReader`
decode path `MaterializedReplaySession` (`codetracer/src/db-backend`) uses to
present locals-at-a-step. Asserting on `ct-print --full`'s `vars[]` therefore
tests the exact consumer view of the value stream.

## Why GF1 needs no new VM hook (the load-bearing derivation)

GF1's constructs (typed/inferred/untyped locals; all operator classes incl.
`**`, bitwise, comparison, logical, ternary; `is`/`as`/`in`; `const`; named &
unnamed `enum`) are each written to a NAMED local via `var name = <expr>`.

Reading `modules/gdscript/gdscript_compiler.cpp` +
`modules/gdscript/gdscript_byte_codegen.cpp`:

- A local variable declaration `var name = <expr>`
  (`GDScriptCompiler::_parse_expression` + the `Node::VARIABLE` case in
  `_parse_block`, ~gdscript_compiler.cpp:2213) compiles the initializer to an
  address `src_address`, then emits
  `gen->write_assign[_with_conversion](local, src_address)`.
- `write_assign` / `write_assign_with_conversion`
  (gdscript_byte_codegen.cpp:908/968) ALWAYS emit an `OPCODE_ASSIGN` or an
  `OPCODE_ASSIGN_TYPED_*` into the named local `local`.
- Every operator / cast / type-test / ternary / logical sub-result is computed
  into a FRESH COMPILER TEMPORARY (`codegen.add_temporary()`), e.g.
  `gen->write_cast(result, …)` (compiler.cpp:592),
  `gen->write_type_test(result, …)` (:971),
  `gen->write_binary_operator(result, …)` (:907), the ternary/and/or targets,
  etc. There is NO codegen path that writes an `is`/`as`/`**`/bitwise/ternary
  result DIRECTLY into a named local: the value always lands in a temporary and
  is then copied into the named slot by the following `write_assign`.
- A fully-constant initializer (`const K`, `Dir.DOWN`, an unnamed-enum member)
  is folded to `codegen.add_constant(...)` by `_parse_expression`
  (compiler.cpp:255), then likewise `write_assign(local, const)`.

Consequently the named local ALWAYS receives its value through an
`OPCODE_ASSIGN*` opcode, and those assign opcodes are ALREADY HOOKED by G4
(`CT_TRACE_ASSIGN` after `OPCODE_ASSIGN`, `OPCODE_ASSIGN_NULL/TRUE/FALSE`, and
the five `OPCODE_ASSIGN_TYPED_*`). The G4 `OPCODE_OPERATOR` /
`OPCODE_OPERATOR_VALIDATED` hooks fire too, but only ever see temporaries
(skipped as unnamed). **GF1 therefore adds NO new VM hook** — it is a coverage
milestone proving the existing capture pipeline is correct across the full
operator/typing/const/enum surface. This is confirmed empirically: the G4-era
binary captures every row below.

The `var name = <expr>` result is captured on the step at that source line
because the OPCODE_LINE step for the line fires first, then the assign opcode
writes the slot before the next line — the values.dat ↔ steps.dat parallel
index.

Runtime "seed" locals (`s7=7, s2=2, s12=12, s10=10, s1=1, sf7=7.0`) feed the
operators so the operator opcodes actually EXECUTE at runtime; a fully-literal
`2 ** 10` would be constant-folded at compile time. The seeds are themselves
captured (Int/Float) and are asserted too.

## `test-programs/gdscript/gf_typing.gd` — captured values (GF1)

All in `_init`. Values hand-derived (bit patterns spelled out):

```
line  varname   value.kind  value   derivation
----  --------  ----------  ------  ----------------------------------------
37    a         Int         5       typed  `var a: int = 5`
38    b         Float       2.5     inferred `var b := 2.5`
39    c         String      "s"     untyped `var c = "s"`
42    s7        Int         7       seed
43    s2        Int         2       seed
44    s12       Int         12      seed  (0b1100)
45    s10       Int         10      seed  (0b1010)
46    s1        Int         1       seed
47    sf7       Float       7.0     seed
50    pw        Int         1024    power        2 ** 10
51    idiv      Int         3       int division 7 / 2  (both Int -> Int)
52    fdiv      Float       3.5     float division 7.0 / 2
53    md        Int         1       modulo       7 % 2
56    band      Int         8       bitwise and  0b1100 & 0b1010 = 0b1000
57    bor       Int         14      bitwise or   0b1100 | 0b1010 = 0b1110
58    bxor      Int         6       bitwise xor  0b1100 ^ 0b1010 = 0b0110
59    bnot      Int         -13     bitwise not  ~12 = -(12+1) = -13
60    shl       Int         16      left shift   1 << 4
61    shr       Int         3       right shift  12 >> 2
64    cmp       Bool        true    comparison   7 > 2
67    land      Bool        true    logical and  (7>2) and (2>1)
68    lor       Bool        true    logical or   (7<2) or (2>1)
69    lnot      Bool        true    logical not  not (7<2)
72    tern      Int         100     ternary      100 if 7>2 else 200
75    ris       Bool        true    is           7 is int
76    ras       Float       7.0     as           7 as float
77    rin       Bool        true    in           2 in [1,2,3]
80    d         Int         42      const read   K == 42
83    e         Int         5       named enum   Dir.DOWN (UP=0, DOWN=5, LEFT=6)
84    f         Int         1       unnamed enum B (A=0, B=1)
```

Deterministic checksum (Int results, floats via `int(...)`):
`pw+idiv+md+band+bor+bxor+bnot+shl+shr+tern+d+e+f + int(b)+int(fdiv)+int(ras)`
`= 1024+3+1+8+14+6-13+16+3+100+42+5+1 + 2+3+7 = 1222`, so stdout contains
`CT_GF1_RESULT=1222`.

## Honest boundary — `static var` deferred to GF8

The program declares `static var counter := 0` and does `counter += 1`. A
`static var` write is a class/instance MEMBER write (compiled via
`write_set_static_variable`, not a stack `OPCODE_ASSIGN`), so its slot address
is `ADDR_TYPE_MEMBER`, which `gdscript_ct_trace_assign` intentionally skips
(member/property writes are milestone GF8). Therefore `counter` is NOT captured
and is NOT asserted by GF1 — it is present only to confirm a static-var write
does not break the trace. This matches the GF1 PROPERTIES (operators/typing/
const/enums); static var belongs to the member-writes milestone GF8.

## Parallel-index (values ↔ steps) integrity

Every row above must be attached to the step whose `line` (in `_init`) matches
— not merely "present somewhere". `verify_gf1.py` asserts each
`(line, varname, kind, value)` tuple on the correct step and fails if a value
is missing, on the wrong step, under the wrong name, or with the wrong
value/kind. Three tamper runs (wrong value / wrong type-kind / wrong step)
prove the verifier rejects each corruption (exit nonzero), so the assertion is
not vacuous.

## Types table

Unchanged from G4 — None FIRST at `TypeId(0)` (the CTFS `NONE_TYPE_ID`
invariant), then Int, Float, Bool, String, and a `Variant` (Raw) fallback:

```
None, Int, Float, Bool, String, Variant
```

## Regression (must be unchanged — GF1 adds no recorder code)

Because GF1 adds NO new VM hook, the G2/G3/G4 step/call/value streams are
byte-identical:

- G2 (`g2probe.gd`): `CT_G2_STEPS=30`.
- G3 (`gf_calls.gd`): `_init → outer → inner` nesting, `sibling` after `outer`,
  6 balanced call/return pairs; `CT_G3_RESULT=107`.
- G4 (`gf_values.gd`): 8 captured values on their own steps; `CT_G4_RESULT=47`.
