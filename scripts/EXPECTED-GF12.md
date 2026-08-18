# EXPECTED — GF12 (Threads: Thread / Mutex / Semaphore / WorkerThreadPool + writer thread-safety)

Hand-derived, first-principles facts for `test-programs/gdscript/gf_threads.gd`,
asserted by `scripts/verify_gf12.py` against the real `.ct` produced by the
patched engine and decoded with `ct-print --full`. Written BEFORE running so the
verifier tests the recorder, not the other way round.

## What GF12 proves

GDScript run on a worker thread executes `GDScriptFunction::call` ON THAT OS
THREAD, so the recorder's step/call/return/value hooks fire CONCURRENTLY from
multiple OS threads into the single shared CTFS writer + reused CBOR encoder +
type-id maps. GF12 makes that CORRECT:

1. **Thread-safety** — every emit is serialized under one mutex (writer, encoder
   and type maps are all covered), so concurrent hooks cannot corrupt the trace.
   BEFORE the fix (measured on the GF11 binary) the threaded program CRASHES
   (SIGSEGV/SIGABRT) or HANGS in ~40% of runs; AFTER the fix 0/12 fail.
2. **Thread attribution** — the recorder emits the writer's thread-lifecycle
   events (`ThreadStart` / `ThreadSwitch`, from
   `trace_writer_register_thread_start/_switch`) keyed by `Thread::get_caller_id()`
   (main == `Thread::MAIN_ID` == 1, each worker a unique id). A worker's
   steps/calls/values are therefore attributed to a CodeTracer thread id DISTINCT
   from the main thread — the interleaved single-exec-stream model the trace
   format uses (the same model BEAM uses: one process == one thread).

## The program (`gf_threads.gd`)

- `extends SceneTree`. `_initialize` spawns a classic `Thread` running
  `worker(100)` and submits a `WorkerThreadPool.add_task(pool_task)`, then JOINS
  both (`sem.wait()` for the deterministic hand-off, `wait_to_finish`,
  `wait_for_task_completion`) before `quit()`. So the recording is COMPLETE.
- `add_one(x) -> int` (`var r := x + 1` on line 51) is the leaf helper called on
  BOTH worker threads, never on main.
- `worker(base)` loops `range(WORKER_ITERS=5)` doing `total = add_one(total)`
  (total 0 → 5), then `mutex.lock(); counter += total; mutex.unlock(); sem.post()`.
- `pool_task()` loops `range(POOL_ITERS=3)` (total 0 → 3), then the same
  mutex-guarded `counter += total`.
- Final `counter == 5 + 3 == 8` — deterministic even though the interleaving is
  not — printed as `CT_GF12_COUNTER=8` and `CT_GF12_RESULT=8`.

## Deterministic facts (asserted every run; stable across ≥5 runs)

- `add_one` is called **exactly 8 times** (5 from `worker` + 3 from `pool_task`).
  This is the total per-thread work and it is deterministic regardless of
  scheduling — it is the stable invariant the verifier keys on.
- Exactly **1** `worker` frame and **1** `pool_task` frame.
- Thread ids: the main thread is `1`; `worker` runs on a non-main thread and
  `pool_task` on a DIFFERENT non-main thread (measured stable as 28 and 5, but the
  verifier asserts only "non-main and distinct", not the exact numbers). So the
  trace carries **≥ 2 distinct thread ids** and at least **1 `ThreadStart`** with
  id ≠ 1.
- `@implicit_new` and `_initialize` frames are attributed to the main thread (1).
- Every `add_one` frame is attributed to a **non-main** thread (add_one is never
  called from main).
- The `Mutex`-guarded `counter` is captured COHERENTLY: every captured `counter`
  value is one of `{0, 3, 5, 8}` (a torn/racy write would produce something else),
  and the final `8` is present.
- Worker-thread VALUE capture: the `r` local of `add_one` (line 51) is captured
  on non-main threads with correct values (each `r ∈ {1,2,3,4,5}`), ≥ 5 of the 8
  present. (A value may VERY rarely be dropped — never misattached — when a
  foreign thread's step splits a worker's step/value pair; this is the documented,
  correctness-preserving `g_ct_pending_owner` guard. STEP counts, call records and
  captured return/counter values are unaffected.)
- Well-formed `.ct`: `ct-print --full` decodes it, every `call_entry` has
  `entry_step ≤ exit_step`, and there are no exception (`sekRaise`) events.

## Honest non-determinism (NOT asserted)

- The main↔worker step INTERLEAVING order, and therefore the exact global
  step indices of any given frame, vary run to run.
- The `parent_call_key` linkage of an `add_one` frame is NOT reliable under
  interleaving: the loader reconstructs depth/parent from a SINGLE call stack, so
  interleaved frames from two threads get cross-linked. The verifier attributes
  frames to threads by the thread active at their `entry_step`, not by parent, and
  even that is occasionally off by a boundary frame — so it asserts the total
  (`add_one == 8`) and thread DISTINCTNESS, not an exact per-thread frame split.
- The exact total step count and whether `_process` fires depend on quit timing.

## Tamper (non-vacuity — each MUST be rejected)

- `wrongaddone` — delete one `add_one` frame → count 7 ≠ 8 → rejected.
- `missingworker` — delete the `worker` frame → worker-frame count ≠ 1 → rejected.
- `threadidmain` — rewrite every thread-event id to 1 (worker/pool appear on the
  main thread) → thread-distinctness broken → rejected. Proves the thread-id
  DISTINCTNESS assertion has teeth.
