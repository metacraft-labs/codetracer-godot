#!/usr/bin/env bash
# CodeTracer GDScript recorder — GF11 (Node lifecycle callbacks)
# record-and-verify runner.
#
# Records the GF11 reference program (test-programs/gdscript/gf_node.gd, driven
# by gf_node_main.gd) with the patched engine, decodes its .ct with
# `ct-print --full`, and asserts the hand-derived facts in
# scripts/EXPECTED-GF11.md via scripts/verify_gf11.py:
#   - every engine-invoked lifecycle callback is recorded as a NODE frame
#     (_init/_enter_tree/_ready/_process/_physics_process/_notification/
#     _exit_tree), attributed to gf_node.gd by the source path of its steps
#     (the SceneTree driver also defines `_process`);
#   - _init/_enter_tree/_ready/_exit_tree fire exactly once; _process fires
#     EXACTLY 3 times (deterministic quit trigger); _physics_process >= 1;
#   - each _process frame captures a Float `delta`; each _physics_process frame
#     captures `pd == 1/60` (fixed timestep); _notification captures the Int
#     `what` and the KEY codes PARENTED(18)<ENTER_TREE(10)<READY(13)<tick(16/17)
#     <EXIT_TREE(11) appear in that order;
#   - the callbacks are ordered _init<_enter_tree<_ready<_process<_exit_tree.
#
# GF11 is a COVERAGE milestone: a lifecycle callback is an ordinary method
# dispatched (by the SceneTree, not GDScript source) through
# GDScriptFunction::call, so the existing G2/G3/G4 hooks record it with NO engine
# change. The default BUILD is therefore `never` (fail loudly if the binary is
# missing rather than silently rebuild); pass BUILD=auto/always to (re)build.
#
# Then it PROVES the verifier has teeth with tamper runs (missing callback /
# wrong _process count / wrong notification code / wrong order), each of which
# MUST be rejected.
#
# Finally it re-runs the GF10..G2 regressions (via record-and-verify-gf10.sh with
# BUILD=never) to confirm the recorder is unperturbed.
#
# EXITS NONZERO on any mismatch. This is a real automated test, not a print.
#
# Usage:
#   scripts/record-and-verify-gf11.sh              # BUILD=never (default)
#   BUILD=auto  scripts/record-and-verify-gf11.sh  # rebuild if a source is newer
#   BUILD=always scripts/record-and-verify-gf11.sh # force a rebuild first
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

PLATFORM="${PLATFORM:-macos}"
ARCH="${ARCH:-arm64}"
TARGET="${TARGET:-template_debug}"
BIN="$REPO/bin/godot.${PLATFORM}.${TARGET}.${ARCH}"
CT_PRINT="${CT_PRINT:-$REPO/../codetracer-trace-format-nim/ct-print}"
BUILD="${BUILD:-never}"

log()  { printf '\n=== %s ===\n' "$*"; }
die()  { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

# --- 0. tooling ------------------------------------------------------------
[[ -x "$CT_PRINT" ]] || die "ct-print not found/executable at $CT_PRINT"
command -v python3 >/dev/null || die "python3 not found"

# --- 1. build the patched engine if needed (GF11 adds no recorder code) -----
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
WORK="$(mktemp -d "${TMPDIR:-/tmp}/ct-gf11.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

# record a multi-file project.  record_multi <entry.gd> <extra.gd>...
#   -> echoes "<full.json>\t<stdout_log>"
record_multi() {
	local entry="$1"; shift
	local name proj trace_dir full stdout_log extra
	name="$(basename "$entry" .gd)"
	proj="$WORK/$name"
	trace_dir="$proj/trace"
	mkdir -p "$proj" "$trace_dir"
	cp "$REPO/test-programs/gdscript/$entry" "$proj/$entry"
	for extra in "$@"; do cp "$REPO/test-programs/gdscript/$extra" "$proj/$extra"; done
	cat > "$proj/project.godot" <<-EOF
		config_version=5
		[application]
		config/name="ctgf11-$name"
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

# --- 3. GF11 deliverable: node lifecycle callbacks --------------------------
log "recording gf_node.gd via gf_node_main.gd (GF11 node lifecycle callbacks)"
IFS=$'\t' read -r GF11_FULL GF11_OUT < <(record_multi gf_node_main.gd gf_node.gd)
grep -E "^CT_GF11" "$GF11_OUT" || true
grep -qF "CT_GF11_PROC=3" "$GF11_OUT"   || die "gf_node: stdout missing 'CT_GF11_PROC=3'"
grep -qF "CT_GF11_RESULT=7" "$GF11_OUT" || die "gf_node: stdout missing 'CT_GF11_RESULT=7'"

python3 "$REPO/scripts/verify_gf11.py" verify "$GF11_FULL" \
	|| die "gf_node: verify_gf11.py assertions failed"

# --- 4. prove the verifier has teeth (tamper runs MUST be rejected) --------
log "tamper runs (each MUST be rejected by verify_gf11.py)"
for mode in missingcallback proccount notifcode order; do
	python3 "$REPO/scripts/verify_gf11.py" tamper "$GF11_FULL" "$mode" \
		|| die "tamper($mode) was NOT rejected — verifier is vacuous"
done

# --- 5. regression: GF10..G2 (recorder unperturbed) ------------------------
log "regression: GF10..G2 via record-and-verify-gf10.sh (BUILD=never)"
BUILD=never "$REPO/scripts/record-and-verify-gf10.sh"

log "ALL GF11 + tamper + GF10..G2-regression checks PASSED"
