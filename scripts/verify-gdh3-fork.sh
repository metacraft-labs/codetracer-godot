#!/usr/bin/env bash
# GDH-M3, fork side — the recorder stops re-deriving path ids.
#
# The container half of `gdh3_fork_uses_the_writers_own_path_id` runs in
# `codetracer-trace-format-nim/tests/run_gdh3_gates.sh`, against the real C ABI
# with a real two-version container. This script checks the half that lives
# here: that the FORK actually asks the writer, that the mirror counter is
# GONE, and that the vendored header and archive can answer.
#
# WHY A SOURCE-LEVEL CHECK AT ALL, since source scans are the weakest kind.
# Because the defect it guards is not observable from a recording yet: the
# mirror and the writer agree until a version is minted, and the fork cannot
# mint one until GDH-M5 gives it a reload path. A single-version recording
# therefore cannot distinguish the fixed bundler from the broken one — which is
# exactly why the defect is latent, and why the check has to be structural
# until GDH-M6 can make it behavioural. That limit is stated here rather than
# papered over.
#
# EVERY PATTERN IS ANCHORED TO SYNTAX, NOT VOCABULARY (traps 4 / 4d). The file
# discusses `g_ct_next_path_id` at length in the comment that records why it
# was deleted, so a bare `grep g_ct_next_path_id` would find it and a naive
# "must not contain" check would go red on prose. The checks below match a
# DEFINITION (`^static <type> <name>`) or a CALL (`<name>(`), never a mention.
#
# Usage:  scripts/verify-gdh3-fork.sh
# Exit:   0 iff every check passes.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

SRC=modules/gdscript/gdscript_ct_trace.cpp
HDR=modules/gdscript/ct_writer/include/codetracer_trace_writer.h
ARCH_DIR=modules/gdscript/ct_writer/linuxbsd-x86_64
LIB="$ARCH_DIR/libcodetracer_trace_writer.a"

FALLBACK=modules/gdscript/ct_writer/libcodetracer_trace_writer.a

fails=0
checks=0
ck_fail() { echo "GDH3-FORK-FAIL: $*" >&2; fails=$((fails + 1)); }
ck() { checks=$((checks + 1)); }

for f in "$SRC" "$HDR" "$LIB" "$FALLBACK"; do
  [[ -e "$f" ]] || { echo "GDH3-FORK-FAIL: $f is missing" >&2; exit 1; }
done

echo "== 1. the scan reaches the source (positive control) =="
# Trap 4: assert the scan found something BEFORE asserting what it did not
# find. A pattern that cannot match is indistinguishable from a clean file,
# and every "must not contain" check below would pass over an empty haystack.
ck
if ! grep -qE '^static void gdscript_ct_note_and_bundle_path_locked\(' "$SRC"; then
  ck_fail "the positive control did not match in $SRC, so the scan is not
reading the file and every check below would pass vacuously"
fi
ck
n_static=$(grep -cE '^static ' "$SRC")
if [[ "$n_static" -lt 10 ]]; then
  ck_fail "only $n_static '^static ' lines in $SRC; the scan is not seeing the file"
fi
echo "   control OK ($n_static static definitions seen)"

echo "== 2. the mirror counter is DEFINED nowhere =="
ck
if grep -qE '^\s*static\s+uint64_t\s+g_ct_next_path_id' "$SRC"; then
  ck_fail "g_ct_next_path_id is still DEFINED in $SRC. It mirrors the writer's
private interning counter and is correct only while the writer interns in
first-seen order from 0; trace_writer_register_path_version makes that false,
and from the first reload onward every source view attaches to the wrong file,
silently. Design §6.4: DELETE the mirror, do not adjust it"
fi
ck
if grep -qE 'g_ct_next_path_id\s*\+\+' "$SRC"; then
  ck_fail "g_ct_next_path_id is still INCREMENTED in $SRC"
fi
ck
if grep -qE '^\s*static\s+HashMap<\s*String\s*,\s*uint64_t\s*>\s*g_ct_bundled_path_ids' "$SRC"; then
  ck_fail "g_ct_bundled_path_ids still carries an id column keyed by the path
STRING. A reloaded file has the same string and a new id, so the string-keyed
early return is why the reloaded file's text was never bundled (design §2.2d)"
fi
echo "   the mirror is gone (its explanatory comment is allowed to remain)"

echo "== 3. the bundler ASKS the writer =="
ck
if ! grep -qE 'trace_writer_current_path_id\s*\(' "$SRC"; then
  ck_fail "$SRC never calls trace_writer_current_path_id. The path id a source
view is attached to must come from the writer's own state"
fi
ck
if ! grep -qE 'CT_TW_INVALID_PATH_ID' "$SRC"; then
  ck_fail "$SRC does not check the failure sentinel CT_TW_INVALID_PATH_ID. An
unchecked failure return is a path id of 0xFFFF... handed to
trace_writer_register_source_view"
fi
ck
if ! grep -qE '^\s*static\s+HashSet<\s*uint64_t\s*>\s*g_ct_bundled_path_ids' "$SRC"; then
  ck_fail "the bundled set is not keyed by the writer's path id. Keying it by
the id is what makes a RELOADED file get bundled (new id -> not yet bundled)
while an unchanged file is still bundled only once"
fi
echo "   the bundler takes its id from trace_writer_current_path_id"

echo "== 4. the vendored header declares what the fork needs =="
# The gap GDH-M1 found: the FFI has exported these two since the line-count
# table landed, and this header — the one the fork vendors — declared NEITHER,
# which is the concrete reason GDH-M0 measured meta.dat bit 14 clear.
ck
if ! grep -qE '^void trace_writer_register_step\(' "$HDR"; then
  ck_fail "the header scan's positive control did not match; the scan is blind"
fi
for decl in \
  '^int trace_writer_enable_line_count_table\(' \
  '^int trace_writer_register_path_with_line_count\(' \
  '^uint64_t trace_writer_register_path_version\(' \
  '^uint64_t trace_writer_current_path_id\(' \
  '^void trace_writer_clear_last_error\(' \
  '^#define CT_TW_INVALID_PATH_ID' ; do
  ck
  if ! grep -qE "$decl" "$HDR"; then
    ck_fail "no declaration matching /$decl/ in the vendored $HDR"
  fi
done
echo "   header declares 5 entry points + the failure sentinel"

echo "== 5. the vendored archive EXPORTS them =="
# A declaration the archive cannot satisfy is a link error at best and a
# silently different symbol at worst, so the header check is paired with a
# symbol check over the artifact the link actually consumes.
# The symbol table is captured ONCE into a file and grepped from there,
# never piped into `grep -q`. Under `set -o pipefail` a `grep -q` that exits
# on its first match closes the pipe, `nm` dies of SIGPIPE, and the PIPELINE
# reports 141 — so a symbol that IS present reads as absent, intermittently,
# depending on whether nm had finished writing. That is a harness that
# reports a state it did not reach, and it bit this very script once.
NMOUT="$(mktemp)"
FBOUT="$(mktemp)"
trap 'rm -f "$NMOUT" "$FBOUT"' EXIT
nm "$LIB" >"$NMOUT" 2>/dev/null
ck
if [[ ! -s "$NMOUT" ]]; then
  ck_fail "nm produced NO output for $LIB; every symbol check below would
report 'absent' over an empty haystack"
fi
ck
if ! grep -qE ' T trace_writer_register_step$' "$NMOUT"; then
  ck_fail "the nm positive control did not match in $LIB; the symbol scan is
blind (on a Mach-O host every symbol carries a leading underscore, which is
trap 4a exactly)"
fi
for sym in trace_writer_enable_line_count_table \
           trace_writer_register_path_with_line_count \
           trace_writer_register_path_version \
           trace_writer_current_path_id \
           trace_writer_clear_last_error ; do
  ck
  if ! grep -qE " T $sym\$" "$NMOUT"; then
    ck_fail "$LIB does not export $sym. The vendored archive is stale relative
to the vendored header: refresh both from codetracer-trace-format-nim with
\`just build\`"
  fi
done
echo "   archive exports 5 entry points"

echo "== 6. the OTHER vendored archive (macOS-arm64 fallback) =="
# `modules/gdscript/SCsub` prefers `ct_writer/<platform>-<arch>/` when it
# exists and falls back to `ct_writer/` — which holds the macOS-arm64 build.
# On macOS arm64, this archive IS what the build links against, so missing
# entry points cause a link failure. On a host that cannot refresh it (Linux),
# it continues to report and pass. Anti-vacuity refuses to draw any conclusion
# from an empty or unparsed symbol dump.

HOST_OS="$(uname -s)"
HOST_ARCH="$(uname -m)"
IS_MACOS_ARM64=false
if [[ "$HOST_OS" == "Darwin" && "$HOST_ARCH" == "arm64" ]]; then
  IS_MACOS_ARM64=true
fi

NM_FALLBACK="nm"
if [[ "$HOST_OS" == "Darwin" ]] && [[ -x /usr/bin/nm ]]; then
  NM_FALLBACK="/usr/bin/nm"
elif command -v llvm-nm >/dev/null 2>&1; then
  NM_FALLBACK="llvm-nm"
fi

$NM_FALLBACK "$FALLBACK" >"$FBOUT"
n_fb_tw=$(grep -cE ' (T|D) _?trace_writer_' "$FBOUT" 2>/dev/null || true)

if [[ "$IS_MACOS_ARM64" == "true" ]]; then
  # On macOS arm64, anti-vacuity guard and required entry points check:
  ck
  if [[ ! -s "$FBOUT" ]]; then
    ck_fail "$NM_FALLBACK produced NO output for $FALLBACK; every symbol check below would
report 'absent' over an empty haystack"
  fi
  ck
  if [[ "$n_fb_tw" -lt 40 ]]; then
    ck_fail "only $n_fb_tw trace_writer_* symbols found in $FALLBACK (expected >= 40); archive reader is blind or format not parsed"
  fi

  for sym in trace_writer_enable_line_count_table \
             trace_writer_register_path_with_line_count \
             trace_writer_register_path_version \
             trace_writer_current_path_id \
             trace_writer_clear_last_error \
             trace_writer_set_recording_id ; do
    ck
    if ! grep -qE " (T|D) _?$sym\$" "$FBOUT"; then
      ck_fail "$FALLBACK does not export $sym. The vendored macOS-arm64 archive is stale
relative to the vendored header: refresh from codetracer-trace-format-nim with
\`nix develop --command just build-static-lib\`"
    fi
  done
  if [[ $fails -eq 0 ]]; then
    echo "   macOS-arm64 archive exports all required entry points ($n_fb_tw trace_writer_* symbols verified)"
  fi
else
  # On non-macOS hosts (e.g. Linux), report without failing since host cannot rebuild:
  if [[ ! -s "$FBOUT" || "$n_fb_tw" -lt 40 ]]; then
    echo "   NOTE (not a failure on this host): $NM_FALLBACK produced no usable symbol dump for $FALLBACK."
    echo "   Mach-O archive inspection is unsupported on $HOST_OS $HOST_ARCH without a cross-target reader."
  elif grep -qE " (T|D) _?trace_writer_current_path_id\$" "$FBOUT" && \
       grep -qE " (T|D) _?trace_writer_register_path_version\$" "$FBOUT" && \
       grep -qE " (T|D) _?trace_writer_set_recording_id\$" "$FBOUT"; then
    echo "   fallback archive also exports the new entry points"
  else
    echo "   NOTE (not a failure on this host): $FALLBACK does NOT export"
    echo "   the required entry points. It is the macOS-arm64 build and can"
    echo "   only be refreshed on a macOS host. A macOS build of the fork will"
    echo "   FAIL TO LINK until it is regenerated from"
    echo "   codetracer-trace-format-nim with \`nix develop --command just build-static-lib\`."
  fi
fi

echo
echo "======================================================"
echo "checks run: $checks"
echo "failures:   $fails"
if [[ $fails -ne 0 ]]; then
  exit 1
fi
# The count is written from a run, not read off the source (trap 4c): a check
# that skipped a loop iteration cannot reach the end with the right number.
expected_checks=22
if [[ "$IS_MACOS_ARM64" == "true" ]]; then
  expected_checks=30
fi
if [[ $checks -ne $expected_checks ]]; then
  echo "GDH3-FORK-FAIL: ran $checks checks, expected $expected_checks. A count that moved" >&2
  echo "means a check was skipped or added; reconcile it deliberately." >&2
  exit 1
fi
if [[ "$IS_MACOS_ARM64" == "true" ]]; then
  echo "GDH-M3 (fork side): the mirror is gone, the bundler asks the writer,"
  echo "                    and the vendored header + macOS archive can answer."
else
  echo "GDH-M3 (fork side): the mirror is gone, the bundler asks the writer,"
  echo "                    and the vendored header + Linux archive can answer."
fi
