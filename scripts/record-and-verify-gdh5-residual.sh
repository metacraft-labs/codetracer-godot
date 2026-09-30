#!/usr/bin/env bash
# GDH-M5 residual — a deferred reload's answer must be what happened.
#
# A source reload that arrives while the VM is mid-step is queued for the
# engine's next safe point, and the agent thread waits for it with a bound.
# The coordinator gets exactly one answer, and this gate requires that answer
# to agree with what the engine did:
#
#   late-apply  the safe point took the request, and the bound expired while
#               the apply was running: the answer is APPLIED, and the program
#               runs v2. (Before the fix the answer was `no-safe-point` —
#               a refusal for a reload the engine then applied.)
#   withdrawn   the bound expired while the request was still only queued: the
#               answer is `no-safe-point`, and the program never runs v2.
#   control     the safe point arrives inside the bound: APPLIED, v2 runs.
#
# Each shape is driven by scripts/gdh6_asan_drive.py over the plain patchable
# engine (no sanitizer). Two test hooks, both read and reported by the shipped
# recorder, make the timing deterministic: CT_GDH6_SAFE_POINT_DELAY_MS holds
# the apply after the request is taken, CT_GDH6_SAFE_POINT_CLAIM_HOLD_MS holds
# the safe point before it takes it. Both are off unless set.
#
# Usage: scripts/record-and-verify-gdh5-residual.sh [<output-dir>]
# Env:   BIN (default bin/godot.linuxbsd.template_debug.x86_64.hcr; build it with
#        scripts/build-hcr-patchable-linux.sh), TIMEOUT (default 300).
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="${BIN:-$REPO/bin/godot.linuxbsd.template_debug.x86_64.hcr}"
OUT="${1:-${TMPDIR:-/tmp}/ct-gdh5-residual-$$}"
TIMEOUT="${TIMEOUT:-300}"
FIXTURES="$REPO/test-programs/gdh6"
DRIVE="$REPO/scripts/gdh6_asan_drive.py"
WAIT_S=2
HOLD_MS=9000

log() { echo "[gdh5-residual] $*"; }
die() { echo "[gdh5-residual] DRIVER-FAIL: $*" >&2; exit 2; }

[[ -x "$BIN" ]] || die "no patchable engine at $BIN (scripts/build-hcr-patchable-linux.sh builds it)"
[[ -f "$DRIVE" ]] || die "driver missing: $DRIVE"
mkdir -p "$OUT" || die "cannot create $OUT"
SOCK_DIR="${XDG_RUNTIME_DIR:-/tmp}"
[[ -d "$SOCK_DIR" ]] || SOCK_DIR=/tmp

failures=0
run_shape() { # run_shape <shape> <post-claim delay ms> <pre-claim hold ms>
	local shape="$1" delay="$2" claim_hold="$3" rc
	rm -rf "${OUT:?}/$shape"
	mkdir -p "$OUT/$shape"
	CT_GDH6_RELOAD_WAIT_SECONDS="$WAIT_S" \
	CT_GDH6_SAFE_POINT_DELAY_MS="$delay" \
	CT_GDH6_SAFE_POINT_CLAIM_HOLD_MS="$claim_hold" \
		timeout "$TIMEOUT" python3 "$DRIVE" --engine "$BIN" --fixtures "$FIXTURES" \
		--work "$OUT/$shape" --socket-dir "$SOCK_DIR" >"$OUT/$shape.out" 2>&1
	rc=$?
	grep -E "^\[gdh6-asan\] (shape|assertions)|GDH6-ASAN-FAIL|DRIVER-FAIL" "$OUT/$shape.out"
	if [[ "$rc" == 0 ]]; then
		log "$shape: PASS"
	else
		log "$shape: FAIL (rc $rc; transcript $OUT/$shape.out)"
		failures=$((failures + 1))
	fi
}

log "engine: $BIN   bound: ${WAIT_S}s   hold: ${HOLD_MS}ms"
run_shape control 0 0
run_shape late-apply "$HOLD_MS" 0
run_shape withdrawn 0 "$HOLD_MS"

[[ "$failures" -eq 0 ]] || die "$failures of 3 shapes failed"
log "ALL 3 SHAPES PASSED: every answer agrees with what the engine did"
