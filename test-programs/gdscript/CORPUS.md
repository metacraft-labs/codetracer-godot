# GDScript Recorder — Feature Test Corpus (GT1)

This is the **authoritative index** of every GDScript recorder test program in
this fork. It enumerates one reference `.gd` program per G/GF milestone, the
milestone/feature it covers, its verifier, and its first-principles golden.

It is consumed by two scripts (both source `scripts/corpus-lib.sh`):

- **`scripts/record-and-verify.sh`** — the unified corpus runner. For every
  `primary` program it builds/uses the patched engine, records headless with
  `CT_GDSCRIPT_TRACE`, decodes the produced `.ct` with `ct-print --full`, and
  runs that program's verifier. It runs the **whole** suite (G2 … GF13),
  reports pass/fail per program, and **exits nonzero if any program fails**.
- **`scripts/verify-corpus-no-silent-skip.sh`** — the no-silent-skip gate. It
  hard-fails (nonzero) if the corpus could pass by discovering or verifying
  nothing (see *No-Silent-Skip Contract* below).

Both parse the machine-readable records between the
`CORPUS-MACHINE-BEGIN` / `CORPUS-MACHINE-END` markers. That block is the single
source of truth; the human table below mirrors it.

> **No mocks anywhere.** Every row is recorded by the real patched
> `godotengine/godot@4.6.2-stable` fork over a real `.gd` program, producing a
> real `.ct` decoded by the real `ct-print` (the same `TraceReader` decode path
> `MaterializedReplaySession` uses). No mocked engine, VM, writer, reader, or
> trace exists in the corpus.

## Golden contract (first-principles; regeneration forbidden)

Every `scripts/EXPECTED-*.md` golden is **hand-derived from first principles** —
written by reasoning about the source program's control flow, call tree, and
Variant encodings **before** looking at recorder output — exactly as the BEAM
recorder corpus requires (`BEAM-Materialized-Trace-Recorder.milestones.org`
M1/M17). The `verify_*.py` scripts duplicate those expected facts as literals so
the assertion is not circular, and each proves it is non-vacuous with tamper
runs (a wrong value/name/step/branch is rejected).

**Regenerated goldens are forbidden.** A golden must never be produced by
capturing recorder output and pasting it back. If a recorder change alters the
correct expected facts, update the golden by re-deriving it from first
principles (and say so in the milestone), never by dumping the new trace. This
is the discipline that keeps "every feature is covered" honest.

## Roles

- **primary** — recorded standalone and verified by its `verify_*.py`. These are
  the programs the corpus counts as "features covered".
- **helper** — a class/scene dependency (e.g. an inner-class or base-class file)
  compiled as part of a primary's project. It has no `_init` main of its own and
  is never recorded standalone; the `entry` column names the primary that pulls
  it in.
- **probe** — a negative/edge recording (e.g. a *failing* assert that halts the
  VM) asserted by an inline check rather than a `verify_*.py`.

## Coverage map

| Program                 | Milestone | Role    | Verifier          | Golden            | Result marker(s)              |
| ----------------------- | --------- | ------- | ----------------- | ----------------- | ----------------------------- |
| `g2probe.gd`            | G2        | primary | `verify_g3.py g2` | `EXPECTED-G3.md`  | `CT_G2_STEPS=30`              |
| `gf_calls.gd`           | G3        | primary | `verify_g3.py g3` | `EXPECTED-G3.md`  | `CT_G3_RESULT=107`            |
| `gf_values.gd`          | G4 / G5   | primary | `verify_g4.py`    | `EXPECTED-G4.md`  | `CT_G4_RESULT=47`             |
| `gf_typing.gd`          | GF1       | primary | `verify_gf1.py`   | `EXPECTED-GF1.md` | `CT_GF1_RESULT=1222`          |
| `gf_control_flow.gd`    | GF2       | primary | `verify_gf2.py`   | `EXPECTED-GF2.md` | `CT_GF2_RESULT=281`           |
| `gf_collections.gd`     | GF3       | primary | `verify_gf3.py`   | `EXPECTED-GF3.md` | `CT_GF3_RESULT=476`           |
| `gf_variant_types.gd`   | GF4       | primary | `verify_gf4.py`   | `EXPECTED-GF4.md` | `CT_GF4_RESULT=129`           |
| `gf_functions.gd`       | GF5       | primary | `verify_gf5.py`   | `EXPECTED-GF5.md` | `CT_GF5_RESULT=120`           |
| `gf_lambdas.gd`         | GF6       | primary | `verify_gf6.py`   | `EXPECTED-GF6.md` | `CT_GF6_RESULT=379`           |
| `gf_zoo.gd`             | GF7       | primary | `verify_gf7.py`   | `EXPECTED-GF7.md` | `CT_GF7_RESULT=20`            |
| `gf_animal.gd`          | GF7       | helper  | (via `gf_zoo.gd`) | `EXPECTED-GF7.md` | —                             |
| `gf_dog.gd`             | GF7       | helper  | (via `gf_zoo.gd`) | `EXPECTED-GF7.md` | —                             |
| `gf_props.gd`           | GF8       | primary | `verify_gf8.py`   | `EXPECTED-GF8.md` | `CT_GF8_RESULT=208;CT_GF8_TOTAL=7` |
| `gf_signals.gd`         | GF9       | primary | `verify_gf9.py`   | `EXPECTED-GF9.md` | `CT_GF9_RESULT=100`           |
| `gf_coroutine.gd`       | GF10      | primary | `verify_gf10.py`  | `EXPECTED-GF10.md`| `CT_GF10_RESULT=42`           |
| `gf_node_main.gd`       | GF11      | primary | `verify_gf11.py`  | `EXPECTED-GF11.md`| `CT_GF11_PROC=3;CT_GF11_RESULT=7` |
| `gf_node.gd`            | GF11      | helper  | (via `gf_node_main.gd`) | `EXPECTED-GF11.md` | —                       |
| `gf_threads.gd`         | GF12      | primary | `verify_gf12.py`  | `EXPECTED-GF12.md`| `CT_GF12_RESULT=8`            |
| `gf_diag.gd`            | GF13      | primary | `verify_gf13.py`  | `EXPECTED-GF13.md`| `CT_GF13_RESULT=29`           |
| `gf_diag_assert_fail.gd`| GF13      | probe   | inline halt-check | `EXPECTED-GF13.md`| —                             |
| `n1_nested.gd`          | N1        | primary | `verify_n1.py standalone` | `EXPECTED-N1.md` | `CT_N1_RESULT=12`         |

Notes:

- `g2probe.gd`'s first-principles per-line step sequence is documented under the
  "per-line steps regression (G2)" section of `EXPECTED-G3.md` and asserted by
  `verify_g3.py g2`; there is no separate `EXPECTED-G2.md`.
- `gf_values.gd` covers both G4 (value capture via `ct-print`) and G5
  (`MaterializedReplaySession` time-travel + value-origin, verified separately
  in the db-backend under `cargo test`).
- `gf_threads.gd` is inherently racy: the full race-shakeout loop (record N
  times) lives in `scripts/record-and-verify-gf12.sh`. The corpus runner records
  one representative run and asserts the deterministic facts via `verify_gf12.py`.
- `gf_diag_assert_fail.gd` deliberately halts the VM at a failing `assert`; the
  corpus runner asserts the halt (a step at the assert line, no step after it,
  and the post-assert `SHOULD_NOT_REACH` print absent), mirroring
  `record-and-verify-gf13.sh`.
- `n1_nested.gd` is dual-mode. The corpus records it STANDALONE (no `CT_MCR_*`),
  where the N1 join-key emission is INERT — `verify_n1.py standalone` asserts it
  records its steps but emits ZERO join events (byte-identical to a pre-N1
  recording). The join-key proof itself (call-entry/exit + native-call `(GEID,
  tick)` keys, well-formed and RESOLVABLE against a synthetic native trace per
  the correlation record) runs under a controlled MCR context in the dedicated
  `scripts/record-and-verify-n1.sh` (`verify_n1.py verify` + tamper), which the
  corpus does not set up.

<!-- CORPUS-MACHINE-BEGIN -->
<!--
  fields: program|milestone|role|entry|verifier|cmd|golden|markers
  This block is the single source of truth parsed by corpus-lib.sh.
-->
```
g2probe.gd|G2|primary|g2probe.gd|verify_g3.py|g2|EXPECTED-G3.md|CT_G2_STEPS=30
gf_calls.gd|G3|primary|gf_calls.gd|verify_g3.py|g3|EXPECTED-G3.md|CT_G3_RESULT=107
gf_values.gd|G4|primary|gf_values.gd|verify_g4.py|verify|EXPECTED-G4.md|CT_G4_RESULT=47
gf_typing.gd|GF1|primary|gf_typing.gd|verify_gf1.py|verify|EXPECTED-GF1.md|CT_GF1_RESULT=1222
gf_control_flow.gd|GF2|primary|gf_control_flow.gd|verify_gf2.py|verify|EXPECTED-GF2.md|CT_GF2_RESULT=281
gf_collections.gd|GF3|primary|gf_collections.gd|verify_gf3.py|verify|EXPECTED-GF3.md|CT_GF3_RESULT=476
gf_variant_types.gd|GF4|primary|gf_variant_types.gd|verify_gf4.py|verify|EXPECTED-GF4.md|CT_GF4_RESULT=129
gf_functions.gd|GF5|primary|gf_functions.gd|verify_gf5.py|verify|EXPECTED-GF5.md|CT_GF5_RESULT=120
gf_lambdas.gd|GF6|primary|gf_lambdas.gd|verify_gf6.py|verify|EXPECTED-GF6.md|CT_GF6_RESULT=379
gf_zoo.gd|GF7|primary|gf_zoo.gd gf_animal.gd gf_dog.gd|verify_gf7.py|verify|EXPECTED-GF7.md|CT_GF7_RESULT=20
gf_animal.gd|GF7|helper|gf_zoo.gd|-|-|EXPECTED-GF7.md|-
gf_dog.gd|GF7|helper|gf_zoo.gd|-|-|EXPECTED-GF7.md|-
gf_props.gd|GF8|primary|gf_props.gd|verify_gf8.py|verify|EXPECTED-GF8.md|CT_GF8_RESULT=208;CT_GF8_TOTAL=7
gf_signals.gd|GF9|primary|gf_signals.gd|verify_gf9.py|verify|EXPECTED-GF9.md|CT_GF9_RESULT=100
gf_coroutine.gd|GF10|primary|gf_coroutine.gd|verify_gf10.py|verify|EXPECTED-GF10.md|CT_GF10_RESULT=42
gf_node_main.gd|GF11|primary|gf_node_main.gd gf_node.gd|verify_gf11.py|verify|EXPECTED-GF11.md|CT_GF11_PROC=3;CT_GF11_RESULT=7
gf_node.gd|GF11|helper|gf_node_main.gd|-|-|EXPECTED-GF11.md|-
gf_threads.gd|GF12|primary|gf_threads.gd|verify_gf12.py|verify|EXPECTED-GF12.md|CT_GF12_RESULT=8
gf_diag.gd|GF13|primary|gf_diag.gd|verify_gf13.py|verify|EXPECTED-GF13.md|CT_GF13_RESULT=29
gf_diag_assert_fail.gd|GF13|probe|gf_diag_assert_fail.gd|-|-|EXPECTED-GF13.md|-
n1_nested.gd|N1|primary|n1_nested.gd|verify_n1.py|standalone|EXPECTED-N1.md|CT_N1_RESULT=12
```
<!-- CORPUS-MACHINE-END -->
