#!/usr/bin/env bash
# CodeTracer GDScript recorder — GF8 (Properties get/set & Annotations
# @export/@onready, incl. MEMBER-write capture) record-and-verify runner.
#
# Records the GF8 reference program (test-programs/gdscript/gf_props.gd) with the
# patched engine, decodes its .ct with ct-print --full, and asserts the
# hand-derived facts in scripts/EXPECTED-GF8.md via scripts/verify_gf8.py:
#   - class MEMBER writes captured by NAME on their step (hp/level @export
#     defaults; _t backing-field init; x plain member; total static var 0->7;
#     ready_mark @onready; got/_t inside the property setter);
#   - the property get/set accessors record as FRAMES (@temp_getter / @temp_setter)
#     children of `run`, with the getter's `return _t` == 100.0 captured (GF5);
#   - the setter's backing member write `_t = 100.0` is captured INSIDE the
#     setter frame.
#
# GF8 IS an engine change (member/static-var write capture in gdscript_ct_trace
# + three hooks in gdscript_vm.cpp), so this runner rebuilds when a recorder
# source is newer than the binary.
#
# Then it PROVES the verifier has teeth with tamper runs (wrong member value /
# wrong member name / missing setter frame), each of which MUST be rejected.
#
# Finally it re-runs the GF7..G2 regressions (GF7 is multi-file) to confirm the
# value path still captures the SAME stack-slot values and the step/call streams
# are unchanged. GF8 newly captures the members GF1 (`counter`) and GF7
# (`species`) had deferred; those two verifiers' member-ABSENCE expectations were
# updated minimally (now asserted PRESENT) — see EXPECTED-GF8.md.
#
# EXITS NONZERO on any mismatch. This is a real automated test, not a print.
#
# Usage:
#   scripts/record-and-verify-gf8.sh              # build-if-needed, then verify
#   BUILD=never scripts/record-and-verify-gf8.sh  # fail if binary missing
#   BUILD=always scripts/record-and-verify-gf8.sh # force a rebuild first
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

# --- 0. tooling ------------------------------------------------------------
[[ -x "$CT_PRINT" ]] || die "ct-print not found/executable at $CT_PRINT"
command -v python3 >/dev/null || die "python3 not found"

# --- 1. build the patched engine if needed ---------------------------------
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
WORK="$(mktemp -d "${TMPDIR:-/tmp}/ct-gf8.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

# record_multi <entry.gd> <extra.gd>...  -> echoes "<full.json>\t<stdout_log>"
record_multi() {
	local entry="$1"; shift
	local name proj trace_dir full stdout_log
	name="$(basename "$entry" .gd)"
	proj="$WORK/$name"
	trace_dir="$proj/trace"
	mkdir -p "$proj" "$trace_dir"
	cp "$REPO/test-programs/gdscript/$entry" "$proj/$entry"
	local extra
	for extra in "$@"; do
		cp "$REPO/test-programs/gdscript/$extra" "$proj/$extra"
	done
	cat > "$proj/project.godot" <<-EOF
		config_version=5
		[application]
		config/name="ctgf8-$name"
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

record() { record_multi "$1"; }  # single-file (regression) record

# --- 3. GF8 deliverable: properties get/set + @export/@onready + member writes
log "recording gf_props.gd (GF8 properties/annotations/member-write capture)"
IFS=$'\t' read -r GF8_FULL GF8_OUT < <(record gf_props.gd)
cat "$GF8_OUT"
grep -qF "CT_GF8_RESULT=208" "$GF8_OUT" || die "gf_props.gd: stdout missing 'CT_GF8_RESULT=208'"
grep -qF "CT_GF8_TOTAL=7" "$GF8_OUT" || die "gf_props.gd: stdout missing 'CT_GF8_TOTAL=7'"

python3 "$REPO/scripts/verify_gf8.py" verify "$GF8_FULL" \
	|| die "gf_props.gd: verify_gf8.py assertions failed"

# --- 4. prove the verifier has teeth (tamper runs MUST be rejected) --------
log "tamper runs (each MUST be rejected by verify_gf8.py)"
for mode in value membername missingsetter; do
	python3 "$REPO/scripts/verify_gf8.py" tamper "$GF8_FULL" "$mode" \
		|| die "tamper($mode) was NOT rejected — verifier is vacuous"
done

# --- 5. regression: GF7 + GF6 + GF5 + GF4 + GF3 + GF2 + GF1 + G4 + G3 + G2 ---
# GF8 changes the value path; the prior stack-slot captures + step/call streams
# must be unchanged. GF1 (`counter`) and GF7 (`species`) now capture members
# that were deferred; those verifiers were updated to assert PRESENT.
log "regression: GF7 (multi-file) + GF6 + GF5 + GF4 + GF3 + GF2 + GF1 + G4 + G3 + G2"

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

log "ALL GF8 + tamper + GF7/GF6/GF5/GF4/GF3/GF2/GF1/G4/G3/G2-regression checks PASSED"
