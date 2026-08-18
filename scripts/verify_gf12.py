#!/usr/bin/env python3
"""Assert GF12 (Threads) facts against a real .ct produced by the patched engine,
decoded via `ct-print --full`.

`test-programs/gdscript/gf_threads.gd` spawns a classic `Thread` running
`worker(100)` and a `WorkerThreadPool.add_task(pool_task)`, both incrementing a
`Mutex`-guarded `counter`, and JOINS both before `quit()`. Each worker runs
GDScript through `GDScriptFunction::call` ON ITS OWN OS THREAD, so the recorder's
hooks fire concurrently into the single shared writer. GF12 makes that correct:
every emit is serialized under a mutex, and the recorder emits the writer's
`ThreadStart`/`ThreadSwitch` events (keyed by `Thread::get_caller_id()`) so a
worker's steps/calls/values carry a thread id DISTINCT from the main thread.

ATTRIBUTION MODEL (important):
  * A STEP's thread is reliable — the recorder emits a `ThreadStart`/`ThreadSwitch`
    marker and the step it precedes ATOMICALLY under the writer lock, so walking
    the exec stream and tracking the current thread from the markers attributes
    every step exactly. We therefore attribute WORK by STEP LINE NUMBERS.
  * A CALL's `entry_step` is NOT reliable for thread attribution — it is a
    `stepCount` snapshot that another thread may claim at a boundary — so the
    verifier keys call-level facts on COUNTS (which are exact: every call reaches
    the call stream), never on `entry_step`→thread.

THE TEETH (see scripts/EXPECTED-GF12.md for the hand-derived facts):
  - `add_one` called EXACTLY 8x (call count); exactly 1 `worker` + 1 `pool_task`;
  - the worker thread (the non-main thread that runs `sem.post()`, line 64) and the
    pool thread (line 73) are DISTINCT and both NON-main (thread-id distinctness);
  - `add_one`'s body line 51 runs EXACTLY 8 times, split 5 on the worker thread and
    3 on the pool thread — the deterministic per-thread work, stable across runs;
  - main-only lines run on the main thread (1); ≥2 distinct non-main thread ids and
    ≥1 `ThreadStart` (id ≠ 1);
  - the Mutex-guarded `counter` is captured COHERENTLY (every value ∈ {0,3,5,8},
    final 8 present) — no torn/racy write;
  - worker-thread VALUE capture: `add_one`'s `r` (line 51) captured on non-main
    threads with correct values (each ∈ {1..5}), ≥ 5 of 8 present;
  - well-formed `.ct` (decodes; entry_step ≤ exit_step; no exception events).

It EXITS NONZERO on any mismatch.

Usage:
  verify_gf12.py verify <full.json>
  verify_gf12.py tamper <full.json> <mode>
    # wrongaddone | missingworker | threadidmain | workerwork
"""
import json
import sys

MAIN_TID = 1  # Thread::MAIN_ID
EXPECTED_TYPES = ["None", "Int", "Float", "Bool", "String", "Variant", "Object"]

ADD_ONE_LINE = 51        # `var r := x + 1`
WORKER_SEMPOST_LINE = 64  # `sem.post()`   — unique to worker()
WORKER_COUNTER_LINE = 62  # `counter += total` in worker()
POOL_COUNTER_LINE = 73    # `counter += total` in pool_task() — unique to pool
MAIN_ONLY_LINES = (77, 82)  # `t := Thread.new()` / `sem.wait()` — main only

EXPECTED_ADD_ONE = 5 + 3    # WORKER_ITERS + POOL_ITERS
WORKER_ADD_ONE = 5
POOL_ADD_ONE = 3
COHERENT_COUNTERS = {0, 3, 5, 8}
FINAL_COUNTER = 8
R_MIN_CAPTURED = 5          # ≥5 of 8 (rare single drop tolerated; never misattach)


class VerifyError(Exception):
    pass


def load(path):
    with open(path) as f:
        return json.load(f)


def steps_of(doc):
    return sorted((e for e in doc["events"] if e["kind"] == "step"),
                  key=lambda s: s["step_index"])


def calls_of(doc):
    return [e for e in doc["events"] if e["kind"] == "call_entry"]


def is_thread_event(e):
    return e.get("step_kind", "").startswith("sekThread")


def walk_steps(doc):
    """Yield (line, thread_id, step) for each non-thread-event step, tracking the
    current thread from ThreadStart/ThreadSwitch markers (reliable attribution)."""
    cur = MAIN_TID
    for e in steps_of(doc):
        sk = e.get("step_kind", "")
        if sk in ("sekThreadStart", "sekThreadSwitch"):
            cur = e["thread_id"]
            continue
        if sk == "sekThreadExit":
            continue
        yield e.get("line"), cur, e


def frames(doc, fn):
    return [c for c in calls_of(doc) if c.get("function") == fn]


def threads_running_line(doc, line):
    return {tid for ln, tid, _ in walk_steps(doc) if ln == line}


def verify(doc):
    # --- 1. types table ----------------------------------------------------
    if doc["types"] != EXPECTED_TYPES:
        raise VerifyError(f"types table mismatch: {doc['types']}")

    # --- 2. call counts: deterministic totals (reliable from call stream) --
    add_one = frames(doc, "add_one")
    worker = frames(doc, "worker")
    pool = frames(doc, "pool_task")
    if len(add_one) != EXPECTED_ADD_ONE:
        raise VerifyError(
            f"add_one must be called exactly {EXPECTED_ADD_ONE} times "
            f"(5 worker + 3 pool), got {len(add_one)}")
    if len(worker) != 1:
        raise VerifyError(f"expected exactly 1 worker frame, got {len(worker)}")
    if len(pool) != 1:
        raise VerifyError(f"expected exactly 1 pool_task frame, got {len(pool)}")

    # --- 3. thread identity via STEPS (reliable) ---------------------------
    # The worker thread is the non-main thread that runs sem.post() (line 64,
    # unique to worker); the pool thread runs pool_task's counter write (line 73).
    wt = threads_running_line(doc, WORKER_SEMPOST_LINE) - {MAIN_TID}
    pt = threads_running_line(doc, POOL_COUNTER_LINE) - {MAIN_TID}
    if len(wt) != 1:
        raise VerifyError(f"could not identify a single worker thread (line 64): {wt}")
    if len(pt) != 1:
        raise VerifyError(f"could not identify a single pool thread (line 73): {pt}")
    worker_tid = next(iter(wt))
    pool_tid = next(iter(pt))
    if worker_tid == MAIN_TID or worker_tid == 0:
        raise VerifyError(f"worker thread is not a distinct worker thread: {worker_tid}")
    if pool_tid == MAIN_TID or pool_tid == 0:
        raise VerifyError(f"pool thread is not a distinct worker thread: {pool_tid}")
    if worker_tid == pool_tid:
        raise VerifyError(
            f"worker and pool_task must be on DISTINCT threads, both {worker_tid}")

    # main-only lines really run on the main thread
    for ln in MAIN_ONLY_LINES:
        tids = threads_running_line(doc, ln)
        if tids and tids != {MAIN_TID}:
            raise VerifyError(f"main-only line {ln} ran off the main thread: {tids}")

    # --- 4. deterministic per-thread WORK: add_one body line 51 ------------
    l51 = [(tid) for ln, tid, _ in walk_steps(doc) if ln == ADD_ONE_LINE]
    if len(l51) != EXPECTED_ADD_ONE:
        raise VerifyError(
            f"add_one body (line 51) must run {EXPECTED_ADD_ONE}x, got {len(l51)}")
    if any(tid == MAIN_TID for tid in l51):
        raise VerifyError("add_one body ran on the main thread (impossible)")
    n_worker = sum(1 for tid in l51 if tid == worker_tid)
    n_pool = sum(1 for tid in l51 if tid == pool_tid)
    if n_worker != WORKER_ADD_ONE:
        raise VerifyError(
            f"worker thread must run add_one {WORKER_ADD_ONE}x, got {n_worker}")
    if n_pool != POOL_ADD_ONE:
        raise VerifyError(
            f"pool thread must run add_one {POOL_ADD_ONE}x, got {n_pool}")

    # --- 5. thread-lifecycle events present + ≥2 distinct non-main threads --
    tevs = [e for e in steps_of(doc) if is_thread_event(e)]
    nonmain_starts = [e for e in tevs
                      if e.get("step_kind") == "sekThreadStart" and e["thread_id"] != MAIN_TID]
    if not nonmain_starts:
        raise VerifyError("no ThreadStart with a non-main thread id was emitted")
    nonmain_threads = {tid for _, tid, _ in walk_steps(doc) if tid != MAIN_TID}
    if len(nonmain_threads) < 2:
        raise VerifyError(f"expected ≥2 distinct non-main threads, got {nonmain_threads}")

    # --- 6. Mutex-guarded counter captured coherently ----------------------
    counter_vals = [
        v["value"].get("i")
        for e in steps_of(doc) for v in e.get("vars", [])
        if v["varname"] == "counter"
    ]
    bad = [c for c in counter_vals if c not in COHERENT_COUNTERS]
    if bad:
        raise VerifyError(f"incoherent counter value(s) captured (racy write?): {bad}")
    if FINAL_COUNTER not in counter_vals:
        raise VerifyError(
            f"final Mutex-guarded counter=={FINAL_COUNTER} not captured; got {counter_vals}")

    # --- 7. worker-thread VALUE capture: add_one's `r` ---------------------
    r_vals = []
    for ln, tid, e in walk_steps(doc):
        if ln != ADD_ONE_LINE:
            continue
        for v in e.get("vars", []):
            if v["varname"] != "r":
                continue
            if tid == MAIN_TID:
                raise VerifyError("add_one `r` captured on the main thread (impossible)")
            rv = v["value"].get("i")
            if rv not in (1, 2, 3, 4, 5):
                raise VerifyError(f"add_one `r` out of range on worker thread: {rv}")
            r_vals.append(rv)
    if len(r_vals) < R_MIN_CAPTURED:
        raise VerifyError(
            f"too few worker-thread `r` values captured: {len(r_vals)} < {R_MIN_CAPTURED}")

    # --- 8. well-formedness ------------------------------------------------
    for c in calls_of(doc):
        if c["entry_step"] > c["exit_step"]:
            raise VerifyError(
                f"call {c.get('function')} entry_step>{c['exit_step']} (malformed)")
    for e in steps_of(doc):
        if e.get("step_kind") == "sekRaise":
            raise VerifyError("unexpected exception (sekRaise) event in trace")

    print(
        "GF12 verify OK: "
        f"add_one x{len(add_one)}; worker@tid{worker_tid} (5x add_one, sem.post) / "
        f"pool_task@tid{pool_tid} (3x add_one) — distinct, both non-main; "
        f"non-main threads {sorted(nonmain_threads)}; {len(nonmain_starts)} non-main ThreadStart; "
        f"add_one body line51 x{len(l51)} split {n_worker}/{n_pool}; "
        f"counter coherent {sorted(set(counter_vals))} (final 8); "
        f"worker-thread `r` captured x{len(r_vals)} ({sorted(r_vals)}); "
        "types [None,Int,Float,Bool,String,Variant,Object]; well-formed")
    return {"worker_tid": worker_tid, "pool_tid": pool_tid,
            "n_worker": n_worker, "n_pool": n_pool}


def _del_first(doc, pred):
    for i, e in enumerate(doc["events"]):
        if pred(e):
            del doc["events"][i]
            return True
    return False


def tamper(doc, mode):
    if mode == "wrongaddone":
        if not _del_first(doc, lambda e: e["kind"] == "call_entry" and e.get("function") == "add_one"):
            raise SystemExit("tamper setup failed: no add_one frame")
    elif mode == "missingworker":
        if not _del_first(doc, lambda e: e["kind"] == "call_entry" and e.get("function") == "worker"):
            raise SystemExit("tamper setup failed: no worker frame")
    elif mode == "threadidmain":
        # Collapse every thread-event id to main: worker/pool work now attributes
        # to the main thread, breaking thread-id distinctness.
        n = 0
        for e in doc["events"]:
            if e["kind"] == "step" and e.get("step_kind", "").startswith("sekThread"):
                e["thread_id"] = MAIN_TID
                n += 1
        if n == 0:
            raise SystemExit("tamper setup failed: no thread events to rewrite")
    elif mode == "workerwork":
        # Drop ONE worker-thread add_one body step so the per-thread work count
        # (5 on the worker thread) no longer holds.
        wt = threads_running_line(doc, WORKER_SEMPOST_LINE) - {MAIN_TID}
        if len(wt) != 1:
            raise SystemExit("tamper setup failed: worker thread not identifiable")
        worker_tid = next(iter(wt))
        # find a line-51 step attributed to the worker thread and delete it
        target = None
        for ln, tid, e in walk_steps(doc):
            if ln == ADD_ONE_LINE and tid == worker_tid:
                target = e["step_index"]
                break
        if target is None:
            raise SystemExit("tamper setup failed: no worker-thread line-51 step")
        _del_first(doc, lambda e: e["kind"] == "step" and e.get("step_index") == target)
    else:
        raise SystemExit(f"unknown tamper mode {mode}")

    try:
        verify(doc)
    except VerifyError:
        print(f"tamper({mode}) correctly REJECTED")
        return
    raise SystemExit(f"tamper({mode}) was NOT caught — verifier is vacuous")


def main():
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    cmd, path = sys.argv[1], sys.argv[2]
    doc = load(path)
    if cmd == "verify":
        try:
            verify(doc)
        except VerifyError as e:
            print(f"GF12 verify FAILED: {e}", file=sys.stderr)
            raise SystemExit(1)
    elif cmd == "tamper":
        tamper(doc, sys.argv[3])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
