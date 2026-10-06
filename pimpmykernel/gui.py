"""Qt-Oberfläche in der Systemsprache; lange Aktionen laufen im Worker."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QDate, QLocale, QSettings, QSize, Qt, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QCloseEvent, QDesktopServices, QIcon
from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QCheckBox,
                            QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFrame,
                            QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                            QListWidgetItem, QMainWindow, QMessageBox, QPlainTextEdit,
                            QProgressBar, QPushButton, QScrollArea, QSizePolicy,
                            QStackedWidget, QVBoxLayout, QWidget)

from .i18n import language_preference, plural, tr
from . import __version__
from .build import BuildEngine, BuildResult, cleanup_workspace, install_packages
from .kernel_packages import InstalledKernel, RemovalPlan, installed_kernels, plan_kernel_removal
from .menuconfig import menuconfig_terminal
from .models import (BUILD_PACKAGES, BuildOptions, Cancelled, KernelError, Release,
                     SystemInfo, boot_configs, cache_home, default_output, job_choices,
                     missing_dependencies, upstream_version, version_key)
from .network import Network
from .runner import CommandRunner, privileged_command
from .style import STYLE


ASSETS = Path(__file__).with_name("assets")
PAGE_INSTALLED = 3
PAGE_SYSTEM = 4


def format_number(value: float, decimals: int = 1) -> str:
    locale = QLocale()
    locale.setNumberOptions(locale.numberOptions() | locale.NumberOption.OmitGroupSeparator)
    return locale.toString(float(value), "f", decimals)


def label(text: str, name: str = "", wrap: bool = False) -> QLabel:
    widget = QLabel(text)
    widget.setObjectName(name)
    widget.setWordWrap(wrap)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    return widget


def button(text: str, callback: Callable | None = None, name: str = "") -> QPushButton:
    widget = QPushButton(text)
    widget.setObjectName(name)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    if callback:
        widget.clicked.connect(callback)
    return widget


def card(name: str = "card") -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName(name)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(22, 20, 22, 20)
    layout.setSpacing(12)
    return frame, layout


def scrollable(widget: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setWidget(widget)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    return area


class Worker(QThread):
    logged = pyqtSignal(str)
    staged = pyqtSignal(int, str, str)
    progressed = pyqtSignal(object, object)
    config_review = pyqtSignal(object)
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str, bool)

    def __init__(self, task: Callable, parent=None):
        super().__init__(parent)
        self.task = task
        self.cancel = threading.Event()

    def run(self):
        try:
            self.succeeded.emit(self.task(self))
        except Exception as exc:
            self.failed.emit(str(exc), isinstance(exc, Cancelled))


@dataclass
class ConfigReviewRequest:
    worker: Worker
    changes: str
    completed: threading.Event = field(default_factory=threading.Event)
    accepted: bool = False


class ConfigReviewDialog(QDialog):
    def __init__(self, changes: str, parent: QWidget):
        super().__init__(parent)
        self.setWindowTitle(tr("menuconfig · Konfiguration prüfen"))
        self.resize(860, 620)
        self.setMinimumSize(660, 440)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 26, 28, 26)
        layout.setSpacing(16)
        layout.addWidget(label(tr("Deine Kernel-Konfiguration"), "heading"))
        layout.addWidget(label(tr("make menuconfig ist beendet. Prüfe die gespeicherten Änderungen "
                               "gegenüber der vorbereiteten Konfiguration. Erst mit „Build fortsetzen“ "
                               "beginnt die Kompilierung."), "muted", True))
        self.changes = QPlainTextEdit(changes or tr("Keine Änderungen gegenüber der vorbereiteten Konfiguration gespeichert."))
        self.changes.setReadOnly(True)
        self.changes.setAccessibleName(tr("Gespeicherte menuconfig-Änderungen"))
        layout.addWidget(self.changes, 1)
        layout.addWidget(label(tr("Fehlerhafte Optionen können fehlende Treiber oder einen nicht startfähigen "
                               "Kernel verursachen. Der eigene Kernel behält den Suffix -pmk."), "error", True))
        controls = QDialogButtonBox()
        abort = controls.addButton(tr("Build abbrechen"), QDialogButtonBox.ButtonRole.RejectRole)
        abort.setObjectName("danger")
        self.proceed = controls.addButton(tr("Build fortsetzen"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.proceed.setObjectName("primary")
        self.proceed.setAutoDefault(False)
        abort.setDefault(True)
        abort.clicked.connect(self.reject)
        self.proceed.clicked.connect(self.accept)
        layout.addWidget(controls)


class InstallDialog(QDialog):
    def __init__(self, result: BuildResult, system: SystemInfo, parent: QWidget):
        super().__init__(parent)
        self.setWindowTitle(tr("Kernel installieren"))
        self.setMinimumWidth(660)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 26, 28, 26)
        layout.setSpacing(16)
        layout.addWidget(label(tr('Linux {version} installieren', version=result.version), "heading"))
        layout.addWidget(label(tr("Die folgenden Pakete werden mit APT installiert. Dafür öffnet sich "
                               "die Passwortabfrage deines Systems."), "muted", True))
        files = QPlainTextEdit("\n".join(p.name for p in result.packages))
        files.setReadOnly(True)
        files.setMaximumHeight(145)
        layout.addWidget(files)
        layout.addWidget(label(tr("Deine bisherigen Kernel bleiben als Rückfallmöglichkeit erhalten. "
                               "Nach der Installation kannst du den neuen Kernel durch einen Neustart verwenden. "
                               "Treiber wie NVIDIA/DKMS benötigen passende Module für den neuen Kernel. "
                               "Die gespeicherten .deb-Dateien bleiben auch nach der Installation im Exportordner erhalten."), "muted", True))
        self.accept_box = QCheckBox(tr("Ich habe die Paketliste geprüft und möchte diesen Kernel installieren."))
        layout.addWidget(self.accept_box)
        self.secure_box = None
        blocked = system.secure_boot == "on"
        if blocked:
            layout.addWidget(label(tr("Secure Boot ist aktiv. Die Installation ist hier gesperrt, weil der "
                                   "Kernel noch nicht für deine Firmware signiert ist. Die Pakete kannst du "
                                   "für einen separaten Signierungsablauf verwenden."), "error", True))
        elif system.secure_boot == "unknown":
            layout.addWidget(label(tr("Der Secure-Boot-Status konnte nicht erkannt werden. Ein unsignierter "
                                   "Kernel startet bei aktivem Secure Boot nicht."), "muted", True))
            self.secure_box = QCheckBox(tr("Secure Boot ist deaktiviert oder ich habe die Signierung eingerichtet."))
            layout.addWidget(self.secure_box)
        controls = QDialogButtonBox()
        cancel = controls.addButton(tr("Zurück"), QDialogButtonBox.ButtonRole.RejectRole)
        self.install = controls.addButton(tr("Jetzt installieren"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.install.setObjectName("primary")
        self.install.setEnabled(False)
        cancel.clicked.connect(self.reject)
        self.install.clicked.connect(self.accept)

        def update():
            self.install.setEnabled(not blocked and self.accept_box.isChecked()
                                    and (self.secure_box is None or self.secure_box.isChecked()))

        self.accept_box.toggled.connect(update)
        if self.secure_box:
            self.secure_box.toggled.connect(update)
        layout.addWidget(controls)


class RemoveKernelsDialog(QDialog):
    def __init__(self, plan: RemovalPlan, running: str, parent: QWidget):
        super().__init__(parent)
        self.setWindowTitle(tr("Kernel deinstallieren"))
        self.setMinimumWidth(660)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 26, 28, 26)
        layout.setSpacing(16)
        layout.addWidget(label(tr("Ausgewählte Kernel deinstallieren"), "heading"))
        layout.addWidget(label(tr("Kernel: ") + ", ".join(plan.releases), wrap=True))
        layout.addWidget(label(tr("Diese Pakete werden mit APT entfernt. Dafür erscheint die "
                               "Passwortabfrage deines Systems."), "muted", True))
        files = QPlainTextEdit("\n".join(plan.packages))
        files.setReadOnly(True)
        files.setMaximumHeight(180)
        layout.addWidget(files)
        layout.addWidget(label(tr('Der laufende Kernel {running} bleibt erhalten. Gespeicherte .deb-Dateien und Build-Protokolle bleiben im Exportordner verfügbar.', running=running), "muted", True))
        self.accept_box = QCheckBox(tr("Ich habe die Paketliste geprüft und möchte diese Kernel deinstallieren."))
        layout.addWidget(self.accept_box)
        controls = QDialogButtonBox()
        back = controls.addButton(tr("Zurück"), QDialogButtonBox.ButtonRole.RejectRole)
        self.remove = controls.addButton(tr("Jetzt deinstallieren"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.remove.setObjectName("danger")
        self.remove.setEnabled(False)
        self.accept_box.toggled.connect(self.remove.setEnabled)
        back.clicked.connect(self.reject)
        self.remove.clicked.connect(self.accept)
        layout.addWidget(controls)


class CancelCleanupDialog(QDialog):
    def __init__(self, workspace: Path, parent: QWidget):
        super().__init__(parent)
        self.setWindowTitle(tr("Build abgebrochen · Aufräumen"))
        self.setMinimumWidth(660)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 26, 28, 26)
        layout.setSpacing(16)
        layout.addWidget(label(tr("Build abgebrochen"), "heading"))
        layout.addWidget(label(tr("Die Build-Prozesse wurden beendet. Möchtest du die Arbeitsdateien "
                               "dieses Builds jetzt löschen?"), wrap=True))
        layout.addWidget(label(tr("Entfernt werden der Download, der entpackte Quellcode, bisherige "
                               "Kompilierdateien und temporäre Hilfsdateien aus diesem Ordner:"), "muted", True))
        path = label(str(workspace), "small", True)
        path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(path)
        layout.addWidget(label(tr("Das gespeicherte Build-Protokoll und bereits exportierte Pakete bleiben "
                               "erhalten. Zur Fehlersuche kannst du die Arbeitsdateien behalten und "
                               "später über „Arbeitsordner löschen“ entfernen."), "muted", True))
        controls = QDialogButtonBox()
        self.keep = controls.addButton(tr("Zur Fehlersuche behalten"), QDialogButtonBox.ButtonRole.RejectRole)
        self.remove = controls.addButton(tr("Arbeitsdateien löschen"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.remove.setObjectName("primary")
        self.remove.setDefault(True)
        self.keep.clicked.connect(self.reject)
        self.remove.clicked.connect(self.accept)
        layout.addWidget(controls)


class MainWindow(QMainWindow):
    def __init__(self, *, auto_refresh: bool = True):
        super().__init__()
        self.setWindowTitle("PimpMyKernel")
        self.setWindowIcon(QIcon(str(ASSETS / "icon.png")))
        self.resize(1280, 920)
        self.setMinimumSize(1040, 740)
        self.settings = QSettings("PimpMyKernel", "PimpMyKernel")
        self.system = SystemInfo.detect()
        self.releases: list[Release] = []
        self.current_releases: list[Release] = []
        self.archived_releases: list[Release] = []
        self.archive_loaded = False
        self.kernels: list[InstalledKernel] = []
        self.kernels_stale = True
        self.pending_kernel_refresh = False
        self.pending_removal_plan: RemovalPlan | None = None
        self.download_current = 0
        self.download_total = 0
        self.worker: Worker | None = None
        self.worker_kind = ""
        self.engine: BuildEngine | None = None
        self.result: BuildResult | None = None
        self.started_at: float | None = None
        self.running = False
        self.close_pending = False
        self.offer_cleanup_on_cancel = False
        self.pending_cancel_cleanup: tuple[Path, Path] | None = None
        self.log_buffer: list[str] = []
        self.setStyleSheet(STYLE.replace("__CHECK_ICON__", str(ASSETS / "check.svg"))
                          .replace("__ARROW_ICON__", str(ASSETS / "chevron.svg")))
        self._create_layout()
        self._load_settings()
        self.refresh_configs()
        self.refresh_system()
        self.log_timer = QTimer(self)
        self.log_timer.timeout.connect(self.flush_logs)
        self.log_timer.start(100)
        self.elapsed_timer = QTimer(self)
        self.elapsed_timer.timeout.connect(self.update_elapsed)
        self.elapsed_timer.start(1000)
        if auto_refresh:
            QTimer.singleShot(150, self.refresh_releases)

    def _create_layout(self):
        central = QWidget()
        central.setObjectName("central")
        self.setCentralWidget(central)
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(222)
        nav = QVBoxLayout(sidebar)
        nav.setContentsMargins(18, 28, 18, 22)
        nav.setSpacing(9)
        logo = QLabel()
        logo.setPixmap(QIcon(str(ASSETS / "icon.png")).pixmap(QSize(64, 64)))
        nav.addWidget(logo)
        nav.addWidget(label("PimpMyKernel", "brand"))
        nav.addWidget(label(tr("Dein Kernel. Dein Tempo."), "small"))
        nav.addSpacing(28)
        self.nav_group = QButtonGroup(self)
        for index, text in enumerate((tr("01   Kernel auswählen"), tr("02   Build vorbereiten"), tr("03   Build && Pakete"),
                                      tr("Installierte Kernel"), tr("Systemcheck"))):
            item = button(text, name="nav")
            item.setCheckable(True)
            self.nav_group.addButton(item, index)
            nav.addWidget(item)
        self.nav_group.button(0).setChecked(True)
        self.nav_group.idClicked.connect(self.navigate)
        nav.addStretch()
        nav.addWidget(label(tr("AKTIVER KERNEL"), "small"))
        nav.addWidget(label(self.system.kernel, "value", True))
        nav.addSpacing(10)
        nav.addWidget(label(tr("{cpus} CPU-Threads · {ram} GiB RAM", cpus=self.system.cpus,
                               ram=format_number(self.system.ram_gib, 0)), "muted"))
        nav.addWidget(label(tr('Version {__version__} · Linux Desktop', __version__=__version__), "small"))
        outer.addWidget(sidebar)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(32, 24, 32, 20)
        body_layout.setSpacing(20)
        header = QHBoxLayout()
        self.page_title = label(tr("Dein nächster Kernel"), "pageTitle")
        header.addWidget(self.page_title)
        header.addStretch()
        self.connection = label("kernel.org", "badge")
        header.addWidget(self.connection)
        body_layout.addLayout(header)
        self.stack = QStackedWidget()
        self.stack.addWidget(self._release_page())
        self.stack.addWidget(self._options_page())
        self.stack.addWidget(self._progress_page())
        self.stack.addWidget(self._installed_page())
        self.stack.addWidget(self._system_page())
        body_layout.addWidget(self.stack, 1)
        footer = QHBoxLayout()
        self.footer_status = label(tr("Bereit · Quellen werden direkt von kernel.org geladen"), "small")
        footer.addWidget(self.footer_status, 1)
        footer.addWidget(button(tr("Hilfe"), self.show_help))
        body_layout.addLayout(footer)
        outer.addWidget(body, 1)

    def _release_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)
        layout.addWidget(label(tr("Wähle ein offizielles Release. Wir kümmern uns um den Weg zu deinen .deb-Paketen."), "muted", True))
        hero, hero_layout = card("hero")
        top = QHBoxLayout()
        top.addWidget(label(tr("NEUESTES STABILES RELEASE"), "eyebrow"))
        top.addStretch()
        top.addWidget(label(tr("Empfohlen"), "badge"))
        hero_layout.addLayout(top)
        self.hero_version = label(tr("Wird geprüft …"), "heroVersion")
        hero_layout.addWidget(self.hero_version)
        self.hero_detail = label(tr("Aktuelle Releases werden von kernel.org abgefragt."), "muted", True)
        hero_layout.addWidget(self.hero_detail)
        self.hero_status = label("", "success", True)
        self.hero_status.hide()
        hero_layout.addWidget(self.hero_status)
        layout.addWidget(hero)
        release_header = QHBoxLayout()
        release_header.addWidget(label(tr("Verfügbare Kernel"), "heading"))
        release_header.addStretch()
        self.show_rc = QCheckBox(tr("Vorabversionen anzeigen"))
        self.show_rc.toggled.connect(self.populate_releases)
        release_header.addWidget(self.show_rc)
        self.refresh_button = button(tr("↻  Aktualisieren"), self.refresh_releases)
        release_header.addWidget(self.refresh_button)
        layout.addLayout(release_header)
        filters = QHBoxLayout()
        self.series_filter = QComboBox()
        self.series_filter.addItem(tr("Alle Kernelreihen"), None)
        self.series_filter.setMinimumWidth(170)
        self.series_filter.setAccessibleName(tr("Kernelreihe filtern"))
        self.series_filter.currentIndexChanged.connect(self.populate_releases)
        filters.addWidget(self.series_filter)
        self.version_search = QLineEdit()
        self.version_search.setPlaceholderText(tr("Version suchen, z. B. 7.2.8"))
        self.version_search.setClearButtonEnabled(True)
        self.version_search.setAccessibleName(tr("Kernel-Version suchen"))
        self.version_search.textChanged.connect(self.populate_releases)
        filters.addWidget(self.version_search, 1)
        self.archive_button = button(tr("Ältere Versionen laden"), self.load_archive_releases)
        self.archive_button.setToolTip(tr("Alle veröffentlichten Versionen ab Linux 4.x aus dem offiziellen Archiv laden."))
        filters.addWidget(self.archive_button)
        layout.addLayout(filters)
        self.release_list = QListWidget()
        self.release_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.release_list.setSpacing(2)
        self.release_list.itemSelectionChanged.connect(self.select_release)
        self.release_list.itemDoubleClicked.connect(lambda _: self.navigate(1) if self.selected_release() else None)
        self.release_list.setAccessibleName(tr("Verfügbare Kernel-Versionen"))
        layout.addWidget(self.release_list, 1)
        self.release_error = label("", "error", True)
        self.release_error.hide()
        layout.addWidget(self.release_error)
        self.release_hint = label(tr("Stable erhält aktuelle Fehlerkorrekturen. LTS bietet langfristige Pflege. "
                                  "Über „Ältere Versionen laden“ findest du frühere Releases aller Reihen."), "small", True)
        layout.addWidget(self.release_hint)
        actions = QHBoxLayout()
        self.selected_label = label(tr("Noch kein Kernel ausgewählt"), "muted")
        actions.addWidget(self.selected_label, 1)
        self.next_button = button(tr("Build vorbereiten  →"), lambda: self.navigate(1), "primary")
        self.next_button.setEnabled(False)
        actions.addWidget(self.next_button)
        layout.addLayout(actions)
        return page

    def _options_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)
        self.options_summary = label(tr("Wähle zuerst einen Kernel im ersten Schritt."), "muted", True)
        layout.addWidget(self.options_summary)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(16)
        configuration, config_layout = card()
        config_layout.addWidget(label(tr("1   Deine bewährte Konfiguration"), "heading"))
        config_layout.addWidget(label(tr("Die neueste Konfiguration aus /boot wird vorausgewählt. Neue Optionen "
                                     "beantwortet make oldconfig mit den Kernel-Standardwerten."), "muted", True))
        config_row = QHBoxLayout()
        self.config_combo = QComboBox()
        self.config_combo.setAccessibleName(tr("Kernel-Konfiguration"))
        config_row.addWidget(self.config_combo, 1)
        self.config_browse = button(tr("Datei wählen …"), self.choose_config)
        config_row.addWidget(self.config_browse)
        config_layout.addLayout(config_row)
        content_layout.addWidget(configuration)
        parallel, parallel_layout = card()
        parallel_layout.addWidget(label(tr("2   Rechenleistung wählen"), "heading"))
        parallel_layout.addWidget(label(tr('{cpus} CPU-Threads erkannt (wie nproc). Mehr Jobs beschleunigen den Build und benötigen mehr RAM.', cpus=self.system.cpus), "muted", True))
        grid = QGridLayout()
        grid.setHorizontalSpacing(22)
        grid.addWidget(label(tr("Kernel kompilieren"), "muted"), 0, 0)
        grid.addWidget(label(tr("Debian-Pakete bauen"), "muted"), 0, 1)
        self.build_jobs = QComboBox()
        self.package_jobs = QComboBox()
        recommended = max(1, min(self.system.cpus - 2, int(self.system.ram_gib / 2) or 1))
        choices = job_choices(self.system.cpus)
        recommended = max(n for n in choices if n <= recommended)
        for combo in (self.build_jobs, self.package_jobs):
            for n in choices:
                text = plural("-j{count} · {count} Job", "-j{count} · {count} Jobs", n)
                combo.addItem(text + (tr(" · empfohlen") if n == recommended else ""), n)
            combo.setCurrentIndex(combo.findData(recommended))
        self.build_jobs.setAccessibleName(tr("Parallele Jobs für make"))
        self.package_jobs.setAccessibleName(tr("Parallele Jobs für make bindeb-pkg"))
        grid.addWidget(self.build_jobs, 1, 0)
        grid.addWidget(self.package_jobs, 1, 1)
        parallel_layout.addLayout(grid)
        content_layout.addWidget(parallel)
        output_card, output_layout = card()
        output_layout.addWidget(label(tr("3   Pakete speichern"), "heading"))
        output_layout.addWidget(label(tr("Alle .deb-Dateien landen zusammen mit Protokoll und Konfiguration "
                                     "in einem neuen Unterordner deiner Wahl."), "muted", True))
        output_row = QHBoxLayout()
        self.output_edit = QLineEdit(str(default_output()))
        self.output_edit.setAccessibleName(tr("Ausgabeverzeichnis für Debian-Pakete"))
        output_row.addWidget(self.output_edit, 1)
        self.output_browse = button(tr("Ordner wählen …"), self.choose_output)
        output_row.addWidget(self.output_browse)
        output_layout.addLayout(output_row)
        self.cleanup_box = QCheckBox(tr("Download und Arbeitsordner nach erfolgreichem Export löschen"))
        self.cleanup_box.setChecked(True)
        output_layout.addWidget(self.cleanup_box)
        content_layout.addWidget(output_card)
        adjustments, adjustments_layout = card()
        adjustments_layout.addWidget(label(tr("Build-Optionen"), "heading"))
        self.debug_box = QCheckBox(tr("Debugsymbole und BTF weglassen · spart Zeit und Speicherplatz"))
        self.debug_box.setChecked(True)
        self.debug_box.setToolTip(tr("Entfernt Debug-Informationen und BTF. Für eBPF/Tracing mit BTF-Anforderung deaktivieren."))
        adjustments_layout.addWidget(self.debug_box)
        self.cert_box = QCheckBox(tr("Fehlende Zertifikatpfade automatisch bereinigen · Ubuntu/Canonical"))
        self.cert_box.setChecked(True)
        self.cert_box.setToolTip(
            tr("Leert fehlende Zertifikatpfade wie debian/canonical-certs.pem und "
            "debian/canonical-revoked-certs.pem in der kopierten Build-Konfiguration. "
            "Vorhandene Zertifikatdateien bleiben erhalten. Der Modulschlüssel "
            "certs/signing_key.pem wird beim Build automatisch erzeugt. "
            "Die ursprüngliche /boot/config-Datei bleibt unverändert."))
        adjustments_layout.addWidget(self.cert_box)
        adjustments_layout.addWidget(label(tr("Der eigene Kernel erhält den Suffix -pmk. Alle Änderungen werden "
                                           "in kernel.config und config-changes.diff gespeichert."), "small", True))
        content_layout.addWidget(adjustments)
        expert, expert_layout = card("notice")
        expert_layout.addWidget(label(tr("Für Linux-Profis und Enthusiasten"), "heading"))
        self.menuconfig_box = QCheckBox(tr("Vor dem Kompilieren make menuconfig öffnen"))
        self.menuconfig_box.setToolTip(tr("Optionale manuelle Kernel-Konfiguration. Nur verwenden, "
                                       "wenn du die Folgen der gewählten Optionen kennst."))
        expert_layout.addWidget(self.menuconfig_box)
        expert_layout.addWidget(label(tr("Du solltest wissen, was du änderst: Fehlerhafte Optionen können "
                                      "Treiber entfernen oder dazu führen, dass der Kernel nicht startet."), "error", True))
        expert_layout.addWidget(label(tr("menuconfig öffnet sich in einem eigenen Terminal. Speichere in .config "
                                      "und beende das Menü. Danach prüfst du die Änderungen und wählst "
                                      "„Build fortsetzen“ oder „Build abbrechen“."), "muted", True))
        expert_layout.addWidget(label(tr("Die Build-Optionen oben werden vorher angewendet. Deine danach "
                                      "gespeicherten Menüeinstellungen werden übernommen. Für das Terminal "
                                      "wird xterm benötigt; die Funktion ist bei jedem App-Start ausgeschaltet."), "small", True))
        content_layout.addWidget(expert)
        content_layout.addStretch()
        layout.addWidget(scrollable(content), 1)
        self.build_notice = label("", "error", True)
        layout.addWidget(self.build_notice)
        actions = QHBoxLayout()
        actions.addWidget(button(tr("←  Kernel auswählen"), lambda: self.navigate(0)))
        actions.addStretch()
        self.build_button = button(tr("Kernel bauen  →"), self.start_build, "primary")
        self.build_button.setEnabled(False)
        actions.addWidget(self.build_button)
        layout.addLayout(actions)
        return page

    def _progress_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)
        status, status_layout = card()
        row = QHBoxLayout()
        self.stage_title = label(tr("Bereit für deinen Build"), "heading")
        row.addWidget(self.stage_title, 1)
        self.elapsed = label("00:00", "muted")
        row.addWidget(self.elapsed)
        status_layout.addLayout(row)
        self.stage_detail = label(tr("Wähle einen Kernel und lege die Build-Einstellungen fest."), "muted", True)
        status_layout.addWidget(self.stage_detail)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        status_layout.addWidget(self.progress_bar)
        self.download_panel = QWidget()
        download_layout = QVBoxLayout(self.download_panel)
        download_layout.setContentsMargins(0, 8, 0, 0)
        download_layout.setSpacing(8)
        self.download_label = label(tr("Kernel-Archiv wird heruntergeladen"), "muted")
        download_layout.addWidget(self.download_label)
        self.download_bar = QProgressBar()
        self.download_bar.setObjectName("downloadProgress")
        self.download_bar.setRange(0, 100)
        self.download_bar.setValue(0)
        self.download_bar.setFormat("%p%")
        self.download_bar.setTextVisible(True)
        self.download_bar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.download_bar.setAccessibleName(tr("Download-Fortschritt des Kernel-Archivs"))
        download_layout.addWidget(self.download_bar)
        self.download_detail = label("", "small", True)
        download_layout.addWidget(self.download_detail)
        status_layout.addWidget(self.download_panel)
        self.download_panel.hide()
        self.stage_step = label(tr("System → Download → Prüfung → Konfiguration → Build → Pakete → Export"), "small", True)
        status_layout.addWidget(self.stage_step)
        layout.addWidget(status)
        self.result_card, result_layout = card("hero")
        self.result_title = label(tr("Deine Pakete sind fertig"), "heading")
        result_layout.addWidget(self.result_title)
        self.result_detail = label("", "muted", True)
        self.result_detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        result_layout.addWidget(self.result_detail)
        result_layout.addWidget(label(tr("Die .deb-Dateien bleiben auch nach der Installation in diesem Ordner erhalten."), "small", True))
        result_buttons = QHBoxLayout()
        result_buttons.addWidget(button(tr("Paketordner öffnen"), self.open_result))
        self.install_button = button(tr("Kernel installieren …"), self.start_install, "primary")
        result_buttons.addWidget(self.install_button)
        result_buttons.addStretch()
        result_layout.addLayout(result_buttons)
        self.result_card.hide()
        layout.addWidget(self.result_card)
        log_header = QHBoxLayout()
        log_header.addWidget(label(tr("Live-Protokoll"), "heading"))
        log_header.addStretch()
        self.autoscroll = QCheckBox(tr("Ausgabe folgen"))
        self.autoscroll.setChecked(True)
        log_header.addWidget(self.autoscroll)
        self.save_log_button = button(tr("Protokoll speichern …"), self.save_log)
        log_header.addWidget(self.save_log_button)
        layout.addLayout(log_header)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(8000)
        self.log_view.setPlaceholderText(tr("Hier erscheinen die tatsächlichen Befehle und ihre Ausgabe."))
        self.log_view.setAccessibleName(tr("Live-Protokoll des Kernel-Builds"))
        layout.addWidget(self.log_view, 1)
        controls = QHBoxLayout()
        self.workspace_button = button(tr("Arbeitsordner öffnen"), self.open_workspace)
        self.workspace_button.hide()
        controls.addWidget(self.workspace_button)
        self.cleanup_button = button(tr("Arbeitsordner löschen …"), self.clean_failed_workspace)
        self.cleanup_button.hide()
        controls.addWidget(self.cleanup_button)
        controls.addStretch()
        self.cancel_button = button(tr("Build abbrechen"), self.cancel_build, "danger")
        self.cancel_button.setEnabled(False)
        controls.addWidget(self.cancel_button)
        layout.addLayout(controls)
        return page

    def _installed_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)
        layout.addWidget(label(tr("Verwalte die installierten Kernel und entferne nicht mehr benötigte Versionen."), "muted", True))
        notice, notice_layout = card("notice")
        notice_layout.addWidget(label(tr('Laufender Kernel: {kernel} · geschützt', kernel=self.system.kernel), "heading"))
        notice_layout.addWidget(label(tr("Der laufende Kernel kann nicht ausgewählt werden. Mindestens ein "
                                     "installierter Kernel bleibt erhalten. Exportierte .deb-Dateien und "
                                     "Build-Protokolle werden bei der Deinstallation nicht gelöscht."), "muted", True))
        layout.addWidget(notice)
        header = QHBoxLayout()
        self.kernel_count = label(tr("Installierte Kernel"), "heading")
        header.addWidget(self.kernel_count, 1)
        self.kernel_refresh_button = button(tr("↻  Aktualisieren"), self.refresh_installed_kernels)
        header.addWidget(self.kernel_refresh_button)
        layout.addLayout(header)
        self.kernel_list = QListWidget()
        self.kernel_list.setSpacing(2)
        self.kernel_list.setAccessibleName(tr("Installierte Kernel zur Deinstallation auswählen"))
        self.kernel_list.itemChanged.connect(self.update_kernel_selection)
        layout.addWidget(self.kernel_list, 1)
        self.kernel_error = label("", "error", True)
        self.kernel_error.hide()
        layout.addWidget(self.kernel_error)
        layout.addWidget(label(tr("Zugehörige Image-, Modul- und Header-Pakete werden zusammen angezeigt. "
                               "Gemeinsam verwendete Header bleiben für andere Kernel erhalten. "
                               "Vor dem Entfernen kannst du die genaue Paketliste prüfen."), "small", True))
        actions = QHBoxLayout()
        self.kernel_selection = label(tr("Noch keine Kernel zur Deinstallation ausgewählt"), "muted", True)
        actions.addWidget(self.kernel_selection, 1)
        self.remove_kernels_button = button(tr("Ausgewählte Kernel deinstallieren …"), self.prepare_kernel_removal, "danger")
        self.remove_kernels_button.setEnabled(False)
        actions.addWidget(self.remove_kernels_button)
        layout.addLayout(actions)
        return page

    def _system_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)
        layout.addWidget(label(tr("Alles, was dein System für den Build braucht, auf einen Blick."), "muted", True))
        language_card, language_layout = card()
        language_layout.addWidget(label(tr("Sprache / Language"), "heading"))
        self.language_combo = QComboBox()
        self.language_combo.setAccessibleName(tr("Sprache der Anwendung auswählen"))
        self.language_combo.addItem(tr("Automatisch (Systemsprache)"), "auto")
        self.language_combo.addItem("Deutsch", "de")
        self.language_combo.addItem("English", "en")
        self.language_combo.addItem("Español", "es")
        language_layout.addWidget(self.language_combo)
        self.language_notice = label(tr("Die Auswahl wird gespeichert und beim nächsten App-Start angewendet. "
                                        "Mit „Automatisch“ folgt die Anwendung der Systemsprache."), "muted", True)
        language_layout.addWidget(self.language_notice)
        self.language_combo.currentIndexChanged.connect(self.save_language_preference)
        layout.addWidget(language_card)
        system_card, system_layout = card()
        system_layout.addWidget(label(tr("Dein System"), "heading"))
        grid = QGridLayout()
        grid.setVerticalSpacing(13)
        values = ((tr("Distribution"), self.system.distro), (tr("Aktiver Kernel"), self.system.kernel),
                  (tr("Prozessor"), tr('{cpus} verfügbare Threads · {architecture}', cpus=self.system.cpus, architecture=self.system.architecture)),
                  (tr("Arbeitsspeicher"), format_number(self.system.ram_gib) + " GiB"),
                  (tr("Secure Boot"), {"on": tr("Aktiv · eigene Signierung erforderlich"), "off": tr("Deaktiviert"),
                                    "unknown": tr("Nicht erkannt")}[self.system.secure_boot]),
                  (tr("Arbeitsbereich"), str(cache_home() / "jobs")))
        for row, (key, value) in enumerate(values):
            grid.addWidget(label(key, "muted"), row, 0)
            item = label(value, wrap=True)
            item.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            grid.addWidget(item, row, 1)
        grid.setColumnStretch(1, 1)
        system_layout.addLayout(grid)
        layout.addWidget(system_card)
        dep_card, dep_layout = card()
        dep_layout.addWidget(label(tr("Build-Werkzeuge"), "heading"))
        self.dependencies_label = label(tr("Wird geprüft …"), "muted", True)
        dep_layout.addWidget(self.dependencies_label)
        self.dependencies_detail = label("", "small", True)
        dep_layout.addWidget(self.dependencies_detail)
        actions = QHBoxLayout()
        self.check_button = button(tr("Erneut prüfen"), self.refresh_system)
        actions.addWidget(self.check_button)
        self.dependencies_button = button(tr("Werkzeuge installieren …"), self.install_dependencies, "primary")
        actions.addWidget(self.dependencies_button)
        actions.addStretch()
        dep_layout.addLayout(actions)
        layout.addWidget(dep_card)
        note, note_layout = card("notice")
        note_layout.addWidget(label(tr("Vor dem ersten Build"), "heading"))
        note_layout.addWidget(label(tr("Je nach Konfiguration braucht ein Kernel-Build viel Zeit und mehrere "
                                   "zehn GiB freien Speicher. Plane mindestens 30 GiB ein; mit Debugsymbolen "
                                   "eher 60 GiB oder mehr. Dein Rechner sollte währenddessen am Strom hängen. "
                                   "Ein älterer Kernel bleibt für den Start über das Bootmenü verfügbar."), "muted", True))
        layout.addWidget(note)
        layout.addStretch()
        return scrollable(page)

    def navigate(self, index: int):
        self.stack.setCurrentIndex(index)
        self.nav_group.button(index).setChecked(True)
        self.page_title.setText((tr("Dein nächster Kernel"), tr("Alles für deinen Build"), tr("Vom Quellcode zum Kernel"),
                                 tr("Deine installierten Kernel"), tr("Dein System ist die Basis"))[index])
        if index == PAGE_INSTALLED and self.kernels_stale:
            self.refresh_installed_kernels()

    def _load_settings(self):
        self.language_combo.blockSignals(True)
        self.language_combo.setCurrentIndex(self.language_combo.findData(language_preference(self.settings)))
        self.language_combo.blockSignals(False)
        self.output_edit.setText(self.settings.value("output", str(default_output()), type=str))
        for key, combo in (("build_jobs", self.build_jobs), ("package_jobs", self.package_jobs)):
            value = self.settings.value(key, combo.currentData(), type=int)
            index = combo.findData(value)
            if index >= 0:
                combo.setCurrentIndex(index)
        for key, box in (("cleanup", self.cleanup_box), ("skip_debug", self.debug_box), ("repair_certificates", self.cert_box)):
            box.setChecked(self.settings.value(key, True, type=bool))

    def save_language_preference(self):
        # Keep the current session and its workers in their startup language.
        self.settings.setValue("language", self.language_combo.currentData())
        self.settings.sync()
        if self.settings.status() != self.settings.Status.NoError:
            self.language_notice.setText(tr("Die Sprachauswahl konnte nicht gespeichert werden. "
                                            "Bitte die Schreibrechte für die App-Einstellungen prüfen."))
        else:
            self.language_notice.setText(tr("Sprachauswahl gespeichert. Die neue Sprache gilt beim nächsten App-Start."))

    def _save_settings(self):
        self.settings.setValue("output", self.output_edit.text().strip())
        self.settings.setValue("build_jobs", self.build_jobs.currentData())
        self.settings.setValue("package_jobs", self.package_jobs.currentData())
        self.settings.setValue("cleanup", self.cleanup_box.isChecked())
        self.settings.setValue("skip_debug", self.debug_box.isChecked())
        self.settings.setValue("repair_certificates", self.cert_box.isChecked())

    def _launch_worker(self, kind: str, task: Callable, success: Callable):
        if self.worker is not None:
            return
        worker = Worker(task, self)
        self.worker = worker
        self.worker_kind = kind
        worker.logged.connect(self.queue_log)
        worker.staged.connect(self.on_stage)
        worker.progressed.connect(self.on_download)
        worker.config_review.connect(self.on_config_review)
        worker.succeeded.connect(success)
        worker.failed.connect(self.on_failure)
        worker.finished.connect(self.worker_finished)
        worker.start()

    def refresh_releases(self):
        if self.worker:
            return
        self.refresh_button.setEnabled(False)
        self.archive_button.setEnabled(False)
        self.connection.setText(tr("Verbindung …"))
        self.release_error.hide()
        self.footer_status.setText(tr("Aktuelle Releases werden geladen …"))
        self._launch_worker("releases", lambda w: Network(w.cancel).releases(), self.on_releases)

    def on_releases(self, releases: list[Release]):
        self.current_releases = releases
        self.merge_releases()
        stable = next((r for r in releases if r.channel == "stable" and r.buildable), None)
        if stable:
            self.hero_version.setText("Linux " + stable.version)
            date = self.format_date(stable.date)
            self.hero_detail.setText(tr('Veröffentlicht am {date} · OpenPGP-signiertes Quellarchiv', date=date))
            current = version_key(upstream_version(self.system.kernel))
            latest = version_key(stable.version)
            if current == latest:
                self.hero_status.setText(tr('✓  Dein System läuft bereits auf dem neuesten stabilen Kernel (Linux {version}). Ein Update ist aktuell nicht erforderlich.', version=stable.version))
            elif current > latest:
                self.hero_status.setText(tr('Dein aktiver Kernel {kernel} ist neuer als das aktuelle stabile Release. Ein Update ist aktuell nicht erforderlich.', kernel=self.system.kernel))
            else:
                self.hero_status.setText(tr("Ein neueres stabiles Release als dein aktiver Kernel ist verfügbar."))
            self.hero_status.show()
        else:
            self.hero_version.setText(tr("LTS verfügbar"))
            self.hero_detail.setText(tr("Wähle ein gepflegtes Release aus der Liste."))
            self.hero_status.hide()
        self.connection.setText(tr("●  kernel.org verbunden"))
        self.footer_status.setText(tr("Release-Liste aktualisiert · {time}", time=time.strftime("%H:%M")))

    def load_archive_releases(self):
        if self.worker or self.running:
            return
        self.release_error.hide()
        self.archive_button.setText(tr("Archiv wird geladen …"))
        self.footer_status.setText(tr("Ältere Versionen aller Kernelreihen werden von kernel.org geladen …"))
        self._launch_worker("archive", lambda w: Network(w.cancel).archive_releases(), self.on_archive_releases)
        self.set_busy(True)

    def on_archive_releases(self, releases: list[Release]):
        self.archived_releases = releases
        self.archive_loaded = True
        self.merge_releases()
        self.release_error.hide()
        self.connection.setText(tr("●  kernel.org verbunden"))
        self.footer_status.setText(tr('Kernel-Archiv geladen · {value} Versionen · Kernelreihe wählen oder nach einer Version suchen', value=len(releases)))

    def merge_releases(self):
        by_version = {release.version: release for release in self.archived_releases}
        for release in self.current_releases:
            if release.eol and release.version in by_version:
                by_version[release.version] = replace(by_version[release.version], eol=True)
            else:
                by_version[release.version] = release
        self.releases = sorted(by_version.values(),
                               key=lambda r: (r.channel == "stable" and r.buildable, version_key(r.version)),
                               reverse=True)
        selected_series = self.series_filter.currentData()
        self.series_filter.blockSignals(True)
        self.series_filter.clear()
        self.series_filter.addItem(tr("Alle Kernelreihen"), None)
        for major in sorted({int(release.version.split(".")[0]) for release in self.releases}, reverse=True):
            self.series_filter.addItem(f"Linux {major}.x", major)
        index = self.series_filter.findData(selected_series)
        self.series_filter.setCurrentIndex(max(0, index))
        self.series_filter.blockSignals(False)
        self.populate_releases()

    @staticmethod
    def format_date(date: str) -> str:
        parsed = QDate.fromString(date, Qt.DateFormat.ISODate)
        if not parsed.isValid():
            return date or tr("unbekannt")
        locale = QLocale()
        pattern = re.sub(r"(?<!y)yy(?!y)", "yyyy", locale.dateFormat(locale.FormatType.ShortFormat))
        return locale.toString(parsed, pattern)

    def populate_releases(self):
        selected = self.selected_release()
        major = self.series_filter.currentData()
        search = self.version_search.text().strip().lower().removeprefix("linux").strip().removeprefix("v")
        self.release_list.blockSignals(True)
        self.release_list.clear()
        for release in self.releases:
            if major is not None and int(release.version.split(".")[0]) != major:
                continue
            if search and not release.version.startswith(search):
                continue
            if release.is_rc and not self.show_rc.isChecked():
                continue
            annotation = tr(" · nicht baubar") if not release.buildable else ""
            item = QListWidgetItem(tr('Linux {version}     {label}{annotation}\nVeröffentlicht {value}', version=release.version, label=release.label, annotation=annotation, value=self.format_date(release.date)) + (tr(" · Pflege beendet") if release.eol else ""))
            item.setData(Qt.ItemDataRole.UserRole, release)
            if not release.buildable:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled & ~Qt.ItemFlag.ItemIsSelectable)
            self.release_list.addItem(item)
            if selected and selected.version == release.version and release.buildable:
                self.release_list.setCurrentItem(item)
        if self.release_list.currentRow() < 0:
            for row in range(self.release_list.count()):
                if self.release_list.item(row).data(Qt.ItemDataRole.UserRole).buildable:
                    self.release_list.setCurrentRow(row)
                    break
        self.release_list.blockSignals(False)
        if not self.release_list.count() and (major is not None or search):
            self.release_hint.setText(tr("Keine passende Version. ") +
                                      (tr("Passe die Kernelreihe oder den Suchtext an.") if self.archive_loaded
                                       else tr("Lade ältere Versionen oder passe den Filter an.")))
        elif self.archive_loaded:
            self.release_hint.setText(tr("Archiv-Versionen lassen sich erneut bauen und installieren. "
                                      "Vorabversionen werden nur angezeigt und sind nicht baubar."))
        else:
            self.release_hint.setText(tr("Stable erhält aktuelle Fehlerkorrekturen. LTS bietet langfristige Pflege. "
                                      "Über „Ältere Versionen laden“ findest du frühere Releases aller Reihen."))
        self.select_release()

    def selected_release(self) -> Release | None:
        item = self.release_list.currentItem()
        release = item.data(Qt.ItemDataRole.UserRole) if item else None
        return release if release and release.buildable else None

    def select_release(self):
        release = self.selected_release()
        self.next_button.setEnabled(bool(release) and not self.running)
        self.build_button.setEnabled(bool(release) and not self.running and self.system.debian)
        self.selected_label.setText(tr('Ausgewählt: Linux {version}', version=release.version) if release else tr("Noch kein Kernel ausgewählt"))
        self.options_summary.setText(tr('Linux {version} · {label} · Originalquelle kernel.org', version=release.version, label=release.label) if release else tr("Wähle zuerst einen Kernel im ersten Schritt."))

    def refresh_configs(self):
        self.config_combo.clear()
        try:
            for path in boot_configs():
                suffix = tr(" · aktiv") if path.name == "config-" + self.system.kernel else ""
                self.config_combo.addItem(str(path) + suffix, str(path))
        except KernelError as exc:
            self.build_notice.setText(str(exc))
        if not self.config_combo.count():
            self.build_notice.setText(tr("Keine Konfiguration in /boot gefunden. Bitte eine passende .config-Datei wählen."))

    def choose_config(self):
        path, _ = QFileDialog.getOpenFileName(self, tr("Kernel-Konfiguration wählen"), "/boot", tr("Alle Dateien (*)"))
        if path:
            index = self.config_combo.findData(path)
            if index < 0:
                self.config_combo.addItem(path, path)
                index = self.config_combo.count() - 1
            self.config_combo.setCurrentIndex(index)

    def choose_output(self):
        path = QFileDialog.getExistingDirectory(self, tr("Paketverzeichnis wählen"), self.output_edit.text())
        if path:
            self.output_edit.setText(path)

    def refresh_installed_kernels(self):
        if self.worker or self.running:
            self.pending_kernel_refresh = True
            self.kernel_selection.setText(tr("Die Kernel-Liste wird nach dem laufenden Vorgang aktualisiert."))
            return
        self.pending_kernel_refresh = False
        self.kernel_error.hide()
        self._launch_worker("kernels", lambda w: installed_kernels(), self.on_installed_kernels)
        self.set_busy(True)
        self.kernel_selection.setText(tr("Installierte Kernel werden gelesen …"))

    def on_installed_kernels(self, kernels: list[InstalledKernel]):
        checked = set(self.selected_kernel_releases())
        self.kernels = kernels
        self.kernels_stale = False
        self.kernel_list.blockSignals(True)
        self.kernel_list.clear()
        for kernel in kernels:
            protected = kernel.active or len(kernels) == 1
            status = tr(" · aktiv, geschützt") if kernel.active else tr(" · letzter installierter Kernel, geschützt") if protected else ""
            size = kernel.installed_bytes / 1024**2
            size_text = format_number(size)
            packages = plural("{count} Paket", "{count} Pakete", len(kernel.packages))
            item = QListWidgetItem(tr("Linux {release}{status}\n{packages} · {size} MiB installiert",
                                     release=kernel.release, status=status, packages=packages, size=size_text) +
                                   (tr("\nDieser Kernel läuft gerade und kann nicht entfernt werden.") if kernel.active else ""))
            item.setData(Qt.ItemDataRole.UserRole, kernel)
            protection = (tr('Linux {release} ist der aktuell laufende Kernel.\nEr kann nicht entfernt werden, solange er läuft.\n\n', release=kernel.release) if kernel.active else
                          tr("Der letzte installierte Kernel kann nicht entfernt werden.\n\n") if protected else "")
            item.setToolTip(protection + tr("Zugehörige Pakete:\n") + "\n".join(kernel.packages))
            if protected:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            else:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked if kernel.release in checked else Qt.CheckState.Unchecked)
            self.kernel_list.addItem(item)
        self.kernel_list.blockSignals(False)
        self.kernel_count.setText(plural("{count} installierter Kernel", "{count} installierte Kernel", len(kernels)))
        self.kernel_error.hide()
        self.update_kernel_selection()
        self.footer_status.setText(tr("Liste der installierten Kernel aktualisiert"))

    def selected_kernel_releases(self) -> tuple[str, ...]:
        selected = []
        for row in range(self.kernel_list.count()):
            item = self.kernel_list.item(row)
            kernel = item.data(Qt.ItemDataRole.UserRole)
            if (item.checkState() == Qt.CheckState.Checked and not kernel.active
                    and len(self.kernels) > 1):
                selected.append(kernel.release)
        return tuple(selected)

    def update_kernel_selection(self):
        selected = self.selected_kernel_releases()
        self.remove_kernels_button.setEnabled(bool(selected) and not self.running
                                               and not self.kernels_stale and self.system.debian)
        if selected:
            self.kernel_selection.setText(tr("Zur Deinstallation ausgewählt: {count}", count=len(selected)))
        elif not self.kernels and not self.kernels_stale:
            self.kernel_selection.setText(tr("Keine installierten Kernel-Images gefunden."))
        else:
            self.kernel_selection.setText(tr("Noch keine Kernel zur Deinstallation ausgewählt"))

    def prepare_kernel_removal(self):
        if self.worker or self.running or self.kernels_stale:
            return
        selected = self.selected_kernel_releases()
        if not selected:
            return
        self.kernel_error.hide()
        self.pending_removal_plan = None

        def ready(plan: RemovalPlan):
            self.pending_removal_plan = plan

        self._launch_worker("remove-preview", lambda w: plan_kernel_removal(selected), ready)
        self.set_busy(True)
        self.kernel_selection.setText(tr("Deinstallation wird vorbereitet · Paketabhängigkeiten werden geprüft …"))

    def confirm_kernel_removal(self, plan: RemovalPlan):
        dialog = RemoveKernelsDialog(plan, self.system.kernel, self)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        dialog.deleteLater()
        if not accepted or self.close_pending:
            self.footer_status.setText(tr("Deinstallation nicht gestartet · Kernel bleiben installiert"))
            return
        self.navigate(2)
        self.started_at = time.monotonic()
        self.stage_title.setText(tr("Ausgewählte Kernel werden deinstalliert"))
        self.stage_detail.setText(tr("Bitte die Administratorabfrage bestätigen. APT entfernt die ausgewählten Pakete."))
        self.stage_step.setText(tr("Kernelpflege · ") + ", ".join(plan.releases))
        self.progress_bar.setRange(0, 0)

        def done(_):
            self.kernels_stale = True
            self.stage_title.setText(tr("Kernel deinstalliert"))
            self.stage_detail.setText(tr("Die ausgewählten Kernel wurden entfernt. Exportierte .deb-Dateien bleiben erhalten."))
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(100)
            self.navigate(PAGE_INSTALLED)
            self.footer_status.setText(tr("Deinstallation abgeschlossen · Kernel-Liste wird aktualisiert"))

        self._launch_worker("remove", lambda w: CommandRunner(w.cancel, w.logged.emit).run(
            privileged_command("remove", *plan.packages)), done)
        self.set_busy(True)

    def refresh_system(self):
        missing = missing_dependencies()
        self.dependencies_label.setObjectName("error" if missing else "success")
        self.dependencies_label.setText(tr("Es fehlen noch Build-Werkzeuge.") if missing else tr("✓  Alle benötigten Build-Werkzeuge sind vorhanden."))
        self.dependencies_detail.setText(tr("Fehlend: ") + ", ".join(missing) if missing else tr("Compiler, Konfigurationsbibliotheken, Paketbau und OpenPGP sind einsatzbereit."))
        self.dependencies_button.setEnabled(bool(missing) and self.system.debian and not self.running)
        if not self.system.debian:
            self.build_notice.setText(tr("Dieses Tool erstellt .deb-Pakete und unterstützt Debian/Ubuntu-Systeme."))
        elif self.system.secure_boot == "on":
            self.build_notice.setText(tr("Secure Boot ist aktiv: Pakete können gebaut werden, benötigen vor der Installation aber eine eigene Signierung."))
        self.dependencies_label.style().unpolish(self.dependencies_label)
        self.dependencies_label.style().polish(self.dependencies_label)

    def set_busy(self, busy: bool):
        self.running = busy
        for widget in (self.config_combo, self.config_browse, self.output_edit, self.output_browse,
                       self.build_jobs, self.package_jobs, self.cleanup_box, self.debug_box,
                       self.cert_box, self.menuconfig_box, self.release_list, self.show_rc, self.refresh_button, self.check_button,
                       self.series_filter, self.version_search, self.archive_button,
                       self.kernel_list, self.kernel_refresh_button):
            widget.setEnabled(not busy)
        self.cancel_button.setEnabled(busy and self.worker_kind == "build")
        self.install_button.setEnabled(not busy)
        self.cleanup_button.setEnabled(not busy)
        self.update_kernel_selection()
        self.select_release()
        self.refresh_system()

    def start_build(self):
        if self.worker or self.running:
            return
        release = self.selected_release()
        if not release:
            self.navigate(0)
            return
        config = self.config_combo.currentData()
        if not config:
            self.build_notice.setText(tr("Bitte zuerst eine Kernel-Konfiguration wählen."))
            return
        options = BuildOptions(release, Path(config), Path(self.output_edit.text().strip()).expanduser(),
                               self.build_jobs.currentData(), self.package_jobs.currentData(),
                               self.debug_box.isChecked(), self.cert_box.isChecked(), self.cleanup_box.isChecked(),
                               menuconfig=self.menuconfig_box.isChecked())
        try:
            options.validate()
            if options.menuconfig:
                menuconfig_terminal()
        except (KernelError, OSError) as exc:
            self.build_notice.setText(str(exc))
            return
        if missing_dependencies():
            self.navigate(PAGE_SYSTEM)
            self.footer_status.setText(tr("Bitte zuerst die fehlenden Build-Werkzeuge installieren."))
            return
        self._save_settings()
        self.offer_cleanup_on_cancel = False
        self.pending_cancel_cleanup = None
        self.result = None
        self.result_card.hide()
        self.cleanup_button.hide()
        self.workspace_button.hide()
        self.log_view.clear()
        self.log_buffer.clear()
        self.started_at = time.monotonic()
        self.elapsed.setText("00:00")
        self.reset_download()
        self.download_panel.hide()
        self.navigate(2)
        self.footer_status.setText(tr('Linux {version} wird gebaut …', version=release.version))

        def task(worker: Worker):
            self.engine = BuildEngine(options, worker.cancel, worker.logged.emit,
                                      worker.staged.emit, self.throttled_progress(worker),
                                      review_config=lambda changes: self.request_config_review(worker, changes))
            return self.engine.run()

        self._launch_worker("build", task, self.on_built)
        self.set_busy(True)

    @staticmethod
    def request_config_review(worker: Worker, changes: str) -> bool:
        request = ConfigReviewRequest(worker, changes)
        worker.config_review.emit(request)
        while not request.completed.wait(0.1):
            if worker.cancel.is_set():
                raise Cancelled(tr("Konfigurationsprüfung abgebrochen."))
        if worker.cancel.is_set():
            raise Cancelled(tr("Konfigurationsprüfung abgebrochen."))
        return request.accepted

    def on_config_review(self, request: ConfigReviewRequest):
        dialog = None
        try:
            if request.worker is not self.worker or self.close_pending or request.worker.cancel.is_set():
                return
            dialog = ConfigReviewDialog(request.changes, self)
            timer = QTimer(dialog)
            timer.timeout.connect(lambda: dialog.reject() if request.worker.cancel.is_set() else None)
            timer.start(100)
            request.accepted = dialog.exec() == QDialog.DialogCode.Accepted
            timer.stop()
            if not request.accepted and not self.close_pending:
                self.cancel_build()
        finally:
            request.completed.set()
            if dialog:
                dialog.deleteLater()

    def throttled_progress(self, worker: Worker):
        last = [0.0]

        def progress(current: int, total: int):
            now = time.monotonic()
            if now - last[0] >= 0.1 or current == total:
                # Python integer signals preserve bytes without Qt's 32-bit limit.
                worker.progressed.emit(current, total)
                last[0] = now

        return progress

    def on_stage(self, index: int, title: str, detail: str):
        if index == 1:
            self.reset_download()
            self.download_panel.show()
        elif index == 2:
            # The engine reaches signature checking only after a committed download.
            self.download_bar.setRange(0, 100)
            self.download_bar.setValue(100)
            self.download_bar.setFormat("%p%")
            self.download_label.setText(tr("✓  Kernel-Archiv vollständig heruntergeladen"))
        self.stage_title.setText(title)
        self.stage_detail.setText(detail)
        self.stage_step.setText(tr('Schritt {value} von 9 · ', value=index + 1) + title)
        self.progress_bar.setRange(0, 0)
        self.queue_log(f"\n── {title} ──")

    def reset_download(self):
        self.download_current = self.download_total = 0
        self.download_bar.setRange(0, 100)
        self.download_bar.setValue(0)
        self.download_bar.setFormat("%p%")
        self.download_label.setText(tr("Kernel-Archiv wird heruntergeladen"))
        self.download_detail.setText(tr("{size} MiB · Quelle: kernel.org", size=format_number(0)))

    def on_download(self, current: int, total: int):
        self.download_current, self.download_total = current, total
        current_mib = format_number(current / 1024**2)
        if total > 0:
            percent = min(99, max(0, current * 100 // total))
            self.download_bar.setRange(0, 100)
            self.download_bar.setValue(percent)
            self.download_bar.setFormat("%p%")
            total_mib = format_number(total / 1024**2)
            detail = tr('{current_mib} von {total_mib} MiB · Quelle: kernel.org', current_mib=current_mib, total_mib=total_mib)
        else:
            self.download_bar.setRange(0, 0)
            self.download_bar.setFormat(tr("Größe unbekannt"))
            detail = tr('{current_mib} MiB heruntergeladen · Gesamtgröße unbekannt · kernel.org', current_mib=current_mib)
        self.download_detail.setText(detail)

    def queue_log(self, line: str):
        self.log_buffer.append(line)

    def flush_logs(self):
        if not self.log_buffer:
            return
        scrollbar = self.log_view.verticalScrollBar()
        previous = scrollbar.value()
        self.log_view.appendPlainText("\n".join(self.log_buffer))
        self.log_buffer.clear()
        scrollbar.setValue(scrollbar.maximum() if self.autoscroll.isChecked() else previous)

    def update_elapsed(self):
        if self.running and self.started_at is not None:
            seconds = int(time.monotonic() - self.started_at)
            hours, seconds = divmod(seconds, 3600)
            minutes, seconds = divmod(seconds, 60)
            self.elapsed.setText(f"{hours:02}:{minutes:02}:{seconds:02}" if hours else f"{minutes:02}:{seconds:02}")

    def on_built(self, result: BuildResult):
        self.result = result
        self.stage_title.setText(tr("Dein Kernel ist fertig"))
        self.stage_detail.setText(tr("Pakete erfolgreich erstellt, geprüft und gespeichert."))
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        self.stage_step.setText(tr("Alle 9 Schritte abgeschlossen · Signiert von ") + result.signer)
        self.result_title.setText(tr("Linux {version} · {packages}", version=result.version,
                                     packages=plural("{count} Paket", "{count} Pakete", len(result.packages))))
        size = sum(p.stat().st_size for p in result.packages) / 1024**2
        self.result_detail.setText(tr("{size} MiB gespeichert in\n{directory}", size=format_number(size),
                                      directory=result.directory) + ("\n" + result.warning if result.warning else ""))
        self.result_card.show()
        self.footer_status.setText(tr("Build abgeschlossen · Installation kann jetzt separat gestartet werden"))
        if result.workspace:
            self.workspace_button.show()
            self.cleanup_button.show()

    def on_failure(self, message: str, cancelled: bool):
        if self.worker_kind in {"kernels", "remove-preview"}:
            self.kernels_stale = True
            self.kernel_error.setText(message)
            self.kernel_error.show()
            self.footer_status.setText(tr("Kernel-Liste nicht verfügbar") if self.worker_kind == "kernels"
                                       else tr("Deinstallation nicht gestartet · Details in der Kernelverwaltung"))
            return
        if self.worker_kind == "remove":
            self.kernels_stale = True
            self.pending_kernel_refresh = True
        if self.worker_kind == "releases":
            self.connection.setText(tr("Verbindung fehlgeschlagen"))
            self.release_error.setText(message + tr("\nPrüfe deine Internetverbindung und klicke auf Aktualisieren."))
            self.release_error.show()
            if not self.releases:
                self.hero_version.setText(tr("Nicht erreichbar"))
                self.hero_detail.setText(tr("Die aktuelle Release-Liste konnte nicht geladen werden."))
            self.footer_status.setText(tr("Release-Abfrage fehlgeschlagen"))
            return
        if self.worker_kind == "archive":
            retry = tr("Archiv aktualisieren") if self.archive_loaded else tr("Ältere Versionen laden")
            self.release_error.setText(message + tr('\nKlicke auf „{retry}“, um es erneut zu versuchen.', retry=retry))
            self.release_error.show()
            self.footer_status.setText(tr("Archiv-Abfrage abgebrochen") if cancelled else tr("Archiv-Abfrage fehlgeschlagen"))
            return
        self.flush_logs()
        if self.worker_kind == "build" and self.download_bar.value() < 100:
            self.download_label.setText(tr("Download abgebrochen") if cancelled else tr("Download nicht abgeschlossen"))
        self.stage_title.setText(tr("Build abgebrochen") if cancelled else tr("Vorgang fehlgeschlagen"))
        self.stage_detail.setText(message[-1800:])
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.queue_log(message)
        self.footer_status.setText(tr("Abgebrochen · Arbeitsdateien bleiben erhalten") if cancelled else tr("Fehler · Details im Protokoll"))
        if self.engine and self.engine.workspace and self.engine.workspace.exists():
            self.workspace_button.show()
            self.cleanup_button.show()
            if self.worker_kind == "build" and cancelled and self.offer_cleanup_on_cancel:
                self.pending_cancel_cleanup = (self.engine.workspace, self.engine.work_root)

    def worker_finished(self):
        worker = self.worker
        kind = self.worker_kind
        pending_removal = self.pending_removal_plan if kind == "remove-preview" else None
        self.pending_removal_plan = None
        pending_cleanup = self.pending_cancel_cleanup if self.worker_kind == "build" else None
        self.pending_cancel_cleanup = None
        self.offer_cleanup_on_cancel = False
        self.worker = None
        self.worker_kind = ""
        self.flush_logs()
        self.set_busy(False)
        self.refresh_button.setEnabled(True)
        self.archive_button.setText(tr("Archiv aktualisieren") if self.archive_loaded else tr("Ältere Versionen laden"))
        if worker:
            worker.deleteLater()
        if pending_cleanup:
            self.offer_aborted_cleanup(*pending_cleanup)
        if pending_removal and not self.close_pending:
            self.confirm_kernel_removal(pending_removal)
        if self.close_pending and self.worker is None:
            QTimer.singleShot(0, self.close)
        elif self.pending_kernel_refresh and self.worker is None:
            QTimer.singleShot(0, self.refresh_installed_kernels)

    def cancel_build(self, *, offer_cleanup: bool = True):
        if self.worker and self.worker_kind == "build":
            self.offer_cleanup_on_cancel |= offer_cleanup
            self.worker.cancel.set()
            self.cancel_button.setEnabled(False)
            self.stage_detail.setText(tr("Abbruch angefordert · laufende Prozesse werden beendet …"))

    def offer_aborted_cleanup(self, path: Path, root: Path):
        if not path.exists():
            return
        dialog = CancelCleanupDialog(path, self)
        reply = dialog.exec()
        dialog.deleteLater()
        if reply == QDialog.DialogCode.Accepted:
            self.start_workspace_cleanup(path, root)
        else:
            self.footer_status.setText(tr("Abgebrochen · Arbeitsdateien zur Fehlersuche behalten"))

    def open_result(self):
        if self.result:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.result.directory)))

    def open_workspace(self):
        if self.engine and self.engine.workspace and self.engine.workspace.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.engine.workspace)))

    def clean_failed_workspace(self):
        if self.worker or self.running or not self.engine or not self.engine.workspace:
            return
        path = self.engine.workspace
        reply = QMessageBox.question(self, tr("Arbeitsordner löschen"), tr('Download und Build-Dateien aus diesem Arbeitsordner löschen?\n\n{path}\n\nExportierte Pakete und Protokolle bleiben erhalten.', path=path),
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                     QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.start_workspace_cleanup(path, self.engine.work_root)

    def start_workspace_cleanup(self, path: Path, root: Path):
        if self.worker or self.running:
            return
        self.navigate(2)
        self.started_at = time.monotonic()
        self.stage_step.setText(tr("Aufräumen"))
        self.stage_title.setText(tr("Arbeitsordner wird gelöscht"))
        self.stage_detail.setText(str(path))
        self.progress_bar.setRange(0, 0)

        def done(_):
            if self.engine and self.engine.workspace == path:
                self.engine.workspace = None
                self.engine.runner.temp_dir = None
            self.cleanup_button.hide()
            self.workspace_button.hide()
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(100)
            self.stage_title.setText(tr("Arbeitsordner gelöscht"))
            self.stage_step.setText(tr("Aufräumen abgeschlossen"))
            self.stage_detail.setText(tr("Exportierte Pakete und gespeicherte Protokolle bleiben verfügbar."))
            self.footer_status.setText(tr("Temporäre Build-Dateien gelöscht"))

        self._launch_worker("cleanup", lambda w: cleanup_workspace(path, root), done)
        self.set_busy(True)

    def save_log(self):
        self.flush_logs()
        path, _ = QFileDialog.getSaveFileName(self, tr("Protokoll speichern"), "pimpmykernel.log", tr("Protokoll (*.log)"))
        if not path:
            return
        try:
            if self.engine and self.engine.log_path and self.engine.log_path.exists():
                import shutil
                if Path(path).resolve() != self.engine.log_path.resolve():
                    shutil.copyfile(self.engine.log_path, path)
            else:
                Path(path).write_text(self.log_view.toPlainText())
        except OSError as exc:
            QMessageBox.warning(self, tr("Protokoll speichern"), str(exc))

    def start_install(self):
        if self.running or self.worker or not self.result:
            return
        self.system = SystemInfo.detect()
        if InstallDialog(self.result, self.system, self).exec() != QDialog.DialogCode.Accepted:
            return
        self.navigate(2)
        self.started_at = time.monotonic()
        self.stage_title.setText(tr("Kernel wird installiert"))
        self.stage_detail.setText(tr("Bitte die Administratorabfrage bestätigen. APT richtet die Kernel-Pakete ein."))
        self.progress_bar.setRange(0, 0)

        def task(worker):
            install_packages(self.result, CommandRunner(worker.cancel, worker.logged.emit))

        def done(_):
            self.kernels_stale = True
            self.stage_title.setText(tr("Installation abgeschlossen"))
            self.stage_detail.setText(tr("Du kannst den neuen Kernel nach einem Neustart verwenden."))
            self.result_title.setText(tr("Installiert · Linux ") + self.result.version)
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(100)
            self.footer_status.setText(tr("Kernel installiert · Neustart bei Gelegenheit"))

        self._launch_worker("install", task, done)
        self.set_busy(True)

    def install_dependencies(self):
        if self.worker or self.running:
            return
        reply = QMessageBox.question(self, tr("Build-Werkzeuge installieren"), tr("Die Paketlisten werden aktualisiert "
                                     "und folgende Werkzeuge mit APT eingerichtet:\n\n") + ", ".join(BUILD_PACKAGES)
                                     + tr("\n\nDafür erscheint die grafische Passwortabfrage."),
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                     QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.navigate(2)
        self.started_at = time.monotonic()
        self.stage_title.setText(tr("Build-Werkzeuge werden installiert"))
        self.stage_detail.setText(tr("Bitte die Administratorabfrage bestätigen. APT wird vorbereitet."))
        self.progress_bar.setRange(0, 0)

        def done(_):
            self.stage_title.setText(tr("Build-Werkzeuge installiert"))
            self.stage_detail.setText(tr("Du kannst jetzt den Kernel-Build vorbereiten."))
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(100)
            self.footer_status.setText(tr("Build-Werkzeuge eingerichtet"))

        self._launch_worker("dependencies", lambda w: CommandRunner(w.cancel, w.logged.emit).run(
            privileged_command("dependencies")), done)
        self.set_busy(True)

    def show_help(self):
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("PimpMyKernel · Hilfe"))
        dialog.setMinimumWidth(650)
        dialog.resize(760, 720)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(28, 26, 28, 26)
        layout.setSpacing(16)
        layout.addWidget(label(tr("In drei Schritten zu deinem Kernel"), "heading"))
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(16)
        content_layout.addWidget(label(tr("1. Wähle Stable oder LTS von kernel.org. Über „Ältere Versionen laden“ "
                               "kannst du auch frühere Releases auswählen; Kernelreihe und Suche grenzen die Liste ein.\n\n"
                               "2. Übernimm deine /boot-Konfiguration, wähle die Anzahl der Jobs und einen Paketordner.\n\n"
                               "3. Starte den Build. Nach dem Export kannst du die fertigen Pakete installieren."), wrap=True))
        content_layout.addWidget(label(tr("Der Ablauf verwendet make oldconfig mit Standardantworten, make -jN und "
                               "make bindeb-pkg -jN. Kompiliert wird mit deinem Benutzerkonto. "
                               "Administratorrechte sind nur für geschützte Konfigurationen, Build-Werkzeuge "
                               "sowie die bewusst gewählte Installation oder Deinstallation nötig. "
                               "Exportierte .deb-Dateien bleiben auch nach der Installation erhalten."), "muted", True))
        content_layout.addWidget(label(tr("Unter „Installierte Kernel“ kannst du ältere Versionen auswählen und "
                               "deinstallieren. Der laufende Kernel ist geschützt. Vor dem Entfernen "
                               "werden die Paketabhängigkeiten und die genaue Paketliste geprüft."), "muted", True))
        content_layout.addWidget(label(tr("Für erfahrene Nutzer bietet „Build vorbereiten“ optional make menuconfig. "
                               "Du bearbeitest die kopierte Konfiguration im Terminal und prüfst danach "
                               "die Änderungen. Falsche Kerneloptionen können den Kernel unbootbar machen."), "muted", True))
        content_layout.addWidget(label(tr("Bei Fehlern: Protokoll speichern und die letzten Fehlermeldungen prüfen. "
                               "Der Arbeitsordner bleibt erhalten, bis du ihn selbst löschst. "
                               "Nach einem Klick auf „Build abbrechen“ kannst du die Arbeitsdateien "
                               "direkt löschen oder zur Fehlersuche behalten. Die Prozesse werden vorher "
                               "beendet; das kann einige Sekunden dauern. "
                               "Paketinstallationen werden zum Schutz der Paketverwaltung zu Ende geführt."), "muted", True))
        content_layout.addStretch()
        layout.addWidget(scrollable(content), 1)
        links = QHBoxLayout()
        links.addWidget(button("kernel.org", lambda: QDesktopServices.openUrl(QUrl("https://www.kernel.org/"))))
        links.addWidget(button(tr("Signaturprüfung"), lambda: QDesktopServices.openUrl(QUrl("https://www.kernel.org/signature.html"))))
        links.addStretch()
        layout.addLayout(links)
        controls = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        controls.rejected.connect(dialog.reject)
        layout.addWidget(controls)
        dialog.exec()

    def closeEvent(self, event: QCloseEvent):
        if self.worker is not None:
            self.close_pending = True
            if self.worker_kind in {"releases", "archive"}:
                self.worker.cancel.set()
                # Keep the Qt objects alive until the bounded network request returns.
                self.footer_status.setText(tr("Abfrage wird beendet. Das Fenster schließt anschließend."))
            elif self.worker_kind == "build":
                self.cancel_build(offer_cleanup=False)
                self.footer_status.setText(tr("Build wird abgebrochen. Das Fenster schließt anschließend."))
            else:
                self.footer_status.setText(tr("Das Fenster schließt, sobald der laufende Vorgang abgeschlossen ist."))
            event.ignore()
            return
        self._save_settings()
        event.accept()
