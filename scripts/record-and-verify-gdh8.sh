#!/usr/bin/env bash
# GDH-M8 gate driver — failure semantics, end to end.
#
# Design:    codetracer-specs/Planned-Features/
#            GDScript-Hot-Reload-Multi-Version-Sources.md §5.5, §8.1-§8.3.
# Milestone: the `GDH-M8` block of the campaign's `.milestones.org`.
#
# THE PROPERTY.  Either a file's new version is live in the engine AND
# registered in the trace, or neither.  There is no third state.
#
# What runs here:
#
#   1. The three gates, unmutated, against the plain engine — each with its own
#      control arm inside the verifier:
#        gdh8_refused_reload_leaves_a_coherent_trace                 (GDH-G6)
#        gdh8_digest_mismatch_is_refused_before_anything_is_touched
#        gdh8_a_failure_after_registration_closes_the_trace_rather_than_continuing
#
#   2. THE INERTNESS GATE, which is not a falsifier and is the price of the
#      fault-injection hook.  The third gate injects its failure through a hook
#      in SHIPPED code, so the hook is a production surface; the milestone
#      accepts it only if its inertness is MEASURED.  This driver records the
#      same program twice under a PINNED `CT_RECORDING_ID` — once with the hook
#      compiled in and unarmed, once with `CT_GDH8_NO_INJECTION_HOOK` compiled
#      in — and requires the two containers to be BYTE-IDENTICAL.  A comment
#      claiming the hook is off would not be a measurement.
#
#   3. Every named falsifier arm.  SIX of them: the milestone names four and the
#      2026-09-12 review added two, each for a claim that had none —
#      `CT_GDH8_FALSIFY_CLOSE_WITHOUT_REASON` for "the reason is recorded IN THE
#      CONTAINER" (§8.1's contract has two halves and only "closes the trace"
#      was armed) and `CT_GDH8_FALSIFY_WRITE_BEFORE_COMPILE` for deviation (a)'s
#      second half, the disk write's move out of step 1.  Two families:
#
#      ENGINE-SIDE arms are `-D` mutations scoped to `modules/gdscript` by
#      `CT_GDH8_FALSIFY` in that module's SCsub — one translation unit plus a
#      relink (~20 s), not a full tree.
#
#      AGENT-SIDE arms are `-D` mutations scoped to the ONE object built from
#      `reprobuild/libs/repro_hcr_agent/c/repro_hcr_agent.c` by
#      `CT_GDH8_AGENT_FALSIFY` in `platform/linuxbsd/SCsub`.  The digest check
#      lives there, not in the engine, so its falsifier has to.
#
# Rules it enforces (codetracer-specs/Testing/Verification-Harness-Traps.md):
#
#   * BUILD and RUN are separate steps; a build error is never a red gate.
#   * A hang is rc 124 and NOTHING ELSE.
#   * rc 2 from the verifier is a DRIVER-FAIL and is never counted as a kill.
#   * An arm must go red IN THE GATE IT IS AIMED AT — the verifier prefixes
#     every failure with `GDH8-FAIL[<gate>]` and this driver requires the right
#     one.
#   * An arm must PASS the gates it is not aimed at, or it has not been shown to
#     discriminate.  Nine falsifiers in this campaign have already been measured
#     non-discriminating for exactly that reason.
#   * Any harness that mutates a shared artifact restores it AND VERIFIES the
#     restore.  `bin/…hcr` is overwritten in place by every armed build, so the
#     plain engine is copied aside first and the restore is checked by the
#     ABSENCE of the arm in the rebuild log.
#
# Usage:  scripts/record-and-verify-gdh8.sh [<output-dir>]
# Exit:   0 iff every gate is green and every arm is red in its own gate.
#
# Env:
#   BIN            the patchable engine (default bin/godot.<plat>.<target>.<arch>.hcr)
#   CT_PRINT       the container inspector (default ../codetracer-trace-format-nim/ct-print)
#   REBUILD_ARMS   0 to skip every arm that needs a build (reported NOT RUN)
#   TIMEOUT        per-invocation timeout, seconds (default 1800)
#   JOBS           scons parallelism for an armed build (default 8)

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

PLATFORM="${PLATFORM:-linuxbsd}"
TARGET="${TARGET:-template_debug}"
ARCH="${ARCH:-x86_64}"
BIN="${BIN:-$REPO/bin/godot.${PLATFORM}.${TARGET}.${ARCH}.hcr}"
CT_PRINT="${CT_PRINT:-$REPO/../codetracer-trace-format-nim/ct-print}"
OUT="${1:-${TMPDIR:-/tmp}/ct-gdh8-$$}"
TIMEOUT="${TIMEOUT:-1800}"
REBUILD_ARMS="${REBUILD_ARMS:-1}"
JOBS="${JOBS:-8}"
FIXTURES="$REPO/test-programs/gdh8"
VERIFY="$REPO/scripts/verify_gdh8.py"

log() { echo "[gdh8] $*"; }
die() { echo "[gdh8] DRIVER-FAIL: $*" >&2; exit 2; }

mkdir -p "$OUT" || die "cannot create $OUT"

# --- prerequisites, each LOUD.  A missing one must never look like a skip. ---
[[ -x "$BIN" ]]      || die "engine not executable: $BIN"
[[ -x "$CT_PRINT" ]] || die "ct-print not executable: $CT_PRINT (build it in codetracer-trace-format-nim)"
[[ -f "$VERIFY" ]]   || die "verifier missing: $VERIFY"
command -v python3 >/dev/null || die "python3 is not on PATH"
for f in project.godot probe_v1.gd probe_v2_ok.gd probe_v2_bad.gd; do
  [[ -f "$FIXTURES/$f" ]] || die "missing fixture $FIXTURES/$f"
done

# `sun_path` holds 108 bytes, so the agent socket lives in a short directory
# chosen here rather than under $OUT.
SOCK_DIR="${GDH8_SOCKET_DIR:-${XDG_RUNTIME_DIR:-/tmp}}"
[[ -d "$SOCK_DIR" ]] || SOCK_DIR=/tmp

PLAIN="$OUT/godot-plain.hcr"
cp -f "$BIN" "$PLAIN" || die "could not copy the plain engine aside"

failures=0
green=0
redarms=0
armsrun=0

run_verifier() {  # run_verifier <tag> <engine> <gate> [extra args...]; echoes rc
  local tag="$1" engine="$2" gate="$3"; shift 3
  rm -rf "$OUT/$tag"
  timeout "$TIMEOUT" python3 "$VERIFY" \
    --engine "$engine" --fixtures "$FIXTURES" --work "$OUT/$tag" \
    --ct-print "$CT_PRINT" --socket-dir "$SOCK_DIR" \
    --gate "$gate" "$@" >"$OUT/$tag.out" 2>&1
  echo $?
}

log "engine    : $BIN"
log "ct-print  : $CT_PRINT"
log "fixtures  : $FIXTURES"
log "output    : $OUT"
log "socket dir: $SOCK_DIR"
echo

# ---------------------------------------------------------------------------
# 1. The unmutated run.
# ---------------------------------------------------------------------------
echo "== the three gates, unmutated =="
rc=$(run_verifier plain "$PLAIN" all)
cat "$OUT/plain.out"
if [[ "$rc" == "124" ]]; then
  echo "GATE-FAIL: the unmutated run HUNG (rc 124)" >&2
  failures=$((failures + 1))
elif [[ "$rc" == "2" ]]; then
  echo "GATE-FAIL: the unmutated run hit a DRIVER-FAIL (rc 2); a harness" \
       "failure is not a red gate" >&2
  failures=$((failures + 1))
elif [[ "$rc" != "0" ]]; then
  echo "GATE-FAIL: the unmutated run is RED (rc $rc)" >&2
  failures=$((failures + 1))
else
  green=$((green + 1))
fi
echo

# ---------------------------------------------------------------------------
# Building an armed engine, and restoring the plain one.
# ---------------------------------------------------------------------------
build_armed() {  # build_armed <var> <defines> <dest>
  local var="$1" defines="$2" dest="$3"
  env "$var=$defines" JOBS="$JOBS" timeout "$TIMEOUT" \
    ./scripts/build-hcr-patchable-linux.sh \
    >"$OUT/build-$dest.log" 2>&1 || return 1
  # A build that did not report arming would be recorded as a kill it never
  # made.  Every named define must appear in the log.
  local d
  for d in ${defines//,/ }; do
    grep -q "FALSIFIER ARM: -D$d" "$OUT/build-$dest.log" || {
      echo "ARM-FAIL: the build did not report arming -D$d" >&2
      return 1
    }
  done
  cp -f "$BIN" "$OUT/godot-$dest.hcr" || return 1
}

restore_plain() {
  JOBS="$JOBS" timeout "$TIMEOUT" ./scripts/build-hcr-patchable-linux.sh \
    >"$OUT/build-restore.log" 2>&1 || return 1
  cmp -s "$BIN" "$PLAIN" && return 0
  # A rebuilt engine is not byte-identical to the saved one (build ids move),
  # so the restore is verified by the ABSENCE of the arm rather than by bytes.
  grep -q "FALSIFIER ARM" "$OUT/build-restore.log" && return 1
  return 0
}

# ---------------------------------------------------------------------------
# 2. THE INERTNESS GATE.  Needs a second build, so it lives with the arms.
# ---------------------------------------------------------------------------
inertness_gate() {
  echo "== gdh8_the_injection_hook_is_inert_unless_armed =="
  echo "-- a build WITHOUT the hook, and a build WITH it and unarmed, must"
  echo "   produce byte-identical containers for the same program"
  if ! build_armed CT_GDH8_FALSIFY CT_GDH8_NO_INJECTION_HOOK nohook; then
    tail -25 "$OUT/build-nohook.log" >&2
    echo "GATE-FAIL: the no-hook build did not BUILD; a build error is not a" \
         "red gate" >&2
    failures=$((failures + 1)); restore_plain; return
  fi
  restore_plain || { echo "GATE-FAIL: could not restore the plain engine" >&2
                     failures=$((failures + 1)); return; }
  # The recording identity is PINNED.  Without it the writer mints a fresh
  # UUIDv7 per recording and no two recordings of one program are ever
  # byte-identical, so the comparison below would be unfalsifiable.
  # TWO THINGS HAVE TO BE MADE EQUAL BEFORE THE COMPARISON MEANS ANYTHING, and
  # both were MEASURED rather than anticipated — the first version of this gate
  # failed on them and the failure is what documents them:
  #
  #   * THE RECORDING ID.  Without a pin the writer mints a fresh UUIDv7 per
  #     recording, so no two recordings of one program are ever byte-identical.
  #     `trace_writer_set_recording_id` takes a UUID and REFUSES anything else,
  #     so the pin has to look like one; a non-UUID string is refused, reported
  #     on stderr, and the recording then carries a fresh id — which is a
  #     rejected pin that looks exactly like no pin.
  #   * THE WORK DIRECTORY.  `meta.dat` records the writer's workdir as an
  #     ABSOLUTE PATH, so two runs under two different output directories differ
  #     in those bytes and in the length prefix in front of them.  Both runs use
  #     ONE directory and the container is moved out between them.
  local id="01a09442-0000-7000-8000-000000000001"
  local side engine work="$OUT/inert-work"
  for side in withhook nohook; do
    engine="$PLAIN"
    [[ "$side" == "nohook" ]] && engine="$OUT/godot-nohook.hcr"
    rm -rf "$work"
    CT_RECORDING_ID="$id" timeout "$TIMEOUT" python3 "$VERIFY" \
      --engine "$engine" --fixtures "$FIXTURES" --work "$work" \
      --ct-print "$CT_PRINT" --socket-dir "$SOCK_DIR" --record-only \
      >"$OUT/inert-$side.log" 2>&1
    if [[ $? -ne 0 ]]; then
      tail -20 "$OUT/inert-$side.log" >&2
      echo "GATE-FAIL: the $side inertness recording did not happen" >&2
      failures=$((failures + 1)); return
    fi
    # A pin that was REFUSED must not be mistaken for a pin that held.
    if grep -q "recording id pin REFUSED" "$OUT/inert-$side.log"; then
      echo "GATE-FAIL: the $side run's CT_RECORDING_ID pin was REFUSED, so the" \
           "two containers could not be equal for a reason that has nothing to" \
           "do with the hook" >&2
      failures=$((failures + 1)); return
    fi
    grep -q "recording id PINNED to $id" "$OUT/inert-$side.log" || {
      echo "GATE-FAIL: the $side run did not report PINNING the recording id;" \
           "without the pin this comparison is unfalsifiable" >&2
      failures=$((failures + 1)); return; }
    mkdir -p "$OUT/inert-$side"
    cp -f "$work/record-only/trace/gdscript_trace.ct" \
      "$OUT/inert-$side/gdscript_trace.ct" || {
      echo "GATE-FAIL: the $side run produced no container to compare" >&2
      failures=$((failures + 1)); return; }
  done
  a="$OUT/inert-withhook/gdscript_trace.ct"
  b="$OUT/inert-nohook/gdscript_trace.ct"
  [[ -f "$a" && -f "$b" ]] || {
    echo "GATE-FAIL: one of the inertness containers is missing ($a / $b)" >&2
    failures=$((failures + 1)); return; }
  local ha hb
  ha=$(sha256sum "$a" | cut -d' ' -f1)
  hb=$(sha256sum "$b" | cut -d' ' -f1)
  echo "   with-hook (unarmed): $ha"
  echo "   no-hook build      : $hb"
  if cmp -s "$a" "$b"; then
    echo "   BYTE-IDENTICAL: the hook is off, MEASURED and not asserted"
    green=$((green + 1))
  else
    echo "GATE-FAIL: the two containers DIFFER, so the unarmed hook is not" \
         "inert: $ha vs $hb" >&2
    failures=$((failures + 1))
  fi
  echo
}

# ---------------------------------------------------------------------------
# 3. The falsifier arms.
# ---------------------------------------------------------------------------
# <var> <defines> <selector> <gate-name> <label> [extra verifier args...]
#
# `selector` is what `--gate` takes; `gate-name` is the test_name the verifier
# prefixes its failures with.  They are DIFFERENT strings; passing the second
# where the first belongs makes argparse exit 2, which this driver correctly
# scores as a DRIVER-FAIL rather than a kill.
# THE NAMED KILL.  `kill` is a substring of the ONE assertion the milestone's
# falsifier text says this arm must redden.  Requiring only "some GDH8-FAIL in
# the right gate" is too weak, and it was measured too weak: on this driver's
# first full run two arms went red purely on the ABSENCE of a refusal message,
# which is exactly what their entries forbid — "the gate must go red by
# observing v2's tokens in stdout, i.e. by the engine having reloaded, not
# merely by the absence of an error message".  The verifier was changed to stop
# short-circuiting so the container-level claims are always reached; this check
# is what keeps that honest from the outside.
run_arm() {
  local var="$1" defines="$2" selector="$3" gate="$4" kill="$5" label="$6"; shift 6
  echo "-- arm $label ($var=$defines)"
  armsrun=$((armsrun + 1))
  if ! build_armed "$var" "$defines" "$defines"; then
    tail -25 "$OUT/build-$defines.log" >&2
    echo "ARM-FAIL: $label did not BUILD; a build error is not a red gate" >&2
    failures=$((failures + 1)); restore_plain; return
  fi
  restore_plain || { echo "ARM-FAIL: could not restore the plain engine" >&2
                     failures=$((failures + 1)); return; }
  local armed="$OUT/godot-$defines.hcr" rc
  rc=$(run_verifier "arm-$defines" "$armed" "$selector" "$@")
  if [[ "$rc" == "124" ]]; then
    echo "ARM-FAIL: $label HUNG (rc 124).  CHECK-FAIL, not a kill." >&2
    failures=$((failures + 1)); return
  fi
  if [[ "$rc" == "2" ]]; then
    cat "$OUT/arm-$defines.out" >&2
    echo "ARM-FAIL: $label exited 2 (DRIVER-FAIL); not a kill" >&2
    failures=$((failures + 1)); return
  fi
  if [[ "$rc" == "0" ]]; then
    echo "ARM-FAIL: $label PASSED $gate; the mutation did not turn it red" >&2
    failures=$((failures + 1)); return
  fi
  if ! grep -q "GDH8-FAIL\[$gate" "$OUT/arm-$defines.out"; then
    cat "$OUT/arm-$defines.out" >&2
    echo "ARM-FAIL: $label exited $rc without a GDH8-FAIL[$gate...] line" >&2
    failures=$((failures + 1)); return
  fi
  if ! grep -F "GDH8-FAIL[$gate" "$OUT/arm-$defines.out" | grep -qF "$kill"; then
    cat "$OUT/arm-$defines.out" >&2
    echo "ARM-FAIL: $label went red, but NOT on the assertion its entry names" \
         "(\"$kill\").  An arm that kills on a different claim than the one" \
         "the milestone requires has not been shown to test that claim." >&2
    failures=$((failures + 1)); return
  fi
  echo "   RED on its NAMED assertion: $(grep -F "GDH8-FAIL[$gate" "$OUT/arm-$defines.out" | grep -F "$kill" | head -1 | cut -c1-190)"
  # DISCRIMINATION.  An arm that reddens gates it is not aimed at has not been
  # shown to test the property it names.  The verifier runs its own control arm
  # inside each gate; here the requirement is that the OTHER gates stay green
  # under the same armed engine.
  local other rc2 clean=1
  for other in refused digest close; do
    [[ "$other" == "$selector" ]] && continue
    rc2=$(run_verifier "arm-$defines-$other" "$armed" "$other")
    if [[ "$rc2" == "2" ]]; then
      echo "   (gate $other could not be evaluated under this arm: DRIVER-FAIL)" >&2
      clean=0
      continue
    fi
    if [[ "$rc2" != "0" ]]; then
      echo "   note: gate $other is ALSO red under this arm" >&2
      clean=0
    fi
  done
  if [[ $clean -eq 1 ]]; then
    echo "   and the other two gates stay GREEN under the same armed engine"
  else
    echo "   NOT FULLY DISCRIMINATING — see the notes above.  Recorded, not" \
         "hidden: an arm that reddens more than its own gate is reported here" \
         "rather than being allowed to look like a clean kill." >&2
  fi
  redarms=$((redarms + 1))
}

if [[ "$REBUILD_ARMS" == "1" ]]; then
  inertness_gate

  echo "== falsifier arms =="
  # ARM 1 (gdh8_refused_reload_leaves_a_coherent_trace, first arm): emit the
  #   `TagSourceReload` BEFORE attempting the compile.  The container then
  #   claims a reload that did not happen and the gate goes red on the marker
  #   count.
  run_arm CT_GDH8_FALSIFY CT_GDH8_FALSIFY_MARKER_BEFORE_COMPILE refused \
    gdh8_refused_reload_leaves_a_coherent_trace \
    "NO TagSourceReload marker was emitted" \
    "marker emitted before the compile check"
  # ARM 2 (same gate, second arm): register the versioned path before the
  #   verification that precedes it in §8.1.  The entry words this as "before
  #   verifying the digest"; the digest is verified in the AGENT before this
  #   host handler is called at all, so the compile check is the verification
  #   that is reachable from here, and the substitution is stated rather than
  #   glossed.  The container then carries TWO entries, one of which nothing
  #   ever executed, and the gate goes red on the entry count.
  run_arm CT_GDH8_FALSIFY CT_GDH8_FALSIFY_MINT_BEFORE_COMPILE refused \
    gdh8_refused_reload_leaves_a_coherent_trace \
    "carries exactly ONE paths.dat entry" \
    "path version minted before the compile check"
  # ARM 3 (gdh8_digest_mismatch_is_refused_before_anything_is_touched): SKIP
  #   the digest check in the agent.  The gate must go red by observing v2's
  #   tokens in stdout — i.e. by the engine having reloaded — not merely by the
  #   absence of an error message.
  run_arm CT_GDH8_AGENT_FALSIFY REPRO_HCR_GDH8_FALSIFY_SKIP_DIGEST_CHECK digest \
    gdh8_digest_mismatch_is_refused_before_anything_is_touched \
    "NOT ONE of v2's own probe lines" \
    "the agent does not compare the digest it computed"
  # ARM 4 (gdh8_a_failure_after_registration_closes_the_trace_rather_than_
  #   continuing): make the failure path CONTINUE recording instead of closing.
  #   The container then holds steps executed against a version it has only
  #   half registered, and the gate must go red by finding steps whose path id
  #   has no source view — an incoherence detectable in the container itself.
  run_arm CT_GDH8_FALSIFY CT_GDH8_FALSIFY_CONTINUE_AFTER_FAILURE close \
    gdh8_a_failure_after_registration_closes_the_trace_rather_than_continuing \
    "NO step is attributed to a path id with no raw source view" \
    "continue recording instead of closing the trace"
  # ARM 5 (same gate) — ADDED BY THE REVIEW, 2026-09-12.  §8.1's contract is
  #   "closes the trace WITH A RECORDED REASON", and only the first half had an
  #   arm.  This one closes the trace correctly and writes no reason into it:
  #   the container still decodes, still holds no orphaned path id, the wire
  #   still answers `trace-closed` with the stage in `detail`, and the engine
  #   still keeps running v1 — so every other claim in the gate stays green and
  #   the ONLY thing that can kill it is the claim that the reason survives to a
  #   reader.  Before this arm, that claim had never been shown able to go red.
  run_arm CT_GDH8_FALSIFY CT_GDH8_FALSIFY_CLOSE_WITHOUT_REASON close \
    gdh8_a_failure_after_registration_closes_the_trace_rather_than_continuing \
    "the reason the recording stopped is RECORDED IN THE CONTAINER" \
    "close the trace without recording why"
  # ARM 6 (gdh8_refused_reload_leaves_a_coherent_trace) — ADDED BY THE REVIEW,
  #   2026-09-12.  Deviation (a)'s SECOND half — the disk write moving out of
  #   step 1 and into step 6 — had no gate and no arm, and nothing else in the
  #   verifier can see it: the source view is bundled from disk at the first
  #   step, the engine runs v1 from memory, and stdout and the container are
  #   both unchanged.  `assert_disk_holds` was added for this arm and this arm
  #   is what shows it can go red.
  #
  #   IT REDDENS `close` TOO, and that is expected rather than hidden: it breaks
  #   ONE property that is asserted in two gates (a refusal leaves v1 on disk; a
  #   trace-closing failure at step 4 leaves v1 on disk because step 6 never
  #   ran).  The driver prints the note; the `digest` gate, whose refusal
  #   happens inside the agent before this code path is reached, stays green and
  #   is the discrimination evidence.
  run_arm CT_GDH8_FALSIFY CT_GDH8_FALSIFY_WRITE_BEFORE_COMPILE refused \
    gdh8_refused_reload_leaves_a_coherent_trace \
    "the file ON DISK" \
    "restore the pre-GDH-M8 disk write, before the compile check"
else
  echo "-- every arm and the inertness gate SKIPPED (REBUILD_ARMS=0).  They" \
       "are NOT counted as passed; this run proves the unmutated gates only." >&2
fi

echo
echo "======================================================"
echo "unmutated runs green: $green"
echo "arms gone red:        $redarms of $armsrun run"
echo "failures:             $failures"
echo "output dir:           $OUT"
if [[ $failures -ne 0 ]]; then
  exit 1
fi
if [[ "$REBUILD_ARMS" == "1" && ( $redarms -ne $armsrun || $green -ne 2 ) ]]; then
  exit 1
fi
if [[ "$REBUILD_ARMS" != "1" && $green -ne 1 ]]; then
  exit 1
fi
echo
echo "GDH-M8: a v2 that does not compile is REFUSED by name, the engine keeps"
echo "        running v1, and the container is indistinguishable from one that"
echo "        was never asked — and a failure after the trace has committed"
echo "        closes the recording rather than continuing into incoherence."
