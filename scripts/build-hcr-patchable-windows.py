#!/usr/bin/env python3
"""Build the rendering-capable Windows x86_64 HCR Godot variant.

The profile comes from reprobuild's emitter; this script does not duplicate a
single MSVC or LINK flag. It initializes the real Visual Studio environment,
keeps the full PDB, and selects a distinct SCons suffix so ordinary Godot
artifacts are never overwritten.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


REPO = Path(__file__).resolve().parents[1]


def checked(args: list[str], cwd: Path, env: dict[str, str]) -> str:
    process = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if process.returncode != 0:
        raise RuntimeError(
            f"command failed with exit {process.returncode}: "
            f"{subprocess.list2cmdline(args)}\n{process.stdout}"
        )
    return process.stdout


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--reprobuild",
        type=Path,
        default=REPO.parent / "reprobuild",
    )
    parser.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    parser.add_argument("--suffix", default="hcrwin")
    parser.add_argument(
        "--target",
        choices=("template_debug", "template_release"),
        default="template_debug",
        help=(
            "Godot runtime profile. Both variants retain the full PDB and "
            "consume the same Windows HCR patchability profile."
        ),
    )
    parser.add_argument("--vulkan", choices=("yes", "no"), default="yes")
    parser.add_argument(
        "--recording-headless",
        action="store_true",
        help=(
            "build the HWG-M5 runtime: no Vulkan/glslang or unused GUI, "
            "physics, navigation, and XR subsystems"
        ),
    )
    parser.add_argument("scons_args", nargs="*")
    args = parser.parse_args()
    if sys.platform != "win32":
        raise RuntimeError("the Windows HCR Godot recipe requires Windows")
    if not args.reprobuild.is_dir():
        raise RuntimeError(f"reprobuild checkout not found: {args.reprobuild}")
    sys.path.insert(0, str(args.reprobuild / "libs" / "repro_hcr_agent"))
    from build_windows_agent import visual_studio_environment

    env = visual_studio_environment()
    # Some constrained agent launchers omit Windows' conventional folder
    # variables even though vcvars64 succeeds. SCons uses these variables to
    # locate vswhere before it considers the already-initialized VC paths.
    system_drive = Path(env.get("SystemDrive", "C:") + "\\")
    env.setdefault("ProgramFiles", str(system_drive / "Program Files"))
    env.setdefault("ProgramW6432", str(system_drive / "Program Files"))
    env.setdefault("ProgramFiles(x86)", str(system_drive / "Program Files (x86)"))
    # Path.home() can fail under the deliberately sparse environment used by
    # the HCR integration launcher.  LOCALAPPDATA is only a cache root for the
    # tools involved here, so give it a deterministic build-local fallback.
    local_app_data = REPO / "build" / "hcr-windows-localappdata"
    local_app_data.mkdir(parents=True, exist_ok=True)
    env.setdefault("LOCALAPPDATA", str(local_app_data))
    nim = shutil.which("nim.exe", path=env.get("PATH"))
    if nim is None:
        raise RuntimeError("nim.exe is required to build the profile emitter")
    profile_dir = REPO / "build" / "hcr-windows-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    emitter = profile_dir / "hcr_patchable_profile.exe"
    checked(
        [
            nim,
            "c",
            "-d:release",
            "--hints:off",
            "--warnings:off",
            f"--nimcache:{profile_dir / 'nimcache'}",
            f"--out:{emitter}",
            str(args.reprobuild / "scripts" / "hcr_patchable_profile.nim"),
        ],
        args.reprobuild,
        env,
    )
    profile = json.loads(checked([str(emitter), "--format=json"], REPO, env))
    if profile.get("supportProfile") != "windows-x86_64-msvc-pe-direct-hcr-v1":
        raise RuntimeError(f"unexpected HCR profile: {profile!r}")
    compile_flags = profile.get("compileFlags")
    link_flags = profile.get("linkFlags")
    if not compile_flags or not link_flags:
        raise RuntimeError("the Windows HCR profile emitted an empty flag set")
    env["HCR_PATCHABLE_CCFLAGS"] = " ".join(compile_flags)
    env["HCR_PATCHABLE_LINKFLAGS"] = " ".join(link_flags)

    scons = shutil.which("scons.exe", path=env.get("PATH")) or shutil.which(
        "scons", path=env.get("PATH")
    )
    if scons:
        command = [scons]
    elif importlib.util.find_spec("SCons") is not None:
        command = [sys.executable, "-m", "SCons"]
    else:
        raise RuntimeError(
            "SCons is unavailable. Install the Godot build dependency with "
            f"'{sys.executable} -m pip install scons'."
        )
    vulkan = "no" if args.recording_headless else args.vulkan
    command.extend(
        [
            f"-j{args.jobs}",
            "platform=windows",
            f"target={args.target}",
            "arch=x86_64",
            "debug_symbols=yes",
            "hcr_patchable=yes",
            f"extra_suffix={args.suffix}",
            f"vulkan={vulkan}",
            "opengl3=no",
            "d3d12=no",
            "winrt=no",
            "accesskit=no",
            # The HCR demo is a purpose-built runtime, not the editor. Keeping
            # unrelated import/codec/network modules out materially reduces
            # both the patchable image and the recorder's mandatory pristine
            # PE control-flow scan. GDScript is the sole module the demo uses;
            # rendering, scenes, GDExtension, and PNG capture are engine/
            # driver facilities rather than optional modules.
            "modules_enabled_by_default=no",
            "module_gdscript_enabled=yes",
            "disable_path_overrides=no",
        ]
    )
    if args.recording_headless:
        command.extend(
            [
                "disable_advanced_gui=yes",
                "disable_physics_2d=yes",
                "disable_physics_3d=yes",
                "disable_navigation_2d=yes",
                "disable_navigation_3d=yes",
                "disable_xr=yes",
            ]
        )
    if vulkan == "yes":
        # Vulkan's runtime shader compiler is a module rather than an
        # engine/driver facility. A pruned rendering build without it links
        # and starts, but every RD shader compilation is refused. The headless
        # MCR variant deliberately omits both Vulkan and this module so its
        # startup does not execute renderer work that the recorded scene never
        # observes.
        command.append("module_glslang_enabled=yes")
    command.extend(args.scons_args)
    output = checked(command, REPO, env)
    build_log = REPO / "build" / f"hcr-windows-{args.suffix}.log"
    build_log.write_text(output, encoding="utf-8")

    stem = f"godot.windows.{args.target}.x86_64.{args.suffix}"
    image = REPO / "bin" / f"{stem}.exe"
    pdb = REPO / "bin" / f"{stem}.pdb"
    if not image.is_file() or image.stat().st_size == 0:
        raise RuntimeError(f"SCons succeeded but the engine is absent: {image}")
    if not pdb.is_file() or pdb.stat().st_size == 0:
        raise RuntimeError(f"SCons succeeded but the full PDB is absent: {pdb}")
    print(
        json.dumps(
            {
                "schemaId": "codetracer.godot.hcr-windows-build.v1",
                "supportProfile": profile["supportProfile"],
                "compileFlags": compile_flags,
                "linkFlags": link_flags,
                "image": str(image),
                "pdb": str(pdb),
                "buildLog": str(build_log),
                "vulkan": vulkan == "yes",
                "runtimeProfile": (
                    "recording-headless" if args.recording_headless else "rendering"
                ),
                "target": args.target,
            }
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as failure:
        print(f"build-hcr-patchable-windows: {failure}", file=sys.stderr)
        raise SystemExit(1)
