# CodeTracer HCR demo — HCR1: a Godot engine function replaced while the engine runs.
#
# The program prints one line per tick and then quits. Each line carries the
# value of a NATIVE engine function, `CoreBind::OS::get_processor_count()`,
# reached from GDScript through the ordinary method-bind path:
#
#   VM opcode CALL -> MethodBind -> CoreBind::OS::get_processor_count() const
#                                -> ::OS::get_singleton()->get_processor_count()
#
# A coordinator publishes a direct entry patch into that function part-way
# through the run. The observable is FIRST-PRINCIPLES and needs no tooling to
# read: the printed number is the host's real processor count before the patch
# and the patch body's literal 4242 after it, in one process, with no restart.
#
#   CT_HCR_TICK=1 CT_HCR_VALUE=24
#   ...
#   CT_HCR_TICK=13 CT_HCR_VALUE=4242      <- the patch landed here
#   ...
#
# WHY THIS FUNCTION. Three properties, each of which had to hold:
#
#   * it is reached through a MEMBER-FUNCTION POINTER stored in a MethodBind,
#     so GCC cannot inline it at the call site and the out-of-line body that
#     gets patched is the body that actually runs;
#   * it returns a plain `int` in EAX, so a patch body of `int f(void)` is ABI-
#     compatible with it and needs no argument handling;
#   * its unpatched value is a fact about the host, so a run that printed 4242
#     from the start would be visibly wrong rather than plausibly right.
#
# WHY THE SLEEP. `OS.delay_msec` keeps the run long enough for a patch to land
# in the middle of it, which is the entire point: the same call site must be
# observed BEFORE and AFTER. A run that only ever showed the patched value
# would not distinguish "hot patch" from "built that way".
#
# The value is NOT hardcoded here on purpose. The expected pre-patch value is
# the host's processor count, which the driver reads independently from
# `nproc`; the expected post-patch value is the literal the patch body returns,
# which the driver reads independently out of the patch object's own bytes.

extends MainLoop

const TICKS := 40
const TICK_DELAY_MSEC := 250

var tick := 0

func _process(_delta):
	tick += 1
	print("CT_HCR_TICK=%d CT_HCR_VALUE=%d" % [tick, OS.get_processor_count()])
	OS.delay_msec(TICK_DELAY_MSEC)
	return tick >= TICKS
