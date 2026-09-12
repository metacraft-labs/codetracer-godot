#!/usr/bin/env python3
"""GDH-M8 verifier — failure semantics.  Design §8.1's invariant, graded on a
real Godot recording:

    Either a file's new version is live in the engine AND registered in the
    trace, or neither.  There is no third state.

Design:    codetracer-specs/Planned-Features/
           GDScript-Hot-Reload-Multi-Version-Sources.md §5.5, §8.1-§8.3.
Milestone: the `GDH-M8` block of the campaign's `.milestones.org`.

Three gates live here, each with its own control arm.

`gdh8_refused_reload_leaves_a_coherent_trace`                      (GDH-G6)
    A v2 that does NOT compile.  The engine keeps running v1 and keeps
    recording; the container opens; it carries exactly ONE entry for the
    fixture path, exactly ONE raw source view holding v1's bytes, and NO
    `TagSourceReload`; every step resolves to v1; the post-refusal step count
    is non-zero.  `sourceReloadResult` reports `refused` / `parse-error`.

`gdh8_digest_mismatch_is_refused_before_anything_is_touched`
    A `sourceChanged` whose `content` does not hash to its `snapshotDigest` is
    refused with `digest-mismatch`, with no engine change and no trace change.

`gdh8_a_failure_after_registration_closes_the_trace_rather_than_continuing`
    A failure injected between trace registration and the engine swap (§8.1
    steps 4-6) closes the trace with a recorded reason and leaves the engine on
    v1.  The trace must remain READABLE: opening it and decoding its full step
    stream must succeed, and no step may be attributed to a path id with no
    source view.

`allowed_mocks: none` for the first two.  The third injects its failure through
a real fault-injection hook in the SHIPPED code path rather than through a
double — a double would prove this harness's ordering rather than the product's
— and the hook's inertness is a gate of its own, run by the driver
(`record-and-verify-gdh8.sh`) because it needs a second BUILD.

WHAT THIS FILE DOES NOT REIMPLEMENT.  The CTFS reader, the `paths.dat`
line-count-table layout and the agent wire are already written, reviewed and
used by shipped gates.  They are imported, so there is ONE implementation of
each rather than a second that can drift.

Harness rules (codetracer-specs/Testing/Verification-Harness-Traps.md):

  * Every gate counts its own assertions and asserts the count (trap 4c), so a
    guard that returned early is RED rather than quietly making fewer claims.
  * The `--events` dump is proven COMPLETE by header arithmetic, never by
    `lines > 0` (trap 4).
  * ANTI-VACUITY FIRST.  A run in which the notification never arrived also
    produces a one-version container, and the two must not be conflated: the
    refusal is asserted — by outcome AND by reason — before anything about the
    container is looked at.  A crash is not a refusal (trap 1), so the engine's
    exit code is asserted to be 0 and a dead engine is a CHECK-FAIL.
  * A harness failure (rc 2) is never a kill.
  * Two independent readers.  Path entries are read BOTH by this file's own
    CTFS reader and out of `ct-print`'s header, and required to agree — a
    single reader's opinion about its own output is not a measurement.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# `Gdh6Container` is `verify_gdh0`'s CTFS reader plus the meta.dat bit-14
# `paths.dat` record layout; `ct_print_events` carries the dump-completeness
# arithmetic. Imported rather than re-implemented, so there is ONE of each.
from verify_gdh6 import Gdh6Container, ct_print_events  # noqa: E402
import verify_gdh5 as agentwire  # noqa: E402

FIXTURE_PATH = "res://probe.gd"

# Anchored to something only CODE can produce — a leading tab and the `print(`
# receiver.  Trap 4d: a pattern that also matched the fixture's own header
# comment (which quotes the token shape) would be satisfied by prose, and this
# fixture's header does quote it.
PROBE_LINE_RE = re.compile(r'^\t+print\("GDH8\|it=", n, "\|L=(\d+)\|"\)')
TOKEN_RE = re.compile(r"GDH8\|it=(\d+)\|L=(\d+)\|")


# ---------------------------------------------------------------------------
# Verdicts.  A gate failure, a harness failure and a dead engine are three
# different things and none may look like another.
# ---------------------------------------------------------------------------

class Checker:
    def __init__(self, gate: str) -> None:
        self.gate = gate
        self.asserted = 0
        self.failures: list[str] = []

    def note(self, msg: str) -> None:
        print("[gdh8]   %s" % msg)

    def ck(self, ok: bool, msg: str) -> bool:
        self.asserted += 1
        if not ok:
            self.failures.append(msg)
            print("GDH8-FAIL[%s]: %s" % (self.gate, msg), file=sys.stderr)
        return bool(ok)

    def eq(self, actual, expected, msg: str) -> bool:
        return self.ck(actual == expected,
                       "%s (expected %r, got %r)" % (msg, expected, actual))

    def check_fail(self, msg: str) -> None:
        """The INSTRUMENT failed, not the subject.  Never a kill."""
        print("GDH8-CHECK-FAIL[%s]: %s" % (self.gate, msg), file=sys.stderr)
        self.failures.append("CHECK-FAIL: " + msg)

    def expect_count(self, expected: int) -> None:
        if self.asserted != expected:
            msg = ("assertion count is %d, expected %d — this gate did not "
                   "make all the claims it is supposed to make"
                   % (self.asserted, expected))
            self.failures.append(msg)
            print("GDH8-FAIL[%s]: %s" % (self.gate, msg), file=sys.stderr)

    @property
    def red(self) -> bool:
        return bool(self.failures)

    def report(self) -> bool:
        if self.red:
            print("[gdh8] %s: RED — %d failure(s), %d assertions"
                  % (self.gate, len(self.failures), self.asserted))
        else:
            print("[gdh8] %s: GREEN — %d assertions"
                  % (self.gate, self.asserted))
        return not self.red


def die(msg: str):
    print("DRIVER-FAIL: %s" % msg, file=sys.stderr)
    sys.exit(2)


# ---------------------------------------------------------------------------
# The fixtures, measured rather than declared.
# ---------------------------------------------------------------------------

def read_fixture(path: str) -> dict:
    with open(path, "rb") as handle:
        raw = handle.read()
    text = raw.decode("utf-8")
    lines = text.split("\n")
    newlines = raw.count(b"\n")
    addressable = newlines if raw.endswith(b"\n") else newlines + 1
    probes: dict[int, str] = {}
    for idx, line in enumerate(lines):
        m = PROBE_LINE_RE.match(line)
        if not m:
            continue
        declared_line, actual_line = int(m.group(1)), idx + 1
        if declared_line != actual_line:
            die("%s line %d declares `L=%d`.  The token's line number is "
                "written by hand (GDScript has no __LINE__) and it no longer "
                "agrees with the line it sits on, so every comparison in this "
                "gate would be made against a number the fixture does not "
                "mean.  Fix the fixture." % (path, actual_line, declared_line))
        probes[actual_line] = line
    if not probes:
        die("%s declares no probe lines at all; the pattern this harness "
            "matches with is anchored to a leading tab and a `print(` "
            "receiver, so a reformatted fixture reads as empty" % path)
    return dict(path=path, bytes=raw, text=text, lines=lines,
                sha256=hashlib.sha256(raw).hexdigest(),
                addressable_lines=addressable,
                probe_lines=sorted(probes), probe_text=probes)


def fixture_preconditions(ck: Checker, v1: dict, v2ok: dict, v2bad: dict,
                          engine: str, work: str) -> None:
    """The preconditions on the FIXTURES themselves, before anything is run.

    Without them every gate below degrades into something weaker and still
    passes, which is the shape this campaign keeps finding.
    """
    # (i) v2_ok is v1 plus lines appended at the END and nothing else.  That is
    # what makes every line of v1 an executed line of v2 as well, and it is why
    # the version discriminator below can be "lines that exist only in v2".
    ck.ck(v2ok["bytes"].startswith(v1["bytes"]),
          "probe_v2_ok.gd starts with probe_v1.gd BYTE FOR BYTE — the fixture "
          "is append-only, so v1's line numbers survive the reload")
    # (ii) v2 has probe lines v1 does not reach AT ALL.  `> v1's addressable
    # line count` is the strong form: a consumer resolving a post-reload step
    # to v1's text is asked for a line that text does not have.
    v2_only = [n for n in v2ok["probe_lines"] if n not in v1["probe_lines"]]
    ck.ck(len(v2_only) > 0 and min(v2_only) > v1["addressable_lines"],
          "v2 has probe lines %r that lie past the whole of v1 (%d lines), so "
          "their appearance in stdout can only mean the engine reloaded"
          % (v2_only, v1["addressable_lines"]))
    ck.ck(len(v1["probe_lines"]) != len(v2ok["probe_lines"]),
          "the two versions have DIFFERENT probe-line counts (%d vs %d) — "
          "equal counts would let a flat product stand in for the sum"
          % (len(v1["probe_lines"]), len(v2ok["probe_lines"])))
    ck.ck(v1["sha256"] != v2ok["sha256"],
          "v1 and v2_ok are different files")
    # (iii) THE PARSE-ERROR FIXTURE ACTUALLY FAILS TO PARSE, and it fails in
    # the engine rather than in this harness's opinion.  A `v2_bad` that in
    # fact compiled would make the refusal gate measure nothing at all, and a
    # v2_bad rejected for some OTHER reason (bad utf-8, empty) would exercise a
    # different code path while looking the same from here.
    ck.ck(v2bad["bytes"].startswith(v1["bytes"]),
          "probe_v2_bad.gd also starts with probe_v1.gd byte for byte, so it "
          "differs from probe_v2_ok.gd only in the defect")
    proj = os.path.join(work, "fixture-precheck")
    if os.path.isdir(proj):
        shutil.rmtree(proj)
    os.makedirs(proj)
    shutil.copyfile(os.path.join(os.path.dirname(v1["path"]), "project.godot"),
                    os.path.join(proj, "project.godot"))
    with open(os.path.join(proj, "probe.gd"), "wb") as handle:
        handle.write(v2bad["bytes"])
    try:
        proc = subprocess.run(
            [engine, "--headless", "--path", proj, "--script", "res://probe.gd"],
            capture_output=True, text=True, timeout=180)
        blob = proc.stdout + proc.stderr
    except subprocess.TimeoutExpired:
        blob = ""
        ck.check_fail("the parse-error precheck run timed out")
    ck.ck("Parse Error" in blob,
          "GODOT ITSELF calls probe_v2_bad.gd a parse error when asked to load "
          "it cold — the fixture's defect is measured against the engine, not "
          "asserted by this harness")
    ck.ck("GDH8_BEGIN" not in blob,
          "and the broken fixture never reaches `_initialize`, so it is "
          "unrunnable rather than merely warned about")


# ---------------------------------------------------------------------------
# One recording.
# ---------------------------------------------------------------------------

class Run:
    def __init__(self) -> None:
        self.rc: int | None = None
        self.stdout = ""
        self.reload_results: list[dict] = []
        self.failure: str | None = None
        self.container_path: str | None = None
        self.reload_tick: int | None = None
        # The project's own `probe.gd`, so the ON-DISK state is gradeable.
        # `assert_disk_holds` is the only user; see the note there for why a
        # gate that never looks at the disk misses half of design §8.1's
        # ordering change.
        self.script_path: str | None = None

    def script_sha256(self) -> str | None:
        if not self.script_path or not os.path.isfile(self.script_path):
            return None
        with open(self.script_path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()

    def tokens(self) -> list[tuple[int, int]]:
        """`(iteration, line)` in EMISSION order."""
        return [(int(m.group(1)), int(m.group(2)))
                for m in TOKEN_RE.finditer(self.stdout)]

    def lines_seen(self) -> set[int]:
        return {line for _it, line in self.tokens()}

    def tokens_after_reload(self) -> list[tuple[int, int]]:
        if self.reload_tick is None:
            return []
        return [(it, line) for it, line in self.tokens()
                if it > self.reload_tick]


def source_changed(reload_id: str, path: str, generation: int, content: bytes,
                   digest_override: str | None = None) -> dict:
    """`agentwire.source_changed`, with the snapshot digest overridable.

    The override is the ONLY difference, and it is what
    `gdh8_digest_mismatch_is_refused_before_anything_is_touched` needs: a
    notification whose `content` does not hash to its `snapshotDigest` while
    every other field — `lineCount` included — is correct, so the refusal
    cannot come from a different check.
    """
    msg = agentwire.source_changed(reload_id, path, generation, content)
    if digest_override is not None:
        msg["sourceChanged"]["changedFiles"][0]["snapshotDigest"] = digest_override
    return msg


def record(engine: str, fixtures_dir: str, work: str, schedule: list,
           bound: float, sock_dir: str, env_extra: dict | None = None) -> Run:
    """Record one run, delivering `schedule` = [(tick, bytes, generation,
    digest_override)]."""
    result = Run()
    project = os.path.join(work, "project")
    if os.path.isdir(project):
        shutil.rmtree(project)
    os.makedirs(project)
    shutil.copyfile(os.path.join(fixtures_dir, "project.godot"),
                    os.path.join(project, "project.godot"))
    shutil.copyfile(os.path.join(fixtures_dir, "probe_v1.gd"),
                    os.path.join(project, "probe.gd"))
    result.script_path = os.path.join(project, "probe.gd")
    trace_dir = os.path.join(work, "trace")
    os.makedirs(trace_dir, exist_ok=True)

    sock_path = os.path.join(sock_dir, "gdh8-%d.sock" % os.getpid())
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
    env["REPRO_HCR_AGENT_POLL"] = "1"
    env["CT_GDSCRIPT_TRACE"] = trace_dir
    env.update(env_extra or {})
    argv = [engine, "--headless", "--path", project, "--script", "res://probe.gd"]
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
        return finish("the engine never connected to the agent socket within "
                      "%.1f s" % bound)
    peer = agentwire.AgentPeer(conn)

    kind, obj = peer.read(bound)
    if kind != "hello":
        proc.kill()
        return finish("the engine's first frame was %r (%s)" % (kind, obj))
    caps = obj["hello"]["capabilities"]
    if "source-reload" not in caps:
        proc.kill()
        return finish("the engine's hello does not advertise source-reload; it "
                      "advertised %r" % (caps,))
    peer.send(agentwire.hello_ack(obj["hello"]["supportProfile"]))

    for tick, content, generation, digest_override in schedule:
        marker = "GDH8_TICK=%d " % tick
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
            return finish("the fixture never printed %r; the reload window for "
                          "generation %d was never entered" % (marker, generation))
        peer.send(source_changed("gdh8-r-%04d" % generation, FIXTURE_PATH,
                                 generation, content, digest_override))
        result.reload_tick = tick
        kind, obj = peer.read(bound)
        if kind != "sourceReloadResult":
            proc.kill()
            return finish("no sourceReloadResult for generation %d: %s (%s)"
                          % (generation, kind, obj))
        result.reload_results.append(obj["sourceReloadResult"])

    return finish(None)


# ---------------------------------------------------------------------------
# Reading a container.
# ---------------------------------------------------------------------------

class View:
    def __init__(self, run: Run, ct_print: str, work: str, tag: str):
        self.dump = ct_print_events(ct_print, run.container_path,
                                    os.path.join(work, tag + ".events"))
        self.container = Gdh6Container(run.container_path)
        self.sized = self.container.sized_paths()
        self.ids = [i for i, (p, _c) in enumerate(self.sized) if p == FIXTURE_PATH]
        self.steps = [e for e in self.dump["events"] if e["kind"] == "step"]
        self.markers = [e for e in self.dump["events"]
                        if e["kind"] == "source_reload"]
        self.raw_views = [v for v in self.container.source_views()
                          if v["view_kind"] == 0]


def assert_refusal(ck: Checker, run: Run, want_reason: str, tag: str,
                   want_outcome: str = "refused") -> bool:
    """The ANTI-VACUITY block, and it runs before ANY container claim.

    A run in which the notification never arrived also produces a one-version
    container.  Conflating the two is the silent-self-pass shape verbatim, so
    the refusal is established first — by outcome, by reason, and by the
    engine's exit code, because a crash is not a refusal.

    IT DOES NOT SHORT-CIRCUIT THE GATE, and that is a correction rather than an
    oversight.  The first version of this file padded the remaining claims out
    with `unreachable` when the refusal could not be established, which kept
    the assertion count honest and destroyed the gate's discrimination: under
    `REPRO_HCR_GDH8_FALSIFY_SKIP_DIGEST_CHECK` the reload APPLIES, so the
    refusal block went red and the gate never reached "not one of v2's probe
    lines appears in stdout" — which is the kill the milestone's falsifier text
    REQUIRES ("the gate must go red by observing v2's tokens in stdout, i.e. by
    the engine having reloaded, not merely by the absence of an error
    message").  The same was true of the trace-closing arm and its
    orphaned-source-view check.  Measured on the driver's first full run, both
    arms killed on the absence of an error and nothing else.

    Every check below therefore runs unconditionally.  Nothing is lost: a run
    in which the notification never arrived still reddens these assertions, so
    it still cannot pass.
    """
    ok = True
    ok &= ck.eq(len(run.reload_results), 1,
                "%s: the coordinator got exactly ONE sourceReloadResult" % tag)
    res = run.reload_results[0] if run.reload_results else {}
    ok &= ck.eq(res.get("outcome"), want_outcome,
                "%s: the reload's OUTCOME is %r.  Design §4.3's enum has three "
                "values and this agent only ever emitted two of them until "
                "GDH-M8; `refused` means nothing was touched and `failed` "
                "means the session is degraded, which is exactly §8.1's "
                "steps-1-3 versus steps-4-6 split" % (tag, want_outcome))
    refused = res.get("refusedFiles") or [{}]
    ok &= ck.eq(refused[0].get("reason"), want_reason,
                "%s: and refused BY NAME, with the reason design §5.5 has for "
                "it" % tag)
    ok &= ck.ck(bool((refused[0].get("detail") or "").strip()),
                "%s: the refusal carries a non-empty detail, so it is "
                "diagnosable and not just a code" % tag)
    ok &= ck.eq(run.rc, 0,
                "%s: the engine EXITED 0.  Trap 1 — a crash is not a refusal, "
                "and an arm whose engine died is a CHECK-FAIL" % tag)
    ok &= ck.ck("GDH8_END" in run.stdout,
                "%s: the program ran to its own end (`GDH8_END`), so "
                "\"the engine kept running\" is measured" % tag)
    return bool(ok)


def assert_single_version_container(ck: Checker, view: View, v1: dict,
                                    tag: str) -> None:
    """The container half of "indistinguishable from never having been asked"."""
    ck.ck(view.dump["complete"],
          "%s: the --events dump is COMPLETE by header arithmetic: %d lines == "
          "1 + steps(%d) + 2*calls(%d) + io(%d) + reloads(%d) = %d"
          % (tag, view.dump["lines"], view.dump["counts"]["steps"],
             view.dump["counts"]["calls"], view.dump["counts"]["io_events"],
             view.dump["counts"].get("source_reloads", 0),
             view.dump["expected_lines"]))
    ck.eq([p for p, _c in view.sized], view.dump["header"].get("paths", []),
          "%s: this harness's own paths.dat decode equals what ct-print "
          "reports — two independent readers agreeing, not one opinion" % tag)
    ck.eq(len(view.ids), 1,
          "%s: the container carries exactly ONE paths.dat entry for %s"
          % (tag, FIXTURE_PATH))
    recorded = [c for (p, c) in view.sized if p == FIXTURE_PATH]
    ck.eq(recorded, [v1["addressable_lines"]],
          "%s: and its recorded line count is v1's own" % tag)
    fixture_views = [v for v in view.raw_views if v["path_id"] in view.ids]
    ck.eq(len(fixture_views), 1,
          "%s: exactly ONE raw source view is attached to it" % tag)
    ck.eq([hashlib.sha256(v["content"]).hexdigest() for v in fixture_views],
          [v1["sha256"]],
          "%s: and its bytes hash-equal probe_v1.gd as read at run time — the "
          "text the container carries is v1's, not the refused content's" % tag)
    ck.eq(len(view.markers), 0,
          "%s: NO TagSourceReload marker was emitted; a refusal changed "
          "nothing, so there is no boundary to record" % tag)
    fixture_steps = [s for s in view.steps if s["path_id"] in view.ids]
    ck.eq(sorted({s["path_id"] for s in fixture_steps}), view.ids,
          "%s: every step on the fixture path resolves to that one version"
          % tag)


def assert_step_token_bijection(ck: Checker, run: Run, view: View, v1: dict,
                                tag: str) -> None:
    """The trace's probe steps against the program's own output, ORDERED.

    `sorted(a) == sorted(b)` would pass on a trace that permuted two steps, so
    the comparison is element-wise at equal offsets.
    """
    probe_lines = set(v1["probe_lines"])
    decoded = [(s["step_index"], s["line"]) for s in view.steps
               if s["path_id"] in view.ids and s["line"] in probe_lines]
    decoded.sort()
    tokens = run.tokens()
    ck.ck(len(tokens) > 0,
          "%s: the stdout token sequence is NON-EMPTY (an empty comparison is "
          "a pass for free)" % tag)
    ck.eq(len(decoded), len(tokens),
          "%s: the trace carries one probe step per emitted token" % tag)
    trace_seq = [line for _idx, line in decoded]
    token_seq = [line for _it, line in tokens]
    first_bad = next((i for i in range(min(len(trace_seq), len(token_seq)))
                      if trace_seq[i] != token_seq[i]), None)
    ck.ck(first_bad is None and len(trace_seq) == len(token_seq),
          "%s: ORDERED one-to-one match over %d probe steps"
          % (tag, len(token_seq))
          + ("" if first_bad is None
             else " — diverges at offset %d (stdout %r, trace %r)"
                  % (first_bad, token_seq[max(0, first_bad - 2):first_bad + 3],
                     trace_seq[max(0, first_bad - 2):first_bad + 3])))
    after = run.tokens_after_reload()
    ck.ck(len(after) > 0,
          "%s: the POST-REFUSAL token count is non-zero (%d tokens at "
          "iterations past the reload tick) — \"the engine kept running\" is "
          "measured, not assumed" % (tag, len(after)))
    # And the same statement read off the CONTAINER, which is the half that
    # matters: stdout could carry them while the recorder had stopped.
    n_before = len(tokens) - len(after)
    ck.ck(len(decoded) > n_before,
          "%s: and the container carries %d probe steps past the refusal "
          "point, so the RECORDER kept running too"
          % (tag, len(decoded) - n_before))


def assert_disk_holds(ck: Checker, run: Run, want: dict, want_name: str,
                      tag: str) -> None:
    """THE FILE ON DISK, which is the other half of §8.1's ordering change.

    ADDED BY THE GDH-M8 REVIEW, 2026-09-12, because it was missing and its
    absence was not visible from inside any existing claim.  Deviation (a) of
    the audit has two halves: the engine swap moved AFTER trace registration,
    and the DISK WRITE moved with it, out of step 1 and into step 6 — "so a
    refusal no longer deletes v1 from disk".  Every assertion in this file
    reads either the wire, stdout, or the container, and NONE of them can see
    the disk: the raw source view is bundled from disk at the FIRST step, long
    before the reload, so it still holds v1 whatever a later write does; the
    engine goes on running v1 from memory whatever is on disk; and the
    container is unchanged.  A build that restored the pre-GDH-M8 write
    position therefore passed all three gates, MEASURED — the review armed
    exactly that mutation to check.

    So the claim is made directly, against the bytes in the project directory
    the engine was pointed at.
    """
    got = run.script_sha256()
    ck.eq(got, want["sha256"],
          "%s: the file ON DISK (%s) is %s — design §8.1's disk write is part "
          "of step 6, so nothing before step 6 may have touched it"
          % (tag, os.path.basename(run.script_path or "<none>"), want_name))


def assert_no_v2_tokens(ck: Checker, run: Run, v1: dict, v2ok: dict,
                        tag: str) -> None:
    v2_only = {n for n in v2ok["probe_lines"] if n not in v1["probe_lines"]}
    seen = run.lines_seen()
    ck.eq(sorted(seen & v2_only), [],
          "%s: NOT ONE of v2's own probe lines %r appears in stdout — the "
          "engine is still running v1" % (tag, sorted(v2_only)))
    ck.eq(sorted(seen), v1["probe_lines"],
          "%s: and the lines that DID fire are exactly v1's %r"
          % (tag, v1["probe_lines"]))


# ---------------------------------------------------------------------------
# The control arm shared by the first two gates: the same notification,
# applied.  It is what shows the one-entry result is caused by the refusal.
# ---------------------------------------------------------------------------

def assert_applied_control(ck: Checker, run: Run, view: View, v1: dict,
                           v2ok: dict, tag: str) -> None:
    ck.eq(len(run.reload_results), 1,
          "%s CONTROL: the coordinator got exactly one result" % tag)
    res = run.reload_results[0] if run.reload_results else {}
    ck.eq(res.get("outcome"), "applied",
          "%s CONTROL: the very same fixture, correctly delivered, IS APPLIED "
          "— so the refusal above is caused by the defect and not by the "
          "harness" % tag)
    ck.eq(run.rc, 0, "%s CONTROL: the engine exited 0" % tag)
    v2_only = {n for n in v2ok["probe_lines"] if n not in v1["probe_lines"]}
    ck.eq(sorted(run.lines_seen() & v2_only), sorted(v2_only),
          "%s CONTROL: v2's own probe lines DO appear in stdout" % tag)
    ck.ck(view.dump["complete"],
          "%s CONTROL: the dump is COMPLETE (%d == %d)"
          % (tag, view.dump["lines"], view.dump["expected_lines"]))
    ck.eq(len(view.ids), 2,
          "%s CONTROL: the container carries TWO paths.dat entries for the "
          "fixture path" % tag)
    ck.eq([c for (p, c) in view.sized if p == FIXTURE_PATH],
          [v1["addressable_lines"], v2ok["addressable_lines"]],
          "%s CONTROL: each entry states ITS OWN line count" % tag)
    ck.eq(len(view.markers), 1,
          "%s CONTROL: and exactly ONE TagSourceReload marker" % tag)
    fixture_views = [v for v in view.raw_views if v["path_id"] in view.ids]
    ck.eq(sorted(hashlib.sha256(v["content"]).hexdigest()
                 for v in fixture_views),
          sorted([v1["sha256"], v2ok["sha256"]]),
          "%s CONTROL: both versions' raw source views are attached and each "
          "hash-equals its own fixture file" % tag)
    assert_disk_holds(ck, run, v2ok, "probe_v2_ok.gd's bytes — an APPLIED "
                      "reload does write the file, so the refusal claims above "
                      "are measuring the refusal and not an engine that never "
                      "writes at all", tag + " CONTROL")


# ---------------------------------------------------------------------------
# Gate 1 — GDH-G6.
# ---------------------------------------------------------------------------

REFUSED_CLAIMS = 33  # written FROM A RUN (trap 4c), never guessed


def gate_refused(ck: Checker, refused: Run, r_view: View, control: Run,
                 c_view: View, v1: dict, v2ok: dict) -> None:
    assert_refusal(ck, refused, "parse-error", "parse-error")
    assert_disk_holds(ck, refused, v1, "still probe_v1.gd's bytes — the "
                      "refused content was never written, so v1 is live in the "
                      "engine AND on disk", "parse-error")
    ck.ck("[ct-gdh8] reload REFUSED" in refused.stdout
          and "reason=parse-error" in refused.stdout,
          "the RECORDER reports the refusal on its own channel too, so the "
          "wire answer and the host's log agree")
    assert_no_v2_tokens(ck, refused, v1, v2ok, "parse-error")
    assert_single_version_container(ck, r_view, v1, "parse-error")
    assert_step_token_bijection(ck, refused, r_view, v1, "parse-error")
    assert_applied_control(ck, control, c_view, v1, v2ok, "parse-error")


# ---------------------------------------------------------------------------
# Gate 2 — digest mismatch.
# ---------------------------------------------------------------------------

DIGEST_CLAIMS = 34  # written FROM A RUN (trap 4c)


def gate_digest(ck: Checker, refused: Run, r_view: View, control: Run,
                c_view: View, v1: dict, v2ok: dict, supplied_digest: str) -> None:
    # --- anti-vacuity on the NOTIFICATION, before anything else.  A digest
    # field left empty would be refused for the wrong reason
    # (`digest-algorithm-unsupported`) and this gate would pass while testing a
    # different code path.
    ck.ck(bool(supplied_digest) and supplied_digest.startswith("sha256:")
          and len(supplied_digest) == len("sha256:") + 64
          and all(c in "0123456789abcdef" for c in supplied_digest[7:]),
          "the supplied snapshotDigest %r is WELL FORMED and non-empty, so the "
          "refusal cannot be `digest-algorithm-unsupported`" % supplied_digest)
    true_digest = "sha256:" + hashlib.sha256(v2ok["bytes"]).hexdigest()
    ck.ck(supplied_digest != true_digest,
          "and it DIFFERS from the true digest of the content, computed here "
          "by this harness (%s)" % true_digest)
    assert_refusal(ck, refused, "digest-mismatch", "digest-mismatch")
    assert_disk_holds(ck, refused, v1, "still probe_v1.gd's bytes — the agent "
                      "refused before the host handler ran at all", "digest-mismatch")
    assert_no_v2_tokens(ck, refused, v1, v2ok, "digest-mismatch")
    assert_single_version_container(ck, r_view, v1, "digest-mismatch")
    assert_step_token_bijection(ck, refused, r_view, v1, "digest-mismatch")
    assert_applied_control(ck, control, c_view, v1, v2ok, "digest-mismatch")


# ---------------------------------------------------------------------------
# Gate 3 — a failure after registration closes the trace.
# ---------------------------------------------------------------------------

CLOSE_CLAIMS = 22  # written FROM A RUN (trap 4c)


def gate_close(ck: Checker, injected: Run, i_view: View, control: Run,
               c_view: View, v1: dict, v2ok: dict, stage: str) -> None:
    # --- anti-vacuity: THE HOOK ACTUALLY FIRED.  A run in which it did not
    # produces a perfectly coherent trace and would pass for free.
    fired = ('[ct-gdh8] FAULT INJECTED at design §8.1 stage "%s"' % stage) \
        in injected.stdout
    ck.ck(fired,
          "the fault-injection hook FIRED — a run in which it did not reach "
          "the armed stage produces a coherent trace and would pass for free")
    ck.ck("[ct-gdh8] CLOSING THE TRACE at §8.1 stage" in injected.stdout,
          "and the recorder reports CLOSING the trace, which is §8.1's "
          "recovery contract and not a generic error")
    assert_refusal(ck, injected, "trace-closed", "injected",
                   want_outcome="failed")
    # Defensive indexing, not decoration: with a bare `[0]` an arm that
    # produced NO result at all would raise, python would exit 1, and the
    # driver would read that as "the gate went red" — a harness crash wearing a
    # kill's clothes.  `assert_refusal` above has already reddened this case by
    # name.
    first = injected.reload_results[0] if injected.reload_results else {}
    detail = ((first.get("refusedFiles") or [{}])[0].get("detail") or "")
    ck.ck("§8.1 step" in detail and stage in detail.lower(),
          "the recorded reason NAMES THE STAGE it failed at (%r)" % detail[:160])

    # --- THE ENGINE CONTINUES, UNRELOADED — in memory AND on disk.  The close
    # happens at §8.1 step 4, so step 6's write must never have run.
    assert_no_v2_tokens(ck, injected, v1, v2ok, "injected")
    assert_disk_holds(ck, injected, v1, "still probe_v1.gd's bytes — the trace "
                      "closed before §8.1 step 6, so the swap's write never "
                      "happened", "injected")

    # --- THE TRACE IS READABLE: a real decode, not a successful `open`.
    ck.ck(i_view.dump["complete"],
          "the closed trace's FULL step stream decodes: %d dump lines == the "
          "%d its own header declares"
          % (i_view.dump["lines"], i_view.dump["expected_lines"]))
    ck.ck(len(i_view.steps) > 0,
          "and it carries steps (%d) — an empty container decodes for free"
          % len(i_view.steps))
    ck.eq(len(i_view.steps), i_view.dump["counts"]["steps"],
          "every step the header declares was decoded")

    # --- THE COHERENCE PROPERTY.  This is what the falsifier breaks: a run
    # that continued instead of closing attributes its later steps to a version
    # whose source view was never written.
    have_views = {v["path_id"] for v in i_view.raw_views}
    orphans = sorted({s["path_id"] for s in i_view.steps} - have_views)
    ck.eq(orphans, [],
          "NO step is attributed to a path id with no raw source view "
          "(orphaned ids: %r) — the container never holds execution against a "
          "version it has only half registered" % orphans)
    ck.eq(len(i_view.markers), 0,
          "no boundary marker was emitted: the close happened at §8.1 step 4, "
          "before step 5 could record one")

    # --- the reason is IN THE CONTAINER, not only on the session's stderr.
    reason_events = [e for e in i_view.dump["events"]
                     if e["kind"] not in ("step", "source_reload")
                     and "§8.1" in json.dumps(e, ensure_ascii=False)]
    ck.ck(len(reason_events) >= 1,
          "the reason the recording stopped is RECORDED IN THE CONTAINER "
          "(%d event(s) naming §8.1) — a trace that stops for a reason nobody "
          "can recover from it is the defect one layer down"
          % len(reason_events))

    # --- THE CONTROL: the same run with the injection disabled completes
    # normally with two versions registered.
    ck.eq((control.reload_results[0] if control.reload_results else {})
          .get("outcome"), "applied",
          "CONTROL (injection disabled): the same run completes normally")
    ck.eq(len(c_view.ids), 2,
          "CONTROL: with TWO versions registered")
    ck.eq(len(c_view.markers), 1, "CONTROL: and one marker")
    ck.ck("[ct-gdh8] FAULT INJECTED" not in control.stdout,
          "CONTROL: and the hook did NOT fire in it")


# ---------------------------------------------------------------------------
# Main.
# ---------------------------------------------------------------------------

GATES = ["refused", "digest", "close"]
RELOAD_TICK = 8


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", required=True)
    parser.add_argument("--fixtures", required=True)
    parser.add_argument("--work", required=True)
    parser.add_argument("--ct-print", default=None)
    parser.add_argument("--bound", type=float, default=120.0)
    parser.add_argument("--socket-dir", default=None)
    parser.add_argument("--gate", default="all", choices=["all"] + GATES)
    parser.add_argument("--record-only", action="store_true",
                        help="record ONE applied reload and exit 0, printing "
                             "the container path.  Used by the driver's "
                             "inertness gate, which needs two recordings from "
                             "two BUILDS and no assertions at all.")
    parser.add_argument("--inject-stage", default="bundle",
                        choices=["bundle", "marker", "swap"],
                        help="which §8.1 stage the close gate injects at")
    args = parser.parse_args()

    if not os.access(args.engine, os.X_OK):
        die("the engine is not executable: %s" % args.engine)
    ct_print = os.path.abspath(args.ct_print or os.path.join(
        HERE, "..", "..", "codetracer-trace-format-nim", "ct-print"))
    if not os.access(ct_print, os.X_OK):
        die("ct-print is not executable: %s" % ct_print)
    names = ["project.godot", "probe_v1.gd", "probe_v2_ok.gd", "probe_v2_bad.gd"]
    for name in names:
        if not os.path.isfile(os.path.join(args.fixtures, name)):
            die("missing fixture %s in %s" % (name, args.fixtures))
    sock_dir = args.socket_dir or os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    if not os.path.isdir(sock_dir):
        sock_dir = "/tmp"
    os.makedirs(args.work, exist_ok=True)

    print("[gdh8] engine       : %s" % args.engine)
    print("[gdh8] ct-print     : %s" % ct_print)
    print("[gdh8] inject stage : %s" % args.inject_stage)

    v1 = read_fixture(os.path.join(args.fixtures, "probe_v1.gd"))
    v2ok = read_fixture(os.path.join(args.fixtures, "probe_v2_ok.gd"))
    v2bad = read_fixture(os.path.join(args.fixtures, "probe_v2_bad.gd"))

    if args.record_only:
        run = record(args.engine, args.fixtures,
                     os.path.join(args.work, "record-only"),
                     [(RELOAD_TICK, v2ok["bytes"], 2, None)],
                     args.bound, sock_dir)
        if run.failure:
            die("the record-only run did not happen: %s" % run.failure)
        if run.rc != 0:
            die("the record-only engine exited %s" % run.rc)
        if not run.container_path:
            die("the record-only run produced no container")
        # The engine's own report about the recording-id pin, echoed here so
        # the driver's inertness gate can tell a pin that HELD from one that
        # was refused. They produce the same container shape and different
        # bytes, and a refused pin that looked like no pin is exactly the kind
        # of silent degradation this campaign keeps finding.
        for line in run.stdout.splitlines():
            if "recording id" in line:
                print(line.strip())
        print("CONTAINER %s" % run.container_path)
        return 0

    checkers: list[Checker] = []
    pre = Checker("gdh8_fixture_preconditions")
    print("== %s ==" % pre.gate)
    fixture_preconditions(pre, v1, v2ok, v2bad, args.engine, args.work)
    pre.expect_count(7)
    checkers.append(pre)
    pre.report()
    if pre.red:
        print("\n[gdh8] the fixtures cannot support the gates; nothing was "
              "recorded.")
        return 1

    def load(run: Run, tag: str) -> View:
        if run.failure:
            die("the %s run did not happen: %s" % (tag, run.failure))
        if run.rc != 0:
            die("the %s engine exited %s — a dead engine is a CHECK-FAIL, not "
                "a refusal\n%s" % (tag, run.rc, run.stdout[-3000:]))
        if not run.container_path:
            die("the %s run produced no container" % tag)
        return View(run, ct_print, args.work, tag)

    def go(tag: str, content: bytes, digest_override=None, env=None) -> Run:
        return record(args.engine, args.fixtures,
                      os.path.join(args.work, tag),
                      [(RELOAD_TICK, content, 2, digest_override)],
                      args.bound, sock_dir, env)

    # The APPLIED control recording is shared by the first two gates: it is the
    # same notification, correctly delivered, and it is what shows a one-entry
    # container is caused by the refusal rather than by the fixture.
    control = None
    c_view = None
    if args.gate in ("all", "refused", "digest", "close"):
        control = go("control", v2ok["bytes"])
        c_view = load(control, "control")

    if args.gate in ("all", "refused"):
        ck = Checker("gdh8_refused_reload_leaves_a_coherent_trace")
        print("== %s ==" % ck.gate)
        run = go("refused", v2bad["bytes"])
        view = load(run, "refused")
        gate_refused(ck, run, view, control, c_view, v1, v2ok)
        ck.expect_count(REFUSED_CLAIMS)
        checkers.append(ck)
        ck.report()

    if args.gate in ("all", "digest"):
        ck = Checker("gdh8_digest_mismatch_is_refused_before_anything_is_touched")
        print("== %s ==" % ck.gate)
        # A well-formed sha256 that is NOT this content's: v1's own digest.
        # Using a real digest of a real file rather than a random string keeps
        # the notification exactly as plausible as a correct one.
        wrong = "sha256:" + v1["sha256"]
        run = go("digest", v2ok["bytes"], digest_override=wrong)
        view = load(run, "digest")
        gate_digest(ck, run, view, control, c_view, v1, v2ok, wrong)
        ck.expect_count(DIGEST_CLAIMS)
        checkers.append(ck)
        ck.report()

    if args.gate in ("all", "close"):
        ck = Checker("gdh8_a_failure_after_registration_closes_the_trace"
                     "_rather_than_continuing")
        print("== %s ==" % ck.gate)
        run = go("close", v2ok["bytes"],
                 env={"CT_GDH8_INJECT_FAILURE": args.inject_stage})
        view = load(run, "close")
        gate_close(ck, run, view, control, c_view, v1, v2ok, args.inject_stage)
        ck.expect_count(CLOSE_CLAIMS)
        checkers.append(ck)
        ck.report()

    total = sum(c.asserted for c in checkers)
    red = [c for c in checkers if c.red]
    print()
    print("[gdh8] %d assertion(s) over %d gate(s); %d red"
          % (total, len(checkers), len(red)))
    if total == 0:
        die("no check ran at all; a run that asserts nothing is not a pass")
    return 1 if red else 0


if __name__ == "__main__":
    sys.exit(main())
