/**************************************************************************/
/*  gdscript_ct_trace.h — CodeTracer GDScript recorder (consumer)         */
/**************************************************************************/
// The CodeTracer GDScript recorder is a pure CONSUMER of the general
// GDScriptTracer hook (gdscript_tracer.h). This is its ONE engine-facing entry
// point: register the recorder's tracer with the VM. Called once from
// initialize_gdscript_module(), before the GDScriptLanguage constructor runs.
// It is a no-op unless CT_GDSCRIPT_TRACE=<output-dir> is set in the environment.
#ifndef GDSCRIPT_CT_TRACE_H
#define GDSCRIPT_CT_TRACE_H

void gdscript_ct_trace_register();

#endif // GDSCRIPT_CT_TRACE_H
