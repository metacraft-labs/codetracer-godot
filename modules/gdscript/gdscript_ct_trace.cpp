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
