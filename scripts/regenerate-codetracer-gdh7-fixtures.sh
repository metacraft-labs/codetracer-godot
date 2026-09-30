#!/usr/bin/env bash
# Re-record the hot-reload fixtures CodeTracer's db-backend gdh7 tests read,
# and install them into a sibling `codetracer` checkout:
#
#   codetracer/src/db-backend/tests/fixtures/gdscript/
#     gdh7_reloaded/gdscript_trace.ct   test-programs/gdh6 recorded with two
#                                       live source reloads (v1 -> v2 -> v3)
#     gdh7_reloaded/probe_v{1,2,3}.gd   the three source versions, byte-identical
#                                       to test-programs/gdh6/probe_v*.gd
#     gdh7_control/gdscript_trace.ct    the same program with no reload
#
# The producer is scripts/verify_gdh6.py, the GDH-M6 gate driver: it records
# both runs over the patchable engine and grades them. The recordings are
# installed only if EVERY GDH-M6 gate is green over them and their meta.dat
# schema versions are the ones the format prescribes: 5 for the reloaded run
# (it carries source-reload markers, which need the extended flags) and 4 for
# the control.
#
# Usage: scripts/regenerate-codetracer-gdh7-fixtures.sh
# Env:   CODETRACER (default ../codetracer), CT_PRINT (the `nix develop` shell
#        exports the one at the pinned writer revision), BIN (default
#        bin/godot.linuxbsd.template_debug.x86_64.hcr, built by
#        scripts/build-hcr-patchable-linux.sh).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="${BIN:-$REPO/bin/godot.linuxbsd.template_debug.x86_64.hcr}"
CT_PRINT="${CT_PRINT:-$REPO/../codetracer-trace-format-nim/ct-print}"
CODETRACER="${CODETRACER:-$REPO/../codetracer}"
FIXTURES="$CODETRACER/src/db-backend/tests/fixtures/gdscript"
PROGRAMS="$REPO/test-programs/gdh6"

log() { printf '\n=== %s ===\n' "$*"; }
die() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

[[ -x "$BIN" ]] || die "no patchable engine at $BIN (scripts/build-hcr-patchable-linux.sh builds it)"
[[ -x "$CT_PRINT" ]] || die "ct-print not found/executable at $CT_PRINT"
[[ -d "$FIXTURES" ]] || die "no GDScript fixture directory at $FIXTURES (set CODETRACER)"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/ct-gdh7.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
SOCK_DIR="${XDG_RUNTIME_DIR:-/tmp}"
[[ -d "$SOCK_DIR" ]] || SOCK_DIR=/tmp

log "recording and grading with verify_gdh6.py (all gates)"
if ! python3 "$REPO/scripts/verify_gdh6.py" --engine "$BIN" --fixtures "$PROGRAMS" \
	--work "$WORK/run" --ct-print "$CT_PRINT" --socket-dir "$SOCK_DIR" \
	--gate all --falsify none >"$WORK/run.out" 2>&1; then
	cat "$WORK/run.out" >&2
	die "a GDH-M6 gate is red over these recordings; nothing was installed"
fi
grep -E "GREEN|^== " "$WORK/run.out" || true

meta_dat_version() {
	python3 - "$1" <<'PY'
import struct, sys
data = open(sys.argv[1], "rb").read()
hits = data.count(b"CTMD")
if hits != 1:
    sys.exit("expected exactly one meta.dat header in %s, found %d" % (sys.argv[1], hits))
at = data.find(b"CTMD")
print(struct.unpack("<H", data[at + 4:at + 6])[0])
PY
}

RELOADED="$WORK/run/reloaded/trace/gdscript_trace.ct"
CONTROL="$WORK/run/control/trace/gdscript_trace.ct"
[[ -f "$RELOADED" && -f "$CONTROL" ]] || die "the driver did not leave both recordings under $WORK/run"
v="$(meta_dat_version "$RELOADED")"
[[ "$v" == 5 ]] || die "reloaded recording: meta.dat v$v, expected 5 (it carries source-reload markers)"
v="$(meta_dat_version "$CONTROL")"
[[ "$v" == 4 ]] || die "control recording: meta.dat v$v, expected 4 (no reload)"

mkdir -p "$FIXTURES/gdh7_reloaded" "$FIXTURES/gdh7_control"
cp "$RELOADED" "$FIXTURES/gdh7_reloaded/gdscript_trace.ct"
for n in 1 2 3; do cp "$PROGRAMS/probe_v$n.gd" "$FIXTURES/gdh7_reloaded/probe_v$n.gd"; done
cp "$CONTROL" "$FIXTURES/gdh7_control/gdscript_trace.ct"
log "installed gdh7_reloaded (meta.dat v5) and gdh7_control (meta.dat v4) into $FIXTURES"
