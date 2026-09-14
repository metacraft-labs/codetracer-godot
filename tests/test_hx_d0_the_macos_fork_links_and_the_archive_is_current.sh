#!/usr/bin/env bash
# test_name: hx_d0_the_macos_fork_links_and_the_archive_is_current
# Gate type: integration
# Design doc: HCR/HCR-Overview.md §7
# Real components: real macOS arm64 host, real fork link, real regenerated archive
# Allowed mocks: none
#
# Verifies milestone HX-D-0:
# 1. Host is Darwin arm64 (loud unsupported on Linux or other arch).
# 2. Vendored header codetracer_trace_writer.h declares required entry points (count >= 40).
# 3. macOS arm64 archive libcodetracer_trace_writer.a is non-empty and exports floor count >= 40.
# 4. Control arm: Linux-slot archive exports match macOS archive exports.
# 5. Header declared set is fully exported by the macOS archive (0 missing symbols).
# 6. Fork link test: real compilation and linking using SCsub link line:
#    -framework Security -framework CoreFoundation -lzstd
# 7. scripts/verify-gdh3-fork.sh passes cleanly with 30 checks on macOS.
# 8. Falsifier arm (tested via --include-falsifier or standalone): asserts linking fails
#    with undefined symbols and verify-gdh3-fork.sh exits non-zero on stale archive.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

# If not already running inside a shell with zstd library available, re-exec via nix develop
if [[ -z "${IN_NIX_DEV_SHELL:-}" ]] && ! clang -lzstd -x c - -o /dev/null </dev/null 2>/dev/null; then
  if command -v nix >/dev/null 2>&1; then
    export IN_NIX_DEV_SHELL=1
    exec nix develop --command "$0" "$@"
  fi
fi

RUN_FALSIFIER=false
for arg in "$@"; do
  case "$arg" in
    --include-falsifier|--falsifier)
      RUN_FALSIFIER=true
      ;;
    --help|-h)
      echo "Usage: $0 [--include-falsifier]"
      exit 0
      ;;
  esac
done

echo "=== HX-D-0: macOS arm64 Fork Links & Archive Is Current ==="

# 1. Host check (anti-vacuity: must be Darwin arm64)
HOST_OS="$(uname -s)"
HOST_ARCH="$(uname -m)"
echo "1. Checking host platform: ${HOST_OS} ${HOST_ARCH}..."
if [[ "$HOST_OS" != "Darwin" || "$HOST_ARCH" != "arm64" ]]; then
  echo "FATAL [ANTI-VACUITY]: Host is ${HOST_OS} ${HOST_ARCH}, not Darwin arm64." >&2
  echo "This gate requires a real macOS arm64 host; a Linux run must be a loud failure, not a pass." >&2
  exit 1
fi
echo "   OK: Running on real macOS arm64 host."

# Paths
HDR="modules/gdscript/ct_writer/include/codetracer_trace_writer.h"
MAC_LIB="modules/gdscript/ct_writer/libcodetracer_trace_writer.a"
LINUX_LIB="modules/gdscript/ct_writer/linuxbsd-x86_64/libcodetracer_trace_writer.a"

for f in "$HDR" "$MAC_LIB" "$LINUX_LIB"; do
  if [[ ! -s "$f" ]]; then
    echo "FATAL: Required file $f is missing or empty!" >&2
    exit 1
  fi
done

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

NM_BIN="nm"
if [[ -x /usr/bin/nm ]]; then
  NM_BIN="/usr/bin/nm"
fi

# 2. Anti-vacuity: parse declared symbols from header
echo "2. Parsing declared trace_writer_* entry points from vendored header..."
grep -E '\btrace_writer_[a-zA-Z0-9_]+\(' "$HDR" | grep -v 'typedef' | \
  sed -E 's/.*\b(trace_writer_[a-zA-Z0-9_]+)\(.*/\1/' | sort -u > "$TMPDIR/hdr_declared.txt"

N_HDR=$(wc -l < "$TMPDIR/hdr_declared.txt" | tr -d ' ')
echo "   Found $N_HDR declared functions in $HDR"
if [[ "$N_HDR" -lt 40 ]]; then
  echo "FATAL [ANTI-VACUITY]: Expected at least 40 declared functions in header, found $N_HDR" >&2
  exit 1
fi

# 3. Anti-vacuity: scan macOS archive exports
echo "3. Reading macOS archive exported symbols..."
$NM_BIN "$MAC_LIB" > "$TMPDIR/mac_nm.txt"
if [[ ! -s "$TMPDIR/mac_nm.txt" ]]; then
  echo "FATAL [ANTI-VACUITY]: $NM_BIN produced no output for $MAC_LIB" >&2
  exit 1
fi

grep -E ' (T|D) _trace_writer_' "$TMPDIR/mac_nm.txt" | awk '{print $3}' | \
  sed 's/^_//' | sort -u > "$TMPDIR/mac_exported.txt"

N_MAC=$(wc -l < "$TMPDIR/mac_exported.txt" | tr -d ' ')
echo "   Found $N_MAC exported trace_writer_* symbols in $MAC_LIB"
if [[ "$N_MAC" -lt 40 ]]; then
  echo "FATAL [ANTI-VACUITY]: Expected at least 40 exported symbols in macOS archive, found $N_MAC" >&2
  exit 1
fi

# Assert all declared symbols are exported
echo "4. Asserting header declared set is satisfied by macOS archive..."
MISSING_DECL=$(comm -23 "$TMPDIR/hdr_declared.txt" "$TMPDIR/mac_exported.txt")
if [[ -n "$MISSING_DECL" ]]; then
  echo "FATAL: macOS archive is missing declared header symbols:" >&2
  echo "$MISSING_DECL" >&2
  exit 1
fi
echo "   OK: All $N_HDR declared symbols are exported by the macOS archive."

# Check critical entry points explicitly
for sym in trace_writer_current_path_id \
           trace_writer_register_path_version \
           trace_writer_set_recording_id \
           trace_writer_enable_line_count_table \
           trace_writer_register_path_with_line_count \
           trace_writer_clear_last_error \
           trace_writer_register_source_reload \
           trace_writer_source_reload_count ; do
  if ! grep -q "^${sym}$" "$TMPDIR/mac_exported.txt"; then
    echo "FATAL: Required entry point $sym not exported by $MAC_LIB" >&2
    exit 1
  fi
done
echo "   OK: All critical entry points verified present in macOS archive."

# 5. Control arm: Linux-slot comparison
echo "5. Running control arm: comparing against Linux-slot archive..."
$NM_BIN "$LINUX_LIB" > "$TMPDIR/linux_nm.txt"
grep -E ' (T|D) trace_writer_' "$TMPDIR/linux_nm.txt" | awk '{print $3}' | sort -u > "$TMPDIR/linux_exported.txt"
N_LINUX=$(wc -l < "$TMPDIR/linux_exported.txt" | tr -d ' ')
echo "   Found $N_LINUX exported trace_writer_* symbols in Linux archive."
if [[ "$N_LINUX" -lt 40 ]]; then
  echo "FATAL [CONTROL ARM]: Expected at least 40 symbols in Linux archive, found $N_LINUX" >&2
  exit 1
fi

MISSING_VS_LINUX=$(comm -23 "$TMPDIR/linux_exported.txt" "$TMPDIR/mac_exported.txt")
if [[ -n "$MISSING_VS_LINUX" ]]; then
  echo "FATAL [CONTROL ARM]: Symbols exported in Linux archive but missing in macOS archive:" >&2
  echo "$MISSING_VS_LINUX" >&2
  exit 1
fi
EXTRA_VS_LINUX=$(comm -13 "$TMPDIR/linux_exported.txt" "$TMPDIR/mac_exported.txt")
if [[ -n "$EXTRA_VS_LINUX" ]]; then
  echo "FATAL [CONTROL ARM]: Symbols exported in macOS archive but missing in Linux archive:" >&2
  echo "$EXTRA_VS_LINUX" >&2
  exit 1
fi
echo "   OK: macOS archive and Linux archive export identical symbol sets ($N_MAC symbols)."

# 6. Real fork link test
echo "6. Exercising real fork link against regenerated archive..."
cat << 'EOF' > "$TMPDIR/link_harness.c"
#include "modules/gdscript/ct_writer/include/codetracer_trace_writer.h"
#include <stdio.h>
#include <string.h>

int main(void) {
    codetracer_trace_writer_init();
    trace_writer_clear_last_error();
    trace_writer_t w = trace_writer_new("/tmp/test_harness_dir", FFI_TRACE_FORMAT_BINARY);
    if (!w) {
        fprintf(stderr, "trace_writer_new failed: %s\n", trace_writer_last_error());
        return 1;
    }
    trace_writer_set_workdir(w, "/tmp/test_harness_dir");
    trace_writer_begin_metadata(w, "");
    trace_writer_begin_events(w, "/tmp/test_harness_dir/events.bin");
    trace_writer_begin_paths(w, "");
    trace_writer_set_recording_id(w, "018d4567-e89b-7123-a456-426614174000");
    trace_writer_enable_line_count_table(w);
    trace_writer_register_path_with_line_count(w, "res://main.gd", 50);
    trace_writer_start(w, "res://main.gd", 1);
    uint64_t pid = trace_writer_current_path_id(w, "res://main.gd");
    if (pid == CT_TW_INVALID_PATH_ID) {
        fprintf(stderr, "trace_writer_current_path_id returned invalid sentinel: %s\n",
                trace_writer_last_error());
        return 1;
    }
    uint64_t v2 = trace_writer_register_path_version(w, "res://main.gd", 2);
    (void)v2;
    uint64_t reloads = trace_writer_source_reload_count(w);
    (void)reloads;
    trace_writer_finish_events(w);
    trace_writer_finish_metadata(w);
    trace_writer_finish_paths(w);
    trace_writer_close(w);
    trace_writer_free(w);
    printf("Fork link harness executed successfully (pid=%llu)!\n", (unsigned long long)pid);
    return 0;
}
EOF

mkdir -p "$TMPDIR/harness_out"
sed -i.bak "s|/tmp/test_harness_dir|$TMPDIR/harness_out|g" "$TMPDIR/link_harness.c"

# Use clang with SCsub link flags: -framework Security -framework CoreFoundation -lzstd
CC_CMD="clang"

$CC_CMD -I. "$TMPDIR/link_harness.c" "$MAC_LIB" \
  -framework Security -framework CoreFoundation -lzstd \
  -o "$TMPDIR/link_harness"

"$TMPDIR/link_harness"
echo "   OK: Real fork link succeeded and executed cleanly."

# 7. Run verify-gdh3-fork.sh
echo "7. Running scripts/verify-gdh3-fork.sh..."
scripts/verify-gdh3-fork.sh
echo "   OK: verify-gdh3-fork.sh passed."

# 8. Falsifier arm (if requested)
if [[ "$RUN_FALSIFIER" == "true" ]]; then
  echo "8. Running falsifier verification..."
  # Obtain stale archive from git HEAD (before this task)
  git show HEAD:modules/gdscript/ct_writer/libcodetracer_trace_writer.a > "$TMPDIR/stale_lib.a"

  echo "   [Falsifier 1] Verifying fork link fails with stale archive..."
  if $CC_CMD -I. "$TMPDIR/link_harness.c" "$TMPDIR/stale_lib.a" \
       -framework Security -framework CoreFoundation -lzstd \
       -o "$TMPDIR/stale_link_harness" >"$TMPDIR/stale_link.log" 2>&1; then
    echo "FATAL [FALSIFIER]: Fork link unexpectedly succeeded with stale archive!" >&2
    exit 1
  fi
  if ! grep -q "_trace_writer_current_path_id" "$TMPDIR/stale_link.log"; then
    echo "FATAL [FALSIFIER]: Stale link failure did not name _trace_writer_current_path_id:" >&2
    cat "$TMPDIR/stale_link.log" >&2
    exit 1
  fi
  echo "   OK: Fork link failed as expected, naming undefined symbols."

  echo "   [Falsifier 2] Verifying verify-gdh3-fork.sh exits non-zero with stale archive..."
  cp "$MAC_LIB" "$TMPDIR/backup_mac_lib.a"
  cp "$TMPDIR/stale_lib.a" "$MAC_LIB"
  if scripts/verify-gdh3-fork.sh >"$TMPDIR/stale_verify.log" 2>&1; then
    cp "$TMPDIR/backup_mac_lib.a" "$MAC_LIB"
    echo "FATAL [FALSIFIER]: verify-gdh3-fork.sh unexpectedly passed on stale archive!" >&2
    exit 1
  fi
  cp "$TMPDIR/backup_mac_lib.a" "$MAC_LIB"
  if ! grep -q "GDH3-FORK-FAIL: .* does not export trace_writer_current_path_id" "$TMPDIR/stale_verify.log"; then
    echo "FATAL [FALSIFIER]: verify-gdh3-fork.sh did not fail with expected message:" >&2
    cat "$TMPDIR/stale_verify.log" >&2
    exit 1
  fi
  echo "   OK: verify-gdh3-fork.sh exited non-zero with explicit GDH3-FORK-FAIL."
fi

echo "=== All checks passed for HX-D-0 ==="
