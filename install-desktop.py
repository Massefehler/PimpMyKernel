#!/usr/bin/env python3
"""Installiert einen Starter nur für den aktuellen Benutzer, ohne sudo."""

import os
import subprocess
from pathlib import Path

from pimpmykernel.i18n import initialize_language, tr


def exec_quote(value: str) -> str:
    # Desktop Entry's Exec uses its own escaping, not shell or JSON escaping.
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
    return '"' + value + '"'


def main():
    initialize_language()
    root = Path(__file__).resolve().parent
    data = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    applications = data / "applications"
    applications.mkdir(parents=True, exist_ok=True)
    entry = applications / "pimpmykernel.desktop"
    template = (root / "packaging/pimpmykernel.desktop").read_text(encoding="utf-8")
    template = template.replace("Exec=pimpmykernel\n", "Exec=" + exec_quote(str(root / "start.sh")) + "\n")
    template = template.replace("Icon=pimpmykernel\n", "Icon=" + str(root / "pimpmykernel/assets/icon.png") + "\n")
    entry.write_text(template, encoding="utf-8")
    import shutil
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", str(applications)], check=False)
    print(tr("Startmenü-Eintrag installiert: {entry}", entry=entry))


if __name__ == "__main__":
    main()
