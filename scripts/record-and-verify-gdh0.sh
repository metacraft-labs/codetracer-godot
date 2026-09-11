#!/usr/bin/env bash
# CodeTracer GDScript recorder — GDH-M0, the runnable falsifier.
#
# A headless `template_debug` Godot engine runs `test-programs/gdh0/probe_v1.gd`
# in a loop under CT_GDSCRIPT_TRACE. Part-way through, the file on disk is
# overwritten with `probe_v2.gd` and the engine is told to reload it through a
# path that exists TODAY — `core:reload_scripts` on the `--remote-debug` peer.
# The engine's own stdout proves v2 executed. Then the recording is measured.
#
# Everything here runs against real binaries: the real fork engine, the real
# recorder linked into it, the real `ct-print`. `allowed_mocks: none`, and there
# are none. No format change, no C ABI change, no agent change — the milestone's
# `external_prereqs` is `none` and this script honours that literally.
#
# THE GATE THIS DRIVER CARRIES:
#
#   GDH-G0a  PERMANENT.  The engine really reloaded — asserted from the
#            ENGINE'S OWN STDOUT, independently of the trace.
#
# GDH-G0b — the second, DATED gate this driver used to run — was DELETED by
# GDH-M6 on 2026-09-11.  It asserted what the container carried while the
# defect was live: ONE paths.dat entry, ONE raw source view holding v1's bytes,
# and post-reload steps decoding to line numbers that do not exist in v1 at
# all.  GDH-M0's own text required the deletion — "a test that asserts a defect
# must not be allowed to become furniture" — and it happened only because a
# strictly stronger gate replaces it.  The claim-for-claim mapping is in the
# note at the point of deletion in `scripts/verify_gdh0.py`, and the
# replacement runs from `scripts/record-and-verify-gdh6.sh`.
#
# FOUR ARMS, all real recordings:
#
#   reload       the subject. Overwrite + reload the real path.
#   control      no overwrite, no reload request. Must show ONE version and
#                say so, rather than passing in silence.
#   wrongtarget  overwrite lands, but the reload request names a path the
#                engine never loads. GDH-G0a MUST go red ("the reload did not
#                happen"); a green G0a here would mean the gate has no teeth.
#   identical    the replacement is byte-identical to v1. The run then cannot
#                distinguish a reload from no reload and MUST be a CHECK-FAIL —
#                EXPECTED-HCR1.md's "an unchanged observation must not pass".
#
# Every hand-derived value is in scripts/EXPECTED-GDH0.md. Nothing about the
# fixture is hardcoded in scripts/verify_gdh0.py: the insertion height, the
# probe body's line numbers, the tick count and the per-version tokens are all
# measured from the two .gd files at run time.
#
# Usage:
#   scripts/record-and-verify-gdh0.sh [<output-dir>]
#
# Environment overrides: BIN, CT_PRINT, ARMS.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLATFORM="${PLATFORM:-linuxbsd}"
TARGET="${TARGET:-template_debug}"
ARCH="${ARCH:-x86_64}"
BIN="${BIN:-$REPO/bin/godot.${PLATFORM}.${TARGET}.${ARCH}}"
CT_PRINT="${CT_PRINT:-$REPO/../codetracer-trace-format-nim/ct-print}"
FIXTURES="$REPO/test-programs/gdh0"
V1="$FIXTURES/probe_v1.gd"
V2="$FIXTURES/probe_v2.gd"
VERIFY="$REPO/scripts/verify_gdh0.py"
ARMS="${ARMS:-reload control wrongtarget identical}"

OUT="${1:-${TMPDIR:-/tmp}/ct-gdh0-$$}"

log()  { printf '[gdh0] %s\n' "$*"; }
head_() { printf '\n[gdh0] === %s ===\n' "$*"; }
die()  { printf '[gdh0] FATAL: %s\n' "$*" >&2; exit 2; }
fail_count=0
check_fail() { printf '[gdh0] CHECK-FAIL: %s\n' "$*" >&2; fail_count=$((fail_count + 1)); }

# --- prerequisites, loudly -------------------------------------------------
# A missing prerequisite must never look like a pass or a skip. Each of these
# is a hard stop naming the path that was looked for.
[[ -x "$BIN" ]]       || die "engine not built/executable: $BIN"
[[ -x "$CT_PRINT" ]]  || die "ct-print not found/executable: $CT_PRINT (build it in codetracer-trace-format-nim)"
[[ -f "$V1" ]]        || die "fixture missing: $V1"
[[ -f "$V2" ]]        || die "fixture missing: $V2"
[[ -f "$FIXTURES/project.godot" ]] || die "fixture project missing: $FIXTURES/project.godot"
[[ -f "$VERIFY" ]]    || die "verifier missing: $VERIFY"
command -v python3 >/dev/null || die "python3 not on PATH"

mkdir -p "$OUT" || die "cannot create $OUT"
log "engine   : $BIN"
log "ct-print : $CT_PRINT"
log "fixtures : $V1 / $V2"
log "output   : $OUT"

# --- /dev/shm hygiene ------------------------------------------------------
# The GDScript recorder writes its container directly and does not use the
# MCR shared rings, so this run should leak nothing. Counting before and after
# makes that a MEASUREMENT rather than an assumption — an accumulation of
# ct_rb_* / ct_mcr_* segments has been observed to fake unrelated failures in
# later recordings on this host.
shm_ct_count() { ls /dev/shm 2>/dev/null | grep -c '^ct_' || true; }
SHM_BEFORE="$(shm_ct_count)"
log "/dev/shm ct_* segments before: $SHM_BEFORE"

# --- deliverable 4: is `--path <real dir>` available in THIS binary? --------
# Design §5.1: `--path` is gated on OVERRIDE_PATH_ENABLED, which the fork's
# build script turns on with `disable_path_overrides=no`. A stock export
# template aborts on `--path`, and without an editable res:// there is nothing
# to overwrite. The probe distinguishes the two refusals; exactly one must be
# present, so a scan that could see NEITHER cannot pass.
head_ "deliverable 4: --path availability"
python3 "$VERIFY" pathcheck "$BIN" > "$OUT/pathcheck.json" 2>"$OUT/pathcheck.log"
PATHCHECK_RC=$?
cat "$OUT/pathcheck.log"
[[ "$PATHCHECK_RC" -eq 0 ]] || die '--path <real dir> is NOT available in '"$BIN"'; GDH-M0 cannot run against it (see '"$OUT"'/pathcheck.log)'
log "recorded: $OUT/pathcheck.json"

# --- the fixture's own preconditions ---------------------------------------
head_ "fixture preconditions"
python3 "$VERIFY" fixture "$V1" "$V2" > "$OUT/fixture.json" 2>"$OUT/fixture.log"
FIXTURE_RC=$?
cat "$OUT/fixture.log"
[[ "$FIXTURE_RC" -eq 0 ]] || die "the fixture pair cannot support the gate (see $OUT/fixture.log)"
log "recorded: $OUT/fixture.json"

# --- the arms ---------------------------------------------------------------
arms_run=0
for arm in $ARMS; do
	head_ "arm: $arm"
	arm_out="$OUT/$arm"
	python3 "$VERIFY" record "$BIN" "$V1" "$V2" "$FIXTURES" "$arm_out" --arm "$arm"
	rec_rc=$?
	if [[ "$rec_rc" -ne 0 ]]; then
		check_fail "arm $arm: recording failed (rc=$rec_rc)"
		continue
	fi

	verify_args=("$arm_out" "$V1" "$V2" --ct-print "$CT_PRINT")
	case "$arm" in
		# GDH-G0b was deleted by GDH-M6 on 2026-09-11 (see the note at the
		# point of deletion in verify_gdh0.py). The `reload` and `control`
		# arms now assert GDH-G0a only, which is the half that was always
		# meant to outlive the defect.
		wrongtarget|identical) verify_args+=(--expect-g0a-red) ;;
	esac
	python3 "$VERIFY" verify "${verify_args[@]}"
	ver_rc=$?
	if [[ "$ver_rc" -ne 0 ]]; then
		check_fail "arm $arm: verification failed (rc=$ver_rc)"
	fi
	arms_run=$((arms_run + 1))
done

# The loop's membership is knowable, so the control is the COUNT, not its
# non-emptiness: `at least one arm ran` is satisfied by one arm of four
# (Verification-Harness-Traps.md 4b).
expected_arms="$(printf '%s\n' $ARMS | grep -c .)"
[[ "$arms_run" -eq "$expected_arms" ]] \
	|| check_fail "only $arms_run of $expected_arms arms ran to a verdict"

# --- /dev/shm after ---------------------------------------------------------
SHM_AFTER="$(shm_ct_count)"
log "/dev/shm ct_* segments after: $SHM_AFTER"
if [[ "$SHM_AFTER" -gt "$SHM_BEFORE" ]]; then
	log "cleaning $((SHM_AFTER - SHM_BEFORE)) leaked ct_* segment(s)"
	for f in $(ls /dev/shm 2>/dev/null | grep '^ct_'); do rm -f "/dev/shm/$f"; done
fi

head_ "result"
if [[ "$fail_count" -eq 0 ]]; then
	log "GDH-M0: OK — $expected_arms arms, artifacts under $OUT"
	log "  GDH-G0a green on the reload arm, red on both falsifier arms."
	log "  GDH-G0b: DELETED by GDH-M6 on 2026-09-11. It was a dated snapshot of"
	log "  the defect, and the defect is gone; its claims are now made, more"
	log "  strongly, by scripts/record-and-verify-gdh6.sh."
	exit 0
fi
log "GDH-M0: $fail_count check failure(s); artifacts under $OUT"
log "  GDH-G0a is the only gate here now, and it is PERMANENT: it says the"
log "  engine really reloaded, from the engine's own stdout and independently"
log "  of any trace. A failure means the reload stopped happening — not that"
log "  the container changed. Do not adjust the assertion."
exit 1
