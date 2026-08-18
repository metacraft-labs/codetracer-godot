#!/usr/bin/env python3
"""Assert GF11 (Node lifecycle callbacks) facts against a real .ct produced by
the patched engine, decoded via `ct-print --full`.

A plain `extends Node` (test-programs/gdscript/gf_node.gd) is driven through its
full lifecycle by a real headless SceneTree (gf_node_main.gd): add_child -> tick
-> remove_child -> quit. Every engine-invoked lifecycle callback
(`_init`/`_enter_tree`/`_ready`/`_process`/`_physics_process`/`_notification`/
`_exit_tree`) still enters GDScriptFunction::call, so the existing G3 call/return
hook records each as a FRAME and the G4 assign hook captures the argument read
into a local. GF11 adds NO engine hook — it is a coverage milestone.

THE TEETH (see scripts/EXPECTED-GF11.md for the hand-derived facts):
  - each lifecycle callback is recorded as a NODE frame (attributed by the source
    path of its steps, since the SceneTree driver also defines `_process`);
  - `_init`/`_enter_tree`/`_ready`/`_exit_tree` fire exactly once;
  - `_process` fires EXACTLY 3 times (deterministic quit trigger);
  - `_physics_process` fires >= 1 time (host-timing dependent count — documented)
    and every physics tick captures `pd == 1/60` (fixed timestep, deterministic);
  - each `_process` frame captures a Float `delta` (value host-dependent, >= 0);
  - callback ORDER: _init < _enter_tree < _ready < first _process < _exit_tree;
  - `_notification` captures the Int `what`, and the KEY lifecycle codes
    PARENTED(18) < ENTER_TREE(10) < READY(13) < first PROCESS(17)/PHYSICS(16)
    < EXIT_TREE(11) are present in that capture order.

It EXITS NONZERO on any mismatch.

Usage:
  verify_gf11.py verify <full.json>
  verify_gf11.py tamper <full.json> <mode>   # missingcallback|proccount|notifcode|order
"""
import json
import sys

NODE_PATH = "res://gf_node.gd"
DRIVER_PATH = "res://gf_node_main.gd"
EXPECTED_TYPES = ["None", "Int", "Float", "Bool", "String", "Variant", "Object"]
PHYSICS_STEP = 1.0 / 60.0

# Node notification codes (scene/main/node.h).
PARENTED = 18
ENTER_TREE = 10
READY = 13
PHYSICS_PROC = 16
PROCESS = 17
EXIT_TREE = 11


class VerifyError(Exception):
    pass


def load(path):
    with open(path) as f:
        return json.load(f)


def step_index_map(doc):
    return {s["step_index"]: s for s in doc["events"] if s["kind"] == "step"}


def calls(doc):
    return [e for e in doc["events"] if e["kind"] == "call_entry"]


def frame_path(doc, steps, ce):
    """Attribute a frame to its source file via the path of its steps (NOT the
    function name, which the SceneTree driver shares for `_process`)."""
    s = steps.get(ce["entry_step"])
    if s is not None:
        return s["path"]
    # fall back: first step within [entry, exit] carrying this function
    for si in range(ce["entry_step"], ce["exit_step"] + 1):
        s = steps.get(si)
        if s is not None:
            return s["path"]
    return "?"


def node_frames(doc, steps, fn):
    return sorted(
        [c for c in calls(doc)
         if c["function"] == fn and frame_path(doc, steps, c) == NODE_PATH],
        key=lambda c: c["entry_step"],
    )


def local_on_frame(steps, ce, name):
    for si in range(ce["entry_step"], ce["exit_step"] + 1):
        s = steps.get(si)
        if not s:
            continue
        for v in s.get("vars", []):
            if v["varname"] == name:
                return v
    return None


def verify(doc):
    steps = step_index_map(doc)

    # --- 1. every lifecycle callback recorded as a NODE frame --------------
    singles = {
        "_init": node_frames(doc, steps, "_init"),
        "_enter_tree": node_frames(doc, steps, "_enter_tree"),
        "_ready": node_frames(doc, steps, "_ready"),
        "_exit_tree": node_frames(doc, steps, "_exit_tree"),
    }
    for fn, fr in singles.items():
        if len(fr) != 1:
            raise VerifyError(f"expected exactly 1 node `{fn}` frame, got {len(fr)}")

    proc = node_frames(doc, steps, "_process")
    phys = node_frames(doc, steps, "_physics_process")
    notif = node_frames(doc, steps, "_notification")

    # --- 2. _process EXACTLY 3 (deterministic), _physics_process >= 1 ------
    if len(proc) != 3:
        raise VerifyError(f"node `_process` must fire exactly 3 times, got {len(proc)}")
    if len(phys) < 1:
        raise VerifyError(f"node `_physics_process` must fire >= 1 time, got {len(phys)}")
    if len(notif) < 6:
        raise VerifyError(f"node `_notification` must fire >= 6 times, got {len(notif)}")

    # --- 3. captured delta: _process Float>=0 ; _physics_process == 1/60 ----
    for f in proc:
        v = local_on_frame(steps, f, "d")
        if v is None or v["type_name"] != "Float":
            raise VerifyError("_process frame missing Float local `d` (delta)")
        if v["value"].get("f", -1.0) < 0.0:
            raise VerifyError(f"_process delta negative: {v['value']}")
    for f in phys:
        v = local_on_frame(steps, f, "pd")
        if v is None or v["type_name"] != "Float":
            raise VerifyError("_physics_process frame missing Float local `pd` (delta)")
        if abs(v["value"].get("f", 0.0) - PHYSICS_STEP) > 1e-9:
            raise VerifyError(f"_physics_process delta != 1/60: {v['value']}")

    # --- 4. callback ORDERING: init < enter < ready < first process < exit --
    e_init = singles["_init"][0]["entry_step"]
    e_enter = singles["_enter_tree"][0]["entry_step"]
    e_ready = singles["_ready"][0]["entry_step"]
    e_exit = singles["_exit_tree"][0]["entry_step"]
    first_proc = proc[0]["entry_step"]
    first_phys = phys[0]["entry_step"]
    if not (e_init < e_enter < e_ready):
        raise VerifyError(
            f"lifecycle order _init<_enter_tree<_ready violated: "
            f"{e_init},{e_enter},{e_ready}")
    if not (e_ready < min(first_proc, first_phys)):
        raise VerifyError("_ready must precede the first _process/_physics_process")
    if not (max(f["entry_step"] for f in proc + phys) < e_exit):
        raise VerifyError("_exit_tree must follow all _process/_physics_process frames")

    # --- 5. _notification captures Int `what`; key codes present + ordered --
    captured = []  # (entry_step, code)
    for f in notif:
        v = local_on_frame(steps, f, "n")
        if v is None or v["type_name"] != "Int":
            raise VerifyError("_notification frame missing Int local `n` (what)")
        captured.append((f["entry_step"], v["value"].get("i")))
    codes = [c for _, c in captured]
    for key, label in [(PARENTED, "PARENTED"), (ENTER_TREE, "ENTER_TREE"),
                       (READY, "READY"), (EXIT_TREE, "EXIT_TREE")]:
        if key not in codes:
            raise VerifyError(f"notification {label}={key} not captured; got {codes}")
    if PROCESS not in codes and PHYSICS_PROC not in codes:
        raise VerifyError(f"neither PROCESS(17) nor PHYSICS_PROCESS(16) captured; got {codes}")

    def first_step_of(code):
        for st, c in captured:
            if c == code:
                return st
        raise VerifyError(f"notification code {code} absent")
    s_par = first_step_of(PARENTED)
    s_ent = first_step_of(ENTER_TREE)
    s_rdy = first_step_of(READY)
    s_tick = min([st for st, c in captured if c in (PROCESS, PHYSICS_PROC)])
    s_exit = first_step_of(EXIT_TREE)
    if not (s_par < s_ent < s_rdy < s_tick < s_exit):
        raise VerifyError(
            f"notification order PARENTED<ENTER_TREE<READY<tick<EXIT_TREE violated: "
            f"{s_par},{s_ent},{s_rdy},{s_tick},{s_exit}")

    # --- 6. types table --------------------------------------------------
    if doc["types"] != EXPECTED_TYPES:
        raise VerifyError(f"types table mismatch: {doc['types']}")

    print(
        "GF11 verify OK: node lifecycle frames "
        f"_init/_enter_tree/_ready/_exit_tree x1, _process x3 (delta Float>=0), "
        f"_physics_process x{len(phys)} (>=1, pd==1/60), _notification x{len(notif)}; "
        "order _init<_enter_tree<_ready<_process<_exit_tree; "
        "notif codes PARENTED(18)<ENTER_TREE(10)<READY(13)<tick(16/17)<EXIT_TREE(11); "
        "types [None,Int,Float,Bool,String,Variant,Object]")


def _del_first_call(doc, steps, fn):
    for i, e in enumerate(doc["events"]):
        if (e["kind"] == "call_entry" and e["function"] == fn
                and frame_path(doc, steps, e) == NODE_PATH):
            del doc["events"][i]
            return True
    return False


def tamper(doc, mode):
    steps = step_index_map(doc)
    if mode == "missingcallback":
        if not _del_first_call(doc, steps, "_ready"):
            raise SystemExit("tamper setup failed: no _ready node frame")
    elif mode == "proccount":
        if not _del_first_call(doc, steps, "_process"):
            raise SystemExit("tamper setup failed: no _process node frame")
    elif mode == "notifcode":
        # Corrupt the captured ENTER_TREE(10) code so the key-code check fails.
        done = False
        for f in node_frames(doc, steps, "_notification"):
            v = local_on_frame(steps, f, "n")
            if v is not None and v["value"].get("i") == ENTER_TREE:
                v["value"]["i"] = 9999
                done = True
                break
        if not done:
            raise SystemExit("tamper setup failed: no ENTER_TREE capture")
    elif mode == "order":
        # Move _exit_tree before _ready -> ordering check fails.
        exit_fr = node_frames(doc, steps, "_exit_tree")[0]
        ready_fr = node_frames(doc, steps, "_ready")[0]
        exit_fr["entry_step"], ready_fr["entry_step"] = (
            ready_fr["entry_step"], exit_fr["entry_step"])
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
            print(f"GF11 verify FAILED: {e}", file=sys.stderr)
            raise SystemExit(1)
    elif cmd == "tamper":
        tamper(doc, sys.argv[3])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
