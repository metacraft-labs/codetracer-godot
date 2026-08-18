#!/usr/bin/env bash
# CodeTracer GDScript recorder — GF13 (Diagnostics & String Formatting:
# assert / push_error / push_warning / string formatting) record-and-verify
# runner. This is the LAST GF-series milestone.
#
# GF13 DOES change the recorder (a small hook in OPCODE_CALL_UTILITY records
# push_error / push_warning as events.dat special events), so the default BUILD
# is `auto` (rebuild if a recorder source is newer than the binary). String
# formatting and `assert` need NO engine change — a formatted result is an
# ordinary String captured by the G4 assign path, and an assert statement is an
# ordinary per-line step.
#
# It records the GF13 reference program (test-programs/gdscript/gf_diag.gd) with
# the patched engine, decodes its .ct with `ct-print --full`, and asserts the
# hand-derived facts in scripts/EXPECTED-GF13.md via scripts/verify_gf13.py:
#   - each formatted string's EXACT captured value (pf="7/a/3.14", ff="x y",
#     raw="a\nb" with a LITERAL backslash, tq="line1\nline2" with an embedded
#     newline, cc="a-x"); plus i=7, ok=true;
#   - the assert line is a recorded STEP and execution CONTINUED past it;
#   - push_warning / push_error each recorded once as an io event with the right
#     kind (elkTraceLogEvent / elkError), level tag and message.
#
# It then PROVES the verifier has teeth with tamper runs (wrong formatted string
# / missing assert step / wrong push message / missing push event), each of
# which MUST be rejected.
#
# It also runs a SEPARATE failing-assert probe (gf_diag_assert_fail.gd) to
# document — honestly, with a real recording — that a FAILING assert HALTS the
# VM: the trace ends at the assert line (the recorder flushes via atexit) and the
# statement after the assert never records a step.
#
# Finally it re-runs the GF12..G2 regressions (the recorder must stay
# byte-identical for programs that call no diagnostics — the utility hook only
# fires for push_error/push_warning).
#
# EXITS NONZERO on any mismatch, crash, or corrupted trace.
#
# Usage:
#   scripts/record-and-verify-gf13.sh               # BUILD=auto (default)
#   BUILD=always scripts/record-and-verify-gf13.sh  # force a rebuild first
#   BUILD=never  scripts/record-and-verify-gf13.sh  # never build (binary must exist)
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

# --- 1. build the patched engine if needed (GF13 changes the recorder) -----
need_build=0
case "$BUILD" in
	always) need_build=1 ;;
	never)  need_build=0 ;;
	auto)
		if [[ ! -x "$BIN" ]]; then
			need_build=1
		else
			for f in modules/gdscript/gdscript_ct_trace.cpp \
			         modules/gdscript/gdscript_ct_trace.h \
			         modules/gdscript/gdscript_vm.cpp \
			         modules/gdscript/gdscript.cpp \
			         modules/gdscript/SCsub; do
				if [[ "$f" -nt "$BIN" ]]; then need_build=1; fi
			done
		fi ;;
	*) die "unknown BUILD=$BUILD (auto|always|never)" ;;
esac

if [[ "$need_build" == 1 ]]; then
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
[[ -x "$BIN" ]] || die "engine binary missing at $BIN (BUILD=$BUILD)"
log "engine: $BIN"

# --- 2. work dir -----------------------------------------------------------
WORK="$(mktemp -d "${TMPDIR:-/tmp}/ct-gf13.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

# record a single-file program.  record_one <entry.gd> -> "<full.json>\t<stdout_log>"
record_one() {
	local entry="$1"
	local name proj trace_dir full stdout_log
	name="$(basename "$entry" .gd)"
	proj="$WORK/$name"
	trace_dir="$proj/trace"
	mkdir -p "$proj" "$trace_dir"
	cp "$REPO/test-programs/gdscript/$entry" "$proj/$entry"
	cat > "$proj/project.godot" <<-EOF
		config_version=5
		[application]
		config/name="ctgf13-$name"
	EOF
	stdout_log="$WORK/$name.stdout"
	# push_error / a failing assert make the engine exit nonzero; `|| true` keeps
	# the runner going (we assert on the trace, not the exit code).
	CT_GDSCRIPT_TRACE="$trace_dir" \
		"$BIN" --headless --path "$proj" --script "res://$entry" \
		>"$stdout_log" 2>&1 || true
	local ct="$trace_dir/gdscript_trace.ct"
	[[ -f "$ct" ]] || die "no trace produced at $ct"
	full="$WORK/$name.full.json"
	"$CT_PRINT" --full "$ct" > "$full" || die "ct-print --full failed on $ct"
	printf '%s\t%s\n' "$full" "$stdout_log"
}

# --- 3. GF13 deliverable: diagnostics + string formatting ------------------
log "recording gf_diag.gd (GF13 diagnostics & string formatting)"
IFS=$'\t' read -r GF13_FULL GF13_OUT < <(record_one gf_diag.gd)
grep -E "^CT_GF13" "$GF13_OUT" || true
grep -qF "CT_GF13_RESULT=29" "$GF13_OUT" || die "gf_diag: stdout missing 'CT_GF13_RESULT=29'"

python3 "$REPO/scripts/verify_gf13.py" verify "$GF13_FULL" \
	|| die "gf_diag: verify_gf13.py assertions failed"

# --- 4. prove the verifier has teeth (tamper runs MUST be rejected) --------
log "tamper runs (each MUST be rejected by verify_gf13.py)"
for mode in fmtvalue assertstep pushmsg pushmissing; do
	python3 "$REPO/scripts/verify_gf13.py" tamper "$GF13_FULL" "$mode" \
		|| die "tamper($mode) was NOT rejected — verifier is vacuous"
done

# --- 5. failing-assert probe (SEPARATE run; documents the halt honestly) ---
log "failing-assert probe: gf_diag_assert_fail.gd (a failing assert HALTS the VM)"
IFS=$'\t' read -r FAIL_FULL FAIL_OUT < <(record_one gf_diag_assert_fail.gd)
# The statement after the failing assert must NEVER run.
if grep -qF "SHOULD_NOT_REACH" "$FAIL_OUT"; then
	die "failing-assert probe: 'SHOULD_NOT_REACH' printed — the assert did NOT halt"
fi
# The trace must exist and record the assert line (20) but NOT the print (21):
# the recorder flushed what it recorded up to the halt.
python3 - "$FAIL_FULL" <<'PY' || die "failing-assert probe: trace did not end at the assert line"
import json, sys
doc = json.load(open(sys.argv[1]))
lines = [e.get("line") for e in doc["events"] if e["kind"] == "step"]
assert 20 in lines, f"expected a step at the assert line 20; got {lines}"
assert 21 not in lines, f"line 21 (after the failing assert) must NOT record a step; got {lines}"
print(f"failing-assert probe OK: steps end at the assert (line 20 present, 21 absent); "
      f"recorded step lines = {lines}")
PY

# --- 6. regression: GF12..G2 (recorder byte-identical without diagnostics) --
log "regression: GF12..G2 via record-and-verify-gf12.sh (BUILD=never)"
BUILD=never "$REPO/scripts/record-and-verify-gf12.sh"

log "ALL GF13 + tamper + failing-assert-probe + GF12..G2-regression checks PASSED"
