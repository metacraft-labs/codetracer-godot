#!/usr/bin/env bash
# CodeTracer GDScript recorder — GT1 UNIFIED corpus runner.
#
# The single entry point for the whole GDScript feature corpus. It discovers
# every program from the authoritative manifest (test-programs/gdscript/CORPUS.md)
# and, for each `primary` program: uses the patched engine, records the program
# headless with CT_GDSCRIPT_TRACE, decodes the produced .ct with `ct-print --full`,
# checks the deterministic result marker(s), and runs the program's verifier
# (scripts/verify_*.py — the SAME first-principles assertions the per-milestone
# record-and-verify-*.sh runners use). `probe` rows get an inline halt-check.
#
# It runs the WHOLE suite (G2 … GF13 — every GF/G milestone), collects pass/fail
# per program, and EXITS NONZERO if any program fails. This is the durable,
# CI-enforced consolidation of the GF/G test suite:
#     verify_gdscript_feature_corpus_all_pass
#
# No mocks: real patched engine, real programs, real .ct, real ct-print.
#
# Usage:
#   scripts/record-and-verify.sh               # BUILD=never (default; binary must exist)
#   BUILD=auto   scripts/record-and-verify.sh  # build the engine if missing
#   BUILD=always scripts/record-and-verify.sh  # force a rebuild first
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/corpus-lib.sh"

corpus_require_tools
corpus_ensure_engine
log "engine: $BIN"
log "manifest: $MANIFEST"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/ct-corpus.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

# --- run every primary/probe program in the manifest -----------------------
total=0
passed=0
declare -a FAILED=()
declare -a RESULTS=()

# Check all ';'-separated markers appear in a stdout log.
check_markers() {
	local log_file="$1" markers="$2" m
	[[ "$markers" == "-" ]] && return 0
	local IFS=';'
	for m in $markers; do
		[[ -z "$m" ]] && continue
		grep -qF "$m" "$log_file" || { printf 'missing marker %q\n' "$m" >&2; return 1; }
	done
	return 0
}

# Inline halt-check for the failing-assert probe (mirrors record-and-verify-gf13.sh).
verify_probe() {
	local full="$1" stdout_log="$2"
	if grep -qF "SHOULD_NOT_REACH" "$stdout_log"; then
		echo "failing-assert probe: 'SHOULD_NOT_REACH' printed — the assert did NOT halt" >&2
		return 1
	fi
	python3 - "$full" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1]))
lines = [e.get("line") for e in doc["events"] if e["kind"] == "step"]
assert 20 in lines, f"expected a step at the assert line 20; got {lines}"
assert 21 not in lines, f"line 21 (after the failing assert) must NOT record a step; got {lines}"
print(f"probe OK: assert-line step present (20), post-assert line 21 absent; steps={lines}")
PY
}

while IFS='|' read -r program milestone role entry verifier cmd golden markers; do
	program="${program// /}"; role="${role// /}"
	[[ "$role" == "helper" ]] && continue   # compiled as part of a primary; not run standalone

	total=$((total + 1))
	log "[$milestone] $program ($role)"

	# record (entry may name several files: entry + helper deps, space-separated)
	read -r -a entry_files <<< "$entry"
	IFS=$'\t' read -r FULL OUT CT < <(corpus_record "$WORK" "${entry_files[@]}")
	grep -E '^CT_' "$OUT" || true

	ok=1
	nsteps="$(corpus_step_count "$FULL")"
	if [[ "$nsteps" -le 0 ]]; then
		echo "FAIL: $program produced ZERO steps (silently-empty trace)" >&2
		ok=0
	fi
	if [[ "$ok" == 1 ]] && ! check_markers "$OUT" "$markers"; then
		echo "FAIL: $program missing result marker(s)" >&2
		ok=0
	fi
	if [[ "$ok" == 1 ]]; then
		if [[ "$role" == "probe" ]]; then
			verify_probe "$FULL" "$OUT" || ok=0
		else
			python3 "$REPO/scripts/$verifier" "$cmd" "$FULL" || ok=0
		fi
	fi

	if [[ "$ok" == 1 ]]; then
		passed=$((passed + 1))
		RESULTS+=("PASS  [$milestone] $program ($nsteps steps)")
		printf 'PASS: %s [%s] — %s steps\n' "$program" "$milestone" "$nsteps"
	else
		FAILED+=("$program")
		RESULTS+=("FAIL  [$milestone] $program")
		printf 'FAIL: %s [%s]\n' "$program" "$milestone"
	fi
done < <(corpus_records)

# --- summary ---------------------------------------------------------------
log "CORPUS SUMMARY"
for r in "${RESULTS[@]}"; do echo "  $r"; done
echo ""
manifest_primaries="$(corpus_records | awk -F'|' '{gsub(/ /,"",$3)} $3!="helper"' | wc -l | tr -d ' ')"
disk_gd="$(corpus_disk_programs | wc -l | tr -d ' ')"
manifest_all="$(corpus_manifest_programs | wc -l | tr -d ' ')"
echo "programs run (primary+probe): $total / manifest primaries+probes: $manifest_primaries"
echo "manifest programs (all roles): $manifest_all / .gd files on disk: $disk_gd"
echo "passed: $passed / $total"

[[ "$total" -gt 0 ]] || die "no corpus programs discovered"
[[ "$total" == "$manifest_primaries" ]] || die "ran $total but manifest lists $manifest_primaries primary/probe programs"
if [[ "${#FAILED[@]}" -gt 0 ]]; then
	die "corpus FAILED: ${FAILED[*]}"
fi

log "ALL $passed/$total CORPUS PROGRAMS PASSED (G2 … GF13)"
