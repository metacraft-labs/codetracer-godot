#!/usr/bin/env python3
"""Check that a recording carries a well-formed ``CodePatchEvent`` (HLX-M7).

Design: ``reprobuild-specs/HCR/Linux-ELF-Provider.md`` §10.3.
Protocol: ``codetracer-specs/Planned-Features/Hot-Code-Reloading-High-Level-Interfaces.md``
§7.2, which fixes the event's six fields::

    CodePatchEvent { geid, patchId, patchedSymbols, patchBundle,
                     codeHashBefore, codeHashAfter }

WHAT THIS EXISTS TO PREVENT, MEASURED
-------------------------------------
Before HLX-M7, a recording taken while the HCR provider patched a live Godot
engine had an IDENTICAL SET OF EVENT KINDS to a control recording with no patch.
Nothing in the container distinguished post-patch execution from pre-patch
execution, so a replay would have reproduced the ORIGINAL code's semantics
against events the NEW code produced — wrong, silently, from the patch point on.

So this script does not ask "is there a code-patch event". It asks whether the
event's contents are CHECKABLE and CHECK OUT, because the campaign this belongs
to has a bad record with fields that look like measurements and are literals.

THE THREE INDEPENDENT ANCHORS
-----------------------------
A hash you can only compare against itself is not evidence. Every digest here is
recomputed from bytes obtained by a different route:

1. ``codeHashBefore`` / ``codeHashAfter`` are recomputed with Python's
   ``hashlib`` from the per-site ``wordBefore`` / ``wordAfter`` the SAME event
   records. A fabricated or zeroed digest cannot survive this, and neither can a
   digest computed over something other than the patched bytes.
2. ``patchBundle`` is recomputed from the patch object bytes the DRIVER built
   and reported — a completely separate transport and a completely separate
   SHA-256 implementation from the agent's C one. If the agent's hash function
   were broken, this is where it shows.
3. ``wordAfter`` must decode as ``E9 rel32 90 90 90`` whose ``rel32`` lands
   exactly on the ``dispatchAddress`` the event records separately. That ties
   the recorded bytes to the recorded addresses; a fabricated word fails it.

Plus the differential the campaign already had, INVERTED: the patched and
unpatched recordings must now differ, and differ SPECIFICALLY by this event.

COMPLETENESS BEFORE ABSENCE
---------------------------
No conclusion is drawn from anything missing until the dump it is missing from
has been shown complete — line count equal to the count ``trace info`` reports.
"Non-empty" is not enough: a dump that stopped early is non-empty and its
absences mean nothing.

Exit 0 when every check holds, 1 otherwise. Every failure is printed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

FAILURES: list[str] = []
NOTES: list[str] = []


def fail(msg: str) -> None:
    FAILURES.append(msg)
    print(f"CHECK-FAIL: {msg}")


def ok(msg: str) -> None:
    NOTES.append(msg)
    print(f"ok: {msg}")


def read_trace_info(path: Path) -> dict[str, str]:
    info: dict[str, str] = {}
    for line in path.read_text(errors="replace").splitlines():
        if ": " in line:
            key, _, value = line.partition(": ")
            info[key.strip()] = value.strip()
    return info


def dump_is_complete(dump: Path, info: dict[str, str], label: str) -> bool:
    """A dump whose length does not match the header's count proves nothing.

    This is one line per event by construction, so the count is exact. A check
    that settled for ``lines > 0`` here has already been shipped once in this
    campaign and stayed green under ``head -10``.
    """
    if "events" not in info:
        fail(f"{label}: `trace info` did not report an events count")
        return False
    try:
        reported = int(info["events"])
    except ValueError:
        fail(f"{label}: events count is not a number: {info['events']!r}")
        return False
    lines = sum(1 for _ in dump.open(errors="replace"))
    if lines != reported:
        fail(
            f"{label}: the event dump has {lines} lines but the trace reports "
            f"{reported} events. The dump is INCOMPLETE, so nothing may be "
            f"concluded from what it does or does not contain."
        )
        return False
    ok(f"{label}: event dump is complete — {lines} lines == {reported} events")
    return True


EVENT_TYPE_RE = re.compile(r"^\[(\d+)\] type=(\w+) tid=(\d+) geid=(\d+) ")


def event_kinds(dump: Path) -> dict[str, int]:
    kinds: dict[str, int] = {}
    with dump.open(errors="replace") as fh:
        for line in fh:
            m = EVENT_TYPE_RE.match(line)
            if m:
                kinds[m.group(2)] = kinds.get(m.group(2), 0) + 1
    return kinds


def code_patch_lines(dump: Path) -> list[str]:
    out = []
    with dump.open(errors="replace") as fh:
        for line in fh:
            if " type=evCodePatch " in line:
                out.append(line.rstrip("\n"))
    return out


FIELD_RE = re.compile(r"(\w+)=([^\s\]]+)")


def parse_event_line(line: str) -> dict[str, str]:
    """Pull ``key=value`` pairs out of the rendered event line.

    The site record is bracketed, so its fields are prefixed to keep them
    distinct from the event's own.
    """
    fields: dict[str, str] = {}
    head, _, site = line.partition("site[")
    for key, value in FIELD_RE.findall(head):
        fields[key] = value
    if site:
        site_body = site.rstrip()
        if site_body.endswith("]"):
            site_body = site_body[:-1]
        for key, value in FIELD_RE.findall(site_body):
            fields["site." + key] = value
    return fields


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def strip_prefix(value: str, prefix: str = "sha256:") -> str:
    return value[len(prefix):] if value.startswith(prefix) else value


ZERO_DIGEST = "0" * 64


def check_patched(dump: Path, info: Path, driver_json: Path,
                  expect_patch_id: str, expect_symbol: str) -> None:
    tinfo = read_trace_info(info)
    if not dump_is_complete(dump, tinfo, "patched"):
        return

    if tinfo.get("codePatches") is None:
        fail("patched: `trace info` printed no `codePatches:` line. That line is "
             "printed unconditionally by a ct-mcr that knows about the event, so "
             "its absence means this trace was read by one that does not — and "
             "no conclusion about the event may be drawn from such a reader.")
    elif tinfo["codePatches"] != "1":
        fail(f"patched: trace info reports codePatches={tinfo['codePatches']}, expected 1")
    else:
        ok("patched: trace info reports codePatches: 1")

    lines = code_patch_lines(dump)
    if len(lines) != 1:
        fail(f"patched: found {len(lines)} evCodePatch events in the dump, expected exactly 1")
        return
    line = lines[0]
    f = parse_event_line(line)

    # --- §7.2 field 1: geid ---------------------------------------------
    geid = int(f.get("geid", "0"))
    if geid <= 0:
        fail(f"patched: CodePatchEvent geid is {geid}; §7.2 makes it the boundary "
             "between two code versions, and 0 is not a boundary")
    else:
        ok(f"patched: geid={geid}")

    # It has to be a BOUNDARY: there must be recorded events on both sides of
    # it. An event at the very start or the very end divides nothing, and would
    # be equally consistent with the patch having been recorded at the wrong
    # moment.
    before = after = 0
    with dump.open(errors="replace") as fh:
        for row in fh:
            m = EVENT_TYPE_RE.match(row)
            if not m:
                continue
            g = int(m.group(4))
            if g < geid:
                before += 1
            elif g > geid:
                after += 1
    if before == 0 or after == 0:
        fail(f"patched: the CodePatchEvent geid {geid} has {before} events before "
             f"it and {after} after it; it does not divide the recording, so it "
             "is not the boundary §7.2 describes")
    else:
        ok(f"patched: the geid divides the recording — {before} events before, {after} after")

    # --- §7.2 field 2: patchId ------------------------------------------
    if f.get("patchId") != expect_patch_id:
        fail(f"patched: patchId={f.get('patchId')!r}, expected {expect_patch_id!r}")
    else:
        ok(f"patched: patchId={f['patchId']}")

    # --- §7.2 field 3: patchedSymbols -----------------------------------
    symbols = f.get("patchedSymbols", "")
    if not symbols or symbols in ("-", "none"):
        fail("patched: patchedSymbols is empty. An event that says the code "
             "changed but not WHICH code is not the event §7.2 specifies.")
    elif expect_symbol not in symbols.split(","):
        fail(f"patched: patchedSymbols={symbols!r} does not name {expect_symbol!r}")
    else:
        ok(f"patched: patchedSymbols={symbols}")

    # --- the per-site record, which is what makes the hashes checkable ---
    try:
        word_before = int(f["site.wordBefore"], 16)
        word_after = int(f["site.wordAfter"], 16)
        window = int(f["site.window"], 16)
        dispatch = int(f["site.dispatch"], 16)
        window_len = int(f["site.windowLen"])
    except (KeyError, ValueError) as exc:
        fail(f"patched: the event carries no usable site record ({exc}); without "
             "it the hashes cannot be recomputed by anyone")
        return

    if word_before == word_after:
        fail("patched: wordBefore == wordAfter, so nothing changed; an event "
             "reporting a patch that changed no bytes is not a record of a patch")
    if window_len != 8:
        fail(f"patched: windowLen={window_len}, expected the 8-byte naturally "
             "aligned publication window the provider's §4.2 rule requires")
    if window % 8 != 0:
        fail(f"patched: window 0x{window:x} is not 8-byte aligned, so the "
             "publishing store was not single-copy atomic")

    b_before = word_before.to_bytes(8, "little")
    b_after = word_after.to_bytes(8, "little")

    # --- ANCHOR 3: the published word must be the jump it claims ---------
    if b_after[0] != 0xE9:
        fail(f"patched: wordAfter starts with 0x{b_after[0]:02x}, not 0xE9; the "
             "provider publishes a 5-byte `E9 rel32`")
    else:
        rel32 = int.from_bytes(b_after[1:5], "little", signed=True)
        target = window + 5 + rel32
        if target != dispatch:
            fail(f"patched: wordAfter's rel32 targets 0x{target:x} but the event "
                 f"records dispatch=0x{dispatch:x}. The recorded bytes and the "
                 "recorded addresses disagree, so at least one of them is fabricated.")
        else:
            ok(f"patched: wordAfter decodes to `jmp 0x{target:x}` == the recorded "
               f"dispatchAddress (rel32={rel32})")
        if b_after[5:] != b"\x90\x90\x90":
            fail(f"patched: the three bytes after the jump are {b_after[5:].hex()}, "
                 "not the NOP fill the 8-byte publication window requires")

    # --- §7.2 fields 5 and 6: the code hashes ----------------------------
    # ANCHOR 1: recomputed here, by a different SHA-256 implementation, from
    # bytes the event itself records.
    got_before = strip_prefix(f.get("codeHashBefore", ""))
    got_after = strip_prefix(f.get("codeHashAfter", ""))
    want_before = sha256_hex(b_before)
    want_after = sha256_hex(b_after)

    for name, got in (("codeHashBefore", got_before), ("codeHashAfter", got_after)):
        if len(got) != 64:
            fail(f"patched: {name} is {got!r}, not a 64-hex-digit SHA-256")
        elif got == ZERO_DIGEST:
            fail(f"patched: {name} is all zeros. SHA-256 of no input is all "
                 "zeros, so this is a placeholder, not a digest.")
    if got_before == got_after:
        fail("patched: codeHashBefore == codeHashAfter, but the patched bytes "
             "changed. Two equal hashes over different inputs is a constant.")
    if got_before != want_before:
        fail(f"patched: codeHashBefore={got_before} but SHA-256 of the recorded "
             f"pre-patch bytes {b_before.hex()} is {want_before}. The digest is "
             "not a hash of the code it claims to cover.")
    else:
        ok(f"patched: codeHashBefore == sha256({b_before.hex()}) == {want_before}")
    if got_after != want_after:
        fail(f"patched: codeHashAfter={got_after} but SHA-256 of the recorded "
             f"post-patch bytes {b_after.hex()} is {want_after}.")
    else:
        ok(f"patched: codeHashAfter == sha256({b_after.hex()}) == {want_after}")

    # --- §7.2 field 4: patchBundle ---------------------------------------
    driver = json.loads(driver_json.read_text())
    patch_bytes = bytes.fromhex(driver["patchBytesHex"])
    want_bundle = sha256_hex(patch_bytes)
    got_bundle = strip_prefix(f.get("patchBundle", ""))
    if got_bundle != want_bundle:
        # ANCHOR 2, and the strongest one: these bytes reached this script by a
        # completely different route (the driver read the patch object off
        # disk) and are hashed by a completely different implementation.
        fail(f"patched: patchBundle={got_bundle} but SHA-256 of the {len(patch_bytes)} "
             f"patch bytes the driver actually sent is {want_bundle}")
    else:
        ok(f"patched: patchBundle == sha256(the driver's {len(patch_bytes)} patch "
           f"bytes) == {want_bundle}")

    embedded = int(f.get("patchBundleBytes", "-1"))
    total = int(f.get("patchBundleTotalLen", "-1"))
    if total != len(patch_bytes):
        fail(f"patched: patchBundleTotalLen={total}, expected {len(patch_bytes)}")
    elif embedded != total:
        fail(f"patched: only {embedded} of {total} bundle bytes are embedded, so "
             "the trace is not self-contained for this patch (§7.3 replays the "
             "bundle out of the container)")
    else:
        ok(f"patched: the full {total}-byte patch bundle is embedded in the trace")

    # --- the tier, which is what stops the boundary being over-read ------
    tier = f.get("tier", "")
    if tier not in ("tier1-no-quiescence", "tier2-quiesced"):
        fail(f"patched: tier={tier!r}. Design §10.3 requires the publication tier "
             "be recorded, because under tier 1 the geid boundary is only "
             "approximate and a reader that assumed exactness would be wrong.")
    else:
        ok(f"patched: publicationTier is recorded as {tier}")

    # --- the support profile, which §7.3 needs to refuse a foreign host ---
    profile = f.get("supportProfile", "")
    driver_profile = driver.get("supportProfile", "")
    if not profile:
        fail("patched: the event records no supportProfile; a bundle replayed on "
             "a foreign host would then be applied blindly (§7.3)")
    elif profile != driver_profile:
        fail(f"patched: supportProfile={profile!r} but the agent negotiated "
             f"{driver_profile!r}")
    else:
        ok(f"patched: supportProfile={profile}")

    # --- two transports, one set of digests ------------------------------
    cpe = driver.get("codePatchEvent")
    if not cpe:
        fail("patched: the driver's result carries no codePatchEvent report, so "
             "the trace's digests cannot be cross-checked against the wire's")
    else:
        if not cpe.get("recorded"):
            fail(f"patched: the agent reports the event was NOT recorded "
                 f"(bridgeResult={cpe.get('bridgeResult')}, "
                 f"bridgePresent={cpe.get('bridgePresent')})")
        if not cpe.get("hashSelfTest"):
            fail("patched: the agent's SHA-256 self-test FAILED, so its digests "
                 "are not digests")
        if not cpe.get("claimHeld"):
            fail("patched: the agent does not hold the claim on the published "
                 "window (§10.1), so MCR could still take those bytes")
        for name, key in (("codeHashBefore", "codeHashBefore"),
                          ("codeHashAfter", "codeHashAfter"),
                          ("patchBundle", "patchBundle")):
            wire = strip_prefix(cpe.get(key, ""))
            trace = strip_prefix(f.get(name, ""))
            if wire != trace:
                fail(f"patched: {name} differs between the agent's wire report "
                     f"({wire}) and the trace ({trace})")
        ok("patched: the agent's wire report and the trace agree on all three digests")


def check_control(dump: Path, info: Path) -> dict[str, int]:
    tinfo = read_trace_info(info)
    if not dump_is_complete(dump, tinfo, "control"):
        return {}
    if tinfo.get("codePatches") != "0":
        fail(f"control: trace info reports codePatches={tinfo.get('codePatches')}, "
             "expected 0 — an unpatched run must not carry a code-patch event")
    else:
        ok("control: trace info reports codePatches: 0")
    lines = code_patch_lines(dump)
    if lines:
        fail(f"control: the unpatched recording carries {len(lines)} evCodePatch "
             "event(s); the event is then not evidence of a patch")
    else:
        ok("control: the unpatched recording carries no evCodePatch event")
    return event_kinds(dump)


def check_differential(patched_dump: Path, control_kinds: dict[str, int]) -> None:
    """The campaign's own control, inverted.

    Before HLX-M7 it was MEASURED that a patched recording and an unpatched one
    had the same set of event kinds — which is why a replay could not tell them
    apart. After HLX-M7 they must differ, and differ by exactly this event.
    """
    if not control_kinds:
        fail("differential: no control event kinds were collected, so the "
             "comparison compared nothing")
        return
    patched_kinds = event_kinds(patched_dump)
    if not patched_kinds:
        fail("differential: no patched event kinds were collected")
        return
    only_patched = set(patched_kinds) - set(control_kinds)
    only_control = set(control_kinds) - set(patched_kinds)
    print(f"info: patched has {len(patched_kinds)} event kinds, "
          f"control has {len(control_kinds)}")
    if only_patched != {"evCodePatch"}:
        fail(f"differential: the kinds present only in the patched recording are "
             f"{sorted(only_patched)}, expected exactly ['evCodePatch']. Before "
             "HLX-M7 this set was EMPTY and that was the defect; a set with "
             "other members means something else also changed and the "
             "comparison is no longer clean.")
    else:
        ok("differential: the patched recording differs from the unpatched "
           "control by exactly one event kind, evCodePatch")
    if only_control:
        fail(f"differential: kinds present only in the CONTROL: {sorted(only_control)}. "
             "The patch removed a class of event from the recording, which is a "
             "capture regression, not a code-patch boundary.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--patched-events", type=Path, required=True)
    ap.add_argument("--patched-info", type=Path, required=True)
    ap.add_argument("--driver-json", type=Path, required=True)
    ap.add_argument("--control-events", type=Path)
    ap.add_argument("--control-info", type=Path)
    ap.add_argument("--patch-id", required=True)
    ap.add_argument("--symbol", required=True)
    args = ap.parse_args()

    for path in (args.patched_events, args.patched_info, args.driver_json):
        if not path.is_file():
            print(f"CHECK-FAIL: required input missing: {path}")
            return 1

    check_patched(args.patched_events, args.patched_info, args.driver_json,
                  args.patch_id, args.symbol)

    if args.control_events and args.control_info:
        if not args.control_events.is_file() or not args.control_info.is_file():
            fail("the control arm was requested but its artifacts are missing; "
                 "a differential with one arm is not a differential")
        else:
            control_kinds = check_control(args.control_events, args.control_info)
            check_differential(args.patched_events, control_kinds)
    else:
        fail("no control arm was given. The whole point of this gate is that a "
             "patched recording must now be DISTINGUISHABLE from an unpatched "
             "one; without the other arm nothing is being distinguished.")

    print()
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} check(s) FAILED")
        return 1
    print(f"RESULT: PASS — {len(NOTES)} checks held")
    return 0


if __name__ == "__main__":
    sys.exit(main())
