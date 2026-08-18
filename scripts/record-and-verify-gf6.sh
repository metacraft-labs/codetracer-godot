#!/usr/bin/env bash
# CodeTracer GDScript recorder — GF6 (Lambdas & Closures / local capture)
# record-and-verify runner.
#
# Records the GF6 reference program (gf_lambdas.gd) with the patched engine,
# decodes its .ct with ct-print --full, and asserts the hand-derived facts in
# scripts/EXPECTED-GF6.md via scripts/verify_gf6.py:
#   - a lambda `.call(...)` records as a nested call/return FRAME
#     ("<anonymous lambda>") with its return value captured (GF5);
#   - the lambda's PARAM (x -> seen_x=5) and CAPTURED outer local
#     (base -> seen_base=10) are readable at lambda execution;
#   - CAPTURE-BY-VALUE: after the outer `base` mutates 10 -> 999, the lambda
#     STILL returns 15 and seen_base STILL reads 10 (base itself is [10, 999]);
#   - a lambda stored in a var is value-captured as Callable{method=
#     "<anonymous lambda>"} (add/doubler/outer/inner);
#   - a NESTED lambda capturing two scopes nests at depth 2 (returns 307).
#
# GF6 adds NO recorder code (like GF1/GF2): a lambda is a GDScriptFunction, so
# its `.call(...)` invocation goes through GDScriptFunction::call and the
# existing G2/G3/G4/GF5 hooks fire. Captures are injected as leading parameters
# (gdscript_analyzer.cpp resolve_pending_lambda_bodies) so they are named stack
# slots; being args they are prologue-placed and observed via reads-into-locals
# (the GF5 supplied-arg seam). This runner therefore does NOT force a rebuild;
# it uses the existing (GF5-era) binary. See EXPECTED-GF6.md.
#
# Then it PROVES the verifier has teeth with tamper runs (wrong param / wrong
# captured value / capture-by-reference / wrong return / wrong nesting), each of
# which MUST be rejected.
#
# Finally it re-runs the GF5 + GF4 + GF3 + GF2 + GF1 + G4 + G3 + G2 regressions
# to confirm the recorder is unchanged (streams byte-identical).
#
# EXITS NONZERO on any mismatch. This is a real automated test, not a print.
#
# Usage:
#   scripts/record-and-verify-gf6.sh              # build-if-needed, then verify
#   BUILD=never scripts/record-and-verify-gf6.sh  # fail if binary missing
#   BUILD=always scripts/record-and-verify-gf6.sh # force a rebuild first
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
# GF6 adds no recorder code, so a rebuild is only needed if the binary is
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
WORK="$(mktemp -d "${TMPDIR:-/tmp}/ct-gf6.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

record() { # <script.gd> -> echoes "<full.json>\t<stdout_log>"
	local gd="$1"
	local name proj trace_dir full stdout_log
	name="$(basename "$gd" .gd)"
	proj="$WORK/$name"
	trace_dir="$proj/trace"
	mkdir -p "$proj" "$trace_dir"
	cp "$REPO/test-programs/gdscript/$gd" "$proj/$gd"
	cat > "$proj/project.godot" <<-EOF
		config_version=5
		[application]
		config/name="ctgf6-$name"
	EOF

	stdout_log="$WORK/$name.stdout"
	CT_GDSCRIPT_TRACE="$trace_dir" \
		"$BIN" --headless --path "$proj" --script "res://$gd" \
		>"$stdout_log" 2>&1 || true

	local ct="$trace_dir/gdscript_trace.ct"
	[[ -f "$ct" ]] || die "no trace produced at $ct"
	full="$WORK/$name.full.json"
	"$CT_PRINT" --full "$ct" > "$full" || die "ct-print --full failed on $ct"
	printf '%s\t%s\n' "$full" "$stdout_log"
}

# --- 3. GF6 deliverable: lambdas & closures --------------------------------
log "recording gf_lambdas.gd (GF6 lambdas/closures/capture)"
IFS=$'\t' read -r GF6_FULL GF6_OUT < <(record gf_lambdas.gd)
cat "$GF6_OUT"
grep -qF "CT_GF6_RESULT=379" "$GF6_OUT" || die "gf_lambdas.gd: stdout missing 'CT_GF6_RESULT=379'"

python3 "$REPO/scripts/verify_gf6.py" verify "$GF6_FULL" \
	|| die "gf_lambdas.gd: verify_gf6.py assertions failed"

# --- 4. prove the verifier has teeth (tamper runs MUST be rejected) --------
log "tamper runs (each MUST be rejected by verify_gf6.py)"
for mode in param captured capturebyvalue return nesting; do
	python3 "$REPO/scripts/verify_gf6.py" tamper "$GF6_FULL" "$mode" \
		|| die "tamper($mode) was NOT rejected — verifier is vacuous"
done

# --- 5. regression: GF5 + GF4 + GF3 + GF2 + GF1 + G4 + G3 + G2 unchanged -----
# GF6 adds no recorder code, so all prior streams must be byte-identical.
log "regression: GF5 + GF4 + GF3 + GF2 + GF1 + G4 + G3 + G2"
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

log "ALL GF6 + tamper + GF5/GF4/GF3/GF2/GF1/G4/G3/G2-regression checks PASSED"
