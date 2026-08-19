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
#include "gdscript_ct_trace.h"

#include "core/string/ustring.h"
#include "core/variant/array.h"
#include "core/variant/dictionary.h"
#include "core/variant/variant.h"
// §5.2 source bundling: read the recorded `.gd` source text so the `res://`
// virtual path resolves at replay, and mirror the writer's path interning.
#include "core/io/file_access.h"
#include "core/templates/hash_map.h"
#include "gdscript_function.h"
// GF8: member-write name resolution. GDScriptFunction::get_script() yields the
// owning GDScript, whose debug_get_member_by_index / debug_get_static_var_by_index
// invert member_indices / static_variables_indices (StringName<->index).
#include "gdscript.h"

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

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <mutex> // GF12: serialize the shared writer/encoder across worker threads

// N1/N2: dlsym(RTLD_DEFAULT, "ct_mcr_now" / "ct_mcr_mark_span_*") — weakly
// resolve the MCR interposer's exported context-reader and native-marker entry
// points at runtime, so the standalone engine neither links nor depends on them
// (absent -> join-key emission + native-anchor emission are inert).
#include <dlfcn.h>
#include <unistd.h> // getpid() — process-stable OTel trace id for the span markers

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

// ---------------------------------------------------------------------------
// N1: nested-trace correlation join keys (GEID, tick).
//
// When the patched engine runs INSIDE a CodeTracer MCR recording, every GDScript
// call-entry/exit and native-call boundary is tagged with the parent native
// trace's (GEID, tick) so the nested GDScript trace can be correlated to the
// parent. Wire contract: codetracer-trace-format-spec/nested-trace-correlation.md.
// The keys ride the SAME events.dat special-event channel GF10/GF13 use (no new
// CTFS stream, no C-ABI change).
//
// CONTEXT SOURCE (tried in order; INERT — no join events at all — when neither is
// present, so a standalone recording is byte-identical):
//   1. ct_mcr_now(CtMcrCoordinates*) — the MCR interposer's exported live-GEID
//      reader (codetracer-native-recorder ct_interpose trace_context.nim). Present
//      only when the process is under `ct-mcr record` (the interposer dylib is
//      loaded); resolved WEAKLY via dlsym(RTLD_DEFAULT). Trusted only when
//      recordingAvailable && hasGeid.
//   2. CT_MCR_GEID / CT_MCR_TICK env vars — a controllable shim standing in for
//      the live interface when there is no real MCR context (drives the N1
//      synthetic-native-context test). Provides a BASE (geid, tick); the recorder
//      adds a monotonic per-join offset so each join event gets a distinct, ordered
//      key — mimicking the live counter and satisfying the correlation record's
//      GEID-monotonic ordering rule (nested-trace-correlation.md §3.3).
//
// ct_mcr_now exposes the live GEID but not a dedicated per-thread tick field today
// (nested-trace-correlation.md §6); N1 uses its monotonic-time sample as the tick
// on the real path. The authoritative tick + a native event anchoring
// native->nested is an N2 dependency.
struct CtMcrCoordinates {
	int64_t wallTimeUnixNs;
	int64_t monotonicTimeNs;
	uint64_t geid;
	uint32_t hasGeid;
	uint32_t recordingAvailable;
};
typedef void (*CtMcrNowFn)(CtMcrCoordinates *);

// N2: the MCR interposer's native-marker entry points (trace_context.nim,
// exported as ct_mcr_mark_span_start / ct_mcr_mark_span_end). Calling these
// ALLOCATES an authoritative native (GEID, tick) AND EMITS a real native event
// (a span marker) into the parent MCR trace's ring — the native ANCHOR the
// correlation record §5 / §6 says native->nested needs (a native event that
// points AT the nested trace), which N1's read-only ct_mcr_now cursor could not
// provide. Signature per trace_context.nim (OpenTelemetry-style span markers):
//   int ct_mcr_mark_span_start(const char *traceIdHex/*32*/, const char *spanIdHex/*16*/,
//                              const char *parentSpanIdHex/*16 or null*/,
//                              const char *serviceName, CtMcrCoordinates *out);
//   int ct_mcr_mark_span_end  (const char *traceIdHex, const char *spanIdHex,
//                              const char *parentSpanIdHex, const char *serviceName,
//                              uint64_t startGeid, CtMcrCoordinates *out);
// The returned `out.geid` is the authoritative allocated GEID of the emitted
// native event (with hasGeid/recordingAvailable set). NOTE: CtMcrCoordinates does
// NOT surface the allocated per-thread TICK (it carries only wall/monotonic
// clocks + geid), so tick fidelity (nested-trace-correlation.md §6 gap (a))
// remains the monotonic-clock sample even on this authoritative path; the GEID is
// fully authoritative and now backed by a real native anchor (gap (b) closed).
typedef int (*CtMcrMarkSpanStartFn)(const char *, const char *, const char *, const char *, CtMcrCoordinates *);
typedef int (*CtMcrMarkSpanEndFn)(const char *, const char *, const char *, const char *, uint64_t, CtMcrCoordinates *);

static int g_ct_join_resolved = 0; // 0 unknown, -1 no source, 1 have a source
static CtMcrNowFn g_ct_mcr_now = nullptr;
static CtMcrMarkSpanStartFn g_ct_mark_span_start = nullptr; // N2 native anchor (weak)
static CtMcrMarkSpanEndFn g_ct_mark_span_end = nullptr;     // N2 native anchor (weak)
static bool g_ct_have_shim = false;
static uint64_t g_ct_shim_geid = 0;
static uint64_t g_ct_shim_tick = 0;
static uint64_t g_ct_join_counter = 0; // monotonic per emitted join (shim offset + ordering aid)

// content prefix that identifies a nested-trace join event (parent-kind gdscript).
static const char *CT_JOIN_TAG = "ct-nested-join:gdscript";

static void gdscript_ct_resolve_join_source() {
	if (g_ct_join_resolved != 0) {
		return;
	}
	g_ct_join_resolved = -1;
	// 1. The live MCR interface (weak; absent when standalone).
	void *sym = dlsym(RTLD_DEFAULT, "ct_mcr_now");
	if (sym) {
		g_ct_mcr_now = (CtMcrNowFn)sym;
		g_ct_join_resolved = 1;
	}
	// N2: the native-marker entry points, weakly (both present or both absent —
	// they live in the same interposer dylib, so the enter/exit span stack stays
	// balanced). Absent when standalone (macOS today) -> native-anchor emission is
	// inert and the join keys fall back to the N1 sample path.
	g_ct_mark_span_start = (CtMcrMarkSpanStartFn)dlsym(RTLD_DEFAULT, "ct_mcr_mark_span_start");
	g_ct_mark_span_end = (CtMcrMarkSpanEndFn)dlsym(RTLD_DEFAULT, "ct_mcr_mark_span_end");
	// 2. The env shim (also honored as a controlled override; the live interface
	//    takes precedence when it yields a usable coordinate).
	const char *g = getenv("CT_MCR_GEID");
	const char *t = getenv("CT_MCR_TICK");
	if (g && g[0] != '\0') {
		g_ct_shim_geid = strtoull(g, nullptr, 0);
		g_ct_shim_tick = (t && t[0] != '\0') ? strtoull(t, nullptr, 0) : 0;
		g_ct_have_shim = true;
		g_ct_join_resolved = 1;
	}
}

// Sample the parent native (geid, tick). Returns false when no parent context is
// available (standalone) — the caller then emits nothing.
static bool gdscript_ct_sample_join_key(uint64_t *out_geid, uint64_t *out_tick) {
	gdscript_ct_resolve_join_source();
	if (g_ct_join_resolved != 1) {
		return false;
	}
	// Prefer the live MCR interface when it yields a usable coordinate.
	if (g_ct_mcr_now) {
		CtMcrCoordinates c = {};
		g_ct_mcr_now(&c);
		if (c.recordingAvailable && c.hasGeid) {
			*out_geid = c.geid;
			*out_tick = (uint64_t)c.monotonicTimeNs;
			return true;
		}
	}
	// Fall back to the controllable shim: base + monotonic per-join offset.
	if (g_ct_have_shim) {
		*out_geid = g_ct_shim_geid + g_ct_join_counter;
		*out_tick = g_ct_shim_tick + g_ct_join_counter;
		return true;
	}
	return false;
}

// Emit one nested-trace join event bound to the current step. ASSUMES the emit
// lock is held. `site` is 0 call-enter / 1 call-exit / 2 native-call. INERT (no
// event) when tracing is inactive, no step exists yet, or no parent context is
// available (standalone -> byte-identical).
//
// `have_override` lets the caller supply an AUTHORITATIVE (geid, tick) sampled
// from a native anchor it just emitted (N2 ct_mcr_mark_span_*), instead of the
// read-only ct_mcr_now cursor / shim. When set, the join key is exactly the
// native anchor's coordinate, so native->nested resolves to a REAL native event
// (nested-trace-correlation.md §3.2), not just the sampled cursor.
static void gdscript_ct_emit_join_locked(int site, bool have_override = false,
		uint64_t ov_geid = 0, uint64_t ov_tick = 0) {
	if (g_ct_disabled || !g_ct_writer || !g_ct_started) {
		return;
	}
	uint64_t geid = 0, tick = 0;
	if (have_override) {
		geid = ov_geid;
		tick = ov_tick;
	} else if (!gdscript_ct_sample_join_key(&geid, &tick)) {
		return;
	}
	// step this join binds to: next_step_index() accounts for the pending step, so
	// the just-registered line is next_step_index() - 1 (same rule as the async
	// markers — see gdscript_ct_emit_async_marker).
	uint64_t next = trace_writer_next_step_index(g_ct_writer);
	uint64_t step = (next > 0) ? (next - 1) : 0;
	uint64_t thread = gdscript_ct_current_thread();
	const char *site_str = (site == 0) ? "call-enter" : (site == 1) ? "call-exit"
																	 : "native-call";
	// content: self-describing (ct-print surfaces this as the io event `text`; it
	// does NOT surface metadata). metadata: the same fields (structured consumers).
	char content[192];
	snprintf(content, sizeof(content),
			"%s geid=%llu tick=%llu step=%llu site=%s thread=%llu",
			CT_JOIN_TAG, (unsigned long long)geid, (unsigned long long)tick,
			(unsigned long long)step, site_str, (unsigned long long)thread);
	char meta[160];
	snprintf(meta, sizeof(meta),
			"geid=%llu tick=%llu step=%llu site=%s thread=%llu",
			(unsigned long long)geid, (unsigned long long)tick,
			(unsigned long long)step, site_str, (unsigned long long)thread);
	trace_writer_register_special_event(g_ct_writer, FFI_EVENT_TRACE_LOG_EVENT, meta, content);
	g_ct_join_counter++;
}

// ---------------------------------------------------------------------------
// N2: native-anchor span markers at the GDScriptFunction::call boundary.
//
// Closes N1 gap (b): where N1 only SAMPLED the parent's read-only cursor
// (ct_mcr_now), N2 emits a REAL native event (an OTel-style span marker) into the
// parent MCR trace at each GDScript frame's entry/exit via ct_mcr_mark_span_*.
// The marker allocates an authoritative native GEID and IS a native event that a
// native->nested lookup can land on — the co-located native anchor for the
// GDScript call. Its returned GEID becomes the call-enter / call-exit join key
// (passed as the emit override) so the two traces share the same authoritative
// coordinate at that boundary.
//
// INERT STANDALONE: on a build with no MCR interposer loaded (macOS today, or any
// standalone run) dlsym yields null for both symbols, the span stack is never
// touched, and nothing is emitted on either side — the nested .ct stays
// byte-identical and the join keys fall back to the N1 sample path. The real
// native-marker effect is exercised under a live `ct-mcr record` on the Linux
// substrate (N2 e2e — see GDScript-Recorder.milestones.org N2 runbook).
struct CtNativeSpanFrame {
	uint64_t start_geid; // authoritative GEID the matching mark_span_end links to
	char span_id[17];    // 16 hex chars + NUL — this frame's OTel span id
	bool used;           // true iff mark_span_start actually emitted a native event
};
// Per-thread span nesting stack (LIFO, mirrors GDScriptFunction::call nesting on
// that thread). thread_local keeps pairing correct even though the emit lock
// serialises threads.
static thread_local CtNativeSpanFrame g_ct_span_stack[256];
static thread_local int g_ct_span_depth = 0;
static thread_local uint64_t g_ct_span_counter = 0;

// The process-stable 32-hex-char OTel trace id (all this engine's GDScript span
// markers share one trace id; computed once from the pid).
static const char *gdscript_ct_trace_id_hex() {
	static char trace_id[33];
	static bool ready = false;
	if (!ready) {
		// Two u64 halves => exactly 32 hex chars. A fixed high tag + the pid keeps
		// it well-formed and stable for this process.
		snprintf(trace_id, sizeof(trace_id), "%016llx%016llx",
				(unsigned long long)0xC0DE772ACE900001ULL,
				(unsigned long long)getpid());
		ready = true;
	}
	return trace_id;
}

// Emit the native span-start anchor for the frame just entered. Returns true and
// fills *out_geid/*out_tick with the authoritative native coordinate when the
// marker was emitted; false (no override) when the native-marker interface is
// absent (standalone) or the marker did not emit. ASSUMES the emit lock is held.
static bool gdscript_ct_native_span_enter(uint64_t *out_geid, uint64_t *out_tick) {
	gdscript_ct_resolve_join_source();
	if (!g_ct_mark_span_start || !g_ct_mark_span_end) {
		return false; // no native-anchor path — leave the span stack untouched (inert).
	}
	if (g_ct_span_depth >= (int)(sizeof(g_ct_span_stack) / sizeof(g_ct_span_stack[0]))) {
		return false; // pathological recursion depth — skip the anchor, stay balanced-safe.
	}
	CtNativeSpanFrame &frame = g_ct_span_stack[g_ct_span_depth];
	frame.used = false;
	frame.start_geid = 0;
	// This frame's span id: (thread << 40) ^ counter, formatted as 16 hex.
	uint64_t span_val = (gdscript_ct_current_thread() << 40) ^ (g_ct_span_counter + 1);
	g_ct_span_counter++;
	snprintf(frame.span_id, sizeof(frame.span_id), "%016llx", (unsigned long long)span_val);
	// Parent span = the enclosing frame's span id (null at the outermost frame).
	const char *parent = (g_ct_span_depth > 0) ? g_ct_span_stack[g_ct_span_depth - 1].span_id : nullptr;
	g_ct_span_depth++; // push BEFORE emitting so a re-entrant emit sees the right parent.

	CtMcrCoordinates coords = {};
	int rc = g_ct_mark_span_start(gdscript_ct_trace_id_hex(), frame.span_id, parent, "gdscript", &coords);
	if (rc == 1 && coords.hasGeid && coords.recordingAvailable) {
		frame.used = true;
		frame.start_geid = coords.geid;
		*out_geid = coords.geid;
		*out_tick = (uint64_t)coords.monotonicTimeNs; // tick fidelity: §6 gap (a)
		return true;
	}
	return false;
}

// Emit the native span-end anchor for the frame being exited, matching the most
// recent gdscript_ct_native_span_enter on this thread. Returns true + the
// authoritative end coordinate when emitted. ASSUMES the emit lock is held.
static bool gdscript_ct_native_span_exit(uint64_t *out_geid, uint64_t *out_tick) {
	gdscript_ct_resolve_join_source();
	if (!g_ct_mark_span_start || !g_ct_mark_span_end || g_ct_span_depth <= 0) {
		return false; // inert / no matching push.
	}
	g_ct_span_depth--; // pop
	CtNativeSpanFrame &frame = g_ct_span_stack[g_ct_span_depth];
	if (!frame.used) {
		return false; // the matching start did not emit a native anchor.
	}
	const char *parent = (g_ct_span_depth > 0) ? g_ct_span_stack[g_ct_span_depth - 1].span_id : nullptr;
	CtMcrCoordinates coords = {};
	int rc = g_ct_mark_span_end(gdscript_ct_trace_id_hex(), frame.span_id, parent, "gdscript",
			frame.start_geid, &coords);
	if (rc == 1 && coords.hasGeid && coords.recordingAvailable) {
		*out_geid = coords.geid;
		*out_tick = (uint64_t)coords.monotonicTimeNs;
		return true;
	}
	return false;
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

	// The .ct lands in the directory of the events path:
	//   <out_dir>/gdscript_trace.ct
	String events_path = String::utf8(out_dir).path_join("events.bin");
	CharString events_cs = events_path.utf8();
	trace_writer_set_workdir(g_ct_writer, out_dir);
	trace_writer_begin_metadata(g_ct_writer, "");
	trace_writer_begin_events(g_ct_writer, events_cs.get_data());
	trace_writer_begin_paths(g_ct_writer, "");
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
// register_source_view keys on `path_id`, and the writer interns paths in
// first-seen order starting at 0 EXCLUSIVELY through trace_writer_start /
// trace_writer_register_step below (ensure_function_id interns the function
// NAME, not the path, in the multi-stream backend). We therefore mirror that
// interning here with a first-seen map + counter so our `path_id` matches the
// index the reader recovers from paths.dat. Bundling is done once per file, on
// first sight, and is best-effort: a source that cannot be read is simply not
// bundled (the origin gap remains for that file — honest degradation) rather
// than emitting empty text.
static HashMap<String, uint64_t> g_ct_bundled_path_ids;
static uint64_t g_ct_next_path_id = 0;

static void gdscript_ct_bundle_source_locked(const String &p_res_path, uint64_t p_path_id) {
	Error err = OK;
	Vector<uint8_t> bytes = FileAccess::get_file_as_bytes(p_res_path, &err);
	if (err != OK || bytes.is_empty()) {
		return; // unreadable / empty — skip; do not bundle empty source
	}
	CharString name_cs = p_res_path.utf8();
	// view_kind 0 = raw original source; NULL sourcemap = identity bundle.
	trace_writer_register_source_view(
			g_ct_writer,
			p_path_id,
			/*view_kind=*/0,
			name_cs.get_data(), (size_t)name_cs.length(),
			bytes.ptr(), (size_t)bytes.size(),
			nullptr, 0);
}

// Record the writer-mirrored path id for `p_res_path` on first sight and
// bundle its source text once. Must be called from inside the emit lock,
// immediately after the trace_writer_start / trace_writer_register_step call
// that interns the same path, so ids stay in lockstep with paths.dat.
static void gdscript_ct_note_and_bundle_path_locked(const String &p_res_path) {
	if (g_ct_bundled_path_ids.has(p_res_path)) {
		return;
	}
	uint64_t path_id = g_ct_next_path_id++;
	g_ct_bundled_path_ids.insert(p_res_path, path_id);
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
	if (unlikely(!g_ct_started)) {
		g_ct_started = true;
		// Registers the first (pending) step at this real source line.
		trace_writer_start(g_ct_writer, src_cs.get_data(), p_line);
		gdscript_ct_note_and_bundle_path_locked(source_str); // first path -> id 0
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
	// N2: emit a NATIVE span-start anchor for this GDScript frame (a real native
	// event in the parent MCR trace) and use its authoritative (GEID, tick) as the
	// call-enter join key. Falls back to the N1 sample path when the native-marker
	// interface is absent (standalone -> inert, nothing emitted on either side).
	uint64_t a_geid = 0, a_tick = 0;
	bool anchored = gdscript_ct_native_span_enter(&a_geid, &a_tick);
	// N1/N2: tag this GDScript frame's entry with the parent native (GEID, tick).
	// Inert (nothing emitted) standalone or before the first step exists.
	gdscript_ct_emit_join_locked(0 /* call-enter */, anchored, a_geid, a_tick);
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

	// N2: emit the matching NATIVE span-end anchor for this frame (the native event
	// the parent MCR trace closes the frame's span with) and use its authoritative
	// (GEID, tick) as the call-exit join key. Balanced with the span-start pushed by
	// the matching gdscript_ct_trace_call (per-thread LIFO stack). Inert / sample
	// fallback when the native-marker interface is absent.
	uint64_t x_geid = 0, x_tick = 0;
	bool x_anchored = gdscript_ct_native_span_exit(&x_geid, &x_tick);
	// N1/N2: tag this GDScript frame's exit with the parent native (GEID, tick),
	// on BOTH exit paths (normal + await-resume). Emitted before the return record
	// (order is irrelevant — the join binds to the current step, not the return).
	// Inert (nothing emitted) standalone or before the first step exists.
	gdscript_ct_emit_join_locked(1 /* call-exit */, x_anchored, x_geid, x_tick);

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

// N1: native-call join. Called from the native-call opcodes (OPCODE_CALL and
// OPCODE_CALL_METHOD_BIND*) right after the native method executed — the crossing
// where the parent native MCR trace is the continuation of this GDScript step.
void gdscript_ct_trace_native_call() {
	CtEmitLock lk; // serialize + reentrancy-guard (as every emit hook does)
	if (!lk.engaged) {
		return;
	}
	// Never creates the writer; a native call only matters once recording is live.
	if (g_ct_disabled || !g_ct_writer) {
		return;
	}
	gdscript_ct_emit_join_locked(2 /* native-call */);
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

void gdscript_ct_trace_assign(const GDScriptFunction *p_func, int p_dest_address,
		const Variant &p_value, int p_line) {
	CtEmitLock lk; // GF12: serialize + reentrancy-guard
	if (!lk.engaged) {
		return;
	}
	// A value can only attach to an already-registered step; refuse otherwise
	// so values.dat stays parallel-indexed to steps.dat.
	if (g_ct_disabled || !g_ct_writer || !g_ct_started || !p_func) {
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

	// Decode the destination address. STACK writes (local variables / arguments)
	// resolve their name from stack_debug; MEMBER writes (instance fields —
	// GF8) resolve it from the owning script's member_indices. CONSTANT slots
	// are never written.
	int addr_type = (p_dest_address & GDScriptFunction::ADDR_TYPE_MASK) >> GDScriptFunction::ADDR_BITS;
	int slot = p_dest_address & GDScriptFunction::ADDR_MASK;

	if (addr_type == GDScriptFunction::ADDR_TYPE_MEMBER) {
		// GF8: a write into an instance member slot — the common `member = expr`
		// / `self.member = expr` case, plus member initializers, @export
		// defaults and @onready assignments (all of which the compiler emits as
		// an OPCODE_ASSIGN* into an ADDR_TYPE_MEMBER destination — see
		// gdscript_compiler.cpp). The 24-bit slot is the member INDEX into the
		// instance's `members` array; invert it to the declared name via the
		// owning script's member_indices (debug_get_member_by_index).
		const GDScript *scr = p_func->get_script();
		if (!scr) {
			return;
		}
		StringName mname = scr->debug_get_member_by_index(slot);
		String mname_str = String(mname);
		if (mname_str.is_empty() || mname_str == "<error>") {
			// Not a resolvable member (should not happen for a real field);
			// skip rather than emit a bogus name.
			return;
		}
		gdscript_ct_emit_named_value(mname_str, p_value);
		return;
	}

	if (addr_type != GDScriptFunction::ADDR_TYPE_STACK) {
		return;
	}

	// Resolve slot -> declared name using the same table Godot's own debugger
	// uses for `debug_get_stack_level_locals` (GDScriptFunction::stack_debug,
	// populated only when local tracking is on — we force it via
	// gdscript_ct_trace_active(); see GDScriptLanguage's constructor). We pass
	// `line + 1` so a variable declared ON the current line (its stack_debug
	// entry has sd.line == line) is IN scope: debug_get_stack_member_state
	// keeps entries with sd.line < p_line, so the +1 includes the just-declared
	// local while still excluding anything declared on a later line.
	List<Pair<StringName, int>> locals;
	p_func->debug_get_stack_member_state(p_line + 1, &locals);
	const StringName *name = nullptr;
	for (const Pair<StringName, int> &e : locals) {
		if (e.second == slot) {
			name = &e.first;
			break;
		}
	}
	if (!name) {
		// The slot is not a named local at this line: it is a compiler
		// temporary (expression intermediate results never enter stack_debug),
		// so we skip it — mirroring codetracer-nim's resolveTracedSlotSym.
		return;
	}

	// Loop iterators and other synthetic locals ARE in stack_debug but carry an
	// `@`-prefixed name; gdscript_ct_emit_named_value treats them as temporaries.
	gdscript_ct_emit_named_value(String(*name), p_value);
}

// GF8: member writes whose opcode carries the member NAME directly (not a
// stack-slot address). See the header for the covered opcodes. Same
// parallel-index contract as gdscript_ct_trace_assign: a member value can only
// attach to an already-registered step.
void gdscript_ct_trace_member_assign(const StringName &p_name, const Variant &p_value) {
	CtEmitLock lk; // GF12: serialize + reentrancy-guard
	if (!lk.engaged) {
		return;
	}
	if (g_ct_disabled || !g_ct_writer || !g_ct_started) {
		return;
	}
	// GF12: parallel-index safety under threads (see gdscript_ct_trace_assign).
	if (g_ct_pending_owner != gdscript_ct_current_thread()) {
		return;
	}
	gdscript_ct_emit_named_value(String(p_name), p_value);
}
