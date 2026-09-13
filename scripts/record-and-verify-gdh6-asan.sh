#!/usr/bin/env bash
# GDH-M6 — `gdh6_a_timed_out_deferral_does_not_outlive_its_request`.
#
# Design:    codetracer-specs/Planned-Features/
#            GDScript-Hot-Reload-Multi-Version-Sources.md §5.6.
# Milestone: the `GDH-M6` block of the campaign's `.milestones.org`.
#
# WHY THIS GATE EXISTS, stated because it guards a fix and not a feature.
# GDH-M5's deferral path originally queued a RAW POINTER into the waiting
# thread's frame and let the waiter clear the queue when its 30 s bound
# expired.  The safe point takes that pointer under the lock, then RELEASES
# the lock to do the apply — which writes a file and recompiles a script, so
# it is not quick — and a timeout inside that window destroys the object the
# safe point is still writing into.  A use-after-free with a 30-second fuse.
# It is a `shared_ptr` now, so a timeout means "stop waiting" and not "delete
# what the other thread is using".  A fix with no gate is a fix the next
# refactor can undo with nothing going red.
#
# THE KILL IS A SANITIZER REPORT, NOT A CRASH.  A 30-second fuse usually does
# not blow on demand: the freed frame is often still readable and the run
# completes looking fine.  An arm that "detects" the defect by segfaulting has
# not been distinguished from any other way of dying, and per
# `Verification-Harness-Traps.md` trap 1 a kill has to be a diagnosis.  So this
# driver REQUIRES an AddressSanitizer report on stderr and treats a bare
# non-zero exit as a CHECK-FAIL.
#
# TWO TEST HOOKS make the race deterministic, both read from the environment
# by the shipped recorder and both reported by it when they are in effect:
#
#   CT_GDH6_RELOAD_WAIT_SECONDS   the waiter's bound (shipping: 30).  The
#                                 milestone requires it to be settable: a gate
#                                 that takes half a minute to answer gets
#                                 disabled, and a disabled gate is not a gate.
#   CT_GDH6_SAFE_POINT_DELAY_MS   how long the safe point holds the request
#                                 before applying it.  This is what opens the
#                                 window; without it the gate would be waiting
#                                 for a race to happen on its own.
#
# ASan is scoped to `modules/gdscript` by `CT_GDH6_ASAN` in that module's
# SCsub, so each build here is one translation unit plus a relink rather than
# a full tree.
#
# Usage:  scripts/record-and-verify-gdh6-asan.sh [<output-dir>]
# Exit:   0 iff the clean run is green AND the raw-pointer arm produced an
#         ASan report naming the request object.
#
# Env: TIMEOUT (default 900), JOBS (default 4), REBUILD (0 to reuse binaries
#      already under the output dir).

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

BIN="$REPO/bin/godot.linuxbsd.template_debug.x86_64.hcr"
OUT="${1:-${TMPDIR:-/tmp}/ct-gdh6-asan-$$}"
TIMEOUT="${TIMEOUT:-900}"
JOBS="${JOBS:-4}"
REBUILD="${REBUILD:-1}"
FIXTURES="$REPO/test-programs/gdh6"
DRIVE="$REPO/scripts/gdh6_asan_drive.py"

# The waiter must time out WHILE the apply is in flight, so the safe point's
# hold has to outlast the bound by a comfortable margin.
WAIT_S="${GDH6_WAIT_S:-2}"
DELAY_MS="${GDH6_DELAY_MS:-9000}"
# The fixture runs 600 ticks x 50 ms = 30 s, so a 9 s hold after a reload at
# tick 6 leaves ~20 s of margin.  The margin is not decoration: with the gate
# fixtures (1.8 s total) the program ended before the apply finished and the
# gate reported the fixture rather than the subject.

log() { echo "[gdh6-asan] $*"; }
die() { echo "[gdh6-asan] DRIVER-FAIL: $*" >&2; exit 2; }

mkdir -p "$OUT" || die "cannot create $OUT"
[[ -f "$DRIVE" ]] || die "driver missing: $DRIVE"
command -v python3 >/dev/null || die "python3 is not on PATH"
for f in project.godot asan_v1.gd asan_v2.gd; do
  [[ -f "$FIXTURES/$f" ]] || die "missing fixture $FIXTURES/$f"
done

SOCK_DIR="${GDH6_SOCKET_DIR:-${XDG_RUNTIME_DIR:-/tmp}}"
[[ -d "$SOCK_DIR" ]] || SOCK_DIR=/tmp

failures=0

# The plain engine is copied aside BEFORE any armed build, because an armed
# build overwrites `bin/…hcr` in place. This byte-exact copy is what the exit
# trap below restores from, and it doubles as the oracle for "is the engine in
# the tree the plain one" — a question this campaign has repeatedly needed a
# checkable answer to.
# NOTE THE FILENAME, it is the whole point. `$OUT/godot-plain.hcr` is ALREADY
# TAKEN in this driver: its first ASan build is LABELLED `plain` (meaning "ASan
# but unmutated"), and `build_asan` copies that SANITIZED engine to
# `$OUT/godot-$label.hcr`. A snapshot written there is overwritten by the first
# build and the exit trap then "restores" a sanitized engine over the driver's
# own correct un-sanitized rebuild — measured on 2026-09-13, which is how this
# comment came to exist. The name below cannot collide with any `$label`.
PLAIN="$OUT/godot-unsanitized-original.hcr"
cp -f "$BIN" "$PLAIN" || die "could not copy the un-sanitized engine aside"

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
ct_gdh6_asan_restore_bin_on_exit() {
  local rc=$?
  if [[ -f "$PLAIN" ]] && ! cmp -s "$BIN" "$PLAIN"; then
    if cp -f "$PLAIN" "$BIN"; then
      echo "[gdh6-asan] restored the plain engine to $BIN on exit (it was armed)" >&2
    else
      echo "[gdh6-asan] WARNING: could NOT restore the plain engine to $BIN — an" \
           "ARMED engine is left in the tree. Copy $PLAIN back by hand." >&2
    fi
  fi
  exit $rc
}
trap ct_gdh6_asan_restore_bin_on_exit EXIT INT TERM

build_asan() {  # build_asan <label> [falsify-define]
  local label="$1" falsify="${2:-}"
  CT_GDH6_ASAN=1 CT_GDH6_FALSIFY="$falsify" JOBS="$JOBS" \
    timeout "$TIMEOUT" ./scripts/build-hcr-patchable-linux.sh \
    >"$OUT/build-$label.log" 2>&1 || return 1
  grep -q "GDH-M6: -fsanitize=address on modules/gdscript" "$OUT/build-$label.log" || {
    echo "the build did not report arming ASan; a build in which the flag was" \
         "silently dropped must not be recorded as a clean run" >&2
    return 1
  }
  if [[ -n "$falsify" ]]; then
    grep -q "FALSIFIER ARM: -D$falsify" "$OUT/build-$label.log" || {
      echo "the build did not report arming -D$falsify" >&2
      return 1
    }
  fi
  cp -f "$BIN" "$OUT/godot-$label.hcr" || return 1
}

# ANTI-VACUITY ON THE BUILD ITSELF: a binary in which `-fsanitize=address` was
# dropped runs clean and reports nothing, and "ASan is quiet" would then be
# indistinguishable from "ASan is absent".  The sanitizer's own symbol is the
# evidence.
assert_asan_linked() {  # assert_asan_linked <binary>
  local bin="$1" nsyms lib
  # TWO INDEPENDENT SIGNALS, because the obvious single one was measured wrong.
  # The first version of this check looked for `__asan_init` /
  # `__asan_version_mismatch_check` specifically and reported a genuinely
  # instrumented binary as un-instrumented: neither name is among the 37
  # `__asan_*` symbols the compiler actually imports here (they are
  # `__asan_report_*`, `__asan_stack_malloc_*`, `__asan_handle_no_return`, …).
  # A check that names one symbol is a check that depends on a compiler's
  # inlining decisions.
  nsyms="$(nm -D "$bin" 2>/dev/null | grep -c '__asan_' || true)"
  lib="$(ldd "$bin" 2>/dev/null | grep -c 'libasan' || true)"
  if [[ "$nsyms" -lt 1 || "$lib" -lt 1 ]]; then
    echo "GDH6-CHECK-FAIL: $bin does not look instrumented" \
         "($nsyms __asan_* dynamic symbols, $lib libasan DT_NEEDED entries)." \
         "A build in which -fsanitize=address was silently dropped runs clean" \
         "and reports nothing, so \"ASan is quiet\" would prove nothing." >&2
    return 1
  fi
  log "ASan is linked into $(basename "$bin"): $nsyms __asan_* dynamic symbols, libasan present"
  return 0
}

run_case() {  # run_case <tag> <binary> <delay_ms>
  local tag="$1" bin="$2" delay="$3"
  rm -rf "$OUT/$tag"; mkdir -p "$OUT/$tag"
  ASAN_OPTIONS="detect_stack_use_after_return=1:detect_leaks=0:abort_on_error=0:halt_on_error=0" \
  CT_GDH6_RELOAD_WAIT_SECONDS="$WAIT_S" \
  CT_GDH6_SAFE_POINT_DELAY_MS="$delay" \
  timeout "$TIMEOUT" python3 "$DRIVE" \
    --engine "$bin" --fixtures "$FIXTURES" --work "$OUT/$tag" \
    --socket-dir "$SOCK_DIR" >"$OUT/$tag.out" 2>&1
  echo $?
}

log "output     : $OUT"
log "waiter bound: ${WAIT_S}s   safe-point hold: ${DELAY_MS}ms"
echo

# ---------------------------------------------------------------------------
# 1. The clean run: the waiter times out, the safe point finishes, ASan quiet.
# ---------------------------------------------------------------------------
if [[ "$REBUILD" == "1" ]]; then
  log "building the ASan engine (plain)"
  build_asan plain || die "the ASan engine did not build"
fi
[[ -x "$OUT/godot-plain.hcr" ]] || die "no ASan engine at $OUT/godot-plain.hcr"
assert_asan_linked "$OUT/godot-plain.hcr" || failures=$((failures + 1))

echo "== gdh6_a_timed_out_deferral_does_not_outlive_its_request =="
rc=$(run_case clean "$OUT/godot-plain.hcr" "$DELAY_MS")
cat "$OUT/clean.out"
if [[ "$rc" == "124" ]]; then
  echo "GATE-FAIL: the clean run HUNG (rc 124)" >&2; failures=$((failures + 1))
elif [[ "$rc" != "0" ]]; then
  echo "GATE-FAIL: the clean run is RED (rc $rc)" >&2; failures=$((failures + 1))
fi
if grep -qE "AddressSanitizer: [a-z-]+" "$OUT/clean.out"; then
  echo "GATE-FAIL: the UNMUTATED run produced an AddressSanitizer report:" >&2
  grep -m1 -A6 "AddressSanitizer:" "$OUT/clean.out" >&2
  failures=$((failures + 1))
else
  echo "   ASan reported nothing over the unmutated run"
fi
echo

# ---------------------------------------------------------------------------
# 2. The CONTROL: the safe point arrives BEFORE the bound expires.
#    Without it, "ASan is quiet" could mean the race window never opened.
# ---------------------------------------------------------------------------
echo "-- control: the safe point arrives before the bound expires"
rc=$(run_case control "$OUT/godot-plain.hcr" 0)
if [[ "$rc" != "0" ]]; then
  cat "$OUT/control.out" >&2
  echo "CONTROL-FAIL: the reload was not applied and the waiter not woken" \
       "(rc $rc)" >&2
  failures=$((failures + 1))
elif grep -q "TIMED OUT" "$OUT/control.out"; then
  echo "CONTROL-FAIL: the control run TIMED OUT; it is supposed to be the arm" \
       "in which the safe point wins the race" >&2
  failures=$((failures + 1))
else
  echo "   applied before the bound, waiter woken, ASan quiet"
fi
echo

# ---------------------------------------------------------------------------
# 3. The falsifier arm: the raw-pointer queue.
# ---------------------------------------------------------------------------
echo "-- arm raw-pointer queue (-DCT_GDH6_FALSIFY_RAW_POINTER_QUEUE, +ASan)"
if [[ "$REBUILD" == "1" ]]; then
  build_asan rawptr CT_GDH6_FALSIFY_RAW_POINTER_QUEUE || {
    tail -25 "$OUT/build-rawptr.log" >&2
    echo "ARM-FAIL: the raw-pointer arm did not BUILD; a build error is not a" \
         "red gate" >&2
    failures=$((failures + 1))
  }
fi
if [[ -x "$OUT/godot-rawptr.hcr" ]]; then
  assert_asan_linked "$OUT/godot-rawptr.hcr" || failures=$((failures + 1))
  rc=$(run_case rawptr "$OUT/godot-rawptr.hcr" "$DELAY_MS")
  if grep -qE "AddressSanitizer: (heap-use-after-free|stack-use-after-return|heap-buffer-overflow)" "$OUT/rawptr.out"; then
    echo "   RED by the SANITIZER'S REPORT:"
    grep -m1 -E "AddressSanitizer: [a-z-]+" "$OUT/rawptr.out" | sed 's/^/     /'
    # The report has to be ABOUT the request object, or it is some other bug.
    if grep -qi "CtReloadRequest\|gdscript_ct_hcr_source_reload\|ct_apply_reload_locked" "$OUT/rawptr.out"; then
      echo "     and its stack names the reload request path"
    else
      echo "ARM-NOTE: the ASan report does not name CtReloadRequest or the" \
           "reload path; it is a real report but it may be a different defect." \
           "Recorded rather than counted as this gate's kill." >&2
      failures=$((failures + 1))
    fi
  elif [[ "$rc" == "124" ]]; then
    echo "ARM-FAIL: the raw-pointer arm HUNG (rc 124). CHECK-FAIL, not a kill." >&2
    failures=$((failures + 1))
  elif [[ "$rc" != "0" ]]; then
    echo "ARM-FAIL: the raw-pointer arm exited $rc with NO AddressSanitizer" \
         "report. A bare non-zero exit is a CHECK-FAIL, not a kill: it has not" \
         "been distinguished from any other way of dying." >&2
    failures=$((failures + 1))
  else
    echo "ARM-FAIL: the raw-pointer arm ran CLEAN. Either the race window did" \
         "not open or ASan is not watching the object; both are CHECK-FAILs." >&2
    failures=$((failures + 1))
  fi
else
  echo "ARM-FAIL: no raw-pointer binary to run" >&2
  failures=$((failures + 1))
fi

# ---------------------------------------------------------------------------
# 4. PUT THE TREE BACK.  Added at review, 2026-09-11, after measuring what
#    this script used to leave behind.
#
# Every build above overwrites `bin/…hcr` IN PLACE, and this script had no
# restore step at all — so a completed run left the shared engine binary
# `-fsanitize=address` instrumented AND carrying
# `CT_GDH6_FALSIFY_RAW_POINTER_QUEUE`.  Measured: 37 `__asan_*` dynamic
# symbols and a libasan DT_NEEDED entry in `bin/…hcr` after a green run.
#
# That is not merely untidy.  `record-and-verify-gdh6.sh` copies `$BIN` aside
# at startup and calls the copy PLAIN — it has no way to know otherwise — so
# the next person to run the gates would have graded all four of them against
# a deliberately mutated, sanitized engine and been told "90 assertions, 0
# red".  A green over a known-broken binary is the exact shape this campaign
# keeps finding, and here the harness was manufacturing it.
#
# The restore is VERIFIED rather than assumed, by the absence of the two
# things the builds above require the presence of.  A failed restore is a
# failure of this script: leaving the tree armed is not a lesser outcome than
# a red gate.
if [[ "$REBUILD" == "1" ]]; then
  echo
  echo "-- restoring the plain, un-sanitized engine"
  if ! CT_GDH6_ASAN=0 CT_GDH6_FALSIFY="" JOBS="$JOBS" \
       timeout "$TIMEOUT" ./scripts/build-hcr-patchable-linux.sh \
       >"$OUT/build-restore.log" 2>&1; then
    tail -25 "$OUT/build-restore.log" >&2
    echo "RESTORE-FAIL: the plain engine did not rebuild; bin/ is left ARMED" \
         "and the next gate run would grade against it" >&2
    failures=$((failures + 1))
  elif grep -q "FALSIFIER ARM\|GDH-M6: -fsanitize=address" "$OUT/build-restore.log"; then
    echo "RESTORE-FAIL: the restore build still reports an arm or the" \
         "sanitizer" >&2
    failures=$((failures + 1))
  elif [[ "$(nm -D "$BIN" 2>/dev/null | grep -c '__asan_')" -ne 0 ]]; then
    echo "RESTORE-FAIL: $BIN still imports __asan_* symbols after the" \
         "restore build" >&2
    failures=$((failures + 1))
  else
    log "restored: $(basename "$BIN") is un-sanitized and un-armed"
  fi
fi

echo
echo "======================================================"
echo "failures: $failures"
echo "output:   $OUT"
[[ $failures -eq 0 ]] || exit 1
echo "GDH-M6: a deferral whose bound expires while the safe point is still"
# `failed` in double quotes is COMMAND SUBSTITUTION, not emphasis.  Found at
# review by running the gate: the success banner printed
# `record-and-verify-gdh6-asan.sh: line 247: failed: command not found` and
# then a sentence with a hole in it.  Harmless to the verdict — `failures` is
# already decided above — but it is the gate's own summary of what it proved,
# and a summary that runs a stray command and drops the word it was
# emphasising is not one to quote.
echo "        applying answers 'failed' with a named reason, the safe point"
echo "        completes, and AddressSanitizer reports nothing — while the"
echo "        raw-pointer form of the same code is killed by ASan's report."
