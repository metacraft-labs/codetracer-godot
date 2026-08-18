# GF4 expected facts — hand-derived from source (first-principles, pre-recording)

These facts are derived by reading the reference program
(`test-programs/gdscript/gf_variant_types.gd`) plus the GF4 encoder contract
(`modules/gdscript/gdscript_ct_trace.cpp`), NOT by reading recorder output.
`scripts/verify_gf4.py` asserts them against the real `.ct` produced by the
patched engine (decoded with `ct-print --full`). If the recorder and these notes
ever disagree, one of them is wrong — do not regenerate this file from recorder
output.

## What the C-ABI writer supports for structs (the load-bearing constraint)

The header `modules/gdscript/ct_writer/include/codetracer_trace_writer.h` and its
Nim implementation (`codetracer-trace-format-nim`) expose:

- `ct_value_begin_struct(encoder, type_id, field_count)` — writes a `Struct`
  value as `{kind:"Struct", field_values:[...], type_id}`. It carries **only
  positional field_values**; the streaming encoder has **no** call to attach
  literal field names (the `field_names` CBOR schema exists in the format but is
  only reachable from the non-streaming in-memory `encodeCborValueRecord`, not
  from the C-ABI streaming encoder).
- `trace_writer_ensure_type_id(handle, kind, lang_type)` — registers a type by
  `kind` + a single lang-type **name**. It stores `TypeSpecificInfo=None`; there
  is **no** per-field-name registration for a `Struct` type either.
- `trace_writer_register_variable_cbor` stores the variable with `type_id = 0`
  (the real type lives inside the CBOR bytes), so the per-var `type_name`
  ct-print prints is `None`. The authoritative type identity is the **value
  node's own `type_id`**, resolved against the root `types` table.

**Decision (documented honestly):** GF4 encodes each Godot math/struct type as a
real `Struct` value (the kind the db-backend's `ValueRecord::Struct{field_values}`
consumes), with **one registered `Struct` type per Godot type** (`"Vector3"`,
`"Color"`, `"Transform3D"`, ...). The type NAME distinguishes it and is
verifiable via the value node's `type_id` + the root `types` table. Field NAMES
are conveyed **positionally**, in each Godot type's canonical field order
(documented in the table below), because the C-ABI cannot attach literal field
names without extending `libcodetracer_trace_writer.a` — which is out of scope
for a change confined to `modules/gdscript/`. Emitting literal per-field names is
a clean follow-up (a `ct_value_begin_struct_named` C-ABI + a Struct-type
field-name registration) but is not required for faithful, verifiable capture of
the field values.

## Shallow-Object decision (honest)

`OBJECT`/`RefCounted` is a **faithful shallow** representation: a `Struct`
`"Object"` with two fields — `class` (`String`, from `get_class()`) and `id`
(`Int`, from `get_instance_id()`). There is **no property walk**, so the encoder
emits two scalars and stops: a deep/cyclic object graph cannot recurse (no
visited-guard is needed because there is no recursion into object members at
all). The instance `id` is process-nondeterministic, so the verifier asserts the
`class` name and that `id` is an `Int`, but not its value.

## Handle-type mappings

- `STRING_NAME` (`&"foo"`) -> `String "foo"` (unchanged from G4; grouped with STRING).
- `NODE_PATH` (`^"a/b"`) -> `String "a/b"` (its `String()` form).
- `RID` -> `Struct "RID" {id:Int}` (`get_id()`; `RID()` has id 0, deterministic).
- `CALLABLE` -> `Struct "Callable" {method:String}` (`get_method()`; the
  nondeterministic bound object id is deliberately omitted).
- `SIGNAL` -> `Struct "Signal" {name:String}` (`get_name()`).
- `NIL` (`null`) -> `None`.

## Reader choice

Verification reads the trace through `ct-print --full` — the canonical Nim CTFS
decoder, the same `TraceReader` decode path `MaterializedReplaySession` uses to
present locals-at-a-step. A `Struct` value decodes to
`{"kind":"Struct","field_values":[...],"type_id":n}`; a `Sequence` to
`{"kind":"Sequence","elements":[...],"is_slice":bool,"type_id":n}`; scalars as in
GF3. The value node's `type_id` indexes the root `types` array to recover the
registered type name.

## Expected captured locals (line, name -> structure, field order)

| Line | Local     | Expected structured value (canonical field order) |
| ---- | --------- | ------------------------------------------------- |
| 28   | `v2`      | Struct `Vector2` {x=1.5, y=2.5} (Float) |
| 29   | `v2i`     | Struct `Vector2i` {x=3, y=4} (Int) |
| 30   | `v3`      | Struct `Vector3` {x=1.5, y=2.5, z=3.5} (Float) |
| 31   | `v3i`     | Struct `Vector3i` {x=5, y=6, z=7} (Int) |
| 32   | `v4`      | Struct `Vector4` {x=1, y=2, z=3, w=4} (Float) |
| 33   | `v4i`     | Struct `Vector4i` {x=8, y=9, z=10, w=11} (Int) |
| 35   | `r2`      | Struct `Rect2` {position=Vector2{1,2}, size=Vector2{3,4}} |
| 36   | `r2i`     | Struct `Rect2i` {position=Vector2i{5,6}, size=Vector2i{7,8}} |
| 37   | `col`     | Struct `Color` {r=0.1, g=0.2, b=0.3, a=1.0} (Float, ~float32) |
| 38   | `pl`      | Struct `Plane` {normal=Vector3{0,1,0}, d=5.0} |
| 39   | `q`       | Struct `Quaternion` {x=0, y=0, z=0, w=1} |
| 40   | `ab`      | Struct `AABB` {position=Vector3{1,2,3}, size=Vector3{4,5,6}} |
| 41   | `bs`      | Struct `Basis` {x=Vector3{2,0,0}, y=Vector3{0,3,0}, z=Vector3{0,0,4}} (rows; diagonal so rows==columns) |
| 42   | `t2`      | Struct `Transform2D` {x=Vector2{1,0}, y=Vector2{0,1}, origin=Vector2{9,10}} |
| 43   | `t3`      | Struct `Transform3D` {basis=Basis{{1,0,0},{0,1,0},{0,0,1}}, origin=Vector3{7,8,9}} |
| 44   | `proj`    | Struct `Projection` {x=Vector4{1,0,0,0}, y={0,1,0,0}, z={0,0,1,0}, w={0,0,0,1}} |
| 46   | `sname`   | String "foo" (StringName) |
| 47   | `npath`   | String "a/b" (NodePath) |
| 48   | `rid`     | Struct `RID` {id=0} |
| 49   | `callable`| Struct `Callable` {method="my_method"} |
| 50   | `sig`     | Struct `Signal` {name="my_signal"} |
| 51   | `obj`     | Struct `Object` {class="RefCounted", id=<any Int>} |
| 52   | `nil_val` | None |
| 54   | `pv2`     | Sequence[`Array`] [Vector2{1,2}, Vector2{3,4}] |
| 55   | `pv3`     | Sequence[`Array`] [Vector3{1,2,3}] |
| 56   | `pcol`    | Sequence[`Array`] [Color{1,0,0,1}] |

`col`'s r/g/b are stored in Godot as 32-bit floats, so 0.1/0.2/0.3 read back as
~0.10000000149; the verifier's 1e-6 tolerance covers this. All other float
fields (1.5, 2.5, integers-as-float) are exact in float32.

## Lazy type interning (preserved from GF3)

The per-Godot-type `Struct` types (and the `Array`/`Dictionary`/`Pair` collection
types used by the packed struct-arrays) are interned **lazily**, on first
encounter, via `gct_stype` / `gdscript_ct_ensure_collection_types` inside the
encode path. A scalar-only recording never reaches those paths, so its `types`
table stays byte-identical to G4/GF1/GF2 (`[None, Int, Float, Bool, String,
Variant]`) — re-asserted by the G4/GF1/GF2 regressions. In this GF4 trace the
first six entries are still `[None, Int, Float, Bool, String, Variant]` (None at
TypeId 0), followed by the struct/collection types in encounter order; the
verifier asserts the leading six and the presence of each struct type name.

## Deterministic checksum

`checksum` sums integer contributions from the constructed values:

```
  v2i.x+y (7) + v3i.x+y+z (18) + v4i.x+y+z+w (38)
+ int(r2.size.x)+int(r2.size.y) (3+4=7) + r2i.position.x+y (5+6=11)
+ int(pl.d) (5) + int(q.w) (1) + int(ab.size.x) (4) + int(bs.x.x) (2)
+ int(t2.origin.x)+int(t2.origin.y) (9+10=19) + int(t3.origin.z) (9)
+ int(proj.w.w) (1) + String(sname).length() (3) + int(rid.get_id()) (0)
+ pv2.size()+pv3.size()+pcol.size() (2+1+1=4)
= 129
```

The program prints `CT_GF4_RESULT=129`; the runner greps for it as a determinism
guard.

## Non-vacuity (tamper runs, each MUST be rejected)

`scripts/verify_gf4.py tamper <full.json> <mode>`:

- `fieldval` — v3.x Float 1.5 -> 9.9 (wrong field value).
- `kind`     — turn v3 from a Struct into an Int (wrong kind).
- `typename` — rename v3's registered type "Vector3" -> "Bogus" (wrong type name).
- `shape`    — flatten r2.position from a Struct to an Int (wrong nested shape /
  field structure).
