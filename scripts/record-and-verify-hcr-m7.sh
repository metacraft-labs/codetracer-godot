#!/usr/bin/env bash
# HLX-M7 verification gate `e2e_hcr_linux_patch_under_active_mcr_recording`.
#
# Design: `reprobuild-specs/HCR/Linux-ELF-Provider.md` §10.
# Protocol: `Hot-Code-Reloading-High-Level-Interfaces.md` §7.2.
#
# WHAT THIS GATE IS FOR
# ---------------------------------------------------------------------------
# A real Godot engine function is replaced in a live 31-thread process while
# `ct-mcr record` is recording it. That already worked before HLX-M7 — and it
# was MEASURED that the resulting recording had an IDENTICAL SET OF EVENT KINDS
# to a control recording with no patch. Nothing in the container distinguished
# post-patch execution from pre-patch execution, so a replay would have
# reproduced the ORIGINAL code's semantics against events the NEW code produced:
# silently wrong from the patch point onwards, which is worse than a trace that
# refuses.
#
# So this gate runs BOTH arms and requires them to differ, and to differ
# specifically by a `CodePatchEvent` whose every field checks out. The
# comparison is the campaign's own control, INVERTED.
#
# `allowed_mocks: none`. Real patchable engine, real `ct-mcr record`, real
# `libct_interpose` in the target, real HCR patch through the production
# coordinator over the production Unix-socket wire, real trace container read
# back with `ct-mcr trace`.
#
# THE FALSIFIER ARMS ARE RUN, NOT DESCRIBED
# ---------------------------------------------------------------------------
# This campaign has twice measured a reviewed falsifier exiting 0 with the
# defect fully present, so every arm below is executed on every run and the gate
# FAILS if an arm goes green. Eight of them mutate the produced artifacts —
# deleting the event, zeroing the digests, fabricating one, breaking the jump,
# emptying the symbol list, dropping the tier, truncating the dump after the
# event, forging the support profile — and the ninth is a real producer-side
# negative: the checker is pointed at the UNPATCHED recording, where the event
# genuinely was never emitted.
#
# The last two were added by review on 2026-09-10, and `dump-truncated` is the
# one to keep: it leaves the event and all eighteen field checks intact and is
# caught ONLY by the "the dump's line count equals the count `trace info`
# reports" rule. That is the property this campaign has lost most often, and
# until that arm existed nothing here exercised it.
#
# Usage: scripts/record-and-verify-hcr-m7.sh [<output-dir>]
# Environment: BIN, CT_MCR, REPROBUILD_DIR, PATCH_AFTER, MARKER_TIMEOUT_MS,
#              CONTROL_TICKS
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="${BIN:-$REPO/bin/godot.linuxbsd.template_debug.x86_64.hcr}"
CT_MCR="${CT_MCR:-$REPO/../codetracer-native-recorder/ct_cli/ct_cli}"
PROJ="$REPO/test-programs/hcr1"
ENTRY="res://hcr1_live_patch.gd"
PATCH_ID="hcr1-godot-under-mcr"
OUT="${1:-/tmp/ct-hcr-m7-$$}"

log() { printf '[hcr-m7] %s\n' "$*"; }
die() { printf '[hcr-m7] FATAL: %s\n' "$*" >&2; exit 1; }
fail_count=0
check_fail() { printf '[hcr-m7] CHECK-FAIL: %s\n' "$*" >&2; fail_count=$((fail_count + 1)); }

# Missing prerequisites are LOUD. Never a skip: a gate that quietly returns
# when its subject is absent is counted as passed while checking nothing.
[[ -x "$BIN" ]]    || die "patchable engine not built: $BIN (scripts/build-hcr-patchable-linux.sh)"
[[ -x "$CT_MCR" ]] || die "ct-mcr (ct_cli) not found/executable: $CT_MCR"
[[ -f "$PROJ/project.godot" ]] || die "demo project missing: $PROJ/project.godot"
command -v python3 >/dev/null || die "python3 not on PATH"
mkdir -p "$OUT" || die "cannot create $OUT"

# The socket path lives in an abstract-namespace-free `sun_path`, which is 108
# bytes. A long `--output` directory silently produced "socket path too long"
# from inside the driver, which surfaced as "the driver never created the
# socket". Check it here, where the message can say what is actually wrong.
SOCK_PROBE="$OUT/hcr1-mcr.sock"
if [[ "${#SOCK_PROBE}" -ge 100 ]]; then
	die "the output directory makes a ${#SOCK_PROBE}-byte Unix socket path, and sun_path holds 108. Pass a shorter <output-dir> (e.g. /tmp/ct-m7)."
fi

TARGET_SYMBOL="${TARGET_SYMBOL:-_ZNK8CoreBind2OS19get_processor_countEv}"

# Check (D) SPAWNS TWO REPLAYS, AND REPLAYS ARE LICENSED. On the free tier
# `ct-mcr replay-worker` allows five a day and then exits 1 with "license check
# failed: Daily replay limit reached (N/5)" BEFORE it reads the trace — so both
# arms of (D) fail without ever reaching the provenance check, and the gate's
# verdict silently depends on how many replays this host happened to run today.
# Measured on 2026-09-10: a full run from a plain shell reported "(D) the
# patched replay failed (rc=1) but not because of the CodePatchEvent", which
# reads like a product defect and is not one.
#
# The recorder's nix dev shell exports CODETRACER_LICENSE_FILE; a plain shell
# does not. Refuse up front rather than mid-run, and name the remedy — this is
# a missing prerequisite, and a prerequisite is stated LOUDLY, never skipped and
# never left to surface as an unrelated-looking failure four minutes in.
if [[ -z "${CODETRACER_LICENSE_FILE:-}" ]]; then
	CANDIDATE="$(cd "$REPO/.." && pwd)/codetracer-native-recorder/tests/fixtures/licensing/ct-test-license.dat"
	die "CODETRACER_LICENSE_FILE is not set, so check (D)'s two 'ct-mcr replay-worker' runs would hit the free tier's five-replays-a-day cap and fail before reading the trace. Export it and re-run:
    export CODETRACER_LICENSE_FILE=$CANDIDATE
  (the recorder's 'nix develop' shell exports it for you.)"
fi
[[ -r "$CODETRACER_LICENSE_FILE" ]] \
	|| die "CODETRACER_LICENSE_FILE=$CODETRACER_LICENSE_FILE is not readable; check (D) needs it."

# --- ARM A: the patched recording -------------------------------------------
# Delegated to the demo script, which owns the coordinator, the marker-gated
# patch, the /dev/shm hygiene and the behavioural before/after assertion.
log "ARM A: patching a live engine under ct-mcr record"
PATCHED_OUT="$OUT/patched"
mkdir -p "$PATCHED_OUT"
BIN="$BIN" CT_MCR="$CT_MCR" TARGET_SYMBOL="$TARGET_SYMBOL" \
	bash "$REPO/scripts/hcr-patch-godot-under-mcr.sh" "$PATCHED_OUT" \
	> "$OUT/arm-a.log" 2>&1
ARM_A_RC=$?
tail -25 "$OUT/arm-a.log"
if [[ "$ARM_A_RC" -ne 0 ]]; then
	die "ARM A failed (rc=$ARM_A_RC). Full log: $OUT/arm-a.log. Nothing downstream can be concluded from a run whose patch did not land."
fi
log "ARM A: rc=0"

for f in trace-events.txt trace-info.txt patch-result.json; do
	[[ -s "$PATCHED_OUT/$f" ]] || die "ARM A produced no $f"
done

# --- ARM B: the unpatched control -------------------------------------------
# The SAME engine, the SAME project, the SAME recorder — with no coordinator
# socket in the environment, so the in-target agent returns immediately and no
# patch is ever published. That single difference is what makes the comparison
# below a comparison of the patch rather than of two unrelated runs.
log "ARM B: the same program recorded with NO patch"
CONTROL_CT="$OUT/control.ct"
CONTROL_LOG="$OUT/control-record.log"
CONTROL_TICKS="${CONTROL_TICKS:-40}"
env -u REPRO_HCR_AGENT_SOCKET \
	"$CT_MCR" record --output "$CONTROL_CT" -- \
		"$BIN" --headless --path "$PROJ" --script "$ENTRY" \
	> "$CONTROL_LOG" 2>&1
CONTROL_RC=$?
log "ARM B: ct-mcr record exit=$CONTROL_RC"
[[ "$CONTROL_RC" -eq 0 ]] || check_fail "ARM B: the control recording failed (rc=$CONTROL_RC); see $CONTROL_LOG"
[[ -f "$CONTROL_CT" ]] || die "ARM B: no container at $CONTROL_CT"

# The control has to have RUN THE SAME PROGRAM, or the differential is between
# two things that were never comparable. The engine prints one tick line per
# iteration; require the same number the patched arm produces.
CONTROL_TICK_LINES="$(grep -c "CT_HCR_TICK=" "$CONTROL_LOG" || true)"
if [[ "$CONTROL_TICK_LINES" -ne "$CONTROL_TICKS" ]]; then
	check_fail "ARM B: the control printed $CONTROL_TICK_LINES CT_HCR_TICK lines, expected $CONTROL_TICKS. It did not run the same workload as the patched arm, so the event-kind differential would be between two different programs."
else
	log "ARM B: $CONTROL_TICK_LINES ticks, the same workload as the patched arm"
fi
# And it must show NO post-patch value anywhere: a control that somehow got
# patched is not a control.
if grep -q "CT_HCR_VALUE=4242" "$CONTROL_LOG"; then
	check_fail "ARM B: the control run reported the PATCHED value 4242. It was patched, so it is not a control."
fi

"$CT_MCR" trace info "$CONTROL_CT" > "$OUT/control-trace-info.txt" 2>&1 \
	|| die "ARM B: 'ct-mcr trace info' failed on $CONTROL_CT"
"$CT_MCR" trace events "$CONTROL_CT" > "$OUT/control-trace-events.txt" \
	2> "$OUT/control-trace-events.err"
CONTROL_EVENTS_RC=$?
[[ "$CONTROL_EVENTS_RC" -eq 0 ]] || die "ARM B: 'ct-mcr trace events' failed (rc=$CONTROL_EVENTS_RC): $(head -3 "$OUT/control-trace-events.err")"

# --- the checker ------------------------------------------------------------
CHECK=("python3" "$REPO/scripts/verify_code_patch_event.py"
	"--patched-events" "$PATCHED_OUT/trace-events.txt"
	"--patched-info" "$PATCHED_OUT/trace-info.txt"
	"--driver-json" "$PATCHED_OUT/patch-result.json"
	"--control-events" "$OUT/control-trace-events.txt"
	"--control-info" "$OUT/control-trace-info.txt"
	"--patch-id" "$PATCH_ID"
	"--symbol" "$TARGET_SYMBOL")

log "verifying the CodePatchEvent"
"${CHECK[@]}" > "$OUT/verify.log" 2>&1
VERIFY_RC=$?
cat "$OUT/verify.log"
if [[ "$VERIFY_RC" -ne 0 ]]; then
	check_fail "the CodePatchEvent verification FAILED (rc=$VERIFY_RC)"
fi

# --- (D) the trace stops lying to a REPLAY, not just to a reader ------------
# §7.3 replays a patched recording by applying the stored bundle to the replay
# process at the event's geid, and no replay path in this tree does that yet.
# The event is therefore only half the fix: a replay that ignored it would start
# with the original binary, run past the boundary with the ORIGINAL code, serve
# events the PATCHED code produced, and report success — the same silent
# wrongness one layer down. `reportRecordProvenance` refuses instead, by geid.
#
# BOTH DIRECTIONS ARE CHECKED. A refusal is only evidence if the same command
# does NOT refuse a recording with no patch in it; otherwise "it refused" is
# equally consistent with a replay that refuses everything.
log "(D) a replay of the patched recording must be REFUSED"
REPLAY_TIMEOUT_S="${REPLAY_TIMEOUT_S:-600}"
timeout --signal=TERM "$REPLAY_TIMEOUT_S" \
	"$CT_MCR" replay-worker "$PATCHED_OUT/godot_native.ct" \
	> "$OUT/replay-patched.log" 2>&1
REPLAY_PATCHED_RC=$?
if [[ "$REPLAY_PATCHED_RC" -eq 124 ]]; then
	# A timeout is a SYMPTOM, not a diagnosis, and 124 is the only rc that means
	# it. Say so rather than folding it into "refused".
	check_fail "(D) 'ct-mcr replay-worker' on the patched trace HUNG (rc=124 after ${REPLAY_TIMEOUT_S}s). A hang is not a refusal; the refusal is supposed to happen before any replay work starts."
elif [[ "$REPLAY_PATCHED_RC" -eq 0 ]]; then
	check_fail "(D) 'ct-mcr replay-worker' REPLAYED the patched trace (rc=0). It ran the original code past the patch boundary against the patched code's events and called it a success."
elif ! grep -q "CodePatchEvent" "$OUT/replay-patched.log"; then
	check_fail "(D) the patched replay failed (rc=$REPLAY_PATCHED_RC) but not because of the CodePatchEvent; its message does not mention one. A failure for an unrelated reason is not the refusal being tested. See $OUT/replay-patched.log"
else
	log "(D) refused, by geid:"
	grep -m1 "CodePatchEvent" "$OUT/replay-patched.log" | cut -c1-200
fi

# The negative control, on a CHEAP unpatched recording rather than the Godot
# one: what is being controlled is whether the refusal is SPECIFIC to the event,
# and any no-patch container answers that. Replaying the 1.3M-event Godot
# control would answer the same question in twenty minutes.
#
# THE CONTROL IS A POSITIVE MARKER, NOT AN ABSENCE. `replay-worker` ends by
# LISTENING on a query socket, so it never exits on its own and the timeout's
# rc 124 is its NORMAL outcome here — an exit code says nothing. What is checked
# is that the log contains `record provenance:`, which is printed by the very
# function that would have refused: reaching that line is proof the check ran
# and passed, where "no CodePatchEvent in the log" alone would also be satisfied
# by a worker that died before the check.
log "(D) a replay of an unpatched recording must NOT hit that refusal"
CONTROL_TINY_SRC="$OUT/replay-control.c"
CONTROL_TINY_BIN="$OUT/replay-control-target"
CONTROL_TINY_CT="$OUT/replay-control.ct"
cat > "$CONTROL_TINY_SRC" <<'CEOF'
#include <stdio.h>
int main(void) { printf("hlx-m7-replay-control\n"); return 0; }
CEOF
command -v gcc >/dev/null || die "(D) gcc not on PATH; the negative control needs a target to record"
gcc -O0 -o "$CONTROL_TINY_BIN" "$CONTROL_TINY_SRC" 2>"$OUT/replay-control-build.log" \
	|| die "(D) could not build the negative-control target; see $OUT/replay-control-build.log"
env -u REPRO_HCR_AGENT_SOCKET "$CT_MCR" record --output "$CONTROL_TINY_CT" -- \
	"$CONTROL_TINY_BIN" > "$OUT/replay-control-record.log" 2>&1
TINY_REC_RC=$?
if [[ "$TINY_REC_RC" -ne 0 ]]; then
	check_fail "(D) could not record the negative control (rc=$TINY_REC_RC); without it the refusal above proves only that something failed. See $OUT/replay-control-record.log"
else
	# A SHORT timeout on purpose. The control only has to reach the provenance
	# check, which happens in the first seconds; after that `replay-worker`
	# parks on its query socket forever and every further second is spent
	# waiting for something that never happens.
	timeout --signal=TERM "${REPLAY_CONTROL_TIMEOUT_S:-90}" \
		"$CT_MCR" replay-worker "$CONTROL_TINY_CT" \
		> "$OUT/replay-control.log" 2>&1
	REPLAY_CONTROL_RC=$?
	if grep -q "CodePatchEvent" "$OUT/replay-control.log"; then
		check_fail "(D) the CodePatchEvent refusal fired on a recording with NO patch in it. The refusal is not specific to the event, so the refusal above is not evidence about the event."
	elif ! grep -q "record provenance:" "$OUT/replay-control.log"; then
		check_fail "(D) the negative-control replay never reached the provenance check (rc=$REPLAY_CONTROL_RC), so it cannot show the refusal is specific. See $OUT/replay-control.log"
	else
		log "(D) the unpatched recording reached the provenance check and was NOT refused (replay rc=$REPLAY_CONTROL_RC; 124 is normal — replay-worker idles on its query socket)"
	fi
fi

# --- falsifier arms ---------------------------------------------------------
# Each arm mutates a COPY of the produced artifacts and requires the checker to
# go red. An arm that stays green means the checker does not test the property
# the arm removed, and the gate says so rather than recording a verification.
ARMS_DIR="$OUT/falsifiers"
mkdir -p "$ARMS_DIR"
arm_failures=0

run_arm() {
	local name="$1" events="$2" info="$3" driver="$4"
	local out="$ARMS_DIR/$name.log"
	python3 "$REPO/scripts/verify_code_patch_event.py" \
		--patched-events "$events" --patched-info "$info" \
		--driver-json "$driver" \
		--control-events "$OUT/control-trace-events.txt" \
		--control-info "$OUT/control-trace-info.txt" \
		--patch-id "$PATCH_ID" --symbol "$TARGET_SYMBOL" > "$out" 2>&1
	local rc=$?
	if [[ "$rc" -eq 0 ]]; then
		printf '[hcr-m7] FALSIFIER-HOLLOW: arm %s exited 0 with the defect present. See %s\n' "$name" "$out" >&2
		arm_failures=$((arm_failures + 1))
	else
		printf '[hcr-m7] arm %-24s RED (rc=%d): %s\n' "$name" "$rc" \
			"$(grep -m1 '^CHECK-FAIL' "$out" | cut -c1-140)"
	fi
}

prepare_arm() {
	# Copies the three patched-arm artifacts into a per-arm directory and echoes
	# the directory. The mutation is applied by the caller to the copy, never to
	# the originals.
	local name="$1"
	local dir="$ARMS_DIR/$name"
	mkdir -p "$dir"
	cp "$PATCHED_OUT/trace-events.txt" "$dir/events.txt"
	cp "$PATCHED_OUT/trace-info.txt" "$dir/info.txt"
	cp "$PATCHED_OUT/patch-result.json" "$dir/driver.json"
	printf '%s\n' "$dir"
}

log "running falsifier arms"

# F1 — the event is REMOVED. This is the pre-HLX-M7 world exactly: the patch
# still happened, the trace still says nothing about it.
d="$(prepare_arm event-removed)"
grep -v " type=evCodePatch " "$PATCHED_OUT/trace-events.txt" > "$d/events.txt"
python3 - "$d/info.txt" <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1])
out = []
for line in p.read_text().splitlines():
    if line.startswith("events: "):
        out.append("events: %d" % (int(line.split(": ")[1]) - 1))
    elif line.startswith("codePatches: "):
        out.append("codePatches: 0")
    else:
        out.append(line)
p.write_text("\n".join(out) + "\n")
PY
run_arm event-removed "$d/events.txt" "$d/info.txt" "$d/driver.json"

# F2 — the digests are ZEROED. A placeholder that is the right shape.
d="$(prepare_arm hashes-zeroed)"
sed -E 's/(codeHashBefore=sha256:)[0-9a-f]{64}/\1'"$(printf '0%.0s' {1..64})"'/; s/(codeHashAfter=sha256:)[0-9a-f]{64}/\1'"$(printf '0%.0s' {1..64})"'/' \
	"$PATCHED_OUT/trace-events.txt" > "$d/events.txt"
run_arm hashes-zeroed "$d/events.txt" "$d/info.txt" "$d/driver.json"

# F3 — `codeHashAfter` is FABRICATED: a real, well-formed SHA-256 that is
# simply not the hash of the bytes the event says it covers. This is the arm
# that a "is it 64 hex digits" check would pass and a recomputation kills.
d="$(prepare_arm hash-fabricated)"
FAKE="$(printf 'not-the-code' | sha256sum | cut -d' ' -f1)"
sed -E "s/(codeHashAfter=sha256:)[0-9a-f]{64}/\1$FAKE/" \
	"$PATCHED_OUT/trace-events.txt" > "$d/events.txt"
run_arm hash-fabricated "$d/events.txt" "$d/info.txt" "$d/driver.json"

# F4 — the published word no longer encodes a jump to the recorded dispatch
# address. The bytes and the addresses stop agreeing.
d="$(prepare_arm jump-target-broken)"
python3 - "$PATCHED_OUT/trace-events.txt" "$d/events.txt" <<'PY'
import re, sys
src, dst = sys.argv[1], sys.argv[2]
out = []
for line in open(src, errors="replace"):
    if " type=evCodePatch " in line:
        m = re.search(r"wordAfter=0x([0-9A-Fa-f]{16})", line)
        word = int(m.group(1), 16)
        # Perturb the rel32 by one byte, leaving the E9 and the NOP fill intact
        # so only the TARGET is wrong.
        b = bytearray(word.to_bytes(8, "little"))
        b[1] ^= 0x10
        line = line.replace(m.group(0),
                            "wordAfter=0x%016X" % int.from_bytes(b, "little"))
        # and keep the hash consistent with the mutated bytes, so this arm
        # tests the JUMP-TARGET check specifically and not the hash check.
        import hashlib
        line = re.sub(r"codeHashAfter=sha256:[0-9a-f]{64}",
                      "codeHashAfter=sha256:" + hashlib.sha256(bytes(b)).hexdigest(),
                      line)
    out.append(line)
open(dst, "w").writelines(out)
PY
run_arm jump-target-broken "$d/events.txt" "$d/info.txt" "$d/driver.json"

# F5 — `patchedSymbols` is emptied. The event says the code changed but not
# which code, which is not the event §7.2 specifies.
d="$(prepare_arm symbols-emptied)"
sed -E 's/patchedSymbols=[^ ]+/patchedSymbols=-/' \
	"$PATCHED_OUT/trace-events.txt" > "$d/events.txt"
run_arm symbols-emptied "$d/events.txt" "$d/info.txt" "$d/driver.json"

# F6 — the publication tier is dropped, so a reader would take the approximate
# tier-1 boundary for an exact one (design §10.3).
d="$(prepare_arm tier-dropped)"
sed -E 's/ tier=[^ ]+//' "$PATCHED_OUT/trace-events.txt" > "$d/events.txt"
run_arm tier-dropped "$d/events.txt" "$d/info.txt" "$d/driver.json"

# F7 — THE PRODUCER-SIDE NEGATIVE, and the only arm that mutates nothing. The
# checker is pointed at the UNPATCHED recording, where the event was genuinely
# never emitted. Every arm above tests the checker; this one tests that the
# producer really is the reason the other artifacts pass.
d="$(prepare_arm control-as-patched)"
run_arm control-as-patched "$OUT/control-trace-events.txt" \
	"$OUT/control-trace-info.txt" "$d/driver.json"

# F8 — THE DUMP IS TRUNCATED AFTER THE EVENT. Added by review, 2026-09-10,
# because no arm above exercised the one property this campaign keeps losing:
# "an absence read out of a dump nobody proved complete".
#
# The mutation keeps the `evCodePatch` line and EVERY field in it byte-for-byte
# correct, keeps `trace info` untouched, and simply drops the ~4,600 lines that
# followed. So all eighteen field checks — every recomputed digest, the rel32
# decode, the bundle anchor — still pass, and the ONLY thing that can catch it
# is the line-count-equals-reported-count check. A gate that had settled for
# `lines > 0` (or for `grep -q`) would go green on a 1.34-million-line dump
# that is missing its tail. Measured: RED, and red on exactly that check.
d="$(prepare_arm dump-truncated)"
CPE_LINE="$(grep -n " type=evCodePatch " "$PATCHED_OUT/trace-events.txt" | head -1 | cut -d: -f1)"
if [[ -z "$CPE_LINE" ]]; then
	check_fail "arm dump-truncated could not be built: no evCodePatch line to truncate after"
else
	head -n "$((CPE_LINE + 10))" "$PATCHED_OUT/trace-events.txt" > "$d/events.txt"
	run_arm dump-truncated "$d/events.txt" "$d/info.txt" "$d/driver.json"
fi

# F9 — THE SUPPORT PROFILE IS FORGED. Added by review, 2026-09-10. `tier` had
# an arm and `supportProfile` did not, though it is the field §7.3 uses to
# refuse a bundle on a foreign host — the one place a wrong-but-plausible value
# is most costly. The mutation is a real profile string one version off, and it
# is caught only because the checker compares the trace's value against the one
# the DRIVER reports the agent negotiated: a second transport, as with the
# bundle digest, rather than the event agreeing with itself.
d="$(prepare_arm profile-forged)"
sed -E 's/supportProfile=[^ ]+/supportProfile=linux-x86_64-elf-direct-hcr-v2/' \
	"$PATCHED_OUT/trace-events.txt" > "$d/events.txt"
run_arm profile-forged "$d/events.txt" "$d/info.txt" "$d/driver.json"

if [[ "$arm_failures" -ne 0 ]]; then
	check_fail "$arm_failures falsifier arm(s) exited 0 with the defect present. The gate does not discriminate on those properties; strengthen it rather than recording a verification."
else
	log "all 9 falsifier arms went RED"
fi

# --- verdict ----------------------------------------------------------------
log "artifacts in $OUT"
if [[ "$fail_count" -ne 0 ]]; then
	log "RESULT: $fail_count check(s) FAILED"
	exit 1
fi
log "RESULT: PASS — a patched recording is distinguishable from an unpatched one"
log "         by a CodePatchEvent whose every field was recomputed and checked."
