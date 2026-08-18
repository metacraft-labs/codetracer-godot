# GF3 expected facts — hand-derived from source (first-principles, pre-recording)

These facts are derived by reading the reference program
(`test-programs/gdscript/gf_collections.gd`) plus the GF3 encoder contract
(`modules/gdscript/gdscript_ct_trace.cpp`), NOT by reading recorder output.
`scripts/verify_gf3.py` asserts them against the real `.ct` produced by the
patched engine (decoded with `ct-print --full`). If the recorder and these notes
ever disagree, one of them is wrong — do not regenerate this file from recorder
output.

## Reader choice

Verification reads the trace through `ct-print --full` — the canonical Nim CTFS
decoder from `codetracer-trace-format-nim`, the same `TraceReader` decode path
`MaterializedReplaySession` (`codetracer/src/db-backend`) uses to present
steps + locals-at-a-step. Each `step` event carries `function`, `line`, and a
`vars[]` array of decoded `{varname, value}`. A compound value decodes to:

- Sequence: `{"kind":"Sequence","elements":[...],"is_slice":bool,"type_id":n}`
- Tuple:    `{"kind":"Tuple","elements":[...],"type_id":n}`
- scalars:  `{"kind":"Int","i":n}` / `{"kind":"Float","f":x}` /
  `{"kind":"String","text":s}` / `{"kind":"Bool","b":..}` / `{"kind":"None"}`

## The encoding contract (GF3)

The recorder's `Variant` -> `ct_value_*` switch encodes, RECURSIVELY through the
same scalar path (element types here are G4 scalars — Int/Float/String):

- `ARRAY` (untyped) and typed `Array[T]`               -> `Sequence` of elements.
- `PACKED_{BYTE,INT32,INT64}_ARRAY`                    -> `Sequence` of `Int`.
- `PACKED_{FLOAT32,FLOAT64}_ARRAY`                     -> `Sequence` of `Float`.
- `PACKED_STRING_ARRAY`                                -> `Sequence` of `String`.
- `DICTIONARY` (untyped) and typed `Dictionary[K,V]`   -> `Sequence` of
  key/value `Tuple`s (`Tuple[key, value]` per entry — the Python/Ruby dict
  pattern, mirroring the Ruby recorder's Hash -> Seq-of-Pair encoding).

Nesting is encoded recursively with a depth cap (8); beyond it a still-nesting
collection degrades to a `Raw` printed form. Packed **Vector**/**Color** arrays
contain GF4 struct types and remain `Raw` (deferred to GF4). Godot `Dictionary`
preserves insertion order, so key order is deterministic.

## Value capture is parallel-indexed (G4 model, unchanged)

Each `var name := <collection>` compiles the literal into a compiler temporary,
then emits an `OPCODE_ASSIGN` (or `OPCODE_ASSIGN_TYPED_ARRAY` /
`OPCODE_ASSIGN_TYPED_DICTIONARY`) into the named slot, captured by the G4 assign
hook at that source line. A mutating METHOD call (`mut.append(4)`) is an
`OPCODE_CALL` — no assign, so it is not captured directly; the post-mutation
value becomes visible by reassigning `mut` into a fresh local (`mut_after`),
which observes the shared array's current contents.

## Expected captured collections (line, name -> structure)

| Line | Local        | Expected structured value |
| ---- | ------------ | ------------------------- |
| 35   | `a_untyped`  | Sequence[Int 1, String "two", Float 3.0] |
| 36   | `a_typed`    | Sequence[Int 10, Int 20, Int 30]  (`Array[int]`) |
| 37   | `p_byte`     | Sequence[Int 1, Int 2, Int 255]   (`PackedByteArray`) |
| 38   | `p_i32`      | Sequence[Int 100, Int 200, Int 300] (`PackedInt32Array`) |
| 39   | `p_i64`      | Sequence[Int 1000, Int 2000]      (`PackedInt64Array`) |
| 40   | `p_f32`      | Sequence[Float 1.5, Float 2.5]    (`PackedFloat32Array`) |
| 41   | `p_f64`      | Sequence[Float 3.5, Float 4.5]    (`PackedFloat64Array`) |
| 42   | `p_str`      | Sequence[String "x", String "y", String "z"] (`PackedStringArray`) |
| 43   | `d_untyped`  | Sequence[Tuple("a", Int 1), Tuple("b", Int 2)] |
| 44   | `d_typed`    | Sequence[Tuple("x", Int 10), Tuple("y", Int 20)] (`Dictionary[String,int]`) |
| 45   | `nested`     | Sequence[Sequence[Int 1, Int 2], Sequence[Int 3, Int 4]] |
| 46   | `d_with_arr` | Sequence[Tuple("nums", Sequence[Int 7, Int 8, Int 9])] |
| 48   | `mut`        | Sequence[Int 1, Int 2, Int 3]   (pre-mutation) |
| 50   | `mut_after`  | Sequence[Int 1, Int 2, Int 3, Int 4] (post-append; length 3 -> 4) |

## Deterministic checksum

`checksum = a_untyped.size()(3) + a_typed[0..2](60) + p_byte[2](255)`
`         + p_i32[0](100) + p_i64.size()(2) + p_f32.size()(2) + p_f64.size()(2)`
`         + p_str.size()(3) + d_untyped["a"]+["b"](3) + d_typed["x"]+["y"](30)`
`         + nested[0][0]+nested[1][1](5) + d_with_arr["nums"][0](7)`
`         + mut_after.size()(4)`
`         = 476`

The program prints `CT_GF3_RESULT=476`; the runner greps for it as a
determinism guard.

## Non-vacuity (tamper runs, each MUST be rejected)

`scripts/verify_gf3.py tamper <full.json> <mode>`:

- `elem`    — a_untyped[0] Int 1 -> 999 (wrong element value).
- `length`  — drop a_typed's last element (wrong length 3 -> 2).
- `dictkey` — d_untyped first pair key "a" -> "z" (wrong dict key).
- `dictval` — d_untyped first pair value 1 -> 999 (wrong dict value).
- `nesting` — flatten nested[0] from a Sequence to an Int (wrong nesting shape).
