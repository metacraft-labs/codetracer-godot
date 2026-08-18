#!/usr/bin/env python3
"""Assert GF10 (coroutines & `await`, async-continuation integration) facts
against a real .ct produced by the patched engine, decoded via `ct-print --full`.

This is the RECORDER-SIDE proof: it asserts that the engine emits the async
suspend/resume MARKERS and that the coroutine's steps/values/calls are recorded
with balanced frames and surviving locals. The authoritative proof that these
markers pair into a CodeTracer `ContinuationLink` (HTTP-Request-Panel.md §3.2)
lives in the db-backend integration test
`verify_gdscript_await_continuation_link.rs` (which reads the marker METADATA
`ct-print` does not surface), following the G5 MaterializedReplaySession pattern.

It EXITS NONZERO on any mismatch. Expected facts are hand-derived in
scripts/EXPECTED-GF10.md and duplicated here as literals so the check is not
circular.

The recorded program `test-programs/gdscript/gf_coroutine.gd`:
  work(): base=10; `await go` (SUSPEND on a signal); on resume kept=base=10
          (surviving local), result=kept+payload=42; returns 42.
  _initialize(): `await work()` (SUSPEND on a coroutine); on resume r=42, check=42.

THE TEETH:
  - exactly 2 suspend markers + 2 resume markers (one pair per await);
  - work & _initialize each record as TWO balanced frames (suspend + resume
    portion) across the yield (resolves open question #4);
  - the pre-await local `base` survives: kept==10 on resume;
  - the join value r==42 / check==42.

Usage:
  verify_gf10.py verify <full.json>
  verify_gf10.py tamper <full.json> <mode>   # markers|surviving|joinvalue|balance
"""
import json
import sys


class VerifyError(Exception):
    pass


SUSPEND_TAG = "ct-async-suspend:gdscript-coroutine"
RESUME_TAG = "ct-async-resume:gdscript-coroutine"
EXPECTED_TYPES = ["None", "Int", "Float", "Bool", "String", "Variant", "Object"]


def load(path):
    with open(path) as f:
        return json.load(f)


def steps(doc):
    return [e for e in doc["events"] if e["kind"] == "step"]


def ios(doc):
    return [e for e in doc["events"] if e["kind"] == "io"]


def calls(doc):
    return [e for e in doc["events"] if e["kind"] == "call_entry"]


def var_on_step(step, name):
    for v in step.get("vars", []):
        if v["varname"] == name:
            return v["value"].get("i")
    return None


def find_step_with_var(doc, name, value):
    for s in steps(doc):
        if var_on_step(s, name) == value:
            return s
    return None


def verify(doc):
    # 1. MARKERS: exactly two suspend + two resume.
    markers = ios(doc)
    susp = [e for e in markers if e["text"] == SUSPEND_TAG]
    res = [e for e in markers if e["text"] == RESUME_TAG]
    if len(susp) != 2:
        raise VerifyError(f"expected 2 suspend markers, got {len(susp)}")
    if len(res) != 2:
        raise VerifyError(f"expected 2 resume markers, got {len(res)}")

    # 2. BALANCE (open question #4): work & _initialize each record as TWO frames.
    fns = [c["function"] for c in calls(doc)]
    if fns.count("work") != 2:
        raise VerifyError(f"coroutine `work` must record 2 balanced frames, got {fns.count('work')}")
    if fns.count("_initialize") != 2:
        raise VerifyError(f"`_initialize` must record 2 balanced frames, got {fns.count('_initialize')}")

    # 3. SURVIVING LOCAL: base==10 captured, and kept==base==10 on resume.
    if find_step_with_var(doc, "base", 10) is None:
        raise VerifyError("pre-await local base=10 not captured")
    if find_step_with_var(doc, "kept", 10) is None:
        raise VerifyError("surviving local kept=10 (base survived the suspension) not captured on resume")

    # 4. JOIN VALUE + result.
    if find_step_with_var(doc, "result", 42) is None:
        raise VerifyError("work result=42 not captured")
    if find_step_with_var(doc, "r", 42) is None:
        raise VerifyError("await work() join r=42 not captured")

    # 5. Types table unchanged shape (scalar set + Object for the coroutine state).
    if doc["types"] != EXPECTED_TYPES:
        raise VerifyError(f"types table mismatch: {doc['types']}")

    print("GF10 verify OK: 2 suspend + 2 resume markers; work/_initialize each 2 balanced frames; "
          "base=10 survived -> kept=10; result=42; r=42")


def tamper(doc, mode):
    """Corrupt the doc; the SAME verify() must then FAIL (proves non-vacuity)."""
    if mode == "markers":
        # Drop one resume marker.
        for i, e in enumerate(doc["events"]):
            if e["kind"] == "io" and e["text"] == RESUME_TAG:
                del doc["events"][i]
                break
    elif mode == "surviving":
        for s in steps(doc):
            for v in s.get("vars", []):
                if v["varname"] == "kept":
                    v["value"]["i"] = 999
    elif mode == "joinvalue":
        for s in steps(doc):
            for v in s.get("vars", []):
                if v["varname"] == "r":
                    v["value"]["i"] = 999
    elif mode == "balance":
        # Remove one `work` frame.
        for i, e in enumerate(doc["events"]):
            if e["kind"] == "call_entry" and e["function"] == "work":
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
        except VerifyError as e:
            print(f"GF10 verify FAILED: {e}", file=sys.stderr)
            raise SystemExit(1)
    elif cmd == "tamper":
        tamper(doc, sys.argv[3])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
