# EXPECTED-GODOT-4.7.2-BASE — what changed when this fork moved from 4.6.2 to 4.7.2

Base before:  `godotengine/godot` `4.6.2-stable` (`up-4.6.2`), 42 fork commits on top,
              fork HEAD `5206371fda`.
Base after:   `godotengine/godot` `4.7.2-stable` (`up-4.7.2` = `ed1daf0bf0`), merged into
              the `codetracer` adaptation branch.
Measured:     2026-09-13, x86_64-linux, GCC 15.3.0, `nix develop` shell,
              load average 120-190 throughout (a shared box; wall times are not
              a clean benchmark).

**Why the base moved.** The Flame demo's `FlameField` GDExtension
(`codetracer-flame-demo`) is built against `godot-cpp` `master` @ `82c6c44`,
whose `extension_api.json` header reads `Godot Engine v4.7.stable` — the 4.7
GDExtension ABI. A 4.6.2 engine cannot load it, which blocked the demo's HCR
beats. `godot-cpp` has no `4.7.x` release branch or tag (only `10.0.0-rc1` /
`10.0.0-rc2`), so `master` is the correct match and the pin is left alone.

**Nothing in this file is read by any verifier.** It records the seams, the
measurements and the three places where upstream changed a construct rather than
moving it, so that a later reader can tell a re-applied hook from a
mechanically-merged one.

---

## 1. Merge, not rebase

`git merge --no-commit --no-ff up-4.7.2` on branch `codetracer`.

This is a product-adapted third-party fork, so per
`metacraft-dev-guidelines/policies/branching-policy.md` upstream is merged INTO
the product-named adaptation branch. That also buys one conflict-resolution pass
instead of 42.

**The merge base is `4.6-stable` (`89cea14398`), not `4.6.2-stable`**, and that
is the single most surprising fact about this merge. `4.6.2` is a maintenance
release on Godot's `4.6` branch; `4.7.2` descends from `master`, which left the
`4.6` line at `4.6-stable`. So a three-way merge sees every 4.6.2 maintenance
cherry-pick as a change made by *us*, and conflicts wherever 4.7.2 carries a
differently-shaped forward-port of the same fix.

| | count |
|---|---|
| conflicted files | **113** |
| … of which files this fork actually modifies | **1** (`modules/gdscript/gdscript_vm.cpp`) |
| … of which are 4.6.2-maintenance-vs-4.7.2 artifacts | 112 |
| upstream-deleted files still unmerged (`UD`) | 9 |
| staged files in the merge | 6072 |

The 112 were resolved by taking **stock 4.7.2 verbatim** (`git checkout up-4.7.2
-- .`), because this fork does not touch any of them and 4.7.2's tree is
authoritative for them. The 9 `UD` files (8 `accesskit` dynwrappers +
`drivers/metal/metal_objects.mm`, plus `doc/translations/de.po`) were removed —
all are genuinely absent from 4.7.2's tree. Seven stale
`thirdparty/jolt_physics/patches/*.patch` files that 4.6.2 added and 4.7.2
renumbered were removed for the same reason.

The nine files this fork modifies were then re-merged with **`up-4.6.2` as the
base** (`git merge-file`), which is the correct base for *our* hunks and which
reduces the conflict set to the three below.

**The invariant this leaves, and it is checkable:** the merged tree is exactly
stock `4.7.2` plus this fork's added files and its 9 modified files.

```
$ git diff --cached --name-status up-4.7.2 | awk '{print $1}' | sort | uniq -c
    130 A
      9 M
```

(129 pre-existing additions plus this file, which the upgrade itself adds.)

`.gitignore`, `SConstruct`, `modules/gdscript/SCsub`, `modules/gdscript/gdscript.cpp`,
`modules/gdscript/gdscript_vm.cpp`, `modules/gdscript/register_types.cpp`,
`platform/linuxbsd/SCsub`, `platform/linuxbsd/godot_linuxbsd.cpp`,
`platform/linuxbsd/os_linuxbsd.cpp` — eight of the nine merged with zero
conflicts and were verified hook-by-hook against stock 4.7.2 rather than assumed.

---

## 2. The three conflicts, all in `gdscript_vm.cpp`

### 2.1 `LOAD_INSTRUCTION_ARGS` — formatting only

Upstream reflowed the macro's continuation backslashes (column-aligned →
single-space). Our `CT_TRACE_ASSIGN` macro sits immediately above it. Resolved by
keeping upstream's reflow and re-inserting `CT_TRACE_ASSIGN` in upstream's style.
No semantic content.

### 2.2 / 2.3 The epilogue — **upstream unified the two exit paths into one**

This is the conflict that mattered, and a mechanical resolution here would have
double-reported or lost every return event.

At 4.6.2 the epilogue had **two** `exit_function()` calls in two mutually
exclusive, jointly exhaustive branches, and this fork carried a
`gdscript_trace_return(retvalue)` immediately before each:

```
if (!p_state || awaited) { <hook>; exit_function(); free stack[FIXED..]; }
free stack[0..FIXED];  call_depth--;
if (p_state && !awaited) { completed.emit(...); <hook>; exit_function(); }
```

4.7.2 collapses that into **one unconditional** `exit_function()` placed after
the `completed.emit`, and moves the stack teardown after it (it now deliberately
does not destroy `ADDR_STACK_CLASS`, which is constructed without taking a
reference):

```
if (p_state && !awaited) { completed.emit(...); }
<hook>;
exit_function();
stack[ADDR_STACK_SELF].~Variant();  stack[ADDR_STACK_NIL].~Variant();
for (i = FIXED_ADDRESSES_MAX .. _stack_size) stack[i].~Variant();
call_depth--;
```

So the two hook sites collapse to **one**, at `gdscript_vm.cpp:4089`, immediately
before `exit_function()` at `:4090`. The observable order is unchanged on both
paths, which is why one site suffices:

- normal or suspending exit — `completed.emit` does not run, so the return is
  still reported before anything else in the epilogue, exactly as at 4.6.2;
- await-resume completion — `completed.emit` still runs first, so the resumed
  continuation is still reported before this frame's return, exactly as at 4.6.2.

Placing the hook *after* `exit_function()` would attribute the return to the
caller's frame. The reasoning is repeated in the source at the hook site so it
cannot be "tidied" away.

### 2.4 What did NOT change, which is the load-bearing part

`gdscript_vm.cpp` churned **294+/285−** across the upgrade, which is the number
that makes this seam frightening. Ignoring whitespace it is **53+/44−** — so
roughly four fifths of that churn is pure reformatting, and it has a named cause:
the `.clang-format` rewrite of §9.3 set `AlignEscapedNewlines: DontAlign` and
`AlignTrailingComments: Kind: Never`, which re-flowed every column-aligned macro
continuation and trailing comment in the file. What is left is **23 semantic
hunks**, and they were read individually.

The recorder's whole basis is the per-line seam, and **both halves of it are
byte-identical between 4.6.2 and 4.7.2**:

- the `OPCODE_LINE` handler in `gdscript_vm.cpp` — diffed in full, identical;
  the hook goes after `ip += 2` exactly as before (now `:3969` / `:3975`);
- `GDScriptByteCodeGenerator::write_newline` (`gdscript_byte_codegen.cpp:1828-1835`)
  still emits `OPCODE_LINE` for every source line under
  `should_track_call_stack()`, so the *granularity* did not change either.

No opcode operand encoding changed anywhere in the VM, which is what makes
`CT_TRACE_ASSIGN(m_dst, m_code_ofs)` still correct — it indexes
`_code_ptr[ip + 1 + m_code_ofs]` by hand, so a changed operand layout would
silently resolve the wrong stack slot and mis-attribute every captured value
rather than fail. The histogram of instruction-pointer advances is identical
across the upgrade:

```
4.6.2:  2×"ip += 1"  8×"ip += 2"  25×"ip += 3"  20×"ip += 4"  29×"ip += 5"
        4×"ip += 6"  1×"ip += 7"  1×"ip += 7 + _pointer_size"  2×"ip += 9"
4.7.2:  identical, term for term
```

The remaining semantic churn in this file is elsewhere — `temporary_slots` became a
`Vector<Pair<…>>`, `ADDR_STACK_CLASS` is now constructed without an implicit
ref, `p_state->stack_size` is zeroed to move stack ownership, an autoload lookup
gained a real error message, and a dozen error strings were reworded.

---

## 3. Two breaks upstream forced

### 3.1 `GDScriptParser::ParserError` became a span

`gdscript_parser.h:263-278`. 4.6.2 had `int line = 0, column = 0`. 4.7.2 has
`start_line`, `start_column`, `end_line`, `end_column`.

`modules/gdscript/gdscript_ct_trace.cpp`'s GDH-M8 pre-check
(`ct_gdscript_content_compiles`) read `e->get().line` in two places and stopped
compiling. Re-applied as `e->get().start_line`.

`start_line` is not merely the surviving spelling — it is the field
`GDScript::reload()` itself passes to `_err_print_error` in both of its error
branches (`gdscript.cpp:825` and `:840`) and to `debug_break_parse` (`:822`,
`:835`). GDH-M8b's gate reads the engine's own `"Parse Error: … at
res://<path>:NN"` **and** the refusal's `detail` on the same failure, so the two
must name one line and it must be the engine's. Reporting `end_line` would have
compiled, run, and quietly disagreed.

**This break is invisible to the stock build.** The whole GDH-M5/M8 reload path
is inside `#if defined(CT_HCR_AGENT_ENABLED)` (`gdscript_ct_trace.cpp:1593-3201`),
which only the `hcr_patchable` build defines — so `target=template_debug` built
and linked clean with `.line` still present, and only the patchable build failed.
Correspondingly, the fix left the stock object **byte-identical**: scons
recompiled the translation unit and then declined to relink.

### 3.2 `-fsanitize=address` now requires `ASAN_ENABLED`, or the build refuses

Godot 4.7 added a guard to `core/typedefs.h` (**`:65-67`**) that did not exist at
4.6.2:

```c
#if (GD_HAS_FEATURE(address_sanitizer) || defined(__SANITIZE_ADDRESS__)) && !defined(ASAN_ENABLED)
#error Address sanitizer was enabled without defining `ASAN_ENABLED`
#endif
```

(with the same shape for `LSAN_ENABLED` and `MSAN_ENABLED`.)

GDH-M6's ASan gate turns the sanitizer on for `modules/gdscript` only, through
`CT_GDH6_ASAN=1` in `modules/gdscript/SCsub`, and at 4.6.2 the flag alone was
enough. At 4.7.2 every translation unit in the module now fails at that
`#error`, so the gate could not build at all — `record-and-verify-gdh6-asan.sh`
exited **rc 2** (`DRIVER-FAIL: the ASan engine did not build`) in 22 s, which is
a *driver* failure and correctly not a kill.

Fixed by pairing the define with the flag at the same scope, which is exactly
what upstream does for its own `use_asan` (`platform/linuxbsd/detect.py`:
`env.Append(CPPDEFINES=["ASAN_ENABLED"])` immediately before the
`-fsanitize=address` CCFLAGS):

```python
env_gdscript.Append(CPPDEFINES=["ASAN_ENABLED"])
env_gdscript.Append(CCFLAGS=["-fsanitize=address", "-fno-omit-frame-pointer"])
env.Append(LINKFLAGS=["-fsanitize=address"])
```

Scoped to `env_gdscript` and not the whole tree, matching where the flag is
applied: `ASAN_ENABLED` gates real engine behaviour (allocator hooks, poisoning
helpers), so a translation unit that is not sanitized must not claim to be.

**This is a build-contract change, not a gate weakening** — the gate's assertions
are untouched; it simply could not compile until the fork satisfied the new
contract.

---

## 4. Premises re-verified at 4.7.2 rather than assumed

Two gates rest on a specific upstream behaviour, and a gate that passes because
its fixture stopped triggering the condition is the worst outcome available here.

| premise | 4.6.2 | 4.7.2 | holds? |
|---|---|---|---|
| **GDH-M0** — `core:reload_scripts` accepts a three-element `[cmd, thread_id, data]`, `thread_id = Thread::MAIN_ID` | `remote_debugger.cpp:350-371` | **`:353-374`**, `ERR_CONTINUE(cmd.size() != 3)` at **`:359`** | **yes**, verbatim |
| … `Thread::MAIN_ID == 1` | `core/os/thread.h:72` | `:72` | **yes**, unmoved |
| … the main thread is registered unconditionally | `remote_debugger.cpp:798` | **`:801`** | **yes** |
| … reload is applied during idle poll only | `remote_debugger.cpp:686` | **`:689`** | **yes** |
| **GDH-M8b** — the compiler refuses `self.<prop>` inside that property's own getter | `gdscript_compiler.cpp:802-806` | **`:802-806`** (unmoved) | **yes** |
| … and that refusal is inside `#ifdef DEBUG_ENABLED` | guard at `:801`, `#endif` at `:808` | same lines | **yes** |
| … so the fixture is a compile error in `template_debug` only | `SConstruct` | `:542` `editor_build` false → no `TOOLS_ENABLED`; `:544`/`:563` `debug_features` → `DEBUG_ENABLED` | **yes** |

`test-programs/gdh8/probe_v2_uncompilable.gd` still contains exactly the
construct `gdscript_compiler.cpp:802` refuses (`return
self.gdh8_compiler_only_defect` inside `gdh8_read_the_property`, which is that
property's `get =`), and GDH-M0's reload arm really reloaded — 8 ticks on v1,
16 on v2, exactly one transition.

---

## 5. Constructs that CHANGED rather than moved

Renumbering a citation whose construct was rewritten is worse than leaving it
wrong, so these three are called out rather than silently re-anchored.

1. **`ResourceFormatLoaderGDScript` left `gdscript.cpp`.** 4.7.2 split it into
   the new `modules/gdscript/gdscript_resource_format.{cpp,h}`. The
   `CACHE_MODE_REPLACE`/`p_update_from_disk` claim that was `gdscript.cpp:2910-2911`
   is now `gdscript_resource_format.cpp:41`; the code itself is unchanged.

2. **`GDScript::instances` changed type and the placeholder branch is gone.**
   `RBSet<Object *>` → `SelfList<GDScriptInstance>::List` (`gdscript.h:175`), and
   `_prepare_compilation`'s live-instance migration block shrank from 47 lines
   (`gdscript_compiler.cpp:3046-3092`) to 10 (**`:3066-3075`**) because the
   `#ifdef TOOLS_ENABLED` placeholder re-creation was removed entirely. The cited
   claim — migration via `GDScriptInstance::reload_members()` — still holds; the
   machinery around it does not. This fork touches none of it.

3. **The two return hook sites became one**, §2.2 above. Every citation of the
   4.6.2 pair (`~4006`/`~4025`/`~4030`/`~4049`) now names the single site
   `:4089`.

---

## 6. Where the GDH anchors went

Verified by opening both trees, not by applying an offset. Every entry below was
read back out of the 4.7.2 tree after editing.

| construct | 4.6.2 | 4.7.2 |
|---|---|---|
| `valid = false;` before the parse | `gdscript.cpp:814` | **`:812`** |
| `err = parser.parse(source, path, false)` | `:819` | **`:818`** |
| parse branch, `return ERR_PARSE_ERROR` | `:822-830` | **`:820-827`** |
| `"Parse Error: "` (parse / analyze) | — | **`:825`** / **`:840`** |
| analyze branch, `return ERR_PARSE_ERROR` | `:846` | **`:844`** |
| `"Compile Error: "` | `:856` | **`:854`** |
| `return ERR_COMPILATION_FAILED` | `:862` | **`:860`** |
| `GDScript::reload` | `:740-905` | **`:738-903`** |
| `_save_old_static_data` / restore, both `#ifdef TOOLS_ENABLED` | `:808-812` / `:892-901` | **`:806-810`** / **`:890-899`** |
| the `_static_init()` early return between save and restore | `:884-889` | **`:883-888`** |
| `GDScriptLanguage::reload_scripts` (body entirely `#ifdef DEBUG_ENABLED`) | `:2420-2556` | **`:2471-2604`** |
| `scr->load_source_code(scr->get_path())` | `:2509` | **`:2558`** |
| `scr->reload(p_soft_reload)` — the dropped `Error` | `:2511` | **`:2560`** |
| `reload_scripts` declared `virtual void` | `gdscript.h:633` | **`:631`** |
| `orphan_subclasses.remove(...)` | `gdscript.cpp:2871` | **`:2928`** |
| the compiler's getter refusal | `gdscript_compiler.cpp:802-806` | **unmoved** |
| `_prepare_compilation` clears members/functions | `:2742-2752` | **`:2735-2745`** |
| `_prepare_compilation(base, …)` (two sites) | `:2794` | **`:2787`**, **`:2809`** |
| `PROPERTY_USAGE_SCRIPT_VARIABLE` | `:2877` | **`:2897`** |
| `get_orphan_subclass(inner_class->fqcn)` | `:3158` | **`:3141`** |
| `GDScriptCache::add_static_script` / `finish_compiling` | `:3333` / `:3336` | **`:3316`** / **`:3319`** |
| `_poll_messages` | `remote_debugger.cpp:350-371` | **`:353-374`** |
| `core:reload_scripts` capture | `:527-530` | **`:530-533`** |
| `poll_events`' two-element inner shape | `:663` | **`:666`** |
| `_core_capture` | `:719-724` | **`:722-727`** |
| `hcr_patchable` profile block (fork-relative) | `SConstruct:643-695` | **`:651-703`** |
| `CT_HCR_AGENT_ENABLED` define | `SConstruct:694` | **`:702`** |
| `template_debug` → `DEBUG_ENABLED` | `SConstruct:534-555` | **`:542-563`** |
| `OVERRIDE_PATH_ENABLED` | `SConstruct:1146-1147` | **`:1171-1172`** |

### 6.1 How many, and how they were checked

**251 citations were re-anchored** across 22 files in three repos — 165
filename-qualified (`gdscript.cpp:814`), 86 bare continuations (`=:2555=`, whose
filename is inherited from the nearest preceding file mention in the same
paragraph), plus a dozen corrected by hand where the original was already
approximate or where a C++ comment's continuation carried no quoting for a
pattern to match.

Files touched: `codetracer-specs/Planned-Features/GDScript-Hot-Reload-Multi-Version-Sources.{md,milestones.org}`,
`codetracer-specs/Recording-Backends/GDScript-Recorder.{md,milestones.org}`,
`codetracer-specs/Planned-Features/Mixed-Trace-GDScript.md`,
`codetracer-specs/Marketing/Home-Demo-Screencast.milestones.org`,
`reprobuild-specs/HCR-{Linux-ELF-Provider,Per-Platform-Handoff}.milestones.org`,
`scripts/EXPECTED-{GDH0,GF1,GF5,GF6,GF7,HCR1}.md`,
`scripts/verify_{gdh0,gdh5,gdh8,gf6,n1}.py`,
`modules/gdscript/gdscript_ct_trace.cpp`, and
`test-programs/gdh8/probe_v2_{bad,uncompilable}.gd`.

**The re-anchoring was then checked by reading the tree back.** Every distinct
`<engine file>:<line>` citation now in those repos — 80 of them — was resolved
against the 4.7.2 sources and its text printed. None is out of range, none lands
on a blank line, and the one that landed on a bare `}` (`gdscript.cpp:819`, a
range start that should have been the `if (err) {` at `:820`) was corrected. A
workspace-wide sweep for the same filenames found no further citing file.

Three audit paragraphs in the hot-reload milestones assert line-level
verification in so many words ("the citations hold — …", "verbatim in the
tree"); all three were re-derived construct-by-construct against 4.7.2 rather
than renumbered, which is how the `parser.parse`, `"Parse Error"` and
`"Compile Error"` anchors in them were found to have been off by one to three
lines *at 4.6.2 already*. Those are now exact.

`modules/gdscript/SCsub` and `platform/linuxbsd/SCsub` did not move at all, so
the citations into them (`SCsub:103-114`, `SCsub:62-75`) are still exact.
`modules/gdscript/gdscript_ct_trace.{cpp,h}`, `gdscript_tracer.{cpp,h}`,
`ct_writer/` and `test-programs/` are fork-local additions the merge did not
touch, so citations *into* them survive; citations *out of* them into engine
files were re-anchored with everything else.

---

## 7. The vendored writer archive

`modules/gdscript/ct_writer/linuxbsd-x86_64/libcodetracer_trace_writer.a` was
rebuilt from `codetracer-trace-format-nim` at HEAD `81a18e5` with the repo's own
`nimble buildStaticLib`, inside its `nix develop` shell. The result is
**byte-identical** to the vendored copy — same 2 222 258 bytes, same
`sha256:b80b6c35…` — so the archive was already current and the build is
reproducible. No re-vendor was needed.

`modules/gdscript/ct_writer/libcodetracer_trace_writer.a`, the macOS-arm64
fallback, is **still stale and cannot be fixed from a Linux host**. It does not
export `trace_writer_current_path_id`, so a macOS build of this fork fails to
link. That is `HX-D-0` in
`reprobuild-specs/HCR-Per-Platform-Handoff.milestones.org`;
`scripts/verify-gdh3-fork.sh` reports it by name on every run rather than
skipping it, and did so here.

---

## 8. Builds

Both from a clean merged tree, `-j12` on a 24-core box whose load average never
dropped below 120 — the wall times are honest measurements of this run, not
benchmarks.

| | stock | patchable |
|---|---|---|
| command | `scons platform=linuxbsd target=template_debug arch=x86_64 module_gdscript_enabled=yes vulkan=no metal=no opengl3=no disable_path_overrides=no` | `scripts/build-hcr-patchable-linux.sh` |
| scons exit code | **0** | **0** |
| wall | **787 s** | **711 s** (scons; 730 s including the profile projection) |
| binary | `bin/godot.linuxbsd.template_debug.x86_64` | `bin/godot.linuxbsd.template_debug.x86_64.hcr` |
| size | **86 544 552** bytes (was 84 781 992 at 4.6.2) | **110 957 064** bytes (was 108 207 848) |
| reports | `4.7.2.stable.custom_build` | `4.7.2.stable.custom_build` |

The patchable profile projected out of the Reprobuild DSL unchanged:
`-fpatchable-function-entry=16,0 -falign-functions=16` /
`-Wl,--build-id=sha1`, agent source
`reprobuild/libs/repro_hcr_agent/c/repro_hcr_agent.c`.

The patchable build was produced **twice** from this tree, in independent runs,
and both came out byte-identical —
`sha256:3a97806fbdba60c3d299a9647c05a0d3710d591d9a380c5b56129be9cd13d63c`,
build-id `ecda4575438b31b58d707011187adbc5e3967f01` — so it is bit-reproducible
here, and "is this the plain engine or an armed one" is a question with a
checkable answer (see §11).

`scripts/verify_hcr_patchable.py` on the new binary — **all checks passed**:

```
ELF type: ET_DYN (PIE)
ok: .note.gnu.build-id present: type=3 20 bytes = ecda4575438b31b58d707011187adbc5e3967f01
ok: .symtab present: 206879 symbols, 155172 defined FUNC (of 155578 FUNC)
ok: .dynsym for comparison: 342 defined FUNC (the pre-HCR ceiling)
ok: __patchable_function_entries present: SHF_ALLOC, addr=0x57bb758, 1069336 bytes = 133667 entries
ok: __start___patchable_function_entries / __stop___ both defined
    sled sample: entry[0]   sled@0x7842f2 aligned-window@0x7842f8 (+6) sled_bytes=9090…(16)
    sled sample: entry[334] sled@0x7a7120 aligned-window@0x7a7120 (+0) sled_bytes=9090…(16)
    sled sample: entry[668] sled@0x7b7440 aligned-window@0x7b7440 (+0) sled_bytes=9090…(16)
ok: sampled 400 sleds across the whole section: every one is 16 NOP bytes with an
    8-byte-aligned window inside it
ok: in-target HCR agent linked, Linux x86_64 arm compiled in
info: .note.gnu.property present (64 bytes) but carries NO IBT/SHSTK -> no endbr64,
      sled starts at the entry label
```

The `.dynsym` ceiling is still **342** defined FUNCs, exactly as
`build-hcr-patchable-linux.sh`'s header records for 4.6.2, against **155 172**
defined FUNCs in `.symtab`. So the reason the patchable profile has to exist —
that a stripped engine offers the provider 0.2 % of its functions and not `main`
— is unchanged at 4.7.2.

---

## 9. Gates re-established at 4.7.2

Every gate below was re-run on the 4.7.2 binaries. Evidence recorded against
4.6.2 elsewhere in the campaign is left in place as the historical record; these
are the 4.7.2 numbers.

| gate | driver | result |
|---|---|---|
| GDH-M3 (fork side) | `scripts/verify-gdh3-fork.sh` | **22 checks, 0 failures** |
| GT1 no-silent-skip | `scripts/verify-corpus-no-silent-skip.sh` | **passed** (18 programs discovered, all verified, all > 0 steps) |
| GT1 corpus | `scripts/record-and-verify.sh` | **18 / 18 passed**, G2 … GF13 + N1 |
| GDH-M0 | `scripts/record-and-verify-gdh0.sh` | **OK** — G0a green on the reload arm (16 assertions; 8×v1 → 16×v2, 1 transition) and on the control arm (9), **red on both falsifier arms** |
| GDH-M1 (writer) | `codetracer-trace-format-nim/tests/run_gdh1_gates.sh` | **green, 9/9 arms red** |
| GDH-M2 (writer) | `…/run_gdh2_gates.sh` | **green, 9/9 arms red**, no-reload recording byte-identical |
| GDH-M3 (writer) | `…/run_gdh3_gates.sh` | **3/3 gates green, 3/3 arms red** |
| GDH-M5 | `scripts/record-and-verify-gdh5.sh` | rc **0** — **2 of 2 gates green, 2 of 2 arms red, 0 failures**. Run twice: before the restore-trap change, and again after it (272 s) to prove the change did not break the driver |
| GDH-M6 | `scripts/record-and-verify-gdh6.sh` → `verify_gdh6.py --gate all` | unmutated run **98 assertions over 7 gates, 0 red**; **8 of 9 arms red**; **1 ARM-FAIL** — `CT_GDH6_FALSIFY_STRING_KEYED_BUNDLE`, which is pre-existing falsifier rot, see §10. Driver rc **1** because of it, correctly. 790 s |
| GDH-M8 / M8b | `scripts/record-and-verify-gdh8.sh` + `verify_gdh8.py --gate all` | rc **0**, 2042 s. Unmutated: **169 assertions over 6 gates, 0 red** (preconditions 11, refused 33, digest 34, close 22, compile 34, line-table 35). **2 unmutated runs green** (the second is the injection-hook inertness pair). **10 of 10 arms red, 0 failures** |
| GDH-M6 ASan | `scripts/record-and-verify-gdh6-asan.sh` | rc **0**, 237 s — **10 assertions, 0 failures**; ASan silent over the unmutated run, and the `CT_GDH6_FALSIFY_RAW_POINTER_QUEUE` arm **killed by ASan's own report** (`stack-use-after-return`, stack naming the reload request path), which is the designed kill rather than a crash. First attempt was rc **2** — a *driver* failure, not a kill — because of the new `ASAN_ENABLED` contract (§3.2) |
| HCR demo | `scripts/hcr-patch-godot-linux.sh` | **PASS** — `40 ticks: 8 before the patch (value=24), 32 after (value=4242); transition at tick 9`, `outcome: applied`, `publicationTier: 2`, `oldCodeRetained: true`, peak 29 threads |
| HCR under MCR | `scripts/hcr-patch-godot-under-mcr.sh` | **PASS**, rc **0**, 87 s — the engine was hot-patched *while MCR recorded it* and both survived. 40 ticks, 8 before (24) / 32 after (4242); recording 1 360 999 events, 31 threads, 32 030 720 bytes; **dump proven complete by header arithmetic** (1 360 999 lines == 1 360 999 reported events, not `> 0`); `evCodePatch` in the trace at `tier=tier2-quiesced`, `claimHeld: true`, `bridgePresent: true` |
| flame GDExtension loads | `codetracer-flame-demo` `just gdext` + headless load | **loads** — see §9.2 |

`tests/run_gdh4_gates.sh` does not exist in `codetracer-trace-format-nim` —
only `run_gdh1`, `run_gdh2` and `run_gdh3`.

### 9.1 Notes

- The GT1 corpus is the strongest single signal that the per-line seam and the
  re-applied return hook are correct: GF5 grades captured **return values** and
  GF10 grades `await` suspend/resume, and both are exactly what the
  epilogue-unification touched.
- GDH-M8's arms 4 (`CONTINUE_AFTER_FAILURE`) and 5 (`CLOSE_WITHOUT_REASON`)
  additionally redden the `compile` gate, and arm 6 (`WRITE_BEFORE_COMPILE`)
  additionally reddens `close`. The driver prints this rather than hiding it, and
  it is **pre-recorded baseline behaviour, not new** — the GDH-M8b arm table says
  verbatim *"Arms 4 and 5 additionally redden `compile` and arm 6 additionally
  reddens `close`"*. Checked against that table rather than treated as a finding.
- `gf_threads.gd` (GF12) recorded 61 steps in one run and 55 in another. That is
  thread-interleaving nondeterminism in the *count*; `verify_gf12.py` asserts
  structural properties (≥ 2 distinct non-main thread ids, exactly one `worker`
  frame, exactly one `pool_task` frame) and is count-independent, so both runs
  pass. Not a regression, and not a property that was weakened.

### 9.1b Load, and how a failure was classified

Everything here was measured on a **shared** box whose load average sat between
120 and 206 throughout, driven by other tenants' `nix` processes rather than by
these builds. This campaign has already measured that its lanes are
load-sensitive and that the signature is a **60-second timeout** — a prior
reviewer watched a runner start and the very next arm die at 62 s on
`Timeout after 60.0s waiting for event 'stopped'`, having been correct in the
three rounds before and four after; it was reported as a recorder regression on
`dev` and was not one.

So the rule applied to every result below, decided before the results came in:

- a **timeout-shaped** failure — `Timeout after 60.0s`, rc **124**, a hang arm
  that did not report — is SUSPECT at this load and is re-run before it is
  written down, with both the original and the re-run reported;
- a **genuine assertion** failure — a wrong value, a missing marker, an arm that
  stays green — is not load-sensitive in that way and is reported as-is.

**No timeout-shaped failure occurred in any lane.** The one non-green result,
`CT_GDH6_FALSIFY_STRING_KEYED_BUNDLE`, is of the second kind — an arm that stayed
green — and §10 shows by static comparison against the pre-merge tree that it is
neither load nor the rebase.

### 9.2 The flame GDExtension — the point of the exercise

`cmake -S gdextension -B gdextension/build -DCMAKE_BUILD_TYPE=Release
-DGODOTCPP_TARGET=template_debug -DGODOTCPP_BUILD_PROFILE=…/build_profile.json`
then `cmake --build … -j4`, after `git submodule update --init
third_party/godot-cpp`. Configure rc **0**, build rc **0**, 146 s, output
`bin/libflamefield.macos.template_debug.so` — 2 738 536 bytes, `ELF 64-bit LSB
shared object, x86-64`, exporting `flame_library_init`. (The `.macos.` infix on a
Linux `.so` is deliberate and documented in `flame_field.gdextension`: the
CMakeLists hardcodes it in `OUTPUT_NAME`, and the manifest's `linux.x86_64` entry
must match the real build output.)

`godot-cpp` stays pinned at the committed submodule SHA `82c6c44`, whose
`gdextension/extension_api.json` header reads `Godot Engine v4.7.stable`. There is
no `4.7.x` branch or tag of godot-cpp (`git tag` shows only `10.0.0-rc1` and
`10.0.0-rc2`), so `master` @ `82c6c44` remains the correct match for a 4.7.2
engine and the pin was NOT moved.

**`--import` cannot be done with this fork.** `.godot/extension_list.cfg` is
written by the EDITOR; a `template_debug` export template only reads it. Running
`--import` with the fork produces no `.godot/` at all and the class then resolves
to a placeholder — which looks exactly like a failed extension load and is not
one. The project cache was therefore built with the flame demo's own pinned
editor (`godot4` `4.7.1.stable.nixpkgs.a13da4feb`), whose first pass exits
**134** in the documented upstream `_gen_extensions_docs` headless crash and
whose second pass exits **0** and writes
`extension_list.cfg = res://flame_field.gdextension`.

Loaded into the fork, positively rather than by absence of errors
(`--script res://ct_gdext_probe.gd`, a throwaway probe in a scratch copy of the
project — nothing was added to `codetracer-flame-demo`):

```
Godot Engine v4.7.2.stable.custom_build
CT_PROBE class_exists=true
CT_PROBE parent=MultiMeshInstance3D
CT_PROBE instantiated=true
CT_PROBE class=FlameField
CT_PROBE n_props=73
CT_PROBE n_methods=318
CT_PROBE flamefield_methods=["get_live_count", "get_sim_frame", "_process"]
CT_PROBE VERDICT=PASS
ENGINE_RC=0
```

`get_live_count` and `get_sim_frame` are FlameField's own C++ bindings, so this
is the real extension and not a placeholder: a placeholder reports class
`Node`/`Object` and carries neither method. A plain `--quit-after 45` run of the
project's own main scene on the fork is also clean — rc 0, no "Cannot get class
'FlameField'", no placeholder warning, and none of the downstream
`res://scripts/flame.gd` errors that the placeholder produced.

**The same probe was run against BOTH engines** — `…template_debug.x86_64` and
`…template_debug.x86_64.hcr` — with identical output. The patchable one is the
configuration the demo's HCR beats actually need, so loading into the stock
engine alone would not have answered the question.

**The 4.6.2 blocker is cleared.**

---

### 9.3 Lint and style

There is **no `just lint` in this fork** — it has no `Justfile`; style is enforced by
`.pre-commit-config.yaml` (clang-format v21.1.7 for C/C++, ruff v0.15.8 for
`*.py` / `SConstruct` / `SCsub`). Both were run against the changed files and,
crucially, **against the pre-merge fork HEAD as a baseline**, because the fork's
129 added files were never part of an upstream clang-format pass and carry
pre-existing violations:

| | pre-merge `5206371fda` | merged |
|---|---|---|
| clang-format violations (the 9 C++ files this fork owns/modifies) | 38 | **39** |
| `ruff check` errors (`SConstruct`, both `SCsub`, `scripts/*.py`) | 17 | 17 |
| `ruff format --check` would reformat | 25 | 25 |

**The one extra clang-format violation is upstream's, not this change's.** Godot
4.7.2 rewrote `.clang-format` itself: `IncludeBlocks: Preserve` → `Regroup` with
a new eight-tier `IncludeCategories` scheme, and `AlignEscapedNewlines: Right`
(the LLVM default, previously left commented) → `DontAlign`. The new violation is
`Regroup` asking for a blank line after the main header in
`gdscript_ct_trace.cpp`'s include block — a file whose first 60 lines are
byte-identical to the pre-merge version. Every other hunk clang-format wants in
that file is trailing-comment realignment in pre-existing fork code.

Upstream's new `.clang-format` opens with *"If you change this file, please format
all files of the codebase as part of your PR: `prek run clang-format --all`"*.
Upstream did that for its own tree; this fork's added files were not in that pass.
Reformatting them is a fork-wide decision whose diff would bury the merge, so it
is **owed work, not done here**, and the count above is the honest baseline for
whoever does it.

**This also independently corroborates the conflict-1 resolution of §2.1.**
`AlignEscapedNewlines: DontAlign` is precisely why upstream reflowed
`LOAD_INSTRUCTION_ARGS`'s and `GD_ERR_BREAK`'s column-aligned continuation
backslashes to single-space — so keeping upstream's reflow and matching it in
`CT_TRACE_ASSIGN` was not a cosmetic preference, it was the config's instruction.

`codetracer-specs`' own suite — `just check` (catalog, milestone refs, crosslinks,
citations, repomix paths, stale public caches, plus four self-tests) — is **rc 0**
after the spec edits, with the milestone-ref ratchet still at its 15 pre-existing
ambiguous references and none of them in a file this change touched.

> A CAUTION FOR THE NEXT AGENT, learned the hard way here: this repo's
> `pyproject.toml` sets `fix = true` under `[tool.ruff]`, so a bare
> `ruff check <files>` **rewrites them in place** — it reported "8 fixed" and had
> silently modified five `scripts/*.py`. Use `ruff check --no-fix` when you mean
> to inspect. The five files were restored from the index and verified.

## 10. A falsifier that has stopped discriminating — `CT_GDH6_FALSIFY_STRING_KEYED_BUNDLE`

**NOT caused by the 4.7.2 rebase, and established by measurement rather than
inference.** Recorded here because it was found here, and because the arm it
concerns is the one that proves the bundled-source cache is keyed on the
writer's path id rather than the `res://` string — the property GDH-M3 deleted
`g_ct_next_path_id` to establish.

The arm restores the string-keyed early return in
`gdscript_ct_note_and_bundle_path_locked` (`gdscript_ct_trace.cpp:770-785`), and
the milestone records its expected result as *"RED, control GREEN: 1 raw view for
a file that ran in three versions"*. Measured at 4.7.2, from a starting binary
verified `sha256`-equal to a freshly built plain engine:

```
-- arm string-keyed source bundle (engine: -DCT_GDH6_FALSIFY_STRING_KEYED_BUNDLE)
ARM-FAIL: string-keyed source bundle PASSED gdh6_both_versions_retrievable_end_to_end;
          the mutation did not turn it red
[gdh6] gdh6_both_versions_retrievable_end_to_end: GREEN — 14 assertions
[gdh6] gdh6_both_versions_retrievable_end_to_end [CONTROL: no reload]: GREEN — 14 assertions
```

The arm was genuinely armed — `build-CT_GDH6_FALSIFY_STRING_KEYED_BUNDLE.log`
carries the `FALSIFIER ARM: -D…` line the driver requires, and
`godot-CT_GDH6_FALSIFY_STRING_KEYED_BUNDLE.hcr` is a distinct binary (110 957 264
bytes against the plain 110 957 064). So the mutation is compiled in and has no
effect on the gate.

**Why.** There are now **two** paths that attach a source view, and the arm
mutates only one:

- `gdscript_ct_note_and_bundle_path_locked` — the LAZY bundler, which reads the
  file back off disk at the first step after a reload. This is where the arm's
  early return lives.
- `gdscript_ct_bundle_bytes_locked`, called from the reload sequence
  (`gdscript_ct_trace.cpp:2341-2351`), which bundles **the bytes whose digest the
  reload already verified** and then records the id in `g_ct_bundled_path_ids`
  itself.

GDH-M8 introduced the second path and says so in the source, at the call site:
*"Until GDH-M8 the view was bundled lazily … Recording the id in
`g_ct_bundled_path_ids` is what keeps the lazy path from bundling it a second
time; **that path is still the one that bundles the FIRST version of every
file**"*. So under the arm, version 1 is still bundled by the lazy path and
versions 2 and 3 are bundled by the reload sequence, which the arm never
touches — three raw views, gate green.

**Why this is not the rebase.** `gdscript_ct_bundle_bytes_locked` occurs
identically (3 occurrences) at the pre-merge fork HEAD `5206371fda` and in the
merged tree, and the only NON-COMMENT change the merge made to the whole of
`gdscript_ct_trace.cpp` is the two `ParserError::line` → `start_line` renames of
§3.1:

```
$ git diff --cached 5206371fda -- modules/gdscript/gdscript_ct_trace.cpp \
    | grep -E '^[-+]' | grep -vE '^(\+\+\+|---)' | grep -vE '^[-+][[:space:]]*(//|$)'
-   r_detail += ": line " + itos(e->get().line) + ": " + e->get().message;
+   r_detail += ": line " + itos(e->get().start_line) + ": " + e->get().message;
-   r_detail += ": line " + itos(e->get().line) + ": " + e->get().message;
+   r_detail += ": line " + itos(e->get().start_line) + ": " + e->get().message;
```

and all three bundling functions hash identically across the merge
(`gdscript_ct_note_and_bundle_path_locked` `728365f5…`,
`gdscript_ct_bundle_bytes_locked` `01e018cc…`,
`gdscript_ct_bundle_source_locked` `67dc082d…`, comments stripped).

**This is therefore pre-existing falsifier rot, dating from GDH-M8, and it is
owed work for GDH-M6, not for this base change.** Nothing was adjusted to make
it pass: the arm is reported as ARM-FAIL by its own driver, which is the correct
behaviour, and `record-and-verify-gdh6.sh` exits non-zero because of it. The
repair is to arm BOTH bundling paths (or to move the arm to
`gdscript_ct_bundle_bytes_locked`, which is now the one the reload gate actually
exercises) — a GDH-M6/M8 change with its own review, deliberately not made here.

## 11. A hazard found while re-running the gates (not a 4.7.2 issue)

`record-and-verify-gdh6.sh` copies the plain engine aside and calls
`restore_plain()` after each falsifier arm — but the restore is a normal
statement, not a `trap`. If the driver is killed (this run hit the harness's
10-minute foreground ceiling) **while an armed build is linking, the orphaned
`scons` finishes and leaves an ARMED engine in `bin/`**, and the next gate to run
picks it up silently. Observed here: a binary of 110 946 824 bytes at the
`bin/` path, 10 232 bytes smaller than the plain 110 957 064, which an unarmed
rebuild then had to recompile all 21 `modules/gdscript` translation units to
undo — the tell that the objects carried a `-D`.

Two things made it visible rather than silent, and both are worth keeping:

- the driver's own `$OUT/godot-plain.hcr` snapshot, taken before any arm, gives a
  byte-for-byte oracle for "is this the plain engine";
- the patchable build is bit-reproducible, so `sha256` of a fresh build is a
  second independent oracle.

Every gate in §9 was run against a binary checked `sha256`-equal to `3a97806f…`
first — **by hand, between driver invocations.** Corrected 2026-09-13 by this
base change's review, which grepped for the check rather than trusting this
sentence, whose earlier wording ("that check is reported per-gate rather than
asserted once") read as if the harness did it: **no driver asserts, or even
computes, a `sha256` of the engine binary.** `3a97806f…` and `bb095e57…` appear
in the drivers only inside comments; the only `sha256sum` calls in a gate driver
are `record-and-verify-gdh8.sh:292-293`, which hash the two inertness
*containers*; and `verify_gdh6.py` / `verify_gdh8.py`'s `hashlib.sha256` hashes
fixture source and line tables. The one binary property the harness does assert
is `nm -D | grep -c '__asan_'` at `record-and-verify-gdh6-asan.sh:332`, once per
run and only under `REBUILD=1`. Making it an assertion is owed work, tracked with
the `setsid` item in
`codetracer-specs/Planned-Features/GDScript-Hot-Reload-Multi-Version-Sources.milestones.org`
— it belongs on `$PLAIN` immediately after the unconditional `cp -f "$BIN"
"$PLAIN"`, which is the one surviving route by which an armed engine can still be
snapshotted and then "restored" with a success log.

**Fixed here.** `record-and-verify-gdh8.sh` already carried `trap
ct_gdh8_restore_bin_on_exit EXIT INT TERM` (added earlier in this campaign);
`record-and-verify-gdh5.sh`, `record-and-verify-gdh6.sh` and
`record-and-verify-gdh6-asan.sh` carried **no trap at all**. All three now carry
one in GDH-M8's idiom, plus the byte-exact `$PLAIN` snapshot that GDH-M5's and
the ASan driver also lacked. No gate logic was touched — the change is a
snapshot, a handler and a `trap` line — and each driver was re-run afterwards
(§9) so the addition is not taken on trust.

The residual mid-rebuild gap is left open and documented in the inserted comment:
the handler restores, and an orphaned `scons` that outlives the shell then
finishes linking the armed binary *after* it. Closing that needs the armed build
moved into its own process group and stopped by the handler — a restructuring of
the build invocations in three drivers, which is not something to do blind on a
base change.

### 11.1 The trap I added to the ASan driver was itself wrong, and the per-gate hash check is what caught it

Recorded because it is the most instructive thing that happened here, and because
it was a defect I INTRODUCED while fixing a different one.

`record-and-verify-gdh6-asan.sh` gained the snapshot-and-trap of §11 with
`PLAIN="$OUT/godot-plain.hcr"` — copied from GDH-M6's driver, where that name is
correct. **In the ASan driver that name is already taken.** Its first build is
labelled `plain`, meaning "ASan but unmutated", and `build_asan` copies that
engine to `$OUT/godot-$label.hcr`. So the sequence was:

1. my snapshot wrote the real un-sanitized engine to `$OUT/godot-plain.hcr`;
2. `build_asan plain` overwrote it with the **sanitized** engine;
3. the driver's own final restore rebuilt correctly — 21 `modules/gdscript`
   translation units, and `build-restore.log` contains zero asan lines, so it
   genuinely produced an un-sanitized engine, and it said so;
4. **my exit trap then copied the sanitized snapshot over it**, reporting
   "restored the plain engine … (it was armed)" while doing the opposite.

The gate itself passed — rc 0, 10 assertions, 0 failures, ASan silent on the
unmutated run and the raw-pointer arm killed by ASan's own report. Had the gate
been the only thing checked, this would have shipped a sanitized 116 819 232-byte
engine at the shared `bin/` path, wearing a log line claiming it had been
restored. The thing that caught it was the **`sha256` assertion around every
gate**: `bb095e57…` where `3a97806f…` was required, and `nm -D | grep -c
__asan_` returning 39.

Fixed by renaming the snapshot to `$OUT/godot-unsanitized-original.hcr`, which
cannot collide with any `$label`, with the reason recorded at the assignment.
GDH-M5's and GDH-M6's snapshots were checked for the same collision and do not
have it: GDH-M5's arm artefacts are `godot-$define.hcr` (`CT_GDH5_FALSIFY_*`),
and GDH-M6's `PLAIN` was already `$OUT/godot-plain.hcr` before this change with
arm artefacts named `godot-$defines.hcr` — `record-and-verify-gdh6.sh:295` calls
`build_armed "$defines" "$defines"` and `build_armed` ends in `cp -f "$BIN"
"$OUT/godot-$dest.hcr"`, so the names are `godot-CT_GDH6_FALSIFY_*.hcr`.
(Corrected 2026-09-13: this read `attribution` / `retrievable` / `discoverable`,
which are the verifier's `--gate` selectors, not artefact names. The conclusion —
no collision with `plain` — is unaffected either way.)

**The general lesson, and it cuts against the fix in §11:** a restore mechanism
copied between drivers inherits the first driver's namespace assumptions. "Restore
from a snapshot" is only as good as the snapshot's provenance, and a trap that
reports success while restoring the wrong bytes is worse than no trap — it
converts a detectable mess into a claim. The independent oracle (a hash against a
freshly built engine) is what makes either safe.

---

## 12. What was NOT done

- The macOS-arm64 writer archive (`HX-D-0`) — impossible from this host.
- No milestone `:status:` was advanced. This is a base change, not new
  capability.
- Nothing was committed. The merge is left staged with `MERGE_HEAD` set to
  `ed1daf0bf0` so the merge topology is preserved for whoever commits it.
