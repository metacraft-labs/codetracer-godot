#!/usr/bin/env bash
# CodeTracer GDScript recorder — GF10 (Coroutines & `await`, async-continuation
# integration) record-and-verify runner.
#
# Records the GF10 reference program (test-programs/gdscript/gf_coroutine.gd)
# with the patched engine, decodes its .ct with ct-print --full, and asserts the
# hand-derived facts in scripts/EXPECTED-GF10.md via scripts/verify_gf10.py:
#   - the engine emits 2 suspend + 2 resume async markers (one pair per await);
#   - the coroutine `work` and `_initialize` each record as TWO BALANCED frames
#     across their yield (open question #4);
#   - the pre-await local `base` survives the suspension (kept==10 on resume);
#   - the join value r==42 / check==42.
#
# UNLIKE the GF1/GF2/GF6/GF7/GF9 COVERAGE milestones, GF10 CHANGES the recorder
# (two new VM hooks: OPCODE_AWAIT suspend + OPCODE_AWAIT_RESUME resume markers),
# so the default BUILD is `auto` (rebuild if a source file is newer than the
# binary). Pass BUILD=never to fail loudly if the binary is stale.
#
# The AUTHORITATIVE proof that the markers pair into a CodeTracer
# ContinuationLink (registration/continuation step ids, link_type await) lives in
# the db-backend integration test
# `codetracer/src/db-backend/tests/verify_gdscript_await_continuation_link.rs`
# (it reads the marker METADATA that ct-print does not surface), opened via the
# real MaterializedReplaySession — the same pattern as the G5 test.
#
# Then it PROVES the verifier has teeth with tamper runs, and re-runs the
# GF9..G2 regressions (via record-and-verify-gf9.sh) to confirm the recorder is
# unperturbed for non-await programs (the new hooks fire only inside `await`).
#
# EXITS NONZERO on any mismatch.
#
# Usage:
#   scripts/record-and-verify-gf10.sh              # BUILD=auto (default)
#   BUILD=never  scripts/record-and-verify-gf10.sh # do not rebuild
#   BUILD=always scripts/record-and-verify-gf10.sh # force a rebuild first
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

PLATFORM="${PLATFORM:-macos}"
ARCH="${ARCH:-arm64}"
TARGET="${TARGET:-template_debug}"
BIN="$REPO/bin/godot.${PLATFORM}.${TARGET}.${ARCH}"
CT_PRINT="${CT_PRINT:-$REPO/../codetracer-trace-format-nim/ct-print}"
BUILD="${BUILD:-auto}"

log()  { printf '\n=== %s ===\n' "$*"; }
die()  { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

[[ -x "$CT_PRINT" ]] || die "ct-print not found/executable at $CT_PRINT"
command -v python3 >/dev/null || die "python3 not found"

# --- build the patched engine if needed (GF10 adds new recorder code) --------
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

# --- work dir --------------------------------------------------------------
WORK="$(mktemp -d "${TMPDIR:-/tmp}/ct-gf10.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

record() {
	local entry="$1"
	local name proj trace_dir full stdout_log
	name="$(basename "$entry" .gd)"
	proj="$WORK/$name"
	trace_dir="$proj/trace"
	mkdir -p "$proj" "$trace_dir"
	cp "$REPO/test-programs/gdscript/$entry" "$proj/$entry"
	cat > "$proj/project.godot" <<-EOF
		config_version=5
		[application]
		config/name="ctgf10-$name"
	EOF
	stdout_log="$WORK/$name.stdout"
	CT_GDSCRIPT_TRACE="$trace_dir" \
		"$BIN" --headless --path "$proj" --script "res://$entry" \
		>"$stdout_log" 2>&1 || true
	local ct="$trace_dir/gdscript_trace.ct"
	[[ -f "$ct" ]] || die "no trace produced at $ct"
	full="$WORK/$name.full.json"
	"$CT_PRINT" --full "$ct" > "$full" || die "ct-print --full failed on $ct"
	printf '%s\t%s\n' "$full" "$stdout_log"
}

# --- GF10 deliverable: coroutines & await ----------------------------------
log "recording gf_coroutine.gd (GF10 coroutines & await)"
IFS=$'\t' read -r GF10_FULL GF10_OUT < <(record gf_coroutine.gd)
cat "$GF10_OUT"
grep -qF "CT_GF10_RESULT=42" "$GF10_OUT" || die "gf_coroutine.gd: stdout missing 'CT_GF10_RESULT=42'"

python3 "$REPO/scripts/verify_gf10.py" verify "$GF10_FULL" \
	|| die "gf_coroutine.gd: verify_gf10.py assertions failed"

# --- prove the verifier has teeth ------------------------------------------
log "tamper runs (each MUST be rejected by verify_gf10.py)"
for mode in markers surviving joinvalue balance; do
	python3 "$REPO/scripts/verify_gf10.py" tamper "$GF10_FULL" "$mode" \
		|| die "tamper($mode) was NOT rejected — verifier is vacuous"
done

# --- regression: GF9..G2 (recorder unperturbed for non-await programs) ------
log "regression: GF9..G2 via record-and-verify-gf9.sh (BUILD=never)"
BUILD=never "$REPO/scripts/record-and-verify-gf9.sh"

log "ALL GF10 + tamper + GF9..G2-regression checks PASSED"
