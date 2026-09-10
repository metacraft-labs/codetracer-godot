#!/usr/bin/env python3
"""Prove that a linked ELF executable is HCR-patchable-SHAPED.

Reprobuild's Linux ELF HCR provider (HLX-M0/M1) accepts a target only if three
independent properties hold, and each one fails differently:

  1. ``__patchable_function_entries`` exists, is SHF_ALLOC (the provider finds
     it at runtime through the linker-synthesised ``__start_``/``__stop_``
     symbols, which only exist for an allocated section), and its entries point
     at real NOP sleds -> otherwise every request is refused ``absent-sled``.
  2. ``.symtab`` exists and covers the binary's functions -> otherwise the
     resolver falls back to ``.dynsym`` and refuses ``elf-symbol-not-found``
     for everything that is not exported.
  3. ``.note.gnu.build-id`` exists -> otherwise HLX-M1's mandatory verification
     refuses with ``elf-build-id-absent``.

And one more that is specific to a hot-patch DEMO rather than to the provider:

  4. the in-target agent is actually linked in, i.e. ``repro_hcr_agent_*`` are
     defined here. A patchable-shaped engine with nobody listening cannot be
     patched, and nothing about its ELF shape says so.

WHY THIS PARSES ELF BY HAND. It could shell out to ``readelf``. It does not,
because the interesting check is #1's SLED CONTENT: the section holds addresses,
and the claim is about the BYTES AT those addresses. A `readelf -x` transcript
of a 100 MB binary is not a practical way to ask that, and the arithmetic
(virtual address -> file offset through the program headers) has to happen
somewhere regardless.

VACUOUS-CHECK DISCIPLINE (`codetracer-specs/Testing/Verification-Harness-Traps.md`
trap 4). Every reader here distinguishes "I read it and the answer is no" from
"I could not read it". A section that is absent is a FAILURE with its own
message; a section that is present but empty is a DIFFERENT failure; a parse
that throws is a third. None of them is allowed to become the string "0".
"""

from __future__ import annotations

import struct
import sys

# --- minimal ELF64 reader ---------------------------------------------------

ET_EXEC, ET_DYN = 2, 3
PT_LOAD = 1
SHT_SYMTAB, SHT_NOTE = 2, 7
SHF_ALLOC = 0x2
STT_FUNC = 2


class ElfError(Exception):
    """A failure to READ, never an answer about content."""


class Section:
    __slots__ = ("name", "type", "flags", "addr", "offset", "size", "entsize", "link")

    def __init__(self, *, name, type_, flags, addr, offset, size, entsize, link):
        self.name = name
        self.type = type_
        self.flags = flags
        self.addr = addr
        self.offset = offset
        self.size = size
        self.entsize = entsize
        self.link = link


class Elf64:
    def __init__(self, path: str):
        self.path = path
        with open(path, "rb") as handle:
            self.data = handle.read()
        if len(self.data) < 64 or self.data[:4] != b"\x7fELF":
            raise ElfError(f"{path}: not an ELF file")
        if self.data[4] != 2 or self.data[5] != 1:
            raise ElfError(f"{path}: not little-endian ELF64")
        (
            self.e_type,
            _machine,
            _version,
            _entry,
            e_phoff,
            e_shoff,
            _flags,
            _ehsize,
            e_phentsize,
            e_phnum,
            e_shentsize,
            e_shnum,
            e_shstrndx,
        ) = struct.unpack_from("<HHIQQQIHHHHHH", self.data, 16)

        self.segments = []
        for i in range(e_phnum):
            p_type, _pf, p_offset, p_vaddr, _pa, p_filesz, _pm, _pal = struct.unpack_from(
                "<IIQQQQQQ", self.data, e_phoff + i * e_phentsize
            )
            if p_type == PT_LOAD:
                self.segments.append((p_vaddr, p_offset, p_filesz))
        if not self.segments:
            raise ElfError(f"{path}: no PT_LOAD segments")

        if e_shoff == 0 or e_shnum == 0:
            raise ElfError(f"{path}: no section headers (fully stripped?)")

        raw = []
        for i in range(e_shnum):
            fields = struct.unpack_from("<IIQQQQIIQQ", self.data, e_shoff + i * e_shentsize)
            raw.append(fields)
        strtab_off = raw[e_shstrndx][4]
        strtab_size = raw[e_shstrndx][5]

        def sname(off: int) -> str:
            if off >= strtab_size:
                raise ElfError(f"{path}: section name offset {off} out of range")
            end = self.data.index(b"\0", strtab_off + off)
            return self.data[strtab_off + off : end].decode("utf-8", "replace")

        # Elf64_Shdr field order, spelled out because an off-by-one here is
        # SILENT: names still resolve, so "section absent" stays right while
        # every size and offset is one field late. (It was wrong once.)
        #   f[0] sh_name  f[1] sh_type   f[2] sh_flags  f[3] sh_addr
        #   f[4] sh_offset f[5] sh_size  f[6] sh_link   f[7] sh_info
        #   f[8] sh_addralign            f[9] sh_entsize
        self.sections = [
            Section(
                name=sname(f[0]),
                type_=f[1],
                flags=f[2],
                addr=f[3],
                offset=f[4],
                size=f[5],
                entsize=f[9],
                link=f[6],
            )
            for f in raw
        ]
        self.by_name = {s.name: s for s in self.sections}

    def vaddr_to_file(self, vaddr: int) -> int:
        for p_vaddr, p_offset, p_filesz in self.segments:
            if p_vaddr <= vaddr < p_vaddr + p_filesz:
                return p_offset + (vaddr - p_vaddr)
        raise ElfError(f"virtual address 0x{vaddr:x} is in no PT_LOAD segment's file image")

    def bytes_at_vaddr(self, vaddr: int, count: int) -> bytes:
        off = self.vaddr_to_file(vaddr)
        if off + count > len(self.data):
            raise ElfError(f"0x{vaddr:x}+{count} runs past end of file")
        return self.data[off : off + count]

    def symbols(self, section: Section):
        strtab = self.sections[section.link]
        entsize = section.entsize or 24
        count = section.size // entsize
        for i in range(count):
            st_name, st_info, _other, st_shndx, st_value, st_size = struct.unpack_from(
                "<IBBHQQ", self.data, section.offset + i * entsize
            )
            end = self.data.index(b"\0", strtab.offset + st_name)
            name = self.data[strtab.offset + st_name : end].decode("utf-8", "replace")
            yield name, st_info & 0xF, st_info >> 4, st_shndx, st_value, st_size


# --- checks -----------------------------------------------------------------

FAILURES: list[str] = []
NOTES: list[str] = []


def fail(msg: str) -> None:
    FAILURES.append(msg)
    print(f"CHECK-FAIL: {msg}")


def ok(msg: str) -> None:
    print(f"  ok: {msg}")


def check_build_id(elf: Elf64) -> None:
    section = elf.by_name.get(".note.gnu.build-id")
    if section is None:
        fail(
            ".note.gnu.build-id is absent -> HLX-M1 refuses every object with "
            "'elf-build-id-absent'. Link with -Wl,--build-id=sha1."
        )
        return
    if section.size == 0:
        fail(".note.gnu.build-id is present but EMPTY (0 bytes)")
        return
    namesz, descsz, ntype = struct.unpack_from("<III", elf.data, section.offset)
    desc_off = section.offset + 12 + ((namesz + 3) & ~3)
    build_id = elf.data[desc_off : desc_off + descsz].hex()
    if descsz == 0 or not build_id:
        fail(".note.gnu.build-id note carries a zero-length descriptor")
        return
    ok(f".note.gnu.build-id present: type={ntype} {descsz} bytes = {build_id}")
    NOTES.append(f"build-id={build_id}")


def check_symtab(elf: Elf64, min_funcs: int) -> None:
    section = elf.by_name.get(".symtab")
    if section is None:
        fail(
            ".symtab is absent (binary is stripped) -> the resolver can only see "
            "exported .dynsym symbols. Build with hcr_patchable=yes."
        )
        return
    if section.type != SHT_SYMTAB:
        fail(f".symtab has type {section.type}, expected SHT_SYMTAB({SHT_SYMTAB})")
        return
    total = 0
    funcs = 0
    defined_funcs = 0
    for _name, sym_type, _bind, shndx, _value, _size in elf.symbols(section):
        total += 1
        if sym_type == STT_FUNC:
            funcs += 1
            if shndx != 0:
                defined_funcs += 1
    if total == 0:
        fail(".symtab is present but holds ZERO symbols (a read that produced nothing)")
        return
    if defined_funcs < min_funcs:
        fail(
            f".symtab holds only {defined_funcs} defined FUNC symbols; expected at "
            f"least {min_funcs} for a full engine build"
        )
        return
    ok(f".symtab present: {total} symbols, {defined_funcs} defined FUNC (of {funcs} FUNC)")
    NOTES.append(f"symtab_defined_funcs={defined_funcs}")

    dynsym = elf.by_name.get(".dynsym")
    if dynsym is not None:
        dyn_defined = sum(
            1
            for _n, t, _b, sh, _v, _s in elf.symbols(dynsym)
            if t == STT_FUNC and sh != 0
        )
        ok(f".dynsym for comparison: {dyn_defined} defined FUNC (the pre-HCR ceiling)")


def check_sleds(elf: Elf64, sample: int, expect_nops: int) -> None:
    section = elf.by_name.get("__patchable_function_entries")
    if section is None:
        fail(
            "__patchable_function_entries is absent -> no NOP sleds, so every "
            "patch request is refused 'absent-sled'. Compile with "
            "-fpatchable-function-entry."
        )
        return
    if section.size == 0:
        fail("__patchable_function_entries is present but EMPTY (0 bytes)")
        return
    if not (section.flags & SHF_ALLOC):
        fail(
            "__patchable_function_entries is NOT SHF_ALLOC, so it is not mapped at "
            "runtime and the linker-synthesised __start_/__stop_ symbols the "
            "provider reads do not exist"
        )
        return
    if section.size % 8 != 0:
        fail(f"__patchable_function_entries size {section.size} is not a multiple of 8")
        return
    count = section.size // 8
    ok(
        f"__patchable_function_entries present: SHF_ALLOC, addr=0x{section.addr:x}, "
        f"{section.size} bytes = {count} entries"
    )
    NOTES.append(f"sled_entries={count}")

    # The provider needs the __start_/__stop_ pair to exist as real symbols.
    symtab = elf.by_name.get(".symtab")
    if symtab is not None:
        wanted = {
            "__start___patchable_function_entries",
            "__stop___patchable_function_entries",
        }
        found = {n for n, _t, _b, sh, _v, _s in elf.symbols(symtab) if n in wanted and sh != 0}
        missing = wanted - found
        if missing:
            fail(f"linker did not synthesise: {sorted(missing)}")
        else:
            ok("__start___patchable_function_entries / __stop___ both defined")

    # Now the part that actually matters: the BYTES the entries point at.
    entries = struct.unpack_from(f"<{count}Q", elf.data, section.offset)
    # Sample evenly across the whole section rather than the first N, so a
    # partial failure late in the link cannot hide behind a good prefix.
    step = max(1, count // sample)
    indices = list(range(0, count, step))[:sample]
    checked = 0
    bad_nop = 0
    bad_window = 0
    unreadable = 0
    first_shown = 0
    for i in indices:
        addr = entries[i]
        if addr == 0:
            continue
        try:
            body = elf.bytes_at_vaddr(addr, expect_nops)
        except ElfError as exc:
            unreadable += 1
            if unreadable <= 3:
                print(f"  !! entry[{i}] 0x{addr:x}: {exc}")
            continue
        checked += 1
        # GCC's -fpatchable-function-entry=N,0 emits N single-byte 0x90 NOPs.
        if body != b"\x90" * expect_nops:
            bad_nop += 1
            if bad_nop <= 3:
                print(f"  !! entry[{i}] 0x{addr:x}: sled is {body.hex()}, expected {expect_nops}x 90")
            continue
        # The publication rule (design §4.2): a 5-byte E9 rel32 must fit inside a
        # single naturally aligned 8-byte window that lies wholly in the sled.
        window = (addr + 7) & ~7
        if window + 8 > addr + expect_nops:
            bad_window += 1
            if bad_window <= 3:
                print(
                    f"  !! entry[{i}] 0x{addr:x}: no aligned 8-byte window inside the "
                    f"{expect_nops}-byte sled (first aligned = 0x{window:x})"
                )
            continue
        if first_shown < 3:
            first_shown += 1
            print(
                f"  sled sample: entry[{i}] sled@0x{addr:x} "
                f"aligned-window@0x{window:x} (+{window - addr}) "
                f"sled_bytes={body.hex()}"
            )

    if checked == 0:
        fail(
            f"sampled {len(indices)} sled entries and could read NONE of them "
            "-- this is a reader failure, not a verdict"
        )
        return
    if bad_nop or bad_window or unreadable:
        fail(
            f"of {len(indices)} sampled sleds: {bad_nop} not all-NOP, "
            f"{bad_window} with no aligned 8-byte window, {unreadable} unreadable"
        )
        return
    ok(
        f"sampled {checked} sleds across the whole section: every one is "
        f"{expect_nops} NOP bytes with an 8-byte-aligned window inside it"
    )


def check_agent(elf: Elf64) -> None:
    symtab = elf.by_name.get(".symtab")
    if symtab is None:
        fail("cannot look for the agent: no .symtab")
        return
    # The PUBLIC entry points, from `repro_hcr_agent.h`. Everything else in the
    # agent is `static` and therefore an `STB_LOCAL` symbol at best -- asking
    # for `repro_hcr_apply_direct_patch` here was wrong, and wrong in the
    # instructive direction: it reported a correctly-linked engine as unlinked.
    public = [
        "repro_hcr_agent_start_from_env",
        "repro_hcr_agent_start_polling_from_env",
        "repro_hcr_agent_default_support_profile",
    ]
    # Presence of the public API is not enough: those three exist on every
    # platform, including one where the direct-patch arm compiled to a stub.
    # These two are `static` symbols that only exist when the LINUX x86_64
    # provider arm was really compiled in -- `repro_hcr_lx_*` is HLX-M0's
    # trampoline provider and `repro_hcr_elf_*` is HLX-M1's resolver. They are
    # STB_LOCAL, which is exactly why an unstripped build is required to see
    # them at all.
    linux_arm = ["repro_hcr_lx_probe_capabilities", "repro_hcr_elf_scan_symbol_table"]
    defined = {
        n
        for n, _t, _b, sh, _v, _s in elf.symbols(symtab)
        if n in set(public) | set(linux_arm) and sh != 0
    }
    missing_public = [w for w in public if w not in defined]
    if missing_public:
        fail(
            f"the in-target HCR agent is NOT linked in (missing: {missing_public}). "
            "The engine is patchable-shaped but nothing listens for a patch."
        )
        return
    missing_arm = [w for w in linux_arm if w not in defined]
    if missing_arm:
        fail(
            f"the agent is linked but its Linux x86_64 provider arm is not "
            f"(missing: {missing_arm}); the direct-patch path is a stub here"
        )
        return
    ok(f"in-target HCR agent linked, Linux x86_64 arm compiled in: {sorted(defined)}")


def check_cf_protection(elf: Elf64) -> None:
    """Informational, not a gate. -fcf-protection changes the sled OFFSET (the
    section entry points past `endbr64`), which HLX-M0's e2e gate asserts as
    +4. Report which regime this binary is in so a sled offset of 0 vs 4 is
    never a surprise."""
    section = elf.by_name.get(".note.gnu.property")
    if section is None:
        print("  info: no .note.gnu.property -> no IBT/SHSTK, sled starts at the entry label")
        NOTES.append("cf_protection=absent")
        return
    # PRESENCE OF THE SECTION IS NOT THE ANSWER. GCC emits .note.gnu.property
    # for plain x86 ISA-used properties with no CET bits at all, so "the section
    # exists" and "-fcf-protection is on" are different claims. Parse the
    # GNU_PROPERTY_X86_FEATURE_1_AND (0xc0000002) entry and read IBT/SHSTK.
    GNU_PROPERTY_X86_FEATURE_1_AND = 0xC0000002
    IBT, SHSTK = 0x1, 0x2
    features = 0
    off = section.offset
    end = section.offset + section.size
    try:
        while off + 12 <= end:
            namesz, descsz, _ntype = struct.unpack_from("<III", elf.data, off)
            desc = off + 12 + ((namesz + 3) & ~3)
            stop = desc + descsz
            while desc + 8 <= stop:
                ptype, psize = struct.unpack_from("<II", elf.data, desc)
                pdata = desc + 8
                if ptype == GNU_PROPERTY_X86_FEATURE_1_AND and psize >= 4:
                    features |= struct.unpack_from("<I", elf.data, pdata)[0]
                desc = pdata + ((psize + 7) & ~7)
            off = stop + ((-stop) % 4)
    except struct.error as exc:
        print(f"  info: .note.gnu.property present but unparsable ({exc}); treating as unknown")
        NOTES.append("cf_protection=unparsable")
        return
    if features & (IBT | SHSTK):
        print(
            f"  info: CET in play (X86_FEATURE_1_AND=0x{features:x}: "
            f"{'IBT ' if features & IBT else ''}{'SHSTK' if features & SHSTK else ''}) "
            "-> expect endbr64 at the entry and the sled at entry+4"
        )
        NOTES.append(f"cf_protection=cet(0x{features:x})")
    else:
        print(
            f"  info: .note.gnu.property present ({section.size} bytes) but carries NO "
            "IBT/SHSTK -> no endbr64, sled starts at the entry label"
        )
        NOTES.append("cf_protection=no-cet")


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: verify_hcr_patchable.py <elf> [--min-funcs N] [--sample N] [--nops N]")
        return 2
    path = sys.argv[1]
    min_funcs = 10000
    sample = 400
    nops = 16
    args = sys.argv[2:]
    for i, arg in enumerate(args):
        if arg == "--min-funcs":
            min_funcs = int(args[i + 1])
        elif arg == "--sample":
            sample = int(args[i + 1])
        elif arg == "--nops":
            nops = int(args[i + 1])

    print(f"verifying HCR-patchable shape: {path}")
    try:
        elf = Elf64(path)
    except (OSError, ElfError) as exc:
        print(f"CHECK-FAIL: cannot read the binary at all: {exc}")
        return 1

    print(f"  ELF type: {'ET_DYN (PIE)' if elf.e_type == ET_DYN else f'type {elf.e_type}'}")
    check_build_id(elf)
    check_symtab(elf, min_funcs)
    check_sleds(elf, sample, nops)
    check_agent(elf)
    check_cf_protection(elf)

    print()
    if FAILURES:
        print(f"NOT PATCHABLE-SHAPED: {len(FAILURES)} check(s) failed")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("PATCHABLE-SHAPED: all checks passed")
    if NOTES:
        print("  " + "  ".join(NOTES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
