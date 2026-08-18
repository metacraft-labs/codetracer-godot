#!/usr/bin/env bash
# CodeTracer GDScript recorder — GF12 (Threads: Thread/Mutex/Semaphore/
# WorkerThreadPool + writer thread-safety) record-and-verify runner.
#
# GF12 DOES change the recorder (writer thread-safety + thread attribution), so
# the default BUILD is `auto` (rebuild if a recorder source is newer than the
# binary). Records the GF12 reference program
# (test-programs/gdscript/gf_threads.gd) with the patched engine, decodes its .ct
# with `ct-print --full`, and asserts the hand-derived facts in
# scripts/EXPECTED-GF12.md via scripts/verify_gf12.py:
#   - add_one called EXACTLY 8x (5 worker + 3 pool) — deterministic per-thread
#     work — with worker/pool_task on DISTINCT non-main threads (thread-id
#     distinctness); main frames on thread 1; ≥2 distinct thread ids + ≥1
#     ThreadStart; the Mutex-guarded counter captured coherently (final 8);
#     worker-thread add_one `r` values captured correctly; well-formed .ct.
#
# It runs the recording NRUNS times (default 5) to shake out races — every run
# must record without a crash/hang and pass the verifier. It then PROVES the
# verifier has teeth with tamper runs (wrong add_one count / missing worker frame
# / thread-ids collapsed to main), each of which MUST be rejected.
#
# Finally it re-runs the GF11..G2 regressions (recorder must stay byte-identical
# for single-threaded programs — the thread-safety guard is inert when only one
# thread ever emits).
#
# EXITS NONZERO on any crash, hang, mismatch, or corrupted trace.
#
# Usage:
#   scripts/record-and-verify-gf12.sh               # BUILD=auto, NRUNS=5
#   BUILD=always scripts/record-and-verify-gf12.sh  # force a rebuild first
#   NRUNS=10 scripts/record-and-verify-gf12.sh      # more race-shakeout runs
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

PLATFORM="${PLATFORM:-macos}"
ARCH="${ARCH:-arm64}"
TARGET="${TARGET:-template_debug}"
BIN="$REPO/bin/godot.${PLATFORM}.${TARGET}.${ARCH}"
CT_PRINT="${CT_PRINT:-$REPO/../codetracer-trace-format-nim/ct-print}"
BUILD="${BUILD:-auto}"
NRUNS="${NRUNS:-5}"
RUN_TIMEOUT="${RUN_TIMEOUT:-60}"

log()  { printf '\n=== %s ===\n' "$*"; }
die()  { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

# --- 0. tooling ------------------------------------------------------------
[[ -x "$CT_PRINT" ]] || die "ct-print not found/executable at $CT_PRINT"
command -v python3 >/dev/null || die "python3 not found"
# `timeout` (coreutils) — used to catch a hang as a hard failure.
TIMEOUT_BIN="$(command -v timeout || command -v gtimeout || true)"

# --- 1. build the patched engine if needed (GF12 changes the recorder) -----
need_build=0
case "$BUILD" in
	always) need_build=1 ;;
	never)  need_build=0 ;;
	auto)
		if [[ ! -x "$BIN" ]]; then
			need_build=1
		else
			for f in modules/gdscript/gdscript_ct_trace.cpp \
			         modules/gdscript/gdscript_ct_trace.h \
			         modules/gdscript/gdscript_vm.cpp \
			         modules/gdscript/gdscript.cpp \
			         modules/gdscript/SCsub; do
				if [[ "$f" -nt "$BIN" ]]; then need_build=1; fi
			done
		fi ;;
	*) die "unknown BUILD=$BUILD (auto|always|never)" ;;
esac

if [[ "$need_build" == 1 ]]; then
	log "building patched engine ($PLATFORM $TARGET $ARCH) via nix develop -c scons"
	nix develop -c bash -c '
		set -euo pipefail
		vars=$(env | sed -n "s/^\(NIX[A-Za-z0-9_]*\)=.*/\1/p" | paste -sd, -)
		exec scons -j"$(sysctl -n hw.ncpu 2>/dev/null || nproc)" \
			platform='"$PLATFORM"' target='"$TARGET"' arch='"$ARCH"' \
			module_gdscript_enabled=yes \
			vulkan=no metal=no opengl3=no \
			disable_path_overrides=no \
			import_env_vars="$vars"
	'
fi
[[ -x "$BIN" ]] || die "engine binary missing at $BIN (BUILD=$BUILD)"
log "engine: $BIN"

# --- 2. work dir -----------------------------------------------------------
WORK="$(mktemp -d "${TMPDIR:-/tmp}/ct-gf12.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

# record gf_threads.gd once into <run> -> echoes "<full.json>\t<stdout_log>"
record_run() {
	local run="$1"
	local proj="$WORK/run$run"
	local trace_dir="$proj/trace"
	mkdir -p "$proj" "$trace_dir"
	cp "$REPO/test-programs/gdscript/gf_threads.gd" "$proj/gf_threads.gd"
	cat > "$proj/project.godot" <<-EOF
		config_version=5
		[application]
		config/name="ctgf12-$run"
	EOF
	local stdout_log="$proj/stdout.log"
	local rc=0
	if [[ -n "$TIMEOUT_BIN" ]]; then
		CT_GDSCRIPT_TRACE="$trace_dir" "$TIMEOUT_BIN" "$RUN_TIMEOUT" \
			"$BIN" --headless --path "$proj" --script "res://gf_threads.gd" \
			>"$stdout_log" 2>&1 || rc=$?
	else
		CT_GDSCRIPT_TRACE="$trace_dir" \
			"$BIN" --headless --path "$proj" --script "res://gf_threads.gd" \
			>"$stdout_log" 2>&1 || rc=$?
	fi
	[[ "$rc" == 124 ]] && die "run$run HANG (engine did not exit within ${RUN_TIMEOUT}s — a deadlock?)"
	[[ "$rc" == 0 ]]   || die "run$run CRASHED (engine exit=$rc — a data race?): $(tail -3 "$stdout_log")"
	local ct="$trace_dir/gdscript_trace.ct"
	[[ -f "$ct" ]] || die "run$run produced no trace at $ct"
	local full="$proj/full.json"
	"$CT_PRINT" --full "$ct" > "$full" || die "run$run: ct-print --full failed (corrupt .ct?)"
	printf '%s\t%s\n' "$full" "$stdout_log"
}

# --- 3. GF12 deliverable: threaded recording, NRUNS times ------------------
log "recording gf_threads.gd x$NRUNS (race shakeout; each MUST record + verify)"
FIRST_FULL=""
for run in $(seq 1 "$NRUNS"); do
	IFS=$'\t' read -r FULL OUT < <(record_run "$run")
	[[ -z "$FIRST_FULL" ]] && FIRST_FULL="$FULL"
	grep -qF "CT_GF12_RESULT=8" "$OUT" || die "run$run: stdout missing 'CT_GF12_RESULT=8' (counter not coherent)"
	python3 "$REPO/scripts/verify_gf12.py" verify "$FULL" \
		|| die "run$run: verify_gf12.py assertions failed"
done
log "all $NRUNS threaded runs recorded cleanly (no crash/hang) and verified — stable"

# --- 4. prove the verifier has teeth (tamper runs MUST be rejected) --------
log "tamper runs (each MUST be rejected by verify_gf12.py)"
for mode in wrongaddone missingworker threadidmain workerwork; do
	python3 "$REPO/scripts/verify_gf12.py" tamper "$FIRST_FULL" "$mode" \
		|| die "tamper($mode) was NOT rejected — verifier is vacuous"
done

# --- 5. regression: GF11..G2 (single-thread byte-identical) ----------------
log "regression: GF11..G2 via record-and-verify-gf11.sh (BUILD=never)"
BUILD=never "$REPO/scripts/record-and-verify-gf11.sh"

log "ALL GF12 (x$NRUNS) + tamper + GF11..G2-regression checks PASSED"
