/**************************************************************************/
/*  gdscript_tracer.h                                                     */
/**************************************************************************/
/*                         This file is part of:                          */
/*                             GODOT ENGINE                               */
/*                        https://godotengine.org                         */
/**************************************************************************/

#ifndef GDSCRIPT_TRACER_H
#define GDSCRIPT_TRACER_H

#include "core/string/string_name.h"
#include "core/variant/variant.h"

class GDScriptFunction;

// -----------------------------------------------------------------------------
// GDScriptTracer — a minimal, general execution-tracing hook for the GDScript
// VM.
//
// This is the engine-side, RECORDER-AGNOSTIC seam. It is the GDScript analogue
// of CPython's `sys.settrace` / Lua's `lua_sethook`: an external consumer
// registers ONE tracer object and receives callbacks at the points the GDScript
// VM already stops for the debugger — per executed source line (the OPCODE_LINE
// / `EngineDebugger::line_poll()` point), function enter/exit
// (GDScriptLanguage::enter_function / exit_function), the value-write opcodes,
// the `await` suspend/resume boundary, and utility-function calls.
//
// Everything a callback receives is an engine-neutral `StringName`/`Variant`:
// the tracer never sees a GDScriptFunction, an opcode, a stack address, or any
// other VM internal. Slot->name resolution (the same mapping the debugger's
// `debug_get_stack_level_locals` performs) is done HERE, so a consumer can be a
// pure downstream module with no dependency on gdscript_vm.cpp internals.
//
// A stock build with no tracer registered pays only a single (predicted) null
// pointer test at each seam — see the inline shims at the bottom of this file.
// -----------------------------------------------------------------------------
class GDScriptTracer {
public:
	virtual ~GDScriptTracer() {}

	// Fired for every executed source line, from the OPCODE_LINE handler (the
	// same point that services breakpoints and EngineDebugger::line_poll()).
	// `source` is the running function's source path; `line` is the 1-based line.
	virtual void on_line(const StringName &source, int line) {}

	// Fired once per GDScriptFunction::call frame, right after enter_function().
	// `function` is the function name, `source` its source path, `line` its
	// definition line.
	virtual void on_call(const StringName &function, const StringName &source, int line) {}

	// Fired once per GDScriptFunction::call frame, right before exit_function(),
	// on every exit path (normal return and await-resume completion). `return_value`
	// is the value the frame yields (NIL for a void / fall-off-the-end function).
	virtual void on_return(const Variant &return_value) {}

	// Fired when a value is written to a NAMED local, argument, or member whose
	// declared name the opcode ALREADY carries (SET_MEMBER / SET_NAMED /
	// SET_NAMED_VALIDATED / SET_STATIC_VARIABLE) — no slot resolution is needed,
	// so the name is passed pre-resolved.
	virtual void on_variable_write(const StringName &name, const Variant &value) {}

	// Fired when a value is written to a stack/member slot addressed by OPERAND
	// (OPCODE_ASSIGN* / OPCODE_OPERATOR* into a local, or an ADDR_TYPE_MEMBER
	// slot). The name is deliberately NOT pre-resolved: `func`/`dest_address`/`line`
	// are handed through opaquely so the consumer can resolve the declared name via
	// gdscript_trace_resolve_slot_name() AT THE MOMENT IT EMITS (e.g. while holding
	// its own emit lock), instead of eagerly here. This matters for a consumer that
	// serializes emit through a single shared pending-step slot across threads:
	// resolving eagerly widens the window between a thread's step and its value,
	// letting a concurrent thread steal the slot and forcing the value to be
	// dropped. `func` is opaque to the consumer — it only passes it back to the
	// resolver, never dereferencing it.
	virtual void on_slot_write(const GDScriptFunction *func, int dest_address, const Variant &value, int line) {}

	// Fired at an `await` suspension and its matching resumption. `context_id`
	// is a stable opaque token (the coroutine CallState) that is identical for a
	// suspend and the resume that continues it, so a consumer can pair them.
	virtual void on_await_suspend(const void *context_id) {}
	virtual void on_await_resume(const void *context_id) {}

	// Fired after a core utility function runs (OPCODE_CALL_UTILITY). `function`
	// is the utility name (e.g. print, push_error, str); `args`/`argc` are the
	// call arguments. General observation point for builtin calls.
	virtual void on_utility_call(const StringName &function, const Variant **args, int argc) {}

	// True if this tracer needs per-function local-variable tables populated
	// (so on_variable_write can resolve stack slots to names). When any active
	// tracer returns true, GDScript forces `track_locals` on at startup — the
	// same table the remote debugger relies on.
	virtual bool wants_local_tracking() const { return true; }

	// Registration: a single active tracer (last registration wins). The engine
	// does NOT take ownership; the consumer owns the object's lifetime.
	static void set_active(GDScriptTracer *p_tracer);
	static GDScriptTracer *get_active();
};

// Fast-path pointer to the active tracer (nullptr when none). The inline shims
// below branch on it so a stock build has effectively zero overhead.
extern GDScriptTracer *gdscript_tracer_active;

// --- VM dispatch shims (called from gdscript_vm.cpp / gdscript.cpp) ----------
// Each is a no-op when no tracer is registered.

static _FORCE_INLINE_ void gdscript_trace_line(const StringName &p_source, int p_line) {
	if (gdscript_tracer_active) {
		gdscript_tracer_active->on_line(p_source, p_line);
	}
}

static _FORCE_INLINE_ void gdscript_trace_call(const StringName &p_function, const StringName &p_source, int p_line) {
	if (gdscript_tracer_active) {
		gdscript_tracer_active->on_call(p_function, p_source, p_line);
	}
}

static _FORCE_INLINE_ void gdscript_trace_return(const Variant &p_return_value) {
	if (gdscript_tracer_active) {
		gdscript_tracer_active->on_return(p_return_value);
	}
}

static _FORCE_INLINE_ void gdscript_trace_await_suspend(const void *p_context_id) {
	if (gdscript_tracer_active) {
		gdscript_tracer_active->on_await_suspend(p_context_id);
	}
}

static _FORCE_INLINE_ void gdscript_trace_await_resume(const void *p_context_id) {
	if (gdscript_tracer_active) {
		gdscript_tracer_active->on_await_resume(p_context_id);
	}
}

static _FORCE_INLINE_ void gdscript_trace_utility_call(const StringName &p_function, const Variant **p_args, int p_argc) {
	if (gdscript_tracer_active) {
		gdscript_tracer_active->on_utility_call(p_function, p_args, p_argc);
	}
}

// Named write whose opcode already carries the member NAME directly
// (SET_MEMBER / SET_NAMED / SET_NAMED_VALIDATED / SET_STATIC_VARIABLE).
static _FORCE_INLINE_ void gdscript_trace_named_write(const StringName &p_name, const Variant &p_value) {
	if (gdscript_tracer_active) {
		gdscript_tracer_active->on_variable_write(p_name, p_value);
	}
}

// Resolve a stack/member write ADDRESS to its source-level declared name, using
// the same tables the debugger uses (GDScriptFunction::debug_get_stack_member_state
// for locals/args, GDScript::debug_get_member_by_index for instance members).
// Returns an empty StringName for a compiler temporary / unresolvable slot, which
// the consumer drops. A consumer calls this from on_slot_write AT EMIT TIME (e.g.
// under its own lock); it is defined in gdscript_tracer.cpp because it touches
// GDScriptFunction internals, so the consumer stays free of VM headers.
StringName gdscript_trace_resolve_slot_name(const GDScriptFunction *p_func, int p_dest_address, int p_line);

static _FORCE_INLINE_ void gdscript_trace_slot_write(const GDScriptFunction *p_func, int p_dest_address,
		const Variant &p_value, int p_line) {
	if (gdscript_tracer_active) {
		gdscript_tracer_active->on_slot_write(p_func, p_dest_address, p_value, p_line);
	}
}

// True when the active tracer wants local-variable tracking forced on.
bool gdscript_tracer_wants_locals();

#endif // GDSCRIPT_TRACER_H
