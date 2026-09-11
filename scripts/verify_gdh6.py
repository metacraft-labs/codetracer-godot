#!/usr/bin/env python3
"""GDH-M6 verifier — end to end.  A real Godot recording of a real reload, and
every step attributed to the version that ran it.

Design:    codetracer-specs/Planned-Features/
           GDScript-Hot-Reload-Multi-Version-Sources.md §2, §6.1-§6.4, §7.1.
Milestone: the `GDH-M6` block of the campaign's `.milestones.org`.

Four gates live here.

`gdh6_no_step_is_attributed_to_the_wrong_version`  (GDH-G3, the headline)
    A BIJECTION, not a containment.  The engine prints one token per executed
    probe line carrying `(iteration, version, line)`; the verifier decodes every
    step in the container that resolves to the fixture path and requires a
    perfect one-to-one ORDERED match.  `sorted(a) == sorted(b)` would pass on a
    trace that permuted two steps across the boundary — which is precisely a
    misattribution — so the comparison is element-wise at equal offsets and
    reports the FIRST offset at which the two diverge.
    Plus the text half: for every step, the source view retrieved for that
    step's path id must carry, AT the decoded line, the exact bytes that
    version's fixture has at that line.  Without it a trace could match every
    line number and still render both halves against one version's text.

`gdh6_both_versions_retrievable_end_to_end`        (GDH-G1 + GDH-G2)
    `paths.dat` entries with the IDENTICAL `res://` string and different
    indices, and one raw source view per version whose bytes hash-equal that
    version's fixture file.

`gdh6_reload_is_discoverable_end_to_end`           (GDH-G7)
    The reloaded recording carries two `TagSourceReload` markers; the no-reload
    recording of the same program carries none; the two containers are
    otherwise identical in their SET of event kinds.  The markers' CONTENTS are
    asserted, not their presence: `(old_path_id, new_path_id)` must be two of
    the ids found for the fixture path, `new_path_id` must be the id the steps
    immediately FOLLOWING the marker resolve to and `old_path_id` the one the
    steps preceding it resolve to, and `reload_ordinal` must be 1 then 2.

`gdh5_reload_is_refused_while_a_step_is_pending`   (inherited from GDH-M5)
    A reload requested while the recorder holds a pending step is deferred to
    the next safe point, never applied mid-step — asserted by checking that no
    step's values were split across a marker in the resulting container.

`allowed_mocks: none`.  Real engine, real recorder, real container, real
`ct-print`.  The one thing implemented here is a minimal CTFS container reader,
ported from `scripts/verify_gdh0.py` (which ported it from
`codetracer_ctfs/container.nim`), because `ct-print` reports a source view's
LENGTH but not its BYTES and the text half needs the bytes.  This copy adds the
`meta.dat` bit-14 `paths.dat` record layout, which GDH-M0's containers did not
have: `payload_len varint + payload + line_count varint`.

Harness rules (codetracer-specs/Testing/Verification-Harness-Traps.md):

  * Every check counts itself and the run asserts its own assertion count
    (trap 4c), so a guard that returned early is RED rather than quietly
    making fewer claims.
  * The `--events` dump is proven COMPLETE by the line-count-equals-header
    -counts rule, never by `lines > 0` (trap 4).
  * A cardinality mismatch over an INCOMPLETE dump is a CHECK-FAIL, not a
    kill: its cause is the instrument, not the subject.
  * "the path did not resolve" and "the path resolved to the wrong version"
    are reported DIFFERENTLY (RESOLUTION-FAILURE vs GDH6-FAIL), because they
    have different fixes and folding them together sends the next reader to
    the wrong file.
  * Every socket wait is bounded and its expiry is a named verdict.

The falsifier arms that live HERE rather than in the engine are the CONSUMER
-side ones the milestone names — arms 2, 3 and 6 — because the defect each
reproduces is a defect of path RESOLUTION, which is the verifier's own job in
this harness.  `--falsify` selects one; it is `none` unless asked for, and the
gate prints the active arm so a green run cannot be mistaken for a measurement
of the real thing.
"""
from __future__ import annotations

import argparse
import base64
import collections
import hashlib
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# Trap 4c: the attribution gate's claim count, WRITTEN FROM A RUN.  It lives
# here rather than inline at `expect_count` because the gate's own
# resolution-failure branch has to pad up to it: that branch cannot reach the
# remaining checks, and a branch that quietly asserts fewer times is exactly
# the degradation `expect_count` exists to catch.  One constant, two readers,
# so the pad and the expectation cannot drift apart (they had, by two).
ATTRIBUTION_CLAIMS = 14

# The CTFS reader and the agent wire are already written, reviewed and used by
# two shipped gates.  Importing them keeps ONE implementation of each rather
# than a second that can drift — and `verify_gdh0.py`'s reader in particular
# carries the offset-table validation that makes an under-reported record count
# raise instead of answering.
from verify_gdh0 import CtfsContainer, _varint  # noqa: E402
import verify_gdh5 as agentwire  # noqa: E402


# ---------------------------------------------------------------------------
# Verdict plumbing.  A gate failure, a harness failure and a resolution failure
# are three different things and none of them is allowed to look like another.
# ---------------------------------------------------------------------------

class Checker:
    def __init__(self, gate: str) -> None:
        self.gate = gate
        self.asserted = 0
        self.failures: list[str] = []
        self.resolution_failures: list[str] = []

    def note(self, msg: str) -> None:
        print("[gdh6]   %s" % msg)

    def ck(self, ok: bool, msg: str) -> bool:
        self.asserted += 1
        if not ok:
            self.failures.append(msg)
            print("GDH6-FAIL[%s]: %s" % (self.gate, msg), file=sys.stderr)
        return ok

    def eq(self, actual, expected, msg: str) -> bool:
        return self.ck(actual == expected,
                       "%s (expected %r, got %r)" % (msg, expected, actual))

    def resolution_fail(self, msg: str) -> None:
        """A path that did not resolve AT ALL.

        Kept apart from `ck` deliberately.  "the path did not resolve" and
        "the path resolved to the wrong version" have different fixes, and a
        harness that folds them together sends the next agent to the wrong
        file.  This still fails the gate — it is not a pass — but it says so
        under its own name.
        """
        self.asserted += 1
        self.resolution_failures.append(msg)
        print("GDH6-RESOLUTION-FAILURE[%s]: %s" % (self.gate, msg),
              file=sys.stderr)

    def check_fail(self, msg: str) -> None:
        """The INSTRUMENT failed, not the subject.  Never a kill."""
        print("GDH6-CHECK-FAIL[%s]: %s" % (self.gate, msg), file=sys.stderr)
        self.failures.append("CHECK-FAIL: " + msg)

    def expect_count(self, expected: int) -> None:
        if self.asserted != expected:
            msg = ("assertion count is %d, expected %d — this gate did not "
                   "make all the claims it is supposed to make"
                   % (self.asserted, expected))
            self.failures.append(msg)
            print("GDH6-FAIL[%s]: %s" % (self.gate, msg), file=sys.stderr)

    @property
    def red(self) -> bool:
        return bool(self.failures or self.resolution_failures)

    def report(self) -> bool:
        if self.red:
            print("[gdh6] %s: RED — %d failure(s), %d resolution failure(s), "
                  "%d assertions" % (self.gate, len(self.failures),
                                     len(self.resolution_failures),
                                     self.asserted))
        else:
            print("[gdh6] %s: GREEN — %d assertions"
                  % (self.gate, self.asserted))
        return not self.red


def die(msg: str) -> "NoReturn":  # noqa: F821
    print("DRIVER-FAIL: %s" % msg, file=sys.stderr)
    sys.exit(2)


# ---------------------------------------------------------------------------
# The container, under meta.dat bit 14.
#
# GDH-M0's containers were written in the BARE interning layout, where a
# `paths.dat` record IS the payload bytes.  A reload session's container sets
# bit 14, and the record grows a trailing line count:
#
#     payload_len: varint
#     payload:     [u8] x payload_len
#     line_count:  varint
#
# (`interning_table.nim`, `ensureQualifiedPathIdWithLineCount` /
# `appendQualifiedPathWithLineCount`.)  Which of the two a reader decodes is
# decided by the meta.dat bits and NEVER by inspecting the bytes — the two
# record spaces overlap, so a reader that sniffed would sometimes be right.
# ---------------------------------------------------------------------------

FLAG_LINE_COUNT_TABLE = 1 << 14


class Gdh6Container(CtfsContainer):
    def has_line_count_table(self) -> bool:
        return bool(self.meta_flags() & FLAG_LINE_COUNT_TABLE)

    def sized_paths(self) -> list[tuple[str, int]]:
        """`paths.dat` as (path, recorded_line_count) pairs.

        Refuses rather than guesses: a record with trailing bytes, or one whose
        payload length runs past the record, means the layout this reader
        implements is not the one on disk — and a reader that answered anyway
        would under-report the path count, which every "there are two versions"
        assertion written over it would then satisfy for free.
        """
        if not self.has_line_count_table():
            raise ValueError(
                "this container does not set meta.dat bit 14, so its paths.dat "
                "records carry no line count; decoding them as if they did "
                "would read the first bytes of the next record as a length")
        out = []
        for i, rec in enumerate(self._variable_records("paths")):
            pos = 0
            payload_len, pos = _varint(rec, pos)
            if pos + payload_len > len(rec):
                raise ValueError(
                    "paths.dat record %d declares a %d-byte payload but the "
                    "record is %d bytes" % (i, payload_len, len(rec)))
            payload = rec[pos:pos + payload_len]
            pos += payload_len
            line_count, pos = _varint(rec, pos)
            if pos != len(rec):
                raise ValueError(
                    "paths.dat record %d has %d trailing byte(s) — the "
                    "line-count-table layout this reader implements is not "
                    "the one on disk" % (i, len(rec) - pos))
            out.append((payload.decode("utf-8"), line_count))
        return out


# ---------------------------------------------------------------------------
# The fixtures, measured rather than declared.
# ---------------------------------------------------------------------------

TOKEN_RE = re.compile(r"GDH6\|it=(\d+)\|v=(\d+)\|L=(\d+)\|")
# Anchored to something only CODE can produce: the leading tab of a GDScript
# statement and the `print(` receiver.  Trap 4d — a pattern that matches the
# fixture's own header comment (which quotes the token shape twice) would be
# satisfied by prose, and this file's headers do quote it.
PROBE_LINE_RE = re.compile(r'^\t+print\("GDH6\|it=", n, "\|v=(\d+)\|L=(\d+)\|"\)')


def read_fixture(path: str) -> dict:
    with open(path, "rb") as handle:
        raw = handle.read()
    text = raw.decode("utf-8")
    lines = text.split("\n")
    # The recorded line count the engine computes: newlines when the file ends
    # in one, newlines + 1 otherwise.  Re-derived here rather than imported, so
    # the two implementations are independent and a disagreement is visible.
    newlines = raw.count(b"\n")
    addressable = newlines if raw.endswith(b"\n") else newlines + 1
    probes: dict[int, str] = {}
    version = None
    for idx, line in enumerate(lines):
        m = PROBE_LINE_RE.match(line)
        if not m:
            continue
        declared_version, declared_line = int(m.group(1)), int(m.group(2))
        actual_line = idx + 1
        if declared_line != actual_line:
            die("%s line %d declares `L=%d`.  The token's line number is "
                "written by hand (GDScript has no __LINE__) and it no longer "
                "agrees with the line it sits on, so every comparison in this "
                "gate would be made against a number the fixture does not "
                "mean.  Fix the fixture."
                % (path, actual_line, declared_line))
        if version is None:
            version = declared_version
        elif version != declared_version:
            die("%s mixes version tags %d and %d" % (path, version,
                                                     declared_version))
        probes[actual_line] = line
    if version is None:
        die("%s declares no probe lines at all; the pattern this harness "
            "matches with is anchored to a leading tab and a `print(` "
            "receiver, so a reformatted fixture reads as empty" % path)
    return dict(path=path, bytes=raw, text=text, lines=lines,
                sha256=hashlib.sha256(raw).hexdigest(),
                addressable_lines=addressable, version=version,
                probe_lines=sorted(probes), probe_text=probes)


def fixture_preconditions(ck: Checker, fixtures: list[dict]) -> None:
    """The two preconditions on the FIXTURES themselves.

    Without them the bijection below is satisfiable by the wrong version and
    the gate silently degrades into a line-number check.
    """
    # (i) probe LINE NUMBERS disjoint between versions.
    seen: dict[int, int] = {}
    overlaps = []
    for fx in fixtures:
        for line in fx["probe_lines"]:
            if line in seen:
                overlaps.append((line, seen[line], fx["version"]))
            seen[line] = fx["version"]
    ck.eq(overlaps, [],
          "the versions' probe LINE NUMBERS are disjoint — a line number that "
          "two versions share would not identify a version and the bijection "
          "would degrade to a line check")

    # The stronger consequence GDH-M0 asked for: each version's probes lie
    # past the previous version's whole file, so a post-reload line number
    # does not exist in the previous version's text AT ALL.
    for prev, cur in zip(fixtures, fixtures[1:]):
        ck.ck(min(cur["probe_lines"]) > prev["addressable_lines"],
              "v%d's first probe line %d lies past the whole of v%d (%d "
              "lines), so a consumer resolving the recorded path to v%d's text "
              "is asked for a line that text does not reach"
              % (cur["version"], min(cur["probe_lines"]), prev["version"],
                 prev["addressable_lines"], prev["version"]))

    # (ii) the probe lines' SOURCE TEXT differs between versions at every
    # compared line, so the text half is a real discrimination.
    texts = [set(fx["probe_text"].values()) for fx in fixtures]
    shared = set.intersection(*texts) if texts else set()
    ck.eq(sorted(shared), [],
          "no two versions share a probe line's SOURCE TEXT — if they did, "
          "\"the retrieved view has the right text at that line\" would be "
          "satisfied by the wrong version for free")

    # (iii) different probe-line COUNTS, so the expected step total is a sum
    # over versions and cannot be written as a flat product.
    counts = [len(fx["probe_lines"]) for fx in fixtures]
    ck.eq(len(set(counts)), len(counts),
          "the versions have pairwise DIFFERENT probe-line counts %r — equal "
          "counts would let a flat product stand in for the sum, and a flat "
          "product agrees with a trace that lost a whole version" % (counts,))


# ---------------------------------------------------------------------------
# Running one recording.
# ---------------------------------------------------------------------------

class Run:
    def __init__(self) -> None:
        self.rc: int | None = None
        self.stdout = ""
        self.reload_results: list[dict] = []
        self.transcript: list[str] = []
        self.failure: str | None = None
        self.container_path: str | None = None
        self.reloads_sent = 0

    def tokens(self) -> list[tuple[int, int, int]]:
        """`(iteration, version, line)` in EMISSION order."""
        return [(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                for m in TOKEN_RE.finditer(self.stdout)]

    def live_iterations(self) -> dict[int, set[int]]:
        out: dict[int, set[int]] = collections.defaultdict(set)
        for it, ver, _line in self.tokens():
            out[ver].add(it)
        return out


def prepare_project(fixtures_dir: str, work: str, v1_name: str) -> str:
    project = os.path.join(work, "project")
    if os.path.isdir(project):
        shutil.rmtree(project)
    os.makedirs(project)
    shutil.copyfile(os.path.join(fixtures_dir, "project.godot"),
                    os.path.join(project, "project.godot"))
    shutil.copyfile(os.path.join(fixtures_dir, v1_name),
                    os.path.join(project, "probe.gd"))
    return project


def record(engine: str, fixtures_dir: str, work: str, schedule: list,
           bound: float, sock_dir: str, polled: bool = True,
           env_extra: dict | None = None,
           expect_timeout: bool = False) -> Run:
    """Record one run, delivering `schedule` = [(tick, fixture, generation)].

    `polled` selects WHERE the notification is serviced.  With
    `REPRO_HCR_AGENT_POLL=1` the agent is drained from the engine's own safe
    point, so the handler applies where it stands.  Without it the agent runs
    on its own detached thread and a notification lands whenever it arrives —
    which is how the DEFERRAL path is reached, and the only way to reach it.
    """
    result = Run()
    project = prepare_project(fixtures_dir, work, "probe_v1.gd")
    trace_dir = os.path.join(work, "trace")
    os.makedirs(trace_dir, exist_ok=True)

    sock_path = os.path.join(sock_dir, "gdh6-%d.sock" % os.getpid())
    if len(sock_path) >= 100:
        die("the agent socket path is %d bytes; sun_path holds 108: %s"
            % (len(sock_path), sock_path))
    if os.path.exists(sock_path):
        os.unlink(sock_path)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(sock_path)
    listener.listen(1)

    env = dict(os.environ)
    env["REPRO_HCR_AGENT_SOCKET"] = sock_path
    if polled:
        env["REPRO_HCR_AGENT_POLL"] = "1"
    else:
        env.pop("REPRO_HCR_AGENT_POLL", None)
    env["CT_GDSCRIPT_TRACE"] = trace_dir
    env.update(env_extra or {})
    argv = [engine, "--headless", "--path", project, "--script",
            "res://probe.gd"]
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=env, text=True,
                            bufsize=1)

    lines: list[str] = []

    def finish(failure: str | None) -> Run:
        result.failure = failure
        rest = proc.stdout.read() if proc.stdout else ""
        result.stdout = "".join(lines) + rest
        result.rc = proc.wait()
        result.container_path = os.path.join(trace_dir, "gdscript_trace.ct")
        if not os.path.isfile(result.container_path):
            result.container_path = None
        try:
            listener.close()
            os.unlink(sock_path)
        except OSError:
            pass
        return result

    listener.settimeout(bound)
    try:
        conn, _ = listener.accept()
    except socket.timeout:
        proc.kill()
        return finish("the engine never connected to the agent socket "
                      "within %.1f s" % bound)
    peer = agentwire.AgentPeer(conn)
    result.transcript = peer.transcript

    kind, obj = peer.read(bound)
    if kind != "hello":
        proc.kill()
        return finish("the engine's first frame was %r (%s)" % (kind, obj))
    caps = obj["hello"]["capabilities"]
    if "source-reload" not in caps:
        proc.kill()
        return finish("the engine's hello does not advertise source-reload; "
                      "it advertised %r" % (caps,))
    peer.send(agentwire.hello_ack(obj["hello"]["supportProfile"]))

    for tick, fixture_name, generation in schedule:
        marker = "GDH6_TICK=%d " % tick
        deadline = time.monotonic() + bound
        reached = False
        while time.monotonic() < deadline:
            line = proc.stdout.readline() if proc.stdout else ""
            if not line:
                break
            lines.append(line)
            if marker in line:
                reached = True
                break
        if not reached:
            proc.kill()
            return finish("the fixture never printed %r; the reload window "
                          "for generation %d was never entered"
                          % (marker, generation))
        with open(os.path.join(fixtures_dir, fixture_name), "rb") as handle:
            content = handle.read()
        peer.send(agentwire.source_changed("gdh6-r-%04d" % generation,
                                           "res://probe.gd", generation,
                                           content))
        result.reloads_sent += 1
        kind, obj = peer.read(bound)
        if kind == "sourceReloadResult":
            result.reload_results.append(obj["sourceReloadResult"])
        elif expect_timeout:
            # The gate that drives a deliberate timeout still has to get an
            # answer; a missing one is a hang, not a timeout.
            proc.kill()
            return finish("no sourceReloadResult for generation %d: %s (%s)"
                          % (generation, kind, obj))
        else:
            proc.kill()
            return finish("no sourceReloadResult for generation %d: %s (%s)"
                          % (generation, kind, obj))

    return finish(None)


# ---------------------------------------------------------------------------
# Decoding a container.
# ---------------------------------------------------------------------------

def ct_print_events(ct_print: str, ct: str, dest: str) -> dict:
    with open(dest, "w") as handle:
        proc = subprocess.run([ct_print, "--events", ct], stdout=handle,
                              stderr=subprocess.PIPE, timeout=900)
    if proc.returncode != 0:
        die("`ct-print --events %s` failed (rc=%d): %s"
            % (ct, proc.returncode, proc.stderr.decode("utf-8", "replace")))
    raw = open(dest, encoding="utf-8").read().splitlines()
    if not raw:
        die("`ct-print --events` produced no output for %s" % ct)
    header = json.loads(raw[0])
    events = [json.loads(line) for line in raw[1:]]
    counts = header["counts"]
    # The COMPLETENESS rule, stated as arithmetic and never as `lines > 0`.
    # `--events` emits one header line, one line per step, TWO per call
    # (entry + exit), one per io event, and one per source-reload marker.
    #
    # MEASURED, not assumed: `counts.steps` does NOT include the markers even
    # though the writer advances its own `stepCount` for one.  The header of a
    # two-reload recording reports steps=310 / source_reloads=2 and the dump
    # is 441 lines = 1 + 310 + 2*64 + 0 + 2.  Adding the term without checking
    # would have been off by exactly the number of markers — i.e. wrong only
    # on the runs this milestone is about.
    expected = (1 + counts["steps"] + 2 * counts["calls"]
                + counts["io_events"] + counts.get("source_reloads", 0))
    return dict(header=header, events=events, lines=len(raw),
                expected_lines=expected, complete=(len(raw) == expected),
                counts=counts)


class Resolver:
    """Maps a step's `path_id` to a VERSION ORDINAL for the fixture path.

    This is the consumer-side behaviour the campaign is about, and it is where
    the milestone's falsifier arms 2, 3 and 6 live: each is a real, named way
    of getting this mapping wrong, and each has shipped somewhere.
    """

    FIXTURE = "res://probe.gd"

    def __init__(self, sized_paths: list[tuple[str, int]], falsify: str):
        self.falsify = falsify
        self.sized = sized_paths
        self.ids = [i for i, (p, _c) in enumerate(sized_paths)
                    if p == self.FIXTURE]
        self.line_counts = {i: sized_paths[i][1] for i in self.ids}

    @property
    def resolvable(self) -> tuple[bool, str]:
        if self.falsify == "fuzzy-unique-only":
            # ARM 6, specific to this campaign's own hazard.  `fuzzy_path_id_
            # for`'s stage 6 (`trace_reader.rs:1341-1356`) returns `Some` only
            # when `matches.len() == 1`.  A second version makes the filename
            # ambiguous and the lookup answers `None` — so the gate must go red
            # on a RESOLUTION FAILURE, reported under its own name, rather than
            # on a wrong attribution.
            if len(self.ids) != 1:
                return False, ("`fuzzy_path_id_for` stage 6 returns Some only "
                               "when exactly one paths.dat entry matches the "
                               "filename; %d entries match `%s`, so the lookup "
                               "answers None and no step can be attributed at "
                               "all" % (len(self.ids), self.FIXTURE))
        if not self.ids:
            return False, ("no paths.dat entry carries `%s`" % self.FIXTURE)
        return True, ""

    def version_of(self, path_id: int):
        """The 1-based version ordinal, or None when `path_id` is not ours."""
        if path_id not in self.ids:
            return None
        if self.falsify == "newest-wins":
            # ARM 2 — the behaviour the codebase ships TODAY: `Db::path_map`
            # is a last-wins map keyed by path STRING
            # (`ctfs_trace_reader/mod.rs:1372-1376`), so every step on the
            # file resolves to the most recently registered version.
            return len(self.ids)
        if self.falsify == "oldest-wins":
            # ARM 3 — the mirror image, and the error an implementer reaches
            # for when fixing arm 2: keep the FIRST registration instead of
            # the last.  A gate that does not kill both is not testing this.
            return 1
        return self.ids.index(path_id) + 1


def source_view_text(views: list[dict], path_id: int) -> list[str] | None:
    for v in views:
        if v["path_id"] == path_id and v["view_kind"] == 0:
            return v["content"].decode("utf-8").split("\n")
    return None


# ---------------------------------------------------------------------------
# GDH-G3 — the headline gate.
# ---------------------------------------------------------------------------

def gate_attribution(ck: Checker, run: Run, fixtures: list[dict],
                     dump: dict, container: Gdh6Container,
                     resolver: Resolver, control: bool) -> None:
    counts = dump["counts"]
    by_version = {fx["version"]: fx for fx in fixtures}

    # --- anti-vacuity, all of it BEFORE any comparison --------------------
    ck.ck(dump["complete"],
          "the --events dump is COMPLETE: %d lines, and 1 + steps(%d) + "
          "2*calls(%d) + io(%d) = %d"
          % (dump["lines"], counts["steps"], counts["calls"],
             counts["io_events"], dump["expected_lines"]))

    tokens = run.tokens()
    ck.ck(len(tokens) > 0,
          "the stdout token sequence is NON-EMPTY (an empty comparison is a "
          "pass for free)")

    resolvable, why = resolver.resolvable
    if not resolvable:
        ck.resolution_fail(
            "the fixture path did not RESOLVE, so no step could be attributed "
            "to any version and the question this gate asks was never "
            "reached: %s" % why)
        # Every remaining assertion still has to be COUNTED, or a resolution
        # failure would quietly shrink the claim set (trap 4b).  They are
        # recorded as unreachable rather than skipped.
        #
        # The padding is derived from the gate's own declared claim count
        # rather than written as a literal.  MEASURED AT REVIEW (2026-09-11):
        # the literal was 13, which made this branch assert SIXTEEN times
        # against an `expect_count(14)` — so the fuzzy-unique-only arm, whose
        # whole point is to be a RESOLUTION FAILURE and nothing else, also
        # emitted a `GDH6-FAIL[...] assertion count is 16, expected 14 — this
        # gate did not make all the claims it is supposed to make`.  That
        # sentence was false (the gate made MORE claims, not fewer) and it put
        # a plain GDH6-FAIL on the one arm the entry requires to be reported
        # under a DIFFERENT name.  The driver's grep happened to match the
        # resolution line first, so the arm was still scored correctly — but a
        # reader, or a stricter driver asserting "no GDH6-FAIL on this arm",
        # would have been told the harness had degraded when it had not.
        # Deriving the pad keeps the two numbers from drifting apart again.
        while ck.asserted < ATTRIBUTION_CLAIMS:
            ck.resolution_fail("unreachable: the path did not resolve")
        return

    probe_lines = {fx["version"]: set(fx["probe_lines"]) for fx in fixtures}
    steps = [e for e in dump["events"] if e["kind"] == "step"]
    ck.eq(len(steps), counts["steps"],
          "every step the header declares was decoded (the markers are "
          "counted separately, under `source_reloads`)")

    decoded: list[tuple[int, int, int]] = []  # (step_index, version, line)
    for s in steps:
        version = resolver.version_of(s["path_id"])
        if version is None:
            continue
        if s["line"] in probe_lines.get(version, ()):
            decoded.append((s["step_index"], version, s["line"]))
    decoded.sort()
    ck.ck(len(decoded) > 0, "the decoded step sequence is NON-EMPTY")

    distinct_ids = {s["path_id"] for s in steps
                    if resolver.version_of(s["path_id"]) is not None}
    if control:
        ck.eq(len(resolver.ids), 1,
              "CONTROL ARM (no reload): the container reports exactly ONE "
              "version of the fixture path")
        ck.eq(len(distinct_ids), 1,
              "CONTROL ARM: every step resolves to that one version")
    else:
        ck.ck(len(resolver.ids) >= 2,
              "at least two path ids resolve to the fixture path (%r)"
              % (resolver.ids,))
        ck.ck(len(distinct_ids) >= 2,
              "at least two distinct path ids actually carry steps (%r)"
              % (sorted(distinct_ids),))

    views = [v for v in container.source_views() if v["view_kind"] == 0]
    contents = {v["path_id"]: v["content"] for v in views}
    if control:
        ck.eq(len(views), 1,
              "CONTROL ARM: exactly one raw source view was retrieved")
        ck.ck(True, "CONTROL ARM: one view cannot differ from itself")
    else:
        ck.ck(len(views) >= 2,
              "at least two raw source views were retrieved (%d)" % len(views))
        ck.eq(len(set(bytes(c) for c in contents.values())), len(contents),
              "and their contents DIFFER — identical views would satisfy the "
              "text half from either version")

    # --- the expected count: a SUM over versions, derived from the fixtures
    # AND the observed reload schedule.  Never a literal, and never a flat
    # product: the versions do not have equal probe-line counts and the
    # reloads do not split the iterations evenly.
    live = run.live_iterations()
    expected_total = 0
    for version, iterations in sorted(live.items()):
        if version not in by_version:
            ck.check_fail("stdout carries a version tag v%d that no fixture "
                          "declares" % version)
            return
        expected_total += len(iterations) * len(by_version[version]["probe_lines"])
    ck.eq(len(tokens), expected_total,
          "the stdout token count equals sum over versions of (iterations "
          "that version was live x that version's probe-line count) = %s"
          % " + ".join("%d x %d" % (len(its), len(by_version[v]["probe_lines"]))
                       for v, its in sorted(live.items())))

    if len(decoded) != expected_total:
        if not dump["complete"]:
            ck.check_fail(
                "cardinality mismatch (%d decoded vs %d expected) over an "
                "INCOMPLETE dump — the cause is the instrument, not the "
                "subject" % (len(decoded), expected_total))
        else:
            ck.ck(False,
                  "the trace carries %d probe steps and the program emitted "
                  "%d tokens; the dump is complete, so the difference is the "
                  "trace's" % (len(decoded), expected_total))
    else:
        ck.ck(True, "the two sequences have equal length (%d)" % expected_total)

    # --- THE BIJECTION.  Ordered, element-wise, first divergence reported.
    token_seq = [(v, line) for _it, v, line in tokens]
    trace_seq = [(v, line) for _idx, v, line in decoded]
    common = min(len(token_seq), len(trace_seq))
    first_bad = None
    for i in range(common):
        if token_seq[i] != trace_seq[i]:
            first_bad = i
            break
    if first_bad is None and len(token_seq) == len(trace_seq):
        ck.ck(True, "ORDERED one-to-one match over %d probe steps: every step "
                    "is attributed to the version that ran it" % common)
    else:
        where = first_bad if first_bad is not None else common
        ck.ck(False,
              "the sequences diverge at offset %d: the program printed "
              "(v%s, line %s) and the trace says (v%s, line %s).  Context "
              "stdout=%r trace=%r"
              % (where,
                 token_seq[where][0] if where < len(token_seq) else "-",
                 token_seq[where][1] if where < len(token_seq) else "-",
                 trace_seq[where][0] if where < len(trace_seq) else "-",
                 trace_seq[where][1] if where < len(trace_seq) else "-",
                 token_seq[max(0, where - 2):where + 3],
                 trace_seq[max(0, where - 2):where + 3]))

    # --- THE TEXT HALF.  A trace could match every line number and still
    # render both halves against one version's text.
    missing_views, wrong_text = [], []
    for step_index, version, line in decoded:
        path_id = next(i for i in resolver.ids
                       if resolver.version_of(i) == version)
        text = source_view_text(views, path_id)
        if text is None:
            missing_views.append((step_index, path_id))
            continue
        if line - 1 >= len(text):
            wrong_text.append((step_index, version, line, "<past end>"))
            continue
        want = by_version[version]["probe_text"][line]
        if text[line - 1] != want:
            wrong_text.append((step_index, version, line, text[line - 1]))
    ck.eq(missing_views, [],
          "every step's path id has a raw source view attached")
    ck.eq(wrong_text[:3], [],
          "every step's source view carries, AT the decoded line, the exact "
          "text that version's fixture has there (%d mismatch(es))"
          % len(wrong_text))

    # --- the recorded per-file sizes are each version's own, not shared.
    recorded = [c for (p, c) in resolver.sized if p == Resolver.FIXTURE]
    want_sizes = [fx["addressable_lines"] for fx in fixtures][:len(recorded)]
    ck.eq(recorded, want_sizes,
          "each version's paths.dat record states ITS OWN line count; a "
          "version registered with the previous one's count would put its "
          "high lines outside its own slot")


# ---------------------------------------------------------------------------
# GDH-G1 + GDH-G2.
# ---------------------------------------------------------------------------

def gate_retrievable(ck: Checker, fixtures: list[dict],
                     container: Gdh6Container, resolver: Resolver,
                     dump: dict, control: bool) -> None:
    names = container.names()
    ck.ck("srcviews.dat" in names and "srcviews.off" in names,
          "the container carries `srcviews.dat` / `srcviews.off` under their "
          "REAL names — base40 truncates the spec's `source_views.dat`, and a "
          "scan for that name finds nothing and passes every negative check "
          "written over it (trap 4).  Directory: %r" % (names,))
    ck.ck(container.has_line_count_table(),
          "meta.dat bit 14 is SET — without it a second paths.dat record "
          "carries no size and both versions share the DefaultLinesPerFile "
          "stride")
    # RESTORED AT REVIEW (2026-09-11).  GDH-G0b asserted this and the mapping
    # note at its deletion did not carry it, so for one commit it was a claim
    # nothing made.  `container.source_views()` decoding anything at all
    # depends on the stream being present, but "the flag that says the stream
    # is there" and "the stream happened to decode" are different statements
    # and G0b made the first one.
    ck.ck(bool(container.meta_flags() & 0x0020),
          "meta.dat bit 5 (FlagHasAlternateSourceViews) is SET")

    sized = resolver.sized
    ck.ck(len(sized) > 0, "paths.dat decoded at least one record")
    ck.ck(Resolver.FIXTURE in [p for p, _c in sized],
          "the fixture's res:// path is among the decoded paths (%r) — "
          "asserted BEFORE the count, because a decode that produced no paths "
          "would satisfy every count assertion for free"
          % ([p for p, _c in sized],))

    want_versions = 1 if control else len(fixtures)
    ck.eq(len(resolver.ids), want_versions,
          "the container carries %d paths.dat entr%s for the fixture path"
          % (want_versions, "y" if want_versions == 1 else "ies"))
    ck.eq(len({p for p, _c in sized if p == Resolver.FIXTURE}), 1,
          "and every one of them carries the IDENTICAL res:// string — a "
          "version is a path INDEX, never a mangled path string, because "
          "every consumer resolves a user-supplied path by name")

    # RESTORED AT REVIEW (2026-09-11): TWO INDEPENDENT READERS AGREE.
    #
    # GDH-G0b asserted `container.paths() == ct-print's header paths` and
    # `len(paths) == counts.paths`, so that "there is one entry" was a
    # measurement two instruments made rather than one instrument's opinion.
    # The deletion note claims "GDH-M6's gates rely on exactly that" — and
    # they did not literally: this file reads `paths.dat` with its own reader
    # and reads steps and markers out of `ct-print`, and never compared the
    # two.  A divergence would have shown up only indirectly, as a count that
    # happened to be wrong, and `Gdh6Container.sized_paths` raising on a
    # mis-decode is a guard against one reader, not agreement between two.
    hdr_paths = dump["header"].get("paths", [])
    ck.eq([p for p, _c in sized], hdr_paths,
          "the container's own paths.dat decodes to exactly what ct-print "
          "reports — two independent readers agreeing, not one opinion")
    ck.eq(len(sized), dump["counts"]["paths"],
          "and its record count matches the header's `counts.paths`")

    views = container.source_views()
    ck.ck(len(views) >= 1, "the srcviews stream decoded at least one view")
    raw_views = [v for v in views if v["view_kind"] == 0]
    ck.eq(len(raw_views), want_versions,
          "exactly %d RAW (view_kind == 0) source view(s) are attached — the "
          "kind is checked because a prettier view (kind 1) would satisfy a "
          "bare count" % want_versions)
    # RESTORED AT REVIEW: G0b asserted the view NAMES the res:// path.  This
    # file keys views by `path_id` alone, which is the right key — but the
    # name is a second, independent statement of what the view is about, and
    # a view attached to the right id under someone else's name is a defect
    # the id-only check cannot see.
    ck.eq(sorted({v["view_name"] for v in raw_views}), [Resolver.FIXTURE],
          "and every raw view NAMES the fixture's res:// path")

    # Hashes are computed HERE, at run time, from the fixture files.  A hash
    # written into this verifier would agree with a stale fixture.
    got = {}
    for view in raw_views:
        version = resolver.version_of(view["path_id"])
        if version is None:
            ck.ck(False, "a raw view is attached to path id %d, which is not "
                         "one of the fixture path's ids" % view["path_id"])
            continue
        got[version] = hashlib.sha256(view["content"]).hexdigest()
    want = {fx["version"]: fx["sha256"] for fx in fixtures[:want_versions]}
    ck.eq(got, want,
          "each version's raw source view hash-equals that version's fixture "
          "file as read by this harness at run time")
    if not control:
        ck.eq(len(set(got.values())), want_versions,
              "and the %d views are pairwise distinct" % want_versions)
    else:
        ck.ck(True, "CONTROL ARM: one view, nothing to distinguish")


# ---------------------------------------------------------------------------
# GDH-G7 — the reload is discoverable.
# ---------------------------------------------------------------------------

def gate_discoverable(ck: Checker, reloaded: dict, noreload: dict,
                      resolver: Resolver, n_expected: int) -> None:
    # (a) both dumps proven COMPLETE.
    ck.ck(reloaded["complete"],
          "the reloaded run's dump is COMPLETE (%d lines == %d expected)"
          % (reloaded["lines"], reloaded["expected_lines"]))
    ck.ck(noreload["complete"],
          "the no-reload run's dump is COMPLETE (%d lines == %d expected)"
          % (noreload["lines"], noreload["expected_lines"]))
    # (b) the no-reload dump is NON-EMPTY before "no markers" is a finding.
    ck.ck(len(noreload["events"]) > 0,
          "the no-reload dump is NON-EMPTY — \"it carries no marker\" is only "
          "a finding about a run that produced events")

    # (c) the event-kind SETS, read from the decoder's own output.
    kinds_reloaded = {e["kind"] for e in reloaded["events"]}
    kinds_noreload = {e["kind"] for e in noreload["events"]}
    ck.ck(len(kinds_reloaded) > 0, "the reloaded run's kind set is non-empty")
    ck.ck(len(kinds_noreload) > 0, "the no-reload run's kind set is non-empty")
    added = kinds_reloaded - kinds_noreload
    removed = kinds_noreload - kinds_reloaded
    if removed or added - {"source_reload"}:
        # (d) any OTHER difference means the two runs did not execute the same
        # program shape, and the gate has measured the fixture rather than the
        # marker.  That is a CHECK-FAIL, not a kill.
        ck.check_fail(
            "the two runs' event-kind sets differ by more than the marker "
            "(added %r, removed %r); they did not execute the same program "
            "shape, so this comparison measured the fixture"
            % (sorted(added), sorted(removed)))
    ck.eq(sorted(added), ["source_reload"],
          "the reloaded run's kind set minus the no-reload run's is exactly "
          "{source_reload}")
    ck.eq(sorted(removed), [],
          "and the reverse difference is empty")

    markers = [e for e in reloaded["events"] if e["kind"] == "source_reload"]
    ck.eq(len(markers), n_expected,
          "the reloaded recording carries %d TagSourceReload marker(s)"
          % n_expected)
    ck.eq(len([e for e in noreload["events"] if e["kind"] == "source_reload"]),
          0, "the no-reload recording carries none")

    # --- THE CONTENTS, not the presence.  A well-formed marker with a zeroed
    # payload is discoverable and useless.
    steps = [e for e in reloaded["events"] if e["kind"] == "step"]
    ck.ck(len(steps) > 0, "there are steps to cross-tie the markers against")
    ordinals = [m["reload_ordinal"] for m in markers]
    ck.eq(ordinals, list(range(1, n_expected + 1)),
          "the ordinals are 1..%d — a writer that emitted a constant would "
          "make the second reload indistinguishable from the first"
          % n_expected)
    for i, m in enumerate(markers):
        ck.eq(m.get("changed_count"), 1,
              "marker %d names exactly one changed file" % (i + 1))
        changed = (m.get("changed") or [{}])[0]
        old_id, new_id = changed.get("old_path_id"), changed.get("new_path_id")
        ck.ck(old_id in resolver.ids and new_id in resolver.ids,
              "marker %d's (old_path_id=%r, new_path_id=%r) are both ids the "
              "fixture path owns (%r)" % (i + 1, old_id, new_id, resolver.ids))
        ck.ck(old_id != new_id,
              "marker %d's two ids differ" % (i + 1))
        ck.eq(changed.get("generation"), i + 2,
              "marker %d carries wire generation %d (generation 1 is the "
              "content the process started with, so a reload's is >= 2)"
              % (i + 1, i + 2))
        # The ids are cross-tied to the steps on either side of the marker.
        pos = m["step_index"]
        before = [s for s in steps if s["step_index"] < pos
                  and s["path_id"] in resolver.ids]
        after = [s for s in steps if s["step_index"] > pos
                 and s["path_id"] in resolver.ids]
        ck.ck(bool(before) and bool(after),
              "marker %d has fixture steps on BOTH sides (%d before, %d "
              "after) — a marker at the very start or end could be cross-tied "
              "to nothing" % (i + 1, len(before), len(after)))
        if before and after:
            ck.eq(before[-1]["path_id"], old_id,
                  "the step immediately PRECEDING marker %d resolves to its "
                  "old_path_id" % (i + 1))
            ck.eq(after[0]["path_id"], new_id,
                  "the step immediately FOLLOWING marker %d resolves to its "
                  "new_path_id" % (i + 1))
        else:
            ck.ck(False, "marker %d could not be cross-tied" % (i + 1))
            ck.ck(False, "marker %d could not be cross-tied" % (i + 1))


# ---------------------------------------------------------------------------
# The inherited GDH-M5 gate: a reload is DEFERRED, never applied mid-step.
# ---------------------------------------------------------------------------

def gate_pending_step(ck: Checker, deferred: Run, dump: dict,
                      container: Gdh6Container, idle: Run,
                      resolver: Resolver) -> None:
    # --- anti-vacuity: the race window was actually ENTERED.
    #
    # This is a precondition, NOT the kill.  The milestone is explicit that the
    # gate must go red "by inspecting the container, not by asserting that the
    # guard was called", and the first version of this gate failed that test:
    # its only discriminating assertion was this line, and its falsifier arm
    # reddened it while the container half stayed green because the field it
    # read (`values`) is not one `ct-print` emits.  The check below was
    # rewritten against a MEASURED difference between the two containers.
    ck.ck("[ct-gdh5] reload deferred to the next safe point" in deferred.stdout,
          "the recorder reports that the reload was DEFERRED; a run in which "
          "the notification happened to land at a safe point never entered "
          "the window this gate grades and must fail, not pass")
    counts = dump["counts"]
    ck.ck(counts["values"] > 0,
          "the container has a non-zero value count (%d) — a container with "
          "no values cannot exhibit a split and would pass for free"
          % counts["values"])
    markers = [e for e in dump["events"] if e["kind"] == "source_reload"]
    ck.ck(len(markers) >= 1,
          "the container carries at least one marker to test against")
    ck.ck(dump["complete"],
          "the dump is COMPLETE (%d lines == %d expected)"
          % (dump["lines"], dump["expected_lines"]))

    # --- THE PROPERTY, read off the container.
    #
    # The writer keeps values.dat parallel-indexed to steps.dat, and a marker
    # occupies a record in BOTH — an exec record and an empty value record —
    # which is what keeps the two in lock-step.  So `values == steps +
    # source_reloads` exactly, and it holds under the mutation too; that makes
    # it a structural invariant worth asserting and NOT a discriminator, and
    # it is labelled as such so nobody later mistakes it for the kill.
    ck.eq(counts["values"], counts["steps"] + counts["source_reloads"],
          "the value stream stays parallel-indexed to the step stream "
          "(values == steps + markers).  STRUCTURAL, not discriminating: it "
          "holds under the mid-step mutation too")

    # THE DISCRIMINATOR, measured rather than reasoned about.  A reload applied
    # where the notification landed flushes the recorder's single pending-step
    # slot in the middle of a step, so a step that executed against the OLD
    # version is written AFTER the marker.  Measured on the two containers:
    # the deferred run has ZERO such steps at both markers; the
    # apply-where-it-lands run has one at each (step 76, line 64 of v1, after
    # a marker at 75; step 177, line 104 of v2, after a marker at 176).
    #
    # The bound is the marker's OWN `in_flight_frames` field, not a literal
    # zero.  Design §5.4 says steps belonging to frames still executing the old
    # version's bytecode legitimately appear after the marker carrying the old
    # id — which is exactly why the field is RECORDED.  A gate that demanded a
    # clean cut would be asserting something the design does not claim; a gate
    # that ignored the field would pass over any number of them.
    # THE KILL: every marker records `in_flight_frames == 0`.
    #
    # This is the container's own statement that the apply happened at a point
    # where NO GDScript frame was executing — which is what "safe point" means
    # (§5.6.1: the poll point is called from `OS_LinuxBSD::run()` between
    # `Main::iteration()` calls, and the recorder MEASURES the depth of its own
    # open-frame stack rather than writing a literal).  A reload applied where
    # the notification landed records >= 1 whenever the VM was mid-frame, and
    # that is the whole difference between the two behaviours.
    #
    # MEASURED, and the measurement corrected an earlier version of this gate.
    # The first formulation allowed up to `in_flight_frames` old-version steps
    # after the marker — and the mutation satisfied it, because it honestly
    # reported `in_flight_frames: 1` and produced exactly one such step.  Both
    # containers were internally consistent; only the FIELD ITSELF separates
    # them (plain: 0 and 0 at both markers; mutated: 1 and 1).  That earlier
    # formulation is kept below, because it is a real invariant and because
    # per §5.4 a future host with true in-flight frames will need it — but it
    # is labelled as non-discriminating FOR THIS ARM so that nobody later
    # mistakes it for the kill.
    in_flight = [(m["step_index"], m.get("in_flight_frames"))
                 for m in markers if m.get("in_flight_frames", 0) != 0]
    ck.eq(in_flight, [],
          "every marker records in_flight_frames == 0: the apply happened at "
          "a point where no GDScript frame was executing, which is what the "
          "safe point IS.  A reload applied where the notification landed "
          "reports the frames it cut across")

    late = []
    steps = [e for e in dump["events"] if e["kind"] == "step"]
    for m in markers:
        pos = m["step_index"]
        old = (m.get("changed") or [{}])[0].get("old_path_id")
        after_old = [s for s in steps
                     if s["step_index"] > pos and s["path_id"] == old]
        if len(after_old) > m.get("in_flight_frames", 0):
            late.append(dict(marker=pos, old_path_id=old,
                             in_flight_frames=m.get("in_flight_frames"),
                             steps=[(s["step_index"], s["line"])
                                    for s in after_old[:4]]))
    ck.eq(late, [],
          "no step of the OLD version appears after its marker beyond the "
          "in-flight-frame count the marker itself records.  NOT "
          "DISCRIMINATING for the apply-where-it-lands arm — measured green "
          "under it — and kept because it is the invariant that will matter "
          "when a host really does reload across live frames")
    # WHY BOTH, stated because the labelling above undersells it (noted at
    # review, 2026-09-11).  `in_flight_frames` is the RECORDER'S OWN REPORT,
    # so on its own it is a self-report and a mutation that lied would pass.
    # `late` is the CONTAINER'S CONSEQUENCE, so on its own the honest mutation
    # satisfies it.  Together they close both doors, and neither alone does:
    #
    #   reports 1, produces 1 (the measured arm)  -> in_flight != 0  -> RED
    #   reports 0, produces 1 (a lying mutation)  -> late != []      -> RED
    #   reports 0, produces 0                      -> applied at a safe point
    #
    # So "the kill" and "NOT DISCRIMINATING" describe the two checks against
    # ONE arm, not their standing: the CONJUNCTION is what makes the property
    # mechanically witnessed, and removing either one reopens a case.

    # A third invariant, also measured NON-discriminating for this arm and
    # kept for the same reason: the mutated run registered four paths.dat
    # records, but only three of them carried the fixture path, so the count
    # below agreed.  Stated so the next reader does not re-derive it.
    ck.eq(len(resolver.ids), len(markers) + 1,
          "the fixture path has exactly one paths.dat entry per reload plus "
          "its original.  NOT DISCRIMINATING for that arm either")

    # --- the control: a reload requested at an IDLE point is applied
    # IMMEDIATELY, so "deferred" is distinguishable from "never applied".
    ck.ck(len(idle.reload_results) > 0,
          "the idle-point CONTROL produced a reload result")
    if idle.reload_results:
        ck.eq(idle.reload_results[0].get("outcome"), "applied",
              "the idle-point CONTROL's reload was APPLIED")
    else:
        ck.ck(False, "the idle-point CONTROL produced no result")
    ck.ck("[ct-gdh5] reload deferred to the next safe point"
          not in idle.stdout,
          "and it was NOT deferred — so `deferred` names a real, distinct "
          "state rather than the only state there is")
    ck.ck(len(deferred.reload_results) > 0
          and deferred.reload_results[0].get("outcome") == "applied",
          "the DEFERRED reload was nonetheless applied, and the coordinator "
          "got exactly one answer for it")


# ---------------------------------------------------------------------------
# Main.
# ---------------------------------------------------------------------------

FIXTURE_NAMES = ["probe_v1.gd", "probe_v2.gd", "probe_v3.gd"]
SCHEDULE = [(8, "probe_v2.gd", 2), (18, "probe_v3.gd", 3)]

GATES = ["attribution", "retrievable", "discoverable", "pending-step"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", required=True)
    parser.add_argument("--fixtures", required=True)
    parser.add_argument("--work", required=True)
    parser.add_argument("--ct-print", default=None)
    parser.add_argument("--bound", type=float, default=120.0)
    parser.add_argument("--socket-dir", default=None)
    parser.add_argument("--gate", default="all", choices=["all"] + GATES)
    parser.add_argument("--falsify", default="none",
                        choices=["none", "newest-wins", "oldest-wins",
                                 "fuzzy-unique-only"],
                        help="consumer-side falsifier arm (milestone arms "
                             "2, 3 and 6).  Named on stdout so a green run "
                             "under one cannot be read as a measurement of "
                             "the real resolver.")
    args = parser.parse_args()

    if not os.access(args.engine, os.X_OK):
        die("the engine is not executable: %s" % args.engine)
    ct_print = args.ct_print or os.path.join(
        HERE, "..", "..", "codetracer-trace-format-nim", "ct-print")
    ct_print = os.path.abspath(ct_print)
    if not os.access(ct_print, os.X_OK):
        die("ct-print is not executable: %s" % ct_print)
    for name in FIXTURE_NAMES + ["project.godot"]:
        if not os.path.isfile(os.path.join(args.fixtures, name)):
            die("missing fixture %s in %s" % (name, args.fixtures))
    sock_dir = args.socket_dir or os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    if not os.path.isdir(sock_dir):
        sock_dir = "/tmp"
    os.makedirs(args.work, exist_ok=True)

    print("[gdh6] engine    : %s" % args.engine)
    print("[gdh6] ct-print  : %s" % ct_print)
    print("[gdh6] falsifier : %s" % args.falsify)

    fixtures = [read_fixture(os.path.join(args.fixtures, n))
                for n in FIXTURE_NAMES]

    checkers: list[Checker] = []

    # --- the fixture's own preconditions, before anything is recorded ------
    pre = Checker("gdh6_fixture_preconditions")
    fixture_preconditions(pre, fixtures)
    pre.expect_count(3 + len(fixtures) - 1)
    checkers.append(pre)
    if pre.red:
        pre.report()
        print("\n[gdh6] the fixtures cannot support the gate; nothing was "
              "recorded.")
        return 1

    def load(run: Run, tag: str):
        if run.failure:
            die("the %s run did not happen: %s" % (tag, run.failure))
        if run.rc != 0:
            die("the %s engine exited %s\n%s" % (tag, run.rc,
                                                 run.stdout[-3000:]))
        if not run.container_path:
            die("the %s run produced no container" % tag)
        dump = ct_print_events(ct_print, run.container_path,
                               os.path.join(args.work, tag + ".events"))
        container = Gdh6Container(run.container_path)
        resolver = Resolver(container.sized_paths(), args.falsify)
        return dump, container, resolver

    need_reloaded = args.gate in ("all", "attribution", "retrievable",
                                  "discoverable")
    need_control = args.gate in ("all", "attribution", "retrievable",
                                 "discoverable")

    if need_reloaded:
        reloaded = record(args.engine, args.fixtures,
                          os.path.join(args.work, "reloaded"), SCHEDULE,
                          args.bound, sock_dir)
        r_dump, r_container, r_resolver = load(reloaded, "reloaded")
    if need_control:
        control = record(args.engine, args.fixtures,
                         os.path.join(args.work, "control"), [],
                         args.bound, sock_dir)
        c_dump, c_container, c_resolver = load(control, "control")

    if args.gate in ("all", "attribution"):
        ck = Checker("gdh6_no_step_is_attributed_to_the_wrong_version")
        print("== %s ==" % ck.gate)
        gate_attribution(ck, reloaded, fixtures, r_dump, r_container,
                         r_resolver, control=False)
        ck.expect_count(ATTRIBUTION_CLAIMS)
        checkers.append(ck)
        ck.report()

        cck = Checker("gdh6_no_step_is_attributed_to_the_wrong_version"
                      " [CONTROL: no reload]")
        print("== %s ==" % cck.gate)
        gate_attribution(cck, control, fixtures[:1], c_dump, c_container,
                         c_resolver, control=True)
        cck.expect_count(ATTRIBUTION_CLAIMS)
        checkers.append(cck)
        cck.report()

    if args.gate in ("all", "retrievable"):
        ck = Checker("gdh6_both_versions_retrievable_end_to_end")
        print("== %s ==" % ck.gate)
        gate_retrievable(ck, fixtures, r_container, r_resolver, r_dump,
                         control=False)
        # Written LAST, from a run (trap 4c): a number guessed from the source
        # is a claim about the file, not about what a run verified.
        # 10 -> 14 at review, when four GDH-G0b claims the deletion note
        # promised were found to be made by nothing: meta.dat bit 5, the
        # two-reader agreement on paths.dat (two assertions), and the view
        # name.
        ck.expect_count(14)
        checkers.append(ck)
        ck.report()

        cck = Checker("gdh6_both_versions_retrievable_end_to_end"
                      " [CONTROL: no reload]")
        print("== %s ==" % cck.gate)
        gate_retrievable(cck, fixtures, c_container, c_resolver, c_dump,
                          control=True)
        cck.expect_count(14)
        checkers.append(cck)
        cck.report()

    if args.gate in ("all", "discoverable"):
        ck = Checker("gdh6_reload_is_discoverable_end_to_end")
        print("== %s ==" % ck.gate)
        gate_discoverable(ck, r_dump, c_dump, r_resolver, len(SCHEDULE))
        # 11 fixed assertions + 7 per marker, both counted from a run.
        ck.expect_count(11 + 7 * len(SCHEDULE))
        checkers.append(ck)
        ck.report()

    if args.gate in ("all", "pending-step"):
        ck = Checker("gdh5_reload_is_refused_while_a_step_is_pending")
        print("== %s ==" % ck.gate)
        # The DEFERRAL path is only reachable with the agent on its OWN
        # thread: with REPRO_HCR_AGENT_POLL=1 the notification is serviced
        # from the safe point itself and `g_ct_in_safe_point` is already
        # true, so nothing is ever queued.  This is the only way to enter
        # the window the gate grades, and the anti-vacuity check above
        # fails the run if it was not entered.
        deferred = record(args.engine, args.fixtures,
                          os.path.join(args.work, "deferred"), SCHEDULE,
                          args.bound, sock_dir, polled=False)
        d_dump, d_container, d_resolver = load(deferred, "deferred")
        idle = record(args.engine, args.fixtures,
                      os.path.join(args.work, "idle"), SCHEDULE,
                      args.bound, sock_dir, polled=True)
        if idle.failure:
            die("the idle-point control did not happen: %s" % idle.failure)
        gate_pending_step(ck, deferred, d_dump, d_container, idle,
                          d_resolver)
        # Written from a run (trap 4c).
        ck.expect_count(12)
        checkers.append(ck)
        ck.report()

    total = sum(c.asserted for c in checkers)
    red = [c for c in checkers if c.red]
    print()
    print("[gdh6] %d assertion(s) over %d gate(s); %d red"
          % (total, len(checkers), len(red)))
    if total == 0:
        die("no check ran at all; a run that asserts nothing is not a pass")
    return 1 if red else 0


if __name__ == "__main__":
    sys.exit(main())
