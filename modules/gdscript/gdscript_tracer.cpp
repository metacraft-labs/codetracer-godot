/**************************************************************************/
/*  gdscript_tracer.cpp                                                   */
/**************************************************************************/
/*                         This file is part of:                          */
/*                             GODOT ENGINE                               */
/*                        https://godotengine.org                         */
/**************************************************************************/

#include "gdscript_tracer.h"

#include "gdscript.h"
#include "gdscript_function.h"

#include "core/templates/list.h"
#include "core/templates/pair.h"

// The one registered tracer (nullptr when none). `gdscript_tracer_active`
// mirrors it as the fast-path pointer the inline shims branch on.
static GDScriptTracer *g_gdscript_tracer = nullptr;
GDScriptTracer *gdscript_tracer_active = nullptr;

void GDScriptTracer::set_active(GDScriptTracer *p_tracer) {
	g_gdscript_tracer = p_tracer;
	gdscript_tracer_active = p_tracer;
}

GDScriptTracer *GDScriptTracer::get_active() {
	return g_gdscript_tracer;
}

bool gdscript_tracer_wants_locals() {
	return g_gdscript_tracer != nullptr && g_gdscript_tracer->wants_local_tracking();
}

// Resolve a write ADDRESS (stack slot or instance member) to its source-level
// declared name and RETURN it (empty StringName = drop). This is the debugger's
// own slot->name mapping, reused: STACK writes resolve through
// GDScriptFunction::debug_get_stack_member_state (the table behind
// debug_get_stack_level_locals), and MEMBER writes through the owning script's
// member_indices (debug_get_member_by_index). Un-named slots (expression
// temporaries never enter stack_debug) return an empty name — the caller only
// records writes that correspond to a real source-level variable. The caller
// (a GDScriptTracer's on_slot_write) invokes this at emit time, so resolution can
// happen under the consumer's own lock rather than eagerly in the VM hook.
StringName gdscript_trace_resolve_slot_name(const GDScriptFunction *p_func, int p_dest_address, int p_line) {
	if (!p_func) {
		return StringName();
	}

	int addr_type = (p_dest_address & GDScriptFunction::ADDR_TYPE_MASK) >> GDScriptFunction::ADDR_BITS;
	int slot = p_dest_address & GDScriptFunction::ADDR_MASK;

	if (addr_type == GDScriptFunction::ADDR_TYPE_MEMBER) {
		// A write into an instance member slot: the common `member = expr` /
		// `self.member = expr` case, member initializers, @export defaults and
		// @onready assignments. The 24-bit slot is the member INDEX into the
		// instance's `members` array; invert it via the owning script.
		const GDScript *scr = p_func->get_script();
		if (!scr) {
			return StringName();
		}
		StringName mname = scr->debug_get_member_by_index(slot);
		// debug_get_member_by_index returns "<error>" for an unresolvable slot.
		if (mname == StringName() || mname == StringName("<error>")) {
			return StringName();
		}
		return mname;
	}

	if (addr_type != GDScriptFunction::ADDR_TYPE_STACK) {
		return StringName();
	}

	// Resolve the stack slot -> declared name using the same table Godot's own
	// debugger uses for debug_get_stack_level_locals. We pass `line + 1` so a
	// variable declared ON the current line (its stack_debug entry has
	// sd.line == line) is in scope: debug_get_stack_member_state keeps entries
	// with sd.line < p_line.
	List<Pair<StringName, int>> locals;
	p_func->debug_get_stack_member_state(p_line + 1, &locals);
	for (const Pair<StringName, int> &e : locals) {
		if (e.second == slot) {
			return e.first;
		}
	}
	// Slot is not a named local at this line: a compiler temporary — dropped.
	return StringName();
}
