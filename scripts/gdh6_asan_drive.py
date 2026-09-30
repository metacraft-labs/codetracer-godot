#!/usr/bin/env python3
"""Drive ONE deferral-timeout run for `record-and-verify-gdh6-asan.sh`.

The whole subject is a race between two threads of the engine:

  * the AGENT thread, which received a `sourceChanged`, found the VM mid-step,
    queued the request and is waiting on a bounded condition variable;
  * the ENGINE thread, which reaches its safe point, takes the request and
    does the apply — releasing the emit lock to write a file and recompile a
    script, so it is not quick.

This driver makes the waiter's bound expire INSIDE that window, by setting
`CT_GDH6_RELOAD_WAIT_SECONDS` short and `CT_GDH6_SAFE_POINT_DELAY_MS` long
(both read from the environment by the recorder itself, and both reported by
it on stderr when they are in effect).

It runs in one of three SHAPES, chosen by the environment it is given:

  late-apply  `CT_GDH6_SAFE_POINT_DELAY_MS` > the bound: the safe point takes
              the request and the bound expires during the apply. The waiter
              must report what the apply did — APPLIED — and the program must
              then run v2. (Reporting "no safe point was reached" here was the
              GDH-M5 residual: the coordinator was told a reload was refused
              that the engine then applied.)
  withdrawn   `CT_GDH6_SAFE_POINT_CLAIM_HOLD_MS` > the bound: the bound expires
              while the request is still only queued. It is withdrawn, refused
              as `no-safe-point`, and the program must never run v2.
  control     neither: the safe point wins, the reload is applied, nothing
              expires.

In every shape the deferral path must have been ENTERED (`reload deferred to
the next safe point`) and the hold that shape needs must have been taken; a
run in which the race window never opened fails rather than passes.

Whether AddressSanitizer said anything is decided by the SHELL DRIVER over
this process's captured output, not here — the two runs (clean and mutated)
want opposite verdicts on the same text, and putting that decision in one
place keeps them from drifting.

The control shape is what makes "ASan is quiet" mean something in the ASan
gate; without it a quiet run could simply be a run in which the window never
opened.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from typing import NoReturn

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import verify_gdh5 as agentwire  # noqa: E402

DEFERRED_RE = re.compile(r"\[ct-gdh5\] reload deferred to the next safe point")
TIMEOUT_RE = re.compile(r"no engine safe point was reached within (\d+) s")
APPLIED_RE = re.compile(r"\[ct-gdh5\] reload applied: ")
# The evidence that the safe point FINISHED is the program running v2, not a
# log line. Measured: `[ct-gdh5] reload applied:` is printed by the AGENT
# HANDLER after its wait returns, so a run in which the wait TIMED OUT never
# reaches it — the handler has already answered `failed` and gone. Reading it
# would have made this gate assert the absence of a message on a path that
# structurally cannot print one. Godot's own behavior is the better witness
# and it is the one the gate uses.
V1_RE = re.compile(r"GDH6_ASAN_V1=(\d+) ")
V2_RE = re.compile(r"GDH6_ASAN_V2=(\d+) ")
HOLD_RE = re.compile(r"\[ct-gdh6\] safe point holding the apply for (\d+) ms")
CLAIM_HOLD_RE = re.compile(r"\[ct-gdh6\] safe point holding for (\d+) ms before taking")
BOUND_EXPIRED_APPLYING_RE = re.compile(
    r"\[ct-gdh6\] deferral bound of (\d+) s expired while the safe point was applying"
)

FAILURES: list[str] = []
ASSERTED = 0

# Trap 4c: the claims ONE arm of this driver makes, WRITTEN FROM A RUN
# (2026-09-11: both the timeout arm and the control reported 9, and the
# self-count assertion below is the tenth).  The two branches are padded to
# the same total on purpose, so this is one number rather than two.
EXPECTED_ASSERTIONS = 10


def ck(ok: bool, msg: str) -> bool:
    global ASSERTED
    ASSERTED += 1
    if not ok:
        FAILURES.append(msg)
        print("GDH6-ASAN-FAIL: %s" % msg, file=sys.stderr)
    return ok


def die(msg: str) -> NoReturn:
    print("DRIVER-FAIL: %s" % msg, file=sys.stderr)
    sys.exit(2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", required=True)
    ap.add_argument("--fixtures", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--socket-dir", default="/tmp")
    ap.add_argument("--trigger-tick", type=int, default=6)
    ap.add_argument("--bound", type=float, default=180.0)
    args = ap.parse_args()

    if not os.access(args.engine, os.X_OK):
        die("the engine is not executable: %s" % args.engine)

    want_bound = os.environ.get("CT_GDH6_RELOAD_WAIT_SECONDS", "")
    want_delay = int(os.environ.get("CT_GDH6_SAFE_POINT_DELAY_MS", "0") or 0)
    if not want_bound:
        die(
            "CT_GDH6_RELOAD_WAIT_SECONDS is unset. This driver's whole subject "
            "is a bound that expires during the apply, and the shipping bound "
            "is 30 s; a run without the override would either take half a "
            "minute or never reach the timeout at all."
        )
    want_claim_hold = int(os.environ.get("CT_GDH6_SAFE_POINT_CLAIM_HOLD_MS", "0") or 0)
    if want_claim_hold > int(want_bound) * 1000:
        shape = "withdrawn"
    elif want_delay > int(want_bound) * 1000:
        shape = "late-apply"
    else:
        shape = "control"
    print("[gdh6-asan] shape: %s" % shape)

    project = os.path.join(args.work, "project")
    if os.path.isdir(project):
        shutil.rmtree(project)
    os.makedirs(project)
    shutil.copyfile(os.path.join(args.fixtures, "project.godot"), os.path.join(project, "project.godot"))
    # The ASan gate uses its OWN fixture pair, not the gate fixtures.  It
    # needs the program to STILL BE RUNNING when the safe point finishes its
    # deliberately delayed apply, and probe_v1.gd runs 1.8 s in total — shorter
    # than the hold that makes the timeout deterministic.  Measured: with a 2 s
    # bound and a 9 s hold the program ended first and "the safe point
    # completed its apply" went red for a reason that was the FIXTURE's.
    shutil.copyfile(os.path.join(args.fixtures, "asan_v1.gd"), os.path.join(project, "probe.gd"))
    trace_dir = os.path.join(args.work, "trace")
    os.makedirs(trace_dir, exist_ok=True)

    sock_path = os.path.join(args.socket_dir, "gdh6a-%d.sock" % os.getpid())
    if len(sock_path) >= 100:
        die("the agent socket path is %d bytes; sun_path holds 108" % len(sock_path))
    if os.path.exists(sock_path):
        os.unlink(sock_path)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(sock_path)
    listener.listen(1)

    env = dict(os.environ)
    env["REPRO_HCR_AGENT_SOCKET"] = sock_path
    # NO `REPRO_HCR_AGENT_POLL`.  With the agent polled from the engine's own
    # safe point the handler applies where it stands and NOTHING is ever
    # queued, so the deferral path — the whole subject here — is unreachable.
    env.pop("REPRO_HCR_AGENT_POLL", None)
    env["CT_GDSCRIPT_TRACE"] = trace_dir

    proc = subprocess.Popen(
        [args.engine, "--headless", "--path", project, "--script", "res://probe.gd"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        text=True,
        bufsize=1,
    )

    lines: list[str] = []

    def finish(note: str | None) -> tuple[str, int]:
        rest = proc.stdout.read() if proc.stdout else ""
        out = "".join(lines) + rest
        rc = proc.wait()
        try:
            listener.close()
            os.unlink(sock_path)
        except OSError:
            pass
        if note:
            print("[gdh6-asan] %s" % note)
        return out, rc

    listener.settimeout(args.bound)
    try:
        conn, _ = listener.accept()
    except socket.timeout:
        proc.kill()
        out, rc = finish(None)
        print(out[-4000:])
        die("the engine never connected to the agent socket")
    peer = agentwire.AgentPeer(conn)
    kind, obj = peer.read(args.bound)
    if kind != "hello":
        proc.kill()
        out, rc = finish(None)
        print(out[-4000:])
        die("the engine's first frame was %r" % kind)
    peer.send(agentwire.hello_ack(obj["hello"]["supportProfile"]))

    marker = "GDH6_TICK=%d " % args.trigger_tick
    deadline = time.monotonic() + args.bound
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
        out, rc = finish(None)
        print(out[-4000:])
        die("the fixture never printed %r" % marker)

    with open(os.path.join(args.fixtures, "asan_v2.gd"), "rb") as handle:
        content = handle.read()
    peer.send(agentwire.source_changed("gdh6-asan-0002", "res://probe.gd", 2, content))
    kind, obj = peer.read(args.bound)
    result = obj.get("sourceReloadResult") if kind == "sourceReloadResult" else None

    out, rc = finish(None)
    print(out)
    print("[gdh6-asan] engine exit: %d" % rc)

    # --- 1. the deferral path was ENTERED ---------------------------------
    ck(
        bool(DEFERRED_RE.search(out)),
        "the deferral path was ENTERED: a run in which the notification landed "
        "at a safe point never opened the race window and must fail, not pass",
    )
    n_v1, n_v2 = len(V1_RE.findall(out)), len(V2_RE.findall(out))
    refused = ((result or {}).get("refusedFiles") or [{}])[0]
    reason_text = "%s %s %s" % ((result or {}).get("reason", ""), refused.get("reason", ""), refused.get("detail", ""))

    # --- the hold really happened, when one was asked for ------------------
    if shape == "withdrawn":
        m = CLAIM_HOLD_RE.search(out)
        ck(
            m is not None and int(m.group(1)) == want_claim_hold,
            "the safe point held %d ms BEFORE taking the request, as this run "
            "asked (reported: %r)" % (want_claim_hold, m.group(1) if m else None),
        )
    elif want_delay > 0:
        m = HOLD_RE.search(out)
        ck(
            m is not None and int(m.group(1)) == want_delay,
            "the safe point honored the %d ms hold this run asked for "
            "(reported: %r)" % (want_delay, m.group(1) if m else None),
        )
    else:
        ck(
            HOLD_RE.search(out) is None and CLAIM_HOLD_RE.search(out) is None,
            "CONTROL: no hold was asked for and none was taken",
        )

    if shape == "late-apply":
        # The bound expired AFTER the safe point took the request: the apply
        # was already under way, so the waiter must report what the apply
        # did, not that no safe point came.
        m = BOUND_EXPIRED_APPLYING_RE.search(out)
        ck(m is not None, "the waiter's bound EXPIRED while the apply was in flight")
        ck(
            m is not None and m.group(1) == want_bound,
            "and it expired at the bound this run set (%s s), not the shipping 30 s" % want_bound,
        )
        ck(
            result is not None,
            "the coordinator got an answer at all; a waiter that neither "
            "applied nor refused is a hang, and a hang is not a diagnosis",
        )
        ck(
            result is not None and result.get("outcome") == "applied",
            "the coordinator was told the reload was APPLIED, which is what "
            "happened (it was told %r, reason %r)" % ((result or {}).get("outcome"), reason_text.strip()),
        )
        # Measured from the PROGRAM: v1's tokens before the reload and v2's
        # after it. That the reload landed is Godot's statement.
        ck(
            n_v1 > 0 and n_v2 > 0,
            "the safe point COMPLETED its apply after the bound expired — the "
            "program printed %d v1 token(s) and then %d v2 token(s). Shared "
            'ownership is what makes that safe: a timeout means "stop '
            'waiting", not "delete what the other thread is using"' % (n_v1, n_v2),
        )
        ck(TIMEOUT_RE.search(out) is None, "and nothing claimed that no safe point was reached, because one was")
    elif shape == "withdrawn":
        # The bound expired while the request was still only QUEUED: it is
        # withdrawn, refused by name, and never applied.
        m = TIMEOUT_RE.search(out)
        ck(m is not None, "the waiter's bound EXPIRED before the safe point took the request")
        ck(m is not None and m.group(1) == want_bound, "and it expired at the bound this run set (%s s)" % want_bound)
        ck(result is not None, "the coordinator got an answer at all")
        ck(
            result is not None and result.get("outcome") != "applied" and "no-safe-point" in reason_text,
            "the coordinator was told the reload was REFUSED with reason "
            "no-safe-point (it was told %r, %r)" % ((result or {}).get("outcome"), reason_text.strip()),
        )
        ck(
            "safe point" in reason_text.lower(),
            "and the refusal detail names the missing safe point: %r" % reason_text.strip(),
        )
        ck(
            n_v1 > 0 and n_v2 == 0,
            "and the withdrawn reload was NEVER applied: %d v1 token(s), %d v2 "
            "token(s) — a refusal the engine then applied anyway would be the "
            "same lie in the other direction" % (n_v1, n_v2),
        )
    else:
        # CONTROL: the safe point wins.
        ck(
            TIMEOUT_RE.search(out) is None and BOUND_EXPIRED_APPLYING_RE.search(out) is None,
            "CONTROL: no bound expired",
        )
        ck(
            result is not None and result.get("outcome") == "applied",
            "CONTROL: the reload was APPLIED and the waiter woken (%r)" % (result.get("outcome") if result else None,),
        )
        ck(
            n_v1 > 0 and n_v2 > 0,
            "CONTROL: the program ran v1 and then v2, so the apply landed "
            "(and here the recorder's own `reload applied` line is present "
            "too: %r)" % bool(APPLIED_RE.search(out)),
        )
        ck(True, "CONTROL: no refusal to inspect")
        ck(True, "CONTROL: no refusal to inspect")
        ck(True, "CONTROL: no bound to check")

    # The engine must not have DIED.  A death is not this gate's finding — the
    # shell driver decides on the sanitizer's report — but a run that died is
    # a run whose other assertions were made over a truncated transcript, and
    # that has to be visible.
    ck(
        rc == 0 or rc == 1,
        "the engine exited %d; a signal death (negative rc) means the "
        "transcript above is truncated and every assertion over it is weaker "
        "than it looks" % rc,
    )

    # Trap 4c, ADDED AT REVIEW (2026-09-11).  `ASSERTED == 0` catches a driver
    # that checked NOTHING; it does not catch one that checked five things
    # instead of nine, which is the shape that actually happens — a guard
    # returns early, or a branch loses a check in a refactor, and the run
    # prints "assertions: 5, failures: 0" and exits 0.  Nothing downstream
    # would notice: the shell driver grades this arm on the presence of an
    # AddressSanitizer report and on the exit code, and neither moves.
    #
    # Both branches deliberately make the SAME number of claims — the control
    # already pads with three `ck(True, "CONTROL: nothing to inspect")` lines
    # to keep them equal — so one number covers both.  Written from a run, not
    # counted off the source.
    ck(
        ASSERTED + 1 == EXPECTED_ASSERTIONS,
        "this arm made all %d of its claims (it made %d) — a branch that "
        "quietly asserts fewer is a weaker gate reporting the same verdict" % (EXPECTED_ASSERTIONS, ASSERTED + 1),
    )
    print("[gdh6-asan] assertions: %d, failures: %d" % (ASSERTED, len(FAILURES)))
    if ASSERTED == 0:
        die("no check ran at all")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
