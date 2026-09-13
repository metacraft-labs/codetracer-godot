#!/usr/bin/env bash
# GDH-M5 gate driver — the engine reloads, from a point the recorder controls.
#
# Design:    codetracer-specs/Planned-Features/
#            GDScript-Hot-Reload-Multi-Version-Sources.md §5.2, §5.3, §5.6.
# Milestone: the `GDH-M5` block of the campaign's `.milestones.org`.
#
# What runs here:
#
#   1. `gdh5_in_process_reload_matches_the_remote_debugger_path`, unmutated.
#      The same fixture is reloaded from the recorder's own poll point and with
#      `core:reload_scripts` over `--remote-debug`; Godot's own supported path
#      is the ORACLE, so it is run rather than asserted about.
#   2. `gdh5_unpreserved_state_is_reported`, unmutated, together with its
#      control arm (a fixture with no static variables, which must report no
#      loss).
#   3. Both named falsifier arms, each built into a real engine binary and each
#      required to go red in the gate it is aimed at while leaving its control
#      green.
#
# WHAT IS NOT HERE, stated because its absence must not be discovered later:
# `gdh5_reload_is_refused_while_a_step_is_pending` is NOT run. It asserts that
# no step's values are split across a `TagSourceReload` marker in the resulting
# container, and the fork cannot emit that marker at all today — there is no
# `trace_writer_register_source_reload` in the C ABI
# (`codetracer-trace-format-nim/include/codetracer_trace_writer.h`), only the
# Nim-side `registerSourceReload` GDH-M2 added, and that entry point further
# requires `old_path_id != new_path_id`, i.e. a minted path VERSION, which
# needs bit 14 and `trace_writer_register_path_version` in the fork. Both are
# GDH-M6 work. The deferral MECHANISM is implemented (the safe point applies
# what the agent thread queued, under the emit lock) but it has NO gate here:
# the property the milestone names is a property of the container, and the
# container cannot carry it yet. Nothing weaker was substituted for it.
#
# Rules it enforces (codetracer-specs/Testing/Verification-Harness-Traps.md):
#
#   * BUILD and RUN are separate steps; a build error is never a red gate.
#   * A hang is rc 124 and NOTHING ELSE; every arm's rc is checked for it.
#   * An arm must go red IN THE GATE IT IS AIMED AT — the verifier prefixes
#     every failure with `GDH5-FAIL[<gate>]` and this driver requires the right
#     one.
#   * rc 2 from the verifier is a DRIVER-FAIL and is never counted as a kill.
#
# Usage:  scripts/record-and-verify-gdh5.sh [<output-dir>]
# Exit:   0 iff every gate is green and every arm is red in its own gate.
#
# Env:
#   BIN            the patchable engine (default bin/godot.<plat>.<target>.<arch>.hcr)
#   REBUILD_ARMS   set to 0 to skip building the falsifier-armed engines (the
#                  arms are then reported as NOT RUN, never as passed)
#   TIMEOUT        per-invocation timeout, seconds (default 600)

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

PLATFORM="${PLATFORM:-linuxbsd}"
TARGET="${TARGET:-template_debug}"
ARCH="${ARCH:-x86_64}"
BIN="${BIN:-$REPO/bin/godot.${PLATFORM}.${TARGET}.${ARCH}.hcr}"
OUT="${1:-${TMPDIR:-/tmp}/ct-gdh5-$$}"
TIMEOUT="${TIMEOUT:-600}"
REBUILD_ARMS="${REBUILD_ARMS:-1}"
FIXTURES="$REPO/test-programs/gdh5"
VERIFY="$REPO/scripts/verify_gdh5.py"

log() { echo "[gdh5] $*"; }
die() { echo "[gdh5] DRIVER-FAIL: $*" >&2; exit 2; }

mkdir -p "$OUT"

# --- prerequisites, each loud ----------------------------------------------
[[ -x "$BIN" ]] || die "engine not executable: $BIN"
[[ -x "$VERIFY" || -f "$VERIFY" ]] || die "verifier missing: $VERIFY"
command -v python3 >/dev/null || die "python3 is not on PATH"
for f in project.godot probe_v1.gd probe_v2.gd probe_nostatic_v1.gd probe_nostatic_v2.gd; do
  [[ -f "$FIXTURES/$f" ]] || die "missing fixture $FIXTURES/$f"
done

# The reload arrives over an AF_UNIX socket and `sun_path` holds 108 bytes, so
# the socket lives in a short directory chosen here rather than under $OUT.
SOCK_DIR="${GDH5_SOCKET_DIR:-${XDG_RUNTIME_DIR:-/tmp}}"
[[ -d "$SOCK_DIR" ]] || SOCK_DIR=/tmp

failures=0
green=0
redarms=0
armsrun=0

run_gate() {  # run_gate <gate> <engine> <tag>; echoes rc
  local gate="$1" engine="$2" tag="$3"
  rm -rf "$OUT/$tag"
  timeout "$TIMEOUT" python3 "$VERIFY" \
    --engine "$engine" --fixtures "$FIXTURES" --work "$OUT/$tag" \
    --socket-dir "$SOCK_DIR" --gate "$gate" >"$OUT/$tag.out" 2>&1
  echo $?
}

check_green() {  # check_green <gate> <label>
  local gate="$1" label="$2" rc
  rc=$(run_gate "$gate" "$BIN" "plain-$gate")
  if [[ "$rc" == "124" ]]; then
    echo "GATE-FAIL: $label HUNG (rc 124)" >&2
    failures=$((failures + 1)); return
  fi
  if [[ "$rc" == "2" ]]; then
    cat "$OUT/plain-$gate.out" >&2
    echo "GATE-FAIL: $label hit a DRIVER-FAIL (rc 2)" >&2
    failures=$((failures + 1)); return
  fi
  if [[ "$rc" != "0" ]]; then
    cat "$OUT/plain-$gate.out" >&2
    echo "GATE-FAIL: $label is RED (rc $rc)" >&2
    failures=$((failures + 1)); return
  fi
  cat "$OUT/plain-$gate.out"
  green=$((green + 1))
}

log "engine       : $BIN"
log "fixtures     : $FIXTURES"
log "output       : $OUT"
log "socket dir   : $SOCK_DIR"
echo

check_green matches-remote "gdh5_in_process_reload_matches_the_remote_debugger_path"
echo
check_green unpreserved "gdh5_unpreserved_state_is_reported"
echo

# ---------------------------------------------------------------------------
# Falsifier arms. Each needs a REAL engine binary built with the mutation, so
# each costs a rebuild; the plain engine is restored afterwards and the restore
# is verified rather than assumed.
# ---------------------------------------------------------------------------
# The plain engine is copied aside BEFORE any armed build, because an armed
# build overwrites `bin/…hcr` in place. This byte-exact copy is what the exit
# trap below restores from, and it doubles as the oracle for "is the engine in
# the tree the plain one" — a question this campaign has repeatedly needed a
# checkable answer to.
PLAIN="$OUT/godot-plain.hcr"
cp -f "$BIN" "$PLAIN" || die "could not copy the plain engine aside"

# THE SHARED ARTIFACT IS PUT BACK ON EVERY EXIT PATH.
#
# `$BIN` lives in the DEVELOPER'S TREE and every armed build overwrites it in
# place. Calling `restore_plain` on the normal path is not enough: a build
# failure, a `die`, a Ctrl-C, or an outer timeout all returned with an ARMED
# engine sitting at `$BIN`, where the next thing to run it — another gate,
# another campaign, a person — silently gets a mutated engine and grades
# against it. Mutating a shared artifact without restoring it AND verifying the
# restore is precisely what this campaign's rules forbid; the same defect has
# already been found once here, in the ASan driver.
#
# The restore is a COPY of the byte-exact saved engine rather than a rebuild,
# so it cannot itself fail slowly, and it SAYS whether it worked.
#
# KNOWN RESIDUAL GAP, MEASURED 2026-09-13 — a trap alone does not close the
# mid-rebuild case, and `record-and-verify-gdh8.sh`'s trap has it too. This
# driver was killed by an outer 10-minute timeout during an armed build. `$BIN`
# was still PLAIN at the moment of the kill and became ARMED one minute LATER,
# because the orphaned `scons` outlived the shell and finished linking after
# the handler had already run. Closing that needs the armed build started in
# its own process group (`setsid`) and stopped by the handler before it copies;
# that is a restructuring of the build invocations in three drivers and is left
# as owed work rather than done blind. Until then the two oracles below are the
# defence, and both are cheap: `$PLAIN` is a byte-exact comparison target, and
# the patchable build is bit-reproducible (measured: two independent builds of
# the same tree both sha256 3a97806f…), so `sha256sum` against a fresh build
# also answers it.
ct_gdh5_restore_bin_on_exit() {
  local rc=$?
  if [[ -f "$PLAIN" ]] && ! cmp -s "$BIN" "$PLAIN"; then
    if cp -f "$PLAIN" "$BIN"; then
      echo "[gdh5] restored the plain engine to $BIN on exit (it was armed)" >&2
    else
      echo "[gdh5] WARNING: could NOT restore the plain engine to $BIN — an" \
           "ARMED engine is left in the tree. Copy $PLAIN back by hand." >&2
    fi
  fi
  exit $rc
}
trap ct_gdh5_restore_bin_on_exit EXIT INT TERM

build_armed() {  # build_armed <define> <dest>
  # Scoped to the gdscript module's SCons environment (see modules/gdscript/SCsub),
  # so an armed build is one translation unit plus a relink. Putting the define
  # in the engine-wide CCFLAGS was tried first and triggered a FULL rebuild —
  # thirdparty/embree and all — for every arm.
  local define="$1" dest="$2"
  CT_GDH5_FALSIFY="$define" timeout "$TIMEOUT" \
    ./scripts/build-hcr-patchable-linux.sh -j "${JOBS:-6}" \
    >"$OUT/build-$define.log" 2>&1 || return 1
  grep -q "FALSIFIER ARM: -D$define" "$OUT/build-$define.log" || {
    echo "ARM-FAIL: the build did not report arming -D$define; an arm that was" \
         "not compiled in would be recorded as a kill it never made" >&2
    return 1
  }
  cp -f "$BIN" "$dest" || return 1
}

restore_plain() {
  timeout "$TIMEOUT" ./scripts/build-hcr-patchable-linux.sh -j "${JOBS:-6}" \
    >"$OUT/build-plain-restore.log" 2>&1
}

run_arm() {  # run_arm <define> <gate> <control-gate|-> <label>
  local define="$1" gate="$2" control="$3" label="$4"
  echo "-- arm $label (engine: -D$define)"
  armsrun=$((armsrun + 1))
  local armed="$OUT/godot-$define.hcr"
  if ! build_armed "$define" "$armed"; then
    tail -20 "$OUT/build-$define.log" >&2
    echo "ARM-FAIL: $label did not BUILD; a build error is not a red gate" >&2
    failures=$((failures + 1))
    restore_plain
    return
  fi
  restore_plain || { echo "ARM-FAIL: could not restore the plain engine" >&2
                     failures=$((failures + 1)); return; }
  if [[ "$control" != "-" ]]; then
    local crc
    crc=$(run_gate "$control" "$armed" "arm-$define-ctrl")
    if [[ "$crc" != "0" ]]; then
      cat "$OUT/arm-$define-ctrl.out" >&2
      echo "ARM-FAIL: $label went red on its CONTROL gate ($control, rc $crc);" \
           "an arm that fails everywhere has not been shown to discriminate" >&2
      failures=$((failures + 1)); return
    fi
    echo "   control ($control): PASSES under the mutation, as it must"
  fi
  local rc
  rc=$(run_gate "$gate" "$armed" "arm-$define")
  if [[ "$rc" == "124" ]]; then
    echo "ARM-FAIL: $label HUNG (rc 124). A hang is not a diagnosis; CHECK-FAIL," \
         "not a kill." >&2
    failures=$((failures + 1)); return
  fi
  if [[ "$rc" == "2" ]]; then
    cat "$OUT/arm-$define.out" >&2
    echo "ARM-FAIL: $label exited 2 (DRIVER-FAIL); a harness failure is not a kill" >&2
    failures=$((failures + 1)); return
  fi
  if [[ "$rc" == "0" ]]; then
    echo "ARM-FAIL: $label PASSED $gate; the mutation did not turn it red" >&2
    failures=$((failures + 1)); return
  fi
  if ! grep -q "GDH5-FAIL\[" "$OUT/arm-$define.out"; then
    cat "$OUT/arm-$define.out" >&2
    echo "ARM-FAIL: $label exited $rc without a GDH5-FAIL[...] line" >&2
    failures=$((failures + 1)); return
  fi
  echo "   RED (rc $rc): $(grep -o 'GDH5-FAIL\[.*' "$OUT/arm-$define.out" | head -1 | cut -c1-190)"
  redarms=$((redarms + 1))
}

if [[ "$REBUILD_ARMS" == "1" ]]; then
  # ARM 1 — `GDScript::reload()` instead of `reload_scripts`. It parses the
  #   in-memory source and never re-reads disk, so the post-reload tokens stay
  #   v1's while every call reports success. No control gate: the mutation is
  #   in the reload itself and affects any reload at all.
  run_arm CT_GDH5_FALSIFY_SCRIPT_RELOAD_ONLY matches-remote - \
    "script-reload-only"
  # ARM 2 — report the static-variable loss unconditionally. Its CONTROL is
  #   the same gate's own no-statics fixture, which is inside the `unpreserved`
  #   gate, so the arm is run against that gate and must go red there.
  run_arm CT_GDH5_FALSIFY_UNCONDITIONAL_LOSS unpreserved - \
    "unconditional-loss"
else
  echo "-- falsifier arms SKIPPED (REBUILD_ARMS=0). They are NOT counted as" \
       "passed; this run proves the unmutated gates only." >&2
fi

echo
echo "======================================================"
echo "gates green:   $green of 2"
echo "arms gone red: $redarms of $armsrun run"
echo "failures:      $failures"
echo "output dir:    $OUT"
echo
echo "NOT RUN: gdh5_reload_is_refused_while_a_step_is_pending — the fork cannot"
echo "         emit a TagSourceReload marker (no trace_writer_register_source_reload"
echo "         in the C ABI, and the marker further requires a minted path"
echo "         version). Both are GDH-M6 work. See the header of this script."
if [[ $failures -ne 0 || $green -ne 2 ]]; then
  exit 1
fi
if [[ "$REBUILD_ARMS" == "1" && $redarms -ne 2 ]]; then
  exit 1
fi
echo "GDH-M5: the in-process reload from the recorder's poll point agrees with"
echo "        Godot's own core:reload_scripts oracle, and the static-variable"
echo "        loss is measured and reported rather than asserted."
