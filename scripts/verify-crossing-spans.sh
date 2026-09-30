#!/usr/bin/env bash
# CodeTracer GDScript recorder — MT3 crossing-span gate.
#
# Records ONE program (test-programs/gdscript/gf_calls.gd: nested calls, and
# engine callbacks entered from native code) twice with the patched engine:
#
#   standalone  no native recorder: the recording must carry NO crossing spans
#               (Mixed-Trace-Implicit-Switch.md §4; GDScript-Recorder MT3).
#   mcr         under a live `ct-mcr record`: the recording must carry one
#               settled, strictly nested `gdscript-frame` span per call.
#
# The two arms keep each other honest. An engine that never emits a crossing
# span passes the standalone arm and fails the MCR arm; one that always emits
# them passes the MCR arm and fails the standalone arm. Both recordings must
# have made calls, so neither verdict can come from an empty recording.
#
# Real binaries throughout: the patched engine, `ct_cli` (ct-mcr), `ct-print`.
#
# Usage:   scripts/verify-crossing-spans.sh
# Environment: BIN, CT_MCR, CT_PRINT, PLATFORM, ARCH, TARGET (as corpus-lib.sh).
set -euo pipefail

case "$(uname -s)-$(uname -m)" in
	Linux-x86_64) : "${PLATFORM:=linuxbsd}" "${ARCH:=x86_64}" ;;
	Darwin-arm64) : "${PLATFORM:=macos}" "${ARCH:=arm64}" ;;
esac
export PLATFORM ARCH

# shellcheck source=scripts/corpus-lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/corpus-lib.sh"

CT_MCR="${CT_MCR:-$REPO/../codetracer-native-recorder/ct_cli/ct_cli}"
[[ -x "$CT_MCR" ]] || die "ct-mcr (ct_cli) not found/executable: $CT_MCR — the MCR arm cannot run, and the gate does not pass without it"
corpus_require_tools
corpus_ensure_engine

WORK="$(mktemp -d "${TMPDIR:-/tmp}/ct-mt3.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

MARKER='CT_G3_RESULT=107'

# test-programs/mt14 is the richer native->VM program, but the engine does not
# currently complete it under `ct-mcr record` on Linux (it crashes before the
# first script line, with or without GDScript tracing), so the gate records a
# program that completes under both.
PROGRAM=gf_calls.gd
new_project() { # <name> -> project dir holding a copy of $PROGRAM
	local dir="$WORK/$1"
	mkdir -p "$dir/trace"
	cp "$PROGRAMS_DIR/$PROGRAM" "$dir/"
	printf 'config_version=5\n[application]\nconfig/name="ctmt3-%s"\n' "$1" >"$dir/project.godot"
	printf '%s\n' "$dir"
}

log "standalone arm: no native recorder"
P="$(new_project standalone)"
CT_GDSCRIPT_TRACE="$P/trace" "$BIN" --headless --path "$P" --script res://$PROGRAM >"$P/run.log" 2>&1 || true
grep -qF "$MARKER" "$P/run.log" || die "standalone: the program did not run to completion ($MARKER missing; see $P/run.log)"
[[ -f "$P/trace/gdscript_trace.ct" ]] || die "standalone: no recording produced"
failed=()
corpus_check_crossing_spans "$P/trace/gdscript_trace.ct" standalone || failed+=(standalone)

log "mcr arm: under a live ct-mcr record"
P="$(new_project mcr)"
CT_GDSCRIPT_TRACE="$P/trace" "$CT_MCR" record --output "$P/native.ct" -- \
	"$BIN" --headless --path "$P" --script res://$PROGRAM >"$P/run.log" 2>&1 \
	|| die "mcr: ct-mcr record failed; see $P/run.log"
grep -qF "$MARKER" "$P/run.log" || die "mcr: the program did not run to completion ($MARKER missing; see $P/run.log)"
[[ -f "$P/trace/gdscript_trace.ct" ]] || die "mcr: no GDScript recording produced"
corpus_check_crossing_spans "$P/trace/gdscript_trace.ct" mcr || failed+=(mcr)

[[ "${#failed[@]}" -eq 0 ]] || die "MT3 crossing-span gate FAILED: ${failed[*]} arm(s)"

log "MT3 crossing-span gate PASSED (standalone: none; under MCR: one per call)"
