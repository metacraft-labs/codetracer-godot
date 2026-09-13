/**************************************************************************/
/*  godot_linuxbsd.cpp                                                    */
/**************************************************************************/
/*                         This file is part of:                          */
/*                             GODOT ENGINE                               */
/*                        https://godotengine.org                         */
/**************************************************************************/
/* Copyright (c) 2014-present Godot Engine contributors (see AUTHORS.md). */
/* Copyright (c) 2007-2014 Juan Linietsky, Ariel Manzur.                  */
/*                                                                        */
/* Permission is hereby granted, free of charge, to any person obtaining  */
/* a copy of this software and associated documentation files (the        */
/* "Software"), to deal in the Software without restriction, including    */
/* without limitation the rights to use, copy, modify, merge, publish,    */
/* distribute, sublicense, and/or sell copies of the Software, and to     */
/* permit persons to whom the Software is furnished to do so, subject to  */
/* the following conditions:                                              */
/*                                                                        */
/* The above copyright notice and this permission notice shall be         */
/* included in all copies or substantial portions of the Software.        */
/*                                                                        */
/* THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,        */
/* EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF     */
/* MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. */
/* IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY   */
/* CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,   */
/* TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE      */
/* SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.                 */
/**************************************************************************/

#include "os_linuxbsd.h"

#include "core/profiling/profiling.h"
#include "main/main.h"

#include <unistd.h>

#include <clocale>
#include <cstdio>
#include <cstdlib>

#if defined(ASAN_ENABLED)
#include <sys/resource.h>
#endif

#if defined(CT_HCR_AGENT_ENABLED)
// CodeTracer hot-code-reload agent (Reprobuild HLX-M0/M1, Linux x86_64 ELF).
//
// The agent is linked INTO the engine because that is the only shape the
// provider has: it publishes a branch into a NOP sled in this process's own
// text, from this process's own thread, using raw `mprotect`/`membarrier`
// syscalls. Nothing attaches from outside.
//
// It is inert unless `REPRO_HCR_AGENT_SOCKET` names a socket a coordinator is
// listening on — `repro_hcr_agent_start_from_env` returns 0 immediately when
// the variable is unset — so an engine built this way behaves exactly like an
// unpatched one under every ordinary run.
#include "repro_hcr_agent.h"

#include "modules/modules_enabled.gen.h" // For gdscript.
#ifdef MODULE_GDSCRIPT_ENABLED
// GDH-M5: the recorder's `sourceChanged` handler must be registered BEFORE the
// agent starts, because the hello — and therefore the advertised
// `source-reload` capability — is built at connect time. The module's own
// registration happens inside `Main::setup`, which is later than here, so the
// install call lives beside the agent start instead.
#include "modules/gdscript/gdscript_ct_trace.h"
#endif
#endif

#if defined(__x86_64) || defined(__x86_64__)
void __cpuid(int *r_cpuinfo, int p_info) {
	// Note: Some compilers have a buggy `__cpuid` intrinsic, using inline assembly (based on LLVM-20 implementation) instead.
	__asm__ __volatile__(
			"xchgq %%rbx, %q1;"
			"cpuid;"
			"xchgq %%rbx, %q1;"
			: "=a"(r_cpuinfo[0]), "=r"(r_cpuinfo[1]), "=c"(r_cpuinfo[2]), "=d"(r_cpuinfo[3])
			: "0"(p_info));
}
#endif

// For export templates, add a section; the exporter will patch it to enclose
// the data appended to the executable (bundled PCK).
#if !defined(TOOLS_ENABLED) && defined(__GNUC__)
static const char dummy[8] __attribute__((section("pck"), used)) = { 0 };

// Dummy function to prevent LTO from discarding "pck" section.
extern "C" const char *pck_section_dummy_call() __attribute__((used));
extern "C" const char *pck_section_dummy_call() {
	return &dummy[0];
}
#endif

int main(int argc, char *argv[]) {
#if defined(__x86_64) || defined(__x86_64__)
	int cpuinfo[4];
	__cpuid(cpuinfo, 0x01);

	if (!(cpuinfo[2] & (1 << 20))) {
		printf("A CPU with SSE4.2 instruction set support is required.\n");

		int ret = system("zenity --warning --title \"Godot Engine\" --text \"A CPU with SSE4.2 instruction set support is required.\" 2> /dev/null");
		if (ret != 0) {
			ret = system("kdialog --title \"Godot Engine\" --sorry \"A CPU with SSE4.2 instruction set support is required.\" 2> /dev/null");
		}
		if (ret != 0) {
			ret = system("Xdialog --title \"Godot Engine\" --msgbox \"A CPU with SSE4.2 instruction set support is required.\" 0 0 2> /dev/null");
		}
		if (ret != 0) {
			ret = system("xmessage -center -title \"Godot Engine\" \"A CPU with SSE4.2 instruction set support is required.\" 2> /dev/null");
		}
		abort();
	}
#endif

#if defined(ASAN_ENABLED)
	// Note: Set stack size to be at least 30 MB (vs 8 MB default) to avoid overflow, address sanitizer can increase stack usage up to 3 times.
	struct rlimit stack_lim = { 0x1E00000, 0x1E00000 };
	setrlimit(RLIMIT_STACK, &stack_lim);
#endif

	godot_init_profiler();

#if defined(CT_HCR_AGENT_ENABLED)
	// Started before anything else the engine does, so a coordinator can be
	// waiting on the handshake while the engine boots. The agent runs on its own
	// detached thread and services patch requests as they arrive; passing no
	// symbol table means every request resolves through the ELF resolver
	// (HLX-M1) against this process's real symbols.
	//
	// The return value is reported rather than swallowed: -1 means the socket
	// was named but the agent could not start, which must not look like "no
	// coordinator was configured".
	{
		const char *hcr_profile = repro_hcr_agent_default_support_profile();
#ifdef MODULE_GDSCRIPT_ENABLED
		// GDH-M5. Registering the handler is also what makes the agent
		// advertise `source-reload`, so a build without the GDScript recorder
		// advertises nothing and refuses a `sourceChanged` by name — which is
		// the honest answer for a host that cannot reload a script.
		gdscript_ct_hcr_install_source_reload_handler();
#endif
		// GDH-M5, design §5.6.1: "a poll point the engine chooses". Set
		// `REPRO_HCR_AGENT_POLL=1` and the agent is serviced from
		// `OS_LinuxBSD::run()` between `Main::iteration()` calls instead of
		// from its own detached thread.
		//
		// It is OPT-IN rather than the new default on purpose. The detached
		// thread is what HLX's live-patch drivers
		// (`scripts/hcr-patch-godot-linux.sh`,
		// `scripts/record-and-verify-hcr-m7.sh`) drive today, and a reload
		// campaign has no business changing when a native patch is applied.
		const char *hcr_poll = getenv("REPRO_HCR_AGENT_POLL");
		const bool hcr_polled = hcr_poll != nullptr && hcr_poll[0] != '\0' &&
				hcr_poll[0] != '0';
		int hcr_rc = hcr_polled
				? repro_hcr_agent_start_polling_from_env(hcr_profile, nullptr, 0)
				: repro_hcr_agent_start_from_env(hcr_profile, nullptr, 0);
		if (getenv("REPRO_HCR_AGENT_SOCKET") != nullptr) {
			fprintf(stderr,
					"[ct-hcr] agent start rc=%d profile=%s direct_patch=%d membarrier_sync_core=%d polled=%d source_reload=%d\n",
					hcr_rc, hcr_profile,
					repro_hcr_agent_host_supports_direct_patch(),
					repro_hcr_agent_host_membarrier_sync_core(),
					hcr_polled ? 1 : 0,
					repro_hcr_agent_advertises_source_reload());
		}
	}
#endif

	OS_LinuxBSD os;

	setlocale(LC_CTYPE, "");

	// We must override main when testing is enabled
	TEST_MAIN_OVERRIDE

	char *cwd = (char *)malloc(PATH_MAX);
	ERR_FAIL_NULL_V(cwd, ERR_OUT_OF_MEMORY);
	char *ret = getcwd(cwd, PATH_MAX);

	Error err = Main::setup(argv[0], argc - 1, &argv[1]);

	if (err != OK) {
		free(cwd);
		if (err == ERR_HELP) { // Returned by --help and --version, so success.
			return EXIT_SUCCESS;
		}
		return EXIT_FAILURE;
	}

	if (Main::start() == EXIT_SUCCESS) {
		os.run();
	} else {
		os.set_exit_code(EXIT_FAILURE);
	}
	Main::cleanup();

	if (ret) { // Previous getcwd was successful
		if (chdir(cwd) != 0) {
			ERR_PRINT("Couldn't return to previous working directory.");
		}
	}
	free(cwd);

	godot_cleanup_profiler();
	return os.get_exit_code();
}
