#!/usr/bin/env bash
# Vendor the CodeTracer trace writer for this host, from the revision
# flake.lock pins.
#
# The writer is built by the flake (package `ct-trace-writer`: the pinned
# codetracer-trace-format-nim source, its own pinned Nim toolchain and Nim
# dependencies). This copies the archive to
# modules/gdscript/ct_writer/<platform>-<arch>/, the header to
# modules/gdscript/ct_writer/include/, and writes PROVENANCE.json, which
# modules/gdscript/SCsub checks on every build that is not run inside
# `nix develop`: a vendored writer from any other revision, or whose bytes do
# not match the record, fails the build.
#
# Usage: scripts/vendor-trace-writer.sh
#   PLATFORM / ARCH default to this host, in scons' spelling.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

case "$(uname -s)-$(uname -m)" in
	Linux-x86_64)  : "${PLATFORM:=linuxbsd}" "${ARCH:=x86_64}" ;;
	Linux-aarch64) : "${PLATFORM:=linuxbsd}" "${ARCH:=arm64}" ;;
	Darwin-arm64)  : "${PLATFORM:=macos}" "${ARCH:=arm64}" ;;
	*) echo "vendor-trace-writer: set PLATFORM and ARCH for this host" >&2; exit 1 ;;
esac

rev="$(python3 -c 'import json; print(json.load(open("flake.lock"))["nodes"]["codetracer-trace-format-nim"]["locked"]["rev"])')"
out="$(nix build --no-link --print-out-paths .#ct-trace-writer)"
built_rev="$(cat "$out/rev")"
[[ "$built_rev" == "$rev" ]] || { echo "vendor-trace-writer: built $built_rev, lock pins $rev" >&2; exit 1; }

dest="modules/gdscript/ct_writer/$PLATFORM-$ARCH"
mkdir -p "$dest" modules/gdscript/ct_writer/include
install -m644 "$out/lib/libcodetracer_trace_writer.a" "$dest/libcodetracer_trace_writer.a"
install -m644 "$out/include/codetracer_trace_writer.h" modules/gdscript/ct_writer/include/codetracer_trace_writer.h

python3 - "$rev" "$dest" <<'PY'
import hashlib, json, sys
rev, dest = sys.argv[1], sys.argv[2]
sha = lambda p: hashlib.sha256(open(p, "rb").read()).hexdigest()
record = {
    "rev": rev,
    "source": "github:metacraft-labs/codetracer-trace-format-nim",
    "built_by": "nix build .#ct-trace-writer (flake.nix)",
    "archive_sha256": sha(dest + "/libcodetracer_trace_writer.a"),
    "header_sha256": sha("modules/gdscript/ct_writer/include/codetracer_trace_writer.h"),
}
with open(dest + "/PROVENANCE.json", "w") as f:
    json.dump(record, f, indent=2)
    f.write("\n")
PY
echo "vendored trace writer $rev for $PLATFORM-$ARCH into $dest"
