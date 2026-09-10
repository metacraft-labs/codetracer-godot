/* CodeTracer HCR demo — HCR1 patch bodies.
 *
 * Compiled to a real ELF relocatable object; the coordinator extracts each
 * function's bytes from that object's own `.text.<name>` section and sends them
 * over the agent wire. Nothing here is hand-assembled and nothing is a literal
 * byte string in a script.
 *
 * ABI. Both bodies replace `int CoreBind::OS::get_processor_count() const`,
 * which takes `this` in RDI and returns `int` in EAX. A `long f(void)` that
 * ignores RDI and leaves its result in EAX/RAX is ABI-compatible with that:
 * ignoring an incoming register argument is always safe on SysV x86_64.
 *
 * NO RELOCATIONS. The provider copies these bytes into a page it owns and jumps
 * to them, so anything the linker would have had to fix up would be fixed up
 * nowhere. Both bodies are therefore built only out of immediates and
 * registers. `hcr1_patch_driver.nim` refuses to send a body that carries a
 * relocation, so this is checked rather than asserted.
 */

/* The main demo. 4242 is arbitrary and that is the point: it cannot be confused
 * with a real processor count, so a run that prints it has been patched. */
int hcr1_patch_processor_count(void) {
	return 4242;
}

/* The thread-reachability probe. Returns the caller's kernel thread id, so the
 * value printed by GDScript names the thread that executed the patched body.
 *
 * Written as inline asm rather than as a call to glibc's `gettid()` because a
 * call would emit `R_X86_64_PLT32` against `gettid`, and a relocated body is
 * not something that can be dropped into a provider page. The syscall number is
 * an immediate, so this assembles to seven bytes with an empty relocation
 * table.
 */
long hcr1_patch_gettid(void) {
	long result;
	__asm__ volatile("syscall"
					 : "=a"(result)
					 : "a"(186L) /* __NR_gettid on x86_64 */
					 : "rcx", "r11", "memory");
	return result;
}
