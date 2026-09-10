# CodeTracer HCR demo — HCR1-T: which kernel threads execute a patched function.
#
# This program answers a question the HLX-M4 milestone exists for and that no
# amount of reading the source can settle: when a Godot engine function is
# hot-patched, does the patched body run on ONE thread or several?
#
# HOW IT IS MEASURED, given that this host has neither gdb nor perf. The patch
# body is not `return 4242` here; it is a two-instruction `gettid(2)`:
#
#   b8 ba 00 00 00    mov  $186,%eax        ; __NR_gettid
#   0f 05             syscall
#   c3                ret
#
# So once the patch is published, `OS.get_processor_count()` returns the KERNEL
# THREAD ID OF WHOEVER CALLED IT. Every line this program prints therefore names
# the thread that executed the patched body, and the set of distinct values is
# the answer — measured from inside the process, with no debugger, no ptrace and
# no instrumentation of the engine.
#
# `gettid` needs no relocation (the syscall number is an immediate), so the body
# is position-independent and can be dropped into a provider-owned page as-is,
# exactly like the `return 4242` body of the main demo.
#
# The worker threads are real `Thread` objects, so the calls come from threads
# the ENGINE created and scheduled, not from anything this test invented at the
# OS level. Each worker calls the target repeatedly while the patch is published
# underneath it — which is also, deliberately, the unsafe case: HLX-M0 through
# HLX-M3 do not quiesce threads, so this run is expected to be observable AND
# is not expected to be safe. Both facts are the measurement.

extends MainLoop

const WORKERS := 4
const ITERATIONS := 30
const ITERATION_DELAY_MSEC := 100

var threads: Array = []

func _worker(index: int) -> void:
	for i in range(ITERATIONS):
		print("CT_HCR_THREAD worker=%d iteration=%d value=%d" % [index, i, OS.get_processor_count()])
		OS.delay_msec(ITERATION_DELAY_MSEC)

func _init():
	for i in range(WORKERS):
		var t := Thread.new()
		t.start(_worker.bind(i))
		threads.append(t)

func _process(_delta):
	# Also call it from the main thread, so the main thread appears in the
	# measured set if and only if it really executes the patched body.
	for i in range(ITERATIONS):
		print("CT_HCR_THREAD worker=main iteration=%d value=%d" % [i, OS.get_processor_count()])
		OS.delay_msec(ITERATION_DELAY_MSEC)
	for t in threads:
		t.wait_to_finish()
	return true
