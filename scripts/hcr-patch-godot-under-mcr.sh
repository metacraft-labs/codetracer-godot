#!/usr/bin/env bash
# CodeTracer HCR + MCR — patch a live Godot engine WHILE it is being recorded.
#
# The combined story the campaign is aiming at: one process, recorded by MCR,
# hot-patched by HCR, with both surviving. This script is the measurement of
# whether that holds today. It runs the HCR1 demo under a live `ct-mcr record`
# and publishes the same direct entry patch part-way through the recording.
#
# WHAT IS NOW REQUIRED TO BE PRESENT. `CodePatchEvent` is HLX-M7 and HAS LANDED,
# so the trace must carry exactly one. It was not always so: before HLX-M7 this
# script reported the event's ABSENCE, and reported it carefully, because a grep
# that finds nothing is satisfied for free by a reader that failed
# (`codetracer-specs/Testing/Verification-Harness-Traps.md`, trap 4). That
# discipline is kept and still earns its place — the event dump is proved
# COMPLETE (line count equal to the count `trace info` reports, never merely
# non-empty) before anything is read out of it, which is now what stops a
# truncated dump from hiding the event rather than what stopped an empty one
# from vacuously "confirming" its absence.
#
# The field-by-field verification of the event — every digest recomputed from
# independently obtained bytes, and the differential against an unpatched
# control — is `scripts/record-and-verify-hcr-m7.sh`, which calls this script.
#
# Usage: scripts/hcr-patch-godot-under-mcr.sh [<output-dir>]
# Environment: BIN, CT_MCR, CT_PRINT, REPROBUILD_DIR, PATCH_AFTER, MARKER_TIMEOUT_MS
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPROBUILD_DIR="${REPROBUILD_DIR:-$(cd "$REPO/.." && pwd)/reprobuild}"
BIN="${BIN:-$REPO/bin/godot.linuxbsd.template_debug.x86_64.hcr}"
CT_MCR="${CT_MCR:-$REPO/../codetracer-native-recorder/ct_cli/ct_cli}"
# The patch is gated on a marker the ENGINE PRINTS, not on a timer. Under
# `ct-mcr record` this is not a refinement, it is the difference between a demo
# and a non-demo: measured on this host across two independent runs, recording
# pushes the engine's time-to-first-output from ~0.1 s to 16-18 s (the exact
# figure moves with host load; the +16-18 s shape does not), so the 6000 ms delay
# this script originally used published the patch before tick 1 and produced 40
# post-patch observations with nothing to compare them to.
PATCH_AFTER="${PATCH_AFTER:-CT_HCR_TICK=8 }"
MARKER_TIMEOUT_MS="${MARKER_TIMEOUT_MS:-300000}"
PROJ="$REPO/test-programs/hcr1"
ENTRY="res://hcr1_live_patch.gd"
PATCH_SYMBOL="hcr1_patch_processor_count"
OUT="${1:-${TMPDIR:-/tmp}/ct-hcr1-mcr-$$}"

log() { printf '[hcr1-mcr] %s\n' "$*"; }
die() { printf '[hcr1-mcr] FATAL: %s\n' "$*" >&2; exit 1; }
fail_count=0
check_fail() { printf '[hcr1-mcr] CHECK-FAIL: %s\n' "$*" >&2; fail_count=$((fail_count + 1)); }

[[ -x "$BIN" ]]    || die "patchable engine not built: $BIN"
[[ -x "$CT_MCR" ]] || die "ct-mcr (ct_cli) not found/executable: $CT_MCR"
[[ -f "$PROJ/project.godot" ]] || die "demo project missing: $PROJ/project.godot"
command -v gcc >/dev/null || die "gcc not on PATH"
command -v python3 >/dev/null || die "python3 not on PATH"
mkdir -p "$OUT" || die "cannot create $OUT"

log "checking the engine is patchable-shaped"
python3 "$REPO/scripts/verify_hcr_patchable.py" "$BIN" > "$OUT/patchable-shape.log" 2>&1 \
	|| { cat "$OUT/patchable-shape.log"; die "engine is not patchable-shaped"; }

# --- /dev/shm hygiene, before AND after ------------------------------------
# MCR leaks one `ct_rb_*` segment per recorded thread (~30 per Godot run) when a
# recorded child dies before teardown, and an accumulation has been measured to
# make later bootstrap handshakes fail in a way that looks like a recorder
# timeout. So clean -- but only segments whose OWNING PID IS DEAD. A blanket
# `rm /dev/shm/ct_*` would destroy a concurrent recording belonging to someone
# else on this machine, and would do it silently.
shm_ct_names() {
	# A glob, not `ls | grep`: shellcheck SC2010, and a filename with a newline
	# in it would otherwise be counted twice.
	local f
	shopt -s nullglob
	for f in /dev/shm/ct_*; do printf '%s\n' "${f##*/}"; done
	shopt -u nullglob
}
shm_ct_count() { shm_ct_names | wc -l; }
shm_ct_clean_dead() {
	local f pid removed=0
	while IFS= read -r f; do
		[[ -n "$f" ]] || continue
		pid="$(printf '%s' "$f" | grep -oE '[0-9]+' | head -n1)"
		if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
			log "leaving /dev/shm/$f alone: pid $pid is still alive"
			continue
		fi
		rm -f "/dev/shm/$f" && removed=$((removed + 1))
	done < <(shm_ct_names)
	[[ "$removed" -gt 0 ]] && log "removed $removed leaked ct_* segment(s)"
	return 0
}
log "cleaning leaked ct_* segments before the run"
shm_ct_clean_dead
SHM_BEFORE="$(shm_ct_count)"
log "/dev/shm ct_* segments before: $SHM_BEFORE"

# --- target symbol, patch object, driver ------------------------------------
TARGET_SYMBOL="${TARGET_SYMBOL:-}"
if [[ -z "$TARGET_SYMBOL" ]]; then
	mapfile -t candidates < <(nm --defined-only "$BIN" 2>/dev/null \
		| awk '$2 == "T" || $2 == "t" { print $3 }' \
		| grep -E '^_ZNK8CoreBind2OS19get_processor_countEv$' | sort -u)
	[[ "${#candidates[@]}" -eq 1 ]] \
		|| die "expected exactly one target symbol, found ${#candidates[@]}"
	TARGET_SYMBOL="${candidates[0]}"
fi
log "target symbol: $TARGET_SYMBOL"

PATCH_OBJ="$OUT/hcr1_patch_bodies.o"
gcc -c -O2 -ffunction-sections "$PROJ/hcr1_patch_bodies.c" -o "$PATCH_OBJ" \
	|| die "could not compile the patch bodies"
EXPECTED_AFTER="$(objdump -d --section=".text.$PATCH_SYMBOL" "$PATCH_OBJ" \
	| sed -n 's/.*mov *\$0x\([0-9a-f]*\),%eax.*/\1/p' | head -n1)"
[[ -n "$EXPECTED_AFTER" ]] || die "could not read the immediate out of $PATCH_SYMBOL"
EXPECTED_AFTER="$((16#$EXPECTED_AFTER))"

DRIVER="$OUT/hcr_patch_driver"
log "building the coordinator driver"
(
	cd "$REPROBUILD_DIR"
	nix develop --no-write-lock-file --command \
		nim c -d:release --hints:off --warnings:off \
		--outdir:"$OUT" scripts/hcr_patch_driver.nim
) > "$OUT/driver-build.log" 2>&1 || { tail -30 "$OUT/driver-build.log"; die "could not build the driver"; }

# --- record + patch ---------------------------------------------------------
SOCK="$OUT/hcr1-mcr.sock"
NATIVE_CT="$OUT/godot_native.ct"
RUN_LOG="$OUT/record.log"
DRIVER_LOG="$OUT/driver.log"
DRIVER_JSON="$OUT/patch-result.json"

log "starting the coordinator (patching once the engine prints '$PATCH_AFTER')"
: > "$RUN_LOG"
"$DRIVER" --socket "$SOCK" \
	--target-symbol "$TARGET_SYMBOL" \
	--patch-object "$PATCH_OBJ" \
	--patch-symbol "$PATCH_SYMBOL" \
	--patch-id "hcr1-godot-under-mcr" \
	--wait-for "$RUN_LOG" --marker "$PATCH_AFTER" \
	--marker-timeout-ms "$MARKER_TIMEOUT_MS" \
	--json-out "$DRIVER_JSON" \
	> "$DRIVER_LOG" 2>&1 &
DRIVER_PID=$!
for _ in $(seq 1 100); do [[ -S "$SOCK" ]] && break; sleep 0.1; done
[[ -S "$SOCK" ]] || { cat "$DRIVER_LOG"; die "driver never created $SOCK"; }

log "recording $ENTRY under a live ct-mcr record"
REPRO_HCR_AGENT_SOCKET="$SOCK" \
	"$CT_MCR" record --output "$NATIVE_CT" -- \
		"$BIN" --headless --path "$PROJ" --script "$ENTRY" \
	>"$RUN_LOG" 2>&1
REC_RC=$?
wait "$DRIVER_PID"; DRIVER_RC=$?
log "ct-mcr record exit: $REC_RC   driver exit: $DRIVER_RC"
cat "$DRIVER_LOG"

SHM_AFTER="$(shm_ct_count)"
log "/dev/shm ct_* segments after: $SHM_AFTER"
if [[ "$SHM_AFTER" -gt "$SHM_BEFORE" ]]; then
	log "this run leaked $((SHM_AFTER - SHM_BEFORE)) ct_* segment(s); cleaning the dead ones"
	shm_ct_clean_dead
	log "/dev/shm ct_* segments after cleanup: $(shm_ct_count)"
fi

# --- (A) did the patch apply, and is it observable in the recorded run? -----
if [[ "$DRIVER_RC" -eq 2 ]]; then
	log "RESULT: the agent REFUSED the patch under MCR. Its own words:"
	python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); f=d.get("patchFailed",{}); print("  stage  :",f.get("stage")); print("  message:",f.get("message"))' "$DRIVER_JSON"
	exit 2
fi
[[ "$DRIVER_RC" -eq 0 ]] || { log "RESULT: driver failure (rc=$DRIVER_RC), not a refusal"; exit 1; }
[[ "$REC_RC" -eq 0 ]] || check_fail "(A) ct-mcr record failed (rc=$REC_RC); see $RUN_LOG"

python3 - "$RUN_LOG" "$(nproc)" "$EXPECTED_AFTER" <<'PY'
import re, sys
log, host_cpus, expected_after = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
ticks = [(int(m.group(1)), int(m.group(2)))
         for m in (re.search(r"CT_HCR_TICK=(\d+) CT_HCR_VALUE=(-?\d+)", line)
                   for line in open(log, errors="replace")) if m]
if not ticks:
    print("[hcr1-mcr] CHECK-FAIL: (A) the recorded run printed NO CT_HCR_TICK lines")
    sys.exit(1)
before = [v for _, v in ticks if v != expected_after]
after = [v for _, v in ticks if v == expected_after]
problems = []
if not before:
    problems.append("(A) every tick already showed %d under MCR" % expected_after)
if not after:
    problems.append("(A) no tick showed %d: the patch did not take effect under MCR" % expected_after)
if any(v != host_cpus for v in before):
    problems.append("(A) pre-patch ticks reported %s, expected %d"
                    % (sorted(set(before)), host_cpus))
print("[hcr1-mcr] (A) %d ticks under MCR: %d before (value=%d), %d after (value=%d)"
      % (len(ticks), len(before), host_cpus, len(after), expected_after))
for p in problems:
    print("[hcr1-mcr] CHECK-FAIL: " + p)
sys.exit(1 if problems else 0)
PY
[[ $? -eq 0 ]] || fail_count=$((fail_count + 1))

# --- (B) did the recording survive? ----------------------------------------
[[ -f "$NATIVE_CT" ]] || die "(B) no native container at $NATIVE_CT"
NATIVE_INFO="$("$CT_MCR" trace info "$NATIVE_CT" 2>&1)" \
	|| die "(B) 'ct-mcr trace info' failed on $NATIVE_CT: $NATIVE_INFO"
printf '%s\n' "$NATIVE_INFO" > "$OUT/trace-info.txt"
native_events="$(printf '%s\n' "$NATIVE_INFO" | sed -n 's/^events: \([0-9]*\)$/\1/p')"
native_threads="$(printf '%s\n' "$NATIVE_INFO" | sed -n 's/^threads: \([0-9]*\)$/\1/p')"
native_program="$(printf '%s\n' "$NATIVE_INFO" | sed -n 's/^program: \(.*\)$/\1/p')"
[[ -n "$native_events"  && "$native_events"  -gt 0 ]] || check_fail "(B) recording has no events (events='$native_events')"
[[ -n "$native_threads" && "$native_threads" -gt 1 ]] || check_fail "(B) recording captured <= 1 thread (threads='$native_threads')"
[[ "$native_program" == "$BIN" ]] || check_fail "(B) recording names program '$native_program', expected '$BIN'"
log "(B) recording: $native_events events, $native_threads threads, $(stat -c%s "$NATIVE_CT") bytes"

# --- (C) what the trace does and does not carry about the patch ------------
# POSITIVE CONTROL FIRST. An event dump that failed would produce an empty file,
# and an empty file satisfies "contains no CodePatchEvent" for free. Prove the
# dump is real before reading anything out of its absences.
"$CT_MCR" trace events "$NATIVE_CT" > "$OUT/trace-events.txt" 2>"$OUT/trace-events.err"
EVENTS_RC=$?
if [[ "$EVENTS_RC" -ne 0 ]]; then
	check_fail "(C) 'ct-mcr trace events' failed (rc=$EVENTS_RC): $(head -5 "$OUT/trace-events.err")"
else
	EVENT_LINES="$(wc -l < "$OUT/trace-events.txt")"
	if [[ "$EVENT_LINES" -eq 0 ]]; then
		check_fail "(C) the event dump is EMPTY -- a reader failure, so its absences mean nothing"
	elif [[ "$EVENT_LINES" -ne "$native_events" ]]; then
		# "Non-empty" is too weak to license reading an ABSENCE out of this file.
		# A dump that stopped early, or that printed a summary instead of the
		# stream, is non-empty and would still satisfy a bare grep -- the same
		# vacuous shape as an empty one, just harder to see. The dump is one line
		# per event, so it must account for EVERY event `trace info` reported.
		check_fail "(C) the event dump has $EVENT_LINES lines but the trace reports $native_events events; the dump is INCOMPLETE, so its absences mean nothing"
	else
		log "(C) event dump is real AND complete: $EVENT_LINES lines == $native_events reported events"
		# HLX-M7 landed the CodePatchEvent, so this flipped from "report what
		# is absent" to "require what must be present". The full field-by-field
		# verification -- every digest recomputed from independently obtained
		# bytes, and the differential against an unpatched control INVERTED --
		# is `scripts/record-and-verify-hcr-m7.sh`, which calls this script for
		# its patched arm. What is asserted HERE is only what this script can
		# see on its own: the event exists, exactly once.
		CODE_PATCH_LINES="$(grep -c " type=evCodePatch " "$OUT/trace-events.txt" || true)"
		if [[ "$CODE_PATCH_LINES" -ne 1 ]]; then
			check_fail "(C) the trace carries $CODE_PATCH_LINES evCodePatch events, expected exactly 1. Without it a reader cannot tell that the process's text changed mid-recording, and a replay past that point would reproduce the OLD code's semantics against the NEW code's events."
		else
			log "(C) the trace carries the CodePatchEvent:"
			grep " type=evCodePatch " "$OUT/trace-events.txt" | head -1
		fi
	fi
fi

log "artifacts in $OUT"
if [[ "$fail_count" -ne 0 ]]; then
	log "RESULT: $fail_count check(s) FAILED"
	exit 1
fi
log "RESULT: PASS — the engine was hot-patched while MCR was recording it, and both survived"
