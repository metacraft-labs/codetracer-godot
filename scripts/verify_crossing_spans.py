#!/usr/bin/env python3
"""Grade a GDScript recording's native<->VM crossing spans (GDScript-Recorder MT3).

A crossing span bounds a VM frame inside a NATIVE recording, so whether a
recording carries them depends on whether the native recorder (MCR) was
recording the engine:

  standalone   no crossing spans at all (Mixed-Trace-Implicit-Switch.md §4:
               "Standalone (non-combined) traces are unaffected: no crossing
               spans"; MT3: "A standalone (non-MCR) recording emits NO crossing
               spans"). The recording must have made calls, or zero spans would
               be true of a program that never entered a frame.
  mcr          one `gdscript-frame` span per recorded call, every one settled,
               strictly nested.

Usage:
  verify_crossing_spans.py standalone <spans.json> <calls>
  verify_crossing_spans.py mcr        <spans.json> <calls>

<spans.json> is `ct-print --spans --json-out <file.ct>`; <calls> is the
number of frames the engine entered (call records other than the
`<toplevel>` root). Exits nonzero on any mismatch.
"""

import json
import sys


def check_standalone(spans, calls):
    problems = []
    if calls <= 0:
        problems.append("the recording made no calls, so 'no crossing spans' proves nothing")
    if spans:
        kinds = sorted({s.get("span_type") for s in spans})
        problems.append(
            "a standalone recording carries %d crossing span(s) of type %s for %d call(s); "
            "it must carry none" % (len(spans), kinds, calls)
        )
    return problems, "no crossing spans over %d calls" % calls


def check_mcr(spans, calls):
    problems = []
    if not spans:
        problems.append("the recording carries NO crossing spans")
    wrong = [s for s in spans if s.get("span_type") != "gdscript-frame"]
    if wrong:
        problems.append("span types other than gdscript-frame: %s" % sorted({s.get("span_type") for s in wrong}))
    still_open = [s["span_id"] for s in spans if s.get("open")]
    if still_open:
        problems.append("spans left open (never settled): %s" % still_open)
    if len(spans) != calls:
        problems.append("%d crossing spans for %d recorded calls (expected one per call)" % (len(spans), calls))
    # Strict nesting: any two crossings are disjoint or one contains the other.
    for i, a in enumerate(spans):
        for b in spans[i + 1 :]:
            a0, a1 = a["start_step"], a["end_step"]
            b0, b1 = b["start_step"], b["end_step"]
            disjoint = a1 < b0 or b1 < a0
            contains = (a0 <= b0 and b1 <= a1) or (b0 <= a0 and a1 <= b1)
            if not (disjoint or contains):
                problems.append(
                    "spans %d [%d,%d] and %d [%d,%d] partially overlap" % (a["span_id"], a0, a1, b["span_id"], b0, b1)
                )
    return problems, "%d gdscript-frame crossing spans, one per call, strictly nested" % len(spans)


def main():
    if len(sys.argv) != 4 or sys.argv[1] not in ("standalone", "mcr"):
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    mode, path, calls = sys.argv[1], sys.argv[2], int(sys.argv[3])
    with open(path) as f:
        spans = json.load(f)
    check = check_standalone if mode == "standalone" else check_mcr
    problems, summary = check(spans, calls)
    if problems:
        for p in problems:
            print("CHECK-FAIL (%s crossing spans): %s" % (mode, p))
        sys.exit(1)
    print("OK (%s crossing spans): %s" % (mode, summary))


if __name__ == "__main__":
    main()
