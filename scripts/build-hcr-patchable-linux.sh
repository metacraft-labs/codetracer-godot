#!/usr/bin/env bash
# CodeTracer patched Godot engine — build an HCR-PATCHABLE Linux engine.
#
# The default engine this repo builds cannot be hot-patched, and nothing about
# it says so. Measured on `bin/godot.linuxbsd.template_debug.x86_64`
# (4.6.2.stable.custom_build, GCC 15.3.0, 84 MB) on 2026-09-10:
#
#   * no `__patchable_function_entries`  -> no NOP sleds, so there is no site
#     the provider can publish a branch into: every request is refused
#     `absent-sled`;
#   * stripped (`LINKFLAGS += -s`, SConstruct's `debug_symbols=no` branch)
#     -> no `.symtab`; 342 defined FUNC symbols in `.dynsym` against 215,264
#     real functions (0.16%), and `main` is not among them;
#   * no `.note.gnu.build-id` -> HLX-M1's mandatory verification refuses the
#     object with `elf-build-id-absent`.
#
# This script builds the same engine with the three properties fixed. It is the
# reproducible form of that recipe; see `hcr_patchable=yes` in the SConstruct.
#
# WHERE THE FLAGS COME FROM. Not from here. `-fpatchable-function-entry`,
# `-falign-functions` and `--build-id` are defined in the Reprobuild project
# DSL (`repro_project_dsl/runtime_core.nim`), which is also what the HCR
# provider's own gates read. This script compiles and runs
# `reprobuild/scripts/hcr_patchable_profile.nim` to project those definitions
# into the environment, so the engine and the provider cannot drift. A drift
# here would be SILENT — a wrong `-fpatchable-function-entry` operand compiles,
# links and runs, and only shows up much later as a refusal that looks like a
# provider bug.
#
# WHAT IS DELIBERATELY *NOT* DONE: `debug_symbols=yes`. The provider resolves
# functions from `.symtab`/`.dynsym`, never from DWARF, so `-g` would buy
# nothing and cost an order of magnitude in object size and link time. The
# SConstruct's `hcr_patchable` branch therefore takes a third option upstream
# Godot does not offer: no `-g`, and no `-s` either.
#
# Usage:
#   scripts/build-hcr-patchable-linux.sh [-j N] [extra scons args...]
#
# Environment:
#   REPROBUILD_DIR  path to the reprobuild checkout (default: ../reprobuild)
#   JOBS            parallelism (default: nproc-4, floor 1)
#   BUILD_LOG       log file (default: <mktemp>)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPROBUILD_DIR="${REPROBUILD_DIR:-$(cd "$REPO/.." && pwd)/reprobuild}"
JOBS="${JOBS:-$(( $(nproc) > 5 ? $(nproc) - 4 : 1 ))}"

log() { printf '[hcr-build] %s\n' "$*"; }
die() { printf '[hcr-build] FATAL: %s\n' "$*" >&2; exit 1; }

[[ -d "$REPROBUILD_DIR" ]] || die "reprobuild checkout not found at $REPROBUILD_DIR (set REPROBUILD_DIR)"
PROFILE_SRC="$REPROBUILD_DIR/scripts/hcr_patchable_profile.nim"
[[ -f "$PROFILE_SRC" ]] || die "profile emitter missing: $PROFILE_SRC"

# --- 1. project the patchable profile out of the Reprobuild DSL ------------
PROFILE_DIR="$(mktemp -d)"
trap 'rm -rf "$PROFILE_DIR"' EXIT
log "compiling the profile emitter from $PROFILE_SRC"
(
	cd "$REPROBUILD_DIR"
	nix develop --no-write-lock-file --command \
		nim c -d:release --hints:off --warnings:off \
		--outdir:"$PROFILE_DIR" scripts/hcr_patchable_profile.nim >/dev/null
) || die "could not build the profile emitter"

PROFILE_TEXT="$("$PROFILE_DIR/hcr_patchable_profile")" \
	|| die "the profile emitter refused to emit a profile (see its stderr above)"
eval "$PROFILE_TEXT"

# A projection that produced nothing must not be mistaken for a projection that
# produced "no flags needed". Assert both variables are populated here, even
# though the SConstruct asserts the compile side again on its own.
[[ -n "${HCR_PATCHABLE_CCFLAGS:-}" ]]   || die "HCR_PATCHABLE_CCFLAGS came back empty"
[[ -n "${HCR_PATCHABLE_LINKFLAGS:-}" ]] || die "HCR_PATCHABLE_LINKFLAGS came back empty"
# NOTE, because this is the obvious place to add one and it is the wrong place:
# a falsifier-arm define does NOT go in the engine-wide CCFLAGS. Changing them
# invalidates every object in the tree, so an armed build becomes a FULL rebuild
# (thirdparty/embree and all) — measured on 2026-09-11 while bringing up
# GDH-M5's arms. `CT_GDH5_FALSIFY` in `modules/gdscript/SCsub` scopes the define
# to the one module that needs it, and an armed build is then one translation
# unit plus a relink.
log "profile CCFLAGS  : $HCR_PATCHABLE_CCFLAGS"
log "profile LINKFLAGS: $HCR_PATCHABLE_LINKFLAGS"
export HCR_PATCHABLE_CCFLAGS HCR_PATCHABLE_LINKFLAGS

# --- 1b. the in-target agent -----------------------------------------------
# A patchable-SHAPED engine still cannot be patched: the provider has no ptrace
# path, so the agent has to be linked in and has to connect out to a
# coordinator. Point the build at the production agent source in the reprobuild
# checkout — never a copy — so engine and coordinator share one implementation.
HCR_AGENT_SOURCE="${HCR_AGENT_SOURCE:-$REPROBUILD_DIR/libs/repro_hcr_agent/c/repro_hcr_agent.c}"
[[ -f "$HCR_AGENT_SOURCE" ]] || die "HCR agent source not found: $HCR_AGENT_SOURCE"
log "agent source     : $HCR_AGENT_SOURCE"
export HCR_AGENT_SOURCE

# --- 2. build the engine ----------------------------------------------------
# `extra_suffix=hcr` keeps the patchable engine beside the ordinary one instead
# of overwriting it, so the two can be compared byte-for-byte.
log "building the engine with -j$JOBS (load average now: $(cut -d' ' -f1-3 /proc/loadavg))"
START="$(date +%s)"
(
	cd "$REPO"
	nix develop --no-write-lock-file --command bash -c '
		set -euo pipefail
		# Godot sanitizes the child environment; `import_env_vars` copies named
		# variables through. The nix cc-wrapper reads NIX_CFLAGS_COMPILE /
		# NIX_LDFLAGS / ... from there, and it matches exact names (no glob),
		# so enumerate every NIX* name currently set.
		vars=$(env | sed -n "s/^\(NIX[A-Za-z0-9_]*\)=.*/\1/p" | paste -sd, -)
		exec scons -j"$1" \
			platform=linuxbsd target=template_debug arch=x86_64 \
			module_gdscript_enabled=yes \
			vulkan=no opengl3=no \
			disable_path_overrides=no \
			hcr_patchable=yes \
			extra_suffix=hcr \
			import_env_vars="$vars" \
			"${@:2}"
	' -- "$JOBS" "$@"
)
RC=$?
END="$(date +%s)"
log "scons exit: $RC; wall time: $(( END - START ))s"
[[ "$RC" -eq 0 ]] || exit "$RC"

BIN="$REPO/bin/godot.linuxbsd.template_debug.x86_64.hcr"
[[ -x "$BIN" ]] || die "scons succeeded but $BIN is missing"
log "built $BIN ($(stat -c%s "$BIN") bytes)"

# --- 3. prove it is patchable-shaped ---------------------------------------
# A build that succeeded is not the claim; the claim is that the three
# properties above now hold. Check each one and fail loudly if not, so a
# silently-non-patchable engine cannot leave this script looking like a pass.
log "verifying patchable shape"
python3 "$REPO/scripts/verify_hcr_patchable.py" "$BIN"
