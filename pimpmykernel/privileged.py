"""Kleiner, isoliert gestarteter Root-Helfer für explizite GUI-Aktionen.

Kein Build, kein beliebiger Shell-Befehl und keine Root-Schreiboperation im
Arbeitsverzeichnis. Konfigurationen werden ausschließlich nach stdout gelesen.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
from pathlib import Path

# Both helpers are invoked with -I. Load only the application's own modules;
# i18n and kernel_packages are pure standard-library modules without Qt.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pimpmykernel.i18n import SUPPORTED_LANGUAGES, configure_language, language, tr

PACKAGES = (
    "build-essential", "bc", "bison", "flex", "libssl-dev", "libelf-dev",
    "libncurses-dev", "dwarves", "fakeroot", "dpkg-dev", "debhelper",
    "rsync", "cpio", "kmod", "xz-utils", "zstd", "git", "gnupg", "ca-certificates", "pkg-config",
)


def read_config(path_string: str) -> bytes:
    path = Path(path_string)
    if (not path.is_absolute() or path.parent != Path("/boot")
            or not re.fullmatch(r"config-[A-Za-z0-9.+_~-]+", path.name)):
        raise ValueError(tr("Nur Kernel-Konfigurationen direkt in /boot dürfen gelesen werden."))
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4 * 1024 * 1024:
            raise ValueError(tr("Ungültige Konfigurationsdatei."))
        return stream.read(4 * 1024 * 1024 + 1)


def validate_packages(arguments: list[str]) -> list[str]:
    if not arguments or len(arguments) > 50:
        raise ValueError(tr("Keine gültige Paketliste angegeben."))
    paths = []
    has_image = False
    for argument in arguments:
        path = Path(argument)
        if not path.is_absolute() or path.is_symlink() or path.suffix != ".deb" or not path.is_file():
            raise ValueError(tr("Installiert werden nur vorhandene .deb-Dateien mit absolutem Pfad."))
        result = subprocess.run(["/usr/bin/dpkg-deb", "--field", str(path), "Package"],
                                capture_output=True, text=True, check=True, timeout=30)
        package = result.stdout.strip()
        if not re.fullmatch(r"linux-(?:image|headers|libc-dev)[a-z0-9+.-]*", package):
            raise ValueError(tr('Kein unterstütztes Kernel-Paket: {package}', package=package))
        has_image = has_image or package.startswith("linux-image-") and not package.endswith("-dbg")
        paths.append(str(path))
    if not has_image:
        raise ValueError(tr("Die Auswahl enthält kein Kernel-Image."))
    return paths


def main() -> int:
    arguments = sys.argv[1:]
    if arguments[:1] == ["--language"]:
        if len(arguments) < 3 or arguments[1] not in SUPPORTED_LANGUAGES:
            return 2
        configure_language([arguments[1]])
        arguments = arguments[2:]
    language()  # Retain the caller's language before clearing the environment.
    if os.geteuid() != 0:
        print(tr("Dieser Helfer benötigt die grafische Administratorfreigabe."), file=sys.stderr)
        return 1
    # Do not inherit apt hooks/options or interpreter search paths from the caller.
    os.environ.clear()
    os.environ.update(PATH="/usr/sbin:/usr/bin:/sbin:/bin", LC_ALL="C")
    os.umask(0o022)
    if not arguments:
        raise ValueError(tr("Unbekannte oder ungültige Administratoraktion."))
    action, *arguments = arguments
    if action == "read-config" and len(arguments) == 1:
        sys.stdout.buffer.write(read_config(arguments[0]))
        return 0
    if action == "dependencies" and not arguments:
        subprocess.run(["/usr/bin/apt-get", "update"], check=True)
        return subprocess.call(["/usr/bin/apt-get", "install", "--yes", "--", *PACKAGES])
    if action == "install":
        paths = validate_packages(arguments)
        return subprocess.call(["/usr/bin/apt-get", "install", "--yes", "--", *paths])
    if action == "remove":
        from pimpmykernel.kernel_packages import simulate_removal, validate_removal_packages
        plan = validate_removal_packages(arguments)
        simulate_removal(plan)
        return subprocess.call(["/usr/bin/apt-get", "--yes", "--no-auto-remove",
                                "purge", "--", *plan.packages])
    raise ValueError(tr("Unbekannte oder ungültige Administratoraktion."))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"PimpMyKernel: {exc}", file=sys.stderr)
        sys.exit(1)
