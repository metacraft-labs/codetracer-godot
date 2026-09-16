#!/usr/bin/env python3
"""HWG-M2 real Windows Godot injection and named-pipe lifecycle gate.

``allowed_mocks: none``. This gate launches the real patchable Godot PE through
the canonical reprobuild DLL injector, drives the production coordinator over
the production named pipe, and requires a GDScript marker to advance after the
protocol settles. The control launches the same engine without injection and
requires the pipe lookup to fail by name.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


REPO = Path(__file__).resolve().parents[1]


def wait_marker(path: Path, minimum_frames: int, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            if path.is_file() and path.stat().st_size:
                last = json.loads(path.read_text(encoding="utf-8"))
                if int(last["frames"]) >= minimum_frames:
                    return last
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(0.025)
    raise AssertionError(
        f"Godot marker did not reach frame {minimum_frames}; last={last!r}"
    )


def stop_process(process_id: int, stop: Path) -> None:
    stop.touch()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(0x00100001, False, process_id)
    if not handle:
        return
    try:
        if kernel32.WaitForSingleObject(handle, 10_000) != 0:
            kernel32.TerminateProcess(handle, 9)
            kernel32.WaitForSingleObject(handle, 5_000)
    finally:
        kernel32.CloseHandle(handle)


def run_checked(args: list[str], cwd: Path, env: dict[str, str]) -> str:
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
    parser.add_argument("--engine", type=Path)
    parser.add_argument("--reprobuild", type=Path, default=REPO.parent / "reprobuild")
    parser.add_argument("--suffix", default="hcrwin")
    args = parser.parse_args()
    if sys.platform != "win32":
        raise RuntimeError("HWG-M2 attach verification requires Windows")
    engine = args.engine or (
        REPO
        / "bin"
        / f"godot.windows.template_debug.x86_64.{args.suffix}.exe"
    )
    pdb = engine.with_suffix(".pdb")
    if not engine.is_file() or not pdb.is_file():
        raise RuntimeError(
            f"patchable engine/PDB missing: {engine}, {pdb}; run "
            "scripts/build-hcr-patchable-windows.py first"
        )

    sys.path.insert(0, str(args.reprobuild / "libs" / "repro_hcr_agent"))
    from build_windows_agent import build_artifacts, visual_studio_environment

    env = visual_studio_environment()
    work = REPO / "build" / "hwg-m2-windows-attach"
    work.mkdir(parents=True, exist_ok=True)
    artifacts = build_artifacts(work / "agent")
    nim = shutil.which("nim.exe", path=env.get("PATH"))
    if nim is None:
        raise RuntimeError("nim.exe is required for the production coordinator")
    coordinator = work / "hx_w5_windows_coordinator.exe"
    run_checked(
        [
            nim,
            "c",
            "--cc:vcc",
            "--hints:off",
            "--warnings:off",
            f"--nimcache:{work / 'nimcache-coordinator'}",
            f"--out:{coordinator}",
            f"-p:{args.reprobuild / 'libs' / 'repro_hcr_agent' / 'src'}",
            f"-p:{args.reprobuild / 'libs' / 'repro_hcr_linker' / 'src'}",
            f"-p:{args.reprobuild / 'libs' / 'repro_hcr_linkgraph' / 'src'}",
            f"-p:{args.reprobuild / 'libs' / 'repro_core' / 'src'}",
            f"-p:{args.reprobuild / 'libs' / 'repro_hash' / 'src'}",
            str(args.reprobuild / "tests" / "windows" / "hx_w5_windows_coordinator.nim"),
        ],
        args.reprobuild,
        env,
    )
    project = REPO / "tests" / "hcr" / "windows_attach_project"

    marker = work / "injected-marker.json"
    stop = work / "injected.stop"
    launcher_log = work / "injected-launcher.log"
    for path in (marker, stop, launcher_log):
        path.unlink(missing_ok=True)
    injected_env = dict(env)
    injected_env["CT_HCR_WINDOWS_ATTACH_MARKER"] = str(marker)
    injected_env["CT_HCR_WINDOWS_ATTACH_STOP"] = str(stop)
    with launcher_log.open("w+", encoding="utf-8") as output:
        launched = subprocess.run(
            [
                str(artifacts["launcher"]),
                "--agent",
                str(artifacts["agent"]),
                "--",
                str(engine),
                "--headless",
                "--path",
                str(project),
                "--script",
                "res://attach_probe.gd",
            ],
            cwd=REPO,
            env=injected_env,
            text=True,
            stdout=output,
            stderr=subprocess.STDOUT,
            timeout=30,
            check=False,
        )
        output.seek(0)
        launcher_output = output.read()
    if launched.returncode != 0:
        raise RuntimeError(f"Godot launcher failed:\n{launcher_output}")
    launcher_evidence = json.loads(launcher_output.splitlines()[-1])
    if not launcher_evidence.get("attached"):
        raise AssertionError(f"agent injection did not complete: {launcher_evidence!r}")
    injected_pid = int(launcher_evidence["pid"])
    try:
        before = wait_marker(marker, 2)
        coordinated = subprocess.run(
            [str(coordinator), str(injected_pid)],
            cwd=REPO,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20,
            check=False,
        )
        if coordinated.returncode != 0:
            raise AssertionError(coordinated.stdout)
        handshake = json.loads(coordinated.stdout.splitlines()[-1])
        after = wait_marker(marker, int(before["frames"]) + 5)
        if int(before["pid"]) != injected_pid or int(after["pid"]) != injected_pid:
            raise AssertionError("Godot PID changed across the HCR handshake")
        if not handshake["handshake_completed"]:
            raise AssertionError(f"coordinator did not settle: {handshake!r}")
        if "direct-patch-injection" not in handshake["capabilities"]:
            raise AssertionError(f"Windows patch capability absent: {handshake!r}")
    finally:
        stop_process(injected_pid, stop)

    control_marker = work / "control-marker.json"
    control_stop = work / "control.stop"
    control_log = work / "control.log"
    for path in (control_marker, control_stop, control_log):
        path.unlink(missing_ok=True)
    control_env = dict(env)
    control_env["CT_HCR_WINDOWS_ATTACH_MARKER"] = str(control_marker)
    control_env["CT_HCR_WINDOWS_ATTACH_STOP"] = str(control_stop)
    with control_log.open("w", encoding="utf-8") as output:
        control = subprocess.Popen(
            [
                str(engine),
                "--headless",
                "--path",
                str(project),
                "--script",
                "res://attach_probe.gd",
            ],
            cwd=REPO,
            env=control_env,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
    try:
        control_state = wait_marker(control_marker, 2)
        absent = subprocess.run(
            [str(coordinator), str(control.pid)],
            cwd=REPO,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20,
            check=False,
        )
        if absent.returncode == 0 or "timed out waiting for HCR agent pipe" not in absent.stdout:
            raise AssertionError(
                "uninjected Godot did not produce the named missing-pipe control:\n"
                + absent.stdout
            )
        if int(control_state["pid"]) != control.pid:
            raise AssertionError("control marker came from a different process")
    finally:
        control_stop.touch()
        try:
            control.wait(timeout=10)
        except subprocess.TimeoutExpired:
            control.kill()
            control.wait(timeout=5)

    print(
        json.dumps(
            {
                "schemaId": "codetracer.godot.hcr-windows-attach.v1",
                "engine": str(engine),
                "pdb": str(pdb),
                "pid": injected_pid,
                "framesBeforeHandshake": before["frames"],
                "framesAfterHandshake": after["frames"],
                "supportProfile": handshake["support_profile"],
                "capabilities": handshake["capabilities"],
                "controlMissingPipe": True,
            }
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as failure:
        print(f"verify-hcr-windows-attach: {failure}", file=sys.stderr)
        raise SystemExit(1)
