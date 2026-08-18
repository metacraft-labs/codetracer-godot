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

// GF8: called from the member-write opcodes that carry the member NAME directly
// (not a stack-slot address that gdscript_ct_trace_assign resolves):
//   - OPCODE_SET_STATIC_VARIABLE — `static var` write; name resolved by the
//     caller via GDScript::debug_get_static_var_by_index(index).
//   - OPCODE_SET_MEMBER — a self native/registered property write; `name` is the
//     StringName the opcode already holds (_global_names_ptr[indexname]).
//   - OPCODE_SET_NAMED — an in-place named write on a base Variant (e.g.
//     `vec.x = 1`); `name` is the mutated field's StringName.
//   - OPCODE_SET_NAMED_VALIDATED — the typed-base variant of the above (e.g.
//     `var vt: Vector2; vt.y = 8`), emitted when the base's static type has a
//     validated setter for the member. The opcode carries only a setter
//     pointer + index, so the caller recovers `name` from the DEBUG-only
//     setter_names table (GDScriptFunction::setter_names[index]) the codegen
//     populated alongside the setters vector.
// `value` is the freshly written Variant. The value is encoded with the SAME
// recursive ct_value_* encoder the G4/GF3/GF4 stack path uses and attached to
// the current step via trace_writer_register_variable_cbor, so member values
// stay parallel-indexed to steps (it refuses to emit before the first step
// exists, just like gdscript_ct_trace_assign). No-op (cheap) when tracing is
// inactive. INSTANCE-member writes addressed via ADDR_TYPE_MEMBER (the common
// `member = expr` / `self.member = expr` / member-initializer / @onready /
// @export-default case) do NOT come through here — they arrive as an ordinary
// OPCODE_ASSIGN* whose destination address gdscript_ct_trace_assign now resolves
// to a member name via GDScript::debug_get_member_by_index.
void gdscript_ct_trace_member_assign(const StringName &name, const Variant &value);

// GF10: async continuation (coroutines & `await`). Conforms to CodeTracer's
// async-continuation model (HTTP-Request-Panel.md §3.2 ContinuationLink /
// Async-Continuation-Algorithms.md §5 AsyncLinkRecord), it does NOT invent a
// parallel scheme.
//
// The GDScript `await` suspend/resume path re-enters GDScriptFunction::call with
// a CallState (`p_state`) — the suspended coroutine frame. On suspension
// (OPCODE_AWAIT with a Signal) the VM creates a Ref<GDScriptFunctionState> whose
// `state` member IS that CallState; on resumption GDScriptFunctionState::resume
// calls `function->call(..., &state)`, so `&gdfs->state` at suspend and
// `p_state` at resume are the SAME pointer. That pointer is the async
// `context_id` (analogous to Nim's Future ptr / Python's coroutine obj).
//
// The recorder emits two markers per await via trace_writer_register_special_event
// (an events.dat record whose reader-side step_id is the current step): a SUSPEND
// marker carrying context_id at the `await` step, and a RESUME marker carrying
// the SAME context_id at the first resumed line. The db-backend pairs them by
// context_id into a ContinuationLink (link_type "await"): registration.step_id =
// the suspend step, continuation.step_id = the resume step. No C-ABI extension is
// required — register_special_event already carries (kind, metadata, content).
//
// Call/return stays BALANCED across the yield with no change to G3: each
// GDScriptFunction::call invocation still runs exactly one enter/exit pair (the
// suspend exit and the resume entry are separate invocations), so a suspended
// coroutine records as two adjacent balanced frames and the coroutine's locals
// survive the suspension (Godot saves/restores the stack), readable with their
// pre-await values on resume. This resolves GDScript-Recorder.md open question #4.

// SUSPEND: called from the OPCODE_AWAIT handler right after `awaited = true`,
// with `call_state` = &gdfs->state (the CallState the resume will re-enter with).
// Emits a suspend marker bound to the current (await-line) step.
void gdscript_ct_trace_await_suspend(const void *call_state);

// RESUME: called from the OPCODE_AWAIT_RESUME handler with `call_state` =
// `p_state` (the same CallState pointer as the matching suspend). The marker is
// DEFERRED to the next per-line step (see gdscript_ct_trace_step) so
// continuation.step_id is the first resumed source line — strictly greater than
// the suspend step, as the ContinuationLink model requires.
void gdscript_ct_trace_await_resume(const void *call_state);

// GF13: diagnostics. GDScript has NO exceptions (there is no try/catch to
// record); push_error / push_warning are the diagnostic surface. They are native
// CORE Variant utility functions (variant_utility.cpp), vararg, so the compiler
// emits them as OPCODE_CALL_UTILITY (never the validated form) — the seam this
// hook is called from, right after Variant::call_utility_function runs. When
// `function` is push_error or push_warning, the diagnostic is recorded as an
// events.dat SPECIAL EVENT (the same channel GF10's async markers use — NOT an
// invented exception model) carrying the joined vararg message (mirroring the
// engine's own join_string). The multi-stream writer maps the FfiEventLogKind to
// an IOEventKind, so `ct-print --full` renders them as two DISTINCT io kinds:
//   push_error   -> FFI_EVENT_ERROR          (io_kind ioError)
//   push_warning -> FFI_EVENT_TRACE_LOG_EVENT (io_kind ioStderr)
// The diagnostic LEVEL is additionally tagged in the event METADATA
// ("ct-push-error" / "ct-push-warning") for a real event-log pane, though
// ct-print does not surface multi-stream io metadata (the io_kind + message
// already distinguish warning from error). The message is the event `content`
// (surfaced as the io event's `text`).
// The event binds to the current step (the push_* call site's own OPCODE_LINE
// step). Any OTHER utility function (print, str, typeof, ...) is ignored, so the
// step/value/call streams of programs that call no diagnostics are byte-identical
// to GF12. No-op (cheap) when tracing is inactive.
//
// `assert` needs NO hook: an `assert(cond, msg)` statement occupies its own
// source line, so its OPCODE_LINE already records it as an ordinary step (a
// passing assert is a no-op step and execution continues); a FAILING assert
// halts the VM in a debug build (OPCODE_BREAK), and the atexit flush still
// serializes whatever was recorded up to the assert line.
void gdscript_ct_trace_utility_diagnostic(const StringName &function,
		const Variant **args, int argc);

#endif // GDSCRIPT_CT_TRACE_H
