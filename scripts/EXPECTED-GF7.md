# EXPECTED — GF7: Classes, Inheritance, `super`, Inner Classes, preload/load

First-principles facts, hand-derived BEFORE running the recorder, that
`scripts/verify_gf7.py` asserts against the real `.ct` produced by the patched
engine over `test-programs/gdscript/gf_zoo.gd` (which drives the base
`gf_animal.gd` + derived `gf_dog.gd`), decoded via `ct-print --full`.

## No engine change (the load-bearing finding — like GF1/GF2/GF6)

GF7 adds **no recorder code**. A class method, a `super.method()` call, an
`_init` constructor, a `_static_init`, and an inner-class method are all just
`GDScriptFunction`s: each invocation goes through `GDScriptFunction::call`, so
the existing G2 step, G3 call/return, G4/GF3/GF4 value, and GF5 return-value
hooks fire unchanged. The recorder binary is byte-identical to GF6 (the
G2/G3/G4/GF1..GF6 streams re-verify green). GF7 is a COVERAGE milestone.

## How a frame's SOURCE PATH is asserted

`ct-print --full` does NOT emit a path on `call_entry` events — only `step`
events carry `path`. The call hook fires at `enter_function`, right *before* the
frame's first `OPCODE_LINE`, so a frame's `entry_step` is the index of its first
step, and **that step's `path` is the frame's source file**. The verifier
resolves each frame's source by `steppath[entry_step]`. This is what lets GF7
assert cross-FILE facts: a derived frame resolves to `res://gf_dog.gd` and the
base frame it reaches via `super` resolves to `res://gf_animal.gd`.

## How labels are emitted (what is faithfully recorded)

- A method frame's `function` is the **bare method name** — `speak`, `_init`,
  `label`, `size` — never a qualified `Dog.speak` / `Tag.label`. The class /
  file is conveyed by the **source path** (from `entry_step`) and the
  depth/parent nesting, not by the name.
- A `super.method()` override and the base method it reaches therefore share the
  SAME `function` label (both `speak`); they are distinguished by source path
  (derived `gf_dog.gd` vs base `gf_animal.gd`) and by depth (override at depth 1,
  base at depth 2 parented to the override).
- An inner-class method (`Tag.label`, `Kennel.size`) records as a plain frame
  named by its method (`label`, `size`), with source = the FILE that contains the
  inner class (`gf_animal.gd` / `gf_dog.gd`).
- `_static_init` records as a `_static_init` frame nested under an
  engine-synthesized `@static_initializer` frame.
- Constructor / method ARGS: `call_entry.args` is EMPTY for GDScript (the call
  hook passes only name/source/line). An arg is observed by the body copying it
  into a named local (the GF5/GF6 seam): `Animal._init` copies `n -> got_name`,
  `Dog._init` copies `n -> pup`.

## The recorded call tree (deterministic; entry order = call_key order)

```
@static_initializer            depth 0            source gf_dog.gd
  _static_init                 depth 1  parent ^  source gf_dog.gd   (marker=7)
@implicit_new                  depth 0            source gf_dog.gd
_init  (gf_zoo driver)         depth 0  parent -1 source gf_zoo.gd    <- MAIN
  @implicit_new x2             depth 1            source gf_animal.gd
  _init  (Dog._init, preload)  depth 1  parent MAIN source gf_dog.gd   (pup="rex")
    _init (Animal._init/super) depth 2  parent ^   source gf_animal.gd (got_name="rex")
  speak  (Dog.speak, preload)  depth 1  parent MAIN source gf_dog.gd   -> "...woof"
    speak (Animal.speak/super) depth 2  parent ^   source gf_animal.gd -> "..."
  @implicit_new x2             depth 1            source gf_animal.gd
  _init  (Dog._init, load)     depth 1  parent MAIN source gf_dog.gd   (pup="spot")
    _init (Animal._init/super) depth 2  parent ^   source gf_animal.gd (got_name="spot")
  speak  (Dog.speak, load)     depth 1  parent MAIN source gf_dog.gd   -> "...woof"
    speak (Animal.speak/super) depth 2  parent ^   source gf_animal.gd -> "..."
  @implicit_new (Tag)          depth 1            source gf_animal.gd
  label  (Tag.label, inner)    depth 1  parent MAIN source gf_animal.gd -> "tag"
  @implicit_new (Kennel)       depth 1            source gf_dog.gd
  size   (Kennel.size, inner)  depth 1  parent MAIN source gf_dog.gd    -> 3
_process                       depth 0  parent -1 source gf_zoo.gd     -> true
```

### Asserted facts

- **Files present**: every one of `res://gf_animal.gd`, `res://gf_dog.gd`,
  `res://gf_zoo.gd` is the source of at least one frame (cross-FILE trace).
- **super (cross-file), speak**: exactly 4 `speak` frames — 2 derived
  (source `gf_dog.gd`, depth 1, parent = MAIN, return `"...woof"`) and 2 base
  (source `gf_animal.gd`, depth 2, each parented to a distinct derived `speak`,
  return `"..."`). This is `Dog.speak -> super -> Animal.speak` across files,
  proven twice (preload + load paths).
- **_init ctor chain (cross-file)**: 2 derived `_init` (source `gf_dog.gd`,
  depth 1, parent MAIN) each with a child base `_init` (source `gf_animal.gd`,
  depth 2) — `Dog._init -> super._init -> Animal._init` across files.
- **ctor arg captured**: `got_name = ["rex","spot"]` (String) in the BASE frame
  (the arg flows through `super._init` cross-file) and `pup = ["rex","spot"]`
  (String) in the DERIVED frame — the SAME values, proving arg propagation.
- **super return value flows back**: `sound = ["...","..."]` (base body),
  `base_sound = ["...","..."]` (derived reads super's return), `out =
  ["...woof","...woof"]`.
- **inner classes**: one `label` frame (depth 1, source `gf_animal.gd`, return
  `"tag"`, capture `made="tag"`) and one `size` frame (depth 1, source
  `gf_dog.gd`, return `3` Int, capture `s=3`).
- **_static_init**: one `_static_init` frame, source `gf_dog.gd`, parent frame
  is `@static_initializer`; capture `marker=7`.
- **types table**: `[None, Int, Float, Bool, String, Variant, Object]` (`Object`
  is the shallow GF4 encoding of the captured Dog/Tag/Kennel instance locals
  `d1`/`d3`/`t`/`k` in the driver).
- **balance**: `#call_entry == #call_exit`.
- **checksum**: `CT_GF7_RESULT=20` (7+7+3+3).

## @abstract / _static_init availability (honest)

Both ARE available in this Godot `4.6.2-stable` build (`@abstract` is a
registered SCRIPT|CLASS|FUNCTION annotation in `gdscript_parser.cpp`;
`_static_init` / `static_initializer` exists in `gdscript.cpp`). So this
milestone EXERCISES both — `Animal` is `@abstract` (never instantiated directly;
its methods are reached only through the concrete `Dog` via `super`), and
`gf_dog.gd` has a `_static_init` whose frame is recorded. Neither is N/A.

## Member/property values → GF8 (deferred)

Instance/static MEMBER writes (`species = ...`, `Tag.v`, `Kennel.count`,
`static var`) are `ADDR_TYPE_MEMBER` writes, which `gdscript_ct_trace_assign`
intentionally skips (milestone GF8). GF7 asserts the CALL/RETURN frames, args
(via read-into-local), returns, and per-frame source paths — NOT member values.
The verifier asserts `species`/`v`/`count` are ABSENT as captured named locals.

## Non-vacuity (tamper runs, each MUST be rejected)

- `srcpath`   — rewrite a base `speak` frame's source to `gf_dog.gd` (breaks the
  "base frame source = gf_animal.gd" cross-file assertion).
- `missingsuper` — delete a depth-2 base `speak` frame (breaks the 4-`speak` /
  2-base-`speak` super-nesting count).
- `ctorarg`   — corrupt the first `got_name` capture `"rex" -> "wrong"`.
- `nesting`   — reparent a base `speak` up to MAIN `_init` (breaks super nesting).
