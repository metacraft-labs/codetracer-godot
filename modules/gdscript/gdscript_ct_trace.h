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

#if defined(CT_HCR_AGENT_ENABLED)
// GDH-M5 — the reload seam.
//
// `gdscript_ct_hcr_install_source_reload_handler()` registers the recorder's
// `sourceChanged` handler with the in-process agent AND, by the same act, makes
// the agent advertise `source-reload` (design §4.4: a host that cannot apply a
// reload must not claim it can). It must run BEFORE the agent is started,
// because the hello is built at connect time — which is why it is called from
// `platform/linuxbsd/godot_linuxbsd.cpp` beside the agent start and not from
// the module's own registration, which happens later.
//
// `gdscript_ct_hcr_safe_point()` is the poll point design §5.6.1 requires: "a
// poll point the engine CHOOSES". It is called once per frame from
// `OS_LinuxBSD::run()`, between `Main::iteration()` calls, which is the only
// moment at which no GDScript frame is executing and no step's values are in
// flight. A reload that arrived on the agent's own thread while the VM was
// mid-step is DEFERRED to here rather than applied where it landed.
void gdscript_ct_hcr_install_source_reload_handler();
void gdscript_ct_hcr_safe_point();
#endif

#endif // GDSCRIPT_CT_TRACE_H
