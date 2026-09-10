#!/usr/bin/env python3
"""GDH-M0 — the runnable falsifier.  Record a real GDScript hot reload in a
headless `template_debug` Godot engine and measure what the resulting `.ct`
does and does NOT say.

Two gates with deliberately different lifetimes (see
`codetracer-specs/Planned-Features/GDScript-Hot-Reload-Multi-Version-Sources.milestones.org`,
GDH-M0):

  GDH-G0a  PERMANENT.  The engine really reloaded.  Asserted from the ENGINE'S
           OWN STDOUT, independently of any trace.
  GDH-G0b  A DATED SNAPSHOT, retired by GDH-M6.  What the container carries
           today: ONE `paths.dat` entry for the fixture path, ONE raw source
           view whose bytes are v1's, while post-reload steps decode to line
           numbers that do not exist in v1 at all.

Every hand-derived value is in `scripts/EXPECTED-GDH0.md`.  Nothing about the
FIXTURE is written into this file: the insertion height, the probe body's line
numbers, the tick count and the per-version output tokens are all measured
from `test-programs/gdh0/probe_v{1,2}.gd` at run time, so a fixture edit cannot
silently disagree with an assertion (the milestone's "computed by the harness
from them, never written into the verifier").

NO MOCKS.  Real engine, real recorder, real container, real `ct-print`.  The
one thing this file implements itself is a minimal READER of the CTFS
container directory, because `ct-print` reports a source view's LENGTH but not
its BYTES, and GDH-G0b's anti-vacuity clause requires the bytes to be
hash-compared with the fixture.  `ct-print` also does not report `meta.dat`
bit 14: `has_line_count_table` exists only in `codetracer_ct_print_lib.nim`,
which the shipped `codetracer_ct_print.nim` does not import — so REBUILDING IT
DOES NOT HELP, and a rebuild was tried (2026-09-10 review) rather than assumed:

    nim c -d:release --mm:arc -p:src \
      --passC:"-I<nix-store>/zstd-1.5.7-dev/include" \
      --passL:"-L<nix-store>/zstd-1.5.7/lib -lzstd" \
      -o:ct-print src/codetracer_ct_print.nim        # 5.4 s, succeeds

The rebuilt binary agrees with the checked-in one on every number GDH-G0b
asserts (paths 1, source_views 1, 299 dump lines) and adds neither the bytes
nor the flag, so the reader below stays.  `CT_PRINT=` selects either.

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
        Assert GDH-G0a and (for arms that carry a container) GDH-G0b.
        `--expect-g0a-red` inverts the G0a verdict: the arm exists to prove the
        gate can fail, so a GREEN G0a there is the harness's own failure.
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
        `ct-print` gives GDH-G0b a SECOND, independent reader for its central
        claim, so "one path entry" is a measurement two instruments agree on
        rather than one instrument's opinion.
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
# `u32 LE length` + `encode_variant(Array)` (`remote_debugger_peer.cpp:98-155`).
# A host->engine command is a THREE-element array
# `[String cmd, int thread_id, Array data]` — `remote_debugger.cpp:350-371`
# `ERR_CONTINUE(cmd.size() != 3)`.  `thread_id` must name a thread the engine
# has registered; the main thread is registered unconditionally at `:798` and
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
# GDH-G0b — what the container carries today.
# ---------------------------------------------------------------------------


def ct_print_events(ct_print: str, ct: str, dest: str) -> tuple[dict, list[dict], int]:
    with open(dest, "w") as handle:
        proc = subprocess.run([ct_print, "--events", ct], stdout=handle,
                              stderr=subprocess.PIPE, timeout=600)
    if proc.returncode != 0:
        die("`ct-print --events %s` failed (rc=%d): %s"
            % (ct, proc.returncode, proc.stderr.decode("utf-8", "replace")))
    raw = open(dest, encoding="utf-8").read().splitlines()
    if not raw:
        die("`ct-print --events` produced no output for %s" % ct)
    header = json.loads(raw[0])
    events = [json.loads(line) for line in raw[1:]]
    return header, events, len(raw)


def check_g0b(ck: Checker, facts: dict, record: dict, obs: dict,
              header: dict, events: list[dict], dump_lines: int,
              container: CtfsContainer) -> None:
    counts = header["counts"]

    # --- anti-vacuity 1: the DUMP IS COMPLETE ------------------------------
    # `--events` emits one header line, one line per step, TWO per call
    # (call_entry + call_exit) and one per io event.  Asserting the exact
    # arithmetic — never `lines > 0` — is the EXPECTED-HCR1.md rule, adopted
    # campaign-wide: a dump truncated by `head` satisfies a grep as well as a
    # complete one.
    expected_lines = 1 + counts["steps"] + 2 * counts["calls"] + counts["io_events"]
    ck.eq(dump_lines, expected_lines,
          "the --events dump is COMPLETE: its line count equals "
          "1 + steps(%d) + 2*calls(%d) + io(%d)"
          % (counts["steps"], counts["calls"], counts["io_events"]))
    ck.eq(len(events), dump_lines - 1, "every non-header line parsed as an event")

    # --- anti-vacuity 2: the SCAN REACHED THE CONTAINER --------------------
    names = container.names()
    ck.ck("srcviews.dat" in names and "srcviews.off" in names,
          "the container carries `srcviews.dat` / `srcviews.off` under their "
          "REAL names — the spec's `source_views.dat` is unfindable because "
          "base40 truncates it, and a scan for it finds nothing and passes "
          "(trap 4).  Directory: %r" % (names,))
    ck.ck("paths.dat" in names, "the container carries `paths.dat`")

    # Two independent readers must agree, or the count below is one
    # instrument's opinion rather than a measurement.
    raw_paths = container.paths()
    ck.eq(raw_paths, header["paths"],
          "the container's own paths.dat decodes to exactly what ct-print "
          "reports — two independent readers agreeing")
    ck.eq(len(raw_paths), counts["paths"],
          "and its record count matches the header's `counts.paths`")

    # --- anti-vacuity 3: paths exist AND the fixture path is among them ----
    paths = header["paths"]
    ck.ck(counts["paths"] >= 1, "the decode produced at least one path")
    ck.ck("res://probe.gd" in paths,
          "the fixture's res:// path is among the decoded paths (%r) — "
          "asserted BEFORE the count, because a decode that produced no paths "
          "at all would satisfy `not two` for free" % (paths,))

    # --- THE SNAPSHOT ------------------------------------------------------
    ck.eq(counts["paths"], 1,
          "TODAY the container carries exactly ONE paths.dat entry for a file "
          "that ran in TWO versions")
    ck.eq(paths, ["res://probe.gd"], "and that entry is the fixture's path")

    views = container.source_views()
    ck.ck(len(views) >= 1, "the srcviews stream decoded at least one view")
    raw_views = [v for v in views if v["view_kind"] == 0]
    ck.eq(len(raw_views), 1,
          "exactly ONE raw (view_kind == 0) source view is attached")
    if raw_views:
        view = raw_views[0]
        ck.eq(view["path_id"], 0, "the raw view is attached to path id 0")
        ck.eq(view["view_name"], "res://probe.gd", "the view names the res:// path")
        ck.ck(len(view["content"]) > 0, "the view's bytes are NON-EMPTY")
        got = hashlib.sha256(view["content"]).hexdigest()
        ck.eq(got, facts["v1_sha256"],
              "the view's bytes hash-equal probe_v1.gd as read by the harness")
        ck.ck(got != facts["v2_sha256"],
              "and they are NOT v2's — v2's text is nowhere in the container")

    # --- the byte scan, with its positive twin -----------------------------
    # A "must not contain" over a haystack the scanner cannot read passes for
    # free.  The v1 probe is the twin: break the scan and it goes red first.
    blob = container.data
    v1_needle = facts["v1_tokens"][0].encode()
    v2_needle = facts["v2_tokens"][0].encode()
    n_v1 = blob.count(v1_needle)
    n_v2 = blob.count(v2_needle)
    ck.ck(n_v1 >= 1,
          "POSITIVE TWIN: v1's first probe token %r occurs %d time(s) in the "
          "container's raw bytes — the scan can see source text at all"
          % (v1_needle.decode(), n_v1))
    ck.eq(n_v2, 0,
          "v2's first probe token %r occurs nowhere in the container"
          % v2_needle.decode())
    v1_full = open(facts["v1_path"], "rb").read()
    ck.eq(blob.count(v1_full), 1,
          "v1's complete text occurs exactly once in the container")

    # --- the steps say otherwise ------------------------------------------
    steps = [e for e in events if e["kind"] == "step"]
    ck.eq(len(steps), counts["steps"], "every declared step was decoded")
    ck.ck(all(s["path_id"] == 0 for s in steps),
          "every step is attributed to path id 0 — the ONLY path there is")
    v1_lines = facts["v1_lines"]
    past_end = [s for s in steps if s["line"] > v1_lines]
    within = [s for s in steps if s["line"] <= v1_lines]

    if not record["reload_sent"] or not record["overwrite_requested"]:
        ck.eq(len(past_end), 0,
              "CONTROL ARM: every step lies inside v1's %d lines" % v1_lines)
        return

    ck.ck(len(within) > 0, "the pre-reload steps are non-empty")
    ck.ck(len(past_end) > 0,
          "THE DEFECT: %d steps decode to a line PAST THE END of the only "
          "source the container carries (v1 has %d lines)"
          % (len(past_end), v1_lines))
    # Both halves can legitimately be EMPTY on a run where the reload silently
    # did not take (measured: a `core:reload_scripts` addressed to an
    # unregistered `thread_id` is dropped by `_poll_messages` without a word).
    # The two assertions above already record that as a failure; computing
    # min()/max() over the empty half here would raise instead, which aborts
    # the check before `expect_count` runs and turns a clean RED verdict into a
    # traceback.  The assertion is made either way, so the count is unchanged.
    if past_end and within:
        first_past = min(s["step_index"] for s in past_end)
        last_within = max(s["step_index"] for s in within)
        ck.ck(first_past > last_within,
              "the two line regimes do not interleave: the last in-range step "
              "is #%d and the first out-of-range step is #%d — exactly one "
              "crossing" % (last_within, first_past))
    else:
        ck.ck(False,
              "the two line regimes cannot be compared: %d step(s) lie inside "
              "v1's %d lines and %d lie past its end, so there is no crossing "
              "to check" % (len(within), v1_lines, len(past_end)))

    expected_v2_body = [n + facts["shift"] for n in facts["v1_probe_body_lines"]]
    ck.eq(sorted(facts["v2_probe_body_lines"]), sorted(expected_v2_body),
          "v2's probe body lines are v1's shifted by the measured insertion")
    ck.ck(all(line > v1_lines for line in facts["v2_probe_body_lines"]),
          "every one of v2's probe body lines is outside v1's file")

    # --- and they agree, in count, with what the ENGINE printed ------------
    versions = []
    per_tick: dict[int, list[str]] = {}
    for tick, token in obs["observations"]:
        per_tick.setdefault(tick, []).append(token)
    for tick in range(1, facts["ticks"] + 1):
        got = per_tick.get(tick, [])
        versions.append("v1" if got == facts["v1_tokens"]
                        else "v2" if got == facts["v2_tokens"] else "?")
    n_v1_ticks, n_v2_ticks = versions.count("v1"), versions.count("v2")
    steps_at_v1_body = sum(1 for s in steps
                           if s["line"] in facts["v1_probe_body_lines"])
    steps_at_v2_body = sum(1 for s in steps
                           if s["line"] in facts["v2_probe_body_lines"])
    ck.eq(steps_at_v1_body, n_v1_ticks * len(facts["v1_probe_body_lines"]),
          "the trace records exactly %d steps on v1's probe body — one per "
          "body line per v1 tick the ENGINE printed" % (n_v1_ticks * len(facts["v1_probe_body_lines"])))
    ck.eq(steps_at_v2_body, n_v2_ticks * len(facts["v2_probe_body_lines"]),
          "and exactly %d on v2's — one per body line per v2 tick"
          % (n_v2_ticks * len(facts["v2_probe_body_lines"])))

    # --- why `checkLineWithinFile` never fired -----------------------------
    flags = container.meta_flags()
    has_line_count_table = bool(flags & 0x4000)
    ck.eq(has_line_count_table, False,
          "meta.dat bit 14 (FlagHasLineCountTable) is CLEAR, which is why "
          "`checkLineWithinFile` (multi_stream_writer.nim:401-431) returned ok "
          "for every one of those out-of-range lines instead of refusing them: "
          "with no line-count table the writer sizes every file at "
          "DefaultLinesPerFile (100000) and has no bound to test against.  The "
          "mis-attribution is therefore SILENT, not a hard error")
    ck.ck(bool(flags & 0x0020),
          "meta.dat bit 5 (FlagHasAlternateSourceViews) is set")


# ---------------------------------------------------------------------------


def cmd_fixture(args) -> int:
    ck = Checker("fixture preconditions")
    facts = fixture_facts(args.v1, args.v2)
    check_fixture(facts, ck)
    print(json.dumps(facts, indent=2, sort_keys=True))
    return 0 if ck.report() else 1


# Trap 4c: each gate's assertion count, per arm, WRITTEN FROM A RUN
# (2026-09-10, this host).  The arms differ legitimately — the control arm's
# GDH-G0a stops once it has established "one version, no transition", and only
# the arms that carry a meaningful container run GDH-G0b — so the fingerprint
# is per (gate, arm) rather than a single number.  A branch that stops making
# claims now goes RED instead of quietly making fewer of them.
EXPECTED_ASSERTIONS = {
    "fixture": {"reload": 12, "control": 12, "wrongtarget": 12, "identical": 10},
    "g0a": {"reload": 16, "control": 9, "wrongtarget": 16, "identical": 16},
    "g0b": {"reload": 31, "control": 23},
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

    g0b_green = True
    if args.check_g0b:
        if not record["ct"] or not os.path.exists(record["ct"]):
            die("arm %s produced no container at %r" % (record["arm"], record["ct"]))
        header, events, dump_lines = ct_print_events(
            args.ct_print, record["ct"], os.path.join(out_dir, "events.jsonl"))
        container = CtfsContainer(record["ct"])
        g0b = Checker("arm %s: GDH-G0b (what the container carries today)" % arm)
        check_g0b(g0b, facts, record, obs, header, events, dump_lines, container)
        g0b.expect_count(EXPECTED_ASSERTIONS["g0b"][arm])
        g0b_green = g0b.report()

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

    return 0 if (fixture_green and g0a_green and g0b_green) else 1


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
    p.add_argument("--check-g0b", action="store_true")
    p.add_argument("--expect-g0a-red", action="store_true")
    p.set_defaults(func=cmd_verify)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
