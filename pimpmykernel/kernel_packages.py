"""Installierte Kernel erkennen und eine gezielte Paketentfernung vorbereiten.

Dieses Modul benötigt nur die Standardbibliothek. Der Root-Helfer verwendet
dieselben Prüfungen erneut, bevor er die bestätigte Entfernung ausführt.
"""

from __future__ import annotations

import os
import platform
import re
import subprocess
from dataclasses import dataclass
from typing import Iterable

from .i18n import tr


PACKAGE_RE = re.compile(
    r"^linux-(?P<kind>image(?:-unsigned)?|headers|modules(?:-extra)?|tools|cloud-tools)-"
    # The full kernel release has a sublevel, unlike series meta-packages.
    r"(?P<release>\d+\.\d+\.\d+(?:[+-][a-z0-9][a-z0-9.+-]*)?)$"
)
QUERY_FORMAT = "${binary:Package}\t${db:Status-Status}\t${Version}\t${Installed-Size}\n"


@dataclass(frozen=True)
class KernelPackage:
    name: str
    version: str
    installed_bytes: int
    kind: str
    release: str
    image: bool
    installed: bool = True


@dataclass(frozen=True)
class InstalledKernel:
    release: str
    packages: tuple[str, ...]
    installed_bytes: int
    active: bool
    installed: bool = True


@dataclass(frozen=True)
class RemovalPlan:
    releases: tuple[str, ...]
    packages: tuple[str, ...]
    installed_bytes: int


def parse_kernel_packages(output: str) -> tuple[KernelPackage, ...]:
    packages = []
    for line in output.splitlines():
        fields = line.split("\t")
        if len(fields) != 4 or fields[1] not in {"installed", "config-files"}:
            continue
        name, status, version, size = fields
        installed = status == "installed"
        if not re.fullmatch(r"[a-z0-9+.-]+(?::[a-z0-9]+)?", name):
            continue
        match = PACKAGE_RE.fullmatch(name.split(":", 1)[0])
        if not match:
            continue  # Meta-Pakete und gemeinsame Bibliotheken sind keine Kernel.
        kind, release = match["kind"], match["release"]
        debug = kind.startswith("image") and release.endswith(("-dbg", "-dbgsym"))
        if debug:
            release = release.removesuffix("-dbg").removesuffix("-dbgsym")
        if kind.startswith("image"):
            # Debian places "unsigned" at the end, Ubuntu after "image".
            release = release.removesuffix("-unsigned")
        try:
            installed_bytes = max(0, int(size)) * 1024 if installed else 0
        except ValueError:
            installed_bytes = 0
        packages.append(KernelPackage(name, version, installed_bytes, kind, release,
                                      kind.startswith("image") and not debug, installed))
    return tuple(packages)


def query_kernel_packages() -> tuple[KernelPackage, ...]:
    result = subprocess.run(["/usr/bin/dpkg-query", "-W", "-f=" + QUERY_FORMAT],
                            capture_output=True, text=True, timeout=20,
                            env={**os.environ, "LC_ALL": "C"})
    if result.returncode:
        raise ValueError(tr("Die installierten Kernel-Pakete konnten nicht gelesen werden.\n")
                         + result.stderr.strip())
    return parse_kernel_packages(result.stdout)


def package_owners(package: KernelPackage, releases: set[str]) -> set[str]:
    if package.kind in {"headers", "tools", "cloud-tools"}:
        # Ubuntu shares packages without a flavour suffix between kernels.
        prefix = package.release
        common = package.kind == "headers" and prefix.endswith("-common")
        if common:
            prefix = prefix.removesuffix("-common")
        elif package.kind == "headers" and prefix.endswith("-common-rt"):
            prefix = prefix.removesuffix("-common-rt") + "-rt"
        owners = {release for release in releases
                  if (release == prefix or release.startswith(prefix + "-"))
                  and not (common and release.startswith(prefix + "-rt-"))}
        # Orphaned common-header residuals can also form their own cleanup entry.
        return owners or ({package.release} & releases)
    return {package.release} & releases


def _kernel_releases(packages: tuple[KernelPackage, ...]) -> set[str]:
    # A removed image may already be gone from dpkg while its modules retain
    # configuration. Keep those releases visible without treating them as a
    # bootable fallback. Shared residuals join their owners, or an orphan entry.
    releases = {package.release for package in packages
                if package.image or (not package.installed
                                     and package.kind not in {"headers", "tools", "cloud-tools"})}
    return releases | {package.release for package in packages
                       if not package.installed and not package_owners(package, releases)}


def group_installed_kernels(packages: Iterable[KernelPackage],
                           running: str) -> list[InstalledKernel]:
    packages = tuple(packages)
    releases = _kernel_releases(packages)
    installed = {package.release for package in packages if package.image and package.installed}
    kernels = []
    for release in sorted(releases, key=lambda value: (tuple(map(int, re.findall(r"\d+", value))), value),
                          reverse=True):
        members = [package for package in packages if release in package_owners(package, releases)]
        kernels.append(InstalledKernel(release, tuple(sorted(package.name for package in members)),
                                       sum(package.installed_bytes for package in members),
                                       release == running, release in installed))
    return kernels


def installed_kernels() -> list[InstalledKernel]:
    return group_installed_kernels(query_kernel_packages(), platform.release())


def create_removal_plan(releases: Iterable[str], packages: Iterable[KernelPackage],
                        running: str) -> RemovalPlan:
    selected = set(releases)
    packages = tuple(packages)
    available = _kernel_releases(packages)
    installed = {package.release for package in packages if package.image and package.installed}
    if not selected or not selected <= available:
        raise ValueError(tr("Die Kernel-Auswahl ist nicht mehr aktuell. Bitte die Liste aktualisieren."))
    if running in selected:
        raise ValueError(tr("Der aktuell laufende Kernel darf nicht deinstalliert werden."))
    if installed and not installed - selected:
        raise ValueError(tr("Mindestens ein installierter Kernel muss erhalten bleiben."))
    members = []
    for package in packages:
        owners = package_owners(package, available)
        if owners and owners <= selected:
            members.append(package)
    # APT interprets trailing +/- as an instruction rather than as a literal name.
    if any(package.name.split(":", 1)[0].endswith(("+", "-")) for package in members):
        raise ValueError(tr("Die Auswahl enthält einen nicht unterstützten Paketnamen."))
    return RemovalPlan(tuple(sorted(selected)), tuple(sorted(package.name for package in members)),
                       sum(package.installed_bytes for package in members))


def validate_removal_packages(arguments: list[str]) -> RemovalPlan:
    if not arguments or len(arguments) > 200 or len(set(arguments)) != len(arguments):
        raise ValueError(tr("Keine gültige Kernel-Paketliste zum Entfernen angegeben."))
    snapshot = query_kernel_packages()
    by_name = {package.name: package for package in snapshot}
    if any(name not in by_name for name in arguments):
        raise ValueError(tr("Entfernt werden nur vorhandene, versionsgebundene Kernel-Pakete."))
    releases = {by_name[name].release for name in arguments} & _kernel_releases(snapshot)
    plan = create_removal_plan(releases, snapshot, platform.release())
    if set(arguments) != set(plan.packages):
        raise ValueError(tr("Die Paketliste passt nicht zu den ausgewählten Kerneln. Bitte erneut prüfen."))
    return plan


def validate_apt_removal(output: str, packages: tuple[str, ...]) -> None:
    expected = {name.split(":", 1)[0] for name in packages}
    removed = set()
    for line in output.splitlines():
        fields = line.split()
        if not fields:
            continue
        if fields[0] in {"Inst", "Conf"}:
            raise ValueError(tr("APT würde zusätzlich Pakete installieren oder einrichten. "
                             "Bitte zuerst die Paketverwaltung des Systems abschließen."))
        if fields[0] in {"Remv", "Purg"} and len(fields) >= 2:
            removed.add(fields[1].split(":", 1)[0])
    additional = removed - expected
    if additional:
        raise ValueError(tr("APT würde weitere Pakete entfernen: ") + ", ".join(sorted(additional))
                         + tr(". Die Deinstallation wurde zum Schutz dieser Pakete gestoppt."))
    if removed != expected:
        raise ValueError(tr("Der APT-Entfernungsplan entspricht nicht mehr der Auswahl. "
                         "Bitte die Kernel-Liste aktualisieren."))


def simulate_removal(plan: RemovalPlan) -> None:
    result = subprocess.run(["/usr/bin/apt-get", "--simulate", "--no-auto-remove",
                             "purge", "--", *plan.packages], capture_output=True, text=True,
                            timeout=60, env={**os.environ, "LC_ALL": "C"})
    if result.returncode:
        details = "\n".join((result.stdout + result.stderr).splitlines()[-15:])
        raise ValueError(tr("APT konnte die Deinstallation nicht vorbereiten.\n") + details)
    validate_apt_removal(result.stdout, plan.packages)


def plan_kernel_removal(releases: Iterable[str]) -> RemovalPlan:
    plan = create_removal_plan(releases, query_kernel_packages(), platform.release())
    simulate_removal(plan)
    return plan
