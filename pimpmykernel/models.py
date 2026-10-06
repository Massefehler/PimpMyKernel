"""Datenmodelle und unprivilegierte Erkennung des lokalen Systems."""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .i18n import tr


class KernelError(Exception):
    """Eine Fehlermeldung, die direkt in der Oberfläche angezeigt werden kann."""


class Cancelled(KernelError):
    pass


VERSION_RE = re.compile(r"^(?:[4-9]|[1-9]\d+)\.\d+(?:\.\d+)?(?:-rc\d+)?$")


def version_key(version: str) -> tuple[int, ...]:
    """Natürliche Reihenfolge; ein RC liegt vor der fertigen Version."""
    core, _, suffix = version.partition("-")
    numbers = [int(n) for n in re.findall(r"\d+", core)]
    numbers = (numbers + [0, 0, 0])[:3]
    return (*numbers, 0 if suffix.startswith("rc") else 1,
            int(suffix[2:]) if re.fullmatch(r"rc\d+", suffix) else 0,
            *[int(n) for n in re.findall(r"\d+", suffix) if not suffix.startswith("rc")])


def upstream_version(version: str) -> str:
    """Distributions-ABI und lokale Zusätze beim Release-Vergleich auslassen."""
    match = re.match(r"\d+\.\d+(?:\.\d+)?(?:-rc\d+)?", version)
    return match[0] if match else version


@dataclass(frozen=True)
class Release:
    version: str
    channel: str
    date: str
    source: str
    signature: str | None
    changelog: str | None = None
    eol: bool = False

    @property
    def label(self) -> str:
        return {"stable": tr("Stable"), "longterm": tr("Langzeitpflege · LTS"),
                "archive": tr("Archiv · ältere Version"),
                "mainline": tr("Mainline · Vorabversion") if self.is_rc else tr("Mainline")}.get(
                    self.channel, self.channel)

    @property
    def is_rc(self) -> bool:
        return "-rc" in self.version

    @property
    def buildable(self) -> bool:
        # Git snapshots/RCs have no detached release signature in releases.json.
        return bool(self.signature) and (not self.eol or self.channel == "archive") and not self.is_rc

    def validate(self) -> None:
        if not VERSION_RE.fullmatch(self.version):
            raise KernelError(tr("Ungültige Kernel-Version in den Release-Daten."))
        major = self.version.split(".")[0]
        expected = f"/pub/linux/kernel/v{major}.x/linux-{self.version}.tar.xz"
        url = urlparse(self.source)
        try:
            port = url.port
        except ValueError as exc:
            raise KernelError(tr("Der Quellcode-Link enthält einen ungültigen Port.")) from exc
        if (url.scheme != "https" or url.hostname not in {"cdn.kernel.org", "www.kernel.org"}
                or url.path != expected or url.query or url.fragment or url.username
                or port not in (None, 443)):
            raise KernelError(tr("Der Quellcode-Link ist kein offizielles Kernel-Release-Archiv."))
        if self.signature != self.source.removesuffix(".xz") + ".sign":
            raise KernelError(tr("Der Link zur Kernel-Signatur passt nicht zum Archiv."))
        if not self.buildable:
            raise KernelError(tr("Für dieses Release ist kein signierter Build verfügbar."))


def parse_releases(data: object) -> list[Release]:
    if not isinstance(data, dict) or not isinstance(data.get("releases"), list):
        raise KernelError(tr("kernel.org hat keine gültige Release-Liste geliefert."))
    releases = []
    for entry in data["releases"]:
        if not isinstance(entry, dict):
            continue
        version = entry.get("version", "")
        channel = entry.get("moniker")
        source = entry.get("source")
        if (channel not in {"stable", "longterm", "mainline"}
                or not isinstance(version, str) or not VERSION_RE.fullmatch(version)
                or not isinstance(source, str)):
            continue
        released = entry.get("released", {})
        release = Release(version, channel,
                          str(released.get("isodate", "")) if isinstance(released, dict) else "",
                          source, entry.get("pgp"), entry.get("changelog"),
                          bool(entry.get("iseol")))
        if release.buildable:
            release.validate()
        releases.append(release)
    releases.sort(key=lambda r: (r.channel == "stable", version_key(r.version)), reverse=True)
    if not any(r.buildable for r in releases):
        raise KernelError(tr("Zurzeit sind keine unterstützten, signierten Releases verfügbar."))
    return releases


def cpu_count() -> int:
    # nproc respects the CPU affinity of the current user/session.
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except AttributeError:
        return max(1, os.cpu_count() or 1)


def job_choices(count: int) -> list[int]:
    return sorted({1, *[n for n in (2, 4, 8, 10, 12, 16) if n <= count], count})


def boot_configs(boot: Path = Path("/boot")) -> list[Path]:
    try:
        return sorted((p for p in boot.glob("config-*") if p.is_file()),
                      key=lambda p: (version_key(p.name.removeprefix("config-")), p.stat().st_mtime),
                      reverse=True)
    except OSError as exc:
        raise KernelError(tr('Die Kernel-Konfigurationen in {boot} können nicht gelesen werden: {exc}', boot=boot, exc=exc)) from exc


def state_home() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "pimpmykernel"


def cache_home() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "pimpmykernel"


def default_output() -> Path:
    # xdg-user-dir respects translated directory names, unlike ~/Downloads.
    if shutil.which("xdg-user-dir"):
        try:
            result = subprocess.run(["xdg-user-dir", "DOWNLOAD"], capture_output=True,
                                    text=True, timeout=3, check=True)
            value = result.stdout.strip()
            if value and Path(value).is_absolute():
                return Path(value) / "PimpMyKernel"
        except (OSError, subprocess.SubprocessError):
            pass
    return Path.home() / "PimpMyKernel-Pakete"


def memory_gib() -> float:
    try:
        text = Path("/proc/meminfo").read_text()
        return int(re.search(r"MemTotal:\s+(\d+)", text)[1]) / 1024**2
    except (OSError, TypeError, ValueError):
        return 0.0


def secure_boot_state() -> str:
    if not Path("/sys/firmware/efi").exists():
        return "off"
    try:
        for path in Path("/sys/firmware/efi/efivars").glob("SecureBoot-*"):
            return "on" if path.read_bytes()[4] else "off"
    except (OSError, IndexError):
        pass
    if shutil.which("mokutil"):
        try:
            text = subprocess.run(["mokutil", "--sb-state"], capture_output=True,
                                  text=True, timeout=5, env={**os.environ, "LC_ALL": "C"}).stdout
            if "SecureBoot enabled" in text:
                return "on"
            if "SecureBoot disabled" in text:
                return "off"
        except (OSError, subprocess.SubprocessError):
            pass
    return "unknown"


@dataclass(frozen=True)
class SystemInfo:
    kernel: str
    distro: str
    cpus: int
    ram_gib: float
    architecture: str
    debian: bool
    secure_boot: str

    @classmethod
    def detect(cls) -> SystemInfo:
        os_info = platform.freedesktop_os_release()
        ids = f"{os_info.get('ID', '')} {os_info.get('ID_LIKE', '')}".split()
        return cls(platform.release(), os_info.get("PRETTY_NAME", "Linux"), cpu_count(),
                   memory_gib(), platform.machine(), "debian" in ids or "ubuntu" in ids,
                   secure_boot_state())


# Include the packaging prerequisites, not only the compiler. apt handles their dependencies.
BUILD_PACKAGES = (
    "build-essential", "bc", "bison", "flex", "libssl-dev", "libelf-dev",
    "libncurses-dev", "dwarves", "fakeroot", "dpkg-dev", "debhelper",
    "rsync", "cpio", "kmod", "xz-utils", "zstd", "git", "gnupg", "ca-certificates", "pkg-config",
)
BUILD_TOOLS = ("make", "gcc", "g++", "ld", "ar", "bc", "bison", "flex", "openssl",
               "pahole", "fakeroot", "dpkg-buildpackage", "dpkg-deb", "dh",
               "rsync", "cpio", "kmod", "xz", "zstd", "git", "gpg", "pkg-config")


def missing_dependencies() -> list[str]:
    missing = [tool for tool in BUILD_TOOLS if not shutil.which(tool)]
    for package in ("libssl-dev", "libelf-dev", "libncurses-dev"):
        try:
            check = subprocess.run(["dpkg-query", "-W", "-f=${Status}", package],
                                   capture_output=True, text=True, timeout=5)
            if check.returncode or "install ok installed" not in check.stdout:
                missing.append(package)
        except (OSError, subprocess.SubprocessError):
            missing.append(package)
    return missing


@dataclass(frozen=True)
class BuildOptions:
    release: Release
    config: Path
    output: Path
    build_jobs: int
    package_jobs: int
    skip_debug: bool = True
    repair_certificates: bool = True
    cleanup: bool = True
    menuconfig: bool = False

    def validate(self) -> None:
        self.release.validate()
        if not self.config.is_file():
            raise KernelError(tr("Bitte eine vorhandene Kernel-Konfiguration auswählen."))
        count = cpu_count()
        if not (1 <= self.build_jobs <= count and 1 <= self.package_jobs <= count):
            raise KernelError(tr('Auf diesem System sind höchstens {value1} parallele Jobs möglich.', value1=count))
        if not self.output.is_absolute():
            raise KernelError(tr("Bitte ein absolutes Ausgabeverzeichnis auswählen."))
