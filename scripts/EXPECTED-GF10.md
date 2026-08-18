# EXPECTED — GF10 (Coroutines & `await`, async-continuation integration)

Hand-derived, first-principles expectations for
`test-programs/gdscript/gf_coroutine.gd`, recorded headless by the patched
engine and decoded via `ct-print --full`. Asserted by `scripts/verify_gf10.py`
(recorder side) and by the db-backend integration test
`codetracer/src/db-backend/tests/verify_gdscript_await_continuation_link.rs`
(the authoritative `ContinuationLink` proof).

## The program

```gdscript
43 func work() -> int:
44     var base := 10
45     var payload: int = await go     # SUSPEND on a SIGNAL
46     var kept := base                # RESUME line — surviving base == 10
47     var result := kept + payload    # 42
48     return result
50 func _initialize() -> void:
51     var r: int = await work()       # SUSPEND on a COROUTINE call
52     var check := r                  # RESUME line — r == 42
```

`_process` emits `go` on the 2nd frame, resuming `work`; `work` returns 42, whose
`completed` signal resumes `_initialize`. Prints `CT_GF10_RESULT=42`.

## The await mechanism (why this works)

- `await <signal>` runs `OPCODE_AWAIT`: the VM builds a `GDScriptFunctionState`
  (`gdfs`), returns it as `retvalue`, sets `awaited = true`, and exits `call()`
  via the `(!p_state || awaited)` branch — which ALREADY records a balanced G3
  return. `GDScriptFunctionState::resume` re-enters `call(..., &gdfs->state)`, so
  `&gdfs->state` at suspend == `p_state` at resume: the stable **context_id**.
- `await <coroutine call>`: the callee returns a `GDScriptFunctionState`;
  `OPCODE_AWAIT` awaits its `completed` signal — same mechanism, own `CallState`.

## Recorder facts (verify_gf10.py)

- **Markers**: exactly **2 suspend** + **2 resume** async markers
  (`ct-async-{suspend,resume}:gdscript-coroutine`), one pair per await. The
  marker METADATA carries `"<CallState_ptr_hex> <step_id>"` (step_id =
  `next_step_index() - 1`, i.e. the exec-stream step the marker refers to; the
  event's implicit step lags by one because the writer FFI buffers a pending
  step).
- **Balance (open question #4)**: `work` and `_initialize` each record as **two
  balanced frames** (suspend-portion + resume-portion). Each `call()` invocation
  still runs one enter/exit pair (G3 unchanged).
- **Surviving local**: `base=10` captured pre-await; on resume `kept=base=10`
  (Godot saves/restores the coroutine stack across the yield).
- **Join**: `result=42` in `work`; `r=42` (and `check=42`) in `_initialize`.
- Types table `[None, Int, Float, Bool, String, Variant, Object]` (Object is the
  coroutine-state handle captured as the suspend-portion return value).
- `CT_GF10_RESULT=42`.

## ContinuationLink facts (db-backend integration test)

Observed via the real `MaterializedReplaySession::continuation_links()`:

- **Signal-await link (inside `work`)**: `link_type=Await`, registration =
  the `await go` step (line 45), continuation = the first resumed line (line 46,
  `kept`), context_id = `work`'s CallState pointer.
- **Coroutine-await link (inside `_initialize`)**: `link_type=Await`,
  continuation = the resume line (line 52, `check`) with `r == 42`, context_id =
  `_initialize`'s CallState pointer (distinct from `work`'s).
- `async_links_from(registration)` resolves to the continuation and
  `async_links_to(continuation)` resolves back (Async-Continuation-Algorithms.md
  §5.2), with `AsyncLinkRecord.link_type == 0` (await).

### Honest note on the coroutine-await registration step

For the nested `await work()`, the recorded registration step is the execution
point at which the await SUSPENDED — which, because the callee `work` ran (and
itself suspended) before `_initialize`'s `OPCODE_AWAIT` executed, co-locates with
`work`'s own suspend step rather than the syntactic `await work()` source line.
The two links are still correctly distinguished and paired by `context_id`
(distinct `CallState` pointers), and each continuation is strictly after its
registration. Attributing a nested coroutine-call registration to the caller's
exact `await` source line would require threading the caller frame's await-line
step through the suspend, and is a documented refinement — it does not affect the
link's identity, ordering, or pairing. The **signal-await** case has a clean
registration exactly on the `await` line.
```
