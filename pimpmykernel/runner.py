"""Prozessgruppen, Live-Ausgabe und zuverlässiger Abbruch ohne Shell."""

from __future__ import annotations

import codecs
import os
import selectors
import shlex
import signal
import subprocess
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

from .i18n import language, tr
from .models import Cancelled, KernelError


class CommandRunner:
    def __init__(self, cancel: threading.Event, log: Callable[[str], None], *,
                 temp_dir: Path | None = None):
        self.cancel = cancel
        self.log = log
        self.temp_dir = temp_dir

    def environment(self) -> dict[str, str]:
        """Gleiche Build-Umgebung für automatische und interaktive Befehle."""
        env = dict(os.environ)
        # A desktop session must not silently redirect kbuild to another tree/toolchain.
        for name in tuple(env):
            if name.startswith("KCONFIG_") or name in {
                "MAKEFLAGS", "MFLAGS", "MAKELEVEL", "KBUILD_OUTPUT", "KBUILD_SRC",
                "ARCH", "CROSS_COMPILE", "LOCALVERSION", "CC", "HOSTCC", "CXX",
                "LD", "LLVM", "LLVM_IAS", "KDEB_SOURCENAME", "KDEB_PKGVERSION",
                "INSTALL_PATH", "INSTALL_MOD_PATH", "DEB_BUILD_OPTIONS", "DEB_BUILD_PROFILES",
            }:
                env.pop(name, None)
        env.update(LC_ALL="C", PYTHONUNBUFFERED="1")
        if self.temp_dir is not None:
            # Keep compiler/packaging helper files inside the job that can be removed.
            env.update(TMPDIR=str(self.temp_dir), TMP=str(self.temp_dir), TEMP=str(self.temp_dir))
        return env

    def run(self, args: list[str], cwd: Path | None = None, *,
            defaults: bool = False, check: bool = True,
            timeout: float | None = None, echo: bool = True,
            capture_lines: int | None = 1200, log_output: bool = True) -> tuple[int, str]:
        if self.cancel.is_set():
            raise Cancelled(tr("Vorgang abgebrochen."))
        if echo:
            self.log("$ " + shlex.join([str(a) for a in args]))
        env = self.environment()
        input_file = tempfile.TemporaryFile(dir=self.temp_dir) if defaults else None
        if input_file:
            # Blank answers select the displayed defaults of `make oldconfig`.
            # A regular file avoids blocked pipe writers and survives all child prompts.
            input_file.write(b"\n" * 1_000_000)
            input_file.seek(0)
        process = None
        selector = selectors.DefaultSelector()
        output = deque(maxlen=capture_lines)
        pending = ""
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        started = time.monotonic()
        interrupted: Exception | None = None
        terminate_at: float | None = None
        try:
            process = subprocess.Popen([str(a) for a in args], cwd=cwd, env=env,
                                       stdin=input_file or subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       start_new_session=True, bufsize=0)
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map() or process.poll() is None:
                now = time.monotonic()
                if interrupted is None and self.cancel.is_set():
                    interrupted = Cancelled(tr("Vorgang abgebrochen."))
                if interrupted is None and timeout and now - started > timeout:
                    interrupted = KernelError(tr('Zeitüberschreitung bei {value}.', value=args[0]))
                if interrupted is not None and terminate_at is None:
                    self._signal_group(process.pid, signal.SIGTERM)
                    terminate_at = now
                if terminate_at is not None and now - terminate_at > 2:
                    # Kill the whole group even when the make parent has already exited.
                    self._signal_group(process.pid, signal.SIGKILL)
                for key, _ in selector.select(timeout=0.1):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    pending += decoder.decode(chunk).replace("\r", "\n")
                    lines = pending.split("\n")
                    pending = lines.pop()
                    for line in lines:
                        if log_output:
                            self.log(line)
                        output.append(line)
                    if len(pending) > 65536:
                        if log_output:
                            self.log(pending)
                        output.append(pending)
                        pending = ""
                if process.poll() is not None and not selector.get_map():
                    break
            pending += decoder.decode(b"", final=True)
            if pending:
                if log_output:
                    self.log(pending)
                output.append(pending)
            code = process.wait()
            captured = "\n".join(output)
            if interrupted:
                raise interrupted
            if check and code:
                tail = "\n".join(list(output)[-12:])
                raise KernelError(tr('{name} wurde mit Fehler {code} beendet.\n{tail}', name=Path(args[0]).name, code=code, tail=tail))
            return code, captured
        except OSError as exc:
            raise KernelError(tr('Der Befehl {value} konnte nicht ausgeführt werden: {exc}', value=args[0], exc=exc)) from exc
        finally:
            selector.close()
            if input_file:
                input_file.close()
            if process:
                if process.poll() is None:
                    self._signal_group(process.pid, signal.SIGKILL)
                    process.wait()
                if process.stdout:
                    process.stdout.close()

    @staticmethod
    def _signal_group(pid: int, sig: signal.Signals) -> None:
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            pass


def privileged_command(action: str, *arguments: str) -> list[str]:
    import shutil
    if not shutil.which("pkexec"):
        raise KernelError(tr("Für die grafische Passwortabfrage fehlt pkexec (Paket policykit-1 / polkitd-pkexec)."))
    helper = Path(__file__).with_name("privileged.py").resolve()
    return ["pkexec", "/usr/bin/python3", "-I", str(helper), "--language", language(),
            action, *arguments]
