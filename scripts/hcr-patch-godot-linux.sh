#!/usr/bin/env bash
# CodeTracer HCR demo — patch a REAL Godot engine function in a LIVE engine.
#
# Runs the patchable engine over a GDScript program that prints the value of a
# native engine function once per tick, and publishes a direct entry patch into
# that function part-way through the run. The observable is the engine's own
# stdout: the printed value changes, in one process, with no restart.
#
# MODE=demo    (default) target returns 4242 after the patch.
# MODE=threads target returns gettid() after the patch, and the program calls it
#              from four engine `Thread`s plus the main thread, so the set of
#              distinct printed values IS the set of kernel threads that
#              executed the patched body. See test-programs/hcr1/*.gd.
#
# Everything is real: the real patched engine, the real in-target C agent, the
# real Unix-socket wire protocol, the real coordinator client, and patch bytes
# read out of a real relocatable object produced by a real gcc invocation.
# Nothing is mocked and no check is disabled to make a run pass.
#
# A REFUSAL IS A RESULT. If the agent declines the patch, this script reports
# the named diagnostic verbatim and exits non-zero WITHOUT retrying, relaxing
# anything, or reporting a pass. That is the measurement the campaign wants.
#
# Usage: scripts/hcr-patch-godot-linux.sh [<output-dir>]
# Environment: BIN, REPROBUILD_DIR, MODE, PATCH_AFTER, MARKER_TIMEOUT_MS, TARGET_SYMBOL
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPROBUILD_DIR="${REPROBUILD_DIR:-$(cd "$REPO/.." && pwd)/reprobuild}"
BIN="${BIN:-$REPO/bin/godot.linuxbsd.template_debug.x86_64.hcr}"
MODE="${MODE:-demo}"
MARKER_TIMEOUT_MS="${MARKER_TIMEOUT_MS:-180000}"
PROJ="$REPO/test-programs/hcr1"
OUT="${1:-${TMPDIR:-/tmp}/ct-hcr1-$$}"

log() { printf '[hcr1] %s\n' "$*"; }
die() { printf '[hcr1] FATAL: %s\n' "$*" >&2; exit 1; }
fail_count=0
check_fail() { printf '[hcr1] CHECK-FAIL: %s\n' "$*" >&2; fail_count=$((fail_count + 1)); }

# PATCH_AFTER is a marker the ENGINE ITSELF must print before the patch is
# published. It is not a timeout: the driver reads it out of the engine's own
# log, so the before-half of the observation is known to exist rather than
# assumed. This replaces the earlier fixed `--delay-ms`, which was a guess about
# how far the engine had got -- and a guess that measurably fails under
# `ct-mcr record`, where time-to-first-output goes from 0.09 s to 16.2 s on this
# host, so a delay tuned without the recorder patches before tick 1 and leaves
# nothing to compare against.
case "$MODE" in
	demo)
		ENTRY="res://hcr1_live_patch.gd"
		PATCH_SYMBOL="hcr1_patch_processor_count"
		PATCH_AFTER="${PATCH_AFTER:-CT_HCR_TICK=8 }"
		;;
	threads)
		ENTRY="res://hcr1_thread_reach.gd"
		PATCH_SYMBOL="hcr1_patch_gettid"
		PATCH_AFTER="${PATCH_AFTER:-worker=3 iteration=8 }"
		;;
	*) die "unknown MODE=$MODE (demo|threads)" ;;
esac

# --- prerequisites, loudly --------------------------------------------------
[[ -x "$BIN" ]] || die "patchable engine not built: $BIN (run scripts/build-hcr-patchable-linux.sh)"
[[ -f "$PROJ/project.godot" ]] || die "demo project missing: $PROJ/project.godot"
[[ -d "$REPROBUILD_DIR" ]] || die "reprobuild checkout not found: $REPROBUILD_DIR"
command -v gcc >/dev/null || die "gcc not on PATH"
command -v nm >/dev/null || die "nm not on PATH"
command -v python3 >/dev/null || die "python3 not on PATH"
mkdir -p "$OUT" || die "cannot create $OUT"

# The engine must be patchable-SHAPED before any of this means anything. A run
# against a stripped, sledless engine would produce a refusal that says nothing
# about the provider, so check first and stop.
log "checking the engine is patchable-shaped"
python3 "$REPO/scripts/verify_hcr_patchable.py" "$BIN" > "$OUT/patchable-shape.log" 2>&1 \
	|| { cat "$OUT/patchable-shape.log"; die "engine is not patchable-shaped; see $OUT/patchable-shape.log"; }
grep -q "^PATCHABLE-SHAPED" "$OUT/patchable-shape.log" \
	|| { cat "$OUT/patchable-shape.log"; die "shape verifier did not report PATCHABLE-SHAPED"; }

# --- /dev/shm hygiene -------------------------------------------------------
# MCR leaks one `ct_rb_*`/`ct_mcr_*` segment per recorded thread, ~30 per Godot
# run, and an accumulation has been measured to make later runs fail for
# unrelated reasons. Count before and after so a leak is visible, not inferred.
# Only segments whose OWNING PID IS DEAD are removed: a blanket
# `rm /dev/shm/ct_*` would silently destroy a concurrent recording belonging to
# another process on this machine.
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
SHM_BEFORE="$(shm_ct_count)"
log "/dev/shm ct_* segments before: $SHM_BEFORE"

# --- resolve the target symbol ---------------------------------------------
# Derived from the engine's own .symtab rather than typed in, and required to be
# UNIQUE: a name that matched two symbols would be resolved by HLX-M1's
# ambiguity rule, not by this script's intention.
TARGET_SYMBOL="${TARGET_SYMBOL:-}"
if [[ -z "$TARGET_SYMBOL" ]]; then
	mapfile -t candidates < <(nm --defined-only "$BIN" 2>/dev/null \
		| awk '$2 == "T" || $2 == "t" { print $3 }' \
		| grep -E '^_ZNK8CoreBind2OS19get_processor_countEv$' | sort -u)
	[[ "${#candidates[@]}" -eq 1 ]] \
		|| die "expected exactly one CoreBind::OS::get_processor_count symbol in $BIN, found ${#candidates[@]}: ${candidates[*]:-<none>}"
	TARGET_SYMBOL="${candidates[0]}"
fi
log "target symbol: $TARGET_SYMBOL"

# --- build the patch object -------------------------------------------------
PATCH_OBJ="$OUT/hcr1_patch_bodies.o"
gcc -c -O2 -ffunction-sections "$PROJ/hcr1_patch_bodies.c" -o "$PATCH_OBJ" \
	|| die "could not compile the patch bodies"
log "patch object: $PATCH_OBJ (symbol $PATCH_SYMBOL)"

# The expected post-patch value is DERIVED FROM THE OBJECT for MODE=demo, not
# written here, so the assertion cannot agree with the script while disagreeing
# with the bytes that were actually sent.
EXPECTED_AFTER=""
if [[ "$MODE" == "demo" ]]; then
	EXPECTED_AFTER="$(objdump -d --section=".text.$PATCH_SYMBOL" "$PATCH_OBJ" \
		| sed -n 's/.*mov *\$0x\([0-9a-f]*\),%eax.*/\1/p' | head -n1)"
	[[ -n "$EXPECTED_AFTER" ]] || die "could not read the immediate out of $PATCH_SYMBOL"
	EXPECTED_AFTER="$((16#$EXPECTED_AFTER))"
	log "patch body returns: $EXPECTED_AFTER (read from the object, not hardcoded)"
fi

# --- build the coordinator driver -------------------------------------------
DRIVER="$OUT/hcr_patch_driver"
log "building the coordinator driver"
(
	cd "$REPROBUILD_DIR"
	nix develop --no-write-lock-file --command \
		nim c -d:release --hints:off --warnings:off \
		--outdir:"$OUT" scripts/hcr_patch_driver.nim
) > "$OUT/driver-build.log" 2>&1 || { tail -30 "$OUT/driver-build.log"; die "could not build the driver"; }
[[ -x "$DRIVER" ]] || die "driver missing after build: $DRIVER"

# --- run --------------------------------------------------------------------
SOCK="$OUT/hcr1.sock"
RUN_LOG="$OUT/engine.log"
# The driver polls this file for the marker, so it must exist before the driver
# starts; otherwise the first few polls read a missing file rather than an empty
# one. Same observable either way, but this keeps the "file is missing" state
# out of the measurement entirely.
: > "$RUN_LOG"
DRIVER_LOG="$OUT/driver.log"
DRIVER_JSON="$OUT/patch-result.json"
THREADS_LOG="$OUT/thread-census.log"

log "starting the coordinator (listening on $SOCK, patching once the engine prints '$PATCH_AFTER')"
"$DRIVER" --socket "$SOCK" \
	--target-symbol "$TARGET_SYMBOL" \
	--patch-object "$PATCH_OBJ" \
	--patch-symbol "$PATCH_SYMBOL" \
	--patch-id "hcr1-godot-$MODE" \
	--wait-for "$RUN_LOG" --marker "$PATCH_AFTER" \
	--marker-timeout-ms "$MARKER_TIMEOUT_MS" \
	--json-out "$DRIVER_JSON" \
	> "$DRIVER_LOG" 2>&1 &
DRIVER_PID=$!

# Wait for the socket to exist before starting the engine; otherwise the agent
# finds nothing to connect to and silently runs unpatched, which would look like
# a refusal that never happened.
for _ in $(seq 1 100); do
	[[ -S "$SOCK" ]] && break
	sleep 0.1
done
[[ -S "$SOCK" ]] || { cat "$DRIVER_LOG"; die "driver never created $SOCK"; }

log "starting the engine on $ENTRY"
REPRO_HCR_AGENT_SOCKET="$SOCK" \
	"$BIN" --headless --path "$PROJ" --script "$ENTRY" \
	> "$RUN_LOG" 2>&1 &
ENGINE_PID=$!

# Thread census, sampled from /proc while the engine actually runs. This is the
# only honest way to answer "how many threads does headless Godot run" — the
# figure cannot be read off the binary.
(
	while kill -0 "$ENGINE_PID" 2>/dev/null; do
		n="$(ls /proc/$ENGINE_PID/task 2>/dev/null | wc -l)"
		[[ "$n" -gt 0 ]] && echo "$(date +%s.%N) $n"
		sleep 0.2
	done
) > "$THREADS_LOG" 2>/dev/null &
CENSUS_PID=$!

wait "$DRIVER_PID"; DRIVER_RC=$?
wait "$ENGINE_PID"; ENGINE_RC=$?
kill "$CENSUS_PID" 2>/dev/null; wait "$CENSUS_PID" 2>/dev/null

log "driver exit: $DRIVER_RC   engine exit: $ENGINE_RC"
cat "$DRIVER_LOG"

SHM_AFTER="$(shm_ct_count)"
log "/dev/shm ct_* segments after: $SHM_AFTER"
if [[ "$SHM_AFTER" -gt "$SHM_BEFORE" ]]; then
	log "this run leaked $((SHM_AFTER - SHM_BEFORE)) ct_* segment(s); cleaning the dead ones"
	shm_ct_clean_dead
fi

MAX_THREADS="$(awk '{ if ($2 > m) m = $2 } END { print m + 0 }' "$THREADS_LOG" 2>/dev/null)"
log "peak thread count of the engine process: ${MAX_THREADS:-unknown} (sampled $(wc -l < "$THREADS_LOG") times)"

# --- verdict ----------------------------------------------------------------
# The driver's exit code distinguishes applied(0) / refused(2) / driver
# failure(1). A refusal is reported and is a non-zero exit, but it is NOT a
# check failure of this script: it is the answer.
if [[ "$DRIVER_RC" -eq 2 ]]; then
	log "RESULT: the agent REFUSED the patch. Its own words:"
	python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); f=d.get("patchFailed",{}); print("  stage  :",f.get("stage")); print("  message:",f.get("message"))' "$DRIVER_JSON" 2>/dev/null \
		|| grep -i "refused" "$DRIVER_LOG"
	log "artifacts in $OUT"
	exit 2
fi
if [[ "$DRIVER_RC" -ne 0 ]]; then
	log "RESULT: the driver itself failed (rc=$DRIVER_RC) — this is NOT a refusal."
	log "artifacts in $OUT"
	exit 1
fi

[[ "$ENGINE_RC" -eq 0 ]] || check_fail "the engine exited $ENGINE_RC (a patched process that crashes is not a working patch); see $RUN_LOG"

if [[ "$MODE" == "demo" ]]; then
	HOST_CPUS="$(nproc)"
	python3 - "$RUN_LOG" "$HOST_CPUS" "$EXPECTED_AFTER" <<'PY'
import re, sys
log, host_cpus, expected_after = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
ticks = []
for line in open(log, errors="replace"):
    m = re.search(r"CT_HCR_TICK=(\d+) CT_HCR_VALUE=(-?\d+)", line)
    if m:
        ticks.append((int(m.group(1)), int(m.group(2))))
problems = []
# A reader that found nothing must fail, never answer.
if not ticks:
    problems.append("the engine printed NO CT_HCR_TICK lines at all -- this is a "
                    "reader/run failure, not evidence about patching")
else:
    before = [v for _, v in ticks if v != expected_after]
    after = [v for _, v in ticks if v == expected_after]
    if not before:
        problems.append("every tick already showed the patched value %d; the run "
                        "does not distinguish a hot patch from a build-time change"
                        % expected_after)
    if not after:
        problems.append("no tick showed the patched value %d -- the patch was "
                        "reported applied but is not observable" % expected_after)
    wrong_before = sorted({v for v in before if v != host_cpus})
    if wrong_before:
        problems.append("pre-patch ticks reported %s, expected the host processor "
                        "count %d" % (wrong_before, host_cpus))
    # The transition must happen ONCE and never go back: a value that flips to
    # the patched one and then back would mean the entry was not stable.
    seq = [v == expected_after for _, v in ticks]
    transitions = sum(1 for a, b in zip(seq, seq[1:]) if a != b)
    if transitions != 1:
        problems.append("the value changed %d times across %d ticks; a published "
                        "entry patch must change it exactly once" % (transitions, len(ticks)))
    first_patched = next((t for t, v in ticks if v == expected_after), None)
    print("[hcr1] %d ticks: %d before the patch (value=%d), %d after (value=%d); "
          "transition at tick %s" % (len(ticks), len(before), host_cpus,
                                     len(after), expected_after, first_patched))
for p in problems:
    print("[hcr1] CHECK-FAIL: " + p)
sys.exit(1 if problems else 0)
PY
	[[ $? -eq 0 ]] || fail_count=$((fail_count + 1))
else
	python3 - "$RUN_LOG" <<'PY'
import re, sys
log = sys.argv[1]
rows = []
for line in open(log, errors="replace"):
    m = re.search(r"CT_HCR_THREAD worker=(\S+) iteration=(\d+) value=(-?\d+)", line)
    if m:
        rows.append((m.group(1), int(m.group(2)), int(m.group(3))))
if not rows:
    print("[hcr1] CHECK-FAIL: the engine printed NO CT_HCR_THREAD lines -- reader/run failure")
    sys.exit(1)
# Pre-patch values are the host processor count (small); post-patch values are
# kernel thread ids (large, and distinct per thread). Split on plausibility
# rather than on a hardcoded boundary: a tid is always > 1000 on Linux after
# boot, and no machine here has that many cores.
tids = sorted({v for _, _, v in rows if v > 1000})
pre = sorted({v for _, _, v in rows if v <= 1000})
by_worker = {}
for worker, _, v in rows:
    if v > 1000:
        by_worker.setdefault(worker, set()).add(v)
print("[hcr1] %d observations; pre-patch value(s) %s; %d DISTINCT kernel thread(s) "
      "executed the patched body: %s" % (len(rows), pre, len(tids), tids))
for worker in sorted(by_worker):
    print("[hcr1]   worker=%-5s -> tid(s) %s" % (worker, sorted(by_worker[worker])))
if not tids:
    print("[hcr1] CHECK-FAIL: no observation shows a kernel thread id -- the "
          "gettid patch never took effect")
    sys.exit(1)
sys.exit(0)
PY
	[[ $? -eq 0 ]] || fail_count=$((fail_count + 1))
fi

log "artifacts in $OUT"
if [[ "$fail_count" -ne 0 ]]; then
	log "RESULT: $fail_count check(s) FAILED"
	exit 1
fi
log "RESULT: PASS — a real Godot engine function was replaced in a live engine"
