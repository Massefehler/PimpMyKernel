"""Der echte Download-, Build-, Export- und Installationsablauf."""

from __future__ import annotations

import difflib
import hashlib
import json
import lzma
import os
import re
import shutil
import tarfile
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable
from uuid import uuid4

from .i18n import tr
from .models import (BuildOptions, Cancelled, KernelError, SystemInfo, cache_home,
                     missing_dependencies, secure_boot_state, state_home)
from .menuconfig import menuconfig_terminal, run_menuconfig
from .network import Network
from .runner import CommandRunner, privileged_command

# Pinned primary fingerprints, published at https://www.kernel.org/signature.html.

SIGNERS = {
    "ABAF11C65A2970B130ABE3C479BE3E4300411886": "Linus Torvalds",
    "647F28654894E3BD457199BE38DBBDC86092693E": "Greg Kroah-Hartman",
    "E27E5D8A3403A2EF66873BBCDEA66FF797772CDC": "Sasha Levin",
    "AC2B29BD34A6AFDDB3F68F35E7BFC8EC95861109": "Ben Hutchings",
}
SIGNER_EMAILS = {
    "ABAF11C65A2970B130ABE3C479BE3E4300411886": "torvalds@kernel.org",
    "647F28654894E3BD457199BE38DBBDC86092693E": "gregkh@kernel.org",
    "E27E5D8A3403A2EF66873BBCDEA66FF797772CDC": "sashal@kernel.org",
    "AC2B29BD34A6AFDDB3F68F35E7BFC8EC95861109": "ben@decadent.org.uk",
}
MARKER = ".pimpmykernel-job.json"


def sha256(path: Path, cancel: threading.Event | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            if cancel and cancel.is_set():
                raise Cancelled(tr("Vorgang abgebrochen."))
            digest.update(chunk)
    return digest.hexdigest()


def create_workspace(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix="pmk-", dir=root)).resolve()
    (path / MARKER).write_text(json.dumps({"id": path.name, "uid": os.getuid()}))
    return path


def cleanup_workspace(path: Path, root: Path) -> None:
    """Delete exactly one private job, never a caller-supplied arbitrary directory."""
    if path.is_symlink() or path.resolve().parent != root.resolve() or not path.name.startswith("pmk-"):
        raise KernelError(tr("Aufräumen abgelehnt: Das Verzeichnis gehört nicht zu diesem Arbeitsbereich."))
    try:
        marker = path / MARKER
        if marker.is_symlink():
            raise ValueError(tr("Markierung ist ein Link"))
        data = json.loads(marker.read_text())
        if data != {"id": path.name, "uid": os.getuid()} or path.stat().st_uid != os.getuid():
            raise ValueError(tr("Unpassende Markierung"))
    except (OSError, ValueError) as exc:
        raise KernelError(tr("Aufräumen abgelehnt: Die Arbeitsbereich-Markierung fehlt oder ist ungültig.")) from exc
    shutil.rmtree(path)


def extract_sources(archive: Path, workspace: Path, version: str,
                    cancel: threading.Event, log: Callable[[str], None]) -> Path:
    expected = f"linux-{version}"
    target = workspace / "compile"
    target.mkdir()
    total = 0
    try:
        with tarfile.open(archive, "r|") as source:
            for member in source:
                if cancel.is_set():
                    raise Cancelled(tr("Vorgang abgebrochen."))
                name = PurePosixPath(member.name)
                if (name.is_absolute() or ".." in name.parts or not name.parts
                        or name.parts[0] != expected):
                    raise KernelError(tr("Das Archiv enthält einen unerwarteten oder unsicheren Pfad."))
                total += member.size
                if total > 8 * 1024**3 or member.size > 1024**3:
                    raise KernelError(tr("Das entpackte Kernel-Archiv ist unerwartet groß."))
                source.extract(member, target, filter="data")
    except (tarfile.TarError, OSError) as exc:
        raise KernelError(tr('Das Kernel-Archiv konnte nicht sicher entpackt werden: {exc}', exc=exc)) from exc
    tree = target / expected
    if not (tree / "Makefile").is_file() or not (tree / "scripts/config").is_file():
        raise KernelError(tr("Das Archiv enthält keinen vollständigen Kernel-Quellbaum."))
    log(tr('Quellcode entpackt: {tree}', tree=tree))
    return tree


@dataclass(frozen=True)
class BuildResult:
    version: str
    directory: Path
    packages: tuple[Path, ...]
    log_path: Path
    workspace: Path | None
    signer: str
    warning: str = ""


class BuildEngine:
    def __init__(self, options: BuildOptions, cancel: threading.Event,
                 log: Callable[[str], None], stage: Callable[[int, str, str], None],
                 progress: Callable[[int, int], None], *, work_root: Path | None = None,
                 review_config: Callable[[str], bool] | None = None):
        self.options = options
        self.cancel = cancel
        self.on_log = log
        self.stage = stage
        self.progress = progress
        self.work_root = work_root or cache_home() / "jobs"
        self.workspace: Path | None = None
        self.log_path: Path | None = None
        self.log_stream = None
        self.runner = CommandRunner(cancel, self.log)
        self.network = Network(cancel)
        self.config_changes: list[str] = []
        self.review_config = review_config

    def log(self, line: str) -> None:
        if self.log_stream:
            self.log_stream.write(line + "\n")
            self.log_stream.flush()
        self.on_log(line)

    def check_cancel(self) -> None:
        if self.cancel.is_set():
            raise Cancelled(tr("Build abgebrochen. Die Arbeitsdateien bleiben zur Prüfung erhalten."))

    def preflight(self) -> None:
        self.options.validate()
        if os.geteuid() == 0:
            raise KernelError(tr("Kernel-Builds dürfen nicht als root gestartet werden."))
        if not SystemInfo.detect().debian:
            raise KernelError(tr("Der .deb-Build und die Installation werden auf Debian/Ubuntu unterstützt."))
        missing = missing_dependencies()
        if missing:
            raise KernelError(tr("Es fehlen Build-Werkzeuge: ") + ", ".join(missing)
                              + tr(". Bitte zuerst über Systemcheck installieren."))
        if self.options.menuconfig:
            menuconfig_terminal()
            if self.review_config is None:
                raise KernelError(tr("make menuconfig benötigt eine anschließende Konfigurationsprüfung."))
        self.work_root.mkdir(parents=True, exist_ok=True)
        self.options.output.mkdir(parents=True, exist_ok=True)
        root = self.work_root.resolve()
        output = self.options.output.resolve()
        if output == root or root in output.parents:
            raise KernelError(tr("Das Ausgabeverzeichnis muss außerhalb der temporären Build-Jobs liegen."))
        required = (10 if self.options.skip_debug else 25) * 1024**3
        if shutil.disk_usage(root).free < required:
            raise KernelError(tr('Im Arbeitsbereich fehlen freie {value} GiB für den Build.', value=required // 1024 ** 3))
        # Check write access before a long build, including read-only mounted paths.
        for directory in (root, output):
            with tempfile.TemporaryFile(dir=directory) as probe:
                probe.write(b"PimpMyKernel")

    def run(self) -> BuildResult:
        try:
            logs = state_home() / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6]
            self.log_path = logs / f"build-{stamp}.log"
            self.log_stream = self.log_path.open("x", encoding="utf-8", buffering=1)
            self.log(f"PimpMyKernel · {self.options.release.version} · {datetime.now().isoformat(timespec='seconds')}")
            self.stage(0, tr("System prüfen"), tr("Werkzeuge, Konfiguration und Speicherplatz prüfen"))
            self.preflight()
            self.check_cancel()
            self.workspace = create_workspace(self.work_root)
            self.runner.temp_dir = self.workspace / "tmp"
            self.runner.temp_dir.mkdir(mode=0o700)
            archive = self.workspace / f"linux-{self.options.release.version}.tar.xz"
            self.stage(1, tr("Quellcode herunterladen"), tr("Offizielles Release von kernel.org"))
            self.log(tr('Quelle: {source}', source=self.options.release.source))
            self.network.download(self.options.release.source, archive, self.progress)
            self.check_cancel()
            self.stage(2, tr("Signatur prüfen"), tr("Archiv entpacken und Herausgeber mit OpenPGP prüfen"))
            tar_path, signer = self.verify_archive(archive)
            self.stage(3, tr("Quellcode vorbereiten"), tr("Signierten Quellbaum sicher entpacken"))
            tree = extract_sources(tar_path, self.workspace, self.options.release.version,
                                   self.cancel, self.log)
            tar_path.unlink()
            self.stage(4, tr("Konfiguration übernehmen"), f"{self.options.config.name} · make oldconfig")
            self.configure(tree)
            if self.options.menuconfig:
                self.edit_configuration(tree)
            self.check_cancel()
            self.stage(5, tr("Kernel kompilieren"), tr('make -j{build_jobs} · Das kann länger dauern', build_jobs=self.options.build_jobs))
            self.runner.run(["make", f"-j{self.options.build_jobs}", "LOCALVERSION=-pmk"], tree)
            self.check_cancel()
            self.stage(6, tr("Debian-Pakete erstellen"), f"make bindeb-pkg -j{self.options.package_jobs}")
            self.runner.run(["make", "bindeb-pkg", f"-j{self.options.package_jobs}",
                             "LOCALVERSION=-pmk", "KDEB_PKGVERSION=" + self.options.release.version + "-1"], tree)
            self.check_cancel()
            self.stage(7, tr("Pakete speichern"), tr("Pakete, Prüfsummen und Konfiguration exportieren"))
            directory, packages = self.export(tree, signer, stamp)
            self.log(tr('Pakete erfolgreich gespeichert: {directory}', directory=directory))
            warning = ""
            workspace = self.workspace
            self.stage(8, tr("Aufräumen"), tr("Download und eigenen Arbeitsordner entfernen") if self.options.cleanup
                       else tr("Arbeitsordner auf Wunsch behalten"))
            if self.options.cleanup:
                try:
                    cleanup_workspace(self.workspace, self.work_root)
                    workspace = None
                    self.log(tr("Download und Arbeitsordner gelöscht."))
                except (OSError, KernelError) as exc:
                    warning = tr('Pakete sind gespeichert. Aufräumen fehlgeschlagen: {exc}', exc=exc)
                    self.log(warning)
            # Preserve the final cleanup outcome in the exported log as well.
            try:
                shutil.copy2(self.log_path, directory / "build.log")
            except OSError as exc:
                warning += ("\n" if warning else "") + tr('Abschlussprotokoll nicht aktualisiert: {exc}', exc=exc)
                self.log(warning)
            return BuildResult(self.options.release.version + "-pmk", directory,
                               tuple(packages), self.log_path, workspace, signer, warning)
        except Exception as exc:
            self.log(str(exc))
            if self.workspace:
                self.log(tr('Arbeitsordner zur Prüfung erhalten: {workspace}', workspace=self.workspace))
            raise
        finally:
            if self.log_stream:
                self.log_stream.close()
                self.log_stream = None

    def verify_archive(self, archive: Path) -> tuple[Path, str]:
        signature = archive.with_name(archive.name.removesuffix(".xz") + ".sign")
        self.network.download(self.options.release.signature, signature, lambda *_: None,
                              limit=1024 * 1024)
        tar_path = archive.with_suffix("")
        total = 0
        try:
            with lzma.open(archive, "rb") as source, tar_path.open("xb") as target:
                while chunk := source.read(1024 * 1024):
                    self.check_cancel()
                    total += len(chunk)
                    if total > 8 * 1024**3:
                        raise KernelError(tr("Das entpackte Archiv ist unerwartet groß."))
                    target.write(chunk)
        except (lzma.LZMAError, EOFError) as exc:
            raise KernelError(tr("Das heruntergeladene Archiv ist beschädigt.")) from exc
        keyring = self.workspace / "keyring"
        keyring.mkdir(mode=0o700)
        verify = ["gpg", "--batch", "--no-auto-key-retrieve", "--homedir", str(keyring),
                  "--status-fd", "1", "--verify", str(signature), str(tar_path)]
        _, status = self.runner.run(verify, check=False, timeout=120)
        missing = re.findall(r"\[GNUPG:\] NO_PUBKEY ([0-9A-F]+)", status)
        if not missing:
            raise KernelError(tr("Die Release-Signatur konnte nicht einem bekannten Herausgeber zugeordnet werden."))
        for key_id in set(missing):
            matches = [fingerprint for fingerprint in SIGNERS if fingerprint.endswith(key_id)]
            if len(matches) != 1:
                raise KernelError(tr("Die Signatur stammt nicht von einem hinterlegten Kernel-Herausgeber."))
            fingerprint = matches[0]
            self.log(tr('Öffentlichen Schlüssel laden: {value} · {fingerprint}', value=SIGNERS[fingerprint], fingerprint=fingerprint))
            # kernel.org recommends WKD; unlike some keyservers it retains the
            # UID information required by stock GnuPG. Trust remains pinned to
            # the primary fingerprint checked below, not merely to the email.
            self.runner.run(["gpg", "--batch", "--homedir", str(keyring), "--auto-key-locate",
                             "clear,wkd", "--locate-keys", SIGNER_EMAILS[fingerprint]], timeout=60)
        code, status = self.runner.run(verify, check=False, timeout=120)
        valid = re.findall(r"\[GNUPG:\] VALIDSIG (.+)", status)
        signer = None
        for record in valid:
            fields = record.split()
            # gpg reports the primary fingerprint after the signing subkey fields.
            primary = fields[9] if len(fields) >= 10 else fields[0]
            if primary in SIGNERS:
                signer = SIGNERS[primary]
        if code or not signer:
            raise KernelError(tr("OpenPGP-Prüfung fehlgeschlagen. Es wird kein Build gestartet."))
        self.log(tr('Release-Signatur gültig: {signer}', signer=signer))
        return tar_path, signer

    def configure(self, tree: Path) -> None:
        config_source = self.options.config
        try:
            if config_source.stat().st_size > 4 * 1024 * 1024:
                raise KernelError(tr("Die gewählte Konfiguration ist unerwartet groß."))
            text = config_source.read_text()
        except PermissionError:
            self.log(tr("Konfiguration geschützt: grafische Administratorfreigabe zum Lesen erforderlich."))
            _, text = self.runner.run(privileged_command("read-config", str(config_source)),
                                      echo=False, capture_lines=None, log_output=False)
        if "CONFIG_" not in text or "\x00" in text:
            raise KernelError(tr("Die gewählte Datei enthält keine gültige Kernel-Konfiguration."))
        (tree / ".config").write_text(text + ("" if text.endswith("\n") else "\n"))
        (self.workspace / "original.config").write_text(text)
        self.log(tr('Konfiguration übernommen: {config_source}', config_source=config_source))
        self.runner.run(["make", "oldconfig"], tree, defaults=True)
        config = tree / "scripts/config"
        self.runner.run([str(config), "--set-str", "LOCALVERSION", "", "--disable", "LOCALVERSION_AUTO"], tree)
        self.config_changes.append(tr("Lokaler Kernel-Suffix: -pmk (eigener Paketname)"))
        if self.options.skip_debug:
            self.runner.run([str(config), "--disable", "DEBUG_INFO", "--disable", "DEBUG_INFO_BTF",
                             "--disable", "DEBUG_INFO_BTF_MODULES", "--enable", "DEBUG_INFO_NONE"], tree)
            self.config_changes.append(tr("Debugsymbole/BTF weggelassen"))
        if self.options.repair_certificates:
            content = (tree / ".config").read_text()
            for symbol in ("SYSTEM_TRUSTED_KEYS", "SYSTEM_REVOCATION_KEYS", "MODULE_SIG_KEY"):
                match = re.search(rf'^CONFIG_{symbol}="(.*)"$', content, re.MULTILINE)
                value = match[1] if match else ""
                if not value or value.startswith("pkcs11:"):
                    continue
                # The default module signing key is generated automatically by kbuild.
                if symbol == "MODULE_SIG_KEY" and value == "certs/signing_key.pem":
                    continue
                paths = value.split()
                if any(not (tree / path).is_file() for path in paths):
                    replacement = "certs/signing_key.pem" if symbol == "MODULE_SIG_KEY" else ""
                    self.runner.run([str(config), "--set-str", symbol, replacement], tree)
                    self.config_changes.append(
                        tr('CONFIG_{symbol}: "{value}" → "{replacement}" (fehlender Zertifikatpfad)', symbol=symbol, value=value, replacement=replacement))
        self.runner.run(["make", "olddefconfig"], tree)
        for change in self.config_changes:
            self.log(tr("Konfiguration: ") + change)

    def edit_configuration(self, tree: Path) -> None:
        self.check_cancel()
        before = (tree / ".config").read_text()
        (self.workspace / "before-menuconfig.config").write_text(before)
        self.stage(4, tr("Kernel-Konfiguration selbst anpassen"), tr("make menuconfig ist im Terminal geöffnet. "
                   "Änderungen in .config speichern und das Menü beenden."))
        run_menuconfig(self.runner, tree, self.workspace)
        self.check_cancel()
        config_path = tree / ".config"
        if (config_path.is_symlink() or not config_path.is_file()
                or config_path.stat().st_size > 4 * 1024 * 1024):
            raise KernelError(tr("menuconfig hat keine gültige .config-Datei hinterlassen."))
        content = config_path.read_text()
        if "\x00" in content or not re.search(r"^CONFIG_[A-Z0-9_]+=", content, re.MULTILINE):
            raise KernelError(tr("Die gespeicherte menuconfig-Konfiguration ist ungültig."))
        # The user's other choices take precedence over the automatic adjustments.
        # Keep only the application's package suffix invariant.
        self.runner.run([str(tree / "scripts/config"), "--set-str", "LOCALVERSION", "",
                         "--disable", "LOCALVERSION_AUTO"], tree)
        self.runner.run(["make", "olddefconfig"], tree)
        after = config_path.read_text()
        changes = "\n".join(difflib.unified_diff(before.splitlines(), after.splitlines(),
                                              fromfile=tr("Vor menuconfig"), tofile=tr("Nach menuconfig"), lineterm=""))
        (self.workspace / "menuconfig-changes.diff").write_text(changes + ("\n" if changes else ""))
        self.stage(4, tr("Konfiguration prüfen"), tr("Prüfe die gespeicherten menuconfig-Änderungen in PimpMyKernel. "
                   "Der Build wartet auf deine Entscheidung."))
        if not self.review_config(changes):
            raise Cancelled(tr("Die menuconfig-Konfiguration wurde nicht übernommen. Build abgebrochen."))
        self.check_cancel()
        self.config_changes.append(tr("Manuelle Konfiguration mit make menuconfig geprüft und übernommen"))
        self.log(tr("Konfiguration: ") + self.config_changes[-1])

    def export(self, tree: Path, signer: str, stamp: str) -> tuple[Path, list[Path]]:
        candidates = sorted(tree.parent.glob("*.deb"))
        if not candidates:
            raise KernelError(tr("Der Paketbau hat keine .deb-Dateien erzeugt."))
        metadata = []
        has_image = False
        for package in candidates:
            self.check_cancel()
            if package.is_symlink() or not package.is_file():
                raise KernelError(tr("Der Paketbau hat einen unerwarteten Dateilink erzeugt."))
            _, fields = self.runner.run(["dpkg-deb", "--field", str(package),
                                         "Package", "Version", "Architecture"])
            info = dict(line.split(": ", 1) for line in fields.splitlines() if ": " in line)
            name = info.get("Package", "")
            if not re.fullmatch(r"linux-(?:image|headers|libc-dev)[a-z0-9+.-]*", name):
                raise KernelError(tr('Unerwartetes Build-Paket: {value}', value=name or package.name))
            has_image |= name.startswith("linux-image-") and not name.endswith("-dbg")
            metadata.append({"file": package.name, "package": name,
                             "version": info.get("Version", ""), "architecture": info.get("Architecture", ""),
                             "sha256": sha256(package, self.cancel), "bytes": package.stat().st_size})
        if not has_image:
            raise KernelError(tr("Der Build enthält kein installierbares Kernel-Image."))
        needed = sum(item["bytes"] for item in metadata) + 16 * 1024**2
        if shutil.disk_usage(self.options.output).free < needed:
            raise KernelError(tr("Das Ausgabeverzeichnis hat zu wenig freien Platz für die Pakete."))
        destination = self.options.output / f"linux-{self.options.release.version}-pmk-{stamp}"
        staging = Path(tempfile.mkdtemp(prefix=".pmk-export-", dir=self.options.output))
        try:
            for package, item in zip(candidates, metadata):
                self.check_cancel()
                target = staging / package.name
                # Copy in chunks so large debug packages remain cancellable.
                with package.open("rb") as source, target.open("xb") as output:
                    while chunk := source.read(1024 * 1024):
                        self.check_cancel()
                        output.write(chunk)
                if sha256(target, self.cancel) != item["sha256"]:
                    raise KernelError(tr("Eine exportierte Paketdatei besteht die Prüfsummenprüfung nicht."))
            shutil.copy2(tree / ".config", staging / "kernel.config")
            shutil.copy2(self.workspace / "original.config", staging / "original.config")
            diff = difflib.unified_diff((self.workspace / "original.config").read_text().splitlines(True),
                                        (tree / ".config").read_text().splitlines(True),
                                        fromfile="original.config", tofile="kernel.config")
            (staging / "config-changes.diff").write_text("".join(diff))
            if self.options.menuconfig:
                for name in ("before-menuconfig.config", "menuconfig-changes.diff"):
                    shutil.copy2(self.workspace / name, staging / name)
            manifest = {
                "schema": 1, "version": self.options.release.version + "-pmk",
                "created": datetime.now(timezone.utc).isoformat(), "source": self.options.release.source,
                "signature": self.options.release.signature, "verified_signer": signer,
                "config_source": str(self.options.config), "config_changes": self.config_changes,
                "build_jobs": self.options.build_jobs, "package_jobs": self.options.package_jobs,
                "menuconfig": self.options.menuconfig,
                "packages": metadata,
            }
            (staging / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
            shutil.copy2(self.log_path, staging / "build.log")
            self.check_cancel()
            staging.rename(destination)
        except Exception:
            shutil.rmtree(staging)
            raise
        return destination, [destination / package.name for package in candidates]


def install_packages(result: BuildResult, runner: CommandRunner) -> None:
    if secure_boot_state() == "on":
        raise KernelError(tr("Secure Boot ist aktiv. Dieser selbst gebaute Kernel muss vor der Installation signiert werden."))
    try:
        manifest = json.loads((result.directory / "manifest.json").read_text())
        recorded = {item["file"]: item["sha256"] for item in manifest["packages"]}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise KernelError(tr("Das Build-Manifest ist nicht lesbar. Installation abgebrochen.")) from exc
    for package in result.packages:
        if (package.parent != result.directory or package.is_symlink() or not package.is_file()
                or recorded.get(package.name) != sha256(package)):
            raise KernelError(tr('Paket fehlt oder wurde verändert: {name}', name=package.name))
    runner.run(privileged_command("install", *[str(path) for path in result.packages]))
