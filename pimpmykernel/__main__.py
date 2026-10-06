"""Startpunkt für `python3 -m pimpmykernel` und den Desktop-Starter."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .i18n import initialize_language, install_qt_translations, tr
from . import __version__


def main() -> int:
    initialize_language()
    parser = argparse.ArgumentParser(description=tr("PimpMyKernel · Linux-Kernel als .deb bauen"))
    parser.add_argument("--version", action="version", version=f"PimpMyKernel {__version__}")
    parser.add_argument("--check", action="store_true", help=tr("Systemcheck ohne GUI als JSON ausgeben"))
    parser.add_argument("--list-releases", action="store_true", help=tr("Aktuelle Releases ohne GUI anzeigen"))
    parser.add_argument("--screenshot", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.check:
        import json
        from dataclasses import asdict
        from .models import SystemInfo, boot_configs, missing_dependencies
        data = asdict(SystemInfo.detect())
        data["missing_dependencies"] = missing_dependencies()
        data["configs"] = [str(p) for p in boot_configs()]
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0
    if args.list_releases:
        from .models import KernelError
        from .network import Network
        try:
            for release in Network().releases():
                print(f"{release.version:14} {release.label:27} {release.date}"
                      + (tr("  (nur Anzeige)") if not release.buildable else ""))
        except KernelError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0
    if os.geteuid() == 0:
        print(tr("Bitte PimpMyKernel als normalen Benutzer starten, ohne sudo."), file=sys.stderr)
        return 1
    try:
        from PyQt6.QtCore import QCoreApplication, QLibraryInfo
        # Some Linux desktop setups inject the Qt5 platform-plugin directory.
        # This process uses Qt6; do not load another Qt major version's plugins.
        plugins = QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath)
        os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = plugins + "/platforms"
        QCoreApplication.setLibraryPaths([plugins])
        from PyQt6.QtCore import QTimer
        from PyQt6.QtWidgets import QApplication
        from .gui import MainWindow
    except ModuleNotFoundError:
        print(tr("PyQt6 fehlt. Ubuntu/Debian: sudo apt install python3-pyqt6\n"
              "Alternativ: python3 -m venv .venv && .venv/bin/pip install -e ."), file=sys.stderr)
        return 1
    app = QApplication(sys.argv[:1])
    install_qt_translations(app)
    app.setApplicationName("PimpMyKernel")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("PimpMyKernel")
    app.setDesktopFileName("pimpmykernel")
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    if args.screenshot:
        def capture():
            if window.worker is not None:
                QTimer.singleShot(250, capture)
                return
            args.screenshot.parent.mkdir(parents=True, exist_ok=True)
            window.grab().save(str(args.screenshot))
            app.quit()
        QTimer.singleShot(2000, capture)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
