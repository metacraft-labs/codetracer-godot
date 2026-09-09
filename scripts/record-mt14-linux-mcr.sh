#!/usr/bin/env bash
# CodeTracer GDScript recorder — MT14 real combined-trace substrate (Linux MCR).
#
# Records test-programs/mt14 with the PATCHED engine under a LIVE `ct-mcr
# record` on Linux and checks what the resulting artifacts actually contain.
# Everything here runs against real binaries: the real patched engine, the real
# `ct_cli` (ct-mcr) recorder, the real `ct-print` reader. Nothing is mocked and
# nothing is stubbed.
#
# WHAT IT ASSERTS
#
#   1. The program ran correctly under the recorder (its five hand-derived
#      markers, see test-programs/mt14/mt14_mixed.gd).
#   2. The NATIVE MCR container is real and non-trivial: > 0 events, the
#      recorded program is the patched engine, more than one thread.
#   3. The VM container is real: steps / calls / values > 0, and one
#      `gdscript-frame` crossing span per call, strictly nested.
#   4. MT14's actual deliverable — ONE container holding native streams AND VM
#      streams AND crossing spans.
#
# (4) DOES NOT HOLD TODAY, and this script exits nonzero saying so rather than
# reporting a pass on (1)-(3). The cause is structural and is recorded in
# codetracer-specs `GDScript-Recorder.milestones.org` MT2 (CORRECTION,
# 2026-09-05): `ct-mcr record` on Linux is a TWO-PROCESS recorder — the child
# writes native events into shm rings while the PARENT `ct-mcr` owns the CTFS
# container (`openTraceWriter` in ct_cli's `record_cmd.nim`) — so the FFI
# writer's `dlsym(RTLD_DEFAULT, "ct_mcr_shared_ctfs")` attach, which can only
# bridge producers in the SAME address space, finds nothing to attach to. The
# in-process ("cooperative") MCR vehicle that CAN publish a shared container is
# built for macOS arm64 only (`ct_cli/src/ct_cli/cooperative_build.nim`).
#
# Usage:
#   scripts/record-mt14-linux-mcr.sh [<output-dir>]
#
# Environment overrides: BIN, CT_MCR, CT_PRINT.
#
# NOTE ON Xvfb.  The GDScript-Recorder N2 runbook calls for Xvfb + Mesa
# lavapipe. That substrate is what the FLAME demo needs (it draws). This
# recording does not: the engine is built `vulkan=no opengl3=no` and run
# `--headless`, so it selects the dummy display driver and never opens a
# display connection. Wrapping this in `xvfb-run` changes nothing about the
# recording and is therefore not done here.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLATFORM="${PLATFORM:-linuxbsd}"
TARGET="${TARGET:-template_debug}"
ARCH="${ARCH:-x86_64}"
BIN="${BIN:-$REPO/bin/godot.${PLATFORM}.${TARGET}.${ARCH}}"
CT_MCR="${CT_MCR:-$REPO/../codetracer-native-recorder/ct_cli/ct_cli}"
CT_PRINT="${CT_PRINT:-$REPO/../codetracer-trace-format-nim/ct-print}"
PROJ="$REPO/test-programs/mt14"
ENTRY="res://mt14_mixed.gd"

OUT="${1:-${TMPDIR:-/tmp}/ct-mt14-$$}"

log() { printf '[mt14] %s\n' "$*"; }
die() { printf '[mt14] FATAL: %s\n' "$*" >&2; exit 1; }
fail_count=0
check_fail() { printf '[mt14] CHECK-FAIL: %s\n' "$*" >&2; fail_count=$((fail_count + 1)); }

# --- prerequisites, loudly -------------------------------------------------
# A missing prerequisite must never look like a pass. Each of these is a hard
# stop with the path that was looked for.
[[ -x "$BIN" ]]      || die "patched engine not built: $BIN (build it with scons — see the milestone runbook)"
[[ -x "$CT_MCR" ]]   || die "ct-mcr (ct_cli) not found/executable: $CT_MCR"
[[ -x "$CT_PRINT" ]] || die "ct-print not found/executable: $CT_PRINT (build it in codetracer-trace-format-nim)"
[[ -f "$PROJ/project.godot" ]] || die "demo project missing: $PROJ/project.godot"
command -v python3 >/dev/null || die "python3 not on PATH (needed for the span checks)"

mkdir -p "$OUT/nested" || die "cannot create $OUT/nested"
NATIVE_CT="$OUT/godot_native.ct"
NESTED_CT="$OUT/nested/gdscript_trace.ct"
RUN_LOG="$OUT/record.log"

# --- /dev/shm hygiene ------------------------------------------------------
# The recorder's per-thread ring segments (`ct_rb_*`) are leaked whenever a
# recorded child dies before teardown, and an accumulation of them makes later
# bootstrap handshakes fail in a way that looks like a recorder timeout. Count
# before and after so the leak is visible rather than inferred.
shm_ct_count() { ls /dev/shm 2>/dev/null | grep -c '^ct_' || true; }
SHM_BEFORE="$(shm_ct_count)"
log "/dev/shm ct_* segments before: $SHM_BEFORE"

# --- record under a live ct-mcr -------------------------------------------
log "recording $ENTRY under a live ct-mcr record"
log "  engine : $BIN"
log "  project: $PROJ"
CT_GDSCRIPT_TRACE="$OUT/nested" \
	"$CT_MCR" record --output "$NATIVE_CT" -- \
		"$BIN" --headless --path "$PROJ" --script "$ENTRY" \
	>"$RUN_LOG" 2>&1
REC_RC=$?
log "ct-mcr record exit: $REC_RC"
[[ "$REC_RC" -eq 0 ]] || die "ct-mcr record failed (rc=$REC_RC); see $RUN_LOG"

SHM_AFTER="$(shm_ct_count)"
log "/dev/shm ct_* segments after: $SHM_AFTER"
if [[ "$SHM_AFTER" -gt "$SHM_BEFORE" ]]; then
	log "cleaning $((SHM_AFTER - SHM_BEFORE)) leaked ct_* segment(s)"
	for f in $(ls /dev/shm 2>/dev/null | grep '^ct_'); do rm -f "/dev/shm/$f"; done
fi

# --- (1) the program ran correctly ----------------------------------------
# The five markers are derived by hand in the .gd header from the literals in
# SAMPLES; they are duplicated here as literals so the assertion is not
# circular.
for marker in \
	'CT_MT14_COUNT=7' \
	'CT_MT14_TOTAL=250' \
	'CT_MT14_BIGGEST=84' \
	'CT_MT14_MEAN=35.7143' \
	'CT_MT14_FLAG=true'
do
	grep -qF "$marker" "$RUN_LOG" || check_fail "(1) program marker missing from the recorded run: $marker"
done
[[ "$fail_count" -eq 0 ]] && log "(1) all five program markers present"

# --- (2) the native MCR container ------------------------------------------
[[ -f "$NATIVE_CT" ]] || die "(2) no native container at $NATIVE_CT"
NATIVE_INFO="$("$CT_MCR" trace info "$NATIVE_CT" 2>&1)" \
	|| die "(2) 'ct-mcr trace info' failed on $NATIVE_CT: $NATIVE_INFO"
native_events="$(printf '%s\n' "$NATIVE_INFO" | sed -n 's/^events: \([0-9]*\)$/\1/p')"
native_threads="$(printf '%s\n' "$NATIVE_INFO" | sed -n 's/^threads: \([0-9]*\)$/\1/p')"
native_program="$(printf '%s\n' "$NATIVE_INFO" | sed -n 's/^program: \(.*\)$/\1/p')"
[[ -n "$native_events"  && "$native_events"  -gt 0 ]] || check_fail "(2) native container has no events (events='$native_events')"
[[ -n "$native_threads" && "$native_threads" -gt 1 ]] || check_fail "(2) native container recorded <= 1 thread (threads='$native_threads')"
[[ "$native_program" == "$BIN" ]] || check_fail "(2) native container names program '$native_program', expected '$BIN'"
log "(2) native: $native_events events, $native_threads threads, $(stat -c%s "$NATIVE_CT") bytes"

# --- (3) the VM container ---------------------------------------------------
[[ -f "$NESTED_CT" ]] || die "(3) no VM container at $NESTED_CT (was CT_GDSCRIPT_TRACE honoured?)"
# Print the tool's OWN message, not just "it failed". The reader refuses a
# container whose writer predates a format correction, and its refusal names the
# correction and tells you to re-record — which is the whole diagnosis. Swallowing
# it turns "the vendored writer archive is older than the reader" into "ct-print
# --summary failed", and the second sends you looking in the wrong place.
VM_SUMMARY="$("$CT_PRINT" --summary "$NESTED_CT" 2>&1)" \
	|| die "(3) 'ct-print --summary' failed on $NESTED_CT: $VM_SUMMARY"
vm_steps="$(printf '%s\n' "$VM_SUMMARY" | sed -n 's/^ *steps: \([0-9]*\)$/\1/p')"
vm_calls="$(printf '%s\n' "$VM_SUMMARY" | sed -n 's/^ *calls: \([0-9]*\)$/\1/p')"
vm_values="$(printf '%s\n' "$VM_SUMMARY" | sed -n 's/^ *values: \([0-9]*\)$/\1/p')"
[[ -n "$vm_steps"  && "$vm_steps"  -gt 0 ]] || check_fail "(3) VM container has no steps (steps='$vm_steps')"
[[ -n "$vm_calls"  && "$vm_calls"  -gt 0 ]] || check_fail "(3) VM container has no calls (calls='$vm_calls')"
[[ -n "$vm_values" && "$vm_values" -gt 0 ]] || check_fail "(3) VM container has no values (values='$vm_values')"

"$CT_PRINT" --spans --json-out "$NESTED_CT" > "$OUT/vm_spans.json" 2>"$OUT/vm_spans.err" \
	|| die "(3) 'ct-print --spans' failed on $NESTED_CT: $(cat "$OUT/vm_spans.err")"
python3 - "$OUT/vm_spans.json" "$vm_calls" <<'PY'
import json, sys
spans = json.load(open(sys.argv[1]))
expected_calls = int(sys.argv[2])
problems = []
if not spans:
    problems.append("the VM container carries NO crossing spans")
wrong = [s for s in spans if s["span_type"] != "gdscript-frame"]
if wrong:
    problems.append("span types other than gdscript-frame: %s" % sorted({s["span_type"] for s in wrong}))
still_open = [s["span_id"] for s in spans if s["open"]]
if still_open:
    problems.append("spans left open (never settled): %s" % still_open)
if len(spans) != expected_calls:
    problems.append("%d crossing spans for %d recorded calls (expected one per call)"
                    % (len(spans), expected_calls))
# Strict nesting: any two crossings are disjoint or one contains the other. A
# partial overlap would mean the recorder's LIFO crossing stack does not mirror
# its call stack.
for i, a in enumerate(spans):
    for b in spans[i + 1:]:
        a0, a1 = a["start_step"], a["end_step"]
        b0, b1 = b["start_step"], b["end_step"]
        disjoint = a1 < b0 or b1 < a0
        contains = (a0 <= b0 and b1 <= a1) or (b0 <= a0 and a1 <= b1)
        if not (disjoint or contains):
            problems.append("spans %d [%d,%d] and %d [%d,%d] partially overlap"
                            % (a["span_id"], a0, a1, b["span_id"], b0, b1))
if problems:
    for p in problems:
        print("CHECK-FAIL: (3) " + p)
    sys.exit(1)
print("[mt14] (3) %d gdscript-frame crossing spans, one per call, strictly nested" % len(spans))
PY
[[ $? -eq 0 ]] || fail_count=$((fail_count + 1))
log "(3) VM: $vm_steps steps, $vm_calls calls, $vm_values values, $(stat -c%s "$NESTED_CT") bytes"

# --- (4) MT14: ONE container ------------------------------------------------
# MT14's deliverable is a SINGLE .ct holding native streams + VM streams +
# crossing spans. Test it directly: does the native container carry the VM
# streams?
#
# FIRST establish that the negative below is a real negative. A grep for
# `gdscript-frame` that finds nothing because the reader could not open the
# stream AT ALL is indistinguishable, on stdout, from one that found no such
# span — and that is not hypothetical: until 2026-09-10 `ct-print --spans`
# scanned only `DefaultMaxRootEntries` (31) root slots, while a 30-thread MCR
# recording declares 128 and parks `spans.dat` in slot 67, so it reported
# `spans: 0` for a container carrying three `process` spans. The MT14 verdict
# was right for an unrelated reason and the check that produced it could not
# have gone the other way. So: assert the native container's span stream is
# READABLE and non-empty first, and only then that none of its spans is a
# gdscript-frame.
NATIVE_SPANS="$("$CT_PRINT" --spans "$NATIVE_CT" 2>&1)" \
	|| die "(4) 'ct-print --spans' failed on the native container: $NATIVE_SPANS"
native_span_count="$(printf '%s\n' "$NATIVE_SPANS" | sed -n 's/^spans: \([0-9]*\).*/\1/p')"
[[ -n "$native_span_count" && "$native_span_count" -gt 0 ]] \
	|| check_fail "(4) the native container's span stream reads as EMPTY ($native_span_count) — a live MCR recording always carries at least one 'process' span, so this is a reader defect, not an answer"
printf '[mt14] (4) native span stream is readable: %s span(s)\n' "$native_span_count"

NATIVE_HAS_VM=0
if printf '%s\n' "$NATIVE_SPANS" | grep -q 'gdscript-frame'; then
	NATIVE_HAS_VM=1
fi

echo
if [[ "$NATIVE_HAS_VM" -eq 1 ]]; then
	log "(4) MT14 MET: the native container carries the VM crossing spans"
else
	log "(4) MT14 UNMET: the recording SPLIT into two containers —"
	log "      native: $NATIVE_CT"
	log "      VM    : $NESTED_CT"
	log "    The native container carries no gdscript-frame crossing spans, so"
	log "    there is no single combined .ct. See this script's header for why."
	fail_count=$((fail_count + 1))
fi

echo
if [[ "$fail_count" -gt 0 ]]; then
	die "$fail_count check(s) failed (artifacts kept in $OUT)"
fi
log "ALL CHECKS PASSED (artifacts in $OUT)"
