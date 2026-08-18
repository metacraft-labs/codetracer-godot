# EXPECTED — GF8 (Properties get/set & Annotations @export/@onready, MEMBER-write capture)

First-principles derivation of the facts asserted by `scripts/verify_gf8.py`
against the `.ct` produced by recording `test-programs/gdscript/gf_props.gd` with
the patched engine and decoding it via `ct-print --full`. Written from the source
+ the codegen/VM before trusting any run; the verifier duplicates these as
literals so the assertion is not circular.

## What GF8 changes (the closed gap)

Before GF8 the value recorder captured only **stack-slot** writes
(`ADDR_TYPE_STACK`): local variables and (indirectly) arguments. Writes into a
class **member** slot (`ADDR_TYPE_MEMBER`) or a **static var** slot were decoded
by `gdscript_ct_trace_assign` and then *skipped*. That deferral is exactly why
G4/GF1/GF3/GF7 could not capture `counter`, `species`, `Tag.v`, `Kennel.count`.

GF8 closes it in `modules/gdscript/`:

- **`ADDR_TYPE_MEMBER` assigns** (the common `member = expr` / `self.member =
  expr`, plus member initializers, `@export` defaults and `@onready`
  assignments — the compiler emits all of these as an `OPCODE_ASSIGN*` into a
  member address, see `gdscript_compiler.cpp`). `gdscript_ct_trace_assign` now
  resolves the 24-bit member INDEX to the declared NAME via
  `GDScript::debug_get_member_by_index` (which inverts `member_indices`) and
  emits the value on the current step, exactly like a stack local.
- **`OPCODE_SET_STATIC_VARIABLE`** (`static var` writes): name resolved via
  `GDScript::debug_get_static_var_by_index`.
- **`OPCODE_SET_MEMBER`** (self native/registered property write) and
  **`OPCODE_SET_NAMED`** (in-place named write on a base Variant): the opcode
  already holds the member `StringName`, passed straight through.
- **`OPCODE_SET_NAMED_VALIDATED`** (GF8 follow-up — the previously silently
  dropped case): the typed-base variant of `SET_NAMED`, emitted when the base's
  *static* type has a validated setter for the member (e.g.
  `var vt: Vector2; vt.y = 8`). Unlike `SET_NAMED` this opcode carries only a
  validated setter pointer + its index — **no `StringName` operand** — so the
  original GF8 hook set could not reach it and the member write was dropped. The
  name is recovered from the DEBUG-only `setter_names` table the codegen
  populates in parallel with the setters vector
  (`write_set_named` → `add_debug_name(setter_names, get_setter_pos(setter),
  p_name)`), indexed by the same `index_setter` operand the handler already
  reads: `gdscript_ct_trace_member_assign(setter_names[index_setter], *value)`.
  The written field value is `*value` (the source Variant), mirroring
  `SET_NAMED`.

### The full `OPCODE_SET*` survey

| opcode | writes a named member? | status |
| --- | --- | --- |
| `OPCODE_SET_NAMED` | yes (untyped base, `.x = …`) | hooked in GF8 |
| `OPCODE_SET_NAMED_VALIDATED` | yes (typed base, `.y = …`) | **newly hooked (this follow-up)** |
| `OPCODE_SET_MEMBER` | yes (native self property) | hooked in GF8 |
| `OPCODE_SET_STATIC_VARIABLE` | yes (`static var`) | hooked in GF8 |
| `OPCODE_SET_KEYED` / `OPCODE_SET_KEYED_VALIDATED` | no — `dict[k] = v` | out of scope (not a named member) |
| `OPCODE_SET_INDEXED_VALIDATED` | no — `arr[i] = v` | out of scope (not a named member) |

Index/keyed sets (`arr[i]=x`, `dict[k]=v`) are deliberately NOT hooked: their
"name" is a runtime key/index, not a statically-known member name, so they do
not belong to the member-write coverage. Capturing element writes into container
locals is a separate future item.

All go through the SAME recursive `ct_value_*` encoder and
`trace_writer_register_variable_cbor`, so member values stay parallel-indexed to
steps and lazy type interning is preserved.

## The program

`gf_props.gd` is a `SceneTree` (so it owns a `root` Window and can add a Node
child, the only way to drive `@onready`). It defines an inner class
`Gadget extends Node`:

| Construct | Write kind | Captured as |
| --- | --- | --- |
| `@export var hp: int = 100` | member initializer (ASSIGN→MEMBER) in `@implicit_new` | `hp` = 100 (Int) |
| `@export_range(0,10) var level: int = 3` | member initializer | `level` = 3 (Int) |
| `var _t: float = 0.0` | member initializer | `_t` = 0.0 (Float) |
| `static var total := 0` | `OPCODE_SET_STATIC_VARIABLE` in `@static_initializer` | `total` = 0 (Int) |
| `x = 5` in `_init` | plain member ASSIGN→MEMBER | `x` = 5 (Int) |
| `total = total + 7` in `_init` | `OPCODE_SET_STATIC_VARIABLE` | `total` = 7 (Int) |
| `@onready var ready_mark := 42` | ASSIGN→MEMBER in `@implicit_ready` | `ready_mark` = 42 (Int) |
| property `temp` set: `var got := v; _t = clamp(got,0,100)` | stack `got` + member `_t` | `got` = 150.0, `_t` = 100.0 (Float) |
| property `temp` get: `return _t` | return value (GF5) | getter frame returns 100.0 (Float) |
| `uv.x = 9.0` in `member_ops` (untyped base) | `OPCODE_SET_NAMED` | `x` = 9.0 (Float) |
| `tv.y = 8.0` in `member_ops` (typed base) | `OPCODE_SET_NAMED_VALIDATED` | `y` = 8.0 (Float) |
| `name = "gadget1"` in `member_ops` (native prop) | `OPCODE_SET_MEMBER` | `name` = "gadget1" (String) |

### member_ops() — the three named-member-write opcodes, exercised directly

The original GF8 fixture drove `ADDR_TYPE_MEMBER` / `SET_STATIC_VARIABLE` writes,
but `SET_MEMBER` / `SET_NAMED` / `SET_NAMED_VALIDATED` were only *manually*
verified by the reviewer — none was locked into the committed test, and
`SET_NAMED_VALIDATED` was in fact **dropped**. `Gadget.member_ops()` now drives
all three deterministically:

- `var uv = Vector2(1.0, 2.0)` — an **untyped** local base, so `uv.x = 9.0`
  compiles to `OPCODE_SET_NAMED` (name operand `"x"`).
- `var tv: Vector2 = Vector2(3.0, 4.0)` — a **typed** local base, so `tv.y = 8.0`
  compiles to `OPCODE_SET_NAMED_VALIDATED` (validated setter for `Vector2.y`),
  the case that was previously silently dropped.
- `name = "gadget1"` — resolves to the native `Node.name` property, compiling to
  `OPCODE_SET_MEMBER`.

`uv`/`tv` are themselves captured as `Struct("Vector2")` stack locals, which is
why the types table now gains `Vector2` (see below).

### Property get/set are FRAMES

`temp` has an inline setter/getter, so the compiler names them
`@temp_setter` / `@temp_getter` (`gdscript_compiler.cpp`: inline accessors are
`"@" + name + "_setter"/"_getter"`). Assigning `temp = 150.0` compiles to a CALL
to `@temp_setter`; reading `temp` compiles to a CALL to `@temp_getter`. Both run
as ordinary `GDScriptFunction::call` frames, so the existing G3/GF5 hooks record
them as call/return with the getter's `return _t` captured as the return value.

- `@temp_setter`: depth 2, parent = the `run` frame, return **None** (void).
  Its body writes the backing member `_t = 100.0` (clamp of the incoming 150.0).
- `@temp_getter`: depth 2, parent = the `run` frame, return **Float 100.0**.

GDScript parameters are not captured directly (the same `call_entry.args` is
empty seam GF5/GF7 documented), so the setter body copies `v` into the named
local `got` to prove it ran with `v == 150.0`.

### @onready ordering (honest note — captured, not read)

Node readiness is DEFERRED: `root.add_child(g)` schedules `_ready`, which the
SceneTree runs only AFTER `_initialize()` returns. Observed: the
`@implicit_ready` frame (where `ready_mark = 42` is assigned) lands after the
`run` frame. So `run()` sees `ready_mark == 0` and the checksum does not read it
— but GF8 still **captures** `ready_mark == 42` on the `@implicit_ready` frame,
which is the `@onready` deliverable. Driving `_ready` before the compute would
need an extra idle frame; the capture (what GF8 asserts) is present regardless.
This is NOT a deferral — `@onready` capture is proven; only its ordering vs the
user compute is scene-driven.

## Deterministic checksum

```
temp = 150.0  -> @temp_setter clamps _t to 100.0
read_back      = temp (@temp_getter) = 100.0
sum = int(100.0) + hp(100) + x(5) + level(3) = 208     (ready_mark not yet set)
Gadget.total = 0 + 7 = 7
```

stdout: `CT_GF8_RESULT=208` and `CT_GF8_TOTAL=7`.

## Asserted facts (verify_gf8.py literals)

Types table (Object appears because the `g := Gadget.new()` stack local is an
Object — a Node — captured via the GF4 shallow-Object encoder; `Vector2` appears
because `member_ops`'s `uv`/`tv` base locals are captured as `Struct("Vector2")`;
the member captures themselves are all scalars):

```
[None, Int, Float, Bool, String, Variant, Object, Vector2]
```

Value captures — each on the step whose (function, line) match, proving
values.dat stays parallel-indexed to steps.dat:

| function | line | var | kind | value |
| --- | --- | --- | --- | --- |
| `@implicit_new` | 58 | `hp` | Int | 100 |
| `@implicit_new` | 59 | `level` | Int | 3 |
| `@implicit_new` | 62 | `_t` | Float | 0.0 |
| `_init` | 74 | `x` | Int | 5 |
| `_init` | 75 | `total` | Int | 7 |
| `@temp_setter` | 70 | `got` | Float | 150.0 |
| `@temp_setter` | 71 | `_t` | Float | 100.0 |
| `@implicit_ready` | 64 | `ready_mark` | Int | 42 |
| `run` | 80 | `read_back` | Float | 100.0 |
| `member_ops` | 98 | `x` | Float | 9.0 | (`OPCODE_SET_NAMED`) |
| `member_ops` | 100 | `y` | Float | 8.0 | (`OPCODE_SET_NAMED_VALIDATED`) |
| `member_ops` | 101 | `name` | String | "gadget1" | (`OPCODE_SET_MEMBER`) |

(`read_back` lands on line 80, not 79: the getter CALL on line 79 defers the
read_back ASSIGN to the next emitted source line — the known G4 parallel-index
detail that a write opcode runs after its line's `OPCODE_LINE`. The three
`member_ops` writes are plain in-place named writes with no intervening call, so
each lands on its own source line.)

Additionally: `total == 0` is captured on the `@static_initializer` frame (the
static-var initializer), proving the static-var path, and `total == 7` is the
later mutation.

Frames:

- exactly one `@temp_setter` call, depth 2, parent = the `run` frame,
  return value kind None; its body carries the `_t = 100.0` backing write.
- exactly one `@temp_getter` call, depth 2, parent = the `run` frame,
  return value Float 100.0.
- call/return balanced (`len(call_entry) == len(call_exit)`).

## Non-vacuity (tamper runs — each MUST be rejected)

- `value` — flip the captured `hp` from 100 to 999.
- `membername` — rename the captured `x` to `notx` so the plain-member assertion
  misses.
- `missingsetter` — drop the `@temp_setter` call_entry/exit so the setter-frame
  assertion fails.
- `validatedvalue` — flip the captured `y` (line 100) from 8.0 to 999.0 on the
  **newly-covered `OPCODE_SET_NAMED_VALIDATED`** write, proving the typed-member
  write is actually asserted (not vacuously accepted).

## Regression — prior member-absence expectations this closes

GF8 captures members that G4/GF1/GF7 had asserted ABSENT. Two prior verifiers'
absence expectations are now outdated and are updated minimally (the member is
now correctly captured; every other prior assertion is untouched):

- `verify_gf1.py`: `counter` (`static var counter := 0`; `counter += 1`) is now
  captured — `[0 (@static_initializer), 0 (@implicit_new), 1 (_init)]`. The
  `DEFERRED_ABSENT` check is replaced by a presence assertion. GF1's typed/
  operator/enum captures and its 6-entry types table are unchanged.
- `verify_gf7.py`: `species = got_name` (`ADDR_TYPE_MEMBER`) is now captured —
  `['rex','spot']` on `Animal._init`. `Tag.v` and `Kennel.count` are only
  DECLARED, never written, so they remain absent. The absence set drops
  `species` (now asserted PRESENT) and keeps `v`, `count`.

No other test program writes a member or static var (verified by grep), so
G2/G3/G4/GF2/GF3/GF4/GF5/GF6 traces are byte-identical: the stack-slot captures,
the step/call streams, and the scalar-only 6-entry types table are unchanged.
