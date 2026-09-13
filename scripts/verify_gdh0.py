#!/usr/bin/env python3
"""GDH-M0 — the runnable falsifier.  Record a real GDScript hot reload in a
headless `template_debug` Godot engine and measure what the resulting `.ct`
does and does NOT say.

Two gates with deliberately different lifetimes (see
`codetracer-specs/Planned-Features/GDScript-Hot-Reload-Multi-Version-Sources.milestones.org`,
GDH-M0):

  GDH-G0a  PERMANENT.  The engine really reloaded.  Asserted from the ENGINE'S
           OWN STDOUT, independently of any trace.  This is the ONLY gate this
           file still carries.

GDH-G0b — the dated snapshot of what the container carried in 2026-09 — was
DELETED by GDH-M6 on 2026-09-11, which is what this milestone's own text said
would happen to it: "a test that asserts a defect must not be allowed to become
furniture".  The long note at the point of deletion maps each of its claims onto
the stronger gate that now makes it, in `scripts/verify_gdh6.py`.  Do not
resurrect it; the defect it described no longer exists, and a run of it today
would go red for the right reason and be indistinguishable from a run that went
red for the wrong one.

Every hand-derived value is in `scripts/EXPECTED-GDH0.md`.  Nothing about the
FIXTURE is written into this file: the insertion height, the probe body's line
numbers, the tick count and the per-version output tokens are all measured
from `test-programs/gdh0/probe_v{1,2}.gd` at run time, so a fixture edit cannot
silently disagree with an assertion (the milestone's "computed by the harness
from them, never written into the verifier").

NO MOCKS.  Real engine, real recorder, real container, real `ct-print`.  The
one thing this file implements itself is a minimal READER of the CTFS
container directory, because `ct-print` reports a source view's LENGTH but not
its BYTES, and the bytes have to be hash-compared with the fixture.  (The
reader survives GDH-G0b's deletion because `verify_gdh6.py` imports it.)  `ct-print` also does not report `meta.dat`
bit 14: `has_line_count_table` exists only in `codetracer_ct_print_lib.nim`,
which the shipped `codetracer_ct_print.nim` does not import — so REBUILDING IT
DOES NOT HELP, and a rebuild was tried (2026-09-10 review) rather than assumed:

    nim c -d:release --mm:arc -p:src \
      --passC:"-I<nix-store>/zstd-1.5.7-dev/include" \
      --passL:"-L<nix-store>/zstd-1.5.7/lib -lzstd" \
      -o:ct-print src/codetracer_ct_print.nim        # 5.4 s, succeeds

The rebuilt binary agreed with the checked-in one on every number GDH-G0b
asserted (paths 1, source_views 1, 299 dump lines) and added neither the bytes
nor the flag, which is why the reader below exists at all.  `CT_PRINT=`
selects either.  (`ct-print` DOES report both today — GDH-M6 measured
`has_line_count_table` and `path_versions` straight out of its header — but a
source view's bytes are still length-only there.)

The reader is a reimplementation of
`codetracer-trace-format-nim/src/codetracer_ctfs/container.nim`
(`readInternalFile`) and `.../variable_record_table.nim`, and it REFUSES rather
than guesses on anything it does not implement.

Subcommands
-----------
  verify_gdh0.py fixture  <v1.gd> <v2.gd>
        Print the fixture-derived facts as JSON and check the fixture's own
        preconditions.  Exits nonzero if the pair cannot support the gate.

  verify_gdh0.py pathcheck <engine>
        Deliverable 4: is `--path <real dir>` available in the binary under
        test?  Distinguishes the OVERRIDE_PATH_ENABLED refusal from the
        not-compiled-in refusal; exactly one must be present.

  verify_gdh0.py record <engine> <v1.gd> <v2.gd> <project-template> <outdir>
                        --arm {reload,control,wrongtarget,identical}
        Run one arm: record headless under CT_GDSCRIPT_TRACE, and for the
        `reload` arm overwrite `probe.gd` on disk part-way through and ask the
        engine to reload it over `core:reload_scripts` on the `--remote-debug`
        peer.  DIES rather than recording a run whose overwrite did not land.

  verify_gdh0.py verify <outdir> <v1.gd> <v2.gd> --arm <arm> [--expect-g0a-red]
        Assert GDH-G0a.  `--expect-g0a-red` inverts the verdict: the arm exists
        to prove the gate can fail, so a GREEN G0a there is the harness's own
        failure.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import select
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time

# ---------------------------------------------------------------------------
# Assertion machinery.  Per Verification-Harness-Traps.md 4c the check counts
# its own assertions and a run that does not reach the expected number is RED,
# so a silent skip cannot pass by simply making fewer claims.
# ---------------------------------------------------------------------------


class Checker:
    def __init__(self, label: str):
        self.label = label
        self.asserted = 0
        self.failures: list[str] = []
        self.notes: list[str] = []

    def note(self, msg: str) -> None:
        self.notes.append(msg)
        print("[gdh0]   %s" % msg, file=sys.stderr)

    def ck(self, ok: bool, msg: str) -> bool:
        self.asserted += 1
        if not ok:
            self.failures.append(msg)
            print("[gdh0]   CHECK-FAIL: %s" % msg, file=sys.stderr)
        return ok

    def eq(self, actual, expected, msg: str) -> bool:
        return self.ck(actual == expected,
                       "%s (expected %r, got %r)" % (msg, expected, actual))

    def expect_count(self, expected: int) -> None:
        """Trap 4c: the check asserts its OWN assertion count.

        A guard that returned early, a loop that skipped a member, a branch
        that was not taken — none of those FAIL any assertion, they simply make
        fewer of them, and the run stays green.  Promoting the count into the
        check turns a silent skip into a red run on the spot, with no second
        run and no human diffing two transcripts.  The numbers are written from
        a run, and a wrong number in the table fails until somebody fixes it —
        which is the intended cost.
        """
        if self.asserted != expected:
            msg = ("assertion count is %d, expected %d — this check did not "
                   "make all the claims it is supposed to make"
                   % (self.asserted, expected))
            self.failures.append(msg)
            print("[gdh0]   CHECK-FAIL: %s" % msg, file=sys.stderr)

    def report(self) -> bool:
        if self.failures:
            print("[gdh0] %s: RED — %d of %d assertions failed"
                  % (self.label, len(self.failures), self.asserted), file=sys.stderr)
        else:
            print("[gdh0] %s: GREEN — %d assertions"
                  % (self.label, self.asserted), file=sys.stderr)
        return not self.failures


def die(msg: str) -> "NoReturn":  # noqa: F821
    print("[gdh0] FATAL: %s" % msg, file=sys.stderr)
    sys.exit(2)


# ---------------------------------------------------------------------------
# A minimal CTFS container reader.
#
# Ported from the Nim implementation, not from the spec: the spec's §1
# describes a free-list root area between the header and the file entries, and
# the in-tree writer does not emit one — its entries start at
# HeaderSize(8) + ExtHeaderSize(8) = 16 with DefaultMaxRootEntries = 31
# (`codetracer_ctfs/types.nim:17-22`).  Reading the code rather than the prose
# is the difference between a reader that works and one that silently reads
# the wrong 24 bytes.
# ---------------------------------------------------------------------------

CTFS_MAGIC = bytes([0xC0, 0xDE, 0x72, 0xAC, 0xE2])
_B40_ALPHABET = "\0" + "0123456789" + "abcdefghijklmnopqrstuvwxyz" + "./-"
_HEADER_SIZE = 8
_EXT_HEADER_SIZE = 8
_FILE_ENTRY_SIZE = 24
_MAX_CHAIN_LEVELS = 5


def _b40_encode(name: str) -> int:
    if len(name) > 12:
        raise ValueError("base40 caps an internal name at 12 chars: %r" % name)
    value = 0
    mult = 1
    for ch in name.ljust(12, "\0"):
        value += _B40_ALPHABET.index(ch) * mult
        mult *= 40
    return value


def _b40_decode(value: int) -> str:
    out = []
    while value > 0:
        rem = value % 40
        value //= 40
        if rem == 0:
            break
        out.append(_B40_ALPHABET[rem])
    return "".join(out)


class CtfsContainer:
    """Read-only view over a `.ct` file's internal-file directory."""

    def __init__(self, path: str):
        with open(path, "rb") as handle:
            self.data = handle.read()
        if self.data[:5] != CTFS_MAGIC:
            raise ValueError("%s is not a CTFS container (bad magic)" % path)
        self.version = self.data[5]
        self.block_size = struct.unpack_from("<I", self.data, 8)[0] or 4096
        self.max_entries = struct.unpack_from("<I", self.data, 12)[0] or 31
        self.path = path

    def entries(self) -> list[tuple[str, int, int]]:
        out = []
        for i in range(self.max_entries):
            off = _HEADER_SIZE + _EXT_HEADER_SIZE + i * _FILE_ENTRY_SIZE
            if off + _FILE_ENTRY_SIZE > len(self.data):
                break
            size, map_block, name = struct.unpack_from("<QQQ", self.data, off)
            if size == 0 and map_block == 0 and name == 0:
                continue
            out.append((_b40_decode(name), size, map_block))
        return out

    def names(self) -> list[str]:
        return [n for n, _s, _m in self.entries()]

    def read_internal(self, name: str) -> bytes:
        encoded = _b40_encode(name)
        found = None
        for i in range(self.max_entries):
            off = _HEADER_SIZE + _EXT_HEADER_SIZE + i * _FILE_ENTRY_SIZE
            if off + _FILE_ENTRY_SIZE > len(self.data):
                break
            size, map_block, nm = struct.unpack_from("<QQQ", self.data, off)
            if nm == encoded:
                found = (size, map_block)
                break
        if found is None:
            raise KeyError("internal file not found: %s" % name)
        size, map_block = found
        if size == 0:
            return b""
        # floor, never round up — §5d: the incomplete final block of an
        # interrupted append must be unaddressable.
        whole_blocks = len(self.data) // self.block_size
        if map_block == 0 or map_block >= whole_blocks:
            raise ValueError("%s: mapping root block %d out of bounds"
                             % (name, map_block))
        usable = self.block_size // 8 - 1
        out = bytearray(size)
        remaining, dest, block_idx = size, 0, 0
        while remaining > 0:
            idx, current, level = block_idx, map_block, 1
            while True:
                cap = usable ** level
                if idx < cap:
                    break
                idx -= cap
                level += 1
                if level > _MAX_CHAIN_LEVELS:
                    raise ValueError("%s: block index too large" % name)
                chain_off = current * self.block_size + usable * 8
                chain = struct.unpack_from("<Q", self.data, chain_off)[0]
                if chain == 0 or chain >= whole_blocks:
                    raise ValueError("%s: chain pointer out of bounds" % name)
                current = chain
            nav, nav_level, nav_idx = current, level, idx
            while nav_level > 1:
                sub = usable ** (nav_level - 1)
                child = struct.unpack_from(
                    "<Q", self.data, nav * self.block_size + (nav_idx // sub) * 8)[0]
                if child == 0 or child >= whole_blocks:
                    raise ValueError("%s: child block out of bounds" % name)
                nav, nav_idx, nav_level = child, nav_idx % sub, nav_level - 1
            data_block = struct.unpack_from(
                "<Q", self.data, nav * self.block_size + nav_idx * 8)[0]
            if data_block == 0 or data_block >= whole_blocks:
                raise ValueError("%s: data block %d out of bounds"
                                 % (name, data_block))
            block_off = data_block * self.block_size
            take = min(remaining, self.block_size)
            out[dest:dest + take] = self.data[block_off:block_off + take]
            dest += take
            remaining -= take
            block_idx += 1
        return bytes(out)

    def meta_flags(self) -> int:
        meta = self.read_internal("meta.dat")
        if meta[:4] != b"CTMD":
            raise ValueError("meta.dat does not start with CTMD")
        return struct.unpack_from("<H", meta, 6)[0]

    @staticmethod
    def _offset_table(base: str, dat: bytes, off: bytes) -> list[int]:
        """Decode and VALIDATE a variable-record offset table.

        `variable_record_table.nim:48-53` writes an initial 0 before any
        record, then one cumulative end-offset per `append` — so a well-formed
        table starts at 0, never decreases, and its last entry is exactly the
        data stream's length.  Checking all three matters because the
        alternative failure mode is the one this campaign has produced four
        times: a reader that mis-models the layout returns FEWER records, or
        none, and every "there is only one" assertion written over it passes
        for free.  Each of these raises rather than answering.
        """
        if len(off) % 8 != 0 or len(off) < 8:
            raise ValueError("%s.off is not a table of u64 offsets "
                             "(%d bytes)" % (base, len(off)))
        offsets = [struct.unpack_from("<Q", off, i * 8)[0]
                   for i in range(len(off) // 8)]
        if offsets[0] != 0:
            raise ValueError("%s.off does not begin at 0 (begins at %d) — the "
                             "table layout this reader implements is not the "
                             "one on disk" % (base, offsets[0]))
        for i in range(len(offsets) - 1):
            if offsets[i + 1] < offsets[i]:
                raise ValueError("%s.off is not non-decreasing at entry %d "
                                 "(%d then %d)"
                                 % (base, i, offsets[i], offsets[i + 1]))
        if offsets[-1] != len(dat):
            raise ValueError(
                "%s.off's last cumulative offset is %d but %s.dat holds %d "
                "bytes — %d byte(s) of the data stream are addressed by no "
                "record, so this reader would silently under-report the "
                "record count"
                % (base, offsets[-1], base, len(dat), len(dat) - offsets[-1]))
        return offsets

    def _variable_records(self, base: str) -> list[bytes]:
        dat = self.read_internal(base + ".dat")
        off = self.read_internal(base + ".off")
        offsets = self._offset_table(base, dat, off)
        return [dat[offsets[i]:offsets[i + 1]] for i in range(len(offsets) - 1)]

    def paths(self) -> list[str]:
        """Decode `paths.dat` / `paths.off` directly.

        In the bare interning layout — the one a container without `meta.dat`
        bit 4 (column-aware) and without bit 14 (line-count table) writes — a
        record IS the payload bytes, with the offset table supplying the length
        (`interning_table.nim:62-88`).  Decoding it here rather than trusting
        `ct-print` gives a SECOND, independent reader for the same claim, so a
        path count is a measurement two instruments agree on rather than one
        instrument's opinion.  GDH-M6's gates rely on exactly that.
        """
        return [rec.decode("utf-8") for rec in self._variable_records("paths")]

    def source_views(self) -> list[dict]:
        """Decode `srcviews.dat` / `srcviews.off`.

        THE STREAM NAMES ARE `srcviews.*`, NOT `source_views.*`.  Base40 caps a
        CTFS internal name at 12 characters, so the spec's `source_views.dat`
        truncates to `source_views` and collides with the `.off` entry; the
        writer works around it at `multi_stream_writer.nim:1699-1708`.  A scan
        for the spec's name finds nothing and passes every "must not contain"
        check written over it — Verification-Harness-Traps.md trap 4, exactly.
        """
        dat = self.read_internal("srcviews.dat")
        off = self.read_internal("srcviews.off")
        offsets = self._offset_table("srcviews", dat, off)
        views = []
        for i in range(len(offsets) - 1):
            rec = dat[offsets[i]:offsets[i + 1]]
            pos = 0
            path_id, pos = _varint(rec, pos)
            view_kind = rec[pos]
            pos += 1
            name_len, pos = _varint(rec, pos)
            view_name = rec[pos:pos + name_len].decode("utf-8")
            pos += name_len
            content_len, pos = _varint(rec, pos)
            content = rec[pos:pos + content_len]
            pos += content_len
            map_len, pos = _varint(rec, pos)
            sourcemap = rec[pos:pos + map_len]
            pos += map_len
            if pos != len(rec):
                raise ValueError(
                    "srcviews record %d has %d trailing bytes — the record "
                    "layout this reader implements is not the one on disk"
                    % (i, len(rec) - pos))
            views.append(dict(path_id=path_id, view_kind=view_kind,
                              view_name=view_name, content=content,
                              sourcemap=sourcemap))
        return views


def _varint(buf: bytes, pos: int) -> tuple[int, int]:
    shift, value = 0, 0
    while True:
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7


# ---------------------------------------------------------------------------
# Godot remote-debugger wire protocol (the host half).
#
# The engine CONNECTS OUT to `--remote-debug tcp://host:port` and speaks
# `u32 LE length` + `encode_variant(Array)` (`remote_debugger_peer.cpp:101-170`).
# A host->engine command is a THREE-element array
# `[String cmd, int thread_id, Array data]` — `remote_debugger.cpp:353-374`
# `ERR_CONTINUE(cmd.size() != 3)`.  `thread_id` must name a thread the engine
# has registered; the main thread is registered unconditionally at `:801` and
# `Thread::MAIN_ID` is 1 (`core/os/thread.h:72`).  A two-element array — the
# shape `poll_events` itself parses at `:663` — is REFUSED here with
# `Condition "cmd.size() != 3" is true`, measured on 2026-09-10 before the
# third element was added.
# ---------------------------------------------------------------------------

_VT_NIL, _VT_BOOL, _VT_INT, _VT_FLOAT, _VT_STRING, _VT_ARRAY = 0, 1, 2, 3, 4, 28
_VT_PACKED_BYTE, _VT_PACKED_I32, _VT_PACKED_I64 = 29, 30, 31
_VT_PACKED_F32, _VT_PACKED_F64, _VT_PACKED_STRING = 32, 33, 34
GODOT_MAIN_THREAD_ID = 1


def _encode_variant(value, out: bytearray) -> None:
    if isinstance(value, bool):
        out += struct.pack("<II", _VT_BOOL, 1 if value else 0)
    elif isinstance(value, int):
        out += struct.pack("<Ii", _VT_INT, value)
    elif isinstance(value, str):
        raw = value.encode("utf-8")
        out += struct.pack("<II", _VT_STRING, len(raw))
        out += raw
        while len(out) % 4:
            out += b"\0"
    elif isinstance(value, list):
        out += struct.pack("<II", _VT_ARRAY, len(value))
        for item in value:
            _encode_variant(item, out)
    else:
        raise TypeError("cannot encode %r as a Variant" % type(value))


def encode_message(array: list) -> bytes:
    body = bytearray()
    _encode_variant(array, body)
    return struct.pack("<I", len(body)) + bytes(body)


def _decode_string(buf: bytes, pos: int) -> tuple[str, int]:
    n = struct.unpack_from("<I", buf, pos)[0]
    pos += 4
    text = buf[pos:pos + n].decode("utf-8", "replace")
    pos += n
    while pos % 4:
        pos += 1
    return text, pos


def _decode_variant(buf: bytes, pos: int):
    header = struct.unpack_from("<I", buf, pos)[0]
    pos += 4
    kind = header & 0xFFFF
    wide = bool(header & (1 << 16))
    if kind == _VT_NIL:
        return None, pos
    if kind == _VT_BOOL:
        return bool(struct.unpack_from("<I", buf, pos)[0]), pos + 4
    if kind == _VT_INT:
        fmt, size = ("<q", 8) if wide else ("<i", 4)
        return struct.unpack_from(fmt, buf, pos)[0], pos + size
    if kind == _VT_FLOAT:
        fmt, size = ("<d", 8) if wide else ("<f", 4)
        return struct.unpack_from(fmt, buf, pos)[0], pos + size
    if kind == _VT_STRING:
        return _decode_string(buf, pos)
    if kind == _VT_PACKED_STRING:
        n = struct.unpack_from("<I", buf, pos)[0]
        pos += 4
        items = []
        for _ in range(n):
            text, pos = _decode_string(buf, pos)
            items.append(text)
        return items, pos
    if kind in (_VT_PACKED_BYTE, _VT_PACKED_I32, _VT_PACKED_I64,
                _VT_PACKED_F32, _VT_PACKED_F64):
        n = struct.unpack_from("<I", buf, pos)[0]
        pos += 4
        fmt = {_VT_PACKED_BYTE: "<B", _VT_PACKED_I32: "<i", _VT_PACKED_I64: "<q",
               _VT_PACKED_F32: "<f", _VT_PACKED_F64: "<d"}[kind]
        width = struct.calcsize(fmt)
        items = [struct.unpack_from(fmt, buf, pos + i * width)[0] for i in range(n)]
        pos += n * width
        while pos % 4:  # a packed byte array is padded to a 4-byte boundary
            pos += 1
        return items, pos
    if kind == _VT_ARRAY:
        n = struct.unpack_from("<I", buf, pos)[0] & 0x7FFFFFFF
        pos += 4
        items = []
        for _ in range(n):
            item, pos = _decode_variant(buf, pos)
            items.append(item)
        return items, pos
    # Anything else is context for the transcript, never a verdict.  Trap 3:
    # log both directions of a boundary you do not own, and reduce an
    # unparseable value to a KIND rather than swallowing it.
    raise ValueError("unhandled Variant type %d" % kind)


# ---------------------------------------------------------------------------
# Fixture facts, measured from the two .gd files.
# ---------------------------------------------------------------------------

PROBE_DEF_RE = re.compile(r"^func probe\(")
TICKS_RE = re.compile(r"^const TICKS := (\d+)$")
TOKEN_RE = re.compile(r'print\("(GDH0_V[12]_[A-Z])')
TICK_LINE_RE = re.compile(r"^GDH0_TICK=(\d+) $")
TOKEN_LINE_RE = re.compile(r"^(GDH0_V[12]_[A-Z]) tick=(\d+)$")


def fixture_facts(v1_path: str, v2_path: str) -> dict:
    """Everything an assertion needs about the fixture, DERIVED from it.

    Deliberately anchored to syntax rather than vocabulary (trap 4d): the probe
    function is found by `^func probe\\(`, never by the words "func probe",
    which also occur in both files' prose.
    """
    v1 = open(v1_path, "rb").read()
    v2 = open(v2_path, "rb").read()
    l1 = v1.decode("utf-8").split("\n")
    l2 = v2.decode("utf-8").split("\n")
    if l1 and l1[-1] == "":
        l1 = l1[:-1]
    if l2 and l2[-1] == "":
        l2 = l2[:-1]

    def probe_line(lines, which):
        hits = [i + 1 for i, ln in enumerate(lines) if PROBE_DEF_RE.match(ln)]
        if len(hits) != 1:
            die("%s: expected exactly one `^func probe(` line, found %r"
                % (which, hits))
        return hits[0]

    def ticks(lines, which):
        hits = [int(m.group(1)) for m in
                (TICKS_RE.match(ln) for ln in lines) if m]
        if len(hits) != 1:
            die("%s: expected exactly one `const TICKS := N` line, found %r"
                % (which, hits))
        return hits[0]

    p1, p2 = probe_line(l1, v1_path), probe_line(l2, v2_path)

    # The probe body is the maximal run of indented lines under the definition.
    def body(lines, at):
        out = []
        for i in range(at, len(lines)):
            if lines[i].startswith("\t"):
                out.append(i + 1)
            else:
                break
        return out

    body1, body2 = body(l1, p1), body(l2, p2)

    def tokens(lines, nums):
        found = []
        for n in nums:
            m = TOKEN_RE.search(lines[n - 1])
            if not m:
                die("probe body line %d has no GDH0_V<n>_<L> token: %r"
                    % (n, lines[n - 1]))
            found.append(m.group(1))
        return found

    tok1, tok2 = tokens(l1, body1), tokens(l2, body2)

    # The insertion, measured by diffing.  Exactly one `insert` opcode, and it
    # must lie entirely ABOVE v2's probe definition.
    opcodes = difflib.SequenceMatcher(None, l1, l2, autojunk=False).get_opcodes()
    inserts = [op for op in opcodes if op[0] == "insert"]
    identical = v1 == v2

    facts = dict(
        v1_path=os.path.abspath(v1_path),
        v2_path=os.path.abspath(v2_path),
        v1_sha256=hashlib.sha256(v1).hexdigest(),
        v2_sha256=hashlib.sha256(v2).hexdigest(),
        v1_bytes=len(v1), v2_bytes=len(v2),
        v1_lines=len(l1), v2_lines=len(l2),
        v1_probe_def_line=p1, v2_probe_def_line=p2,
        v1_probe_body_lines=body1, v2_probe_body_lines=body2,
        v1_tokens=tok1, v2_tokens=tok2,
        ticks=ticks(l1, v1_path),
        v2_ticks=ticks(l2, v2_path),
        insert_opcodes=[list(op) for op in inserts],
        insertion_height=(inserts[0][4] - inserts[0][3]) if len(inserts) == 1 else None,
        insertion_above_probe=(len(inserts) == 1 and inserts[0][4] <= p2 - 1),
        shift=p2 - p1,
        identical=identical,
    )
    return facts


def check_fixture(facts: dict, ck: Checker, *, allow_identical: bool = False) -> None:
    ck.ck(facts["v1_bytes"] > 0 and facts["v2_bytes"] > 0,
          "both fixture files are non-empty")
    if not allow_identical:
        ck.ck(not facts["identical"],
              "v2 is not byte-identical to v1 — an unchanged observation "
              "cannot distinguish a reload from no reload")
    ck.eq(len(facts["insert_opcodes"]), 1,
          "the v1->v2 diff carries exactly ONE insert opcode")
    ck.ck(facts["insertion_above_probe"],
          "the inserted block lies entirely ABOVE v2's `func probe(` line "
          "(insert ends at v2 line %r, probe is at %d)"
          % (facts["insert_opcodes"][0][4] if facts["insert_opcodes"] else None,
             facts["v2_probe_def_line"]))
    ck.ck(facts["insertion_height"] is not None and facts["insertion_height"] >= 5,
          "at least 5 lines are inserted above the probe function "
          "(measured %r)" % (facts["insertion_height"],))
    ck.eq(facts["shift"], facts["insertion_height"],
          "the probe function moved by exactly the insertion height")
    ck.eq(len(facts["v1_probe_body_lines"]), len(facts["v2_probe_body_lines"]),
          "both probe bodies have the same number of lines")
    ck.ck(len(set(facts["v1_tokens"])) == len(facts["v1_tokens"]),
          "v1's probe body carries a DISTINCT token per line: %r" % (facts["v1_tokens"],))
    ck.ck(len(set(facts["v2_tokens"])) == len(facts["v2_tokens"]),
          "v2's probe body carries a DISTINCT token per line: %r" % (facts["v2_tokens"],))
    if not allow_identical:
        ck.ck(not (set(facts["v1_tokens"]) & set(facts["v2_tokens"])),
              "the two versions' token sets are DISJOINT")
    ck.eq(facts["ticks"], facts["v2_ticks"],
          "both versions run the same number of ticks")
    ck.ck(facts["v2_probe_body_lines"][0] > facts["v1_lines"],
          "every line v2 executes in the probe lies PAST THE END of v1's file "
          "(v2 body starts at %d, v1 has %d lines) — so a post-reload step's "
          "line number does not exist in v1 at all"
          % (facts["v2_probe_body_lines"][0], facts["v1_lines"]))


# ---------------------------------------------------------------------------
# `--path <real dir>` availability (deliverable 4).
# ---------------------------------------------------------------------------

PATH_OK_MSG = "Invalid project path specified:"
PATH_ABSENT_MSG = "compiled without support for path overrides"


def cmd_pathcheck(args) -> int:
    ck = Checker("--path availability")
    engine = args.engine
    if not os.access(engine, os.X_OK):
        die("engine not executable: %s" % engine)
    bogus = "/gdh0-definitely-not-a-project-%d" % os.getpid()
    proc = subprocess.run([engine, "--headless", "--path", bogus, "--quit"],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          timeout=120)
    out = proc.stdout.decode("utf-8", "replace")
    enabled = PATH_OK_MSG in out
    absent = PATH_ABSENT_MSG in out
    ck.note("engine: %s" % engine)
    ck.note("rc=%d, output:\n%s" % (proc.returncode, out.strip()))
    # The pairing IS the control (trap 4a): a scan that can see neither message
    # would satisfy a lone "must not contain" assertion for free.
    ck.eq(enabled != absent, True,
          "exactly one of the two --path refusal messages is present "
          "(OVERRIDE_PATH_ENABLED=%r, not-compiled-in=%r)" % (enabled, absent))
    ck.ck(enabled,
          "`--path <real dir>` IS available in this binary — without it there "
          "is no editable res:// to overwrite and GDH-M0 cannot run")
    print(json.dumps(dict(engine=engine, override_path_enabled=enabled,
                          not_compiled_in=absent, rc=proc.returncode)))
    return 0 if ck.report() else 1


# ---------------------------------------------------------------------------
# Recording one arm.
# ---------------------------------------------------------------------------

ARMS = {
    # arm            overwrite?  replacement  reload target
    "reload":       (True,  "v2", "res://probe.gd"),
    "control":      (False, None, None),
    "wrongtarget":  (True,  "v2", "res://gdh0_never_loaded.gd"),
    "identical":    (True,  "v1", "res://probe.gd"),
}


def sha256_file(path: str) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def cmd_record(args) -> int:
    arm = args.arm
    overwrite, replacement, reload_target = ARMS[arm]
    facts = fixture_facts(args.v1, args.v2)
    ticks = facts["ticks"]
    marker_tick = max(2, ticks // 3)
    marker = "GDH0_TICK=%d " % marker_tick

    out_dir = os.path.abspath(args.outdir)
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    proj = os.path.join(out_dir, "project")
    trace_dir = os.path.join(out_dir, "trace")
    os.makedirs(trace_dir)
    shutil.copytree(args.project_template, proj)
    # The fixture pair lives in the template directory; the recorded project
    # must contain exactly ONE .gd — the one being reloaded — so that nothing
    # can attribute a v2 token to a second file.
    for stale in os.listdir(proj):
        if stale.endswith(".gd"):
            os.remove(os.path.join(proj, stale))
    shutil.copyfile(args.v1, os.path.join(proj, "probe.gd"))

    # A PCK-backed run cannot be overwritten underneath.  Refuse to record one.
    for name in os.listdir(proj):
        if name.endswith((".pck", ".zip")):
            die("the project directory carries %s — a PCK-backed run cannot "
                "be overwritten underneath and must not be recorded" % name)
    engine_dir = os.path.dirname(os.path.abspath(args.engine))
    for name in os.listdir(engine_dir):
        if name.endswith(".pck"):
            die("%s sits next to the engine — Godot would mount it and "
                "res:// would not be the --path directory" % name)

    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    env = dict(os.environ)
    env["CT_GDSCRIPT_TRACE"] = trace_dir
    argv = [args.engine, "--headless", "--path", proj,
            "--script", "res://probe.gd",
            "--remote-debug", "tcp://127.0.0.1:%d" % port]

    record = dict(arm=arm, argv=argv, port=port, engine=args.engine,
                  project=proj, trace_dir=trace_dir,
                  marker=marker, marker_tick=marker_tick,
                  reload_target=reload_target,
                  overwrite_requested=overwrite,
                  overwrite_applied=False, overwrite_sha256=None,
                  expected_overwrite_sha256=(
                      facts["v2_sha256"] if replacement == "v2"
                      else facts["v1_sha256"] if replacement == "v1" else None),
                  reload_sent=False, reload_sent_at=None,
                  peer_connected=False, peer_messages=0,
                  rc=None, wall_seconds=None, timed_out=False)

    print("[gdh0] arm=%s  %s" % (arm, " ".join(argv)))
    t0 = time.time()
    proc = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, bufsize=0)
    lines: list[str] = []
    log_path = os.path.join(out_dir, "stdout.log")
    log = open(log_path, "w")

    def pump():
        for raw in proc.stdout:
            text = raw.decode("utf-8", "replace").rstrip("\n")
            lines.append(text)
            log.write(text + "\n")
            log.flush()

    pump_thread = threading.Thread(target=pump, daemon=True)
    pump_thread.start()

    peer_log = open(os.path.join(out_dir, "peer.log"), "w")
    srv.settimeout(60)
    try:
        conn, addr = srv.accept()
        record["peer_connected"] = True
        peer_log.write("connected from %r at %.3f\n" % (addr, time.time() - t0))
    except socket.timeout:
        proc.kill()
        die("the engine never connected to the --remote-debug peer on port %d "
            "within 60 s; a run in which the reload request cannot be "
            "delivered must not be recorded" % port)
    conn.setblocking(False)

    budget = 60.0 + 0.25 * ticks
    deadline = time.time() + budget
    inbuf = b""
    done = False
    while time.time() < deadline:
        if proc.poll() is not None:
            done = True
            break
        ready, _, _ = select.select([conn], [], [], 0.02)
        if ready:
            try:
                chunk = conn.recv(1 << 16)
            except BlockingIOError:
                chunk = b""
            inbuf += chunk
            while len(inbuf) >= 4:
                size = struct.unpack_from("<I", inbuf, 0)[0]
                if len(inbuf) < 4 + size:
                    break
                body, inbuf = inbuf[4:4 + size], inbuf[4 + size:]
                record["peer_messages"] += 1
                try:
                    msg, _ = _decode_variant(body, 0)
                    peer_log.write("<- %r\n" % (msg,))
                except Exception as exc:  # noqa: BLE001 — transcript, not verdict
                    peer_log.write("<- UNDECODED (%s): %s\n"
                                   % (exc, body[:64].hex()))
        if reload_target and not record["reload_sent"]:
            if any(marker in line for line in lines):
                target = os.path.join(proj, "probe.gd")
                if overwrite:
                    src = args.v2 if replacement == "v2" else args.v1
                    tmp = target + ".incoming"
                    shutil.copyfile(src, tmp)
                    os.replace(tmp, target)
                    got = sha256_file(target)
                    record["overwrite_applied"] = True
                    record["overwrite_sha256"] = got
                    # DIE rather than record a run whose overwrite silently
                    # did nothing.  This is the whole point of deliverable 4.
                    if got != record["expected_overwrite_sha256"]:
                        proc.kill()
                        die("the overwrite did not land: %s hashes %s, "
                            "expected %s" % (target, got,
                                             record["expected_overwrite_sha256"]))
                conn.sendall(encode_message(
                    ["core:reload_scripts", GODOT_MAIN_THREAD_ID, [reload_target]]))
                record["reload_sent"] = True
                record["reload_sent_at"] = time.time() - t0
                record["reload_sent_after_line"] = len(lines)
                peer_log.write("-> core:reload_scripts %r at %.3f\n"
                               % ([reload_target], record["reload_sent_at"]))

    if not done and proc.poll() is None:
        # Trap 1: a hang is rc 124 AND NOTHING ELSE.  Record it as such.
        proc.kill()
        record["timed_out"] = True
        proc.wait(timeout=30)
        record["rc"] = 124
    else:
        record["rc"] = proc.wait(timeout=60)
    record["wall_seconds"] = time.time() - t0
    pump_thread.join(timeout=10)
    log.close()
    peer_log.close()
    conn.close()
    srv.close()

    record["final_probe_sha256"] = sha256_file(os.path.join(proj, "probe.gd"))
    ct = os.path.join(trace_dir, "gdscript_trace.ct")
    record["ct"] = ct if os.path.exists(ct) else None
    record["ct_bytes"] = os.path.getsize(ct) if os.path.exists(ct) else 0
    record["stdout_lines"] = len(lines)
    with open(os.path.join(out_dir, "driver.json"), "w") as handle:
        json.dump(record, handle, indent=2, sort_keys=True)
    print("[gdh0] arm=%s rc=%d wall=%.3fs stdout_lines=%d ct=%d bytes"
          % (arm, record["rc"], record["wall_seconds"], len(lines),
             record["ct_bytes"]))
    return 0


# ---------------------------------------------------------------------------
# GDH-G0a — the engine really reloaded, from stdout alone.
# ---------------------------------------------------------------------------


def parse_stdout(path: str) -> dict:
    ticks: list[int] = []
    observations: list[tuple[int, str]] = []
    begin = end = 0
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.rstrip("\n")
        if line == "GDH0_BEGIN":
            begin += 1
        elif line == "GDH0_END":
            end += 1
        m = TICK_LINE_RE.match(line)
        if m:
            ticks.append(int(m.group(1)))
            continue
        m = TOKEN_LINE_RE.match(line)
        if m:
            observations.append((int(m.group(2)), m.group(1)))
    return dict(ticks=ticks, observations=observations, begin=begin, end=end)


def check_g0a(ck: Checker, facts: dict, record: dict, obs: dict) -> None:
    v1_tokens, v2_tokens = set(facts["v1_tokens"]), set(facts["v2_tokens"])
    ticks = obs["ticks"]
    per_tick: dict[int, list[str]] = {}
    for tick, token in obs["observations"]:
        per_tick.setdefault(tick, []).append(token)

    # --- anti-vacuity: the INPUT is complete before anything is claimed ----
    ck.eq(record["rc"], 0,
          "the engine exited 0 (rc 124 would be a HANG and nothing else; "
          "timed_out=%r)" % record["timed_out"])
    ck.eq(record["timed_out"], False, "the run was not killed on the wall clock")
    ck.eq(obs["begin"], 1, "stdout carries exactly one GDH0_BEGIN")
    ck.eq(obs["end"], 1, "stdout carries exactly one GDH0_END — the program "
                         "ran to completion rather than dying mid-loop")
    ck.eq(ticks, list(range(1, facts["ticks"] + 1)),
          "stdout carries every tick 1..%d exactly once, in order" % facts["ticks"])
    ck.eq(len(obs["observations"]), facts["ticks"] * len(facts["v1_tokens"]),
          "stdout carries %d probe observations (%d ticks x %d body lines) — "
          "the COUNT, not merely `> 0`, because a loop that skipped two thirds "
          "of its ticks satisfies every existential control over it"
          % (facts["ticks"] * len(facts["v1_tokens"]), facts["ticks"],
             len(facts["v1_tokens"])))

    # --- every tick is wholly one version ---------------------------------
    versions: list[str] = []
    mixed = []
    for tick in range(1, facts["ticks"] + 1):
        got = per_tick.get(tick, [])
        if got == facts["v1_tokens"]:
            versions.append("v1")
        elif got == facts["v2_tokens"]:
            versions.append("v2")
        else:
            versions.append("?")
            mixed.append((tick, got))
    ck.eq(mixed, [], "every tick's three observations are the ordered token "
                     "sequence of ONE version")

    n_v1 = versions.count("v1")
    n_v2 = versions.count("v2")
    transitions = sum(1 for a, b in zip(versions, versions[1:]) if a != b)
    ck.note("tick versions: %d x v1 then %d x v2, %d transition(s)"
            % (n_v1, n_v2, transitions))

    if not record["reload_sent"] and not record["overwrite_requested"]:
        # The control arm.  It must REPORT one version rather than pass in
        # silence, and it must be v1's.
        ck.ck(n_v1 == facts["ticks"] and n_v2 == 0,
              "CONTROL ARM: with no overwrite and no reload request the run "
              "shows ONE VERSION — v1 — for all %d ticks (v1=%d, v2=%d)"
              % (facts["ticks"], n_v1, n_v2))
        ck.eq(transitions, 0, "CONTROL ARM: no version transition")
        return

    ck.ck(n_v1 > 0, "the pre-reload half is NON-EMPTY (%d ticks showed v1) — "
                    "a run showing v2 from the first tick proves a stale build "
                    "on disk, not a reload" % n_v1)
    ck.ck(n_v2 > 0, "the post-reload half is NON-EMPTY (%d ticks showed v2) — "
                    "THE RELOAD DID NOT HAPPEN if this is zero" % n_v2)
    ck.eq(transitions, 1, "there is EXACTLY ONE version transition")
    ck.eq(versions, ["v1"] * n_v1 + ["v2"] * n_v2,
          "the v1 ticks are a prefix and the v2 ticks a suffix")

    # --- the reload was caused, not coincidental ---------------------------
    ck.ck(record["reload_sent"], "a core:reload_scripts request was delivered")
    ck.ck(record["overwrite_applied"] == record["overwrite_requested"],
          "the overwrite was applied iff this arm asks for one")
    if record["overwrite_requested"]:
        ck.eq(record["overwrite_sha256"], record["expected_overwrite_sha256"],
              "the file on disk really changed under the running engine "
              "(sha256 read back from disk after the rename)")
        ck.eq(record["final_probe_sha256"], record["expected_overwrite_sha256"],
              "and it still held that content when the run ended")
    ck.ck(n_v1 >= record["marker_tick"],
          "no v2 token appeared before the request was sent (the driver "
          "sends it on `%s`, tick %d; v1 ran for %d ticks)"
          % (record["marker"].strip(), record["marker_tick"], n_v1))


# ---------------------------------------------------------------------------
# GDH-G0b WAS HERE, AND IT IS GONE ON PURPOSE (GDH-M6, 2026-09-11).
#
# GDH-M0 shipped TWO gates with deliberately different lifetimes.  GDH-G0a —
# "the engine really reloaded", asserted from the engine's own stdout — is
# PERMANENT and is still above.  GDH-G0b was a DATED SNAPSHOT OF A DEFECT: it
# asserted that the container carried exactly ONE `paths.dat` entry for a file
# that ran in two versions, ONE raw source view whose bytes were v1's, v2's
# text nowhere in the container, and post-reload steps decoding to line numbers
# that do not exist in v1 at all — 129 of 196 of them, silently.
#
# GDH-M0's own text is why it is being deleted rather than kept and inverted:
#
#     "A test that asserts a defect must not be allowed to become furniture;
#      GDH-M6's deliverables include deleting this arm and replacing it with
#      GDH-G1/G2/G3."
#
# Deleting a test is otherwise forbidden in this tree.  This is the one case
# where it is MANDATED, and only because a strictly stronger gate replaces it.
# Here is the replacement, claim for claim, so a reviewer can check that
# nothing G0b measured has simply stopped being measured:
#
#   G0b claim                        | now asserted by
#   ---------------------------------+--------------------------------------
#   exactly one paths.dat entry for  | gdh6_both_versions_retrievable_end_to_
#   a file that ran in two versions  | end — THREE entries, all carrying the
#                                    | IDENTICAL res:// string, differing only
#                                    | by index (GDH-G1 + GDH-G2)
#   exactly one raw source view,     | the same gate — one raw view PER
#   whose bytes are v1's             | VERSION, each hash-compared at run time
#                                    | against its own fixture file
#   v2's text occurs nowhere in the  | inverted by the same gate: every
#   container                        | version's bytes are present and
#                                    | distinct.  The byte-scan's positive
#                                    | twin survives as the view-kind check
#   steps decode to lines that do    | gdh6_no_step_is_attributed_to_the_wrong
#   not exist in the file the        | _version — an ORDERED BIJECTION between
#   container carries, silently      | the engine's printed tokens and the
#                                    | decoded steps, plus a TEXT half that
#                                    | requires each step's own source view to
#                                    | carry the right bytes AT the decoded
#                                    | line (GDH-G3)
#   the reload is not discoverable   | gdh6_reload_is_discoverable_end_to_end
#   in the container                 | — two TagSourceReload markers whose
#                                    | contents are cross-tied to the steps on
#                                    | either side (GDH-G7)
#
# COMPLETED AT REVIEW (2026-09-11).  The table above is the SUBSTANTIVE
# mapping and it held up — but a claim-for-claim audit found FOUR of G0b's
# smaller assertions that the replacement did not in fact make, so for one
# commit they were claims nothing made.  A mapping that is 90% right is how a
# deletion quietly loses coverage, and "strictly stronger" has to mean every
# claim, not every important claim.  All four are now in
# `gdh6_both_versions_retrievable_end_to_end`, which went from 10 assertions
# to 14 (and its control likewise):
#
#   G0b claim                        | restored as
#   ---------------------------------+--------------------------------------
#   meta.dat bit 5                   | asserted directly.  "The view stream
#   (FlagHasAlternateSourceViews)     | decoded" and "the flag says it is
#   is set                           | there" are different statements
#   the container's own paths.dat    | asserted directly (2 assertions: the
#   decodes to what ct-print         | path list, and the record count vs
#   reports, and the count matches   | `counts.paths`).  GDH-M6 reads
#   `counts.paths`                   | paths.dat with one reader and steps
#                                    | with another and had never compared
#                                    | them; a self-consistent reader is not
#                                    | the same as two agreeing
#   the raw view NAMES the res://    | asserted directly.  Keying views by
#   path                             | `path_id` is the right key, but a view
#                                    | on the right id under someone else's
#                                    | name is invisible to an id-only check
#
# NOT restored, and this one is deliberate: G0b's byte SCAN ("v2's first probe
# token occurs nowhere in the container", with a v1 probe as its positive
# twin).  It was a NEGATIVE check needing a twin to prove the scanner could
# see anything; the replacement hash-compares every version's COMPLETE bytes
# against its own fixture, which is positive, exact, and cannot pass by
# failing to look.  The claim is subsumed, not dropped.
#
# Those live in `scripts/verify_gdh6.py`, driven by
# `scripts/record-and-verify-gdh6.sh`, with their hand-derived numbers in
# `scripts/EXPECTED-GDH6.md`.  All four gates were GREEN at review over **98**
# assertions on this host (90 before the four were restored).
#
# `CtfsContainer` and `_varint` BELOW ARE NOT DEAD CODE.  `verify_gdh6.py`
# imports both from this module, deliberately, so that there is ONE CTFS
# reader in this tree rather than a second one that can drift — and this one
# carries the offset-table validation that makes an under-reported record
# count raise instead of answering.  `ct_print_events` went with GDH-G0b;
# GDH-M6 has its own, which adds the source-reload term to the completeness
# arithmetic.
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------


def cmd_fixture(args) -> int:
    ck = Checker("fixture preconditions")
    facts = fixture_facts(args.v1, args.v2)
    check_fixture(facts, ck)
    print(json.dumps(facts, indent=2, sort_keys=True))
    return 0 if ck.report() else 1


# Trap 4c: each gate's assertion count, per arm, WRITTEN FROM A RUN
# (2026-09-10, this host; re-measured 2026-09-11 after GDH-G0b's deletion and
# unchanged, because deleting a gate does not change the remaining one's
# claims).  The arms differ legitimately — the control arm's GDH-G0a stops once
# it has established "one version, no transition" — so the fingerprint is per
# (gate, arm) rather than a single number.  A branch that stops making
# claims now goes RED instead of quietly making fewer of them.
EXPECTED_ASSERTIONS = {
    "fixture": {"reload": 12, "control": 12, "wrongtarget": 12, "identical": 10},
    "g0a": {"reload": 16, "control": 9, "wrongtarget": 16, "identical": 16},
}


def cmd_verify(args) -> int:
    out_dir = os.path.abspath(args.outdir)
    driver_json = os.path.join(out_dir, "driver.json")
    if not os.path.exists(driver_json):
        die("no driver.json in %s — the arm was never recorded.  A missing "
            "prerequisite is a LOUD failure, never a skip" % out_dir)
    record = json.load(open(driver_json))
    facts = fixture_facts(args.v1, args.v2)

    print("[gdh0] === arm %s ===" % record["arm"], file=sys.stderr)
    arm = record["arm"]
    fix = Checker("arm %s: fixture" % arm)
    check_fixture(facts, fix, allow_identical=(arm == "identical"))
    fix.expect_count(EXPECTED_ASSERTIONS["fixture"][arm])
    fixture_green = fix.report()

    obs = parse_stdout(os.path.join(out_dir, "stdout.log"))
    g0a = Checker("arm %s: GDH-G0a (the engine really reloaded)" % arm)
    check_g0a(g0a, facts, record, obs)
    g0a.expect_count(EXPECTED_ASSERTIONS["g0a"][arm])
    g0a_green = g0a.report()

    if args.check_g0b:
        # A stale caller must FAIL, not silently do less.  `--check-g0b` used
        # to select a whole second gate; a driver that still passes it would
        # otherwise run half of what it thinks it is running and report a pass
        # — which is exactly the silent-skip shape this campaign keeps finding.
        die("--check-g0b names GDH-G0b, which GDH-M6 DELETED on 2026-09-11. "
            "Its claims are now made by gdh6_both_versions_retrievable_end_to_"
            "end and gdh6_no_step_is_attributed_to_the_wrong_version; run "
            "scripts/record-and-verify-gdh6.sh. Drop the flag from the caller.")

    if args.expect_g0a_red:
        # The arm exists to prove GDH-G0a can go red.  A GREEN G0a here means
        # the gate has no teeth, which is a harness failure, not a pass.
        if g0a_green:
            print("[gdh0] FALSIFIER ARM %s: GDH-G0a stayed GREEN over a run in "
                  "which the reload could not have happened — THE GATE HAS NO "
                  "TEETH" % record["arm"], file=sys.stderr)
            return 1
        print("[gdh0] FALSIFIER ARM %s: GDH-G0a went RED as required"
              % record["arm"], file=sys.stderr)
        return 0

    return 0 if (fixture_green and g0a_green) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("fixture")
    p.add_argument("v1")
    p.add_argument("v2")
    p.set_defaults(func=cmd_fixture)

    p = sub.add_parser("pathcheck")
    p.add_argument("engine")
    p.set_defaults(func=cmd_pathcheck)

    p = sub.add_parser("record")
    p.add_argument("engine")
    p.add_argument("v1")
    p.add_argument("v2")
    p.add_argument("project_template")
    p.add_argument("outdir")
    p.add_argument("--arm", required=True, choices=sorted(ARMS))
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("verify")
    p.add_argument("outdir")
    p.add_argument("v1")
    p.add_argument("v2")
    p.add_argument("--ct-print", required=True)
    # Retained ONLY so a stale caller gets a named refusal instead of an
    # "unrecognized arguments" traceback.  See cmd_verify.
    p.add_argument("--check-g0b", action="store_true",
                   help="REMOVED by GDH-M6; passing it is now an error")
    p.add_argument("--expect-g0a-red", action="store_true")
    p.set_defaults(func=cmd_verify)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
