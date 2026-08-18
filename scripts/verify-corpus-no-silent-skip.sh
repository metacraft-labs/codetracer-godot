#!/usr/bin/env bash
# CodeTracer GDScript recorder — GT1 NO-SILENT-SKIP gate.
#
# The teeth that make "every GDScript feature is covered" real and durable. It
# hard-fails (exits nonzero) if the corpus could pass by discovering or verifying
# NOTHING. Mirrors the BEAM recorder's verify-*-no-silent-skip.sh discipline.
#
# It HARD-FAILS on ANY of:
#   (a) zero programs discovered in the manifest,
#   (b) any manifest program lacking a verifier and/or golden,
#   (c) any test marked skipped / ignored / xfail (in the manifest or verifiers),
#   (d) the patched-engine binary missing,
#   (e) any corpus program that records but produces ZERO steps (silent-empty .ct),
#   (f) manifest/disk drift — a manifest program absent on disk, or a .gd on disk
#       absent from the manifest.
#
# Implements: verify_gdscript_corpus_no_silent_skip
#
# Usage:
#   scripts/verify-corpus-no-silent-skip.sh
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/corpus-lib.sh"

corpus_require_tools

fail_count=0
gate_fail() { printf 'GATE-FAIL: %s\n' "$*" >&2; fail_count=$((fail_count + 1)); }

# --- (d) engine binary present --------------------------------------------
if [[ ! -x "$BIN" ]]; then
	gate_fail "(d) patched-engine binary missing at $BIN"
	# Without the engine we cannot do the 0-step check; report what we can and exit.
	die "no engine binary — cannot run the corpus (this IS a hard fail)"
fi
log "(d) engine present: $BIN"

# --- (a) at least one program discovered ----------------------------------
mapfile -t ROWS < <(corpus_records)
if [[ "${#ROWS[@]}" -eq 0 ]]; then
	gate_fail "(a) zero programs discovered in the manifest ($MANIFEST)"
	die "manifest yields no programs — hard fail"
fi
log "(a) manifest yields ${#ROWS[@]} program row(s)"

# --- (c) no skip/ignore/xfail markers -------------------------------------
# In the manifest role column …
if corpus_records | awk -F'|' '{gsub(/ /,"",$3); print $3}' | grep -qiE '^(skip|ignore|xfail)$'; then
	gate_fail "(c) a manifest row has role skip/ignore/xfail"
fi
# … or a stray skip flag anywhere in a machine row …
if corpus_records | grep -qiE '\|[[:space:]]*(skip|xfail|ignore)[[:space:]]*(\||$)'; then
	gate_fail "(c) a manifest row carries a skip/xfail/ignore flag"
fi
# … or a skip construct in any corpus verifier.
if grep -RilE 'pytest\.skip|unittest\.skip|@skip|xfail|SKIP_?TEST|@unittest\.skip' "$REPO/scripts/verify_"*.py 2>/dev/null; then
	gate_fail "(c) a verify_*.py contains a skip/xfail construct"
fi
[[ "$fail_count" -eq 0 ]] && log "(c) no skip/ignore/xfail markers found"

# --- (f) manifest <-> disk drift ------------------------------------------
manifest_progs="$(corpus_manifest_programs)"
disk_progs="$(corpus_disk_programs)"
# CORPUS.md is documentation, not a program; it is not expected on the program list.
missing_on_disk="$(comm -23 <(printf '%s\n' "$manifest_progs") <(printf '%s\n' "$disk_progs") || true)"
missing_in_manifest="$(comm -13 <(printf '%s\n' "$manifest_progs") <(printf '%s\n' "$disk_progs") || true)"
if [[ -n "$missing_on_disk" ]]; then
	gate_fail "(f) manifest program(s) not on disk: $(echo $missing_on_disk)"
fi
if [[ -n "$missing_in_manifest" ]]; then
	gate_fail "(f) .gd file(s) on disk not in the manifest (drift): $(echo $missing_in_manifest)"
fi
[[ -z "$missing_on_disk$missing_in_manifest" ]] && log "(f) manifest and disk .gd sets match"

# --- (b) every program has a verifier and/or golden -----------------------
# Collect the set of files referenced as helper deps by some primary.
referenced_helpers="$(corpus_records | awk -F'|' '{gsub(/^ +| +$/,"",$3)} $3=="primary"{print $4}' | tr ' ' '\n' | sort -u)"
while IFS='|' read -r program milestone role entry verifier cmd golden markers; do
	program="${program// /}"; role="${role// /}"; verifier="${verifier// /}"; golden="${golden// /}"
	case "$role" in
		primary|probe)
			[[ "$golden" != "-" && -f "$REPO/scripts/$golden" ]] \
				|| gate_fail "(b) $program: golden missing ($golden)"
			if [[ "$role" == "primary" ]]; then
				[[ "$verifier" != "-" && -f "$REPO/scripts/$verifier" ]] \
					|| gate_fail "(b) $program: verifier missing ($verifier)"
			fi
			;;
		helper)
			[[ "$golden" != "-" && -f "$REPO/scripts/$golden" ]] \
				|| gate_fail "(b) $program: helper golden missing ($golden)"
			printf '%s\n' "$referenced_helpers" | grep -qx "$program" \
				|| gate_fail "(b) helper $program is not referenced by any primary (dead helper)"
			;;
		*)
			gate_fail "(b) $program: unknown role '$role'"
			;;
	esac
done < <(corpus_records)
[[ "$fail_count" -eq 0 ]] && log "(b) every program has a verifier and/or golden"

# --- (e) no program records ZERO steps ------------------------------------
# Skip the (expensive) recording pass if the structural checks already failed —
# a structurally broken manifest is a hard fail regardless of step counts.
if [[ "$fail_count" -gt 0 ]]; then
	die "no-silent-skip gate FAILED with $fail_count structural violation(s) (recording pass skipped)"
fi
WORK="$(mktemp -d "${TMPDIR:-/tmp}/ct-nss.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
zero_step=0
while IFS='|' read -r program milestone role entry verifier cmd golden markers; do
	program="${program// /}"; role="${role// /}"
	[[ "$role" == "helper" ]] && continue
	read -r -a entry_files <<< "$entry"
	IFS=$'\t' read -r FULL OUT CT < <(corpus_record "$WORK" "${entry_files[@]}")
	n="$(corpus_step_count "$FULL")"
	if [[ "$n" -le 0 ]]; then
		gate_fail "(e) $program recorded ZERO steps (silently-empty trace)"
		zero_step=$((zero_step + 1))
	else
		printf '  %s [%s]: %s steps\n' "$program" "$milestone" "$n"
	fi
done < <(corpus_records)
[[ "$zero_step" -eq 0 ]] && log "(e) every recorded program produced > 0 steps"

# --- verdict ---------------------------------------------------------------
if [[ "$fail_count" -gt 0 ]]; then
	die "no-silent-skip gate FAILED with $fail_count violation(s)"
fi
log "NO-SILENT-SKIP GATE PASSED — corpus cannot pass by discovering or verifying nothing"
