# CodeTracer GDScript recorder — GF12 (Threads) reference program.
#
# Deterministic, headless. Run as a Godot main loop:
#   CT_GDSCRIPT_TRACE=<dir> godot --headless --script res://gf_threads.gd
#
# `--script` requires the entry to inherit SceneTree / MainLoop. Everything runs
# synchronously inside `_initialize`, which spawns real OS threads and JOINS them
# before `quit()`, so the recording is complete and deterministic.
#
# WHAT GF12 PROVES (this milestone DOES change the recorder — writer thread-
# safety + thread attribution):
# GDScript run on a worker thread executes `GDScriptFunction::call` ON THAT OS
# THREAD, so the recorder's step/call/return/value hooks fire CONCURRENTLY from
# multiple OS threads into the single shared CTFS writer + encoder. The recorder
# now (a) SERIALIZES every emit under a mutex so the shared writer/encoder/type
# maps cannot race, and (b) emits the writer's thread-lifecycle events
# (ThreadStart / ThreadSwitch) so a worker thread's steps/calls/values are
# attributed to a CodeTracer thread id DISTINCT from the main thread. Godot's
# `Thread::get_caller_id()` (main == 1, each started thread a unique id) is the
# per-step OS thread id supplied to the writer.
#
# TWO worker paths are exercised, plus a `Mutex` guarding a shared counter and a
# `Semaphore` used for deterministic hand-off:
#   A) `Thread.new().start(worker.bind(...))`  — a classic OS thread
#   B) `WorkerThreadPool.add_task(pool_task)`  — a pooled worker thread
# Both increment the SAME `Mutex`-guarded `counter`; both are JOINED before quit,
# so the final `counter` is deterministic even though the interleaving is not.
#
# Deterministic result (hand-derived; see scripts/EXPECTED-GF12.md):
#   worker():    loops range(WORKER_ITERS=5), total = 0->5 via add_one() calls,
#                counter += 5.
#   pool_task(): loops range(POOL_ITERS=3), total = 0->3 via add_one() calls,
#                counter += 3.
#   => counter == 8  => prints "CT_GF12_COUNTER=8" and "CT_GF12_RESULT=8".
# The PER-THREAD work is fully deterministic (fixed loop counts), so each worker
# function's recorded step count is STABLE across runs even though the scheduling
# (and therefore the main<->worker step interleaving) is not.

extends SceneTree

const WORKER_ITERS := 5
const POOL_ITERS := 3

var mutex := Mutex.new()
var sem := Semaphore.new()
var counter := 0

# Leaf helper, called on BOTH worker threads (and never on main). A named local
# `r` is captured on the worker thread, proving worker-thread value capture.
func add_one(x: int) -> int:
	var r := x + 1
	return r

# Body of the classic Thread. Runs entirely on its own OS thread: a for-loop of
# add_one() CALLS (worker-thread call/return nesting), a Mutex-guarded shared
# write, then posts the Semaphore so main can proceed deterministically.
func worker(base: int) -> void:
	var total := 0
	for i in range(WORKER_ITERS):
		total = add_one(total)
	mutex.lock()
	counter += total
	mutex.unlock()
	sem.post()

# Body of the WorkerThreadPool task. Same shape, a different fixed loop count so
# its recorded step count is distinguishable from the Thread worker's.
func pool_task() -> void:
	var total := 0
	for i in range(POOL_ITERS):
		total = add_one(total)
	mutex.lock()
	counter += total
	mutex.unlock()

func _initialize() -> void:
	var t := Thread.new()
	t.start(worker.bind(100))
	var task_id := WorkerThreadPool.add_task(pool_task)
	# Deterministic hand-off: wait for the Thread worker to signal it finished
	# its guarded write before we join, so `counter` is settled.
	sem.wait()
	t.wait_to_finish()
	WorkerThreadPool.wait_for_task_completion(task_id)
	print("CT_GF12_COUNTER=%d" % counter)
	print("CT_GF12_RESULT=%d" % counter)
	quit()

func _process(_delta: float) -> bool:
	return true
