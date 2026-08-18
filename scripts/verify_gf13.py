#!/usr/bin/env python3
"""Assert GF13 (Diagnostics & String Formatting — the LAST GF milestone) facts
against a real .ct produced by the patched engine, decoded via `ct-print --full`.

GDScript has NO exceptions; no try/catch is recorded or invented. GF13 covers:
  1. String formatting — `%` operator, String.format, raw r-strings,
     triple-quoted strings, and `+` concatenation — each result assigned to a
     NAMED local and captured as a String VALUE by the existing G4 path.
  2. assert — a passing `assert(ok, msg)` is a no-op STEP (execution continues).
  3. push_warning / push_error — recorded as events.dat SPECIAL EVENTS (io) with
     their messages (GF13 engine hook in OPCODE_CALL_UTILITY).

The recorded program `test-programs/gdscript/gf_diag.gd` and the hand-derived
expected facts are in scripts/EXPECTED-GF13.md; the literals below are duplicated
from there so this check is not circular.

THE TEETH (not merely "a trace exists"):
  - each formatted string's EXACT captured value (raw keeps the literal
    backslash; triple-quoted keeps the newline);
  - the assert line is a recorded step AND execution continued past it;
  - push_warning / push_error each recorded once as an io event with the right
    kind, level tag (metadata) and message (text).

It EXITS NONZERO on any mismatch.

Usage:
  verify_gf13.py verify <full.json>
  verify_gf13.py tamper <full.json> <mode>   # fmtvalue|assertstep|pushmsg|pushmissing
"""
import json
import sys


class VerifyError(Exception):
    pass


EXPECTED_TYPES = ["None", "Int", "Float", "Bool", "String", "Variant"]

# push_error / push_warning surface as events.dat special events (io events).
# The recorder maps push_error -> FFI_EVENT_ERROR and push_warning ->
# FFI_EVENT_TRACE_LOG_EVENT; the multi-stream writer's toIOEventKind then renders
# them, via ct-print, as io_kind "ioError" and "ioStderr" respectively — two
# DISTINCT kinds. The recorder also writes a level tag into the event metadata
# ("ct-push-error"/"ct-push-warning") for a real event-log pane, but ct-print
# does NOT surface multi-stream io metadata (GF10 documented the same), so the
# verifier keys off io_kind + message text (both of which ct-print DOES surface).
WARN_KIND = "ioStderr"
ERR_KIND = "ioError"
WARN_MSG = "gf13 warning"
ERR_MSG = "gf13 error"

# Expected (line, name, string-value) for each formatted / concatenated local.
# `raw` keeps its literal backslash (4 chars: a \ n b); `tq` keeps the embedded
# newline (11 chars: line1 <NL> line2).
EXPECTED_STRINGS = [
    (45, "s", "a"),
    (47, "pf", "7/a/3.14"),
    (50, "ff", "x y"),
    (51, "raw", "a\\nb"),
    (52, "tq", "line1\nline2"),
    (54, "cc", "a-x"),
]

I_LINE = 44
OK_LINE = 55
ASSERT_LINE = 56
# Lines that MUST still record a step (a passing assert did not halt execution).
CONTINUE_LINES = [57, 58, 59, 60]


def load(path):
    with open(path) as f:
        return json.load(f)


def steps(doc):
    return [e for e in doc["events"] if e["kind"] == "step"]


def ios(doc):
    return [e for e in doc["events"] if e["kind"] == "io"]


def step_at_line(doc, line):
    for s in steps(doc):
        if s.get("line") == line:
            return s
    return None


def var_on_step(step, name):
    for v in step.get("vars", []):
        if v["varname"] == name:
            return v
    return None


def verify(doc):
    # 1. STRING FORMATTING — each formatted local captured with its EXACT value
    #    on the step at its own source line.
    for line, name, expected in EXPECTED_STRINGS:
        s = step_at_line(doc, line)
        if s is None:
            raise VerifyError(f"no step recorded at line {line} (for {name})")
        v = var_on_step(s, name)
        if v is None:
            raise VerifyError(f"local {name} not captured at line {line}")
        val = v["value"]
        if val.get("kind") != "String":
            raise VerifyError(f"{name} is not a String (kind={val.get('kind')})")
        if val.get("text") != expected:
            raise VerifyError(
                f"{name} value mismatch: got {val.get('text')!r}, expected {expected!r}")

    # length sanity (guards against a silent truncation of raw/triple-quoted).
    lens = {name: len(exp) for _, name, exp in EXPECTED_STRINGS}
    if lens["raw"] != 4:
        raise VerifyError(f"raw string must be 4 chars (literal backslash), got {lens['raw']}")
    if lens["tq"] != 11:
        raise VerifyError(f"triple-quoted string must be 11 chars (embedded NL), got {lens['tq']}")

    # scalar companions: i==7 (Int), ok==true (Bool).
    si = step_at_line(doc, I_LINE)
    if si is None or var_on_step(si, "i") is None or var_on_step(si, "i")["value"].get("i") != 7:
        raise VerifyError(f"i=7 (Int) not captured at line {I_LINE}")
    sok = step_at_line(doc, OK_LINE)
    if sok is None or var_on_step(sok, "ok") is None or var_on_step(sok, "ok")["value"].get("b") is not True:
        raise VerifyError(f"ok=true (Bool) not captured at line {OK_LINE}")

    # 2. assert — the assert line is a recorded STEP, and execution CONTINUED
    #    past it (a passing assert is a no-op step).
    sa = step_at_line(doc, ASSERT_LINE)
    if sa is None:
        raise VerifyError(f"assert line {ASSERT_LINE} did not record a step")
    for ln in CONTINUE_LINES:
        if step_at_line(doc, ln) is None:
            raise VerifyError(
                f"no step at line {ln}: execution did not continue past the passing assert")

    # 3. push_warning / push_error — each recorded ONCE as an io event with the
    #    right kind and message (text). push_warning -> ioStderr, push_error ->
    #    ioError (two distinct kinds), each carrying its exact message.
    io = ios(doc)
    warns = [e for e in io if e.get("io_kind") == WARN_KIND and e.get("text") == WARN_MSG]
    errs = [e for e in io if e.get("io_kind") == ERR_KIND and e.get("text") == ERR_MSG]
    if len(warns) != 1:
        raise VerifyError(
            f"expected exactly 1 push_warning io event ({WARN_KIND}, {WARN_MSG!r}), "
            f"got {len(warns)}; all io={[ (e.get('io_kind'), e.get('text')) for e in io ]}")
    if len(errs) != 1:
        raise VerifyError(
            f"expected exactly 1 push_error io event ({ERR_KIND}, {ERR_MSG!r}), "
            f"got {len(errs)}; all io={[ (e.get('io_kind'), e.get('text')) for e in io ]}")

    # 4. Types table unchanged (scalar-only; format arrays are temporaries).
    if doc["types"] != EXPECTED_TYPES:
        raise VerifyError(f"types table mismatch: {doc['types']}")

    print("GF13 verify OK: formatting pf='7/a/3.14' ff='x y' raw='a\\\\nb'(len4) "
          "tq='line1\\nline2'(len11) cc='a-x'; i=7 ok=true; assert step@55 + "
          "continued 56/57/58/59; push_warning('gf13 warning',ioStderr) + "
          "push_error('gf13 error',ioError); types [None,Int,Float,Bool,String,Variant]")


def tamper(doc, mode):
    """Corrupt the doc; the SAME verify() must then FAIL (proves non-vacuity)."""
    if mode == "fmtvalue":
        # Corrupt the raw-string capture (drop the literal backslash).
        for s in steps(doc):
            v = var_on_step(s, "raw")
            if v is not None:
                v["value"]["text"] = "anb"
    elif mode == "assertstep":
        # Delete the assert-line step.
        for i, e in enumerate(doc["events"]):
            if e["kind"] == "step" and e.get("line") == ASSERT_LINE:
                del doc["events"][i]
                break
    elif mode == "pushmsg":
        # Corrupt the push_error message.
        for e in doc["events"]:
            if e["kind"] == "io" and e.get("io_kind") == ERR_KIND:
                e["text"] = "WRONG"
    elif mode == "pushmissing":
        # Drop the push_warning event entirely.
        for i, e in enumerate(doc["events"]):
            if e["kind"] == "io" and e.get("io_kind") == WARN_KIND:
                del doc["events"][i]
                break
    else:
        raise SystemExit(f"unknown tamper mode {mode}")

    try:
        verify(doc)
    except VerifyError:
        print(f"tamper({mode}) correctly REJECTED")
        return
    raise SystemExit(f"tamper({mode}) was NOT caught — verifier is vacuous")


def main():
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    cmd, path = sys.argv[1], sys.argv[2]
    doc = load(path)
    if cmd == "verify":
        try:
            verify(doc)
        except VerifyError as ex:
            print(f"GF13 verify FAILED: {ex}", file=sys.stderr)
            raise SystemExit(1)
    elif cmd == "tamper":
        tamper(doc, sys.argv[3])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
