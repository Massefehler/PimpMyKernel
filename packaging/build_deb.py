#!/usr/bin/env python3
"""Erstellt das App-.deb ohne Root-Rechte und ohne Python-Build-Abhängigkeiten."""

from __future__ import annotations

import argparse
import ast
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


def project_version(root: Path) -> str:
    tree = ast.parse((root / "pimpmykernel/__init__.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(n, ast.Name) and n.id == "__version__" for n in node.targets):
            value = ast.literal_eval(node.value)
            if isinstance(value, str) and re.fullmatch(r"\d+\.\d+\.\d+(?:[+~.-][a-zA-Z0-9.]+)?", value):
                return value
    raise ValueError("Keine gültige Paketversion gefunden.")


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="PimpMyKernel als installierbares .deb paketieren")
    parser.add_argument("--output", type=Path, default=root / "dist")
    args = parser.parse_args()
    if not shutil.which("dpkg-deb"):
        parser.error("dpkg-deb fehlt (Paket dpkg).")
    version = project_version(root)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"pimpmykernel_{version}_all.deb"
    with tempfile.TemporaryDirectory(prefix="pmk-deb-") as temporary:
        package = Path(temporary) / "package"
        library = package / "usr/lib/pimpmykernel/pimpmykernel"
        shutil.copytree(root / "pimpmykernel", library,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
        binary = package / "usr/bin/pimpmykernel"
        binary.parent.mkdir(parents=True)
        shutil.copyfile(root / "packaging/launcher", binary)
        binary.chmod(0o755)
        applications = package / "usr/share/applications"
        applications.mkdir(parents=True)
        shutil.copyfile(root / "packaging/pimpmykernel.desktop", applications / "pimpmykernel.desktop")
        pixmaps = package / "usr/share/pixmaps"
        pixmaps.mkdir(parents=True)
        shutil.copyfile(root / "pimpmykernel/assets/icon.png", pixmaps / "pimpmykernel.png")
        docs = package / "usr/share/doc/pimpmykernel"
        docs.mkdir(parents=True)
        shutil.copyfile(root / "README.md", docs / "README.md")
        shutil.copyfile(root / "README.de.md", docs / "README.de.md")
        shutil.copyfile(root / "LICENSE", docs / "copyright")
        size = sum(p.stat().st_size for p in package.rglob("*") if p.is_file())
        control = package / "DEBIAN"
        control.mkdir()
        executables = {binary}
        for name in ("postinst", "prerm"):
            script = control / name
            shutil.copyfile(root / "packaging" / name, script)
            executables.add(script)
        (control / "control").write_text(
            f"Package: pimpmykernel\nVersion: {version}\nArchitecture: all\n"
            "Maintainer: PimpMyKernel Project <noreply@localhost>\n"
            "Section: admin\nPriority: optional\n"
            "Depends: python3 (>= 3.12), python3-pyqt6 (>= 6.4), libqt6svg6, qt6-translations-l10n, pkexec, ca-certificates\n"
            "Recommends: gnupg, xterm\n"
            f"Installed-Size: {(size + 1023) // 1024}\n"
            "Description: Desktop assistant for building official Linux kernels\n"
            " Checks kernel.org, uses existing kernel configurations, creates\n"
            " Debian packages and offers their installation and kernel removal.\n"
            " With German, English and Spanish Qt interfaces, automatic or manual\n"
            " language selection, signature verification and a live build log.\n")
        # Uniform reproducible metadata; root ownership is stored in the archive only.
        epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "1791158400"))
        for path in package.rglob("*"):
            path.chmod(0o755 if path.is_dir() or path in executables else 0o644)
            os.utime(path, (epoch, epoch))
        os.utime(package, (epoch, epoch))
        partial = output / (target.name + ".part")
        try:
            subprocess.run(["dpkg-deb", "--root-owner-group", "-Zxz", "--build", str(package), str(partial)],
                           check=True, env={**os.environ, "SOURCE_DATE_EPOCH": str(epoch)})
            partial.replace(target)
        except Exception:
            partial.unlink(missing_ok=True)
            raise
    print(f"App-Paket fertig: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
