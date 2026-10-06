"""Unprivilegierter Terminal-Einstieg; führt ausschließlich make menuconfig aus."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pimpmykernel.i18n import SUPPORTED_LANGUAGES, configure_language, tr


def main() -> int:
    if len(sys.argv) not in {2, 3} or os.geteuid() == 0:
        return 2
    if len(sys.argv) == 3:
        if sys.argv[2] not in SUPPORTED_LANGUAGES:
            return 2
        configure_language([sys.argv[2]])
    status_path = Path(sys.argv[1])
    start_path = status_path.parent / "start"
    cancel_path = status_path.parent / "cancel"
    parent = os.getppid()
    identity = {"pid": os.getpid(), "pgid": os.getpgrp()}

    def report(phase: str, code: int | None = None):
        temporary = status_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({**identity, "phase": phase, "returncode": code}))
        temporary.replace(status_path)

    report("ready")
    deadline = time.monotonic() + 30
    # Do not start make until the application has recorded our terminal group.
    while not start_path.exists():
        if cancel_path.exists() or os.getppid() != parent or time.monotonic() > deadline:
            report("finished", 130)
            return 130
        time.sleep(0.05)
    if cancel_path.exists() or os.getppid() != parent:
        report("finished", 130)
        return 130
    print("PimpMyKernel · make menuconfig", flush=True)
    print(tr("Änderungen in .config speichern und menuconfig beenden."), flush=True)
    print(tr("Danach in PimpMyKernel die Änderungen prüfen und den Build fortsetzen.\n"), flush=True)
    try:
        # Inherit the real terminal, including its foreground process group.
        code = subprocess.call(["make", "menuconfig"])
    except OSError as exc:
        print(tr('menuconfig konnte nicht gestartet werden: {exc}', exc=exc), file=sys.stderr, flush=True)
        code = 1
    report("finished", code)
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    sys.exit(main())
