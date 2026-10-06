"""Interaktives menuconfig im Terminal mit überwachten Prozessgruppen."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from .i18n import language, tr
from .models import Cancelled, KernelError
from .runner import CommandRunner


def menuconfig_terminal() -> str:
    terminal = shutil.which("xterm")
    if not terminal:
        raise KernelError(tr("Für make menuconfig fehlt xterm. Bitte zuerst xterm installieren "
                          "(sudo apt install xterm) oder die Profi-Funktion abwählen."))
    return terminal


def _processes() -> dict[int, tuple[int, int, str]]:
    processes = {}
    for path in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = path.read_text().rsplit(")", 1)[1].split()
            processes[int(path.parent.name)] = (int(fields[1]), int(fields[2]), fields[0])
        except (OSError, ValueError, IndexError):
            continue  # A process can exit while /proc is being read.
    return processes


def _signal_group(group: int, sig: signal.Signals) -> None:
    if group <= 1 or group == os.getpgrp():
        raise KernelError(tr("Abbruch abgelehnt: ungültige Terminal-Prozessgruppe."))
    try:
        os.killpg(group, sig)
    except ProcessLookupError:
        pass


def _descendant_groups(processes: dict[int, tuple[int, int, str]], parent: int) -> set[int]:
    descendants = {parent}
    while True:
        found = {pid for pid, (ppid, _, _) in processes.items() if ppid in descendants}
        if found <= descendants:
            break
        descendants.update(found)
    return {group for pid, (_, group, _) in processes.items()
            if pid in descendants and group > 1 and group != os.getpgrp()}


def run_menuconfig(runner: CommandRunner, tree: Path, workspace: Path) -> None:
    if runner.cancel.is_set():
        raise Cancelled(tr("Vorgang abgebrochen."))
    terminal = menuconfig_terminal()
    session = workspace / "menuconfig"
    session.mkdir(mode=0o700)
    status_path = session / "status.json"
    helper = Path(__file__).with_name("menuconfig_helper.py").resolve()
    args = [terminal, "-u8", "-T", "PimpMyKernel · make menuconfig", "-geometry", "120x36",
            "-e", sys.executable, "-I", str(helper), str(status_path), language()]
    runner.log(tr("$ make menuconfig · interaktives Terminal"))
    process = None
    groups: set[int] = set()
    interrupted: Exception | None = None
    terminate_at: float | None = None
    started = time.monotonic()
    status = {}
    diagnostics = session / "terminal.log"
    try:
        with diagnostics.open("wb") as output:
            process = subprocess.Popen(args, cwd=tree, env=runner.environment(), stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            groups.add(process.pid)
            while True:
                processes = _processes()
                try:
                    status = json.loads(status_path.read_text())
                except FileNotFoundError:
                    pass
                if (interrupted is None and status and status.get("phase") == "ready"
                        and not (session / "start").exists()):
                    pid, group = status.get("pid"), status.get("pgid")
                    info = _processes().get(pid)
                    if (not isinstance(pid, int) or not isinstance(group, int) or group <= 1
                            or group == os.getpgrp() or not info or info[:2] != (process.pid, group)):
                        raise KernelError(tr("Die Terminal-Prozessgruppe konnte nicht überprüft werden."))
                    groups.add(group)
                    if not runner.cancel.is_set():
                        (session / "start").touch()
                if interrupted is None and runner.cancel.is_set():
                    interrupted = Cancelled(tr("menuconfig abgebrochen. Die Arbeitsdateien bleiben erhalten."))
                if interrupted is None and not status and time.monotonic() - started > 15:
                    interrupted = KernelError(tr("Das menuconfig-Terminal konnte nicht geöffnet werden."))
                if interrupted is None and process.poll() is not None:
                    if status.get("phase") != "finished":
                        interrupted = KernelError(tr("Das Terminal wurde vorzeitig geschlossen. "
                                                  "Der Kernel-Build wurde nicht fortgesetzt."))
                    elif status.get("returncode") != 0:
                        interrupted = KernelError(tr('make menuconfig wurde mit Fehler {value} beendet.', value=status.get('returncode')))
                if interrupted is not None:
                    (session / "cancel").touch()
                    # xterm's terminal child has its own session. Include children
                    # even when cancellation arrives before the helper handshake.
                    groups.update(_descendant_groups(processes, process.pid))
                    now = time.monotonic()
                    if terminate_at is None:
                        terminate_at = now
                        for group in groups:
                            _signal_group(group, signal.SIGTERM)
                    elif now - terminate_at >= 2:
                        for group in groups:
                            _signal_group(group, signal.SIGKILL)
                alive = any(group in groups and state not in {"Z", "X"}
                            for _, group, state in processes.values())
                if process.poll() is not None and not alive:
                    break
                time.sleep(0.1)
            process.wait()
        if interrupted:
            detail = diagnostics.read_text(errors="replace").strip()[-1600:]
            if isinstance(interrupted, KernelError) and not isinstance(interrupted, Cancelled) and detail:
                raise KernelError(f"{interrupted}\n{detail}")
            raise interrupted
        runner.log(tr("make menuconfig beendet · Änderungen werden vor dem Build geprüft."))
    except (OSError, ValueError) as exc:
        raise KernelError(tr('Das menuconfig-Terminal konnte nicht ausgeführt werden: {exc}', exc=exc)) from exc
    finally:
        if process:
            processes = _processes()
            groups.update(_descendant_groups(processes, process.pid))
            for group in groups:
                if any(pgroup == group and state not in {"Z", "X"} for _, pgroup, state in processes.values()):
                    _signal_group(group, signal.SIGKILL)
            process.wait()
            while any(group in groups and state not in {"Z", "X"}
                      for _, group, state in _processes().values()):
                time.sleep(0.05)
