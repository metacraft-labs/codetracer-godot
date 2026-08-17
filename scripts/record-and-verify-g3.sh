#!/usr/bin/env bash
# CodeTracer GDScript recorder — G3 record-and-verify runner.
#
# Builds the patched engine if needed, records the G3 reference program
# (gf_calls.gd) and the G2 regression probe (g2probe.gd) headless with
# CT_GDSCRIPT_TRACE set, decodes each .ct with ct-print --full, and asserts
# the hand-derived facts in scripts/EXPECTED-G3.md via scripts/verify_g3.py.
#
# EXITS NONZERO on any mismatch. This is a real automated test, not a print.
#
# Usage:
#   scripts/record-and-verify-g3.sh            # build-if-needed, then verify
#   BUILD=never scripts/record-and-verify-g3.sh  # fail if binary missing
#   BUILD=always scripts/record-and-verify-g3.sh # force a rebuild first
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
			# Rebuild if any patched source is newer than the binary.
			for f in modules/gdscript/gdscript_ct_trace.cpp \
			         modules/gdscript/gdscript_ct_trace.h \
			         modules/gdscript/gdscript_vm.cpp \
			         modules/gdscript/SCsub; do
				if [[ "$f" -nt "$BIN" ]]; then need_build=1; fi
			done
		fi ;;
	*) die "unknown BUILD=$BUILD (auto|always|never)" ;;
esac

if [[ "$need_build" == 1 ]]; then
	log "building patched engine ($PLATFORM $TARGET $ARCH) via nix develop -c scons"
	# Godot's SConstruct sanitizes the child env; `import_env_vars` copies named
	# vars back in. The nix clang cc-wrapper reads its -L/-isystem paths (incl.
	# zlib/zstd for `-lz`/`-lzstd`) from the NIX* vars, so ALL of them must be
	# forwarded. `import_env_vars` matches exact names (no glob), so we
	# enumerate the NIX* names from inside the dev shell rather than passing a
	# literal `NIX_*` (which matches nothing).
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

# --- 2. record + verify one program ----------------------------------------
WORK="$(mktemp -d "${TMPDIR:-/tmp}/ct-g3.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

record_and_verify() { # <script.gd> <g2|g3> <stdout-needle>
	local gd="$1" mode="$2" needle="$3"
	local name proj trace_dir full stdout_log
	name="$(basename "$gd" .gd)"
	proj="$WORK/$name"
	trace_dir="$proj/trace"
	mkdir -p "$proj" "$trace_dir"
	cp "$REPO/test-programs/gdscript/$gd" "$proj/$gd"
	cat > "$proj/project.godot" <<-EOF
		config_version=5
		[application]
		config/name="ctg3-$name"
	EOF

	log "recording $gd"
	stdout_log="$WORK/$name.stdout"
	CT_GDSCRIPT_TRACE="$trace_dir" \
		"$BIN" --headless --path "$proj" --script "res://$gd" \
		>"$stdout_log" 2>&1 || true
	cat "$stdout_log"

	local ct="$trace_dir/gdscript_trace.ct"
	[[ -f "$ct" ]] || die "no trace produced at $ct"

	# deterministic stdout value (live process output — authoritative)
	grep -qF "$needle" "$stdout_log" || die "$gd: stdout missing '$needle'"

	full="$WORK/$name.full.json"
	"$CT_PRINT" --full "$ct" > "$full" || die "ct-print --full failed on $ct"

	python3 "$REPO/scripts/verify_g3.py" "$mode" "$full" \
		|| die "$gd: verify_g3.py $mode assertions failed"
}

# G3 deliverable: call/return nesting.
record_and_verify gf_calls.gd g3 "CT_G3_RESULT=107"
# G2 regression: per-line steps still emit correctly.
record_and_verify g2probe.gd  g2 "CT_G2_STEPS=30"

log "ALL G3 + G2-regression checks PASSED"
