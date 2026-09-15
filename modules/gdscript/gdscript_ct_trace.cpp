/**************************************************************************/
/*  gdscript_ct_trace.cpp — CodeTracer GDScript recorder                  */
/*  (G2 steps, G3 calls/returns, G4 values, GF3 collections,             */
/*   GF4 math/struct/handle Variant types, GF8 member/property writes)   */
/**************************************************************************/
// LINKS the existing CTFS writer (libcodetracer_trace_writer.a); does NOT
// reimplement the format.
//
// Lifecycle mirrors the Python/Ruby recorders:
//   codetracer_trace_writer_init()  -> once (Nim runtime init)
//   trace_writer_new(program, 2)    -> format 2 == CTFS multi-stream
//   begin_metadata/events/paths     -> lifecycle stubs
//   trace_writer_start(path, line)  -> first (pending) step
//   trace_writer_register_step(...) -> per executed line (from OPCODE_LINE)
//   ensure_function_id + register_call / register_return
//                                   -> per GDScriptFunction::call frame (G3)
//   ensure_type_id + ct_value_* + register_variable_cbor
//                                   -> per written NAMED stack slot (G4)
//   finish_* + close                -> serialize .ct (via atexit)
//
// G3 note on ordering: the FIRST hook to fire on the outermost function is its
// call entry (enter_function precedes the first OPCODE_LINE), so the writer is
// created there. trace_writer_start is deferred to the first *step* so the step
// stream is byte-identical to G2 (no synthetic declaration-line step is
// emitted). register_call before the first step is fine — the multi-stream
// writer captures the call's entry_step as the next step index.
//
// G4 note on parallel-indexing: a value is emitted from a write opcode, which
// always executes AFTER that line's OPCODE_LINE (its register_step) and BEFORE
// the next line's OPCODE_LINE. So each register_variable_cbor lands on the
// current step, keeping values.dat parallel-indexed to steps.dat (the
// MaterializedReplaySession invariant). We refuse to emit a value before the
// first step exists (g_ct_started), since a value with no step to attach to
// would desync the parallel index.
// This recorder is a pure CONSUMER of the general GDScriptTracer hook
// (gdscript_tracer.h). It registers a tracer, receives engine-neutral
// StringName/Variant callbacks, and does all recorder-specific work here:
// Variant->CBOR encoding, CTFS writer calls, thread attribution, await
// markers, source bundling. It touches NO gdscript_vm.cpp internals — the hook
// already resolved stack/member slots to declared names before calling us.
#include "gdscript_ct_trace.h"
#include "gdscript_tracer.h"

#include "core/string/ustring.h"
#include "core/variant/array.h"
#include "core/variant/dictionary.h"
#include "core/variant/variant.h"
// §5.2 source bundling: read the recorded `.gd` source text so the `res://`
// virtual path resolves at replay, and mirror the writer's path interning.
#include "core/io/file_access.h"
#include "core/templates/hash_map.h"
// GDH-M8b: `add_error_handler` / `ErrorHandlerList`, used to capture the
// compiler's own message for the `compile-error` refusal's `detail`.
#include "core/error/error_macros.h"

// GF4: math / struct / handle Variant types. Most are pulled in transitively by
// variant.h (it holds a union of every math type), but we include them
// explicitly so this TU is self-documenting about what it encodes.
#include "core/math/aabb.h"
#include "core/math/basis.h"
#include "core/math/color.h"
#include "core/math/plane.h"
#include "core/math/projection.h"
#include "core/math/quaternion.h"
#include "core/math/rect2.h"
#include "core/math/rect2i.h"
#include "core/math/transform_2d.h"
#include "core/math/transform_3d.h"
#include "core/math/vector2.h"
#include "core/math/vector2i.h"
#include "core/math/vector3.h"
#include "core/math/vector3i.h"
#include "core/math/vector4.h"
#include "core/math/vector4i.h"
#include "core/object/object.h"
#include "core/string/node_path.h"
#include "core/templates/rid.h"
#include "core/variant/callable.h" // also declares Signal

// GF12: threads. Thread::get_caller_id() yields a stable per-OS-thread id (main
// == Thread::MAIN_ID == 1, each started Thread / WorkerThreadPool worker a unique
// id) — the thread id supplied to the writer's thread-lifecycle events.
#include "core/os/thread.h"

// GDH-M3: the bundled-source set is keyed by the WRITER'S path id, not by the
// `res://` string — see gdscript_ct_note_and_bundle_path_locked below.
#include "core/templates/hash_set.h"

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring> // GDH-M8: strcmp, for the fault-injection stage name
#include <mutex> // GF12: serialize the shared writer/encoder across worker threads
#include <vector> // MT3: LIFO stack of open native<->VM crossing span ids

// The CTFS writer's C header names a parameter `mutable`, which is a C++
// keyword. Shim it out for this C++ translation unit (we never call that
// entry point here).
#define mutable ct_mutable_param_
#include "codetracer_trace_writer.h"
#undef mutable

static trace_writer_t g_ct_writer = nullptr;
static bool g_ct_inited = false;   // init attempted?
static bool g_ct_disabled = false; // tracing off (env unset or init failed)
static bool g_ct_started = false;  // trace_writer_start emitted the first step?

// GDH-M6: is meta.dat bit 14 (the per-file line-count table) on for this
// writer, and which path STRINGS have been given a recorded size? Declared
// here because `gdscript_ct_ensure_writer` below turns the table on; the long
// note explaining both, and the helpers that maintain them, are further down
// beside the source-bundling code they sit next to.
static bool g_ct_line_count_table = false;
static HashSet<String> g_ct_sized_paths;

// GF12: WRITER THREAD-SAFETY (resolves GDScript-Recorder.md open question #3).
//
// When GDScript runs on a worker thread (Thread.start / WorkerThreadPool.add_task)
// GDScriptFunction::call executes ON THAT OS THREAD, so every hook below can fire
// CONCURRENTLY from multiple OS threads into the SINGLE shared writer + the single
// reused CBOR encoder + the cached type-id maps. The linked CTFS writer
// (libcodetracer_trace_writer.a) is NOT thread-safe: it builds one in-memory
// container with a single shared "pending step" slot (see codetracer_trace_writer_ffi.nim)
// and unsynchronized stream buffers, so concurrent emits race and — measured on
// the GF11 binary — CRASH (SIGSEGV / SIGABRT), HANG, or drop data. The writer's
// own contract is therefore "serialize externally", and it exposes thread-
// LIFECYCLE events (trace_writer_register_thread_start / _switch / _exit) to
// attribute a single interleaved exec stream to distinct CodeTracer threads
// (the BEAM recorder's model: one process == one thread via ThreadStart/Switch/Exit).
//
// STRATEGY: a single global mutex serializes the ENTIRE emit path of every hook
// (writer + encoder + type maps are all covered by the one lock), and the recorder
// emits ThreadStart/ThreadSwitch on OS-thread change so a worker's steps/calls/
// values carry a thread id distinct from the main thread.
//
//   * SINGLE-THREAD BYTE-IDENTICAL: a non-threaded program only ever emits on the
//     main thread, so g_ct_active_thread never changes, NO thread event is ever
//     emitted, and g_ct_pending_owner always equals the caller — the guards below
//     are inert and the main-thread step/value/call streams are unchanged.
//   * NO DEADLOCK: the lock is taken per-hook (never held across a VM opcode, so
//     it can never be held across a user Mutex.lock/Semaphore.wait), and a
//     thread-local reentrancy guard makes any hook that fires while this thread is
//     already inside an emit (e.g. a Variant->String that re-enters GDScript) a
//     no-op instead of a recursive self-lock.
static std::mutex g_ct_mutex;
static thread_local bool g_ct_in_emit = false;

// MT3: native<->VM crossing spans. A crossing = one native->VM->native GDScript
// frame. Each function ENTER (on_call -> gdscript_ct_trace_call) opens a
// "gdscript-frame" crossing via trace_writer_begin_crossing and pushes the
// returned span_id; the matching function EXIT (on_return -> gdscript_ct_trace_return)
// pops it and closes it via trace_writer_end_crossing. Crossings nest and MUST
// close strictly LIFO (the writer errors otherwise), so this stack is opened and
// closed at EXACTLY the same sites the writer's own call/return records are, and
// therefore mirrors the writer's internal call-stack nesting one-for-one. The
// whole emit path is serialized by g_ct_mutex, so this stack needs no separate
// lock. A frame that never returns (process killed mid-frame, an await that never
// resumes) simply leaves its crossing OPEN — the OPEN record was already flushed,
// and atexit close commits it — which is acceptable degradation, not corruption.
static std::vector<uint64_t> g_ct_crossing_stack;

// GF12: thread-attribution state (all guarded by g_ct_mutex).
static bool g_ct_have_active = false;     // has any emit run yet?
static uint64_t g_ct_active_thread = 0;   // OS thread the exec stream is currently attributed to
static uint64_t g_ct_main_thread = 0;     // the first thread that ever emitted (implicit default)
// g_ct_pending_owner is the OS thread whose step currently occupies the writer's
// single pending-step slot. A value hook attaches only when it still owns that
// slot; any thread event flushes the pending step, so it invalidates the owner.
// This keeps values.dat parallel-indexed to steps.dat under interleaving: a value
// is NEVER misattached to another thread's step (it is dropped instead — the
// documented, correctness-preserving behavior for the inherently racy step/value
// window; per-thread STEP counts, call/return records and captured return values
// are unaffected because they do not depend on the shared pending slot).
static uint64_t g_ct_pending_owner = 0;
// Threads for which a ThreadStart has already been emitted (the main thread is
// implicitly "started"). A small fixed set: real programs use a handful of worker
// threads; beyond the cap we fall back to ThreadSwitch (still correct, just no
// ThreadStart marker for that extra thread).
static const int CT_MAX_THREADS = 128;
static uint64_t g_ct_started_threads[CT_MAX_THREADS];
static int g_ct_started_count = 0;

static inline uint64_t gdscript_ct_current_thread() {
	return (uint64_t)Thread::get_caller_id();
}

// GF12: RAII lock + reentrancy guard for a hook body. `engaged` is false when the
// same thread is already inside an emit (reentrancy) — the hook then does nothing,
// avoiding a recursive self-deadlock without changing what is recorded.
struct CtEmitLock {
	bool engaged = false;
	CtEmitLock() {
		if (g_ct_in_emit) {
			return;
		}
		g_ct_in_emit = true;
		g_ct_mutex.lock();
		engaged = true;
	}
	~CtEmitLock() {
		if (engaged) {
			g_ct_mutex.unlock();
			g_ct_in_emit = false;
		}
	}
	CtEmitLock(const CtEmitLock &) = delete;
	CtEmitLock &operator=(const CtEmitLock &) = delete;
};

// GF12: called (while holding g_ct_mutex) at the top of each STEP/CALL/RETURN hook,
// before the writer event it precedes. Emits a ThreadStart/ThreadSwitch when the
// emitting OS thread differs from the one the exec stream is currently attributed
// to, so the following event is attributed to the correct CodeTracer thread. NEVER
// emits a thread event for a single-threaded program (the thread never changes).
// `cur` is the caller's current OS thread id.
//
// Thread events are exec-stream events that FLUSH the writer's pending step, so
// emitting one invalidates g_ct_pending_owner (any half-valued step is committed).
// We only emit once the exec stream has begun (g_ct_started); before that the very
// first step is still pending via trace_writer_start and there is nothing to switch.
static void gdscript_ct_note_thread_locked(uint64_t cur) {
	if (!g_ct_have_active) {
		g_ct_have_active = true;
		g_ct_main_thread = cur;
		g_ct_active_thread = cur;
		return; // first emitter (main): implicit default, no event → byte-identical
	}
	if (cur == g_ct_active_thread) {
		return;
	}
	if (!g_ct_started) {
		// Exec stream not begun yet: just remember the active thread; the first
		// real step (trace_writer_start) will anchor it with no thread event.
		g_ct_active_thread = cur;
		return;
	}
	bool started_before = (cur == g_ct_main_thread);
	for (int i = 0; i < g_ct_started_count && !started_before; i++) {
		if (g_ct_started_threads[i] == cur) {
			started_before = true;
		}
	}
	if (started_before) {
		trace_writer_register_thread_switch(g_ct_writer, cur);
	} else {
		trace_writer_register_thread_start(g_ct_writer, cur);
		if (g_ct_started_count < CT_MAX_THREADS) {
			g_ct_started_threads[g_ct_started_count++] = cur;
		}
	}
	// The thread event flushed the writer's pending step: no thread owns it now.
	g_ct_pending_owner = 0;
	g_ct_active_thread = cur;
}

// G4: a single reused streaming CBOR value encoder + cached type ids. The
// encoder is reset before each value; type ids are interned once on the writer
// (ensure_type_id is idempotent, but caching avoids a lookup per value).
static value_encoder_t g_ct_encoder = nullptr;
static uint64_t g_ct_type_none = 0;
static uint64_t g_ct_type_int = 0;
static uint64_t g_ct_type_float = 0;
static uint64_t g_ct_type_bool = 0;
static uint64_t g_ct_type_string = 0;
static uint64_t g_ct_type_raw = 0;
// GF3: compound-collection types. Arrays and Packed*Arrays encode as a
// `Sequence` ("Array"); Dictionaries encode as a `Sequence` ("Dictionary") of
// key/value `Tuple`s ("Pair") — the Python/Ruby dict pattern (mirrors the Ruby
// recorder's Hash -> Seq-of-Pair encoding in
// codetracer-ruby-recorder .../native_tracer/src/lib.rs).
static uint64_t g_ct_type_seq = 0;
static uint64_t g_ct_type_dict = 0;
static uint64_t g_ct_type_pair = 0;
static bool g_ct_types_ready = false;
// GF3: the compound-collection types are interned LAZILY, on first encounter of
// a collection, so a scalar-only recording's types table is byte-identical to
// G4/GF1/GF2 (it never gains Array/Dictionary/Pair). Mirrors the Ruby recorder's
// inline ensure_type_id.
static bool g_ct_coll_types_ready = false;

// GF3: recursion cap for nested collections. Mirrors the Ruby recorder's
// MAX_STREAMING_DEPTH intent — a sane bound so a cyclic Array/Dictionary
// reference cannot recurse forever. Beyond the cap we fall back to the Raw
// printed form (bounded by Godot's own recursion-guarded Variant->String).
static const int CT_MAX_VALUE_DEPTH = 8;

// Cached CT_GDSCRIPT_TRACE presence (env is read once).
static int g_ct_active = -1; // -1 unknown, 0 inactive, 1 active

// GF10: a pending async-resume marker, deferred to the next per-line step so the
// continuation binds to the first RESUMED source line (strictly after the
// suspend step). A single slot is sufficient because GDScript coroutine resume
// runs synchronously on the resuming thread: the OPCODE_AWAIT_RESUME and the
// following OPCODE_LINE are adjacent with no other coroutine interleaving between
// them (nested/concurrent resume is a documented GF12-thread follow-up).
static bool g_ct_pending_resume = false;
static uintptr_t g_ct_pending_resume_ctx = 0;

// GF10: content tags for the suspend/resume markers. The db-backend's
// `gdscript-coroutine` ContinuationPattern keys off these prefixes; the marker
// METADATA carries the context_id (the CallState pointer) as a hex string.
static const char *CT_ASYNC_SUSPEND_TAG = "ct-async-suspend:gdscript-coroutine";
static const char *CT_ASYNC_RESUME_TAG = "ct-async-resume:gdscript-coroutine";

// GF10: emit one async marker as an events.dat special event. content = the
// suspend/resume tag; metadata = "<context_id_hex> <step_id>" where context_id is
// the CallState pointer and step_id is the exec-stream step the marker refers to.
//
// We encode step_id EXPLICITLY rather than relying on the event's implicit step
// binding: the FFI buffers one "pending step" (for late-arriving column deltas /
// values — see flushPendingStep in codetracer_trace_writer_ffi.nim), so the
// IOEvent's implicit stepId (msWriter.stepCount - 1) lags the just-registered
// line by one. trace_writer_next_step_index() DOES account for the pending step
// (it returns stepCount + hasPendingStep), so the step just registered — the one
// this marker refers to — is next_step_index() - 1. The db-backend reads this
// metadata step_id, which matches reader.step(StepId(n)) indexing.
//
// FFI_EVENT_TRACE_LOG_EVENT is a neutral log kind (it does not perturb the
// write/read event kinds recorders rely on).
static void gdscript_ct_emit_async_marker(const char *tag, uintptr_t ctx) {
	// A marker only makes sense once a step exists to anchor it to.
	if (g_ct_disabled || !g_ct_writer || !g_ct_started) {
		return;
	}
	uint64_t next = trace_writer_next_step_index(g_ct_writer);
	uint64_t step = (next > 0) ? (next - 1) : 0;
	char meta[48];
	snprintf(meta, sizeof(meta), "0x%llx %llu",
			(unsigned long long)ctx, (unsigned long long)step);
	trace_writer_register_special_event(g_ct_writer, FFI_EVENT_TRACE_LOG_EVENT, meta, tag);
}

bool gdscript_ct_trace_active() {
	if (g_ct_active < 0) {
		const char *out_dir = getenv("CT_GDSCRIPT_TRACE");
		g_ct_active = (out_dir && out_dir[0] != '\0') ? 1 : 0;
	}
	return g_ct_active == 1;
}

static void gdscript_ct_close() {
	if (g_ct_encoder) {
		ct_value_encoder_free(g_ct_encoder);
		g_ct_encoder = nullptr;
	}
	if (g_ct_writer) {
		trace_writer_finish_events(g_ct_writer);
		trace_writer_finish_metadata(g_ct_writer);
		trace_writer_finish_paths(g_ct_writer);
		trace_writer_close(g_ct_writer);
		trace_writer_free(g_ct_writer);
		g_ct_writer = nullptr;
	}
}

// Lazily create the writer (new + begin_*). Does NOT call trace_writer_start;
// that is deferred to the first step so the emitted step stream matches G2.
// Returns true when tracing is active for this process.
static bool gdscript_ct_ensure_writer() {
	if (g_ct_disabled) {
		return false;
	}
	if (likely(g_ct_inited)) {
		return g_ct_writer != nullptr;
	}

	g_ct_inited = true;
	if (!gdscript_ct_trace_active()) {
		g_ct_disabled = true;
		return false;
	}
	const char *out_dir = getenv("CT_GDSCRIPT_TRACE");

	codetracer_trace_writer_init();
	g_ct_writer = trace_writer_new("gdscript_trace", FFI_TRACE_FORMAT_BINARY);
	if (!g_ct_writer) {
		g_ct_disabled = true;
		return false;
	}

	// GDH-M6: pin the recording identity when asked to.
	//
	// Without a pin the writer mints a fresh UUIDv7 per recording, so NO TWO
	// RECORDINGS OF THE SAME PROGRAM ARE EVER BYTE-IDENTICAL — measured on
	// 2026-09-11, two runs of this engine over one fixture differed in exactly
	// 16 bytes and all sixteen were the id. `gdh6_corpus_is_unchanged` asks
	// whether this campaign altered any corpus recording, and that question is
	// unanswerable without this: the milestone forbids excluding byte ranges
	// from the comparison (an exclusion list grows one entry at a time and each
	// entry is invisible), so the identity has to be made equal rather than
	// ignored.
	//
	// It is OPT-IN and it is REPORTED. A recording produced under a pinned id
	// is not a normal recording — two of them are indistinguishable — so a run
	// must never be able to carry one silently.
	{
		const char *pinned = getenv("CT_RECORDING_ID");
		if (pinned != nullptr && pinned[0] != '\0') {
			trace_writer_clear_last_error();
			if (trace_writer_set_recording_id(g_ct_writer, pinned) == 0) {
				fprintf(stderr, "[ct-gdh6] recording id PINNED to %s "
								"(CT_RECORDING_ID); this recording is "
								"deliberately not unique\n", pinned);
			} else {
				fprintf(stderr, "[ct-gdh6] recording id pin REFUSED (%s): %s\n",
						pinned, trace_writer_last_error());
			}
			fflush(stderr);
		}
	}

	// The .ct lands in the directory of the events path:
	//   <out_dir>/gdscript_trace.ct
	String events_path = String::utf8(out_dir).path_join("events.bin");
	CharString events_cs = events_path.utf8();
	trace_writer_set_workdir(g_ct_writer, out_dir);
	trace_writer_begin_metadata(g_ct_writer, "");
	// IC-M2: stamp every VM-interned string with the "gdscript" origin namespace
	// so the materialized names coexist with native names when this container is
	// combined with the native recorder (MCR). Must be set BEFORE begin_events,
	// which is where the FFI resolves the shared container and initializes the
	// writer. Harmless standalone: a lone gdscript trace just carries the
	// qualifier on its keys (the reader strips it for display).
	trace_writer_set_interning_qualifier(g_ct_writer, "gdscript");
	trace_writer_begin_events(g_ct_writer, events_cs.get_data());
	trace_writer_begin_paths(g_ct_writer, "");

	// GDH-M6: the line-count table, and ONLY when a reload can arrive. See the
	// long note above `g_ct_line_count_table`. `enable_line_count_table` must
	// run before the first path is registered, which is why it is here rather
	// than at the first step.
	//
	// The result is reported either way. A build in which the call was made
	// and REFUSED, and a build in which it was never made, look identical from
	// the container — and the second is the falsifier arm for the corpus gate,
	// so they must not be allowed to look identical from the log either.
	{
		const char *agent_socket = getenv("REPRO_HCR_AGENT_SOCKET");
		bool reload_possible = agent_socket != nullptr && agent_socket[0] != '\0';
#if defined(CT_GDH6_FALSIFY_ALWAYS_LINE_COUNT_TABLE)
		// FALSIFIER ARM (gdh6_corpus_is_unchanged): set the new meta.dat bit
		// UNCONDITIONALLY. Every container in the GT1 corpus then changes —
		// paths.dat records grow a count and the position space is laid out
		// from recorded sizes rather than the DefaultLinesPerFile stride —
		// while every GDH-M6 gate stays green, because a reload session had
		// the bit on anyway. It is the cheapest way to ship the feature and
		// the one that breaks every recording that never reloads.
		reload_possible = true;
#endif
		if (reload_possible) {
			trace_writer_clear_last_error();
			if (trace_writer_enable_line_count_table(g_ct_writer) == 0) {
				g_ct_line_count_table = true;
				fprintf(stderr, "[ct-gdh6] line-count table ENABLED (meta.dat bit 14); "
								"a reload can mint a path version\n");
			} else {
				fprintf(stderr, "[ct-gdh6] line-count table REFUSED: %s\n",
						trace_writer_last_error());
			}
		} else {
			fprintf(stderr, "[ct-gdh6] line-count table off: no "
							"REPRO_HCR_AGENT_SOCKET, so no reload can arrive and "
							"this recording needs no path versions\n");
		}
		fflush(stderr);
	}

	atexit(gdscript_ct_close);
	return true;
}

// G4: intern the scalar/String trace types once. Called only after the writer
// exists, and BEFORE any other type is registered, so the None type takes
// TypeId(0) — the CTFS NONE_TYPE_ID invariant that ct_value_write_none encodes
// against (it always emits type_id 0). Registering None first keeps the null
// value's type name honest ("None") rather than colliding with the first
// scalar type.
static void gdscript_ct_ensure_types() {
	if (likely(g_ct_types_ready)) {
		return;
	}
	g_ct_type_none = trace_writer_ensure_type_id(g_ct_writer, FFI_TYPE_NONE, "None");
	g_ct_type_int = trace_writer_ensure_type_id(g_ct_writer, FFI_TYPE_INT, "Int");
	g_ct_type_float = trace_writer_ensure_type_id(g_ct_writer, FFI_TYPE_FLOAT, "Float");
	g_ct_type_bool = trace_writer_ensure_type_id(g_ct_writer, FFI_TYPE_BOOL, "Bool");
	g_ct_type_string = trace_writer_ensure_type_id(g_ct_writer, FFI_TYPE_STRING, "String");
	g_ct_type_raw = trace_writer_ensure_type_id(g_ct_writer, FFI_TYPE_RAW, "Variant");
	if (!g_ct_encoder) {
		g_ct_encoder = ct_value_encoder_new();
	}
	g_ct_types_ready = (g_ct_encoder != nullptr);
}

// §5.2 SOURCE BUNDLING (Mixed-Native-GDScript-Debugging §3.2 / §5.2).
//
// A GDScript path is a Godot `res://` virtual path that never exists on the
// debugging host's filesystem, so the standalone `.ct` must carry the `.gd`
// SOURCE TEXT for the Editor Pane and the value-origin classifier to resolve
// it (Value-Origin-Tracking §6.1 bundled-sources). We stream each recorded
// file's source into the container's srcviews stream via
// trace_writer_register_source_view (view_kind 0 = raw = the source itself,
// no sourcemap). The bundle is ADDITIVE metadata: the step / value / call
// streams are byte-identical to a run without it.
//
// register_source_view keys on `path_id`, and GDH-M3 changed where that id
// comes from. It used to be re-derived here: a local first-seen map plus a
// `g_ct_next_path_id` counter MIRRORED the writer's private interning counter,
// on the assumption that the writer interns paths in first-seen order starting
// at 0.
//
// THAT ASSUMPTION IS NOW FALSE, AND ITS FAILURE IS SILENT.
// `trace_writer_register_path_version` (design §6.4) appends a SECOND
// paths.dat record for a path string the writer has already interned, so from
// the first hot reload onward the mirror's count and the writer's ids diverge —
// and because the mirror was keyed by STRING, the reloaded file resolved back
// to the FIRST version's id. Every source view from that point on attached to
// the wrong file, with nothing reporting it. A design that added versioning
// while leaving the mirror in place would have shipped a worse defect than the
// one it fixes, which is why GDH-M3's job is to DELETE the mirror rather than
// to adjust it.
//
// So: the id comes from `trace_writer_current_path_id`, which answers with the
// writer's own state, and the "already bundled" set is keyed by that id. Two
// consequences fall out, both wanted:
//
//   * a RELOADED file is a new id, so its text IS bundled — the old
//     string-keyed early return is exactly why a reloaded file's source never
//     reached the container (design §2.2d);
//   * repeat steps on an unchanged file are still bundled once, because the id
//     is unchanged.
//
// Bundling stays best-effort: a source that cannot be read is simply not
// bundled (the origin gap remains for that file — honest degradation) rather
// than emitting empty text.
static HashSet<uint64_t> g_ct_bundled_path_ids;

// ===========================================================================
// GDH-M6 — the line-count table (meta.dat bit 14) and minted path versions.
//
// A reload can only be attributed if the reloaded file gets a SECOND
// paths.dat record, and `trace_writer_register_path_version` requires the
// line-count table: without it both versions would be laid out at the
// DefaultLinesPerFile stride, no bound could be enforced against either, and
// the mis-attribution would be silent — which is precisely the defect GDH-M0
// measured (129 of 196 steps decoding to lines 45-57 of a file the container
// states is 40 lines long, with nothing reporting it).
//
// TWO CONSEQUENCES SHAPE WHERE THE SWITCH GOES.
//
//  1. The table changes the bytes of EVERY container the writer produces —
//     paths.dat records grow a count, and the global position space is laid
//     out from the recorded sizes instead of the stride. Turning it on
//     unconditionally would change every recording in the GT1 corpus, which
//     is exactly what `gdh6_corpus_is_unchanged`'s first falsifier arm is
//     ("set the new meta.dat bit unconditionally"). So it is on only when a
//     reload can ARRIVE: `REPRO_HCR_AGENT_SOCKET` is what makes the in-target
//     agent connect out to a coordinator, and a process no coordinator is
//     attached to has no reload to record.
//
//  2. Under the table the IMPLICIT path registration that
//     `trace_writer_register_step` performs for an unseen path is REFUSED by
//     name (it has no count to record). So every path must be registered
//     through `trace_writer_register_path_with_line_count` BEFORE the first
//     step that mentions it — `gdscript_ct_ensure_path_sized_locked` below,
//     called from the step hook ahead of `trace_writer_start` /
//     `trace_writer_register_step`.
//
// `g_ct_sized_paths` is keyed by the path STRING and is not a mirror of the
// writer's ids — it answers "have I given this file a size yet", which does
// not change when a reload mints a new version of it. GDH-M3 deleted the id
// mirror and nothing here reintroduces one: every id is still ASKED FOR.
//
// (The two variables themselves are declared beside `g_ct_writer` at the top
// of the file, because `gdscript_ct_ensure_writer` — which turns the table on
// — runs before this point in the translation unit.)
// ===========================================================================

// The ceiling the C header names for a file whose lines cannot be counted
// ("conventionally 100000"). It is recorded as the file's size, so the size
// the space uses is the size the container states — an honest over-estimate
// rather than an inferred one. It is NEVER the quiet default: the one call
// site reports on stderr when it falls back.
static const uint64_t CT_LINE_COUNT_CEILING = 100000;

#if defined(CT_GDH6_FALSIFY_STALE_LINE_COUNT)
// Only the falsifier arm needs to remember the size the PREVIOUS version was
// recorded with; the shipped recorder never looks backwards, because the count
// it records is always computed from the bytes it is about to install. The
// variable is compiled in only under the arm so that a build without it cannot
// be carrying half of one.
static uint64_t g_ct_gdh6_last_recorded_lines = 0;
#endif

// The number of ADDRESSABLE lines in `p_bytes`, i.e. the largest 1-based line
// number the file reaches. `checkLineWithinFile` refuses `line > count`, so
// this bound is inclusive.
//
// A file ending in a newline has exactly as many lines as it has newlines
// (`wc -l`); one that does not has one more. Getting this wrong in the safe
// direction would be invisible — an over-estimate simply leaves unused space —
// so it is computed rather than approximated, and 0 is lifted to 1 because
// `register_path_with_line_count` refuses a 0-line file (it would share its
// base with the next one).
static uint64_t gdscript_ct_addressable_lines(const uint8_t *p_bytes, int64_t p_size) {
	if (p_bytes == nullptr || p_size <= 0) {
		return 1;
	}
	uint64_t newlines = 0;
	for (int64_t i = 0; i < p_size; i++) {
		if (p_bytes[i] == '\n') {
			newlines++;
		}
	}
	uint64_t lines = (p_bytes[p_size - 1] == '\n') ? newlines : newlines + 1;
	return lines == 0 ? 1 : lines;
}

// Give `p_res_path` a recorded size before any step interns it. A no-op when
// the line-count table is off, which is every recording that is not part of a
// reload session — so the ordinary step/path streams are untouched.
static void gdscript_ct_ensure_path_sized_locked(const String &p_res_path) {
	if (!g_ct_line_count_table || g_ct_writer == nullptr) {
		return;
	}
	if (g_ct_sized_paths.has(p_res_path)) {
		return;
	}
	g_ct_sized_paths.insert(p_res_path);

	Error err = OK;
	Vector<uint8_t> bytes = FileAccess::get_file_as_bytes(p_res_path, &err);
	uint64_t lines = CT_LINE_COUNT_CEILING;
	bool counted = false;
	if (err == OK && !bytes.is_empty()) {
		lines = gdscript_ct_addressable_lines(bytes.ptr(), (int64_t)bytes.size());
		counted = true;
	}
	CharString path_cs = p_res_path.utf8();
	trace_writer_clear_last_error();
	int rc = trace_writer_register_path_with_line_count(
			g_ct_writer, path_cs.get_data(), lines);
	if (rc != 0) {
		// A refused sizing means every step on this file is about to be
		// refused too (the implicit registration has no count), and those
		// steps would simply never appear. That must not be silent — it is
		// the shape of defect this whole campaign exists to remove.
		fprintf(stderr, "[ct-gdh6] REFUSED sizing %s at %llu line(s): %s\n",
				path_cs.get_data(), (unsigned long long)lines,
				trace_writer_last_error());
		fflush(stderr);
		return;
	}
#if defined(CT_GDH6_FALSIFY_STALE_LINE_COUNT)
	g_ct_gdh6_last_recorded_lines = lines;
#endif
	fprintf(stderr, "[ct-gdh6] sized %s = %llu line(s) (%s)\n",
			path_cs.get_data(), (unsigned long long)lines,
			counted ? "counted" : "CEILING: the file could not be read");
	fflush(stderr);
}

// Attach `p_bytes` as `p_path_id`'s raw source view.
//
// GDH-M8 (design §8.1 step 4) split this out of the disk-reading wrapper below
// so the reload sequence can bundle THE BYTES IT VERIFIED rather than whatever
// is on disk when the next step happens to run. The two callers want different
// sources for the same act and the difference is load-bearing:
//
//   * the ordinary recorder path has no bytes of its own — the file has been on
//     disk since before the process started — so it reads them;
//   * the reload path HAS the bytes, already digest-checked by the agent
//     (repro_hcr_agent.c:2316-2371), and reading the file back instead would
//     re-open a TOCTOU window the digest exists to close (§8.2).
//
// Returns false when nothing was bundled, so a caller that must not continue
// without the view can tell. The old wrapper's silent `return` on an unreadable
// file is preserved for the recorder path, where a missing view is a degraded
// rendering rather than an incoherent trace.
#if !defined(CT_GDH8_NO_INJECTION_HOOK)
// GDH-M8's fault-injection latch for §8.1 step 4.
//
// Once the injection has fired at the bundle stage, source-view registration
// stays refused for the rest of the process. That is FAITHFULNESS, not
// convenience: a writer that refuses a source view refuses it again at the next
// step, and a hook that failed exactly once is rescued by the recorder's own
// lazy bundler — `gdscript_ct_note_and_bundle_path_locked` re-reads the file
// from disk at the first step after the reload and attaches the view the
// injection had just prevented.
//
// MEASURED. Written one-shot first, the falsifier arm
// `CT_GDH8_FALSIFY_CONTINUE_AFTER_FAILURE` went red on the ABSENCE of a refusal
// message and NOT on "steps whose path id has no source view", which is the
// container incoherence its milestone entry requires it to be killed by. The
// driver's named-kill check is what caught it.
//
// It is set only from `ct_gdh8_inject_at`'s caller, which is inert unless
// `CT_GDH8_INJECT_FAILURE` names a stage, and the whole latch is compiled out
// by `CT_GDH8_NO_INJECTION_HOOK` — which is the build the inertness gate
// compares against, byte for byte.
static bool g_ct_gdh8_source_views_poisoned = false;
#endif

static bool gdscript_ct_bundle_bytes_locked(const String &p_res_path,
		uint64_t p_path_id, Vector<uint8_t> bytes) {
#if !defined(CT_GDH8_NO_INJECTION_HOOK)
	if (unlikely(g_ct_gdh8_source_views_poisoned)) {
		return false;
	}
#endif
	if (bytes.is_empty()) {
		return false;
	}
#if defined(CT_GDH6_FALSIFY_STALE_SOURCE_VIEW)
	// FALSIFIER ARM (gdh6_no_step_is_attributed_to_the_wrong_version, arm 4):
	// attach the FIRST version's bytes under every path id, leaving every path
	// id and every step line correct. The container then carries the right
	// NUMBER of views, one per version, each on the right path id — and every
	// one of them renders v1. The line-number half of the gate passes
	// completely; only the TEXT half can see it, which is why the text half
	// exists. A swap is the same defect with a shorter reach, so the arm takes
	// the stronger form.
	{
		static Vector<uint8_t> s_first_bytes;
		if (s_first_bytes.is_empty()) {
			s_first_bytes = bytes;
		}
		bytes = s_first_bytes;
	}
#endif
	CharString name_cs = p_res_path.utf8();
	// view_kind 0 = raw original source; NULL sourcemap = identity bundle.
	trace_writer_clear_last_error();
	int64_t view_index = trace_writer_register_source_view(
			g_ct_writer,
			p_path_id,
			/*view_kind=*/0,
			name_cs.get_data(), (size_t)name_cs.length(),
			bytes.ptr(), (size_t)bytes.size(),
			nullptr, 0);
	// A signed return distinguishes index 0 from an error (see the header).
	return view_index >= 0;
}

static void gdscript_ct_bundle_source_locked(const String &p_res_path, uint64_t p_path_id) {
	Error err = OK;
	Vector<uint8_t> bytes = FileAccess::get_file_as_bytes(p_res_path, &err);
	if (err != OK || bytes.is_empty()) {
		return; // unreadable / empty — skip; do not bundle empty source
	}
	(void)gdscript_ct_bundle_bytes_locked(p_res_path, p_path_id, bytes);
}

// Bundle `p_res_path`'s source text once per PATH ID. Must be called from
// inside the emit lock, immediately after the trace_writer_start /
// trace_writer_register_step call that interns the same path, so the writer
// already knows the path when we ask it for the id.
//
// The id is ASKED FOR, never counted. `trace_writer_current_path_id` returns
// the id a bare `trace_writer_register_step(path, ...)` attributes a step to
// right now — the newest version after a reload, the ordinary interned id
// otherwise — and `CT_TW_INVALID_PATH_ID` for a path the writer has not seen.
// An unknown path is skipped rather than guessed at: bundling source text
// against an invented id would attach it to some other file.
static void gdscript_ct_note_and_bundle_path_locked(const String &p_res_path) {
	CharString path_cs = p_res_path.utf8();
	uint64_t path_id = trace_writer_current_path_id(g_ct_writer, path_cs.get_data());
	if (path_id == CT_TW_INVALID_PATH_ID) {
		return; // the writer does not know this path; do not invent an id for it
	}
#if defined(CT_GDH6_FALSIFY_STRING_KEYED_BUNDLE)
	// FALSIFIER ARM (gdh6_both_versions_retrievable_end_to_end): restore the
	// STRING-keyed early return GDH-M3 deleted. A reloaded file's string has
	// been seen before, so its text is never bundled and the container carries
	// ONE source view for a file that ran in three versions. This is the
	// shipping behaviour GDH-M0 measured, and it is why that gate is phrased
	// on source-view BYTES rather than on path entries alone: the path entries
	// are all still there under this arm.
	{
		static HashSet<String> s_bundled_strings;
		if (s_bundled_strings.has(p_res_path)) {
			return;
		}
		s_bundled_strings.insert(p_res_path);
	}
#endif
	if (g_ct_bundled_path_ids.has(path_id)) {
		return;
	}
	g_ct_bundled_path_ids.insert(path_id);
	gdscript_ct_bundle_source_locked(p_res_path, path_id);
}

void gdscript_ct_trace_step(const StringName &p_source, int64_t p_line) {
	CtEmitLock lk; // GF12: serialize + reentrancy-guard the emit path
	if (!lk.engaged) {
		return;
	}
	if (!gdscript_ct_ensure_writer()) {
		return;
	}
	// GF12: attribute this step to the emitting OS thread (emits ThreadStart/
	// ThreadSwitch on a thread change; a no-op on a single-threaded program).
	uint64_t cur = gdscript_ct_current_thread();
	gdscript_ct_note_thread_locked(cur);

	String source_str = String(p_source);
	CharString src_cs = source_str.utf8();
	// GDH-M6: under the line-count table the implicit registration these two
	// calls would perform for an unseen path is refused by name, so the file's
	// size is recorded FIRST. A no-op when the table is off.
	gdscript_ct_ensure_path_sized_locked(source_str);
	if (unlikely(!g_ct_started)) {
		g_ct_started = true;
		// Registers the first (pending) step at this real source line.
		trace_writer_start(g_ct_writer, src_cs.get_data(), p_line);
		gdscript_ct_note_and_bundle_path_locked(source_str);
		g_ct_pending_owner = cur; // GF12: this thread owns the pending step
		return;
	}

	trace_writer_register_step(g_ct_writer, src_cs.get_data(), p_line);
	gdscript_ct_note_and_bundle_path_locked(source_str);
	g_ct_pending_owner = cur; // GF12: this thread owns the new pending step

	// GF10: flush a pending async-resume marker onto THIS (first resumed) step,
	// so continuation.step_id is the first line executed after the await resumes
	// — strictly greater than the suspend step. The step above was just
	// registered, so the marker binds to it (stepCount - 1).
	if (unlikely(g_ct_pending_resume)) {
		g_ct_pending_resume = false;
		gdscript_ct_emit_async_marker(CT_ASYNC_RESUME_TAG, g_ct_pending_resume_ctx);
	}
}

void gdscript_ct_trace_await_suspend(const void *p_call_state) {
	CtEmitLock lk; // GF12: serialize + reentrancy-guard
	if (!lk.engaged) {
		return;
	}
	gdscript_ct_emit_async_marker(CT_ASYNC_SUSPEND_TAG, (uintptr_t)p_call_state);
}

void gdscript_ct_trace_await_resume(const void *p_call_state) {
	// Defer the resume marker to the next per-line step (gdscript_ct_trace_step).
	// At OPCODE_AWAIT_RESUME the interpreter has not yet emitted an OPCODE_LINE
	// for the resumed body, so binding here would land the marker back on the
	// suspend step; deferring makes continuation.step_id the first resumed line.
	CtEmitLock lk; // GF12: serialize the pending-resume slot write
	if (!lk.engaged) {
		return;
	}
	if (g_ct_disabled) {
		return;
	}
	g_ct_pending_resume = true;
	g_ct_pending_resume_ctx = (uintptr_t)p_call_state;
}

// GF13: metadata tags distinguishing a warning from an error diagnostic. The
// wire format has no dedicated Warning kind, so push_warning rides the neutral
// FFI_EVENT_TRACE_LOG_EVENT kind and carries its level in the event metadata for
// a real event-log pane. (`ct-print --full` does not surface multi-stream io
// metadata, but it DOES render the two events with distinct io kinds — ioError
// vs ioStderr — and the message text, which already distinguish them.)
static const char *CT_PUSH_ERROR_TAG = "ct-push-error";
static const char *CT_PUSH_WARNING_TAG = "ct-push-warning";

void gdscript_ct_trace_utility_diagnostic(const StringName &p_function,
		const Variant **p_args, int p_argc) {
	// Cheap name reject BEFORE taking the lock: OPCODE_CALL_UTILITY fires for
	// every core utility call (print, str, typeof, ...), but only the two
	// diagnostic functions are recorded. StringName == is an O(1) compare.
	static const StringName s_push_error = StringName("push_error");
	static const StringName s_push_warning = StringName("push_warning");
	const bool is_error = (p_function == s_push_error);
	const bool is_warning = (p_function == s_push_warning);
	if (!is_error && !is_warning) {
		return;
	}

	CtEmitLock lk; // GF12: serialize + reentrancy-guard the emit path
	if (!lk.engaged) {
		return;
	}
	// A diagnostic only makes sense once a step exists to anchor it to (the
	// push_* call site's own OPCODE_LINE step); mirrors the async-marker guard.
	if (g_ct_disabled || !g_ct_writer || !g_ct_started) {
		return;
	}

	// Join the vararg args into one message exactly as the engine does
	// (VariantUtilityFunctions::push_error/push_warning call join_string over
	// all args), so the recorded message matches what the program logged.
	String message;
	for (int i = 0; i < p_argc; i++) {
		if (p_args[i]) {
			message += p_args[i]->operator String();
		}
	}
	CharString msg_cs = message.utf8();
	const char *tag = is_error ? CT_PUSH_ERROR_TAG : CT_PUSH_WARNING_TAG;
	// push_error -> FFI_EVENT_ERROR (io_kind ioError); push_warning ->
	// FFI_EVENT_TRACE_LOG_EVENT (io_kind ioStderr) — two distinct io kinds.
	const int kind = is_error ? FFI_EVENT_ERROR : FFI_EVENT_TRACE_LOG_EVENT;
	// content == the message (surfaced by the reader as the io event's `text`);
	// metadata == the level tag (for a real event-log pane; not surfaced by ct-print).
	trace_writer_register_special_event(g_ct_writer, kind, tag, msg_cs.get_data());
}

void gdscript_ct_trace_call(const StringName &p_name, const StringName &p_source, int64_t p_line) {
	CtEmitLock lk; // GF12: serialize + reentrancy-guard
	if (!lk.engaged) {
		return;
	}
	if (!gdscript_ct_ensure_writer()) {
		return;
	}
	// GF12: attribute this call to the emitting OS thread (before register_call so
	// the call's entry_step lands in the correct thread region).
	gdscript_ct_note_thread_locked(gdscript_ct_current_thread());

	CharString name_cs = String(p_name).utf8();
	CharString src_cs = String(p_source).utf8();
	size_t fid = trace_writer_ensure_function_id(g_ct_writer,
			name_cs.get_data(), src_cs.get_data(), p_line);
	trace_writer_register_call(g_ct_writer, fid);

	// MT3: open a native<->VM crossing for this frame, at the SAME site (and
	// under the same lock) as the call record it wraps, so the crossing stack
	// nests one-for-one with the writer's call stack. begin_crossing flushes the
	// pending step just like register_call, so the crossing's start_step equals
	// this call's entry_step. Always push exactly one entry per on_call (0 on
	// failure) so the matching on_return pops exactly one and the stack stays
	// balanced with the call/return pairs. Guarded by the live writer handle.
	if (g_ct_writer) {
		uint64_t span_id = trace_writer_begin_crossing(g_ct_writer, "gdscript-frame");
		g_ct_crossing_stack.push_back(span_id);
	}
}

// GF5: forward declarations — the return hook (below) reuses the recursive
// value encoder defined further down (shared with the G4 assign hook).
static void gdscript_ct_encode_variant(const Variant &value);

void gdscript_ct_trace_return(const Variant &p_return_value) {
	CtEmitLock lk; // GF12: serialize + reentrancy-guard
	if (!lk.engaged) {
		return;
	}
	// Never create the writer from a return: a return only makes sense after a
	// matching call (which already created it). Guard on the live handle.
	if (g_ct_disabled || !g_ct_writer) {
		return;
	}
	// GF12: attribute this return to the emitting OS thread. The return VALUE
	// rides the call stream (not the shared pending step), so it is captured
	// reliably even under interleaving.
	gdscript_ct_note_thread_locked(gdscript_ct_current_thread());

	// MT3: close the native<->VM crossing this frame's matching on_call opened,
	// popping the innermost entry so crossings close strictly LIFO (as the writer
	// requires). Done here — before the return-value encoding below, which has
	// its own early-return paths — so the crossing is closed on EVERY exit path.
	// end_crossing flushes the pending step like register_return, and the
	// crossing's end_step is the last materialized step, unaffected by whether the
	// return record is written before or after this. A 0 span_id (a begin_crossing
	// that failed) is popped but not closed, keeping the stack balanced.
	if (!g_ct_crossing_stack.empty()) {
		uint64_t span_id = g_ct_crossing_stack.back();
		g_ct_crossing_stack.pop_back();
		if (span_id != 0) {
			trace_writer_end_crossing(g_ct_writer, span_id);
		}
	}

	// GF5: capture the return VALUE. Encode it with the same recursive
	// ct_value_* encoder G4/GF3/GF4 use for locals, then attach it to the
	// return record via trace_writer_register_return_cbor. This is a drop-in
	// replacement for register_return (the FFI routes both through the SAME
	// registerReturn), so the number/ordering of call/return records — and thus
	// the G3 nesting + balanced-pair invariant — is unchanged. Returns live in
	// the call stream, not values.dat, so there is no parallel-index constraint
	// (unlike gdscript_ct_trace_assign, this does not require g_ct_started).
	//
	// A `-> void` / fall-off-the-end function has retvalue == NIL, which encodes
	// as a None value node (kind "None", TypeId 0) — consistent with G4's
	// `null -> None`, not the format's bare one-byte VoidReturnMarker.
	gdscript_ct_ensure_types();
	if (!g_ct_encoder) {
		// Encoder unavailable (should not happen once the writer exists): fall
		// back to a valueless return so the call/return pair still balances.
		trace_writer_register_return(g_ct_writer);
		return;
	}
	gdscript_ct_encode_variant(p_return_value);
	size_t cbor_len = 0;
	const uint8_t *cbor = ct_value_get_bytes(g_ct_encoder, &cbor_len);
	if (!cbor || cbor_len == 0) {
		trace_writer_register_return(g_ct_writer);
		return;
	}
	trace_writer_register_return_cbor(g_ct_writer, cbor, cbor_len);
}

// GF3: intern the compound-collection types on first use. Kept out of
// gdscript_ct_ensure_types so a scalar-only recording never registers them and
// its types table stays byte-identical to G4/GF1/GF2. The scalar types (incl.
// None at TypeId(0)) are already interned by gdscript_ct_ensure_types before any
// value is encoded, so these land after them: Array (Seq), Dictionary (Seq),
// Pair (Tuple).
static void gdscript_ct_ensure_collection_types() {
	if (likely(g_ct_coll_types_ready)) {
		return;
	}
	g_ct_type_seq = trace_writer_ensure_type_id(g_ct_writer, FFI_TYPE_SEQ, "Array");
	g_ct_type_dict = trace_writer_ensure_type_id(g_ct_writer, FFI_TYPE_SEQ, "Dictionary");
	g_ct_type_pair = trace_writer_ensure_type_id(g_ct_writer, FFI_TYPE_TUPLE, "Pair");
	g_ct_coll_types_ready = true;
}

// GF4 extension point: encode any still-unsupported Variant type as a Raw
// (printed) value. Godot's Variant->String is recursion-guarded, so this is
// bounded even for cyclic collections reached past the depth cap.
static void gdscript_ct_write_raw(const Variant &value) {
	CharString s = value.operator String().utf8();
	ct_value_write_raw(g_ct_encoder,
			(const uint8_t *)s.get_data(), (size_t)s.length(), g_ct_type_raw);
}

// GF3: encode a packed scalar array as a `Sequence` of a fixed element writer.
// Packed*Array elements are always scalars, so no recursion (depth) is needed.
#define CT_ENCODE_PACKED_SEQ(m_packed_type, m_elem_write)                        \
	do {                                                                        \
		const m_packed_type _a = value.operator m_packed_type();               \
		const int _n = _a.size();                                              \
		ct_value_begin_sequence(g_ct_encoder, g_ct_type_seq, _n);              \
		for (int _i = 0; _i < _n; _i++) {                                      \
			m_elem_write;                                                      \
		}                                                                     \
		ct_value_end_compound(g_ct_encoder);                                   \
	} while (0)

// GF4: intern a Struct type lazily by name. Idempotent (the writer's type
// registry dedups on kind+name), and — crucially for the lazy-interning
// discipline GF3 established — this is ONLY reached when a struct/handle value
// is actually encoded, so a scalar-only recording never registers any of these
// and its types table stays byte-identical to G4/GF1/GF2 ([None, Int, Float,
// Bool, String, Variant]). The C-ABI type registry stores only kind + a lang
// type name (TypeSpecificInfo is None); it has NO per-field-name registration,
// and the streaming CBOR encoder's ct_value_begin_struct writes only positional
// field_values + a type_id (no field_names). So GF4 encodes each Godot math
// type as a Struct of the SAME kind the db-backend's ValueRecord::Struct
// expects, with one registered Struct type per Godot type (the type name, e.g.
// "Vector3", is what distinguishes it and surfaces in ct-print's per-var
// type_name); field NAMES are conveyed positionally by that per-type canonical
// order (documented in scripts/EXPECTED-GF4.md), because the C-ABI exposes no
// way to attach literal field names without extending
// libcodetracer_trace_writer.a (out of scope for a modules/gdscript-confined
// change).
static inline uint64_t gct_stype(const char *name) {
	return trace_writer_ensure_type_id(g_ct_writer, FFI_TYPE_STRUCT, name);
}

// GF4: scalar field writers (the struct field values recurse through the SAME
// scalar path G4 established, so Int/Float/String type ids stay shared).
static inline void gct_f(double v) {
	ct_value_write_float(g_ct_encoder, v, g_ct_type_float);
}
static inline void gct_i(int64_t v) {
	ct_value_write_int(g_ct_encoder, v, g_ct_type_int);
}
static inline void gct_str(const String &v) {
	CharString cs = v.utf8();
	ct_value_write_string(g_ct_encoder,
			(const uint8_t *)cs.get_data(), (size_t)cs.length(), g_ct_type_string);
}

// GF4: float-vector structs (x,y[,z[,w]]) and their integer variants.
static void gct_vec2(const Vector2 &v) {
	ct_value_begin_struct(g_ct_encoder, gct_stype("Vector2"), 2);
	gct_f(v.x);
	gct_f(v.y);
	ct_value_end_compound(g_ct_encoder);
}
static void gct_vec2i(const Vector2i &v) {
	ct_value_begin_struct(g_ct_encoder, gct_stype("Vector2i"), 2);
	gct_i(v.x);
	gct_i(v.y);
	ct_value_end_compound(g_ct_encoder);
}
static void gct_vec3(const Vector3 &v) {
	ct_value_begin_struct(g_ct_encoder, gct_stype("Vector3"), 3);
	gct_f(v.x);
	gct_f(v.y);
	gct_f(v.z);
	ct_value_end_compound(g_ct_encoder);
}
static void gct_vec3i(const Vector3i &v) {
	ct_value_begin_struct(g_ct_encoder, gct_stype("Vector3i"), 3);
	gct_i(v.x);
	gct_i(v.y);
	gct_i(v.z);
	ct_value_end_compound(g_ct_encoder);
}
static void gct_vec4(const Vector4 &v) {
	ct_value_begin_struct(g_ct_encoder, gct_stype("Vector4"), 4);
	gct_f(v.x);
	gct_f(v.y);
	gct_f(v.z);
	gct_f(v.w);
	ct_value_end_compound(g_ct_encoder);
}
static void gct_vec4i(const Vector4i &v) {
	ct_value_begin_struct(g_ct_encoder, gct_stype("Vector4i"), 4);
	gct_i(v.x);
	gct_i(v.y);
	gct_i(v.z);
	gct_i(v.w);
	ct_value_end_compound(g_ct_encoder);
}
static void gct_color(const Color &c) {
	ct_value_begin_struct(g_ct_encoder, gct_stype("Color"), 4);
	gct_f(c.r);
	gct_f(c.g);
	gct_f(c.b);
	gct_f(c.a);
	ct_value_end_compound(g_ct_encoder);
}
// GF4: composite structs whose fields are themselves structs (encoded as nested
// Structs, mirroring how GF3 nests collections).
static void gct_rect2(const Rect2 &r) {
	ct_value_begin_struct(g_ct_encoder, gct_stype("Rect2"), 2); // position, size
	gct_vec2(r.position);
	gct_vec2(r.size);
	ct_value_end_compound(g_ct_encoder);
}
static void gct_rect2i(const Rect2i &r) {
	ct_value_begin_struct(g_ct_encoder, gct_stype("Rect2i"), 2); // position, size
	gct_vec2i(r.position);
	gct_vec2i(r.size);
	ct_value_end_compound(g_ct_encoder);
}
static void gct_plane(const Plane &p) {
	ct_value_begin_struct(g_ct_encoder, gct_stype("Plane"), 2); // normal, d
	gct_vec3(p.normal);
	gct_f(p.d);
	ct_value_end_compound(g_ct_encoder);
}
static void gct_quat(const Quaternion &q) {
	ct_value_begin_struct(g_ct_encoder, gct_stype("Quaternion"), 4);
	gct_f(q.x);
	gct_f(q.y);
	gct_f(q.z);
	gct_f(q.w);
	ct_value_end_compound(g_ct_encoder);
}
static void gct_aabb(const AABB &a) {
	ct_value_begin_struct(g_ct_encoder, gct_stype("AABB"), 2); // position, size
	gct_vec3(a.position);
	gct_vec3(a.size);
	ct_value_end_compound(g_ct_encoder);
}
// Basis: three Vector3 fields named x,y,z. Godot stores the matrix as `rows`;
// we emit the rows in order (for a diagonal/scale basis rows == columns, which
// is what the GF4 fixture uses, so the row-vs-column distinction is moot there).
static void gct_basis(const Basis &b) {
	ct_value_begin_struct(g_ct_encoder, gct_stype("Basis"), 3); // x, y, z (rows)
	gct_vec3(b.rows[0]);
	gct_vec3(b.rows[1]);
	gct_vec3(b.rows[2]);
	ct_value_end_compound(g_ct_encoder);
}
// Transform2D: two basis columns (x, y) + origin, matching GDScript's t.x/t.y/
// t.origin accessors (columns[0], columns[1], columns[2]).
static void gct_xform2d(const Transform2D &t) {
	ct_value_begin_struct(g_ct_encoder, gct_stype("Transform2D"), 3); // x, y, origin
	gct_vec2(t.columns[0]);
	gct_vec2(t.columns[1]);
	gct_vec2(t.columns[2]);
	ct_value_end_compound(g_ct_encoder);
}
static void gct_xform3d(const Transform3D &t) {
	ct_value_begin_struct(g_ct_encoder, gct_stype("Transform3D"), 2); // basis, origin
	gct_basis(t.basis);
	gct_vec3(t.origin);
	ct_value_end_compound(g_ct_encoder);
}
// Projection: four Vector4 columns (x, y, z, w), matching GDScript's p.x..p.w.
static void gct_projection(const Projection &p) {
	ct_value_begin_struct(g_ct_encoder, gct_stype("Projection"), 4);
	gct_vec4(p.columns[0]);
	gct_vec4(p.columns[1]);
	gct_vec4(p.columns[2]);
	gct_vec4(p.columns[3]);
	ct_value_end_compound(g_ct_encoder);
}

// GF4: handle / reference types. Deep or cyclic object graphs are OUT OF SCOPE:
// OBJECT/RefCounted is a FAITHFUL SHALLOW representation — class name + instance
// id only, NO property walk — so it cannot recurse into another object and no
// visited-guard is needed (the encoder emits two scalars and stops). RID, Callable
// and Signal are small Structs carrying the identifying handle (id / method name /
// signal name); the nondeterministic object/instance ids they also carry are
// deliberately NOT emitted (Callable/Signal) so the encoding is deterministic.
static void gct_rid(const RID &r) {
	ct_value_begin_struct(g_ct_encoder, gct_stype("RID"), 1); // id
	gct_i((int64_t)r.get_id());
	ct_value_end_compound(g_ct_encoder);
}
static void gct_callable(const Callable &c) {
	ct_value_begin_struct(g_ct_encoder, gct_stype("Callable"), 1); // method
	gct_str(String(c.get_method()));
	ct_value_end_compound(g_ct_encoder);
}
static void gct_signal(const Signal &s) {
	ct_value_begin_struct(g_ct_encoder, gct_stype("Signal"), 1); // name
	gct_str(String(s.get_name()));
	ct_value_end_compound(g_ct_encoder);
}
static void gct_object(Object *o) {
	if (!o) {
		ct_value_write_none_typed(g_ct_encoder, g_ct_type_none);
		return;
	}
	ct_value_begin_struct(g_ct_encoder, gct_stype("Object"), 2); // class, id
	gct_str(o->get_class());
	gct_i((int64_t)(uint64_t)o->get_instance_id());
	ct_value_end_compound(g_ct_encoder);
}

// GF4: encode a packed struct-array (PackedVector2/3/4Array, PackedColorArray)
// as a Sequence of the matching per-element Struct — picking up the GF3 Raw
// deferrals now that the struct element encoders exist.
#define CT_ENCODE_PACKED_STRUCT_SEQ(m_packed_type, m_elem_encode)                \
	do {                                                                        \
		const m_packed_type _a = value.operator m_packed_type();               \
		const int _n = _a.size();                                              \
		ct_value_begin_sequence(g_ct_encoder, g_ct_type_seq, _n);              \
		for (int _i = 0; _i < _n; _i++) {                                      \
			m_elem_encode(_a[_i]);                                             \
		}                                                                     \
		ct_value_end_compound(g_ct_encoder);                                   \
	} while (0)

// GF3: recursively encode `value` into the reused CBOR encoder.
//   - scalars (int/float/bool/String/null)  -> written directly (G4).
//   - ARRAY (untyped) and typed Array[T]     -> Sequence of encoded elements.
//   - PACKED_{BYTE,INT32,INT64,FLOAT32,FLOAT64,STRING}_ARRAY -> Sequence of the
//     matching scalar.
//   - DICTIONARY (untyped) and typed Dictionary[K,V] -> Sequence of key/value
//     Tuples (the Python/Ruby dict pattern).
//   - GF4 math/struct types (Vector2/2i/3/3i/4/4i, Rect2/2i, Transform2D/3D,
//     Basis, Quaternion, AABB, Plane, Color, Projection) -> named-field Struct
//     (one registered Struct type per Godot type; nested struct fields nest).
//   - GF4 handle/string types: STRING_NAME/NODE_PATH -> String; RID/Callable/
//     Signal/Object(RefCounted) -> shallow Struct.
//   - PACKED_VECTOR2/3/4_ARRAY, PACKED_COLOR_ARRAY -> Sequence of the matching
//     struct element (the GF3 Raw deferrals, now supported).
//   - anything still unhandled -> Raw printed form.
// `depth` bounds nesting; at the cap a still-nesting collection degrades to Raw.
// Math structs are finitely bounded (deepest is Transform3D -> Basis -> Vector3)
// and objects are shallow (no property walk), so they cannot recurse without
// bound and are encoded regardless of `depth`.
static void gdscript_ct_encode_variant_rec(const Variant &value, int depth) {
	switch (value.get_type()) {
		case Variant::NIL:
			ct_value_write_none_typed(g_ct_encoder, g_ct_type_none);
			return;
		case Variant::BOOL:
			ct_value_write_bool_typed(g_ct_encoder, (bool)value ? 1 : 0, g_ct_type_bool);
			return;
		case Variant::INT:
			ct_value_write_int(g_ct_encoder, (int64_t)value, g_ct_type_int);
			return;
		case Variant::FLOAT:
			ct_value_write_float(g_ct_encoder, (double)value, g_ct_type_float);
			return;
		case Variant::STRING:
		case Variant::STRING_NAME: {
			CharString s = String(value).utf8();
			ct_value_write_string(g_ct_encoder,
					(const uint8_t *)s.get_data(), (size_t)s.length(), g_ct_type_string);
			return;
		}
		case Variant::ARRAY: {
			// Untyped Array and typed Array[T] are both Variant::ARRAY.
			if (depth <= 0) {
				gdscript_ct_write_raw(value);
				return;
			}
			gdscript_ct_ensure_collection_types();
			const Array arr = value.operator Array();
			const int n = arr.size();
			ct_value_begin_sequence(g_ct_encoder, g_ct_type_seq, n);
			for (int i = 0; i < n; i++) {
				gdscript_ct_encode_variant_rec(arr[i], depth - 1);
			}
			ct_value_end_compound(g_ct_encoder);
			return;
		}
		case Variant::PACKED_BYTE_ARRAY:
			gdscript_ct_ensure_collection_types();
			CT_ENCODE_PACKED_SEQ(PackedByteArray,
					ct_value_write_int(g_ct_encoder, (int64_t)_a[_i], g_ct_type_int));
			return;
		case Variant::PACKED_INT32_ARRAY:
			gdscript_ct_ensure_collection_types();
			CT_ENCODE_PACKED_SEQ(PackedInt32Array,
					ct_value_write_int(g_ct_encoder, (int64_t)_a[_i], g_ct_type_int));
			return;
		case Variant::PACKED_INT64_ARRAY:
			gdscript_ct_ensure_collection_types();
			CT_ENCODE_PACKED_SEQ(PackedInt64Array,
					ct_value_write_int(g_ct_encoder, (int64_t)_a[_i], g_ct_type_int));
			return;
		case Variant::PACKED_FLOAT32_ARRAY:
			gdscript_ct_ensure_collection_types();
			CT_ENCODE_PACKED_SEQ(PackedFloat32Array,
					ct_value_write_float(g_ct_encoder, (double)_a[_i], g_ct_type_float));
			return;
		case Variant::PACKED_FLOAT64_ARRAY:
			gdscript_ct_ensure_collection_types();
			CT_ENCODE_PACKED_SEQ(PackedFloat64Array,
					ct_value_write_float(g_ct_encoder, (double)_a[_i], g_ct_type_float));
			return;
		case Variant::PACKED_STRING_ARRAY:
			gdscript_ct_ensure_collection_types();
			CT_ENCODE_PACKED_SEQ(PackedStringArray, {
				CharString s = _a[_i].utf8();
				ct_value_write_string(g_ct_encoder,
						(const uint8_t *)s.get_data(), (size_t)s.length(), g_ct_type_string);
			});
			return;
		case Variant::DICTIONARY: {
			// Untyped Dictionary and typed Dictionary[K,V] are both
			// Variant::DICTIONARY. Encode as a Sequence of 2-element (key, value)
			// Tuples — the Python/Ruby dict pattern the UI renders uniformly.
			if (depth <= 0) {
				gdscript_ct_write_raw(value);
				return;
			}
			gdscript_ct_ensure_collection_types();
			const Dictionary d = value.operator Dictionary();
			const Array keys = d.keys(); // Godot preserves insertion order.
			const int n = keys.size();
			ct_value_begin_sequence(g_ct_encoder, g_ct_type_dict, n);
			for (int i = 0; i < n; i++) {
				const Variant k = keys[i];
				ct_value_begin_tuple(g_ct_encoder, g_ct_type_pair, 2);
				gdscript_ct_encode_variant_rec(k, depth - 1);
				gdscript_ct_encode_variant_rec(d[k], depth - 1);
				ct_value_end_compound(g_ct_encoder);
			}
			ct_value_end_compound(g_ct_encoder);
			return;
		}
		// GF4: string/handle types that map to a plain String.
		case Variant::NODE_PATH: {
			const NodePath np = value.operator NodePath();
			gct_str(String(np));
			return;
		}
		// GF4: math / struct types -> named-field Struct (one type per Godot
		// type). Interns its Struct type lazily via gct_stype on first use.
		case Variant::VECTOR2:
			gct_vec2(value.operator Vector2());
			return;
		case Variant::VECTOR2I:
			gct_vec2i(value.operator Vector2i());
			return;
		case Variant::VECTOR3:
			gct_vec3(value.operator Vector3());
			return;
		case Variant::VECTOR3I:
			gct_vec3i(value.operator Vector3i());
			return;
		case Variant::VECTOR4:
			gct_vec4(value.operator Vector4());
			return;
		case Variant::VECTOR4I:
			gct_vec4i(value.operator Vector4i());
			return;
		case Variant::RECT2:
			gct_rect2(value.operator Rect2());
			return;
		case Variant::RECT2I:
			gct_rect2i(value.operator Rect2i());
			return;
		case Variant::PLANE:
			gct_plane(value.operator Plane());
			return;
		case Variant::QUATERNION:
			gct_quat(value.operator Quaternion());
			return;
		case Variant::AABB:
			gct_aabb(value.operator ::AABB());
			return;
		case Variant::BASIS:
			gct_basis(value.operator Basis());
			return;
		case Variant::TRANSFORM2D:
			gct_xform2d(value.operator Transform2D());
			return;
		case Variant::TRANSFORM3D:
			gct_xform3d(value.operator Transform3D());
			return;
		case Variant::PROJECTION:
			gct_projection(value.operator Projection());
			return;
		case Variant::COLOR:
			gct_color(value.operator Color());
			return;
		// GF4: handle types -> shallow Struct.
		case Variant::RID:
			gct_rid(value.operator ::RID());
			return;
		case Variant::CALLABLE:
			gct_callable(value.operator Callable());
			return;
		case Variant::SIGNAL:
			gct_signal(value.operator Signal());
			return;
		case Variant::OBJECT:
			gct_object(value.operator Object *());
			return;
		// GF4: packed struct-arrays -> Sequence of struct elements (GF3 Raw
		// deferrals, now supported).
		case Variant::PACKED_VECTOR2_ARRAY:
			gdscript_ct_ensure_collection_types();
			CT_ENCODE_PACKED_STRUCT_SEQ(PackedVector2Array, gct_vec2);
			return;
		case Variant::PACKED_VECTOR3_ARRAY:
			gdscript_ct_ensure_collection_types();
			CT_ENCODE_PACKED_STRUCT_SEQ(PackedVector3Array, gct_vec3);
			return;
		case Variant::PACKED_VECTOR4_ARRAY:
			gdscript_ct_ensure_collection_types();
			CT_ENCODE_PACKED_STRUCT_SEQ(PackedVector4Array, gct_vec4);
			return;
		case Variant::PACKED_COLOR_ARRAY:
			gdscript_ct_ensure_collection_types();
			CT_ENCODE_PACKED_STRUCT_SEQ(PackedColorArray, gct_color);
			return;
		default:
			// Anything still unhandled remains the Raw printed form (bounded by
			// Godot's recursion-guarded Variant->String).
			gdscript_ct_write_raw(value);
			return;
	}
}

#undef CT_ENCODE_PACKED_STRUCT_SEQ

#undef CT_ENCODE_PACKED_SEQ

static void gdscript_ct_encode_variant(const Variant &value) {
	ct_value_encoder_reset(g_ct_encoder);
	gdscript_ct_encode_variant_rec(value, CT_MAX_VALUE_DEPTH);
}

// G4/GF8: encode `p_value` and attach it to the CURRENT step under `p_name`.
// Shared by the stack-slot path (gdscript_ct_trace_assign) and the member-name
// paths (the ADDR_TYPE_MEMBER branch below + gdscript_ct_trace_member_assign).
// Callers guarantee the writer exists and the first step was emitted. Skips
// empty / `@`-prefixed synthetic names (loop iterators, compiler temporaries),
// mirroring codetracer-nim's resolveTracedSlotSym. Scalar-only recordings keep
// the byte-identical 6-entry types table: gdscript_ct_encode_variant only
// interns collection/struct types when it actually encounters such a value.
static void gdscript_ct_emit_named_value(const String &p_name, const Variant &p_value) {
	if (p_name.is_empty() || p_name[0] == '@') {
		return;
	}
	gdscript_ct_ensure_types();
	if (!g_ct_encoder) {
		return;
	}
	gdscript_ct_encode_variant(p_value);
	size_t cbor_len = 0;
	const uint8_t *cbor = ct_value_get_bytes(g_ct_encoder, &cbor_len);
	if (!cbor || cbor_len == 0) {
		return;
	}
	CharString name_cs = p_name.utf8();
	trace_writer_register_variable_cbor(g_ct_writer,
			name_cs.get_data(), cbor, cbor_len);
}

// A write to a NAMED local / argument / member. The GDScriptTracer hook has
// already resolved the slot/opcode to its declared name (via the debugger's own
// slot->name tables), so this consumer only encodes the Variant and attaches it
// to the current step. Covers the stack-slot path (OPCODE_ASSIGN* into a local),
// the instance-member path (ADDR_TYPE_MEMBER), and the direct-name member-write
// opcodes (SET_MEMBER / SET_NAMED / SET_NAMED_VALIDATED / SET_STATIC_VARIABLE).
static void gdscript_ct_trace_variable_write(const StringName &p_name, const Variant &p_value) {
	CtEmitLock lk; // GF12: serialize + reentrancy-guard
	if (!lk.engaged) {
		return;
	}
	// A value can only attach to an already-registered step; refuse otherwise
	// so values.dat stays parallel-indexed to steps.dat.
	if (g_ct_disabled || !g_ct_writer || !g_ct_started) {
		return;
	}
	// GF12: attach only if THIS thread still owns the writer's pending step. If
	// another thread has registered a step (or a thread event flushed ours) since
	// this thread's own step, the pending slot is foreign and attaching here would
	// misattach the value — drop it instead (parallel-index safety). On a single-
	// threaded program the owner is always this thread, so this never drops.
	if (g_ct_pending_owner != gdscript_ct_current_thread()) {
		return;
	}
	// gdscript_ct_emit_named_value still skips empty / `@`-prefixed synthetic
	// names (loop iterators, compiler temporaries) as a defensive filter.
	gdscript_ct_emit_named_value(String(p_name), p_value);
}

// A write to a stack/member slot addressed by OPERAND (OPCODE_ASSIGN* /
// OPCODE_OPERATOR* into a local, or an ADDR_TYPE_MEMBER slot). Unlike the
// direct-name writes above, the declared name is resolved HERE — under the emit
// lock, AFTER the pending-step ownership check — by calling the engine-side
// resolver gdscript_trace_resolve_slot_name(). Doing the (List-allocating)
// resolution INSIDE the lock rather than eagerly in the VM hook keeps the window
// between this thread's step and its value tiny, so a concurrent worker thread
// cannot register its own step and steal the writer's single shared pending-step
// slot mid-write, which would force this value to be dropped (GF12). This mirrors
// the pre-refactor gdscript_ct_trace_assign ordering exactly. `p_func` is opaque
// here (only handed back to the resolver), so this consumer stays free of any VM
// header.
static void gdscript_ct_trace_slot_write(const GDScriptFunction *p_func, int p_dest_address,
		const Variant &p_value, int p_line) {
	CtEmitLock lk; // GF12: serialize + reentrancy-guard
	if (!lk.engaged) {
		return;
	}
	// A value can only attach to an already-registered step; refuse otherwise so
	// values.dat stays parallel-indexed to steps.dat.
	if (g_ct_disabled || !g_ct_writer || !g_ct_started || !p_func) {
		return;
	}
	// GF12: attach only if THIS thread still owns the writer's pending step (see
	// gdscript_ct_trace_variable_write). Checked BEFORE resolution so a foreign
	// slot costs nothing and — crucially — resolution runs while we still hold the
	// lock and the ownership, closing the steal window that eager resolution opened.
	if (g_ct_pending_owner != gdscript_ct_current_thread()) {
		return;
	}
	StringName name = gdscript_trace_resolve_slot_name(p_func, p_dest_address, p_line);
	if (name == StringName()) {
		// Compiler temporary / unresolvable slot: not a source-level variable.
		return;
	}
	gdscript_ct_emit_named_value(String(name), p_value);
}

// ---------------------------------------------------------------------------
// Consumer registration.
//
// This recorder implements the general GDScriptTracer interface by forwarding
// each callback to the entry points above, and registers a single instance at
// static-init time (which runs before GDScriptLanguage is constructed) when
// CT_GDSCRIPT_TRACE is set. When the env var is unset no tracer is registered,
// so a build that includes this file behaves exactly like a stock+hook engine
// (the VM's tracer pointer stays null and every seam is a no-op).
class GdscriptCtTracer : public GDScriptTracer {
public:
	virtual void on_line(const StringName &p_source, int p_line) override {
		gdscript_ct_trace_step(p_source, p_line);
	}
	virtual void on_call(const StringName &p_function, const StringName &p_source, int p_line) override {
		gdscript_ct_trace_call(p_function, p_source, p_line);
	}
	virtual void on_return(const Variant &p_return_value) override {
		gdscript_ct_trace_return(p_return_value);
	}
	virtual void on_variable_write(const StringName &p_name, const Variant &p_value) override {
		gdscript_ct_trace_variable_write(p_name, p_value);
	}
	virtual void on_slot_write(const GDScriptFunction *p_func, int p_dest_address, const Variant &p_value, int p_line) override {
		gdscript_ct_trace_slot_write(p_func, p_dest_address, p_value, p_line);
	}
	virtual void on_await_suspend(const void *p_context_id) override {
		gdscript_ct_trace_await_suspend(p_context_id);
	}
	virtual void on_await_resume(const void *p_context_id) override {
		gdscript_ct_trace_await_resume(p_context_id);
	}
	virtual void on_utility_call(const StringName &p_function, const Variant **p_args, int p_argc) override {
		gdscript_ct_trace_utility_diagnostic(p_function, p_args, p_argc);
	}
	virtual bool wants_local_tracking() const override { return true; }
};

#if defined(CT_HCR_AGENT_ENABLED)
// ===========================================================================
// GDH-M5 — the engine reloads, from a point the RECORDER controls.
//
// Design: codetracer-specs/Planned-Features/
//         GDScript-Hot-Reload-Multi-Version-Sources.md §5.2, §5.3, §5.6.
//
// No new reload machinery. §5.1 measured that Godot's own path is already in a
// headless `template_debug` build: `GDScriptLanguage::reload_scripts` is
// `#ifdef DEBUG_ENABLED` (gdscript.cpp:2472/:2604), not TOOLS_ENABLED, and it
// re-reads the `.gd` from disk at :2509 before recompiling. What this file adds
// is the TIMING — a reload applied where the recorder can bracket it — and the
// REPORTING of what that reload does not preserve.
//
// Three things are load-bearing and each is here because the obvious
// alternative is wrong:
//
//  1. `reload_scripts`, not `GDScript::reload()`. `reload()` alone parses the
//     in-memory `source` member (gdscript.cpp:818) and never re-reads disk, so
//     it would recompile v1 and report success. `reload_scripts` is the wrapper
//     that calls `load_source_code` first.
//  2. The apply happens at a SAFE POINT the engine chooses, never where the
//     notification landed. A `sourceChanged` delivered on the agent's own
//     thread while the VM is mid-step is queued and the answering thread waits;
//     `gdscript_ct_hcr_safe_point()` applies it between frames and wakes the
//     waiter. §5.6.3: a pending step's values would otherwise attach across the
//     boundary.
//  3. What the reload did not preserve is MEASURED and reported, not asserted.
//     Static values are read before and after and compared by name, so "a
//     static was lost" is evidence rather than a literal in a status field —
//     which is the `oldCodeRetained: true` defect this campaign keeps finding.
// ===========================================================================

#include "gdscript.h"
// GDH-M8, design §8.1 step 2: the new content is COMPILED — parsed and
// analyzed — before anything is touched. `GDScript::reload()` is not the call
// for that: it installs the result, which is the opposite of a pre-check, and
// GDH-M5's `CT_GDH5_FALSIFY_SCRIPT_RELOAD_ONLY` arm already established it is
// the wrong call here for a neighbouring reason. The parser and the analyzer
// used directly answer "is this a program?" without making it the program.
#include "gdscript_analyzer.h"
#include "gdscript_parser.h"
#include "core/io/resource.h"

#include <chrono>
#include <condition_variable>
#include <memory>
#include <thread> // GDH-M6: the safe point's test-settable apply delay

extern "C" {
#include "repro_hcr_agent.h"
}

namespace {

struct CtReloadRequest {
	// Request.
	String res_path;
	Vector<uint8_t> content;
	unsigned int generation = 0;

	// Result, filled at the safe point.
	bool done = false;
	bool applied = false;
	const char *reason = nullptr;
	String detail;
	uint64_t path_id = 0;
	uint64_t step_index = 0;
	Vector<String> unpreserved;
	int deferred_frames = 0;

	// GDH-M6 recording coordinates. `path_id` above is the id post-reload (the
	// version the steps that follow resolve to); these three say what the
	// marker recorded, so a coordinator can correlate its view of the reload
	// with the container without parsing it.
	uint64_t old_path_id = 0;
	uint64_t reload_ordinal = 0; // 0 == no marker was emitted
	uint64_t in_flight_frames = 0;
};

std::mutex g_ct_reload_mutex;
std::condition_variable g_ct_reload_cv;

// A SHARED pointer, not a raw one to the waiting thread's stack.
//
// The first version queued `&req` from the agent thread's frame and had the
// waiter clear the queue on timeout. That is a use-after-free with a 30-second
// fuse: the safe point takes the pointer under the lock, releases the lock to
// do the apply (which writes a file and recompiles a script, so it is not
// quick), and if the waiter timed out in that window the object it is writing
// into has already been destroyed. Shared ownership makes a timeout mean
// "stop waiting", not "delete what the other thread is using".
#if defined(CT_GDH6_FALSIFY_RAW_POINTER_QUEUE)
// FALSIFIER ARM (gdh6_a_timed_out_deferral_does_not_outlive_its_request):
// restore the ORIGINAL raw pointer into the waiting thread's frame, with the
// waiter clearing it on timeout. The safe point takes the pointer under the
// lock, releases the lock to do the apply — which writes a file and recompiles
// a script, so it is not quick — and a timeout inside that window destroys the
// object the safe point is still writing into. A use-after-free with a
// 30-second fuse, which is exactly why it must be killed by AddressSanitizer's
// REPORT and not by a crash: a fuse this long usually does not blow on demand,
// and an arm that "detects" a defect by segfaulting has not been distinguished
// from any other way of dying.
CtReloadRequest *g_ct_reload_queued = nullptr;
#else
std::shared_ptr<CtReloadRequest> g_ct_reload_queued;
#endif

#if defined(CT_GDH6_FALSIFY_MARKER_OUTSIDE_LOCK)
// FALSIFIER ARM (gdh6_reload_is_discoverable_end_to_end) — ADDED AT GDH-M6's
// REVIEW, 2026-09-11.
//
// `include/codetracer_trace_writer.h` states the constraint this arm breaks,
// in the entry's own prose:
//
//     "Ordering matters and is not enforceable from here: emit the marker from
//      the SAME critical section that applies the reload […]. A marker emitted
//      from a different lock hold can be separated from its apply by any
//      number of steps, and the container then states a boundary the execution
//      did not have."
//
// That is a named hazard with, as of the review, NO falsifier. Every other
// GDH-M6 arm attacks the marker's CONTENT (zeroed payload, no marker at all,
// no version minted); none attacks its POSITION, and position is the whole
// reason the marker exists rather than being inferred from `paths.dat`. An
// implementer who moved the emission out of `ct_apply_reload_locked` — which
// is a natural refactor, since the apply is long and the emission is not —
// would break the gate's central cross-tie and nothing would have gone red.
//
// The mutation is the refactor, done faithfully rather than caricatured: the
// marker is still emitted, still exactly once, still with the correct ids,
// generation, ordinal and in-flight count. It is emitted from the NEXT safe
// point instead of this one — one `Main::iteration()` later — so the new
// version's steps for that frame are written BEFORE the marker that announces
// it. Everything a presence check or a content check looks at is still right.
// Only the POSITION is wrong, and the gate must go red on the cross-tie:
// `before[-1].path_id` is then the NEW id, not the old one.
struct CtDeferredMarker {
	bool pending = false;
	ct_tw_source_reload_change change{};
	uint64_t in_flight_frames = 0;
};
CtDeferredMarker g_ct_deferred_marker;
#endif

// GDH-M6: how long the answering thread waits for the engine's safe point.
//
// The shipping value is 30 s. It is TEST-SETTABLE because the gate that grades
// the timeout path has to reach the timeout, and a gate that takes half a
// minute to answer is a gate that gets disabled — and a disabled gate is not a
// gate. The override is read once, from the environment, and is reported in
// the timeout message itself so a run can never be read as having waited the
// shipping bound when it did not.
int ct_reload_wait_seconds() {
	static int cached = -1;
	if (cached >= 0) {
		return cached;
	}
	cached = 30;
	const char *raw = getenv("CT_GDH6_RELOAD_WAIT_SECONDS");
	if (raw != nullptr && raw[0] != '\0') {
		int parsed = atoi(raw);
		if (parsed > 0) {
			cached = parsed;
		}
	}
	return cached;
}

// GDH-M6: an artificial delay INSIDE the safe point's apply, in milliseconds.
//
// The use-after-free this guards against lives in the window between the safe
// point releasing the emit lock to do its work and the waiter's bound
// expiring. That window is real but short, so a gate that waited for it to
// occur naturally would be a flaky gate. Widening it on request is what makes
// the race deterministic; it is off unless the variable is set, and the safe
// point reports when it is honouring it.
int ct_safe_point_delay_ms() {
	static int cached = -1;
	if (cached >= 0) {
		return cached;
	}
	cached = 0;
	const char *raw = getenv("CT_GDH6_SAFE_POINT_DELAY_MS");
	if (raw != nullptr && raw[0] != '\0') {
		int parsed = atoi(raw);
		if (parsed > 0) {
			cached = parsed;
		}
	}
	return cached;
}

// Counters the gates read off stderr. They are the harness's evidence that the
// deferral path was ENTERED, which the milestone's `anti_vacuity` requires:
// a run in which the race window never opened must fail loudly rather than be
// reported as a pass over a reload that was never deferred.
int g_ct_reload_applied_count = 0;
int g_ct_reload_deferred_count = 0;

// True only inside `gdscript_ct_hcr_safe_point()`. It is what distinguishes
// "the engine chose this moment" from "a notification happened to arrive now".
bool g_ct_in_safe_point = false;

// `static_variables_indices` is private to GDScript and we are not a friend, so
// the values are read through the public property surface —
// `GDScript::_get_property_list` (gdscript.cpp:1052-1076) enumerates exactly
// the static variables of the script and its bases, and `GDScript::_get`
// (:954-:1000) resolves them. That is the same surface `MyClass.my_static`
// uses, so what is measured here is what the program itself would see.
void ct_collect_statics(const Ref<Script> &p_script, List<StringName> &r_names,
		List<Variant> &r_values) {
	if (p_script.is_null()) {
		return;
	}
	List<PropertyInfo> props;
	p_script->get_property_list(&props);
	for (const PropertyInfo &pi : props) {
		// `Object::get_property_list` merges the ClassDB properties of
		// Object/Resource/Script — `source_code`, `resource_path`, `script`,
		// `script/source` — with `GDScript::_get_property_list`'s statics.
		// Only the latter carry `PROPERTY_USAGE_SCRIPT_VARIABLE`
		// (gdscript_compiler.cpp:2897, set on every script-declared variable
		// before it is filed into `static_variables_indices`).
		//
		// This filter is here because its absence was CAUGHT, not anticipated:
		// without it, `source_code` changed across the reload — of course it
		// did, the file was rewritten — and was reported as a lost static. The
		// no-statics control arm of `gdh5_unpreserved_state_is_reported` went
		// red on it, which is exactly what that arm is for: a report that names
		// a loss on a script with no statics is the `oldCodeRetained: true`
		// shape, and it would have shipped looking like a measurement.
		if ((pi.usage & PROPERTY_USAGE_SCRIPT_VARIABLE) == 0) {
			continue;
		}
		bool valid = false;
		Variant value = p_script->get(pi.name, &valid);
		if (!valid) {
			continue;
		}
		r_names.push_back(pi.name);
		r_values.push_back(value);
	}
}

// ===========================================================================
// GDH-M8 — design §8.1, the ordering and the recovery contract.
// ===========================================================================

// §8.1 step 2. Does `p_content` COMPILE, as a GDScript at `p_res_path`?
//
// The check must not install anything: a v2 that does not parse has to leave
// the engine running v1, and a pre-check that swapped the script in to find out
// would have already lost that. `GDScript::reload()` parses the IN-MEMORY
// `source` member and then compiles into `this`, so it is unusable here.
//
// What IS used is the same pair `GDScript::reload()` itself uses, in the same
// order, on a stack-local parser: `GDScriptParser::parse` (gdscript.cpp:813-818,
// whose failure is the ERR_PARSE_ERROR at :820-827) then `GDScriptAnalyzer::
// analyze` (:830-844, a second ERR_PARSE_ERROR). Both are run, because a file
// that tokenizes but does not resolve is just as unrunnable as one that does
// not tokenize, and Godot names both the same way.
//
// GDScriptCompiler is deliberately NOT run. It compiles INTO a GDScript object,
// which is precisely the installation this check exists to avoid; its own
// failure mode is `ERR_COMPILATION_FAILED` (:860) and it is reached, on the
// real script, by `reload_scripts` at step 6. A compile error that the analyzer
// does not catch therefore still reaches the engine — that residue is recorded
// in the milestone rather than papered over here, because closing it means
// compiling into a throwaway GDScript and that has its own installation
// hazards (`GDScriptCache`, inner classes, `make_scripts`).
//
// Returns true when the content is a program. On false, `r_detail` names the
// first error with its line, so the refusal is diagnosable from the wire.
//
// THE LINE FIELD IS `start_line`, NOT `line`, AND THE CHOICE IS THE ENGINE'S.
// Godot 4.7 replaced `ParserError`'s `int line, column` with a SPAN —
// `start_line`/`start_column`/`end_line`/`end_column` (gdscript_parser.h:263-278)
// — so `.line` stopped compiling at the 4.7.2 rebase. `start_line` is not merely
// the surviving spelling: it is the one `GDScript::reload()` itself passes to
// `_err_print_error` in BOTH error branches (gdscript.cpp:825 and :840) and to
// `debug_break_parse` (:822, :835). Reporting `end_line` here instead would put
// a different line in this refusal's `detail` than in the engine's own "Parse
// Error: … at res://<path>:NN" on the same failure, and GDH-M8b's gate reads
// both — so the two must name one line, and it must be the engine's.
bool ct_gdscript_content_compiles(const String &p_res_path,
		const Vector<uint8_t> &p_content, String &r_detail) {
	String source;
	if (!p_content.is_empty()) {
		source = String::utf8((const char *)p_content.ptr(), p_content.size());
	}

	GDScriptParser parser;
	Error err = parser.parse(source, p_res_path, /*p_for_completion=*/false);
	if (err != OK) {
		const List<GDScriptParser::ParserError>::Element *e = parser.get_errors().front();
		r_detail = "the new content does not parse as GDScript";
		if (e != nullptr) {
			r_detail += ": line " + itos(e->get().start_line) + ": " + e->get().message;
		}
		return false;
	}

	GDScriptAnalyzer analyzer(&parser);
	err = analyzer.analyze();
	if (err != OK) {
		const List<GDScriptParser::ParserError>::Element *e = parser.get_errors().front();
		r_detail = "the new content parses but does not analyze";
		if (e != nullptr) {
			r_detail += ": line " + itos(e->get().start_line) + ": " + e->get().message;
		}
		return false;
	}
	return true;
}

// GDH-M8b — CAPTURING THE COMPILER'S OWN MESSAGE.
//
// `GDScriptLanguage::reload_scripts` returns void and `GDScript::reload()`'s
// `ERR_COMPILATION_FAILED` is dropped on the floor by it (gdscript.cpp:2560),
// so the only in-process statement that the compiler refused the new source is
// `GDScript::is_valid()` going false. That answers WHETHER but not WHY, and a
// refusal whose `detail` says only "it did not compile" is a code with no
// diagnostic attached — the shape §5.5 exists to keep off this wire.
//
// Godot does print the reason: `_err_print_error("GDScript::reload", path,
// compiler.get_error_line(), "Compile Error: " + compiler.get_error(), false,
// ERR_HANDLER_SCRIPT)` at gdscript.cpp:854. Every error handler on the engine's
// own list sees it, so one is installed for the duration of the swap and taken
// off again immediately. Nothing is intercepted or suppressed: the handler
// COPIES the first script-level error it sees and the normal printer still runs.
//
// It is deliberately not a general error sink. It is armed for exactly one call,
// it keeps only the FIRST message (the compiler stops at its first error), and
// it is disarmed in the same block, so it cannot accumulate state across reloads
// or attribute an unrelated error to this one.
struct CtGdh8CompileErrorSink {
	String message;
	int line = -1;
	bool captured = false;
	ErrorHandlerList entry;

	static void handle(void *p_self, const char *, const char *, int p_line,
			const char *p_error, const char *p_explanation, bool,
			ErrorHandlerType p_type) {
		CtGdh8CompileErrorSink *self = (CtGdh8CompileErrorSink *)p_self;
		if (self == nullptr || self->captured || p_type != ERR_HANDLER_SCRIPT) {
			return;
		}
		self->captured = true;
		self->line = p_line;
		self->message = String::utf8(p_error != nullptr ? p_error : "");
		if (p_explanation != nullptr && p_explanation[0] != '\0') {
			self->message += " (" + String::utf8(p_explanation) + ")";
		}
	}

	CtGdh8CompileErrorSink() {
		entry.errfunc = &CtGdh8CompileErrorSink::handle;
		entry.userdata = this;
		add_error_handler(&entry);
	}
	~CtGdh8CompileErrorSink() { remove_error_handler(&entry); }
};

#if !defined(CT_GDH8_NO_INJECTION_HOOK)
// The fault-injection hook of `gdh8_a_failure_after_registration_closes_the_
// trace_rather_than_continuing`.
//
// §8.1 says a failure at steps 4-6 is not recoverable by continuing. There is
// no way to grade that with a test double: a double would prove the harness's
// ordering rather than the product's, so the failure has to be injected into
// the shipped path. That makes the hook a PRODUCTION SURFACE, and the milestone
// accepts it on one condition — it must be inert unless explicitly armed, and
// its inertness must be MEASURED rather than asserted in a comment.
//
// So: it is off unless `CT_GDH8_INJECT_FAILURE` names a stage; it reads the
// variable once; it prints when it fires; and `CT_GDH8_NO_INJECTION_HOOK`
// compiles it out entirely, so a harness can show that an unarmed build with
// the hook produces a container byte-identical to a build without it. A
// campaign that added a supported way to corrupt a trace would not have
// improved on the defect it was fixing.
//
// Stages, spelled as §8.1 numbers them:
//   "bundle"  — step 4, after the version is minted and before its view exists
//   "marker"  — step 5, after the view and before the boundary is recorded
//   "swap"    — step 6, after the trace is committed and before the engine is
const char *ct_gdh8_injected_stage() {
	static const char *cached = nullptr;
	static bool read_once = false;
	if (!read_once) {
		read_once = true;
		const char *raw = getenv("CT_GDH8_INJECT_FAILURE");
		if (raw != nullptr && raw[0] != '\0') {
			cached = raw;
		}
	}
	return cached;
}

bool ct_gdh8_inject_at(const char *p_stage) {
	const char *armed = ct_gdh8_injected_stage();
	if (armed == nullptr || strcmp(armed, p_stage) != 0) {
		return false;
	}
	fprintf(stderr, "[ct-gdh8] FAULT INJECTED at design §8.1 stage \"%s\" "
					"(CT_GDH8_INJECT_FAILURE); this build carries the "
					"injection hook and it is ARMED\n", p_stage);
	fflush(stderr);
	return true;
}
#else
bool ct_gdh8_inject_at(const char *) { return false; }
#endif

// §8.1's recovery contract: "a failure at 4-6 is NOT recoverable by continuing:
// the recorder closes the trace with a recorded reason and the engine continues
// unreloaded, which is a degraded session with a coherent trace rather than a
// normal session with a wrong one."
//
// Implemented exactly that way. The reason is written INTO the container as an
// events-stream record before the streams are finished, so it survives to a
// reader rather than living only on the session's stderr — a trace that stops
// for a reason nobody can recover from it is the `oldCodeRetained: true` shape
// one layer down. Then the writer is closed and recording is DISABLED, so no
// step after this point can be attributed to a version the container half
// registered.
//
// MUST be called with the emit lock held.
void ct_close_trace_with_reason(const String &p_stage, const String &p_detail) {
#if defined(CT_GDH8_FALSIFY_CLOSE_WITHOUT_REASON)
	// FALSIFIER ARM, added by the GDH-M8 REVIEW 2026-09-12 (gdh8_a_failure_
	// after_registration_closes_the_trace_rather_than_continuing): CLOSE THE
	// TRACE, and record no reason in it.
	//
	// Why this arm and not another. §8.1's contract has two halves — "closes
	// the trace" AND "with a recorded reason" — and only the first half had an
	// arm. `CT_GDH8_FALSIFY_CONTINUE_AFTER_FAILURE` breaks the close and is
	// killed by the orphaned-source-view check; NOTHING killed the second half,
	// so the claim "the reason the recording stopped is RECORDED IN THE
	// CONTAINER" had never been shown to be able to go red. This arm is
	// precisely the difference: the trace still closes, still decodes, still
	// carries no orphaned id, the wire answer is still `trace-closed` with the
	// stage in `detail`, and the engine still keeps running v1 — every other
	// claim in the gate stays green. What is gone is the one thing a READER of
	// the container could have used: the recording simply stops, and why it
	// stopped lives only on a session's stderr, which no consumer has. That is
	// the `oldCodeRetained: true` shape one layer down, and it is exactly the
	// silent degradation this campaign exists to prevent.
	//
	// It must go red ON THAT CLAIM and on no other.
	fprintf(stderr, "[ct-gdh8] CT_GDH8_FALSIFY_CLOSE_WITHOUT_REASON: closing "
					"the trace WITHOUT writing the reason into the container\n");
	fflush(stderr);
#else
	if (g_ct_writer != nullptr) {
		CharString meta_cs = p_stage.utf8();
		// Milestone HX-S-9: Godot's String(const char *) and operator+(const char *, String)
		// call append_latin1 (ustring.h:677, :711), widening each byte to a codepoint.
		// UTF-8 '§' (0xC2 0xA7) becomes U+00C2 U+00A7 and .utf8() re-encodes it as 'Â§'.
		// We explicitly wrap non-ASCII literals in String::utf8(...) to ensure cleanly
		// encoded UTF-8 in the container's FFI_EVENT_ERROR record without 'Â'.
		CharString content_cs = (String::utf8("codetracer: the recording was closed at design "
								 "§8.1 stage ") + p_stage +
				" because the reload could not be completed coherently: " +
				p_detail).utf8();
		trace_writer_register_special_event(g_ct_writer, FFI_EVENT_ERROR,
				meta_cs.get_data(), content_cs.get_data());
	}
#endif
	fprintf(stderr, "[ct-gdh8] CLOSING THE TRACE at §8.1 stage %s: %s\n",
			p_stage.utf8().get_data(), p_detail.utf8().get_data());
	fflush(stderr);
	gdscript_ct_close();
	// Nothing more is recorded. `g_ct_inited` stays true and `g_ct_writer` is
	// null, so `gdscript_ct_ensure_writer` answers false on every later hook
	// without trying to build a second writer over the closed container.
	g_ct_disabled = true;
}

#if defined(CT_GDH8_FALSIFY_CONTINUE_AFTER_FAILURE)
// FALSIFIER ARM (gdh8_a_failure_after_registration_closes_the_trace_rather_
// than_continuing): on a failure at §8.1 steps 4-6, REPORT it and keep
// recording instead of closing. The version has been minted, so every step
// after this point is attributed to a path id whose source view was never
// written — and the container then holds steps against a version it has only
// half registered. The gate must go red by finding that incoherence IN THE
// CONTAINER, not by observing that a close was not called.
const bool ct_gdh8_close_on_late_failure = false;
#else
const bool ct_gdh8_close_on_late_failure = true;
#endif

// The failure verdict for §8.1 steps 4-6, in one place so every site reaches
// the same end state: the trace is closed with the reason recorded in it, the
// engine is left UNRELOADED, and the coordinator is told by name.
//
// Returns TRUE when the caller must abort — which is what it always does in a
// shipping build. It returns false only under the falsifier arm, which is what
// "continue instead of closing" means.
//
// GDH-M8b added `p_reason`. It defaults to `trace-closed`, which is what every
// pre-GDH-M8b site passes and what §5.5 says a steps-4-6 failure answers when
// the CONSEQUENCE is the only thing a coordinator can act on. The compile
// failure is the one case where it is not: the cause is the user's own new
// source, the coordinator's next move is to show them the compiler's message,
// and collapsing that into `trace-closed` would repeat the `writer-refused`
// mistake this milestone spent its whole review splitting apart. The trace
// consequence does not disappear — it is stated in `detail` and recorded in the
// container exactly as for every other site.
bool ct_reload_fail_after_registration(CtReloadRequest &req, const String &p_stage,
		const String &p_detail,
		const char *p_reason = REPRO_HCR_RELOAD_REASON_TRACE_CLOSED) {
	if (!ct_gdh8_close_on_late_failure) {
		// THE FALSIFIER ARM. Keep going, and keep recording. The version is
		// minted and every step after this point is attributed to it, so the
		// container ends up holding steps against a path id whose source view
		// was never written — an incoherence detectable IN THE CONTAINER,
		// which is where the gate must find it.
		fprintf(stderr, "[ct-gdh8] CT_GDH8_FALSIFY_CONTINUE_AFTER_FAILURE: "
						"NOT closing the trace after a §8.1 stage %s failure "
						"(%s); the recording continues against a "
						"half-registered version\n",
				p_stage.utf8().get_data(), p_detail.utf8().get_data());
		fflush(stderr);
		return false;
	}
	req.applied = false;
	req.reason = p_reason;
	// Milestone HX-S-9: String(const char *) is Latin-1, not UTF-8 (ustring.h:677, :688-691).
	// Wrap non-ASCII literals in String::utf8(...) so req.detail carries
	// clean UTF-8 '§' (0xC2 0xA7) rather than double-encoded 'Â§' on the wire.
	req.detail = String::utf8("design §8.1 step ") + p_stage +
			" failed after the trace had committed to the new version, so the "
			"recording was closed rather than continued: " +
			p_detail;
	ct_close_trace_with_reason(p_stage, p_detail);
	return true;
}

// §8.1 STEP 5, on its own because one falsifier arm has to run it apart from
// step 3. MUST be called with the emit lock held, from the same critical
// section as the apply: a marker emitted from a different lock hold can be
// separated from its apply by any number of steps, and the container then
// states a boundary the execution did not have.
bool ct_reload_emit_marker_locked(CtReloadRequest &req, uint64_t p_old_id,
		uint64_t p_new_id) {
	// --- §8.1 STEP 5. Record the boundary.
	//
	// Frames still executing the OLD version's bytecode, MEASURED.
	// `g_ct_crossing_stack` is the recorder's own LIFO of open GDScript frames,
	// maintained at exactly the sites the writer's call/return records are. At
	// an engine safe point it is empty and this is 0 — but it is read rather
	// than assumed, because design §5.4 says steps belonging to in-flight
	// frames legitimately appear after the marker carrying the OLD id, and a
	// consumer must not read the marker as a clean cut on the strength of a
	// literal.
	req.in_flight_frames = (uint64_t)g_ct_crossing_stack.size();
	ct_tw_source_reload_change change;
	change.old_path_id = p_old_id;
	change.new_path_id = p_new_id;
	change.generation = (uint64_t)req.generation;
	trace_writer_clear_last_error();
#if defined(CT_GDH6_FALSIFY_NO_MARKER)
	// FALSIFIER ARM (gdh6_reload_is_discoverable_end_to_end): mint the version
	// and emit NO marker. Every path index is correct, every step is attributed
	// to the version that ran it, and a consumer could still INFER a transition
	// by scanning paths.dat for a repeated string. The gate must still go red,
	// because design §6.3.1 requires the boundary to be RECORDED: an inference
	// cannot say where in the step stream the transition happened, which ids it
	// ran between, or which wire generation was installed. This arm is the whole
	// reason that gate exists separately from GDH-G3.
	//
	// GDH-M8 NOTE: this arm must NOT take the §8.1 recovery path. It models a
	// host that never asked for a marker, not a writer that refused one, and
	// turning it into a trace-closing failure would stop it reproducing the
	// defect the discoverability gate is aimed at.
	(void)change;
	req.reload_ordinal = 0;
	return true;
#elif defined(CT_GDH6_FALSIFY_MARKER_OUTSIDE_LOCK)
	// FALSIFIER ARM (see the note on `g_ct_deferred_marker`): hand the marker to
	// the NEXT safe point instead of emitting it here. Nothing about its
	// contents changes — only the lock hold it is emitted from, which is
	// precisely what the C header says is not enforceable from its side.
	g_ct_deferred_marker.change = change;
	g_ct_deferred_marker.in_flight_frames = req.in_flight_frames;
	g_ct_deferred_marker.pending = true;
	req.reload_ordinal = 1; // not the writer's; the arm is about position
	return true;
#else
	if (ct_gdh8_inject_at("marker")) {
		if (ct_reload_fail_after_registration(req, "5 (emit the boundary marker)",
					String::utf8("injected failure at §8.1 step 5"))) {
			return false;
		}
		return true; // falsifier arm only: no marker, and the reload proceeds
	}
	uint64_t ordinal = trace_writer_register_source_reload(
			g_ct_writer, &change, 1, req.in_flight_frames);
	if (ordinal == CT_TW_INVALID_RELOAD_ORDINAL) {
		// A refused marker is a §8.1 step 5 failure: the version exists and
		// carries a view, and the container would state a transition it cannot
		// locate. Not recoverable by continuing.
		if (ct_reload_fail_after_registration(req, "5 (emit the boundary marker)",
					String("the boundary marker was refused: ") +
							String::utf8(trace_writer_last_error()))) {
			return false;
		}
		return true;
	}
	req.reload_ordinal = ordinal;
	return true;
#endif

}

// §8.1 steps 3-5, under the emit lock, at an engine safe point, and BEFORE the
// engine swap. Returns true when the caller may proceed to step 6.
//
// `g_ct_writer` is non-null (the caller checked): a process that is not
// recording has no trace half to run and goes straight to the swap.
//
// Order within the block matters. `register_path_version` must come first,
// because `registerSourceReload` refuses `old_path_id == new_path_id` — a
// reload that minted no new index cannot attribute its post-reload steps to the
// version that ran them. After it, a bare `trace_writer_register_step` on the
// same string resolves to the NEW id, so the recorder's hot path stays
// version-unaware; only this path is version-aware.
//
// Every refusal is REPORTED, never swallowed. A reload that applied but
// recorded nothing is the exact shape GDH-M0 measured.
bool ct_reload_register_in_trace_locked(CtReloadRequest &req, bool p_emit_marker) {
	CharString path_cs = req.res_path.utf8();
	// §4.3's recording coordinates, so the coordinator can correlate its view
	// of the reload with the trace without parsing the container. They are
	// asked for, never counted: `trace_writer_next_step_index` is the writer's
	// own counter and not a count of `register_step` calls.
	uint64_t id = trace_writer_current_path_id(g_ct_writer, path_cs.get_data());
	req.path_id = (id == CT_TW_INVALID_PATH_ID) ? 0 : id;
	req.step_index = trace_writer_next_step_index(g_ct_writer);

#if defined(CT_GDH6_FALSIFY_NO_VERSION_MINTED)
	// FALSIFIER ARM (gdh6_no_step_is_attributed_to_the_wrong_version, arm 1):
	// apply the reload and mint NO path version. Every post-reload step then
	// resolves, by the writer's ordinary interning, to v1's path id — which is
	// exactly the state GDH-M0 measured and the state this milestone exists to
	// leave. The reload itself still happens: the file is rewritten, the script
	// is recompiled, the program plainly prints v2's and v3's tokens, and the
	// acknowledgement says `applied`. Only the TRACE is wrong, and only about
	// which version ran.
	//
	// It also takes the marker down with it, necessarily rather than
	// incidentally: `registerSourceReload` refuses `old == new`, so a version
	// that was never minted has no transition to record. The driver aims this
	// arm at the attribution gate and states that it reddens the
	// discoverability gate too, rather than hiding it.
	//
	// GDH-M8 NOTE. This arm must keep RETURNING TRUE — it is the "applied but
	// recorded nothing" state, and an arm that started refusing the reload
	// instead would stop reproducing the defect it names.
	req.unpreserved.push_back(
			"source-version-not-minted:CT_GDH6_FALSIFY_NO_VERSION_MINTED");
	return true;
#else
	if (id == CT_TW_INVALID_PATH_ID) {
		// The writer has never seen this file — it was reloaded before it ever
		// executed. There is no old version to transition FROM, so there is
		// nothing truthful to record, and equally nothing INCOHERENT about
		// continuing: the file will be interned fresh at its first step, in one
		// version, exactly as it would have been without the reload. §8.1's
		// invariant is not violated, so this reports and proceeds.
		req.unpreserved.push_back(
				"source-version-not-minted:the recorder has never seen " +
				req.res_path + ", so there is no old path id to record a "
							   "transition from");
		return true;
	}
	if (!g_ct_line_count_table) {
		// This one IS §8.1's forbidden state and until GDH-M8 it proceeded
		// anyway. Without the table a second paths.dat record carries no size,
		// so the recording CANNOT represent a second version — and the reload
		// would then run v2 while every one of its steps was attributed to v1.
		// "A trace that cannot represent the new version must not be allowed to
		// have one": refuse, keep v1 live, nothing touched.
		req.applied = false;
		req.reason = REPRO_HCR_RELOAD_REASON_WRITER_REFUSED;
		req.detail = "writer-refused/line-count-table: this writer has no "
					 "line-count table (meta.dat bit 14), so a second paths.dat "
					 "record would carry no size and both versions would share "
					 "the DefaultLinesPerFile stride; the reload is refused "
					 "rather than applied against a trace that cannot express it";
		return false;
	}

	// --- §8.1 STEP 3. Mint the version. A refusal here is a CLEAN abort.
	uint64_t new_lines = gdscript_ct_addressable_lines(
			req.content.ptr(), (int64_t)req.content.size());
#if defined(CT_GDH6_FALSIFY_STALE_LINE_COUNT)
	// FALSIFIER ARM (gdh6_no_step_is_attributed_to_the_wrong_version, arm 5):
	// register the new version with the OLD version's line count. The paths.dat
	// entries are all there, the ids are all right, the marker is well formed —
	// and the new version's slot in the position space is the wrong SIZE, so
	// every one of its lines past the old file's end has no address inside its
	// own slot. Under the line-count table the writer refuses those steps
	// (checkLineWithinFile), so they vanish rather than addressing into the next
	// file, and the gate sees it as a cardinality mismatch. The distinction
	// matters and the harness reports it: without the table, the same mistake
	// would have SILENTLY spilled into the next file's range, which is the
	// GDH-M0 defect.
	if (g_ct_gdh6_last_recorded_lines != 0) {
		new_lines = g_ct_gdh6_last_recorded_lines;
	}
#endif
	trace_writer_clear_last_error();
	uint64_t new_id = trace_writer_register_path_version(
			g_ct_writer, path_cs.get_data(), new_lines);
	if (new_id == CT_TW_INVALID_PATH_ID) {
		// §8.1 step 3: "If the writer refuses, ABORT the reload and keep v1
		// live." Until GDH-M8 this appended a string to `unpreserved` and fell
		// through to `applied = true`, which left v2 on disk AND live in the
		// engine AND reported applied, carrying v1's path id — §8.1's forbidden
		// third state, reached through the failure path instead of the order.
		req.applied = false;
		req.reason = REPRO_HCR_RELOAD_REASON_WRITER_REFUSED;
		req.detail = String("writer-refused/path-version: ") +
				String::utf8(trace_writer_last_error());
		return false;
	}
	req.old_path_id = id;
	req.path_id = new_id;

	// --- §8.1 STEP 4. Bundle the new source view against the new id, FROM THE
	// BYTES THE AGENT VERIFIED.
	//
	// Until GDH-M8 the view was bundled lazily, at the first step AFTER the
	// reload, by reading the file back off disk
	// (`gdscript_ct_note_and_bundle_path_locked`). Two things were wrong with
	// that and both go away here: between the marker and the next step the
	// container named a version whose text it did not carry, and the text it
	// eventually carried was whatever was on disk at that later moment rather
	// than the bytes whose digest was checked.
	//
	// Recording the id in `g_ct_bundled_path_ids` is what keeps the lazy path
	// from bundling it a second time; that path is still the one that bundles
	// the FIRST version of every file, which is not a reload and has no
	// notification to take bytes from.
	bool bundle_injected = ct_gdh8_inject_at("bundle");
#if !defined(CT_GDH8_NO_INJECTION_HOOK)
	if (bundle_injected) {
		// See `g_ct_gdh8_source_views_poisoned`: the modelled failure persists,
		// so the recorder's lazy bundler cannot quietly undo it.
		g_ct_gdh8_source_views_poisoned = true;
	}
#endif
	if (bundle_injected ||
			!gdscript_ct_bundle_bytes_locked(req.res_path, new_id, req.content)) {
		if (ct_reload_fail_after_registration(req, "4 (bundle the new source view)",
					String("the new version's source view could not be attached "
						   "to path id ") +
							itos((int64_t)new_id) + ": " +
							String::utf8(trace_writer_last_error()))) {
			return false;
		}
		// Falsifier arm only: fall through with NO view for `new_id`.
	} else {
		g_ct_bundled_path_ids.insert(new_id);
	}

	if (!p_emit_marker) {
		// FALSIFIER ARM `CT_GDH8_FALSIFY_MINT_BEFORE_COMPILE` only: mint the
		// version and its view here, and leave the boundary marker to the
		// caller, which emits it after the compile check. The arm reorders ONE
		// thing — the mint relative to §8.1's verification — and must not
		// incidentally delete the marker as well, or it would redden gates it
		// is not aimed at and would not have been shown to discriminate.
		req.reload_ordinal = 0;
		return true;
	}
	return ct_reload_emit_marker_locked(req, id, new_id);
#endif // CT_GDH6_FALSIFY_NO_VERSION_MINTED
}

// Apply one queued reload. MUST be called with `g_ct_mutex` held (the emit
// lock, §5.6.2) and from the engine's safe point.
//
// THE ORDER IS THE SUBSTANCE. Design §8.1:
//
//   1. verify the bytes against snapshotDigest and lineCount  [in the AGENT]
//   2. compile the new script; refuse on parse error
//   3. register the versioned path in the TRACE; refuse and keep v1 live
//   4. bundle the new source view against the new id
//   5. emit the TagSourceReload marker
//   6. swap the engine's script resource
//   7. reply
//
// Trace registration precedes the engine swap because a trace that cannot
// represent the new version must not be allowed to have one. The reverse order
// — which is what this function did until GDH-M8 — produces a running engine
// whose execution the recording cannot express, the silent misattribution the
// campaign exists to prevent.
//
// Steps 1-3 are a CLEAN REFUSAL: nothing has been touched, the engine is still
// running v1, the container is exactly what it would have been had the
// notification never arrived. Steps 4-6 are not recoverable by continuing; see
// `ct_close_trace_with_reason`.
void ct_apply_reload_locked(CtReloadRequest &req) {
	req.path_id = 0;
	req.step_index = 0;

#if defined(CT_GDH8_FALSIFY_WRITE_BEFORE_COMPILE)
	// FALSIFIER ARM, added by the GDH-M8 REVIEW 2026-09-12
	// (gdh8_refused_reload_leaves_a_coherent_trace): PUT THE DISK WRITE BACK
	// WHERE IT WAS. Until GDH-M8 storing the notification's bytes was the FIRST
	// thing this function did, so a refusal at any later point left v1 gone from
	// disk while the engine went on running it from memory. Deviation (a) of the
	// audit names that move as half of its fix.
	//
	// It had NO ARM, and the review measured why that mattered: nothing else in
	// the verifier can see it. The raw source view is bundled from disk at the
	// FIRST step, long before any reload, so it still holds v1; the engine keeps
	// running v1 from memory whatever is on disk; the container is unchanged;
	// stdout is unchanged. This arm is killed only by `assert_disk_holds`, which
	// the review added for it.
	//
	// It reddens TWO gates and that is stated rather than glossed: `refused`
	// (the refusal left v2_bad on disk) and `close` (the trace-closing failure
	// left v2_ok on disk though step 6 never ran). Those are the SAME property
	// asserted in two places, not collateral damage — the digest gate, whose
	// refusal happens in the agent before this function is reached at all, stays
	// green.
	{
		Error werr = OK;
		Ref<FileAccess> wfa = FileAccess::open(req.res_path, FileAccess::WRITE, &werr);
		if (wfa.is_valid() && werr == OK && !req.content.is_empty()) {
			wfa->store_buffer(req.content.ptr(), req.content.size());
			wfa->flush();
		}
		fprintf(stderr, "[ct-gdh8] CT_GDH8_FALSIFY_WRITE_BEFORE_COMPILE: wrote "
						"%s to disk BEFORE the compile check\n",
				req.res_path.utf8().get_data());
		fflush(stderr);
	}
#endif

	// -------- FALSIFIER ARMS of gdh8_refused_reload_leaves_a_coherent_trace --
	//
	// The milestone names two, and both are the SAME mistake at two depths:
	// touch the trace before the content has been shown to be usable.
	//
	//   CT_GDH8_FALSIFY_MARKER_BEFORE_COMPILE  — arm 1, "emit the
	//     `TagSourceReload` before attempting the compile". The container then
	//     claims a reload that did not happen and the gate must go red on the
	//     MARKER COUNT.
	//   CT_GDH8_FALSIFY_MINT_BEFORE_COMPILE    — arm 2. The entry's wording is
	//     "register the versioned path before verifying the digest"; the digest
	//     is verified in the AGENT, before this handler is called at all, so
	//     "before the verification that precedes it in §8.1" is the faithful
	//     reading here and step 2 (the compile) is that verification. The
	//     container then carries TWO entries, one of which nothing ever
	//     executed, and the gate must go red on the ENTRY COUNT.
	//
	// Both leave the reload itself refused — the compile check below still
	// fires — so what they change is only what the CONTAINER says, which is
	// what the gate reads.
	bool ct_gdh8_registered_early = false;
#if defined(CT_GDH8_FALSIFY_MARKER_BEFORE_COMPILE) || \
		defined(CT_GDH8_FALSIFY_MINT_BEFORE_COMPILE)
	if (g_ct_writer != nullptr) {
#if defined(CT_GDH8_FALSIFY_MARKER_BEFORE_COMPILE)
		const bool ct_gdh8_arm_emits_marker = true;
#else
		const bool ct_gdh8_arm_emits_marker = false;
#endif
		if (!ct_reload_register_in_trace_locked(req, ct_gdh8_arm_emits_marker)) {
			return;
		}
		ct_gdh8_registered_early = true;
	}
#endif

	// ---------------------------------------------------------------------
	// §8.1 STEP 1 — verify the bytes against `snapshotDigest` and `lineCount`.
	//
	// ALREADY DONE, and done in the right place: the agent verifies both before
	// this handler is ever reached (`repro_hcr_agent.c:2316-2371`, refusing an
	// algorithm it cannot implement rather than skipping the check). Nothing is
	// repeated here — a second, independent verification in the host would be
	// two implementations of one rule, which is how they drift.
	// ---------------------------------------------------------------------

	// ---------------------------------------------------------------------
	// §8.1 STEP 2 — COMPILE THE NEW SCRIPT. Refuse on parse error.
	//
	// NOTHING HAS BEEN TOUCHED AT THIS POINT. In particular the bytes have NOT
	// been written to disk: until GDH-M8 that write was the first thing this
	// function did, so a refusal at any later step left v1 gone from disk while
	// the engine went on running it from memory. The write is now step 6.
	//
	// Before GDH-M8 there was no compile step AT ALL. `reload_scripts` returns
	// void (gdscript.h:631) and drops `GDScript::reload()`'s Error on the floor
	// (gdscript.cpp:2560), so an unparseable v2 was written to disk, handed to
	// the engine, and acknowledged `applied` with a boundary marker and a fresh
	// path version already in the container. `REPRO_HCR_RELOAD_REASON_PARSE_
	// ERROR` had existed in the agent's vocabulary the whole time with zero
	// uses anywhere under modules/gdscript/.
	// ---------------------------------------------------------------------
	{
		String why;
		if (!ct_gdscript_content_compiles(req.res_path, req.content, why)) {
			req.applied = false;
			req.reason = REPRO_HCR_RELOAD_REASON_PARSE_ERROR;
			req.detail = why;
			fprintf(stderr, "[ct-gdh8] REFUSED (parse-error) %s gen=%u: %s\n",
					req.res_path.utf8().get_data(), req.generation,
					why.utf8().get_data());
			fflush(stderr);
			return;
		}
	}

	// The script must already be loaded. A `core:reload_scripts` addressed to a
	// never-loaded path takes no effect and says nothing — GDH-M0's
	// `wrongtarget` arm measured exactly that — so an unloaded path is refused
	// BY NAME here instead of quietly doing nothing.
	//
	// GDH-M8 gave it its OWN name. It used to answer `writer-refused`, which
	// was the reason string for six unrelated conditions; a gate asserting
	// `reason == "writer-refused"` was satisfied by any of them, and "the trace
	// writer refused" and "the engine has never heard of this file" have
	// nothing in common and different fixes.
	Ref<Resource> res = ResourceCache::get_ref(req.res_path);
	Ref<Script> scr = res;
	if (scr.is_null()) {
		req.applied = false;
		req.reason = REPRO_HCR_RELOAD_REASON_SCRIPT_NOT_LOADED;
		req.detail = "no loaded script at " + req.res_path +
				"; a reload addressed to a path the engine never loaded takes no effect";
		return;
	}

	List<StringName> names;
	List<Variant> before;
	ct_collect_statics(scr, names, before);

	// ---------------------------------------------------------------------
	// §8.1 STEPS 3-5 — the TRACE half, and it runs BEFORE the engine swap.
	//
	// "A trace that cannot represent the new version must not be allowed to
	// have one." Steps 3-5 mint the version, bundle its source view against the
	// new id from the bytes step 1 verified, and record the boundary. A refusal
	// at step 3 is a clean abort with v1 still live; a failure at 4 or 5 is not
	// recoverable by continuing and closes the trace.
	//
	// "Here" is still load-bearing for the marker's POSITION: this runs inside
	// the emit lock, at an engine safe point, so no step can be emitted between
	// the marker and the new path becoming current, and the container cannot
	// state a boundary the execution did not have.
	// ---------------------------------------------------------------------
	if (!ct_gdh8_registered_early && g_ct_writer != nullptr) {
		if (!ct_reload_register_in_trace_locked(req, /*p_emit_marker=*/true)) {
			return; // refused or closed; `req` already says which
		}
	}
#if defined(CT_GDH8_FALSIFY_MINT_BEFORE_COMPILE)
	// The mint-before-compile arm minted above WITHOUT a marker; record the
	// boundary now, so that on a SUCCESSFUL reload the container this arm
	// produces is identical to the correct one and the arm's only effect is
	// the one it names. An arm that also lost the marker would redden gates it
	// is not aimed at, and would not have been shown to discriminate.
	//
	// `!= 0` is NOT the test: path ids are 0-based and the fixture is usually
	// id 0, so a zero check would skip the marker exactly on the first file
	// registered — which is what it did when this arm was first written, and
	// the driver caught it as "reddens gates it is not aimed at".
	if (ct_gdh8_registered_early && req.path_id != req.old_path_id) {
		if (!ct_reload_emit_marker_locked(req, req.old_path_id, req.path_id)) {
			return;
		}
	}
#endif

	// ---------------------------------------------------------------------
	// §8.1 STEP 6 — SWAP THE ENGINE'S SCRIPT RESOURCE.
	//
	// The bytes the notification carried become the bytes on disk, because
	// `reload_scripts` re-reads from disk (gdscript.cpp:2558). Writing them
	// here rather than trusting a path handle is §4.3's "content travels with
	// the notification": a handle races the next edit.
	// ---------------------------------------------------------------------
	if (ct_gdh8_inject_at("swap") &&
			ct_reload_fail_after_registration(req, "6 (swap the engine's script)",
					String::utf8("injected failure at §8.1 step 6"))) {
		return;
	}
	// GDH-M8b. THE TEXT THE ENGINE IS ACTUALLY RUNNING, taken before anything is
	// written, so a compile failure below has something to put back.
	//
	// It is read from the SCRIPT and not from disk on purpose. Disk is not a
	// reliable statement of what the engine is running — `CT_GDH8_FALSIFY_WRITE_
	// BEFORE_COMPILE` is a build in which it demonstrably is not — and the thing
	// that has to be restored is the source the live `GDScript` object was
	// compiled from. `GDScript::reload()` re-parses `source` (gdscript.cpp:818)
	// and `reload_scripts` refills it from disk first (gdscript.cpp:2558), so
	// writing this text back and re-running the same wrapper puts the engine
	// where it was rather than somewhere merely similar.
	String ct_gdh8_pre_swap_source;
	bool ct_gdh8_have_pre_swap_source = false;
	{
		Ref<GDScript> gd_before = scr;
		if (gd_before.is_valid()) {
			ct_gdh8_pre_swap_source = gd_before->get_source_code();
			ct_gdh8_have_pre_swap_source = true;
		}
	}
	{
		Error err = OK;
		Ref<FileAccess> fa = FileAccess::open(req.res_path, FileAccess::WRITE, &err);
		if (fa.is_null() || err != OK) {
			// The trace has already committed to v2 (steps 3-5 are done), so
			// this is a §8.1 steps 4-6 failure and not a clean refusal: the
			// container names a version the engine will never run. Close it.
			(void)ct_reload_fail_after_registration(req,
					"6 (swap the engine's script)",
					"cannot open " + req.res_path + " for writing (err " +
							itos((int)err) + ")");
			return;
		}
		if (!req.content.is_empty()) {
			fa->store_buffer(req.content.ptr(), req.content.size());
		}
		fa->flush();
	}

	// Godot's own supported path, soft. §5.2: `reload_scripts` is the wrapper
	// that re-reads disk; `GDScript::reload()` alone is not.
#if defined(CT_GDH5_FALSIFY_SCRIPT_RELOAD_ONLY)
	// FALSIFIER ARM (gdh5_in_process_reload_matches_the_remote_debugger_path):
	// call `GDScript::reload()` directly. It parses the IN-MEMORY `source`
	// member (gdscript.cpp:818) and never re-reads disk, so the program keeps
	// running v1 while every call reports success. This arm exists because
	// `reload()` is the obvious-looking call and is the wrong one — the gate
	// must go red by finding v1's tokens after the reload, measured against
	// Godot's own `core:reload_scripts` oracle.
	{
		Ref<GDScript> gd = scr;
		if (gd.is_valid()) {
			(void)gd->reload(/*p_keep_state=*/true);
		}
	}
#else
	CtGdh8CompileErrorSink ct_gdh8_sink;
	{
		Array scripts;
		scripts.push_back(scr);
		GDScriptLanguage::get_singleton()->reload_scripts(scripts, /*p_soft_reload=*/true);
	}
#endif

	// ---------------------------------------------------------------------
	// §8.1 STEP 6, SECOND HALF — GDH-M8b: DID THE COMPILER TAKE IT?
	//
	// THE DOOR THE PRE-CHECK CANNOT STAND IN FRONT OF. Step 2 runs
	// `GDScriptParser::parse` and `GDScriptAnalyzer::analyze` on a stack-local
	// parser and installs nothing, which is why it can refuse `parse-error`
	// cleanly. `GDScriptCompiler` cannot be run that way: `compile()` takes a
	// `GDScript *` and writes into it — it clears the target's members, deletes
	// its `GDScriptFunction`s, re-runs `_prepare_compilation` on its BASE script
	// objects (gdscript_compiler.cpp:2787), pulls orphan subclasses out of
	// `GDScriptLanguage`'s global map (:3159) and can register the target in
	// `GDScriptCache`'s static-script list (:3332). A "throwaway" GDScript is
	// therefore not throwaway; giving it the real path collides in
	// `ResourceCache`, and not giving it the real path still lets it mutate the
	// live base scripts of any file with a GDScript `extends`. The pre-check
	// deliberately stops before it, and this is the price: a v2 that parses and
	// analyzes and fails the compiler is only detectable once the engine has
	// already taken it.
	//
	// So it is detected HERE, after the swap, and the state that produces is
	// named rather than dressed up. `GDScript::reload()` sets `valid = false`
	// before it parses (gdscript.cpp:812) and only sets it back on a clean
	// compile, and `reload_scripts` drops the Error (gdscript.cpp:2560), so
	// `is_valid()` is the whole in-process signal.
	//
	// WHAT "RECOVERY" MEANS, PRECISELY. After a failed compile the engine is NOT
	// on v1 and it is NOT on v2 — it is on a GDScript whose members, functions
	// and signals `_prepare_compilation` cleared and never refilled. A MainLoop
	// script in that state stops being called at all, which is a HUNG PROCESS and
	// not a degraded one. There is nothing to "keep running", so the engine is
	// put back by re-running the SAME supported wrapper over the text it was
	// running before the swap. That either works — and is then asserted, by
	// asking `is_valid()` a second time — or it does not, and the wire says which
	// of the two happened. The reply never implies more than happened.
	//
	// THE TRACE IS A SEPARATE QUESTION AND GETS §8.1's ANSWER. Steps 3-5 already
	// minted v2's path version, bundled its source view and recorded the
	// boundary; the engine is going back to v1. Continuing would attribute every
	// later step to a version nothing ever executed, which is exactly the
	// misattribution this campaign exists to prevent, so the recording is CLOSED
	// with the reason written into the container. A restored engine and a closed
	// trace is a degraded session with a coherent recording — §8.1's contract,
	// verbatim.
	// ---------------------------------------------------------------------
#if defined(CT_GDH8_FALSIFY_IGNORE_COMPILE_FAILURE)
	// FALSIFIER ARM (gdh8_a_reload_that_fails_the_compiler_is_refused_by_name):
	// RESTORE THE PRE-GDH-M8b BEHAVIOUR. Never ask whether the compiler took it;
	// report `applied`, keep the marker, keep the recording, and leave the engine
	// on the half-cleared script. This is the defect verbatim, and the gate must
	// go red on the ACKNOWLEDGEMENT — a v2 the compiler refused must not come
	// back `applied` — rather than merely on the process having stopped.
	const bool ct_gdh8b_check_compiled = false;
#else
	const bool ct_gdh8b_check_compiled = true;
#endif
#if defined(CT_GDH8_FALSIFY_NO_RESTORE_AFTER_COMPILE_FAILURE)
	// FALSIFIER ARM (same gate, second arm): detect the compile failure, name it
	// on the wire and close the trace correctly — and DO NOT PUT THE ENGINE BACK.
	// The ACKNOWLEDGEMENT claims stay green — `outcome: failed`, `reason:
	// compile-error`, the stage, the compiler's own message, the closed trace —
	// so what kills this arm is the claim that the SESSION SURVIVES: the process
	// is left on a script the compiler refused and never reaches its own end.
	// Without it "the engine ends up back on v1" would be a sentence with
	// nothing behind it, which is the state deviation (a)'s disk half was
	// measured to be in.
	//
	// WHAT THIS ARM DOES AND DOES NOT SHOW — corrected by the GDH-M8b review,
	// 2026-09-12, because the earlier wording here claimed more than the arms
	// measure. It is NOT true that arms 7 and 8 are independent in the set
	// sense: arm 7 skips the check, so it skips the restore too, and its red set
	// strictly CONTAINS arm 8's. The honest statement is the one-directional
	// one — arm 8 reddens the restore/session claims while leaving arm 7's named
	// kill (`the reload's OUTCOME is 'failed'`) GREEN, so it shows the restore
	// claim can fail on its own. The converse is not shown by any arm, and no
	// arm can show it while `ct_gdh8b_check_compiled` gates both: an
	// acknowledgement-only mutation would have to keep the check and corrupt
	// only the reporting. Recorded as a residual rather than asserted away.
	//
	// Two further wire assertions also go red under this arm, correctly and by
	// design: the `detail` says "could NOT be put back" and the recorder channel
	// prints `restored=NO`. That is the host reporting honestly, not a leak —
	// but "every wire claim stays green under it" was false and is gone.
	const bool ct_gdh8b_restore_v1 = false;
#else
	const bool ct_gdh8b_restore_v1 = true;
#endif
#if defined(CT_GDH8_FALSIFY_RESTORE_SELF_REPORT)
	// FALSIFIER ARM (same gate, THIRD arm) — ADDED BY THE GDH-M8b REVIEW,
	// 2026-09-12. THE SILENT SELF-PASS, IN THE ONE PLACE THIS MILESTONE SAYS IT
	// MUST NOT BE.
	//
	// Arms 7 and 8 both make the host tell the truth: 7 never looks, 8 looks and
	// says `restored=NO`. NEITHER of them models the failure this campaign has
	// now found twenty times — code that REPORTS SUCCESS IT DID NOT OBSERVE. So
	// this arm skips the write and the reload and still answers `restored=yes`,
	// with the detail sentence "put back on the source it was running" and the
	// recorder line `restored=yes`, all of them false.
	//
	// What it proves is about the GATE, not the host: every assertion that reads
	// the host's own account of the restore stays GREEN under it, and the gate
	// must still go red — on the file ON DISK still being the refused content,
	// on `GDH8_END` never being printed, and on the process not ending by
	// itself. If the gate's verdict rested on `restored=yes` it would pass this
	// arm, and "the engine ends up back on v1" would again be a sentence with
	// only the host's word behind it. `restored` is therefore set here WITHOUT
	// being measured, which is precisely what the shipping path must never do.
	const bool ct_gdh8b_restore_self_report = true;
#else
	const bool ct_gdh8b_restore_self_report = false;
#endif
	if (ct_gdh8b_check_compiled) {
		Ref<GDScript> gd_after = scr;
		if (gd_after.is_valid() && !gd_after->is_valid()) {
			String why = "the engine's GDScript compiler refused the new content";
#if !defined(CT_GDH5_FALSIFY_SCRIPT_RELOAD_ONLY)
			if (ct_gdh8_sink.captured) {
				why += ": line " + itos(ct_gdh8_sink.line) + ": " +
						ct_gdh8_sink.message;
			} else {
				why += " (GDScript::reload left the script invalid; the compiler's "
					   "own message was not captured)";
			}
#endif
			// --- PUT THE ENGINE BACK, and MEASURE whether it went back.
			//
			// The bytes written are `String::utf8()` of what `get_source_code()`
			// returned, so the round trip is byte-exact for well-formed UTF-8
			// and is not guaranteed to be for a file that is not. The gate does
			// not take that on trust: it hashes the file on disk after the
			// refusal and requires it to equal `probe_v1.gd`, so a round trip
			// that changed a byte would be RED rather than quietly accepted.
			bool restored = false;
			if (ct_gdh8b_restore_self_report) {
				// FALSIFIER ARM ONLY (CT_GDH8_FALSIFY_RESTORE_SELF_REPORT):
				// claim the restore without performing or measuring it. On the
				// shipping path `restored` is only ever assigned from
				// `is_valid()` below.
				restored = true;
			} else if (ct_gdh8b_restore_v1 && ct_gdh8_have_pre_swap_source) {
				Error rerr = OK;
				Ref<FileAccess> fa = FileAccess::open(req.res_path,
						FileAccess::WRITE, &rerr);
				if (fa.is_valid() && rerr == OK) {
					CharString old_cs = ct_gdh8_pre_swap_source.utf8();
					if (old_cs.length() > 0) {
						fa->store_buffer((const uint8_t *)old_cs.get_data(),
								(uint64_t)old_cs.length());
					}
					fa->flush();
					fa = Ref<FileAccess>();
					Array back;
					back.push_back(scr);
					GDScriptLanguage::get_singleton()->reload_scripts(back,
							/*p_soft_reload=*/true);
					restored = gd_after->is_valid();
				}
			}
			req.unpreserved.push_back(restored
							? "source-version-rolled-back:the compiler refused the "
							  "new content and the engine was put back on the "
							  "source it was running"
							: "engine-left-uncompiled:the compiler refused the new "
							  "content and the engine could NOT be put back");
			why += restored
					? "; the engine was put back on the source it was running "
					  "before the swap and that source compiles"
					: "; the engine could NOT be put back and is left on a script "
					  "the compiler refused";
			fprintf(stderr, "[ct-gdh8b] COMPILE FAILURE at §8.1 step 6: %s "
							"gen=%u restored=%s: %s\n",
					req.res_path.utf8().get_data(), req.generation,
					restored ? "yes" : "NO", why.utf8().get_data());
			fflush(stderr);
			// THE REASON IS SET HERE AND NOT ONLY BY THE HELPER, and that is the
			// GDH-M8 review's own warning taken up rather than repeated. It
			// found the agent's `reason == NULL ? WRITER_REFUSED` fallback
			// "defensive rather than load-bearing today… a silent-collapse
			// hazard for whoever adds the seventh [refusal site]". This IS the
			// seventh. `ct_reload_fail_after_registration` sets the reason on
			// the path that closes the trace — but under
			// `CT_GDH8_FALSIFY_CONTINUE_AFTER_FAILURE` it returns without
			// setting anything, and the fallback then turned a compile failure
			// into `writer-refused` on the wire: the exact name-collapse this
			// milestone's review spent its length undoing, reintroduced by a
			// build that was only supposed to change what the TRACE does.
			//
			// What the compiler did and what the recorder did are two different
			// facts, so they are set in two places. The helper still overwrites
			// `detail` with the fuller §8.1 sentence on the shipping path.
			req.applied = false;
			req.reason = REPRO_HCR_RELOAD_REASON_COMPILE_ERROR;
			req.detail = why;
			(void)ct_reload_fail_after_registration(req,
					"6 (swap the engine's script)", why,
					REPRO_HCR_RELOAD_REASON_COMPILE_ERROR);
			// Unconditional: under `CT_GDH8_FALSIFY_CONTINUE_AFTER_FAILURE` the
			// call above answers "do not abort", and CONTINUING here would
			// report `applied` for content the engine does not run. That arm
			// models a recorder that keeps recording, not a host that lies about
			// the outcome, and the two must not be confused.
			return;
		}
	}

	// What did not survive, measured rather than asserted.
	List<StringName> names_after;
	List<Variant> after;
	ct_collect_statics(scr, names_after, after);
	{
		const List<StringName>::Element *ne = names.front();
		const List<Variant>::Element *be = before.front();
		for (; ne && be; ne = ne->next(), be = be->next()) {
			bool found = false;
			Variant now;
			const List<StringName>::Element *na = names_after.front();
			const List<Variant>::Element *va = after.front();
			for (; na && va; na = na->next(), va = va->next()) {
				if (na->get() == ne->get()) {
					found = true;
					now = va->get();
					break;
				}
			}
			if (!found) {
				req.unpreserved.push_back("static-variable-removed:" + String(ne->get()));
			} else if (now != be->get()) {
				// §5.3: `_save_old_static_data` / `_restore_old_static_data` are
				// TOOLS_ENABLED-only (gdscript.cpp:806-810, :890-899), so a
				// `template_debug` build re-defaults every static through
				// `_static_init()`. That is a real behavioural divergence from
				// the editor and it is reported, never silently absorbed.
				req.unpreserved.push_back("static-variable-lost:" + String(ne->get()) +
						":" + String(be->get()) + "->" + String(now));
			}
		}
	}

#if defined(CT_GDH5_FALSIFY_UNCONDITIONAL_LOSS)
	// FALSIFIER ARM (gdh5_unpreserved_state_is_reported): report a
	// static-variable loss whether or not the script has statics, and whether
	// or not anything changed. This is the `oldCodeRetained: true` shape — an
	// unconditional literal in a status field — and it is here to keep that out
	// of a report the whole milestone rests on. The gate must go red on the
	// no-statics CONTROL fixture, which has nothing to lose.
	req.unpreserved.clear();
	req.unpreserved.push_back("static-variable-lost:counter:reported-unconditionally");
#endif
	req.applied = true;
	req.reason = nullptr;
	g_ct_reload_applied_count++;
}

} // namespace

// The agent's handler. Runs on whichever thread the agent services the socket
// on: the polling thread when `repro_hcr_agent_poll` drives it (which IS the
// safe point), or the agent's own detached thread in the default start mode.
static int gdscript_ct_hcr_source_reload(void *ctx, const char *reload_id,
		const char *language,
		const repro_hcr_source_changed_file *file,
		repro_hcr_source_reload_outcome *out) {
	(void)ctx;
	(void)reload_id;
	(void)language;

	// These outlive the call: the agent reads them out of `out` after we
	// return, so they cannot be stack buffers.
	static CharString s_detail;
	static CharString s_digest;
	static CharString s_unpreserved[REPRO_HCR_AGENT_MAX_UNPRESERVED];

	CtReloadRequest req;
	std::shared_ptr<CtReloadRequest> shared;
	req.res_path = String::utf8(file->source_path);
	req.generation = file->generation;
	req.content.resize((int)file->content_length);
	if (file->content_length > 0) {
		memcpy(req.content.ptrw(), file->content, file->content_length);
	}

#if defined(CT_GDH6_FALSIFY_APPLY_WHERE_IT_LANDS)
	// FALSIFIER ARM (gdh5_reload_is_refused_while_a_step_is_pending): remove
	// the pending-step guard and apply the reload WHERE THE NOTIFICATION
	// LANDED, on the agent's own thread, whether or not the VM is mid-step.
	// The emit lock still serialises the writer, so nothing crashes — the
	// recorder's single pending-step slot is simply flushed in the middle of a
	// step whose values have not all arrived, and the marker lands between a
	// step and its values. The gate must go red by finding that split IN THE
	// CONTAINER, not by observing that a guard was not called.
	const bool ct_gdh6_defer_to_safe_point = false;
#else
	const bool ct_gdh6_defer_to_safe_point = true;
#endif
	if (g_ct_in_safe_point || !ct_gdh6_defer_to_safe_point) {
		// Already at the point the engine chose: apply under the emit lock.
		CtEmitLock lk;
		if (!lk.engaged) {
			// Reentrancy guard engaged means we are INSIDE the emit path. That
			// must never happen at the safe point, and is reported rather than
			// worked around.
			out->applied = 0;
			// GDH-M8: `host-busy`, not `writer-refused`. The trace writer was
			// never asked anything; the HOST could not take the request now.
			out->reason = REPRO_HCR_RELOAD_REASON_HOST_BUSY;
			out->detail = "host-busy/reentrancy: the safe point was reached from "
						  "inside the recorder's emit path";
			return -1;
		}
		ct_apply_reload_locked(req);
	} else {
		// §5.6.3 — a step may be pending. Queue and WAIT for the engine's own
		// safe point. The coordinator still gets exactly one answer; it simply
		// arrives after the reload was applied, which is what "deferred to the
		// next safe point, never applied mid-step" means on the wire.
		std::unique_lock<std::mutex> lock(g_ct_reload_mutex);
		if (g_ct_reload_queued) {
			out->applied = 0;
			out->reason = REPRO_HCR_RELOAD_REASON_HOST_BUSY;
			out->detail = "host-busy/queue-occupied: another reload is already "
						  "queued for the next safe point";
			return -1;
		}
		const int bound_s = ct_reload_wait_seconds();
#if defined(CT_GDH6_FALSIFY_RAW_POINTER_QUEUE)
		// FALSIFIER ARM: queue a pointer into THIS FRAME and let the waiter
		// clear it on timeout. See the note on `g_ct_reload_queued`.
		g_ct_reload_queued = &req;
		CtReloadRequest *watched = &req;
#else
		shared = std::make_shared<CtReloadRequest>(req);
		g_ct_reload_queued = shared;
		std::shared_ptr<CtReloadRequest> watched = shared;
#endif
		g_ct_reload_deferred_count++;
		fprintf(stderr, "[ct-gdh5] reload deferred to the next safe point: %s gen=%u\n",
				watched->res_path.utf8().get_data(), watched->generation);
		fflush(stderr);
		// Bounded: a safe point that never comes must be a named failure, not a
		// stall in the host's reply. The bound is reported in the message, so a
		// run under a test override cannot be read as having waited 30 s.
		if (!g_ct_reload_cv.wait_for(lock, std::chrono::seconds(bound_s),
					[&watched] { return watched->done; })) {
#if defined(CT_GDH6_FALSIFY_RAW_POINTER_QUEUE)
			// The waiter DELETES what the other thread is using: it clears the
			// queue and then returns, destroying `req` — which the safe point
			// is still writing into.
			g_ct_reload_queued = nullptr;
#else
			// Drop OUR reference only. The safe point may still be inside the
			// apply and still owns its own.
			g_ct_reload_queued.reset();
#endif
			out->applied = 0;
			// GDH-M5's RESIDUAL, and GDH-M8 does not hide it. A reload queued
			// in the window between this timeout and the safe point's own
			// clear can still be DROPPED (`g_ct_reload_queued.reset()` here
			// and at the safe point's exit). It has always failed BY NAME and
			// it still does; what GDH-M8 changed is only that the name is now
			// its OWN — `no-safe-point` rather than the `writer-refused` it
			// used to share with five unrelated conditions. The detail
			// sentence is unchanged, so a harness matching on it still
			// matches.
			out->reason = REPRO_HCR_RELOAD_REASON_NO_SAFE_POINT;
			static CharString s_timeout;
			s_timeout = (String("no engine safe point was reached within ") +
					itos(bound_s) + " s").utf8();
			out->detail = s_timeout.get_data();
			fprintf(stderr, "[ct-gdh6] deferral TIMED OUT: no engine safe point "
							"was reached within %d s\n", bound_s);
			fflush(stderr);
			return -1;
		}
		// Read the outcome out of the object the safe point filled in.
		req = *watched;
	}

	s_detail = req.detail.utf8();
	out->detail = s_detail.get_data();
	out->path_index = req.path_id;
	out->step_index = req.step_index;
	out->applied_line_count = 0; // the agent counts the bytes it verified

	{
		// The digest the HOST recomputed over the bytes it applied, not an echo
		// of the request. `repro_hcr_agent_sha256_hex` refuses if its own FIPS
		// self-test fails, so a digest that cannot be shown to be one is never
		// reported as one.
		char hex[65];
		if (repro_hcr_agent_sha256_hex(req.content.ptr(), (size_t)req.content.size(),
					hex, sizeof(hex)) == 0) {
			s_digest = (String("sha256:") + String(hex)).utf8();
			out->applied_digest = s_digest.get_data();
		}
	}

	int reported = 0;
	for (int i = 0; i < req.unpreserved.size() && reported < REPRO_HCR_AGENT_MAX_UNPRESERVED; i++) {
		s_unpreserved[reported] = req.unpreserved[i].utf8();
		out->unpreserved[reported] = s_unpreserved[reported].get_data();
		reported++;
	}
	out->unpreserved_count = reported;

	if (!req.applied) {
		out->applied = 0;
		// THE FALLBACK IS DELIBERATELY NOT A REAL REASON — GDH-M8b REVIEW,
		// 2026-09-12.
		//
		// This used to read `req.reason != nullptr ? req.reason :
		// REPRO_HCR_RELOAD_REASON_WRITER_REFUSED`, and GDH-M8's review called
		// that "a silent-collapse hazard for whoever adds the seventh
		// [refusal site]". GDH-M8b WAS the seventh and the hazard fired
		// exactly as predicted: under `CT_GDH8_FALSIFY_CONTINUE_AFTER_FAILURE`
		// a compile failure reached the wire as `writer-refused`. The fix
		// applied there — set the reason at the site as well as in the helper
		// — repairs the seventh site and leaves the EIGHTH exposed, because
		// the only thing standing between a forgotten assignment and a
		// plausible-looking wrong name was discipline.
		//
		// So the collapse is removed instead of being out-run. A null reason
		// is passed THROUGH, and `repro_hcr_agent.c:2222` substitutes
		// `unspecified-refusal` — a name deliberately outside §5.5's closed
		// vocabulary, so a site that forgets produces something no gate, no
		// coordinator and no reader can mistake for a real refusal. That
		// sentinel already existed; this fallback was masking it. Every real
		// refusal site sets `req.reason` explicitly (the two `writer-refused`
		// ones at :2260 and :2298 included), so nothing depended on the
		// collapse and nothing on the wire changes for any reachable path.
		out->reason = req.reason;
		// GDH-M8: a refusal gets its own stderr line, in the same shape as the
		// apply line below. A harness that could see an apply but not a refusal
		// would have to infer the refusal from the absence of the other line,
		// which is indistinguishable from a notification that never arrived —
		// the conflation this milestone's anti-vacuity clause forbids.
		fprintf(stderr, "[ct-gdh8] reload REFUSED: %s gen=%u reason=%s detail=%s\n",
				req.res_path.utf8().get_data(), req.generation,
				out->reason != nullptr ? out->reason : "(unset — the agent will "
													   "answer unspecified-refusal)",
				out->detail != nullptr ? out->detail : "");
		fflush(stderr);
		return -1;
	}
	out->applied = 1;
	out->reason = "";
	fprintf(stderr,
			"[ct-gdh5] reload applied: %s gen=%u path_id=%llu step_index=%llu unpreserved=%d deferred=%d\n",
			req.res_path.utf8().get_data(), req.generation,
			(unsigned long long)req.path_id, (unsigned long long)req.step_index,
			reported, g_ct_reload_deferred_count);
	// GDH-M6: the marker's own coordinates, on their own line so a harness can
	// match them without parsing the line above. `ordinal=0` means NO marker
	// was emitted and is printed as such rather than omitted — a missing line
	// and a zero are different findings and the `unpreserved` entries above
	// say which refusal produced it.
	fprintf(stderr,
			"[ct-gdh6] reload marker: ordinal=%llu old_path_id=%llu new_path_id=%llu "
			"in_flight_frames=%llu writer_reload_count=%llu\n",
			(unsigned long long)req.reload_ordinal,
			(unsigned long long)req.old_path_id,
			(unsigned long long)req.path_id,
			(unsigned long long)req.in_flight_frames,
			(unsigned long long)(g_ct_writer != nullptr
					? trace_writer_source_reload_count(g_ct_writer)
					: 0));
	for (int i = 0; i < reported; i++) {
		fprintf(stderr, "[ct-gdh5]   unpreserved: %s\n", out->unpreserved[i]);
	}
	fflush(stderr);
	return 0;
}

void gdscript_ct_hcr_install_source_reload_handler() {
	// Registering the handler is what makes the agent advertise
	// `source-reload`. The two are one act on purpose (design §4.4): a host
	// that advertised the capability with nothing to serve it would produce a
	// recording in which post-reload steps are attributed to v1 with nothing
	// saying so — worse than refusing the session.
	(void)repro_hcr_agent_set_source_reload_handler(gdscript_ct_hcr_source_reload, nullptr);
}

void gdscript_ct_hcr_safe_point() {
	// 1. Apply anything the agent thread queued while the VM was mid-step.
	{
#if defined(CT_GDH6_FALSIFY_RAW_POINTER_QUEUE)
		CtReloadRequest *req = nullptr;
#else
		std::shared_ptr<CtReloadRequest> req;
#endif
		{
			std::lock_guard<std::mutex> lock(g_ct_reload_mutex);
			req = g_ct_reload_queued;
		}
		// Holding `req` here is what keeps the object alive across the apply
		// even if the waiting thread gives up on it — see the note on
		// `g_ct_reload_queued`. Under the raw-pointer arm it keeps nothing
		// alive, which is the defect.
		if (req) {
			// GDH-M6 test hook: widen the window between taking the request
			// and finishing with it, so the waiter's bound can expire while
			// the apply is in flight. Off unless asked for.
			const int delay_ms = ct_safe_point_delay_ms();
			if (delay_ms > 0) {
				fprintf(stderr, "[ct-gdh6] safe point holding the apply for %d ms "
								"(CT_GDH6_SAFE_POINT_DELAY_MS)\n", delay_ms);
				fflush(stderr);
				std::this_thread::sleep_for(std::chrono::milliseconds(delay_ms));
			}
			{
				CtEmitLock lk;
				if (lk.engaged) {
					ct_apply_reload_locked(*req);
				} else {
					req->applied = false;
					req->reason = REPRO_HCR_RELOAD_REASON_HOST_BUSY;
					req->detail = "host-busy/emit-lock: the emit lock was not "
								  "available at the safe point";
				}
			}
			{
				std::lock_guard<std::mutex> lock(g_ct_reload_mutex);
				req->done = true;
#if defined(CT_GDH6_FALSIFY_RAW_POINTER_QUEUE)
				g_ct_reload_queued = nullptr;
#else
				g_ct_reload_queued.reset();
#endif
			}
			g_ct_reload_cv.notify_all();
		}
	}

#if defined(CT_GDH6_FALSIFY_MARKER_OUTSIDE_LOCK)
	// FALSIFIER ARM (see `g_ct_deferred_marker`). Emit the marker the apply
	// handed over, from THIS safe point's lock hold rather than the apply's.
	// One `Main::iteration()` has run in between, so the new version's steps
	// for that frame are already in the stream and the marker lands after
	// them. The emission itself is correct in every other respect, which is
	// the point: a gate that checked the marker's presence, its count, its
	// ordinals, its ids, its generation and its in-flight count — all of them
	// — would still be green here.
	if (g_ct_deferred_marker.pending) {
		g_ct_deferred_marker.pending = false;
		CtEmitLock lk;
		if (lk.engaged) {
			(void)trace_writer_register_source_reload(
					g_ct_writer, &g_ct_deferred_marker.change, 1,
					g_ct_deferred_marker.in_flight_frames);
		}
	}
#endif

	// 2. Drain the polled agent, if the process is running one. `g_ct_in_safe_point`
	//    is what tells the handler it may apply where it stands.
	g_ct_in_safe_point = true;
	(void)repro_hcr_agent_poll_nonblocking();
	g_ct_in_safe_point = false;
}
#endif // CT_HCR_AGENT_ENABLED

// Registered once, from initialize_gdscript_module() BEFORE the GDScriptLanguage
// constructor runs (so track_locals is forced on in time). No-op unless
// CT_GDSCRIPT_TRACE is set, so a build that includes this consumer behaves like
// a stock+hook engine when recording is off. Explicit registration (rather than
// a static initializer) keeps it deterministic and immune to linker dead-strip.
void gdscript_ct_trace_register() {
	static GdscriptCtTracer s_tracer;
	if (gdscript_ct_trace_active()) {
		GDScriptTracer::set_active(&s_tracer);
	}
}
