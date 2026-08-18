# EXPECTED — GF11 (Node lifecycle callbacks)

Hand-derived, first-principles expectations for
`test-programs/gdscript/gf_node.gd` (a plain `extends Node`) driven through its
lifecycle by `test-programs/gdscript/gf_node_main.gd` (`extends SceneTree`),
recorded headless by the patched engine and decoded via `ct-print --full`.
Asserted by `scripts/verify_gf11.py`.

These facts are derived BEFORE looking at recorder output (the exact multiplicity
of `_process`, and the fixed physics timestep, follow from the Godot main-loop
model; only `_physics_process` MULTIPLICITY is host-timing dependent, documented
below as a limitation with a `>= 1` bound).

## The programs

`gf_node.gd` — the lifecycle Node (every callback on its own line, each with a
deterministic member-counter side effect, each reading its argument into a stack
local so the value is captured on that callback's step):

```gdscript
func _init():                  init_count += 1
func _enter_tree():            enter_count += 1
func _ready():                 ready_count += 1
func _process(delta):          var d := delta;  process_count += 1
func _physics_process(delta):  var pd := delta; physics_count += 1
func _notification(what):      var n := what;   last_notif = n
func _exit_tree():             exit_count += 1
```

`gf_node_main.gd` — the SceneTree driver:

```gdscript
func _initialize():        node = GfNode.new(); get_root().add_child(node)
func _process(_delta):     if node.process_count >= 3: remove_child; free; quit
```

Prints `CT_GF11_PROC=3`, `CT_GF11_PHYS=<n>=5 on this host`,
`CT_GF11_RESULT=7` (`= init+enter+ready+process+exit = 1+1+1+3+1`).

## Why every engine-invoked callback is recorded as a FRAME (no engine change)

`_init`/`_enter_tree`/`_ready`/`_process`/`_physics_process`/`_notification`/
`_exit_tree` are invoked BY THE ENGINE (SceneTree, not GDScript source), but each
still enters `GDScriptFunction::call`, so the G3 call/return hook records each as
a balanced FRAME and the G4 assign hook captures the argument read into a local.
GF11 is therefore a COVERAGE milestone — the recorder binary is byte-identical to
GF10 (GF10..G2 regressions re-verified green). Confirmed empirically: the
GF10-era binary records the full lifecycle with no new hook.

## Node-frame facts (path `res://gf_node.gd`, distinct from the driver)

Both files define `_process` (the driver's tick callback and the node's
lifecycle callback share the name), so frames are attributed to the NODE by the
source path of their steps — not by function name.

- **Frame multiplicity** (node):
  - `_init` = **1**, `_enter_tree` = **1**, `_ready` = **1**, `_exit_tree` = **1**
  - `_process` = **exactly 3** (deterministic: the driver quits the instant the
    node's `_process` counter reaches `TARGET_PROCESS = 3`, and the node's
    `_process` runs at most once per idle frame, so it can never exceed 3).
  - `_physics_process` = **>= 1** (host-timing dependent — see Limitation).
  - `_notification` = **>= 6** (one per Node notification; the exact count is
    host-timing dependent because PROCESS/PHYSICS notifications accompany each
    tick — only the presence + ordering of the KEY codes is asserted).

- **Ordering** (by frame entry step): `_init` < `_enter_tree` < `_ready` <
  first `_process` (and first `_physics_process`) < `_exit_tree`, and `_exit_tree`
  is the LAST node lifecycle frame. This is the engine's node lifecycle:
  construct -> enter tree -> ready -> per-frame ticks -> exit tree.

- **Captured `delta` (Float)**:
  - Each node `_process` frame captures local `d` of type **Float**, value
    `>= 0.0` (the idle delta varies with host frame time — NOT asserted exact).
  - Each node `_physics_process` frame captures local `pd` = **1/60**
    (`0.016666...`, Float) — the fixed physics timestep
    (`physics_ticks_per_second = 60`) IS deterministic and asserted exactly.

- **Captured `_notification` `what` (Int)**: each `_notification` frame captures
  local `n` (Int) = the notification code. The KEY lifecycle codes are all
  present, in this relative capture order:
  - `NOTIFICATION_PARENTED = 18` (on `add_child`)
  - `NOTIFICATION_ENTER_TREE = 10`
  - `NOTIFICATION_READY = 13`
  - `NOTIFICATION_PHYSICS_PROCESS = 16` / `NOTIFICATION_PROCESS = 17` (per tick)
  - `NOTIFICATION_EXIT_TREE = 11` (on `remove_child`)
  Ordering asserted: step(18) < step(10) < step(13) < step(first 16|17) < step(11).
  (The trace also carries engine-internal codes 1002/2010/27 and the terminal
  19/1/3 — not asserted, since their presence/order is engine-internal.)

- **Types table**: `[None, Int, Float, Bool, String, Variant, Object]` (the base
  scalar set; Object appears from an engine-captured handle, as in GF10).

## Limitation (honest): `_physics_process` multiplicity is host-timing dependent

Godot's headless main loop advances physics by accumulating REAL elapsed wall
time and running `floor(accumulated / physics_step)` fixed-timestep ticks per
idle frame (capped by `max_physics_steps_per_frame`). The NUMBER of physics ticks
between `_ready` and the quit therefore depends on how fast the host runs the few
idle frames — it is NOT a first-principles constant (observed 5 on this host).
The recorder captures EVERY physics tick that fires; the verifier asserts
`_physics_process` fired `>= 1` time (the milestone's own bound) and that each
captured `pd == 1/60`. `_process` multiplicity, by contrast, IS made
deterministic by using the node's process counter as the quit trigger. No
callback is unable to fire headlessly — all seven fire; only the physics-tick
COUNT is unbounded-but->=1.

## Non-vacuity (tamper runs — each MUST be rejected)

- `missingcallback` — drop the `_ready` frame -> "callbacks present" fails.
- `proccount` — drop one node `_process` frame (3 -> 2) -> exact-count fails.
- `notifcode` — corrupt the captured `ENTER_TREE=10` code -> key-code fails.
- `order` — move `_exit_tree` before `_ready` -> lifecycle ordering fails.
