#!/usr/bin/env bash
# GDH-M6 gate driver — end to end.  A real Godot recording of a real hot
# reload, and every step attributed to the version that ran it.
#
# Design:    codetracer-specs/Planned-Features/
#            GDScript-Hot-Reload-Multi-Version-Sources.md §2, §6.1-§6.4, §7.1.
# Milestone: the `GDH-M6` block of the campaign's `.milestones.org`.
# Expected:  scripts/EXPECTED-GDH6.md — every hand-derived number.
#
# What runs here:
#
#   1. The four gates, unmutated, against the plain engine:
#        gdh6_no_step_is_attributed_to_the_wrong_version   (GDH-G3)
#        gdh6_both_versions_retrievable_end_to_end         (GDH-G1 + GDH-G2)
#        gdh6_reload_is_discoverable_end_to_end            (GDH-G7)
#        gdh5_reload_is_refused_while_a_step_is_pending    (inherited, and
#          BLOCKED by GDH-M5 for want of a container that could carry the
#          evidence; this driver is where it first RUNS)
#      Each carries its own control arm inside the verifier.
#
#   2. Every named falsifier arm.  They come in two families and the split is
#      not arbitrary:
#
#      CONSUMER-SIDE (arms 2, 3, 6) are `--falsify` modes of the verifier,
#      because each reproduces a defect of path RESOLUTION and resolution is
#      the verifier's own job in this harness.  Arm 2 is the behaviour the
#      codebase ships TODAY (`Db::path_map` last-wins,
#      `ctfs_trace_reader/mod.rs:1372-1376`), arm 3 is its mirror image, and a
#      gate that does not kill both is not testing this property.  They cost
#      no rebuild.
#
#      RECORDER-SIDE arms are real `-D` mutations compiled into the engine,
#      scoped to `modules/gdscript` by `CT_GDH6_FALSIFY` in that module's
#      SCsub — so an armed build is one translation unit plus a relink rather
#      than a full tree.  Each costs a rebuild, which is why `REBUILD_ARMS=0`
#      exists and why skipping them is reported as NOT RUN and never as
#      passed.
#
# Rules it enforces (codetracer-specs/Testing/Verification-Harness-Traps.md):
#
#   * BUILD and RUN are separate steps; a build error is never a red gate.
#   * A hang is rc 124 and NOTHING ELSE; every arm's rc is checked for it.
#   * rc 2 from the verifier is a DRIVER-FAIL and is never counted as a kill.
#   * An arm must go red IN THE GATE IT IS AIMED AT — the verifier prefixes
#     every failure with `GDH6-FAIL[<gate>]` and this driver requires the
#     right one.
#   * An arm that has a CONTROL must pass it.  An arm that fails everywhere
#     has not been shown to discriminate, and four falsifiers in this campaign
#     have already been measured non-discriminating for exactly that reason.
#   * A RESOLUTION FAILURE is reported and required under its own name; it is
#     a different finding from a wrong attribution and has a different fix.
#
# Usage:  scripts/record-and-verify-gdh6.sh [<output-dir>]
# Exit:   0 iff every gate is green and every arm is red in its own gate.
#
# Env:
#   BIN            the patchable engine (default bin/godot.<plat>.<target>.<arch>.hcr)
#   CT_PRINT       the container inspector (default ../codetracer-trace-format-nim/ct-print)
#   REBUILD_ARMS   0 to skip the recorder-side arms (reported NOT RUN)
#   TIMEOUT        per-invocation timeout, seconds (default 1200)
#   JOBS           scons parallelism for an armed build (default 4)

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

PLATFORM="${PLATFORM:-linuxbsd}"
TARGET="${TARGET:-template_debug}"
ARCH="${ARCH:-x86_64}"
BIN="${BIN:-$REPO/bin/godot.${PLATFORM}.${TARGET}.${ARCH}.hcr}"
CT_PRINT="${CT_PRINT:-$REPO/../codetracer-trace-format-nim/ct-print}"
OUT="${1:-${TMPDIR:-/tmp}/ct-gdh6-$$}"
TIMEOUT="${TIMEOUT:-1200}"
REBUILD_ARMS="${REBUILD_ARMS:-1}"
JOBS="${JOBS:-4}"
FIXTURES="$REPO/test-programs/gdh6"
VERIFY="$REPO/scripts/verify_gdh6.py"

log() { echo "[gdh6] $*"; }
die() { echo "[gdh6] DRIVER-FAIL: $*" >&2; exit 2; }

mkdir -p "$OUT" || die "cannot create $OUT"

# --- prerequisites, each loud ----------------------------------------------
# A missing prerequisite must never look like a pass or a skip.
[[ -x "$BIN" ]]      || die "engine not executable: $BIN"
[[ -x "$CT_PRINT" ]] || die "ct-print not executable: $CT_PRINT (build it in codetracer-trace-format-nim)"
[[ -f "$VERIFY" ]]   || die "verifier missing: $VERIFY"
command -v python3 >/dev/null || die "python3 is not on PATH"
for f in project.godot probe_v1.gd probe_v2.gd probe_v3.gd; do
  [[ -f "$FIXTURES/$f" ]] || die "missing fixture $FIXTURES/$f"
done

# The reload arrives over an AF_UNIX socket and `sun_path` holds 108 bytes, so
# the socket lives in a short directory chosen here rather than under $OUT.
SOCK_DIR="${GDH6_SOCKET_DIR:-${XDG_RUNTIME_DIR:-/tmp}}"
[[ -d "$SOCK_DIR" ]] || SOCK_DIR=/tmp

# The plain engine is copied aside BEFORE any armed build, because an armed
# build overwrites `bin/…hcr` in place.  Restoring it afterwards is verified
# rather than assumed.
PLAIN="$OUT/godot-plain.hcr"
cp -f "$BIN" "$PLAIN" || die "could not copy the plain engine aside"

failures=0
green=0
redarms=0
armsrun=0

run_verifier() {  # run_verifier <tag> <engine> <gate> <falsify>; echoes rc
  local tag="$1" engine="$2" gate="$3" falsify="$4"
  rm -rf "$OUT/$tag"
  timeout "$TIMEOUT" python3 "$VERIFY" \
    --engine "$engine" --fixtures "$FIXTURES" --work "$OUT/$tag" \
    --ct-print "$CT_PRINT" --socket-dir "$SOCK_DIR" \
    --gate "$gate" --falsify "$falsify" >"$OUT/$tag.out" 2>&1
  echo $?
}

log "engine    : $BIN"
log "ct-print  : $CT_PRINT"
log "fixtures  : $FIXTURES"
log "output    : $OUT"
log "socket dir: $SOCK_DIR"
echo

# ---------------------------------------------------------------------------
# 1. The unmutated run.  Every gate, every control arm, one recording set.
# ---------------------------------------------------------------------------
echo "== the four gates, unmutated =="
rc=$(run_verifier plain "$PLAIN" all none)
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
# 2a. Consumer-side arms.  No rebuild.
# ---------------------------------------------------------------------------
# <falsify> <selector> <gate-name> <expect-prefix> <label>
#
# `selector` is what `--gate` takes (the short name); `gate-name` is the
# test_name the verifier prefixes its failures with.  They are DIFFERENT
# strings and the first version of this function passed the second where the
# first belonged — which argparse rejected, so all three arms exited 2 and
# were correctly reported as DRIVER-FAILs rather than as kills.  The rule that
# caught it is the one worth keeping: rc 2 is never a kill.
run_consumer_arm() {
  local falsify="$1" selector="$2" gate="$3" expect="$4" label="$5"
  echo "-- arm $label (verifier: --falsify $falsify)"
  armsrun=$((armsrun + 1))
  local rc
  rc=$(run_verifier "arm-$falsify" "$PLAIN" "$selector" "$falsify")
  if [[ "$rc" == "124" ]]; then
    echo "ARM-FAIL: $label HUNG (rc 124).  A hang is not a diagnosis." >&2
    failures=$((failures + 1)); return
  fi
  if [[ "$rc" == "2" ]]; then
    cat "$OUT/arm-$falsify.out" >&2
    echo "ARM-FAIL: $label exited 2 (DRIVER-FAIL); not a kill" >&2
    failures=$((failures + 1)); return
  fi
  if [[ "$rc" == "0" ]]; then
    echo "ARM-FAIL: $label PASSED $gate; the mutation did not turn it red" >&2
    failures=$((failures + 1)); return
  fi
  if ! grep -q "$expect\[$gate\]" "$OUT/arm-$falsify.out"; then
    cat "$OUT/arm-$falsify.out" >&2
    echo "ARM-FAIL: $label exited $rc without a $expect[$gate] line" >&2
    failures=$((failures + 1)); return
  fi
  # THE CONTROL.  The no-reload arm runs inside the same verifier invocation
  # and must stay GREEN under the mutation: with one version a last-wins map,
  # a first-wins map and a correct one all agree, and an arm that fails
  # everywhere has not been shown to discriminate.
  if ! grep -q "CONTROL: no reload\]: GREEN" "$OUT/arm-$falsify.out"; then
    cat "$OUT/arm-$falsify.out" >&2
    echo "ARM-FAIL: $label also reddened its no-reload CONTROL; an arm that" \
         "fails everywhere has not been shown to discriminate" >&2
    failures=$((failures + 1)); return
  fi
  echo "   control (no reload): PASSES under the mutation, as it must"
  echo "   RED (rc $rc): $(grep -o "$expect\[.*" "$OUT/arm-$falsify.out" | head -1 | cut -c1-170)"
  redarms=$((redarms + 1))
}

echo "== consumer-side falsifier arms =="
# ARM 2 — the behaviour the codebase ships TODAY.
run_consumer_arm newest-wins attribution \
  gdh6_no_step_is_attributed_to_the_wrong_version \
  GDH6-FAIL "newest-wins (Db::path_map last-wins)"
# ARM 3 — its mirror image, the error an implementer reaches for when fixing
#   arm 2.
run_consumer_arm oldest-wins attribution \
  gdh6_no_step_is_attributed_to_the_wrong_version \
  GDH6-FAIL "oldest-wins (first registration kept)"
# ARM 6 — this campaign's own hazard.  It must go red as a RESOLUTION FAILURE
#   and be reported under that name, because "the path did not resolve" and
#   "the path resolved to the wrong version" have different fixes.
run_consumer_arm fuzzy-unique-only attribution \
  gdh6_no_step_is_attributed_to_the_wrong_version \
  GDH6-RESOLUTION-FAILURE "fuzzy_path_id_for stage 6 (unique match only)"
echo

# ---------------------------------------------------------------------------
# 2b. Recorder-side arms.  Each is a real engine build.
# ---------------------------------------------------------------------------
build_armed() {  # build_armed <defines> <dest>
  local defines="$1" dest="$2"
  CT_GDH6_FALSIFY="$defines" JOBS="$JOBS" timeout "$TIMEOUT" \
    ./scripts/build-hcr-patchable-linux.sh \
    >"$OUT/build-$dest.log" 2>&1 || return 1
  # A build that did not report arming would be recorded as a kill it never
  # made.  Every named define must appear.
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
  # A rebuilt engine is not byte-identical to the saved one (build ids and
  # timestamps move), so the restore is verified by the ABSENCE of the arm
  # rather than by bytes.
  grep -q "FALSIFIER ARM" "$OUT/build-restore.log" && return 1
  return 0
}

# <defines> <selector> <gate-name> <expect-prefix> <ctrl> <label> — see the
# note on run_consumer_arm for why the selector and the gate name are separate.
run_engine_arm() {
  local defines="$1" selector="$2" gate="$3" expect="$4" ctrl="$5" label="$6"
  echo "-- arm $label (engine: -D${defines//,/ -D})"
  armsrun=$((armsrun + 1))
  if ! build_armed "$defines" "$defines"; then
    tail -25 "$OUT/build-$defines.log" >&2
    echo "ARM-FAIL: $label did not BUILD; a build error is not a red gate" >&2
    failures=$((failures + 1))
    restore_plain
    return
  fi
  restore_plain || { echo "ARM-FAIL: could not restore the plain engine" >&2
                     failures=$((failures + 1)); return; }
  local armed="$OUT/godot-$defines.hcr" rc
  rc=$(run_verifier "arm-$defines" "$armed" "$selector" none)
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
  if ! grep -q "$expect\[$gate" "$OUT/arm-$defines.out"; then
    cat "$OUT/arm-$defines.out" >&2
    echo "ARM-FAIL: $label exited $rc without a $expect[$gate...] line" >&2
    failures=$((failures + 1)); return
  fi
  if [[ "$ctrl" == "control" ]]; then
    if ! grep -q "CONTROL: no reload\]: GREEN" "$OUT/arm-$defines.out"; then
      cat "$OUT/arm-$defines.out" >&2
      echo "ARM-FAIL: $label also reddened its no-reload CONTROL" >&2
      failures=$((failures + 1)); return
    fi
    echo "   control (no reload): PASSES under the mutation, as it must"
  fi
  echo "   RED (rc $rc): $(grep -o "$expect\[.*" "$OUT/arm-$defines.out" | head -1 | cut -c1-170)"
  redarms=$((redarms + 1))
}

run_flaky_engine_arm() {  # <defines> <selector> <gate> <label> <attempts>
  local defines="$1" selector="$2" gate="$3" label="$4" attempts="$5"
  echo "-- arm $label (engine: -D$defines), up to $attempts attempt(s)"
  armsrun=$((armsrun + 1))
  if ! build_armed "$defines" "$defines"; then
    tail -25 "$OUT/build-$defines.log" >&2
    echo "ARM-FAIL: $label did not BUILD; a build error is not a red gate" >&2
    failures=$((failures + 1)); restore_plain; return
  fi
  restore_plain || { echo "ARM-FAIL: could not restore the plain engine" >&2
                     failures=$((failures + 1)); return; }
  local armed="$OUT/godot-$defines.hcr"
  local crashed=0 killed=0 i rc
  for (( i = 1; i <= attempts; i++ )); do
    rc=$(run_verifier "arm-$defines-$i" "$armed" "$selector" none)
    if [[ "$rc" == "124" ]]; then
      echo "   attempt $i: HUNG (rc 124) — CHECK-FAIL, not a kill" >&2
      crashed=$((crashed + 1)); continue
    fi
    if [[ "$rc" == "2" ]]; then
      echo "   attempt $i: the mutated engine did not survive to write a" \
           "container ($(grep -m1 -oE 'DRIVER-FAIL.*' "$OUT/arm-$defines-$i.out" | cut -c1-120)).
              A death is not a diagnosis; CHECK-FAIL, not a kill."
      crashed=$((crashed + 1)); continue
    fi
    if [[ "$rc" == "0" ]]; then
      echo "   attempt $i: the mutation SURVIVED and the gate stayed green" >&2
      continue
    fi
    if grep -q "GDH6-FAIL\[$gate" "$OUT/arm-$defines-$i.out"; then
      echo "   attempt $i: RED in the container — $(grep -o "GDH6-FAIL\[.*" "$OUT/arm-$defines-$i.out" | head -1 | cut -c1-170)"
      killed=1
      break
    fi
    echo "   attempt $i: exited $rc without a GDH6-FAIL[$gate] line" >&2
    crashed=$((crashed + 1))
  done
  echo "   crashed/unusable attempts: $crashed"
  if [[ $killed -eq 1 ]]; then
    redarms=$((redarms + 1))
  else
    echo "ARM-FAIL: $label was NOT DEMONSTRATED in $attempts attempt(s): the" \
         "mutated engine never survived long enough to write a container the" \
         "gate could inspect. The crashes are evidence that the guard is" \
         "load-bearing, but they are NOT this gate's kill and are not" \
         "recorded as one." >&2
    failures=$((failures + 1))
  fi
}

if [[ "$REBUILD_ARMS" == "1" ]]; then
  echo "== recorder-side falsifier arms =="
  # ARM 1 — apply the reload and mint NO path version.  Post-reload steps then
  #   resolve to v1's id by the writer's ordinary interning, which is exactly
  #   the state GDH-M0 measured.  It necessarily takes the marker with it
  #   (`registerSourceReload` refuses `old == new`), and that is stated here
  #   rather than hidden: it is aimed at the attribution gate.
  run_engine_arm CT_GDH6_FALSIFY_NO_VERSION_MINTED attribution \
    gdh6_no_step_is_attributed_to_the_wrong_version GDH6-FAIL control \
    "no version minted (arm 1)"
  # ARM 5 — register the new version with the OLD version's line count, so its
  #   high lines lie outside its own slot.
  run_engine_arm CT_GDH6_FALSIFY_STALE_LINE_COUNT attribution \
    gdh6_no_step_is_attributed_to_the_wrong_version GDH6-FAIL control \
    "stale line count (arm 5)"
  # ARM 4 — every path id correct, every line correct, and every source view
  #   carrying v1's text.  This arm passes the line-number half completely and
  #   must be caught by the TEXT half, which is why the text half exists.
  run_engine_arm CT_GDH6_FALSIFY_STALE_SOURCE_VIEW attribution \
    gdh6_no_step_is_attributed_to_the_wrong_version GDH6-FAIL control \
    "stale source view (arm 4)"
  # GDH-G1/G2's arm — restore the STRING-keyed bundle early return.
  #
  # STRENGTHENED AT REVIEW (2026-09-11): this arm was passing "" for `ctrl`,
  # so the "the no-reload CONTROL stays GREEN under the mutation" check was
  # SKIPPED for it while all four attribution arms ran it.  The arm went red,
  # which looks like it works — but nothing showed it goes red only where it
  # should, and an arm that would fail everywhere has not been shown to
  # discriminate.  It is the seventh arm in this campaign found weaker than it
  # looked.  `--gate retrievable` runs a `[CONTROL: no reload]` checker, so
  # the control was available all along and is now REQUIRED: with one version
  # a string-keyed bundle and an id-keyed one agree, so the control must pass.
  run_engine_arm CT_GDH6_FALSIFY_STRING_KEYED_BUNDLE retrievable \
    gdh6_both_versions_retrievable_end_to_end GDH6-FAIL control \
    "string-keyed source bundle"
  # GDH-G7's arm — mint the version and emit NO marker.  Every index is right
  #   and a consumer could still INFER the transition; the gate must still go
  #   red, which is the whole reason it exists separately from GDH-G3.
  run_engine_arm CT_GDH6_FALSIFY_NO_MARKER discoverable \
    gdh6_reload_is_discoverable_end_to_end GDH6-FAIL "" \
    "no marker emitted"
  # The inherited GDH-M5 gate's arm — apply the reload where the notification
  #   landed instead of deferring it to the safe point.
  #
  # THIS ARM IS NOT DETERMINISTIC, and that is a measurement rather than an
  # excuse.  Applying a reload while the VM is mid-step usually KILLS the
  # engine: of five runs on 2026-09-11, three exited -11 (SIGSEGV), one closed
  # the agent socket mid-handshake, one stalled before the second reload
  # window, and ONE produced a complete container.  A crash is not a
  # diagnosis (trap 1), so a crashed attempt is recorded as CRASHED and is
  # never counted as a kill; the arm is only red when a surviving run shows
  # the defect IN THE CONTAINER, which is what the milestone requires.
  #
  # The milestone's falsifier text assumed a survivable mutation.  It is not
  # one, and the retry below is the honest way to run it rather than the
  # convenient one.
  run_flaky_engine_arm CT_GDH6_FALSIFY_APPLY_WHERE_IT_LANDS pending-step \
    gdh5_reload_is_refused_while_a_step_is_pending \
    "apply where it lands" "${GDH6_FLAKY_ATTEMPTS:-6}"
else
  echo "-- recorder-side falsifier arms SKIPPED (REBUILD_ARMS=0).  They are" \
       "NOT counted as passed; this run proves the unmutated gates and the" \
       "consumer-side arms only." >&2
fi

echo
echo "======================================================"
echo "unmutated runs green: $green of 1"
echo "arms gone red:        $redarms of $armsrun run"
echo "failures:             $failures"
echo "output dir:           $OUT"
echo
echo "NOT RUN HERE, and run by their own drivers:"
echo "  gdh6_corpus_is_unchanged                        -> scripts/verify-corpus-no-silent-skip.sh"
echo "  gdh6_a_timed_out_deferral_does_not_outlive_its_request"
echo "                                                  -> scripts/record-and-verify-gdh6-asan.sh"
if [[ $failures -ne 0 || $green -ne 1 ]]; then
  exit 1
fi
if [[ "$REBUILD_ARMS" == "1" && $redarms -ne $armsrun ]]; then
  exit 1
fi
echo
echo "GDH-M6: a step recorded BEFORE the reload resolves to v1's content and a"
echo "        step recorded AFTER it resolves to v2's, both out of ONE trace,"
echo "        under the SAME virtual path and different path indices — and no"
echo "        step is attributed to the wrong version."
