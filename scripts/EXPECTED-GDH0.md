# EXPECTED-GDH0 — first-principles facts for the GDH-M0 hot-reload falsifier

Companion to `scripts/record-and-verify-gdh0.sh` and `scripts/verify_gdh0.py`,
in the same spirit as `EXPECTED-HCR1.md` and the `EXPECTED-G*.md` /
`EXPECTED-GF*.md` files: **every value the drivers assert is derived HERE, by
hand, from the sources** — so an assertion that agrees with the script but
disagrees with reality is visible.

The milestone is `GDH-M0` in
`codetracer-specs/Planned-Features/GDScript-Hot-Reload-Multi-Version-Sources.milestones.org`;
the design is the sibling `.md`.

## What GDH0 is

A **real GDScript file replaced on disk while a headless engine is running it**,
reloaded through a path that exists in the engine TODAY — `core:reload_scripts`
delivered on the `--remote-debug` peer — and then the resulting CodeTracer
recording is measured.

Two gates, with deliberately different lifetimes:

| gate | lifetime | claim |
|---|---|---|
| `GDH-G0a` | **permanent** | The engine really reloaded. Asserted from the engine's own stdout, independently of any trace. |
| `GDH-G0b` | **a dated snapshot, retired by GDH-M6** | What the container carries today: ONE `paths.dat` entry, ONE raw source view holding **v1's** bytes, while post-reload steps decode to line numbers that do not exist in v1 at all. |

`GDH-G0b` asserts a defect, and a test that asserts a defect must not become
furniture. It is expected to go **red** the moment GDH-M1 and GDH-M3 land, and
GDH-M6's deliverables include deleting it.

> **`external_prereqs: none`, honoured literally.** Nothing here changes the
> engine, the writer, the trace format, the C ABI or the HCR agent. The engine
> binaries were used as built; `ct-print` was used as checked in.

## The fixtures — `test-programs/gdh0/probe_v{1,2}.gd`

Both are `extends MainLoop` programs with no clock, no RNG and no
host-dependent value, so every printed token is derivable from the source.

`probe_v1.gd` is **40 lines**, sha256
`ab9359734e4cad0c00be7416020d274040c89e164e26036a039133850f2b6430`, 1302 bytes.
`probe_v2.gd` is **57 lines**, sha256
`85d79f83c20c3c68e8d1bd5a9861ac0cfa5a0899d73f6939adca3de6df3fb4a9`, 2302 bytes.

Counted by hand off the two files:

| | v1 | v2 |
|---|---|---|
| `func probe(` declared at | line 27 | line 44 |
| probe body lines | 28, 29, 30 | 45, 46, 47 |
| `_process` body lines | 33, 34, 35, 36, 37 | 50, 51, 52, 53, 54 |
| `_finalize` body line | 40 | 57 |
| per-line probe tokens | `GDH0_V1_A`, `GDH0_V1_B`, `GDH0_V1_C` | `GDH0_V2_A`, `GDH0_V2_B`, `GDH0_V2_C` |
| `const TICKS` | 24 | 24 |

**The insertion is 17 comment lines, added ABOVE `func probe(` and nowhere
else.** 27 + 17 = 44, which is v2's `func probe(` line, and every executed line
below it moves by the same 17. The milestone requires *at least 5*; 17 is
chosen so that a stronger property also holds:

> **Every line v2 executes after the reload lies in 45..57, and v1 is only 40
> lines long.** So a post-reload step's line number does not merely point at
> the *wrong* line of the source the container carries — it points **past the
> end of that file**. The defect is stated as a line number rather than as an
> opinion.

**The insertion height is NEVER written into the verifier.** `verify_gdh0.py`
measures it by diffing the two files with `difflib.SequenceMatcher` and
requires the diff to carry **exactly one** `insert` opcode lying entirely above
v2's `func probe(` line. It then cross-checks that height against the
independently measured `v2_probe_def_line - v1_probe_def_line`. The tick count,
the probe body's line numbers and the per-version tokens are measured the same
way. A fixture edit therefore cannot silently disagree with an assertion.

**The probe function is located by `^func probe\(`, never by the words "func
probe".** Both files' prose mentions the function; a pattern that matched
vocabulary rather than syntax would be satisfied by the comment
(`Verification-Harness-Traps.md` 4d).

### Why the tick delay exists

`OS.delay_msec(TICK_MS)` with `TICK_MS = 100` gives the run a ~2.4 s body.
Without it, **measured on this host on 2026-09-10, the program finished all 24
ticks in 0.10 s** and the reload request landed after the run was over — 24 v1
ticks and no post-reload half at all. This is the same lesson
`EXPECTED-HCR1.md` records for the HCR1 demo: the driver must have a real
window, and it must key off a marker the engine PRINTED rather than off a timer.

### Why `GDH0_TICK=<n> ` has a trailing space

The driver acts on tick 8. Without the trailing space, `GDH0_TICK=8` also
matches `GDH0_TICK=18`. Same rule as `EXPECTED-HCR1.md`'s `CT_HCR_TICK=8 `.

## The reload vehicle

Design §5.1's measurement, re-confirmed here by running it:

* `GDScriptLanguage::reload_scripts` is `#ifdef DEBUG_ENABLED`
  (`modules/gdscript/gdscript.cpp`), and `template_debug` defines
  `DEBUG_ENABLED` without `TOOLS_ENABLED` (`SConstruct:542-544` sets
  `editor_build` / `debug_features`; `:557-563` turns them into the two
  `CPPDEFINES`).
* The remote-debugger command exists with **zero preprocessor conditionals** in
  the file: `_core_capture` stores the request at
  `core/debugger/remote_debugger.cpp:722-725` (its in-break twin inside
  `debug()` is at `:530-531`), and `poll_events` applies it **during idle poll
  only** at `:688-719`, re-reading each script from disk.
* The engine **connects out** to `--remote-debug tcp://host:port`, so the
  driver must be listening first.

### The wire format, measured

`remote_debugger_peer.cpp:101-170`: `u32 LE length` followed by
`encode_variant(Array)`.

A host→engine command is a **three**-element array:

```
["core:reload_scripts", <thread_id>, ["res://probe.gd"]]
```

`remote_debugger.cpp:353-374` — `_poll_messages` does
`ERR_CONTINUE(cmd.size() != 3)`, `cmd[0]` STRING, `cmd[1]` INT, `cmd[2]` ARRAY.
`cmd[1]` must name a thread the engine has registered; the main thread is
registered unconditionally at `:801` and `Thread::MAIN_ID` is **1**
(`core/os/thread.h:72`).

> **A two-element array is refused, and this was measured before it was read.**
> `poll_events` at `:663` parses a two-element shape, which is the obvious
> thing to send. The first run of this driver sent `[cmd, data]` and the engine
> answered, on its own stdout:
> `ERROR: Condition "cmd.size() != 3" is true. Continuing. at: _poll_messages
> (core/debugger/remote_debugger.cpp:359)`.
> The three-element form is the one `_poll_messages` accepts.

## `--path <real dir>` — deliverable 4

`--path` is gated on `OVERRIDE_PATH_ENABLED`, which `SConstruct:1171-1172` sets
from `disable_path_overrides` — and that option **defaults to `True`**
(`SConstruct:281-287`). A stock export template therefore aborts on `--path`
with a *different* message than an enabled build does
(`main/main.cpp:1768-1786`):

| build | message on a bad `--path` |
|---|---|
| `OVERRIDE_PATH_ENABLED` | `Invalid project path specified: "...", aborting.` |
| not compiled in | ``ERR_PRINT("`--path` was specified ... compiled without support for path overrides ...")`` |

`verify_gdh0.py pathcheck` runs `--headless --path <nonexistent> --quit` and
requires **exactly one** of the two to be present. That pairing IS the control
(trap 4a): a scan that could see neither message would satisfy a lone "must not
contain" assertion for free.

**Measured 2026-09-10, both engine binaries in `bin/`:** the
`OVERRIDE_PATH_ENABLED` message. `--path` is available.

### Why the driver must DIE rather than record a silently-ignored overwrite

A PCK-backed run mounts `res://` from an archive and cannot be overwritten
underneath; the file on disk would change and the engine would never see it,
producing a run that *looks* like "the reload did not happen". The driver
refuses that ambiguity three ways, before and during the run:

1. it refuses to record if a `.pck` / `.zip` sits in the project directory, or
   a `.pck` sits next to the engine binary;
2. it re-reads `probe.gd` from disk **after** the rename and compares its
   sha256 with the fixture's — and `die()`s, killing the engine, on a mismatch;
3. it re-reads it again at the end of the run and asserts the content still
   holds, so a run whose overwrite was reverted cannot pass.

## What the driver does, and the four arms

The reload request is published once the engine has PRINTED `GDH0_TICK=8 ` — a
marker read out of the engine's own stdout, not a timer. All four arms are real
recordings of the real engine; `allowed_mocks: none`, and there are none.

| arm | overwrite | reload request | expected |
|---|---|---|---|
| `reload` | v2 | `res://probe.gd` | GDH-G0a **green**, GDH-G0b **green** |
| `control` | none | none | one version only, and the gate must SAY so |
| `wrongtarget` | v2 | `res://gdh0_never_loaded.gd` | GDH-G0a **red** — "the reload did not happen" |
| `identical` | v1 | `res://probe.gd` | GDH-G0a **red** — an unchanged observation must not pass |

The last two are the milestone's own `falsifier:` clauses, run as arms rather
than asserted in prose. `verify --expect-g0a-red` INVERTS the verdict for them:
a **green** GDH-G0a on either would mean the gate has no teeth, and is reported
as a harness failure.

## Derived expectations — GDH-G0a

From the fixtures alone, with `TICKS = 24` and a three-line probe body:

* stdout carries exactly one `GDH0_BEGIN` and exactly one `GDH0_END`;
* the tick numbers are exactly `1..24`, in order, each once;
* there are exactly `24 x 3 = 72` probe observations. **The count, not `> 0`** —
  a loop that skipped two thirds of its ticks satisfies every existential
  control over it (trap 4b);
* each tick's three observations are the ordered token sequence of **one**
  version;
* the v1 ticks are a **non-empty prefix**, the v2 ticks a **non-empty suffix**,
  and there is **exactly one** transition. A run showing v2 from tick 1 proves
  a stale build on disk, not a reload;
* the engine's exit code is **0**. Per trap 1, **rc 124 is a hang and nothing
  else**: the driver's wall-clock arm kills the engine and records 124
  explicitly, and every other non-zero code is a die-before-summary.

## Derived expectations — GDH-G0b

### Anti-vacuity first, subject second

* **The `--events` dump must be COMPLETE**: its line count must equal
  `1 + steps + 2*calls + io_events` exactly — one header line, one line per
  step, **two** per call (`call_entry` + `call_exit`), one per io event. Never
  `lines > 0`. This is `EXPECTED-HCR1.md`'s rule, adopted campaign-wide: a dump
  truncated by `head` satisfies a grep as well as a complete one.
* **The scan must reach the container.** The gate asserts the internal-file
  directory carries `srcviews.dat` / `srcviews.off` **under those names**. The
  spec section calls them `source_views.dat` / `.off`, and base40 caps a CTFS
  internal name at 12 characters, so that name truncates to `source_views` and
  collides with the `.off` entry; the writer works around it at
  `multi_stream_writer.nim:1699-1708`. **A scan for the spec's name finds
  nothing and passes every "must not contain" check written over it** — trap 4
  exactly.
* **Paths exist and the fixture's path is among them, asserted BEFORE the count
  is asserted to be 1** — a decode that produced no paths at all would satisfy
  "not two" for free.
* **Two independent readers must agree.** `verify_gdh0.py` decodes `paths.dat`
  / `paths.off` out of the container itself and requires the result to equal
  what `ct-print` reported. So "one path entry" is a measurement two
  instruments agree on, not one instrument's opinion.
* **The byte scan has a positive twin.** Before asserting that v2's token is
  absent from the container's raw bytes, the gate asserts v1's token is
  **present** — break the reader and the positive half goes red first (trap 4a:
  the pairing is the control).

### The subject

* exactly **one** `paths.dat` entry, and it is `res://probe.gd`;
* exactly **one** raw (`view_kind == 0`) source view, on `path_id` 0, named
  `res://probe.gd`, whose bytes are **non-empty** and **hash-equal to
  `probe_v1.gd`** — and *not* equal to `probe_v2.gd`;
* v1's complete text occurs exactly **once** in the container's raw bytes;
  v2's first probe token occurs **zero** times;
* every step is attributed to `path_id` 0 — the only path there is;
* **the post-reload steps decode to lines 45..57, which do not exist in the
  40-line source the container carries**, and the two line regimes do not
  interleave: the last in-range step index is strictly below the first
  out-of-range one, i.e. exactly one crossing;
* the trace's step counts on each version's probe body equal three times the
  number of ticks the ENGINE printed for that version. This ties the container
  to the engine's own observations rather than to itself.

### Why `checkLineWithinFile` never fires — measured, and not what was expected

`registerStep` refuses a line past a file's recorded slot
(`multi_stream_writer.nim:887` → `checkLineWithinFile:401-431`), so the natural
expectation is that a v2-only line recorded under v1's path id produces a hard
error rather than a silent mis-attribution.

**It does not, and the reason is in the first line of the check:**

```nim
if not w.lineCountTable:
  return ok()
```

The GDScript recorder never turns the line-count table on. The C ABI it links
(`modules/gdscript/ct_writer/include/codetracer_trace_writer.h`) exposes
`trace_writer_register_step(handle, path, line)` and **no path-registration or
line-count entry point at all** — the FFI's
`trace_writer_register_path_with_line_count` is simply not in the vendored
header, and `gdscript_ct_trace.cpp` never calls anything of the kind. So
`meta.dat` bit 14 (`FlagHasLineCountTable`, `meta_dat.nim:265`) is **clear**,
every file is sized at `DefaultLinesPerFile` = 100000, and the writer has no
bound to test line 57 against.

**The mis-attribution is therefore SILENT.** Lines 45..57 sit comfortably
inside path 0's 100000-address slot, so nothing bleeds into a neighbouring
file's range either — the container is internally consistent and simply wrong.
The gate asserts bit 14 is clear, so that if a later change turns the
line-count table on, this explanation goes red rather than going stale.

## Measured baseline, 2026-09-10

Host: Linux x86_64, `nproc` = 24.
Engine: `bin/godot.linuxbsd.template_debug.x86_64`
(`4.6.2.stable.custom_build.91dc4124a`, built 2026-09-09 21:36:29 UTC).
Reader: the checked-in `../codetracer-trace-format-nim/ct-print`.
Command: `scripts/record-and-verify-gdh0.sh <outdir>` — exit **0**.

### GDH-G0a — the engine really reloaded

`reload` arm, verbatim from the engine's own stdout across the transition:

```
GDH0_V1_A tick=7
GDH0_V1_B tick=7
GDH0_V1_C tick=7
GDH0_TICK=7
GDH0_V1_A tick=8
GDH0_V1_B tick=8
GDH0_V1_C tick=8
GDH0_TICK=8
GDH0_V2_A tick=9
GDH0_V2_B tick=9
GDH0_V2_C tick=9
GDH0_TICK=9
GDH0_V2_A tick=10
```

(`GDH0_TICK=<n>` lines carry a trailing space that this listing cannot show.)

| arm | ticks v1 → v2 | transitions | engine rc | wall |
|---|---|---|---|---|
| `reload` | **8 then 16** | **1** | 0 | 2.78 s |
| `control` | 24 then 0 | 0 | 0 | 2.61 s |
| `wrongtarget` | 24 then 0 | 0 | 0 | 2.64 s |
| `identical` | 24 then 0 | 0 | 0 | 2.64 s |

The reload request went out at t = 0.94 s and the transition is at tick 9, the
first tick after the marker the driver waited for. The overwrite was verified
on disk by sha256 immediately after the rename and again at the end of the run.

`wrongtarget` additionally produces the engine's own account of the refusal,
which is why the arm is worth running rather than reasoning about:

```
ERROR: Attempt to open script 'res://gdh0_never_loaded.gd' resulted in error 'File not found'.
   at: load_source_code (modules/gdscript/gdscript.cpp:1140)
ERROR: Could not reload script 'res://gdh0_never_loaded.gd': File not found
   at: poll_events (core/debugger/remote_debugger.cpp:710)
```

### GDH-G0b — what the container carries

`reload` arm, `trace/gdscript_trace.ct`, 188,416 bytes:

```
counts: paths 1  functions 5  varnames 1  types 6
        source_views 1  steps 196  calls 51  values 196  io_events 0
--events dump lines: 299  ==  1 + 196 + 2*51 + 0
paths:        ['res://probe.gd']
source_views: [{path_id: 0, view_kind: 0, view_name: 'res://probe.gd',
                content_len: 1302, map_len: 0}]
meta.dat flags: 0x2f20   ->  bit 5 (alternate source views) SET
                             bit 14 (line count table)      CLEAR
```

`content_len` 1302 is `probe_v1.gd`'s byte count, and the view's bytes hash to
v1's sha256. **`probe_v2.gd`'s text is nowhere in the container**; `GDH0_V2_A`
occurs zero times in its 188,416 raw bytes while `GDH0_V1_A` occurs there and
v1's complete text occurs exactly once.

The step-line histogram is the finding in one line:

```
reload arm   line: 22  25  28  29  30  33  34  35  36  37 | 45  46  47  50  51  52  53  54  57
             hits:  2   1   8   8   8   8   8   8   8   8 | 16  16  16  16  16  16  16  16   1
control arm  line: 22  25  28  29  30  33  34  35  36  37 | 40
             hits:  2   1  24  24  24  24  24  24  24  24 |  1
```

Lines 22 and 25 are `var tick := 0` in `@implicit_new` and `_initialize`'s
body — startup, once each, and identical in both versions. Then, left of the
bar, v1's per-tick lines at **8 hits each**: the 8 ticks the engine printed v1
tokens for. Right of the bar, **lines 45..57 of a file the container says is 40
lines long**, at **16 hits each**, matching the 16 ticks the engine printed v2
tokens for; line 57 is v2's `_finalize`, run once. **129 of the 196 steps are
out of range**; the control arm ends at v1's line 40 instead, which is the same
statement from the other side.

The control arm proves the one-entry shape is not an artifact of the reload:
same one path, same one source view, same v1 bytes, and every step inside v1's
40 lines.

**The premise holds. Design §2 is confirmed by measurement.** The container
carries one version of a file that ran in two, the collapse is silent, and no
consumer reading this trace can recover which version any step executed.

### Assertion counts — the fingerprint

Per `Verification-Harness-Traps.md` 4c the checks assert their own assertion
counts, so a branch that stops making claims goes red instead of quietly making
fewer of them. Written from the run above:

| check | reload | control | wrongtarget | identical |
|---|---|---|---|---|
| fixture preconditions | 12 | 12 | 12 | 10 |
| GDH-G0a | 16 | 9 | 16 | 16 |
| GDH-G0b | 31 | 23 | — | — |

The arms differ legitimately: the control arm's GDH-G0a stops once it has
established "one version, no transition", the `identical` arm's fixture check
skips the two assertions that forbid an identical pair, and only the two arms
carrying a meaningful container run GDH-G0b. That is exactly why the
fingerprint is per (check, arm) rather than a single number.

### Both engine binaries reproduce it

`bin/godot.linuxbsd.template_debug.x86_64.hcr` — the HCR-patchable build with
the in-target agent linked, 108 MB — was run through the `reload` arm as well
and produced the identical verdict: 8 v1 ticks then 16 v2, one transition,
rc 0, GDH-G0a 16/16 green, GDH-G0b 31/31 green, wall 3.21 s. **GDH-M0 needs no
native patching**, so the stock 84 MB build is the one the driver defaults to;
the `.hcr` run exists to show the choice does not carry the result. `BIN=` in
the environment selects either.

### `/dev/shm`

The GDScript recorder writes its container directly and does not use the MCR
shared rings, so this run leaks nothing: `ct_*` segments before and after were
both **1** (a pre-existing segment from unrelated work). The driver counts them
either way, and cleans up any it added, because an accumulation of `ct_rb_*` /
`ct_mcr_*` has been measured to fake unrelated failures in later recordings on
this host.

### Numbers that are per-run rather than constants

Wall times move with host load. Container byte size is block-aligned and was
188,416 for all four arms here, but should be quoted as "≈188 kB", not as a
constant. `peer_messages` (28 for three arms, 32 for `wrongtarget`, which
carries four extra error messages) depends on how the engine batches its
`output` capture and is transcript, not assertion. The step/call/value counts
*are* deterministic for a fixed fixture and a fixed tick count, and are
asserted.

## Negative controls — the demo must be able to fail

Beyond the two falsifier arms, three properties of the harness were checked by
construction rather than assumed:

1. **A missing prerequisite is a LOUD failure.** Every path the driver needs is
   checked with the path printed; a missing `driver.json` is a `die`, never a
   skip. `verify` cannot report on an arm that was not recorded.
2. **The engine failing to connect is fatal.** If no debugger peer connects
   within 60 s the driver kills the engine and dies, rather than recording a
   run in which the reload request could never have been delivered.
3. **The CTFS reader refuses rather than guesses.** It is ported from
   `container.nim` / `variable_record_table.nim` rather than from the spec —
   the spec's §1 describes a free-list root area the in-tree writer does not
   emit, and its entries start at offset 16 with 31 root slots
   (`codetracer_ctfs/types.nim:17-22`). Every block number is bounds-checked
   against `floor(len / blockSize)` (§5d), and a `srcviews` record with
   trailing bytes raises instead of being truncated into a plausible answer.

## Review, 2026-09-10 — mutations run against the finished gate

Everything above was re-measured on an independent run (driver rc **0**, 8 v1
ticks then 16 v2, one transition, container 188,416 bytes, 129 of 196 steps in
45..57), and the container was re-measured with a scanner written for the
review rather than with `verify_gdh0.py`'s reader: `GDH0_V2_{A,B,C}` occur
**0** times in the raw bytes, v1's complete 1302-byte text occurs **once**, and
the `srcviews` record's framing was read straight out of the blob — the bytes
before v1's text are `0x0e "res://probe.gd" 0x96 0x0a`, i.e. the name length,
the name, and the varint 1302, with `0x00` (`map_len`) immediately after. The
`meta.dat` flags were read the same way: `0x2f20` at `CTMD+6`, bit 5 set, bit
14 clear.

Five mutations were then run that the implementation did not try. The gate
survived all five.

| # | mutation | expected | observed |
|---|---|---|---|
| 1 | `thread_id` = 99 instead of `Thread::MAIN_ID` | red | **red** — 24 v1, 0 v2, GDH-G0a 2/16 failed |
| 2 | two-element `[cmd, data]` message | red | **red** — engine printed `Condition "cmd.size() != 3" is true` |
| 3 | `TICK_MS` 100 → 400 in both fixtures | still 8 → 16 | **green**, request at t = 2.95 s not 0.94 s |
| 4 | three extra comment lines in v2's insertion | still green, recomputed | **green** — height 20, shift 20, steps at 48..60 |
| 5 | insertion shrunk to 3 lines | fixture red | **red** — "at least 5 lines are inserted (measured 3)" |

**1 is the sharpest.** `_poll_messages` accepts the three-element array, reads
`cmd[1]` as a thread id, and then does `if (!messages.has(thread)) continue;`
(`remote_debugger.cpp:366-368`) — an unregistered thread id is dropped **with
no diagnostic at all**, unlike mutation 2, which the engine complains about.
The reload silently does not happen and GDH-G0a catches it anyway.

**3 is the one that proves the marker.** Quadrupling the tick period moved the
reload request from 0.94 s to 2.95 s while the split stayed at exactly 8 → 16.
A 0.94 s *timer* would have fired around tick 2. The trigger is the marker
`GDH0_TICK=8 ` read from the engine's stdout, as claimed — this is the exact
defect that broke the Godot HCR demo's first MCR run.

**4 and 5 together prove nothing about the fixture is a constant.** Adding
three lines to v2 moved every derived number (17 → 20, 45..57 → 48..60) with no
edit to the verifier, and the run stayed green end to end; shrinking the
insertion below the milestone's floor of 5 went red on that clause by name.

### Two corrections the review made

* **`ct-print` CAN be rebuilt, and it does not help.** The claim that a rebuild
  needs zstd headers absent from this shell is wrong: the nix store has them,
  and `nim c -d:release --mm:arc -p:src --passC:"-I<zstd-dev>/include"
  --passL:"-L<zstd>/lib -lzstd" -o:ct-print src/codetracer_ct_print.nim`
  succeeds in **5.4 s**. The rebuilt binary agrees with the checked-in one on
  every number GDH-G0b asserts (paths 1, source_views 1, 299 dump lines) and
  still reports **neither** a source view's bytes **nor**
  `has_line_count_table` — that field lives in `codetracer_ct_print_lib.nim`,
  which the shipped `codetracer_ct_print.nim` does not import. So the reader
  below stays, but for the right reason.
* **Two reader defects, both now fixed.** (a) A `<base>.dat` whose declared
  size ran past what `<base>.off` addresses was read **silently**, dropping the
  unaddressed tail; the offset table is now validated to begin at 0, to be
  non-decreasing, and to end exactly at `len(dat)`
  (`variable_record_table.nim:48-53` writes precisely that shape), so all three
  corruptions raise. (b) `check_g0b` computed `min()`/`max()` over the
  out-of-range and in-range step halves *after* asserting they were non-empty —
  on mutation 1, where both assertions fail, that raised and aborted the check
  before `expect_count` ran, turning a clean RED into a traceback. It now makes
  the same single assertion either way, so the 31-assertion fingerprint holds
  on the failing path too.

Nine deliberate container corruptions were fed to the reader (bad magic, zeroed
mapping root, out-of-bounds mapping root, half-truncated file, `.dat` size ±1,
flipped content-length varint, `.off` not a multiple of 8, renamed stream,
broken `CTMD` magic). **All nine raise**; none returns "0 versions" or "not
found". That is the property this campaign has failed four times before.

## Retirement

When GDH-M1 and GDH-M3 land, `GDH-G0b` goes red on the assertions that say
"one" — and that is the campaign working. GDH-M6 deletes this arm and replaces
it with `GDH-G1` / `GDH-G2` / `GDH-G3`. `GDH-G0a`, the fixtures and the driver
survive: "the engine really reloaded" is a permanent property, and every later
gate needs a real reload to measure.
