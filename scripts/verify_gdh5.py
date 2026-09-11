#!/usr/bin/env python3
"""GDH-M5 verifier — the engine reloads, from a point the recorder controls.

Design:    codetracer-specs/Planned-Features/
           GDScript-Hot-Reload-Multi-Version-Sources.md §5.2, §5.3, §5.6.
Milestone: the `GDH-M5` block of the campaign's `.milestones.org`.

Two gates live here.

`gdh5_in_process_reload_matches_the_remote_debugger_path`
    The same fixture is reloaded two ways — in-process from the recorder's poll
    point via a `sourceChanged` notification on the agent socket, and with
    `core:reload_scripts` over `--remote-debug` — and the two must agree about
    which scripts were reloaded and about what the program printed afterwards.
    **Godot's own supported path is the oracle**, which is the whole reason the
    remote-debugger arm is run at all rather than asserted about.

`gdh5_unpreserved_state_is_reported`
    After reloading a script with a static variable, the `sourceReloadResult`
    NAMES the static-variable loss, and the loss is independently confirmed
    from GDScript itself (the fixture prints the static's value every tick).

`allowed_mocks: none`. Both arms run the real `template_debug` engine on a real
project directory. The agent side speaks the real Content-Length wire; the
remote-debug side speaks Godot's real `encode_variant` protocol. The digest
this driver sends is computed with Python's `hashlib`, which is a completely
separate implementation from the agent's `repro_hcr_sha256.h` — so a digest the
engine accepts is a digest two independent implementations agree on.

Harness rules (codetracer-specs/Testing/Verification-Harness-Traps.md):

  * Every socket wait is BOUNDED and its expiry is a named verdict. A missing
    reply is reported as "no reply" with the transcript, never waited out.
  * A run that produced no tokens, or no reloaded-script set, is a CHECK-FAIL
    and never a pass: two runs that each reloaded nothing agree perfectly
    (trap 4).
  * The set comparisons assert the sets are NON-EMPTY before asserting they are
    equal.
"""

from __future__ import annotations

import argparse
import base64
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

# ---------------------------------------------------------------------------
# Verdict plumbing. A gate failure and a harness failure are different things
# and are never allowed to look alike.
# ---------------------------------------------------------------------------

FAILURES: list[str] = []
CHECKS = 0


def die(message: str) -> "NoReturn":  # type: ignore[name-defined]
    print("DRIVER-FAIL: %s" % message, file=sys.stderr)
    sys.exit(2)


def gate_fail(gate: str, message: str) -> None:
    FAILURES.append("GDH5-FAIL[%s]: %s" % (gate, message))
    print("GDH5-FAIL[%s]: %s" % (gate, message), file=sys.stderr)


def check(gate: str, condition: bool, message: str) -> bool:
    global CHECKS
    CHECKS += 1
    if not condition:
        gate_fail(gate, message)
    return condition


# ---------------------------------------------------------------------------
# The HCR agent wire, coordinator half. Content-Length framing + JSON over
# AF_UNIX (`repro_hcr_agent.c`).
# ---------------------------------------------------------------------------

AGENT_SCHEMA = "reprobuild.hcr.agent-protocol.message.v1"
AGENT_SCOPE = "hcr-agent-protocol"


def frame(obj: dict) -> bytes:
    body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    return b"Content-Length: %d\r\n\r\n" % len(body) + body


class AgentPeer:
    """One accepted agent connection, with every wait bounded."""

    def __init__(self, conn: socket.socket) -> None:
        self.conn = conn
        self.buf = b""
        self.transcript: list[str] = []

    def note(self, direction: str, summary: str) -> None:
        self.transcript.append("%s %s" % (direction, summary))

    def dump(self) -> None:
        print("  --- agent socket transcript (%d frames, both directions) ---"
              % len(self.transcript), file=sys.stderr)
        if not self.transcript:
            print("  (empty: not one frame crossed the socket in either "
                  "direction, so the handshake itself did not happen)",
                  file=sys.stderr)
        for line in self.transcript:
            print("  " + line, file=sys.stderr)

    def send(self, obj: dict) -> None:
        self.conn.sendall(frame(obj))
        self.note("coordinator -> engine", obj.get("kind", "?"))

    def read(self, bound_s: float):
        """Returns (kind, obj) or ('TIMEOUT'|'CLOSED', detail)."""
        deadline = time.monotonic() + bound_s
        while True:
            split = self.buf.find(b"\r\n\r\n")
            if split >= 0:
                header = self.buf[:split].decode("ascii", "replace")
                m = re.search(r"(?i)content-length:\s*(\d+)", header)
                if not m:
                    return "CLOSED", "frame without a Content-Length header"
                length = int(m.group(1))
                if len(self.buf) >= split + 4 + length:
                    body = self.buf[split + 4:split + 4 + length]
                    self.buf = self.buf[split + 4 + length:]
                    obj = json.loads(body.decode("utf-8"))
                    self.note("engine -> coordinator", obj.get("kind", "?"))
                    return obj.get("kind", "?"), obj
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "TIMEOUT", ("no frame became readable within %.1f s; "
                                   "the engine is alive and silent" % bound_s)
            self.conn.settimeout(remaining)
            try:
                chunk = self.conn.recv(65536)
            except socket.timeout:
                return "TIMEOUT", ("no frame became readable within %.1f s"
                                   % bound_s)
            except OSError as err:
                return "CLOSED", "the engine's socket errored: %s" % err
            if not chunk:
                return "CLOSED", "the engine closed the connection"
            self.buf += chunk


def hello_ack(profile: str) -> dict:
    return {
        "schemaId": AGENT_SCHEMA,
        "transportScope": AGENT_SCOPE,
        "protocolVersion": 1,
        "messageId": "coordinator-hello-ack-1",
        "kind": "helloAck",
        "hello": {"supportProfile": profile, "agentPid": 0,
                  "capabilities": ["hcr-agent-protocol"]},
    }


def line_start_offsets(content: bytes) -> list[int]:
    if not content:
        return []
    offsets = [0]
    for i, byte in enumerate(content):
        if byte == 0x0A and i + 1 < len(content):
            offsets.append(i + 1)
    return offsets


def source_changed(reload_id: str, path: str, generation: int,
                   content: bytes) -> dict:
    offsets = line_start_offsets(content)
    table = ",".join(str(o) for o in offsets).encode("utf-8")
    return {
        "schemaId": AGENT_SCHEMA,
        "transportScope": AGENT_SCOPE,
        "protocolVersion": 2,
        "messageId": "coordinator-source-changed-" + reload_id,
        "kind": "sourceChanged",
        "sourceChanged": {
            "reloadId": reload_id,
            "language": "gdscript",
            "changedFiles": [{
                "sourcePath": path,
                "generation": generation,
                # hashlib, deliberately: a completely separate implementation
                # from the agent's own SHA-256.
                "snapshotDigest": "sha256:" + hashlib.sha256(content).hexdigest(),
                "lineTableDigest": "sha256:" + hashlib.sha256(table).hexdigest(),
                "lineCount": len(offsets),
                "contentEncoding": "inline",
                "content": base64.b64encode(content).decode("ascii"),
            }],
        },
    }


# ---------------------------------------------------------------------------
# Godot remote-debugger wire, host half.
#
# The engine CONNECTS OUT to `--remote-debug tcp://host:port` and speaks
# `u32 LE length` + `encode_variant(Array)` (`remote_debugger_peer.cpp:98-155`).
# A host->engine command is a THREE-element array `[String cmd, int thread_id,
# Array data]` — `remote_debugger.cpp:350-371`, `ERR_CONTINUE(cmd.size() != 3)`.
# A two-element array is dropped, and an UNREGISTERED thread_id is dropped with
# no diagnostic at all; `Thread::MAIN_ID` is 1 and is registered
# unconditionally.
# ---------------------------------------------------------------------------

_VT_BOOL, _VT_INT, _VT_STRING, _VT_ARRAY = 1, 2, 4, 28
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


# ---------------------------------------------------------------------------
# Running one arm.
# ---------------------------------------------------------------------------

TICK_RE = re.compile(r"GDH5_TICK=(\d+) ")
VERSION_RE = re.compile(r"GDH5_(V\d)_A tick=(\d+)")
STATIC_RE = re.compile(r"GDH5_STATIC=(-?\d+) ")


def prepare_project(fixtures: str, work: str, v1_name: str) -> str:
    project = os.path.join(work, "project")
    if os.path.isdir(project):
        shutil.rmtree(project)
    os.makedirs(project)
    shutil.copyfile(os.path.join(fixtures, "project.godot"),
                    os.path.join(project, "project.godot"))
    shutil.copyfile(os.path.join(fixtures, v1_name),
                    os.path.join(project, "probe.gd"))
    return project


class ArmResult:
    def __init__(self) -> None:
        self.rc: int | None = None
        self.stdout = ""
        self.reload_sent = False
        self.reload_result: dict | None = None
        self.transcript: list[str] = []
        self.failure: str | None = None

    @property
    def tokens(self) -> list[str]:
        return [line.strip() for line in self.stdout.splitlines()
                if line.startswith("GDH5_")]

    def version_ticks(self) -> list[tuple[str, int]]:
        return [(m.group(1), int(m.group(2)))
                for m in VERSION_RE.finditer(self.stdout)]

    def post_reload_tokens(self, at_tick: int) -> list[str]:
        """Version tokens strictly after `at_tick` — the half the reload owns."""
        return ["%s@%d" % (v, t) for v, t in self.version_ticks() if t > at_tick]

    def statics(self) -> list[int]:
        return [int(m.group(1)) for m in STATIC_RE.finditer(self.stdout)]


def run_inproc_arm(engine: str, fixtures: str, work: str, v1: str, v2: str,
                   trigger_tick: int, bound_s: float,
                   sock_dir: str) -> ArmResult:
    """Reload from the recorder's own poll point, over the agent socket."""
    result = ArmResult()
    project = prepare_project(fixtures, work, v1)
    trace_dir = os.path.join(work, "trace")
    os.makedirs(trace_dir, exist_ok=True)

    sock_path = os.path.join(sock_dir, "gdh5-%d.sock" % os.getpid())
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
    argv = [engine, "--headless", "--path", project, "--script", "res://probe.gd"]
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=env, text=True,
                            bufsize=1)

    listener.settimeout(bound_s)
    try:
        conn, _ = listener.accept()
    except socket.timeout:
        proc.kill()
        result.failure = ("the engine never connected to the agent socket "
                          "within %.1f s" % bound_s)
        result.stdout = proc.stdout.read() if proc.stdout else ""
        result.rc = proc.wait()
        return result
    peer = AgentPeer(conn)

    kind, obj = peer.read(bound_s)
    if kind != "hello":
        proc.kill()
        result.failure = "the engine's first frame was %r (%s)" % (kind, obj)
        result.transcript = peer.transcript
        result.stdout = proc.stdout.read() if proc.stdout else ""
        result.rc = proc.wait()
        return result
    caps = obj["hello"]["capabilities"]
    if "source-reload" not in caps:
        proc.kill()
        result.failure = ("the engine's hello does not advertise "
                          "source-reload; it advertised %r" % (caps,))
        result.transcript = peer.transcript
        result.stdout = proc.stdout.read() if proc.stdout else ""
        result.rc = proc.wait()
        return result
    peer.send(hello_ack(obj["hello"]["supportProfile"]))

    # Wait for the fixture to reach the trigger tick, reading stdout as it goes.
    lines: list[str] = []
    deadline = time.monotonic() + bound_s
    reached = False
    marker = "GDH5_TICK=%d " % trigger_tick
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
        result.failure = ("the fixture never printed %r; the reload window was "
                          "never entered" % marker)
        result.transcript = peer.transcript
        result.stdout = "".join(lines) + (proc.stdout.read() if proc.stdout else "")
        result.rc = proc.wait()
        return result

    with open(os.path.join(fixtures, v2), "rb") as handle:
        new_content = handle.read()
    peer.send(source_changed("gdh5-r-0002", "res://probe.gd", 2, new_content))
    result.reload_sent = True
    kind, obj = peer.read(bound_s)
    if kind == "sourceReloadResult":
        result.reload_result = obj["sourceReloadResult"]
    else:
        result.failure = "no sourceReloadResult: %s (%s)" % (kind, obj)

    rest = proc.stdout.read() if proc.stdout else ""
    result.stdout = "".join(lines) + rest
    result.rc = proc.wait()
    result.transcript = peer.transcript
    conn.close()
    listener.close()
    try:
        os.unlink(sock_path)
    except OSError:
        pass
    return result


def run_remote_arm(engine: str, fixtures: str, work: str, v1: str, v2: str,
                   trigger_tick: int, bound_s: float) -> ArmResult:
    """Reload with Godot's own `core:reload_scripts` over `--remote-debug`."""
    result = ArmResult()
    project = prepare_project(fixtures, work, v1)
    trace_dir = os.path.join(work, "trace")
    os.makedirs(trace_dir, exist_ok=True)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    env = dict(os.environ)
    env["CT_GDSCRIPT_TRACE"] = trace_dir
    env.pop("REPRO_HCR_AGENT_SOCKET", None)
    env.pop("REPRO_HCR_AGENT_POLL", None)
    argv = [engine, "--headless", "--path", project, "--script", "res://probe.gd",
            "--remote-debug", "tcp://127.0.0.1:%d" % port]
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, env=env, text=True,
                            bufsize=1)

    listener.settimeout(bound_s)
    try:
        conn, _ = listener.accept()
    except socket.timeout:
        proc.kill()
        result.failure = ("the engine never connected to the remote-debug port "
                          "within %.1f s" % bound_s)
        result.stdout = proc.stdout.read() if proc.stdout else ""
        result.rc = proc.wait()
        return result

    lines: list[str] = []
    deadline = time.monotonic() + bound_s
    reached = False
    marker = "GDH5_TICK=%d " % trigger_tick
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
        result.failure = ("the fixture never printed %r; the reload window was "
                          "never entered" % marker)
        result.stdout = "".join(lines) + (proc.stdout.read() if proc.stdout else "")
        result.rc = proc.wait()
        return result

    # The external overwrite the remote-debugger path requires: it re-reads the
    # `.gd` from disk (`gdscript.cpp:2509`) and has no way to be handed bytes.
    incoming = os.path.join(project, "probe.gd.incoming")
    shutil.copyfile(os.path.join(fixtures, v2), incoming)
    os.replace(incoming, os.path.join(project, "probe.gd"))
    with open(os.path.join(project, "probe.gd"), "rb") as handle:
        landed = hashlib.sha256(handle.read()).hexdigest()
    with open(os.path.join(fixtures, v2), "rb") as handle:
        expected = hashlib.sha256(handle.read()).hexdigest()
    if landed != expected:
        proc.kill()
        result.failure = "the external overwrite did not land"
        result.stdout = "".join(lines)
        result.rc = proc.wait()
        return result

    conn.sendall(encode_message(
        ["core:reload_scripts", GODOT_MAIN_THREAD_ID, ["res://probe.gd"]]))
    result.reload_sent = True

    rest = proc.stdout.read() if proc.stdout else ""
    result.stdout = "".join(lines) + rest
    result.rc = proc.wait()
    conn.close()
    listener.close()
    return result


# ---------------------------------------------------------------------------
# Gates.
# ---------------------------------------------------------------------------

def gate_matches_remote(engine: str, fixtures: str, work: str, trigger: int,
                        bound: float, sock_dir: str) -> None:
    gate = "gdh5_in_process_reload_matches_the_remote_debugger_path"
    print("== %s ==" % gate)

    inproc = run_inproc_arm(engine, fixtures, os.path.join(work, "inproc"),
                            "probe_v1.gd", "probe_v2.gd", trigger, bound,
                            sock_dir)
    remote = run_remote_arm(engine, fixtures, os.path.join(work, "remote"),
                            "probe_v1.gd", "probe_v2.gd", trigger, bound)

    # ---- control arm, stated first: a run that did not happen is never
    # compared against. Two runs that each reloaded nothing agree perfectly.
    for name, arm in (("in-process", inproc), ("remote-debugger", remote)):
        if arm.failure:
            gate_fail(gate, "the %s arm did not produce a run: %s"
                      % (name, arm.failure))
            for line in arm.transcript:
                print("    " + line, file=sys.stderr)
            return
        if not check(gate, arm.rc == 0,
                     "the %s engine exited %s, expected 0" % (name, arm.rc)):
            print(arm.stdout[-2000:], file=sys.stderr)
            return
        if not check(gate, len(arm.tokens) > 0,
                     "the %s arm produced NO tokens at all; there is nothing "
                     "to compare and an empty comparison would pass for free"
                     % name):
            return
        if not check(gate, arm.reload_sent,
                     "the %s arm never delivered its reload" % name):
            return

    inproc_post = inproc.post_reload_tokens(trigger)
    remote_post = remote.post_reload_tokens(trigger)

    # ---- anti-vacuity: BOTH post-reload token streams must be non-empty
    # before they are compared.
    if not check(gate, len(inproc_post) > 0,
                 "the in-process arm printed NO version token after tick %d; "
                 "the run ended before the reload could show" % trigger):
        return
    if not check(gate, len(remote_post) > 0,
                 "the remote-debugger arm printed NO version token after tick "
                 "%d" % trigger):
        return

    inproc_versions = sorted({v.split("@")[0] for v in inproc_post})
    remote_versions = sorted({v.split("@")[0] for v in remote_post})
    check(gate, len(remote_versions) > 0,
          "the oracle arm's reloaded-version set is empty")
    check(gate, inproc_versions == remote_versions,
          "the two paths disagree about what ran after the reload: "
          "in-process %r vs remote-debugger %r"
          % (inproc_versions, remote_versions))
    check(gate, "V2" in remote_versions,
          "the ORACLE never reached v2, so this comparison cannot show that "
          "the in-process path reloads: %r" % (remote_versions,))
    check(gate, "V2" in inproc_versions,
          "the in-process path never reached v2: %r" % (inproc_versions,))

    # The acknowledgement must name the same file, at generation 2.
    res = inproc.reload_result
    if check(gate, res is not None, "no sourceReloadResult arrived"):
        check(gate, res.get("outcome") == "applied",
              "the in-process reload was not applied: %r" % (res,))
        applied = res.get("appliedFiles") or []
        if check(gate, len(applied) == 1,
                 "expected exactly one applied file, got %d" % len(applied)):
            check(gate, applied[0]["generation"] == 2,
                  "the acknowledgement carries generation %r, expected 2"
                  % applied[0]["generation"])
            with open(os.path.join(fixtures, "probe_v2.gd"), "rb") as handle:
                want = "sha256:" + hashlib.sha256(handle.read()).hexdigest()
            check(gate, applied[0].get("appliedDigest") == want,
                  "the engine recomputed %r over the bytes it applied; the "
                  "bytes sent hash to %r"
                  % (applied[0].get("appliedDigest"), want))

    print("  in-process post-reload versions : %r" % (inproc_versions,))
    print("  remote-debug post-reload versions: %r (the oracle)"
          % (remote_versions,))
    print("  in-process tokens: %d, remote tokens: %d"
          % (len(inproc.tokens), len(remote.tokens)))


def gate_unpreserved(engine: str, fixtures: str, work: str, trigger: int,
                     bound: float, sock_dir: str) -> None:
    gate = "gdh5_unpreserved_state_is_reported"
    print("== %s ==" % gate)

    statics = run_inproc_arm(engine, fixtures,
                             os.path.join(work, "statics"),
                             "probe_v1.gd", "probe_v2.gd", trigger, bound,
                             sock_dir)
    control = run_inproc_arm(engine, fixtures,
                             os.path.join(work, "nostatics"),
                             "probe_nostatic_v1.gd", "probe_nostatic_v2.gd",
                             trigger, bound, sock_dir)

    for name, arm in (("statics", statics), ("no-statics control", control)):
        if arm.failure:
            gate_fail(gate, "the %s arm did not produce a run: %s"
                      % (name, arm.failure))
            for line in arm.transcript:
                print("    " + line, file=sys.stderr)
            return
        if not check(gate, arm.rc == 0,
                     "the %s engine exited %s" % (name, arm.rc)):
            print(arm.stdout[-2000:], file=sys.stderr)
            return

    # ---- anti-vacuity: the static's PRE-reload value must be the non-default
    # one the fixture set, so "it is now the default" is evidence of a reset
    # rather than of the fixture never having moved it.
    seen = statics.statics()
    if not check(gate, len(seen) > 0,
                 "the statics fixture printed no GDH5_STATIC= value at all"):
        return
    pre = [v for v in seen[:trigger]]
    if not check(gate, len(pre) > 0 and all(v == 4242 for v in pre),
                 "the static's PRE-reload values are %r, expected every one to "
                 "be the fixture's non-default 4242; without that, \"it is now "
                 "the default\" would not be evidence of a reset" % (pre,)):
        return

    res = statics.reload_result
    if not check(gate, res is not None and res.get("outcome") == "applied",
                 "the statics arm's reload was not applied: %r" % (res,)):
        return
    applied = (res.get("appliedFiles") or [{}])[0]
    unpreserved = applied.get("unpreservedState") or []
    lost = [u for u in unpreserved if u.startswith("static-variable-")]
    check(gate, len(lost) > 0,
          "the acknowledgement named NO static-variable loss; it reported %r"
          % (unpreserved,))
    check(gate, any("counter" in u for u in lost),
          "the reported losses %r do not name `counter`" % (lost,))

    # ---- the loss is real, confirmed from GDScript itself.
    post = seen[trigger:]
    check(gate, len(post) > 0,
          "no GDH5_STATIC= value was printed after the reload, so the report "
          "cannot be confirmed against the program")
    if post:
        check(gate, any(v != 4242 for v in post),
              "the static still reads 4242 after the reload (%r), so the "
              "reported loss is decorative" % (post[:5],))

    # ---- control: a fixture with NO static variables must report no loss.
    cres = control.reload_result
    if check(gate, cres is not None and cres.get("outcome") == "applied",
             "the no-statics control's reload was not applied: %r" % (cres,)):
        capplied = (cres.get("appliedFiles") or [{}])[0]
        cunpreserved = capplied.get("unpreservedState") or []
        clost = [u for u in cunpreserved if u.startswith("static-variable-")]
        check(gate, len(clost) == 0,
              "the no-statics CONTROL reported a static-variable loss %r; an "
              "unconditional literal in a status field is the "
              "`oldCodeRetained: true` defect" % (clost,))

    print("  statics arm reported: %r" % (unpreserved,))
    print("  pre-reload static values: %r" % (pre[:4],))
    print("  post-reload static values: %r" % (post[:4],))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", required=True)
    parser.add_argument("--fixtures", required=True)
    parser.add_argument("--work", required=True)
    parser.add_argument("--trigger-tick", type=int, default=8)
    parser.add_argument("--bound", type=float, default=60.0)
    parser.add_argument("--socket-dir", default=None)
    parser.add_argument("--gate", default="all",
                        choices=["all", "matches-remote", "unpreserved"])
    args = parser.parse_args()

    if not os.access(args.engine, os.X_OK):
        die("the engine is not executable: %s" % args.engine)
    for name in ("project.godot", "probe_v1.gd", "probe_v2.gd",
                 "probe_nostatic_v1.gd", "probe_nostatic_v2.gd"):
        if not os.path.isfile(os.path.join(args.fixtures, name)):
            die("missing fixture %s in %s" % (name, args.fixtures))

    sock_dir = args.socket_dir or os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    if not os.path.isdir(sock_dir):
        sock_dir = "/tmp"
    os.makedirs(args.work, exist_ok=True)

    if args.gate in ("all", "matches-remote"):
        gate_matches_remote(args.engine, args.fixtures, args.work,
                            args.trigger_tick, args.bound, sock_dir)
    if args.gate in ("all", "unpreserved"):
        gate_unpreserved(args.engine, args.fixtures, args.work,
                         args.trigger_tick, args.bound, sock_dir)

    print()
    print("checks run: %d, failures: %d" % (CHECKS, len(FAILURES)))
    if CHECKS == 0:
        die("no check ran at all; a run that asserts nothing is not a pass")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
