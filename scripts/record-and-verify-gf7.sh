#!/usr/bin/env bash
# CodeTracer GDScript recorder — GF7 (Classes / Inheritance / super / inner
# classes / preload/load) record-and-verify runner.
#
# Records the GF7 reference program (a two-file class hierarchy gf_animal.gd
# [base] + gf_dog.gd [derived], driven by the gf_zoo.gd MainLoop) with the
# patched engine, decodes its .ct with ct-print --full, and asserts the
# hand-derived facts in scripts/EXPECTED-GF7.md via scripts/verify_gf7.py:
#   - the cross-FILE call tree with correct per-frame SOURCE PATHS (a frame's
#     source = the path of the step at its entry_step);
#   - super across files: Dog.speak (gf_dog.gd) -> Animal.speak (gf_animal.gd),
#     and the Dog._init -> super Animal._init ctor chain, each proven twice
#     (preload + load paths);
#   - ctor ARGS captured (via read-into-local) and propagated cross-file
#     (got_name == pup == ["rex","spot"]);
#   - inner-class method frames (Tag.label -> "tag", Kennel.size -> 3);
#   - _static_init recorded under @static_initializer (marker=7);
#   - member values (species/v/count) GF8-deferred (asserted ABSENT).
#
# GF7 adds NO recorder code (like GF1/GF2/GF6): a class method, super, _init,
# _static_init, and an inner-class method are all GDScriptFunctions, so the
# existing G2/G3/G4/GF5 hooks fire. This runner therefore does NOT force a
# rebuild; it uses the existing (GF6-era) binary. See EXPECTED-GF7.md.
#
# Then it PROVES the verifier has teeth with tamper runs (wrong frame source /
# missing super frame / wrong ctor arg / wrong nesting), each of which MUST be
# rejected.
#
# Finally it re-runs the GF6..G2 regressions to confirm the recorder is
# unchanged (streams byte-identical).
#
# EXITS NONZERO on any mismatch. This is a real automated test, not a print.
#
# Usage:
#   scripts/record-and-verify-gf7.sh              # build-if-needed, then verify
#   BUILD=never scripts/record-and-verify-gf7.sh  # fail if binary missing
#   BUILD=always scripts/record-and-verify-gf7.sh # force a rebuild first
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
# GF7 adds no recorder code, so a rebuild is only needed if the binary is
# missing or a recorder source is newer than it.
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
WORK="$(mktemp -d "${TMPDIR:-/tmp}/ct-gf7.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

# record_multi <entry.gd> <extra.gd>...  -> echoes "<full.json>\t<stdout_log>"
# Copies the entry script plus every extra sibling script into a fresh project,
# runs the entry as the MainLoop, and decodes the produced .ct.
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
		config/name="ctgf7-$name"
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

# --- 3. GF7 deliverable: classes / inheritance / super / inner / preload ----
log "recording gf_zoo.gd (GF7 classes/inheritance/super/inner/preload/load)"
IFS=$'\t' read -r GF7_FULL GF7_OUT < <(record_multi gf_zoo.gd gf_animal.gd gf_dog.gd)
cat "$GF7_OUT"
grep -qF "CT_GF7_RESULT=20" "$GF7_OUT" || die "gf_zoo.gd: stdout missing 'CT_GF7_RESULT=20'"

python3 "$REPO/scripts/verify_gf7.py" verify "$GF7_FULL" \
	|| die "gf_zoo.gd: verify_gf7.py assertions failed"

# --- 4. prove the verifier has teeth (tamper runs MUST be rejected) --------
log "tamper runs (each MUST be rejected by verify_gf7.py)"
for mode in srcpath missingsuper ctorarg nesting; do
	python3 "$REPO/scripts/verify_gf7.py" tamper "$GF7_FULL" "$mode" \
		|| die "tamper($mode) was NOT rejected — verifier is vacuous"
done

# --- 5. regression: GF6 + GF5 + GF4 + GF3 + GF2 + GF1 + G4 + G3 + G2 --------
# GF7 adds no recorder code, so all prior streams must be byte-identical.
log "regression: GF6 + GF5 + GF4 + GF3 + GF2 + GF1 + G4 + G3 + G2"
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

log "ALL GF7 + tamper + GF6/GF5/GF4/GF3/GF2/GF1/G4/G3/G2-regression checks PASSED"
