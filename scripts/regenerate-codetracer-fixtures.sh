#!/usr/bin/env bash
# Re-record the GDScript fixtures CodeTracer's db-backend tests read, with this
# engine, and install them into a sibling `codetracer` checkout.
#
#   codetracer/src/db-backend/tests/fixtures/gdscript/<program>/
#       gdscript_trace.ct   the recording
#       <program>.gd        the source it was recorded from (byte-identical to
#                           test-programs/gdscript/<program>.gd)
#
# Every recording is graded BEFORE it is installed, by the same checks the
# corpus runner applies (the program's stdout marker and its verifier against
# its EXPECTED-*.md facts), and its meta.dat schema version is asserted. A
# recording that fails any of them is not installed, so a stale engine or
# writer archive cannot silently replace a fixture with a container the
# readers refuse.
#
# Usage:
#   scripts/regenerate-codetracer-fixtures.sh
#
# Environment:
#   CODETRACER   codetracer checkout to install into (default ../codetracer)
#   CT_PRINT     ct-print from codetracer-trace-format-nim at the revision the
#                vendored writer archive was built from
#                (default ../codetracer-trace-format-nim/ct-print)
#   PLATFORM / ARCH / TARGET / BIN / BUILD   as for scripts/corpus-lib.sh;
#                PLATFORM and ARCH default to this host.
set -euo pipefail

case "$(uname -s)-$(uname -m)" in
	Linux-x86_64)  : "${PLATFORM:=linuxbsd}" "${ARCH:=x86_64}" ;;
	Darwin-arm64)  : "${PLATFORM:=macos}" "${ARCH:=arm64}" ;;
esac
export PLATFORM ARCH

# shellcheck source=scripts/corpus-lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/corpus-lib.sh"

CODETRACER="${CODETRACER:-$REPO/../codetracer}"
FIXTURES="$CODETRACER/src/db-backend/tests/fixtures/gdscript"
[[ -d "$FIXTURES" ]] || die "no GDScript fixture directory at $FIXTURES (set CODETRACER)"

# Every recording's meta.dat is version 6, and one made without a reload agent
# does not declare source reloads: flags_ext is 0
# (codetracer-trace-format-spec/internal-files.md §"Extended flags").
EXPECTED_META_VERSION=6

corpus_require_tools
corpus_ensure_engine

WORK="$(mktemp -d "${TMPDIR:-/tmp}/ct-fixtures.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

meta_dat_version() { # <file.ct> -> meta.dat's version and flags_ext
	python3 - "$1" <<'PY'
import struct, sys
data = open(sys.argv[1], "rb").read()
hits = data.count(b"CTMD")
if hits != 1:
    sys.exit("expected exactly one meta.dat header in %s, found %d" % (sys.argv[1], hits))
at = data.find(b"CTMD")
version, = struct.unpack("<H", data[at + 4:at + 6])
flags_ext, = struct.unpack("<I", data[at + 8:at + 12])
print(version, flags_ext)
PY
}

# program | verifier | verifier command | stdout marker
FIXTURE_PROGRAMS=(
	"gf_values|verify_g4.py|verify|CT_G4_RESULT=47"
	"gf_coroutine|verify_gf10.py|verify|CT_GF10_RESULT=42"
)

staged=()
for row in "${FIXTURE_PROGRAMS[@]}"; do
	IFS='|' read -r program verifier cmd marker <<<"$row"
	log "recording $program.gd"
	IFS=$'\t' read -r full stdout_log ct < <(corpus_record "$WORK" "$program.gd")
	grep -qF "$marker" "$stdout_log" || die "$program.gd: stdout is missing '$marker'"
	python3 "$REPO/scripts/$verifier" "$cmd" "$full" || die "$program.gd: $verifier $cmd failed"
	read -r version flags_ext < <(meta_dat_version "$ct")
	[[ "$version" == "$EXPECTED_META_VERSION" ]] \
		|| die "$program.gd: meta.dat schema version $version, expected $EXPECTED_META_VERSION"
	[[ "$flags_ext" == 0 ]] \
		|| die "$program.gd: meta.dat flags_ext $flags_ext, expected 0 (recorded without a reload agent)"
	log "$program.gd: graded, meta.dat v$version, $(corpus_step_count "$full") steps"
	staged+=("$program|$ct")
done

# Install only once every recording has passed.
for entry in "${staged[@]}"; do
	IFS='|' read -r program ct <<<"$entry"
	dest="$FIXTURES/$program"
	mkdir -p "$dest"
	cp "$ct" "$dest/gdscript_trace.ct"
	cp "$PROGRAMS_DIR/$program.gd" "$dest/$program.gd"
	log "installed $dest"
done
