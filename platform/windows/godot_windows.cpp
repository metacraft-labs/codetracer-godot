/**************************************************************************/
/*  godot_windows.cpp                                                     */
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

#include "os_windows.h"

#include "core/profiling/profiling.h"
#include "main/main.h"

#include <clocale>
#include <cstdio>

#if defined(CT_HCR_WINDOWS_AGENT_LOADER)
typedef DWORD(WINAPI *ReproHcrWindowsBootstrapProc)(void *);

static bool start_repro_hcr_windows_agent_from_env() {
	wchar_t agent_path[32768];
	const DWORD capacity = DWORD(sizeof(agent_path) / sizeof(agent_path[0]));
	const DWORD length = GetEnvironmentVariableW(L"REPRO_HCR_AGENT_DLL", agent_path, capacity);
	if (length == 0) {
		return true;
	}
	if (length >= capacity) {
		fprintf(stderr, "[ct-hcr] REPRO_HCR_AGENT_DLL exceeds the Windows path buffer\n");
		return false;
	}
	HMODULE agent = LoadLibraryW(agent_path);
	if (agent == nullptr) {
		fprintf(stderr, "[ct-hcr] LoadLibraryW(REPRO_HCR_AGENT_DLL) failed: %lu\n", (unsigned long)GetLastError());
		return false;
	}
	auto bootstrap = reinterpret_cast<ReproHcrWindowsBootstrapProc>(
			GetProcAddress(agent, "ReproHcrWindowsBootstrap"));
	if (bootstrap == nullptr) {
		fprintf(stderr, "[ct-hcr] agent lacks ReproHcrWindowsBootstrap\n");
		return false;
	}
	const DWORD result = bootstrap(nullptr);
	fprintf(stderr,
			"[ct-hcr] Windows agent start rc=%lu module=%p main_thread=%lu\n",
			(unsigned long)result, static_cast<void *>(agent),
			(unsigned long)GetCurrentThreadId());
	return result == 0;
}
#endif

// For export templates, add a section; the exporter will patch it to enclose
// the data appended to the executable (bundled PCK).
#ifndef TOOLS_ENABLED
#if defined _MSC_VER
#pragma section("pck", read)
__declspec(allocate("pck")) static char dummy[8] = { 0 };

// Dummy function to prevent LTO from discarding "pck" section.
extern "C" char *__cdecl pck_section_dummy_call() {
	return &dummy[0];
}
#if defined _AMD64_
#pragma comment(linker, "/include:pck_section_dummy_call")
#elif defined _X86_
#pragma comment(linker, "/include:_pck_section_dummy_call")
#endif

#elif defined __GNUC__
static const char dummy[8] __attribute__((section("pck"), used)) = { 0 };
#endif
#endif

char *wc_to_utf8(const wchar_t *wc) {
	int ulen = WideCharToMultiByte(CP_UTF8, 0, wc, -1, nullptr, 0, nullptr, nullptr);
	char *ubuf = new char[ulen + 1];
	WideCharToMultiByte(CP_UTF8, 0, wc, -1, ubuf, ulen, nullptr, nullptr);
	ubuf[ulen] = 0;
	return ubuf;
}

int widechar_main(int argc, wchar_t **argv) {
	godot_init_profiler();

	// Prevent Windows from replacing the window with a "ghost" when the main
	// thread briefly stalls (e.g. during editor startup or focus transitions).
	// The ghost window intercepts click-backs, so the editor never receives
	// WM_ACTIVATEAPP and gets permanently stuck at the unfocused throttle.
	// Must be called before any window is created.
	DisableProcessWindowsGhosting();

	OS_Windows os(nullptr);

	setlocale(LC_CTYPE, "");

	char **argv_utf8 = new char *[argc];

	for (int i = 0; i < argc; ++i) {
		argv_utf8[i] = wc_to_utf8(argv[i]);
	}

	TEST_MAIN_PARAM_OVERRIDE(argc, argv_utf8)

	Error err = Main::setup(argv_utf8[0], argc - 1, &argv_utf8[1]);

	if (err != OK) {
		for (int i = 0; i < argc; ++i) {
			delete[] argv_utf8[i];
		}
		delete[] argv_utf8;

		if (err == ERR_HELP) { // Returned by --help and --version, so success.
			return EXIT_SUCCESS;
		}
		return EXIT_FAILURE;
	}

#if defined(CT_HCR_WINDOWS_AGENT_LOADER)
	// Main::setup has initialized the platform and the recorder has registered
	// this thread, while Main::start has not launched the project yet. Loading
	// here keeps the agent outside loader lock and makes its pipe worker pass
	// through the recorder's normal in-process thread-registration path.
	if (!start_repro_hcr_windows_agent_from_env()) {
		for (int i = 0; i < argc; ++i) {
			delete[] argv_utf8[i];
		}
		delete[] argv_utf8;
		Main::cleanup();
		return EXIT_FAILURE;
	}
#endif

	if (Main::start() == EXIT_SUCCESS) {
		os.run();
	} else {
		os.set_exit_code(EXIT_FAILURE);
	}
	Main::cleanup();

	for (int i = 0; i < argc; ++i) {
		delete[] argv_utf8[i];
	}
	delete[] argv_utf8;

	godot_cleanup_profiler();
	return os.get_exit_code();
}

int _main() {
	LPWSTR *wc_argv;
	int argc;
	int result;

	wc_argv = CommandLineToArgvW(GetCommandLineW(), &argc);

	if (nullptr == wc_argv) {
		wprintf(L"CommandLineToArgvW failed\n");
		return 0;
	}

	result = widechar_main(argc, wc_argv);

	LocalFree(wc_argv);
	return result;
}

int main(int argc, char **argv) {
	// override the arguments for the test handler / if symbol is provided
	// TEST_MAIN_OVERRIDE

	// _argc and _argv are ignored
	// we are going to use the WideChar version of them instead
#if defined(CRASH_HANDLER_EXCEPTION) && defined(_MSC_VER)
	__try {
		return _main();
	} __except (CrashHandlerException(GetExceptionInformation())) {
		return 1;
	}
#else
	return _main();
#endif
}

HINSTANCE godot_hinstance = nullptr;

int WINAPI WinMain(HINSTANCE hInstance, HINSTANCE hPrevInstance, LPSTR lpCmdLine, int nCmdShow) {
	godot_hinstance = hInstance;
	return main(0, nullptr);
}
