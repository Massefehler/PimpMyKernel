"""Optional: echten Kernel laden, Signatur prüfen und oldconfig testen; kein Build."""

import argparse
import re
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pimpmykernel.build import BuildEngine, create_workspace, extract_sources
from pimpmykernel.models import BuildOptions, boot_configs
from pimpmykernel.network import Network


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Kernel-Konfiguration für den echten Vorbereitungstest")
    args = parser.parse_args()
    config_source = args.config or boot_configs()[0]
    original_config = config_source.read_text()
    symbols = ("SYSTEM_TRUSTED_KEYS", "SYSTEM_REVOCATION_KEYS", "MODULE_SIG_KEY")

    def certificate_settings(content):
        return {symbol: match[1] if (match := re.search(rf'^CONFIG_{symbol}="(.*)"$', content, re.MULTILINE))
                else None for symbol in symbols}

    before = certificate_settings(original_config)
    network = Network()
    release = next(r for r in network.releases() if r.channel == "stable" and r.buildable)
    with tempfile.TemporaryDirectory(prefix="pmk-upstream-test-") as tmp:
        root = Path(tmp)
        options = BuildOptions(release, config_source, root / "output", 1, 1)
        engine = BuildEngine(options, threading.Event(), print, lambda *_: None, lambda *_: None,
                             work_root=root / "jobs")
        engine.workspace = create_workspace(engine.work_root)
        archive = engine.workspace / f"linux-{release.version}.tar.xz"
        last = [0]

        def progress(current, total):
            mib = current // 1024**2
            if mib - last[0] >= 25:
                print(f"Download: {mib} MiB", flush=True)
                last[0] = mib

        print(f"Echtes Release vorbereiten: {release.version}", flush=True)
        print(f"Konfiguration: {config_source}", flush=True)
        network.download(release.source, archive, progress)
        tar_path, signer = engine.verify_archive(archive)
        tree = extract_sources(tar_path, engine.workspace, release.version, engine.cancel, print)
        engine.configure(tree)
        after = certificate_settings((tree / ".config").read_text())
        for symbol, canonical_path in (("SYSTEM_TRUSTED_KEYS", "debian/canonical-certs.pem"),
                                       ("SYSTEM_REVOCATION_KEYS", "debian/canonical-revoked-certs.pem")):
            if before[symbol] == canonical_path and not (tree / canonical_path).is_file():
                if after[symbol] != "":
                    raise AssertionError(f"Fehlender Canonical-Pfad wurde nicht bereinigt: {symbol}")
        if before["MODULE_SIG_KEY"] == "certs/signing_key.pem":
            if after["MODULE_SIG_KEY"] != "certs/signing_key.pem":
                raise AssertionError("Automatisch erzeugter Modulschlüssel wurde verändert")
        if config_source.read_text() != original_config:
            raise AssertionError("Ursprüngliche Konfiguration wurde verändert")
        print("Zertifikate nach make olddefconfig:", after, flush=True)
        print("Paketbau-Regeln:")
        package_makefile = tree / "scripts/Makefile.package"
        for line in package_makefile.read_text().splitlines():
            if any(term in line for term in ["bindeb", "dpkg-buildpackage", "KDEB"]):
                print(line)
        print(f"OK: Download, Signatur ({signer}), Entpacken und make oldconfig/olddefconfig", flush=True)
        print("Kein Kernel kompiliert oder installiert. Temporäre Testdateien werden entfernt.", flush=True)


if __name__ == "__main__":
    main()
