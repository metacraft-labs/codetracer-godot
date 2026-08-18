/**************************************************************************/
/*  gdscript_ct_trace.h — CodeTracer GDScript recorder (G2 steps, G3 calls,*/
/*  G4 values)                                                            */
/**************************************************************************/
// Minimal glue between the GDScript VM and the CTFS writer
// (libcodetracer_trace_writer.a, C ABI). G2 emits per-line steps; G3 adds
// call/return events at the GDScriptFunction::call frame boundary; G4 adds
// captured local/argument values for written stack slots.
// Activated when the env var CT_GDSCRIPT_TRACE=<output-dir> is set; the
// trace lands at <output-dir>/gdscript_trace.ct.
#ifndef GDSCRIPT_CT_TRACE_H
#define GDSCRIPT_CT_TRACE_H

#include "core/string/string_name.h"

#include <cstdint>

class GDScriptFunction;
class Variant;

// True when CT_GDSCRIPT_TRACE is set (i.e. the recorder is active for this
// process). Cheap and cached; safe to call before the writer exists. Used by
// GDScriptLanguage to force local-variable tracking (stack_debug population) so
// the G4 slot->name mapping has data to work with.
bool gdscript_ct_trace_active();

// Called from the OPCODE_LINE handler in gdscript_vm.cpp for every executed
// source line. Lazily creates the writer on first call (no-op / cheap when
// CT_GDSCRIPT_TRACE is unset). `source` is GDScriptFunction::source.
void gdscript_ct_trace_step(const StringName &source, int64_t line);

// G3: called right after GDScriptLanguage::enter_function() at the top of
// GDScriptFunction::call(). Interns the function (name, source path, initial
// line) and emits a Call. `name`/`source` are GDScriptFunction members.
// enter_function() and exit_function() are bracketed once per call()
// invocation, so pairing gdscript_ct_trace_call with gdscript_ct_trace_return
// keeps the call stream balanced with no manual depth tracking — the loader
// reconstructs depth/parent/child.
void gdscript_ct_trace_call(const StringName &name, const StringName &source, int64_t line);

// G3: called right before GDScriptLanguage::exit_function() on BOTH exit
// paths (the normal return and the yield/await-resume exit) of
// GDScriptFunction::call(). Emits a Return, popping the frame pushed by the
// matching gdscript_ct_trace_call. Every call() invocation runs exactly one
// enter/exit pair, so calls and returns stay balanced even across an `await`
// suspension (a suspended coroutine simply records as two adjacent balanced
// frames rather than one spanning frame — full await-continuation semantics
// are GF10; non-coroutine nesting is exact).
//
// GF5: the return VALUE is now captured. `return_value` is the VM's `retvalue`
// at the exit path — the Variant the OPCODE_RETURN* opcode stored, or a
// default-constructed NIL for a `-> void` / fall-off-the-end function. It is
// encoded with the SAME recursive ct_value_* encoder G4/GF3/GF4 use and
// attached to the return record via trace_writer_register_return_cbor. This is
// ADDITIONAL data on the existing return event: it does NOT add, remove, or
// reorder any call/return record, so the G3 nesting + balanced-pair invariant
// is unchanged (register_return_cbor calls the same registerReturn the bare
// register_return does, just with value bytes). A void / no-return function
// records a None return value (retvalue is NIL), not the format's bare
// VoidReturnMarker — consistent with G4's `null -> None` scalar handling.
void gdscript_ct_trace_return(const Variant &return_value);

// G4: called from the write opcodes (OPCODE_ASSIGN* / OPCODE_OPERATOR* /
// typed-assign variants) in gdscript_vm.cpp right AFTER the result Variant is
// stored to its destination slot. `dest_address` is the raw 24-bit-encoded
// address operand for the destination (`_code_ptr[ip + 1 + dst_ofs]`); `value`
// is the freshly written Variant (`*dst`); `line` is the VM's current source
// line. Only STACK-slot writes that resolve to a NAMED local/argument at the
// current line are recorded — compiler temporaries (which either never enter
// GDScriptFunction::stack_debug or carry an `@`-prefixed synthetic name) are
// skipped, mirroring codetracer-nim's resolveTracedSlotSym. The value is
// encoded to CBOR with the writer's streaming ct_value_* encoder and attached
// to the current step via trace_writer_register_variable_cbor, so values stay
// parallel-indexed to steps. No-op (cheap) when tracing is inactive.
void gdscript_ct_trace_assign(const GDScriptFunction *func, int dest_address,
		const Variant &value, int line);

#endif // GDSCRIPT_CT_TRACE_H
