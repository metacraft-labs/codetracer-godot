/**************************************************************************/
/*  gdscript_ct_trace.h — CodeTracer GDScript recorder (G2 steps, G3 calls)*/
/**************************************************************************/
// Minimal glue between the GDScript VM and the CTFS writer
// (libcodetracer_trace_writer.a, C ABI). G2 emits per-line steps; G3 adds
// call/return events at the GDScriptFunction::call frame boundary.
// Activated when the env var CT_GDSCRIPT_TRACE=<output-dir> is set; the
// trace lands at <output-dir>/gdscript_trace.ct.
#ifndef GDSCRIPT_CT_TRACE_H
#define GDSCRIPT_CT_TRACE_H

#include "core/string/string_name.h"

#include <cstdint>

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
void gdscript_ct_trace_return();

#endif // GDSCRIPT_CT_TRACE_H
