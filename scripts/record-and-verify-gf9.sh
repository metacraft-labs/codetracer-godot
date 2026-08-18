#!/usr/bin/env bash
# CodeTracer GDScript recorder — GF9 (Signals: declare / emit / connect /
# disconnect) record-and-verify runner.
#
# Records the GF9 reference program (test-programs/gdscript/gf_signals.gd) with
# the patched engine, decodes its .ct with ct-print --full, and asserts the
# hand-derived facts in scripts/EXPECTED-GF9.md via scripts/verify_gf9.py:
#   - a connected handler runs as a call FRAME on emit, with the emitted args
#     captured (via read-into-local) and its return recorded;
#   - BOTH handlers run on a single emit after two connects (in connect order);
#   - after disconnect, the disconnected handler's frame is ABSENT on the next
#     emit while the still-connected handler's frame is present (the teeth);
#   - an emit with no connections produces no handler frame.
#
# GF9 is a COVERAGE milestone: a signal handler is an ordinary method dispatched
# synchronously through GDScriptFunction::call, so the existing G2/G3/G4/GF5/GF8
# hooks capture it with NO engine change. The default BUILD is therefore `never`
# (fail loudly if the binary is missing rather than silently rebuild); pass
# BUILD=auto/always to (re)build.
#
# Then it PROVES the verifier has teeth with tamper runs (wrong emitted-arg value
# / disconnected handler reappearing / dropped handler frame / wrong return
# value), each of which MUST be rejected.
#
# Finally it re-runs the GF8..G2 regressions to confirm the recorder is
# unperturbed.
#
# EXITS NONZERO on any mismatch. This is a real automated test, not a print.
#
# Usage:
#   scripts/record-and-verify-gf9.sh              # BUILD=never (default)
#   BUILD=auto  scripts/record-and-verify-gf9.sh  # rebuild if a source is newer
#   BUILD=always scripts/record-and-verify-gf9.sh # force a rebuild first
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

# --- 1. build the patched engine if needed (GF9 adds no recorder code) ------
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
WORK="$(mktemp -d "${TMPDIR:-/tmp}/ct-gf9.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

# record <entry.gd>  -> echoes "<full.json>\t<stdout_log>"
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
		config/name="ctgf9-$name"
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

# record a multi-file project (for the GF7 regression)
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
		config/name="ctgf9-$name"
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

# --- 3. GF9 deliverable: signals emit/connect/disconnect --------------------
log "recording gf_signals.gd (GF9 signals emit/connect/disconnect)"
IFS=$'\t' read -r GF9_FULL GF9_OUT < <(record gf_signals.gd)
cat "$GF9_OUT"
grep -qF "CT_GF9_RESULT=100" "$GF9_OUT" || die "gf_signals.gd: stdout missing 'CT_GF9_RESULT=100'"

python3 "$REPO/scripts/verify_gf9.py" verify "$GF9_FULL" \
	|| die "gf_signals.gd: verify_gf9.py assertions failed"

# --- 4. prove the verifier has teeth (tamper runs MUST be rejected) --------
log "tamper runs (each MUST be rejected by verify_gf9.py)"
for mode in argvalue disconnected dropframe retvalue; do
	python3 "$REPO/scripts/verify_gf9.py" tamper "$GF9_FULL" "$mode" \
		|| die "tamper($mode) was NOT rejected — verifier is vacuous"
done

# --- 5. regression: GF8 + GF7 + GF6 + GF5 + GF4 + GF3 + GF2 + GF1 + G4+G3+G2 -
log "regression: GF8 + GF7 (multi-file) + GF6 + GF5 + GF4 + GF3 + GF2 + GF1 + G4 + G3 + G2"

IFS=$'\t' read -r GF8_FULL GF8_OUT < <(record gf_props.gd)
grep -qF "CT_GF8_RESULT=208" "$GF8_OUT" || die "gf_props.gd: stdout missing 'CT_GF8_RESULT=208'"
grep -qF "CT_GF8_TOTAL=7" "$GF8_OUT" || die "gf_props.gd: stdout missing 'CT_GF8_TOTAL=7'"
python3 "$REPO/scripts/verify_gf8.py" verify "$GF8_FULL" \
	|| die "gf_props.gd: GF8 regression FAILED"

IFS=$'\t' read -r GF7_FULL GF7_OUT < <(record_multi gf_zoo.gd gf_animal.gd gf_dog.gd)
grep -qF "CT_GF7_RESULT=20" "$GF7_OUT" || die "gf_zoo.gd: stdout missing 'CT_GF7_RESULT=20'"
python3 "$REPO/scripts/verify_gf7.py" verify "$GF7_FULL" \
	|| die "gf_zoo.gd: GF7 regression FAILED"

IFS=$'\t' read -r GF6_FULL GF6_OUT < <(record gf_lambdas.gd)
grep -qF "CT_GF6_RESULT=379" "$GF6_OUT" || die "gf_lambdas.gd: stdout missing 'CT_GF6_RESULT=379'"
python3 "$REPO/scripts/verify_gf6.py" verify "$GF6_FULL" \
	|| die "gf_lambdas.gd: GF6 regression FAILED"

IFS=$'\t' read -r GF5_FULL GF5_OUT < <(record gf_functions.gd)
grep -qF "CT_GF5_RESULT=120" "$GF5_OUT" || die "gf_functions.gd: stdout missing 'CT_GF5_RESULT=120'"
python3 "$REPO/scripts/verify_gf5.py" verify "$GF5_FULL" \
	|| die "gf_functions.gd: GF5 regression FAILED"

IFS=$'\t' read -r GF4_FULL GF4_OUT < <(record gf_variant_types.gd)
grep -qF "CT_GF4_RESULT=129" "$GF4_OUT" || die "gf_variant_types.gd: stdout missing 'CT_GF4_RESULT=129'"
python3 "$REPO/scripts/verify_gf4.py" verify "$GF4_FULL" \
	|| die "gf_variant_types.gd: GF4 regression FAILED"

IFS=$'\t' read -r GF3_FULL GF3_OUT < <(record gf_collections.gd)
grep -qF "CT_GF3_RESULT=476" "$GF3_OUT" || die "gf_collections.gd: stdout missing 'CT_GF3_RESULT=476'"
python3 "$REPO/scripts/verify_gf3.py" verify "$GF3_FULL" \
	|| die "gf_collections.gd: GF3 regression FAILED"

IFS=$'\t' read -r GF2_FULL GF2_OUT < <(record gf_control_flow.gd)
grep -qF "CT_GF2_RESULT=281" "$GF2_OUT" || die "gf_control_flow.gd: stdout missing 'CT_GF2_RESULT=281'"
python3 "$REPO/scripts/verify_gf2.py" verify "$GF2_FULL" \
	|| die "gf_control_flow.gd: GF2 regression FAILED"

IFS=$'\t' read -r GF1_FULL GF1_OUT < <(record gf_typing.gd)
grep -qF "CT_GF1_RESULT=1222" "$GF1_OUT" || die "gf_typing.gd: stdout missing 'CT_GF1_RESULT=1222'"
python3 "$REPO/scripts/verify_gf1.py" verify "$GF1_FULL" \
	|| die "gf_typing.gd: GF1 regression FAILED"

IFS=$'\t' read -r G4_FULL G4_OUT < <(record gf_values.gd)
grep -qF "CT_G4_RESULT=47" "$G4_OUT" || die "gf_values.gd: stdout missing 'CT_G4_RESULT=47'"
python3 "$REPO/scripts/verify_g4.py" verify "$G4_FULL" \
	|| die "gf_values.gd: G4 regression FAILED"

IFS=$'\t' read -r G3_FULL G3_OUT < <(record gf_calls.gd)
grep -qF "CT_G3_RESULT=107" "$G3_OUT" || die "gf_calls.gd: stdout missing 'CT_G3_RESULT=107'"
python3 "$REPO/scripts/verify_g3.py" g3 "$G3_FULL" \
	|| die "gf_calls.gd: G3 regression FAILED (nesting/pairing perturbed?)"

IFS=$'\t' read -r G2_FULL G2_OUT < <(record g2probe.gd)
grep -qF "CT_G2_STEPS=30" "$G2_OUT" || die "g2probe.gd: stdout missing 'CT_G2_STEPS=30'"
python3 "$REPO/scripts/verify_g3.py" g2 "$G2_FULL" \
	|| die "g2probe.gd: G2 regression FAILED"

log "ALL GF9 + tamper + GF8/GF7/GF6/GF5/GF4/GF3/GF2/GF1/G4/G3/G2-regression checks PASSED"
