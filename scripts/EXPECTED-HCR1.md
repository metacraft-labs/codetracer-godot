# EXPECTED-HCR1 — first-principles facts for the HCR1 live-patch demo

Companion to `scripts/hcr-patch-godot-linux.sh` and
`scripts/hcr-patch-godot-under-mcr.sh`, in the same spirit as the
`EXPECTED-G*.md` / `EXPECTED-GF*.md` files: every value the drivers assert is
derived HERE, by hand, from the sources — so an assertion that agrees with the
script but disagrees with reality is visible.

## What HCR1 is

A **real Godot engine function replaced while the engine is running**, with no
restart and no reload of anything. Not a plugin reload, not a script reload —
the engine's own native `.text` is rewritten in place by the Reprobuild Linux
ELF HCR provider (HLX-M0/M1) and the change is observed on the engine's stdout.

## The target

`CoreBind::OS::get_processor_count() const`, mangled
`_ZNK8CoreBind2OS19get_processor_countEv`.

> The namespace is `CoreBind`, not `core_bind`. Godot 4.6 renamed it, and this
> file said `core_bind` until the symbol was looked up in the built binary and
> found not to exist. The drivers do not accept a typed-in name for exactly this
> reason: they resolve the symbol out of the engine's own `.symtab` with `nm`
> and abort unless the match is UNIQUE. `OS::get_processor_count` — the virtual
> this one forwards to — would have been the worse choice: it carries a
> `.localalias` STB_LOCAL symbol at the same address, which is the ambiguity
> HLX-M1's disambiguation rule exists to refuse.

Defined at `core/core_bind.cpp:623` (namespace `CoreBind`):

```cpp
int OS::get_processor_count() const {
	return ::OS::get_singleton()->get_processor_count();
}
```

Bound to GDScript at `core/core_bind.cpp:765`:

```cpp
ClassDB::bind_method(D_METHOD("get_processor_count"), &OS::get_processor_count);
```

Three properties make it the right target, and each had to hold:

1. **It cannot be inlined at the call site.** `bind_method` takes its address
   and stores it in a `MethodBind` as a member-function pointer; the VM calls
   it through that pointer. So the out-of-line body that gets patched is the
   body that actually executes. (A function GCC had inlined into its caller
   would be patched successfully and change nothing — a false negative that
   would look like a provider defect.)
2. **`int` return in EAX, `this` in RDI.** A patch body of `int f(void)` is
   ABI-compatible: ignoring an incoming register argument is always safe on
   SysV x86_64.
3. **Its unpatched value is a fact about the host.** A run that printed the
   patched value from the first tick would be visibly wrong, not plausibly
   right.

## The patch bodies

`test-programs/hcr1/hcr1_patch_bodies.c`, compiled `-O2 -ffunction-sections`.
Neither body carries a relocation in its own section, which is what lets the
provider copy it into a page it owns and jump to it. Verified with
`readelf -rW`: the only relocations in the object are in `.eh_frame`.

| symbol | bytes | disassembly | length |
|---|---|---|---|
| `hcr1_patch_processor_count` | `b8 92 10 00 00 c3` | `mov $0x1092,%eax; ret` | 6 |
| `hcr1_patch_gettid` | `b8 ba 00 00 00 0f 05 31 c9 45 31 db c3` | `mov $0xba,%eax; syscall; xor %ecx,%ecx; xor %r11d,%r11d; ret` | 13 |

`0x1092 = 4242`. `0xba = 186 = __NR_gettid` on x86_64.

The drivers do **not** hardcode 4242: `hcr-patch-godot-linux.sh` reads the
immediate back out of the compiled object with `objdump` and asserts against
that, so the assertion cannot agree with the script while disagreeing with the
bytes that were actually sent.

## MODE=demo — `test-programs/hcr1/hcr1_live_patch.gd`

40 ticks, 250 ms apart, so the run lasts 10 s. The coordinator patches once the
engine has PRINTED `CT_HCR_TICK=8 ` — a marker read out of the engine's own log,
not a timer. That distinction is load-bearing and was found by measurement: under
`ct-mcr record` the engine's time-to-first-output goes from ~0.1 s to **16–18 s**
on this host, so the fixed 3 s delay this file originally specified published the
patch before tick 1 and produced 40 post-patch observations with no before-half
to compare them against. The driver now DIES WITHOUT PATCHING if the marker never
appears, rather than falling through to a patch nobody can interpret.

Two independent measurements of that startup cost, both on this host, both
timestamping the engine's own stdout from process start:

| | time to first output | tick 1 → tick 40 |
|---|---|---|
| unrecorded | 0.089 s / 0.231 s | 9.756 s / 9.766 s |
| under `ct-mcr record` | 16.210 s / 17.693 s | 9.777 s / 9.773 s |

Each cell gives the original measurement and the review's re-measurement. The
**shape** is the reproducible result and is what should be quoted: recording
costs roughly **+16–18 s, essentially all of it in engine startup**, and the
steady-state loop is unaffected to within a few milliseconds. The absolute
startup figure moves with host load — this box was carrying an unrelated load
average of 60–75 during the re-measurement — so no single decimal figure for it
is reproducible, while the ~9.77 s ticking figure is.

Each tick prints:

```
CT_HCR_TICK=<n> CT_HCR_VALUE=<v>
```

Derived expectations:

* `v == nproc` for every tick before the patch (the host's real processor
  count, read independently by the driver from `nproc`);
* `v == 4242` for every tick after;
* the value changes **exactly once** across the whole run. A published entry
  patch is a single aligned store into a stable window; a value that flipped
  back would mean the entry did not stay patched.
* both sets are non-empty. A run in which every tick already showed 4242 would
  not distinguish a hot patch from a build-time change, and is a CHECK-FAIL
  rather than a pass.

## MODE=threads — `test-programs/hcr1/hcr1_thread_reach.gd`

Four engine `Thread`s plus the main thread, each calling the target 30 times at
100 ms intervals, with the `gettid` body published underneath them once the
engine has printed `worker=3 iteration=8 `. Each line:

```
CT_HCR_THREAD worker=<id> iteration=<n> value=<v>
```

`v` is the host processor count before the patch and the **kernel thread id of
the calling thread** after it. So the set of distinct large values is exactly
the set of kernel threads that executed the patched body — measured from inside
the process, on a host with neither gdb nor perf.

Values are split on plausibility rather than a hardcoded boundary: a Linux tid
is > 1000 on any booted system, and no host here has that many cores.

This run is deliberately the **unsafe** case. HLX-M0 through HLX-M3 do not
quiesce threads, so publishing while four threads call the target is exactly the
hazard HLX-M4 exists to close. That it is observable is the measurement; that it
is safe is not claimed.

## What the engine must be, for any of this to mean anything

Both drivers run `scripts/verify_hcr_patchable.py` against the engine first and
stop if it fails. The default `bin/godot.linuxbsd.template_debug.x86_64` fails
all of its checks — no `__patchable_function_entries`, no `.symtab`, no
`.note.gnu.build-id` — so it must be rebuilt with
`scripts/build-hcr-patchable-linux.sh`, which produces
`bin/godot.linuxbsd.template_debug.x86_64.hcr` and links the in-target agent.

## Measured, 2026-09-10

Engine `bin/godot.linuxbsd.template_debug.x86_64.hcr`, build-id
`51f32b2bb2e5224158c25ade179ff2450f9bf725`, host with `nproc` = 24.

| run | result |
|---|---|
| `MODE=demo` | 40 ticks: **8 showing 24, then 32 showing 4242**, one transition at tick 9. Engine exit 0. |
| `MODE=threads` | 150 observations: 45 showing 24, then **5 distinct kernel tids**, 21 observations each. Engine exit 0. |
| under `ct-mcr record` | 40 ticks: 8 showing 24, then 32 showing 4242. Recording valid: 399,583 events, 31 threads, 26 MB. |

Independently re-run by review on the same host and the same binary, all three
reproducing: `MODE=demo` exit 0, *"40 ticks: 8 before the patch (value=24), 32
after (value=4242); transition at tick 9"*, peak 29 threads; `MODE=threads`
exit 0, 150 observations, 5 distinct tids (main + four workers, one tid each),
peak 33 threads; under MCR exit 0, 8 before / 32 after, 399,656 events, 31
threads, 26,021,888 bytes.

**Event and thread counts are per-run, not constants.** The recorder's own
bookkeeping varies between recordings of the same program: 399,583 events in the
first run, 399,656 in the review re-run, 399,598 in an unpatched control. Quote
them as "≈400 k events", never as a fixed figure.

Patch cost, from the driver's own timing: **12–38 ms** from patch request to
`hcr/patchApplied`, covering ELF symbol resolution over 197,655 symbols (the
exact `.symtab` entry count, cross-checked with `readelf -sW`), the linear sled
scan over 130,029 `__patchable_function_entries` entries, the `mprotect` round
trip, the publishing store and the `SYNC_CORE` membarrier.

Placement, from `patchApplied`: entry `0x…f80`, dispatch 32.00 MiB away —
**inside `rel32` range by a factor of 64**, so no island was needed.

### The entry address is resolved at runtime, not baked in

The target's link-time address is `0x3a99f80`. Across four unrecorded runs the
agent reported entries `0x59503d57ff80`, `0x568385975f80`, `0x6388d6fd8f80` and
`0x5cc018f12f80` — four different load biases, each page-aligned, each
`entry - 0x3a99f80`. A baked-in constant cannot do that.

The one run that repeats an address is the one under `ct-mcr record`
(`0x555558fedf80`, bias `0x555555554000`, the fixed no-ASLR base): the recorder
disables address-space randomisation so recordings are deterministic. So the
ASLR argument is evidence from the *unrecorded* runs, and the MCR run's stable
address is expected rather than suspicious.

### Negative controls — the demo must be able to fail

Both were run by review, not just asserted.

1. **The before-half must be observed.** With `PATCH_AFTER` set to a marker the
   engine never prints, the driver exits 1 having published nothing:
   `CT_HCR_VALUE=4242` occurs zero times, all 40 ticks show 24, no
   `patch-result.json` is written, and the runner reports *"the driver itself
   failed (rc=1) — this is NOT a refusal"* rather than a pass.

2. **An unchanged observation must not pass.** With the patch body changed to
   `return 24;` — the host's real `nproc`, i.e. a patch that applies but changes
   nothing observable — the agent still reported `patchApplied` and the run still
   completed, and the demo **FAILED**: *"0 before the patch, 40 after"*, then
   *"every tick already showed the patched value 24; the run does not distinguish
   a hot patch from a build-time change"* and *"the value changed 0 times across
   40 ticks"*. This is the important one: it shows the verdict is driven by the
   observation, not by the agent's own success report.

### What a recording of a patched run does NOT contain

`ct-mcr trace events` produced **one line per event, and exactly as many lines as
`trace info` reported** (399,656 = 399,656), indices `[0]`..`[399655]`, covering
all 31 recorded thread ids. The absence of a code-patch event is therefore read
out of a dump proven both real *and* complete — the runner now asserts that
equality rather than merely `lines > 0`, because a truncated dump is non-empty
and would satisfy a bare grep just as well as a good one.

Cross-checked against a control recording of the same program with the agent
inert (no `REPRO_HCR_AGENT_SOCKET`, so no patch): 399,598 events, 30 threads, no
`4242`. **The set of event kinds in the two traces is identical** — patching
introduces no event kind of its own. The only structural difference is one extra
thread, which is the agent's, and it is present whenever the agent is configured
whether or not a patch is ever published.

So: nothing in the container records that the process's text changed, and a
replay would reproduce pre-patch behaviour for the whole run — silently wrong
after the patch point. That is HLX-M7.

## A practical constraint worth knowing

The driver's Unix socket lives in the output directory, and `sun_path` is 108
bytes. Passing a long output directory makes the driver abort with
`socket path too long` before anything is patched. The runner reports it and
stops (it does not fall through to a run with no coordinator), but if you are
choosing an output directory, keep it short.
