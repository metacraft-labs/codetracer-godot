# EXPECTED-GDH6 — the hand-derived facts GDH-M6's gates are measured against

Design:    `codetracer-specs/Planned-Features/GDScript-Hot-Reload-Multi-Version-Sources.md`
           §2, §6.1–§6.4, §7.1.
Milestone: the `GDH-M6` block of the campaign's `.milestones.org`.
Driver:    `scripts/record-and-verify-gdh6.sh` → `scripts/verify_gdh6.py`.
Fixtures:  `test-programs/gdh6/probe_v{1,2,3}.gd` + `project.godot`.
Measured:  2026-09-11, x86_64-linux, `bin/godot.linuxbsd.template_debug.x86_64.hcr`
           (4.6.2.stable.custom_build, GCC 15.3.0), `codetracer-trace-format-nim/ct-print`.

**Nothing in this file is read by the verifier.** Every number below is either
derived by the harness at run time from the fixtures and the observed reload
schedule, or measured from a real run. The file exists so a reviewer can check
the harness's arithmetic against a human's, and so a fixture edit that changes
these numbers is a visible diff rather than a silent redefinition of what the
gate compares.

---

## 1. The fixtures

| | v1 | v2 | v3 |
|---|---|---|---|
| file | `probe_v1.gd` | `probe_v2.gd` | `probe_v3.gd` |
| total lines (`wc -l`) | 68 | 108 | 135 |
| addressable lines (recorded `line_count`) | 68 | 108 | 135 |
| probe lines | 55, 56, 57 | 94, 95, 96, 97 | 120, 121, 122, 123, 124 |
| probe-line count | **3** | **4** | **5** |
| wire generation | 1 (the content the process starts with) | 2 | 3 |

All three files end in a newline, so *addressable lines* equals `wc -l`. That
is the inclusive bound `checkLineWithinFile` enforces (`line > count` is
refused), and it is what the recorder records: see
`gdscript_ct_addressable_lines` in `modules/gdscript/gdscript_ct_trace.cpp`,
which counts newlines and adds one only when the file does not end in one.

### The three properties the fixture set must have

The verifier asserts all three before anything is recorded, and aborts the run
if any fails — a fixture that cannot support the gate must not produce a green
one.

1. **The probe line numbers are pairwise disjoint.** {55,56,57} ∩ {94…97} ∩
   {120…124} = ∅. A line number two versions shared would not identify a
   version, and the bijection would quietly degrade into a line-number check.

2. **Each version's probes lie past the whole of the previous version.**
   94 > 68 and 120 > 108. This is GDH-M0's requirement inherited: a post-reload
   step's line number does not exist in the previous version's text *at all*,
   so a consumer that resolves the recorded path to one source view is not
   merely showing the wrong line — it is being asked for a line the file it has
   does not reach. GDH-M0 measured exactly this and called it silent.

3. **The probe-line counts differ pairwise** (3, 4, 5). This is what stops the
   expected step count being written as a flat product: it has to be a *sum*
   over versions, and a flat product would agree with a trace that lost a whole
   version's worth of steps.

Each version is `probe_v1.gd` with an inert, comments-only block inserted
**above** `func probe`, and nothing else moved. The insertion height is never
written into the verifier; every probe line number is derived by scanning the
fixture for the token literal, and a literal that disagrees with the line it
sits on aborts the run.

---

## 2. The reload schedule and the expected step count

The driver delivers `probe_v2.gd` at generation 2 on tick 8 and `probe_v3.gd`
at generation 3 on tick 18, over the in-target HCR agent's `sourceChanged`
notification. The program runs `TICKS = 30` iterations at `TICK_MS = 60`.

Measured on 2026-09-11, the schedule lands as:

| version | iterations live | probe lines | steps |
|---|---|---|---|
| v1 | 8 | 3 | 24 |
| v2 | 10 | 4 | 40 |
| v3 | 12 | 5 | 60 |
| | **30** | | **124** |

`8 × 3 + 10 × 4 + 12 × 5 = 124`, and the recording carries exactly 124 steps
that resolve to a probe line of the version that ran them. The *iterations
live* column is observed from stdout rather than assumed — the reload is
applied at the engine's next safe point, so which tick it lands on is the
engine's decision and not the driver's — while the *probe lines* column comes
from the fixtures. Neither is a literal in the verifier.

---

## 3. The container, measured

`ct-print --events` on the two-reload recording:

```
counts: paths 3, functions 6, varnames 2, types 6, source_views 3,
        steps 310, calls 64, values 312, io_events 0, source_reloads 2
flags:  has_line_count_table true, has_source_reload true,
        has_alternate_source_views true, has_column_aware_steps false
paths:  ["res://probe.gd", "res://probe.gd", "res://probe.gd"]
path_versions:
  {path_id 0, version_ordinal 0, version_count 3, recorded_line_count 68}
  {path_id 1, version_ordinal 1, version_count 3, recorded_line_count 108}
  {path_id 2, version_ordinal 2, version_count 3, recorded_line_count 135}
source_views:
  {path_id 0, view_kind 0, view_name "res://probe.gd", content_len 2872}
  {path_id 1, view_kind 0, view_name "res://probe.gd", content_len 4959}
  {path_id 2, view_kind 0, view_name "res://probe.gd", content_len 6059}
markers:
  {step_index 77,  reload_ordinal 1, changed [{old 0, new 1, generation 2}],
   changed_count 1, in_flight_frames 0}
  {step_index 178, reload_ordinal 2, changed [{old 1, new 2, generation 3}],
   changed_count 1, in_flight_frames 0}
steps per path id: 0 → 77, 1 → 100, 2 → 133
step index ranges:  0 → [0, 76], 1 → [78, 177], 2 → [179, 311]
```

> **The ranges are in the container's `step_index` space, and the gaps are the
> markers.** This line read `[0,76] / [77,176] / [177,309]` until the GDH-M6
> review re-derived it: those numbers are each path's range of POSITIONS in the
> list of steps *with the markers removed*, which is a different space from the
> `step_index` the two marker records above are stamped with. Printing the two
> side by side invites the reader to conclude that the marker at `step_index 77`
> sits *inside* path 1's range and therefore contradicts the transition.
>
> It does not. A marker consumes a `step_index` of its own
> (`multi_stream_writer` advances `stepCount` for one), so **77 and 178 are
> exactly the two gaps in the step space**, and the corrected ranges state a
> stronger fact than the original did: each marker sits in the gap between the
> last step of the old version and the first step of the new —
> `76 → [77] → 78` and `177 → [178] → 179` — with no step of either version on
> the wrong side of it.
>
> Note also that `ct-print --summary` reports `steps: 312` for this container,
> because it folds the two markers into that line while the `--events` header
> does not. Every number in this document is the `--events` header's.

Three things in that dump are the milestone's headline, and each is worth
stating in words:

* **Three `paths.dat` entries carry the IDENTICAL `res://probe.gd` string** and
  are distinguished only by their index. Nothing is appended to, prefixed to or
  interposed into the string, so a consumer that resolves a user-supplied
  `res://probe.gd` keeps resolving it after a reload (design §6.1).
* **Each entry states its OWN line count** — 68, 108, 135 — so each version
  gets a correctly sized slot in the global position space, appended after
  every existing file. v1's slot keeps its base and its size, which is why
  every address already emitted against v1 still decodes to v1.
* **The step index ranges do not overlap.** Nothing recorded before the first
  reload carries path id 1 or 2, and nothing after the second carries 0 or 1.

### The dump's completeness arithmetic

```
441 lines = 1 (header) + 310 (steps) + 2 × 64 (calls) + 0 (io) + 2 (markers)
```

**`counts.steps` does NOT include the markers**, even though the writer
advances its own `stepCount` for one. This was measured rather than assumed:
adding the `source_reloads` term without checking would have been wrong by
exactly the number of markers — that is, wrong only on the runs this milestone
is about. `EXPECTED-HCR1.md:221-226` records why the completeness rule is
stated as arithmetic and never as `lines > 0`: a truncated dump is non-empty
and satisfies a bare grep just as well as a good one.

### The no-reload control

The same fixture, the same engine, the same agent connection, and no
`sourceChanged` delivered:

```
counts: paths 1, source_views 1, source_reloads 0
flags:  has_line_count_table true
paths:  ["res://probe.gd"], recorded_line_count 68
```

One entry, one view, no marker. The control is what proves the gate is not
simply requiring two versions to exist.

---

## 4. When `meta.dat` bit 14 is on, and why it is conditional

The line-count table is enabled **iff `REPRO_HCR_AGENT_SOCKET` is set**, i.e.
iff a coordinator is attached and a reload can arrive. The engine says which on
stderr, every run:

```
[ct-gdh6] line-count table ENABLED (meta.dat bit 14); a reload can mint a path version
[ct-gdh6] line-count table off: no REPRO_HCR_AGENT_SOCKET, so no reload can arrive …
```

The table changes the bytes of every container it is on for — `paths.dat`
records grow a count and the position space is laid out from recorded sizes
instead of the `DefaultLinesPerFile` stride — so turning it on unconditionally
would change every recording in the GT1 corpus. That is precisely
`gdh6_corpus_is_unchanged`'s first falsifier arm
(`CT_GDH6_FALSIFY_ALWAYS_LINE_COUNT_TABLE`), and it is the reason the switch is
conditional rather than global.

Under the table the implicit path registration that
`trace_writer_register_step` performs for an unseen path is **refused by name**
(it has no count to record), so the recorder sizes every path before the first
step that mentions it:

```
[ct-gdh6] sized res://probe.gd = 68 line(s) (counted)
```

A file whose lines cannot be counted is registered at the ceiling the C header
names (100000) and the fallback is reported, never silent.

---

## 5. The falsifier arms, and what each one is measured to do

### Consumer-side (`verify_gdh6.py --falsify`), no rebuild

| arm | reproduces | verdict | control (no reload) |
|---|---|---|---|
| `newest-wins` | `Db::path_map` last-wins, `ctfs_trace_reader/mod.rs:1372-1376` — **what ships today** | RED: 60 probe steps against 124 tokens; diverges at offset 0 (`v1 L55` vs `v3 L120`); 60 text mismatches | GREEN |
| `oldest-wins` | the mirror image — the error an implementer reaches for when fixing the above | RED: 24 probe steps against 124 tokens; diverges at offset 24 | GREEN |
| `fuzzy-unique-only` | `fuzzy_path_id_for` stage 6, `trace_reader.rs:1341-1356`, which returns `Some` only when `matches.len() == 1` | **RESOLUTION-FAILURE**, reported under its own name: 3 entries match the filename, so the lookup answers `None` and no step is attributed at all | GREEN |

The third is deliberately reported differently from the first two. "The path
did not resolve" and "the path resolved to the wrong version" have different
fixes, and a harness that folds them together sends the next reader to the
wrong file.

Each control staying green is what shows the arm discriminates: with one
version, a last-wins map, a first-wins map and a correct one all agree.

### Recorder-side (`CT_GDH6_FALSIFY=…`), one translation unit plus a relink

| arm | what it does | aimed at |
|---|---|---|
| `CT_GDH6_FALSIFY_NO_VERSION_MINTED` | applies the reload and mints no path version, so post-reload steps resolve to v1's id — **the state GDH-M0 measured** | `gdh6_no_step_is_attributed_to_the_wrong_version` |
| `CT_GDH6_FALSIFY_STALE_LINE_COUNT` | registers the new version with the previous version's line count | same |
| `CT_GDH6_FALSIFY_STALE_SOURCE_VIEW` | every path id and every line correct, every source view carrying v1's text | same — and it passes the line-number half completely, which is why the text half exists |
| `CT_GDH6_FALSIFY_STRING_KEYED_BUNDLE` | restores the string-keyed bundle early return GDH-M3 deleted | `gdh6_both_versions_retrievable_end_to_end` |
| `CT_GDH6_FALSIFY_NO_MARKER` | mints the version, emits no marker | `gdh6_reload_is_discoverable_end_to_end` |
| `CT_GDH6_FALSIFY_APPLY_WHERE_IT_LANDS` | applies the reload where the notification landed | `gdh5_reload_is_refused_while_a_step_is_pending` |
| `CT_GDH6_FALSIFY_ALWAYS_LINE_COUNT_TABLE` | sets bit 14 unconditionally | `gdh6_corpus_is_unchanged` |

Arm 1 necessarily takes the marker down with it: `registerSourceReload` refuses
`old_path_id == new_path_id`, so a version that was never minted has no
transition to record. That is stated rather than hidden, and the arm is aimed
at the attribution gate.

---

## 6. Two measurements that corrected the gates themselves

Both are recorded because the corrected version looks obvious and the original
did not, and because in each case the *arm* is what found it.

**`gdh5_reload_is_refused_while_a_step_is_pending`'s container check was
vacuous, twice.** Its first formulation read a `values` key off each step event
that `ct-print` does not emit, so the loop never ran; under the mutation the
only thing that went red was the "the recorder reports DEFERRED" precondition —
which is the guard-was-called assertion the milestone explicitly forbids as the
kill. Its second formulation allowed up to `in_flight_frames` old-version steps
after the marker, and **the mutation satisfied it**: the mutated engine
honestly reported `in_flight_frames: 1` and produced exactly one such step, so
both containers were internally consistent. Only the field itself separates
them — plain 0 and 0, mutated 1 and 1 — and `in_flight_frames == 0` is now the
kill, because a safe point is *defined* as a point where no GDScript frame is
executing. The two earlier invariants are kept alongside and labelled
NOT DISCRIMINATING for this arm, per the standing rule.

**`CT_GDH6_FALSIFY_APPLY_WHERE_IT_LANDS` is not a survivable mutation.** The
milestone's falsifier text assumed the mutated engine would produce a subtly
wrong container. Measured over five runs on 2026-09-11: three exited **-11**
(SIGSEGV), one closed the agent socket mid-handshake, one stalled before the
second reload window, and **one** produced a complete container. A crash is not
a diagnosis (`Verification-Harness-Traps.md` trap 1), so the driver classifies
a crashed attempt as CRASHED, never as a kill, retries up to
`GDH6_FLAKY_ATTEMPTS` times, and reports NOT DEMONSTRATED if every attempt
died. The crashes are real evidence that the guard is load-bearing; they are
not evidence that the *gate* discriminates, and the two are not allowed to look
alike.
