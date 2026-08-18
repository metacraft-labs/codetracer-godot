#!/usr/bin/env bash
# CodeTracer GDScript recorder — N1 (Nested-Trace Join Keys + Correlation Record)
# record-and-verify runner.
#
# Records test-programs/gdscript/n1_nested.gd with the patched engine under a
# CONTROLLED parent-native context (CT_MCR_GEID / CT_MCR_TICK — the shim standing
# in for the ct-mcr live-GEID interface), decodes its .ct with ct-print --full,
# and asserts via scripts/verify_n1.py that the recorder tags call-entry/exit and
# native-call boundaries with (GEID, tick) join keys that are well-formed and
# RESOLVABLE against a SYNTHETIC native trace per the correlation record
# (codetracer-trace-format-spec/nested-trace-correlation.md §3).
#
# N1 CHANGES the recorder (join-key emission in gdscript_ct_trace.{h,cpp} + two
# native-call hooks in gdscript_vm.cpp), so the default BUILD is `auto` (rebuild
# if a source file is newer than the binary). Pass BUILD=never to fail loudly if
# the binary is stale.
#
# It then:
#   - PROVES the verifier has teeth with tamper runs (each MUST be rejected);
#   - proves the join emission is INERT STANDALONE: the SAME program recorded with
#     NO CT_MCR_* (and not under ct-mcr) produces ZERO join events — the standalone
#     trace is byte-identical to a pre-N1 recording;
#   - re-runs the whole GT1 corpus (record-and-verify.sh, BUILD=never) to confirm
#     the existing corpus is byte-identical (join code inert without a context).
#
# No mocks: real patched engine, real n1_nested.gd, real .ct, real ct-print. The
# SYNTHETIC native trace (a controlled geid.idx stand-in built in verify_n1.py) is
# the sole test fixture — it stands in for the N2 MCR constellation (a full MCR
# run of the patched Godot needs the Linux substrate + a real ct-mcr recording).
#
# EXITS NONZERO on any mismatch.
#
# Usage:
#   scripts/record-and-verify-n1.sh              # BUILD=auto (default)
#   BUILD=never  scripts/record-and-verify-n1.sh # do not rebuild
#   BUILD=always scripts/record-and-verify-n1.sh # force a rebuild first
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

PLATFORM="${PLATFORM:-macos}"
ARCH="${ARCH:-arm64}"
TARGET="${TARGET:-template_debug}"
BIN="$REPO/bin/godot.${PLATFORM}.${TARGET}.${ARCH}"
CT_PRINT="${CT_PRINT:-$REPO/../codetracer-trace-format-nim/ct-print}"
BUILD="${BUILD:-auto}"

# The controlled parent-native context base (the synthetic native geid.idx starts
# here). verify_n1.py builds an INDEPENDENT native index from these same bases.
BASE_GEID="${CT_MCR_GEID:-1000}"
BASE_TICK="${CT_MCR_TICK:-500000}"

log()  { printf '\n=== %s ===\n' "$*"; }
die()  { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

[[ -x "$CT_PRINT" ]] || die "ct-print not found/executable at $CT_PRINT"
command -v python3 >/dev/null || die "python3 not found"

# --- build the patched engine if needed (N1 adds new recorder code) ----------
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

# --- work dir --------------------------------------------------------------
WORK="$(mktemp -d "${TMPDIR:-/tmp}/ct-n1.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

# record <entry.gd> <tag> [MCR]  -> prints "<full.json>\t<stdout.log>"
#   MCR present => record under the controlled CT_MCR_GEID/CT_MCR_TICK context.
record() {
	local entry="$1" tag="$2" mcr="${3:-}"
	local name proj trace_dir full stdout_log
	name="$(basename "$entry" .gd)-$tag"
	proj="$WORK/$name"
	trace_dir="$proj/trace"
	mkdir -p "$proj" "$trace_dir"
	cp "$REPO/test-programs/gdscript/$entry" "$proj/$entry"
	cat > "$proj/project.godot" <<-EOF
		config_version=5
		[application]
		config/name="ctn1-$name"
	EOF
	stdout_log="$WORK/$name.stdout"
	if [[ -n "$mcr" ]]; then
		CT_MCR_GEID="$BASE_GEID" CT_MCR_TICK="$BASE_TICK" CT_GDSCRIPT_TRACE="$trace_dir" \
			"$BIN" --headless --path "$proj" --script "res://$entry" \
			>"$stdout_log" 2>&1 || true
	else
		CT_GDSCRIPT_TRACE="$trace_dir" \
			"$BIN" --headless --path "$proj" --script "res://$entry" \
			>"$stdout_log" 2>&1 || true
	fi
	local ct="$trace_dir/gdscript_trace.ct"
	[[ -f "$ct" ]] || die "no trace produced at $ct"
	full="$WORK/$name.full.json"
	"$CT_PRINT" --full "$ct" > "$full" || die "ct-print --full failed on $ct"
	printf '%s\t%s\n' "$full" "$stdout_log"
}

# --- N1 deliverable: join keys under a controlled MCR context ---------------
log "recording n1_nested.gd UNDER controlled MCR context (CT_MCR_GEID=$BASE_GEID CT_MCR_TICK=$BASE_TICK)"
IFS=$'\t' read -r N1_FULL N1_OUT < <(record n1_nested.gd mcr MCR)
cat "$N1_OUT"
grep -qF "CT_N1_RESULT=12" "$N1_OUT" || die "n1_nested.gd: stdout missing 'CT_N1_RESULT=12'"

python3 "$REPO/scripts/verify_n1.py" verify "$N1_FULL" "$BASE_GEID" "$BASE_TICK" \
	|| die "n1_nested.gd: verify_n1.py assertions failed"

# --- prove the verifier has teeth ------------------------------------------
log "tamper runs (each MUST be rejected by verify_n1.py)"
for mode in geid step; do
	python3 "$REPO/scripts/verify_n1.py" tamper "$N1_FULL" "$BASE_GEID" "$BASE_TICK" "$mode" \
		|| die "tamper($mode) was NOT rejected — verifier is vacuous"
done

# --- prove INERT STANDALONE: no MCR context => zero join events -------------
log "recording n1_nested.gd STANDALONE (no CT_MCR_* — join emission must be inert)"
IFS=$'\t' read -r STD_FULL STD_OUT < <(record n1_nested.gd standalone)
grep -qF "CT_N1_RESULT=12" "$STD_OUT" || die "standalone n1_nested.gd: stdout missing 'CT_N1_RESULT=12'"
python3 - "$STD_FULL" <<'PY' || die "standalone recording emitted join events — NOT inert"
import json, sys
doc = json.load(open(sys.argv[1]))
joins = [e for e in doc["events"]
         if e["kind"] == "io" and e.get("text", "").startswith("ct-nested-join:")]
if joins:
    print(f"FAIL: {len(joins)} join events in a STANDALONE recording (must be 0)",
          file=sys.stderr)
    sys.exit(1)
print("OK: standalone recording has 0 join events (join emission inert without a context).")
PY

# --- regression: whole GT1 corpus byte-identical (join code inert) ----------
log "regression: GT1 corpus via record-and-verify.sh (BUILD=never)"
BUILD=never "$REPO/scripts/record-and-verify.sh"

log "ALL N1 + tamper + inert-standalone + GT1-corpus-regression checks PASSED"
