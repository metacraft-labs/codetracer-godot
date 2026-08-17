/**************************************************************************/
/*  gdscript_ct_trace.cpp — CodeTracer GDScript recorder                  */
/*  (G2 steps, G3 calls/returns, G4 values)                               */
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
#include "core/variant/variant.h"
#include "gdscript_function.h"

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
static bool g_ct_types_ready = false;

// Cached CT_GDSCRIPT_TRACE presence (env is read once).
static int g_ct_active = -1; // -1 unknown, 0 inactive, 1 active

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

// G4: encode `value` into the reused CBOR encoder. Only the common scalar and
// String cases are done here (G4 scope: int/float/bool/String/null); every
// other Variant type falls through to a Raw (printed) value, which is the
// clean extension point milestone GF4 replaces with the full type matrix
// (Vector*/Color/Transform*/Array/Dictionary/Object/Packed* ...).
static void gdscript_ct_encode_variant(const Variant &value) {
	ct_value_encoder_reset(g_ct_encoder);
	switch (value.get_type()) {
		case Variant::NIL: {
			ct_value_write_none_typed(g_ct_encoder, g_ct_type_none);
		} break;
		case Variant::BOOL: {
			ct_value_write_bool_typed(g_ct_encoder, (bool)value ? 1 : 0, g_ct_type_bool);
		} break;
		case Variant::INT: {
			ct_value_write_int(g_ct_encoder, (int64_t)value, g_ct_type_int);
		} break;
		case Variant::FLOAT: {
			ct_value_write_float(g_ct_encoder, (double)value, g_ct_type_float);
		} break;
		case Variant::STRING:
		case Variant::STRING_NAME: {
			CharString s = String(value).utf8();
			ct_value_write_string(g_ct_encoder,
					(const uint8_t *)s.get_data(), (size_t)s.length(), g_ct_type_string);
		} break;
		default: {
			// GF4 extension point: everything else is a Raw printed form for now.
			CharString s = value.operator String().utf8();
			ct_value_write_raw(g_ct_encoder,
					(const uint8_t *)s.get_data(), (size_t)s.length(), g_ct_type_raw);
		} break;
	}
}

void gdscript_ct_trace_assign(const GDScriptFunction *p_func, int p_dest_address,
		const Variant &p_value, int p_line) {
	// A value can only attach to an already-registered step; refuse otherwise
	// so values.dat stays parallel-indexed to steps.dat.
	if (g_ct_disabled || !g_ct_writer || !g_ct_started || !p_func) {
		return;
	}

	// Decode the destination address. Only STACK writes (local variables /
	// arguments) carry a source-level name we resolve here. CONSTANT slots are
	// never written; MEMBER (instance property) writes are milestone GF8.
	int addr_type = (p_dest_address & GDScriptFunction::ADDR_TYPE_MASK) >> GDScriptFunction::ADDR_BITS;
	if (addr_type != GDScriptFunction::ADDR_TYPE_STACK) {
		return;
	}
	int slot = p_dest_address & GDScriptFunction::ADDR_MASK;

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

	String name_str = String(*name);
	// Loop iterators and other synthetic locals ARE in stack_debug but carry an
	// `@`-prefixed name; treat them as temporaries and skip.
	if (name_str.is_empty() || name_str[0] == '@') {
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
	CharString name_cs = name_str.utf8();
	trace_writer_register_variable_cbor(g_ct_writer,
			name_cs.get_data(), cbor, cbor_len);
}
