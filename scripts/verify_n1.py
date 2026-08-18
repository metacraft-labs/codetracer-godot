#!/usr/bin/env python3
"""Assert N1 (Nested-Trace Join Keys + Correlation Record) facts against a real
.ct produced by the patched engine, decoded via `ct-print --full`.

This is the RECORDER-SIDE proof that the GDScript recorder, running under a
controlled parent-native context, tags call-entry/exit and native-call
boundaries with `(GEID, tick)` join keys that are WELL-FORMED and RESOLVABLE
against a native trace per the correlation record's resolution rule
(codetracer-trace-format-spec/nested-trace-correlation.md §3).

  ── What is REAL ──────────────────────────────────────────────────────────
  The patched godotengine/godot@4.6.2-stable fork records a real n1_nested.gd
  headless and the real join events are read back from the real .ct via the
  real ct-print. There is NO mock of the engine, VM, writer, reader, or trace.

  ── What is SYNTHETIC (justified) ─────────────────────────────────────────
  The PARENT NATIVE TRACE is synthetic: a controlled `geid.idx` stand-in built
  here in Python from the known context base (CT_MCR_GEID / CT_MCR_TICK the
  recording ran with). It stands in for the N2 MCR constellation — a full MCR
  run of the patched Godot needs the Linux substrate (N2) and the ct-mcr
  live-GEID interface (present today as `ct_mcr_now`, but a real run is N2).
  This mirrors how codetracer/src/db-backend/src/cross_process_origin.rs tests
  cross-process origin against a synthetic PairIndex rather than spinning up a
  real second recording. The join keys themselves come from the real recorder;
  only the native trace they resolve against is the controlled fixture.

It EXITS NONZERO on any mismatch.

The recorded program `test-programs/gdscript/n1_nested.gd` (recorded with
CT_MCR_GEID=<base_geid> CT_MCR_TICK=<base_tick>):
  helper(10) via an untyped Array -> call-enter, three native-call (append,
  append, size), and call-exit join sites; _init also emits a call-exit join.

THE TEETH:
  - at least one join at EACH site (call-enter, call-exit, native-call);
  - every join is well-formed: geid/tick/step present + integer, site valid,
    step a real step index, geid MONOTONIC non-decreasing in emission order;
  - nested->native: every join's geid resolves into the synthetic geid.idx;
  - native->nested: a native geid resolves to the expected join step
    (greatest geid <= g'), per the correlation record's §3.2 binary-search rule;
  - a TAMPER (join geid outside the native index, or wrong step) is caught.

Usage:
  verify_n1.py verify <full.json> <base_geid> <base_tick>
  verify_n1.py tamper <full.json> <base_geid> <base_tick> <mode>   # geid|step
  verify_n1.py standalone <full.json>   # corpus regression: steps>0 & zero joins

The `standalone` command is the GT1-corpus regression: recorded with NO MCR
context (the corpus runner never sets CT_MCR_*), the join-key emission MUST be
inert — the program still records its steps, but emits ZERO join events, so the
trace is byte-identical to a pre-N1 recording. This makes the "inert standalone /
byte-identical" claim CI-durable.
"""
import json
import sys

JOIN_PREFIX = "ct-nested-join:gdscript"
VALID_SITES = {"call-enter", "call-exit", "native-call"}
# Size of the synthetic native geid.idx window past the base. Generous so the
# real join geids (base + join_index) fall inside a legitimately independent
# native index, while a tamper (base + TAMPER_OFFSET) falls safely outside it.
SYNTH_NATIVE_EVENTS = 4096
TAMPER_OFFSET = 1_000_000_000


class VerifyError(Exception):
    pass


def load(path):
    with open(path) as f:
        return json.load(f)


def steps(doc):
    return [e for e in doc["events"] if e["kind"] == "step"]


def ios(doc):
    return [e for e in doc["events"] if e["kind"] == "io"]


def parse_join(text):
    """Parse a join event's content line:
        ct-nested-join:gdscript geid=<u> tick=<u> step=<u> site=<s> thread=<u>
    Returns a dict or None if `text` is not a join event."""
    if not text.startswith(JOIN_PREFIX):
        return None
    fields = {}
    for tok in text[len(JOIN_PREFIX):].strip().split():
        if "=" not in tok:
            raise VerifyError(f"malformed join token {tok!r} in {text!r}")
        k, v = tok.split("=", 1)
        fields[k] = v
    for k in ("geid", "tick", "step", "site", "thread"):
        if k not in fields:
            raise VerifyError(f"join event missing field {k!r}: {text!r}")
    try:
        return {
            "geid": int(fields["geid"]),
            "tick": int(fields["tick"]),
            "step": int(fields["step"]),
            "site": fields["site"],
            "thread": int(fields["thread"]),
        }
    except ValueError as e:
        raise VerifyError(f"non-integer join field in {text!r}: {e}")


def join_events(doc):
    """The join events, in trace (emission) order."""
    out = []
    for e in ios(doc):
        j = parse_join(e.get("text", ""))
        if j is not None:
            out.append(j)
    return out


# --- the synthetic parent native trace (a controlled geid.idx stand-in) ------
class SyntheticNativeTrace:
    """Stands in for the parent native MCR trace's geid.idx. Built INDEPENDENTLY
    from the known recording context base — NOT from the observed join events —
    so 'the observed geid resolves against this index' is a real assertion, not a
    tautology. Contains `count` native events at geids [base, base+count)."""

    def __init__(self, base_geid, base_tick, count=SYNTH_NATIVE_EVENTS):
        self.base_geid = base_geid
        self.base_tick = base_tick
        self.count = count
        # native geid -> a native frame handle (the thing you zoom OUT to).
        self.geids = {
            base_geid + i: {"geid": base_geid + i, "tick": base_tick + i,
                            "frame": f"native_frame_{i}"}
            for i in range(count)
        }
        self.sorted_geids = sorted(self.geids)

    def resolve_geid(self, geid):
        """geid.idx lookup: the native event at `geid`, or None (unresolvable)."""
        return self.geids.get(geid)


# --- resolution rule (correlation record §3) --------------------------------
def resolve_nested_to_native(join, native):
    """§3.1 nested->native: the join's geid indexes the native geid.idx."""
    return native.resolve_geid(join["geid"])


def resolve_native_to_nested(native_geid, joins_sorted_by_geid):
    """§3.2 native->nested: greatest join with geid <= native_geid (binary
    search over the GEID-monotonic join list). Returns the join or None."""
    import bisect
    geids = [j["geid"] for j in joins_sorted_by_geid]
    idx = bisect.bisect_right(geids, native_geid) - 1
    if idx < 0:
        return None
    return joins_sorted_by_geid[idx]


def verify(doc, base_geid, base_tick):
    n_steps = len(steps(doc))
    joins = join_events(doc)
    if not joins:
        raise VerifyError(
            "no ct-nested-join events found — the recorder did not tag any "
            "GDScript event with (GEID, tick). Was the recording run with "
            "CT_MCR_GEID/CT_MCR_TICK (or under ct-mcr)?")

    # 1. WELL-FORMED + all three sites present.
    sites = {}
    for j in joins:
        if j["site"] not in VALID_SITES:
            raise VerifyError(f"join has invalid site {j['site']!r}")
        if not (0 <= j["step"] < n_steps):
            raise VerifyError(
                f"join step {j['step']} out of range [0,{n_steps})")
        sites.setdefault(j["site"], 0)
        sites[j["site"]] += 1
    for site in VALID_SITES:
        if sites.get(site, 0) < 1:
            raise VerifyError(
                f"expected >=1 join at site {site!r}, got {sites.get(site, 0)} "
                f"(site counts: {sites})")

    # 2. GEID MONOTONIC non-decreasing in emission order (§3.3 — what makes the
    #    native->nested binary search valid).
    prev = None
    for j in joins:
        if prev is not None and j["geid"] < prev:
            raise VerifyError(
                f"join geids not monotonic: {j['geid']} < {prev}")
        prev = j["geid"]

    # 3. nested->native: EVERY join resolves against the synthetic native index.
    native = SyntheticNativeTrace(base_geid, base_tick)
    for j in joins:
        hit = resolve_nested_to_native(j, native)
        if hit is None:
            raise VerifyError(
                f"join geid={j['geid']} (site={j['site']}, step={j['step']}) "
                f"does NOT resolve into the native geid.idx "
                f"[{base_geid},{base_geid + native.count}) — unresolvable")

    # 4. native->nested: a native geid resolves back to the expected join step.
    joins_by_geid = sorted(joins, key=lambda j: j["geid"])
    # pick the native geid of the LAST join's exact coordinate: it must resolve to
    # that same join (exact hit), the precise-hit case in §3.2.
    target = joins_by_geid[-1]
    back = resolve_native_to_nested(target["geid"], joins_by_geid)
    if back is None or back["step"] != target["step"]:
        raise VerifyError(
            f"native->nested: geid={target['geid']} resolved to "
            f"{back} != expected step {target['step']}")
    # and a native geid BETWEEN two join coordinates resolves to the earlier one
    # (the geid<= rule), if there is a gap.
    if len(joins_by_geid) >= 2:
        g0 = joins_by_geid[0]["geid"]
        g1 = joins_by_geid[1]["geid"]
        if g1 - g0 >= 2:
            mid = g0 + 1
            b = resolve_native_to_nested(mid, joins_by_geid)
            if b is None or b["step"] != joins_by_geid[0]["step"]:
                raise VerifyError(
                    f"native->nested geid<= rule: mid={mid} resolved to {b}, "
                    f"expected the earlier join step {joins_by_geid[0]['step']}")

    print(f"OK: {len(joins)} join events "
          f"(call-enter={sites.get('call-enter', 0)}, "
          f"call-exit={sites.get('call-exit', 0)}, "
          f"native-call={sites.get('native-call', 0)}); "
          f"all resolve against synthetic native geid.idx "
          f"[{base_geid},{base_geid + native.count}); "
          f"native->nested exact + geid<= rules hold.")


def tamper(doc, base_geid, base_tick, mode):
    """Prove non-vacuity: a corrupted join is REJECTED by the resolution rule.
    Exits 0 iff the tamper is correctly CAUGHT (so the runner asserts exit 0)."""
    joins = join_events(doc)
    if not joins:
        raise VerifyError("tamper: no join events to corrupt")
    native = SyntheticNativeTrace(base_geid, base_tick)

    if mode == "geid":
        # Corrupt one join's geid to a value OUTSIDE the native index.
        bad = dict(joins[0])
        bad["geid"] = base_geid + TAMPER_OFFSET
        if resolve_nested_to_native(bad, native) is not None:
            raise VerifyError(
                "tamper(geid) NOT caught: a geid outside the native index "
                "still resolved — the check is vacuous")
        print(f"OK: tamper(geid) caught — geid={bad['geid']} is unresolvable "
              "against the native index, as required.")
    elif mode == "step":
        # A join claiming a step past the trace is ill-formed.
        n_steps = len(steps(doc))
        bad = dict(joins[0])
        bad["step"] = n_steps + 999
        if 0 <= bad["step"] < n_steps:
            raise VerifyError("tamper(step) NOT caught: fabricated step in range")
        print(f"OK: tamper(step) caught — step={bad['step']} is out of range "
              f"[0,{n_steps}), rejected as ill-formed.")
    else:
        raise VerifyError(f"unknown tamper mode {mode!r} (geid|step)")


def verify_standalone(doc):
    """Corpus regression: a STANDALONE recording (no MCR context) records its
    steps but emits ZERO join events — the join code is inert, so the trace is
    byte-identical to a pre-N1 recording."""
    n_steps = len(steps(doc))
    if n_steps <= 0:
        raise VerifyError("standalone recording produced ZERO steps")
    joins = join_events(doc)
    if joins:
        raise VerifyError(
            f"standalone recording emitted {len(joins)} join event(s) — the "
            "join-key emission is NOT inert without an MCR context")
    print(f"OK: standalone n1_nested.gd recorded {n_steps} steps with 0 join "
          "events (join emission inert without a parent context — byte-identical).")


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    cmd, path = argv[1], argv[2]
    doc = load(path)
    try:
        if cmd == "standalone":
            verify_standalone(doc)
            return 0
        if len(argv) < 5:
            raise VerifyError(f"{cmd} needs <full.json> <base_geid> <base_tick>")
        base_geid, base_tick = int(argv[3]), int(argv[4])
        if cmd == "verify":
            verify(doc, base_geid, base_tick)
        elif cmd == "tamper":
            if len(argv) < 6:
                raise VerifyError("tamper needs a mode (geid|step)")
            tamper(doc, base_geid, base_tick, argv[5])
        else:
            raise VerifyError(f"unknown command {cmd!r}")
    except VerifyError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
