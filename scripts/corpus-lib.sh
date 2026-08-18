#!/usr/bin/env bash
# CodeTracer GDScript recorder — GT1 shared corpus library.
#
# Sourced by scripts/record-and-verify.sh (the unified corpus runner) and
# scripts/verify-corpus-no-silent-skip.sh (the no-silent-skip gate). It owns the
# single implementation of: engine/tool location, optional build, manifest
# parsing, headless recording, and step counting — so the runner and the gate
# never drift on how a program is recorded.
#
# The corpus manifest (test-programs/gdscript/CORPUS.md) is the AUTHORITATIVE
# index of every corpus program. Its machine-readable records live between the
# CORPUS-MACHINE-BEGIN / CORPUS-MACHINE-END markers, one pipe-delimited row per
# program:
#
#   program|milestone|role|entry|verifier|cmd|golden|markers
#
#   program  : the .gd file name (basename, under test-programs/gdscript/)
#   milestone: G2 / G3 / G4 / GF1 .. GF13 (the feature it covers)
#   role     : primary | helper | probe
#              - primary : recorded standalone AND verified by <verifier>
#              - helper  : a class/scene dependency compiled as part of a
#                          primary's project; <entry> names that primary. Never
#                          recorded standalone (it has no _init main of its own).
#              - probe   : a negative/edge recording (e.g. failing-assert halt)
#                          asserted by an inline halt-check, not a verify_*.py.
#   entry    : space-separated .gd files copied into the recorded project; the
#              FIRST is the --script entry. For helper rows: the primary that
#              pulls it in. (No spaces inside a single file name.)
#   verifier : scripts/<verify_*.py> (primary) or "-" (helper/probe)
#   cmd      : the verifier subcommand (verify | g2 | g3) or "-"
#   golden   : scripts/<EXPECTED-*.md> first-principles facts file
#   markers  : ';'-separated exact substrings that MUST appear in the program's
#              stdout (deterministic result markers), or "-"
#
# No mocks anywhere: every recording is the real patched engine over a real .gd
# producing a real .ct decoded by the real ct-print.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PLATFORM="${PLATFORM:-macos}"
ARCH="${ARCH:-arm64}"
TARGET="${TARGET:-template_debug}"
BIN="${BIN:-$REPO/bin/godot.${PLATFORM}.${TARGET}.${ARCH}}"
CT_PRINT="${CT_PRINT:-$REPO/../codetracer-trace-format-nim/ct-print}"
PROGRAMS_DIR="${CORPUS_PROGRAMS_DIR:-$REPO/test-programs/gdscript}"
MANIFEST="${CORPUS_MANIFEST:-$PROGRAMS_DIR/CORPUS.md}"

log()  { printf '\n=== %s ===\n' "$*"; }
die()  { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

# --- tooling checks --------------------------------------------------------
corpus_require_tools() {
	[[ -x "$CT_PRINT" ]] || die "ct-print not found/executable at $CT_PRINT"
	command -v python3 >/dev/null || die "python3 not found"
}

# --- build the patched engine if requested ---------------------------------
# BUILD=auto|always|never (default: never — GT1 adds no recorder code).
corpus_ensure_engine() {
	local build="${BUILD:-never}"
	local need=0
	case "$build" in
		always) need=1 ;;
		never)  need=0 ;;
		auto)   [[ -x "$BIN" ]] || need=1 ;;
		*) die "unknown BUILD=$build (auto|always|never)" ;;
	esac
	if [[ "$need" == 1 ]]; then
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
	[[ -x "$BIN" ]] || die "engine binary missing at $BIN (BUILD=$build)"
}

# --- manifest parsing ------------------------------------------------------
# Emit the machine-readable manifest rows (pipe-delimited), comments stripped.
corpus_records() {
	[[ -f "$MANIFEST" ]] || die "corpus manifest not found at $MANIFEST"
	awk '
		/^<!-- CORPUS-MACHINE-BEGIN -->/ { inblk=1; next }
		/^<!-- CORPUS-MACHINE-END -->/   { inblk=0; next }
		inblk {
			line=$0
			sub(/^[ \t]+/, "", line)
			# A real record: first pipe field is a .gd program name.
			if (line !~ /^[A-Za-z0-9_]+\.gd\|/) next
			print line
		}
	' "$MANIFEST"
}

# field <row> <n>  -> the n-th pipe field
corpus_field() { awk -F'|' -v n="$2" 'BEGIN{print}' >/dev/null 2>&1 || true; printf '%s' "$1" | awk -F'|' -v n="$2" '{gsub(/^[ \t]+|[ \t]+$/,"",$n); print $n}'; }

# All program basenames listed in the manifest (any role), sorted-unique.
corpus_manifest_programs() {
	corpus_records | awk -F'|' '{gsub(/^[ \t]+|[ \t]+$/,"",$1); print $1}' | sort -u
}

# All .gd files physically present under test-programs/gdscript/, sorted-unique.
corpus_disk_programs() {
	( cd "$PROGRAMS_DIR" && ls -1 *.gd 2>/dev/null ) | sort -u
}

# --- recording -------------------------------------------------------------
# corpus_record <workdir> <entry.gd> [extra.gd ...]
#   Records headless with CT_GDSCRIPT_TRACE and decodes with `ct-print --full`.
#   Prints "<full.json>\t<stdout.log>\t<ct>" (tab-separated).
corpus_record() {
	local work="$1"; shift
	local entry="$1"; shift
	local name proj trace_dir full stdout_log ct extra
	name="$(basename "$entry" .gd)"
	proj="$work/$name"
	trace_dir="$proj/trace"
	mkdir -p "$proj" "$trace_dir"
	cp "$PROGRAMS_DIR/$entry" "$proj/$entry"
	for extra in "$@"; do cp "$PROGRAMS_DIR/$extra" "$proj/$extra"; done
	cat > "$proj/project.godot" <<-EOF
		config_version=5
		[application]
		config/name="ctcorpus-$name"
	EOF
	stdout_log="$proj/stdout.log"
	# push_error / a failing assert make the engine exit nonzero; we assert on
	# the produced trace, not the exit code.
	CT_GDSCRIPT_TRACE="$trace_dir" \
		"$BIN" --headless --path "$proj" --script "res://$entry" \
		>"$stdout_log" 2>&1 || true
	ct="$trace_dir/gdscript_trace.ct"
	[[ -f "$ct" ]] || die "no trace produced at $ct (program $entry)"
	full="$proj/full.json"
	"$CT_PRINT" --full "$ct" > "$full" || die "ct-print --full failed on $ct"
	printf '%s\t%s\t%s\n' "$full" "$stdout_log" "$ct"
}

# corpus_step_count <full.json> -> number of "step" events
corpus_step_count() {
	python3 - "$1" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1]))
print(sum(1 for e in doc.get("events", []) if e.get("kind") == "step"))
PY
}
