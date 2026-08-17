/**************************************************************************/
/*  gdscript_ct_trace.cpp — CodeTracer GDScript recorder (G2 steps, G3 calls)*/
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
//   finish_* + close                -> serialize .ct (via atexit)
//
// G3 note on ordering: the FIRST hook to fire on the outermost function is its
// call entry (enter_function precedes the first OPCODE_LINE), so the writer is
// created there. trace_writer_start is deferred to the first *step* so the step
// stream is byte-identical to G2 (no synthetic declaration-line step is
// emitted). register_call before the first step is fine — the multi-stream
// writer captures the call's entry_step as the next step index.
#include "gdscript_ct_trace.h"

#include "core/string/ustring.h"

#include <cstdlib>

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

static void gdscript_ct_close() {
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
	const char *out_dir = getenv("CT_GDSCRIPT_TRACE");
	if (!out_dir || out_dir[0] == '\0') {
		g_ct_disabled = true;
		return false;
	}

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

void gdscript_ct_trace_step(const StringName &p_source, int64_t p_line) {
	if (!gdscript_ct_ensure_writer()) {
		return;
	}

	CharString src_cs = String(p_source).utf8();
	if (unlikely(!g_ct_started)) {
		g_ct_started = true;
		// Registers the first (pending) step at this real source line.
		trace_writer_start(g_ct_writer, src_cs.get_data(), p_line);
		return;
	}

	trace_writer_register_step(g_ct_writer, src_cs.get_data(), p_line);
}

void gdscript_ct_trace_call(const StringName &p_name, const StringName &p_source, int64_t p_line) {
	if (!gdscript_ct_ensure_writer()) {
		return;
	}

	CharString name_cs = String(p_name).utf8();
	CharString src_cs = String(p_source).utf8();
	size_t fid = trace_writer_ensure_function_id(g_ct_writer,
			name_cs.get_data(), src_cs.get_data(), p_line);
	trace_writer_register_call(g_ct_writer, fid);
}

void gdscript_ct_trace_return() {
	// Never create the writer from a return: a return only makes sense after a
	// matching call (which already created it). Guard on the live handle.
	if (g_ct_disabled || !g_ct_writer) {
		return;
	}
	trace_writer_register_return(g_ct_writer);
}
