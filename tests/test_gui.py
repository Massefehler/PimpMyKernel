import os
import tempfile
import threading
import unittest
from support import GermanTestCase
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QLocale, QSettings, QTimer, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox

from pimpmykernel.build import BuildResult, create_workspace
from pimpmykernel.i18n import language
from pimpmykernel.gui import (PAGE_INSTALLED, PAGE_SYSTEM, CancelCleanupDialog,
                              ConfigReviewDialog, InstallDialog, MainWindow, RemoveKernelsDialog, Worker)
from pimpmykernel.kernel_packages import InstalledKernel, RemovalPlan
from pimpmykernel.models import Cancelled, KernelError, Release, SystemInfo
from test_models_network import release


SYSTEM = SystemInfo("7.2.8", "Ubuntu test", 16, 32, "x86_64", True, "off")
KERNELS = [InstalledKernel("7.2.8", ("linux-image-7.2.8", "linux-headers-7.2.8"), 1024**3, True),
           InstalledKernel("6.8.0-31-generic", ("linux-image-6.8.0-31-generic",
                                             "linux-headers-6.8.0-31-generic"), 1024**3, False)]
PLAN = RemovalPlan((KERNELS[1].release,), KERNELS[1].packages, KERNELS[1].installed_bytes)


class GuiTests(GermanTestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pmk-gui-test-")
        self.root = Path(self.tmp.name)
        self.config = self.root / "config-7.2.8"
        self.config.write_text("CONFIG_TEST=y\n")
        self.settings = QSettings(str(self.root / "settings.ini"), QSettings.Format.IniFormat)
        self.patches = [patch("pimpmykernel.gui.QLocale", side_effect=lambda: QLocale("de_DE")),
                        patch("pimpmykernel.gui.SystemInfo.detect", return_value=SYSTEM),
                        patch("pimpmykernel.gui.missing_dependencies", return_value=[]),
                        patch("pimpmykernel.gui.boot_configs", return_value=[self.config]),
                        patch("pimpmykernel.gui.QSettings", return_value=self.settings),
                        patch("pimpmykernel.gui.installed_kernels", return_value=KERNELS)]
        for item in self.patches:
            item.start()
        self.window = MainWindow(auto_refresh=False)
        self.window.show()
        QTest.qWait(10)

    def tearDown(self):
        if self.window.worker:
            self.window.worker.cancel.set()
            self.window.worker.wait(5000)
            QTest.qWait(10)
        self.window.close()
        self.window.deleteLater()
        QTest.qWait(10)
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def test_selection_navigation_and_cpu_limits(self):
        self.assertFalse(self.window.build_button.isEnabled())
        self.window.on_releases([release(), release("6.18.55", "longterm")])
        self.assertTrue(self.window.next_button.isEnabled())
        QTest.mouseClick(self.window.next_button, Qt.MouseButton.LeftButton)
        self.assertEqual(self.window.stack.currentIndex(), 1)
        self.assertIn("Linux 7.2.9", self.window.options_summary.text())
        values = [self.window.build_jobs.itemData(i) for i in range(self.window.build_jobs.count())]
        self.assertTrue(all(value <= 16 for value in values))
        self.assertTrue({8, 10, 12, 16}.issubset(values))
        self.assertEqual(self.window.config_combo.currentData(), str(self.config))

    def test_language_choice_is_saved_immediately_and_applies_after_restart(self):
        self.assertEqual(self.window.language_combo.currentData(), "auto")
        self.assertFalse(self.settings.contains("language"))
        self.window.language_combo.setCurrentIndex(self.window.language_combo.findData("en"))
        saved = QSettings(self.settings.fileName(), QSettings.Format.IniFormat)
        self.assertEqual(saved.value("language"), "en")
        self.assertIn("beim nächsten App-Start", self.window.language_notice.text())
        self.assertEqual(language(), "de")
        self.assertEqual(self.window.page_title.text(), "Dein nächster Kernel")
        self.window._load_settings()
        self.assertEqual(self.window.language_combo.currentData(), "en")
        self.window._save_settings()
        self.assertEqual(self.settings.value("language"), "en")
        self.window.language_combo.setCurrentIndex(self.window.language_combo.findData("auto"))
        saved.sync()
        self.assertEqual(saved.value("language"), "auto")

    def test_invalid_saved_language_is_shown_as_automatic_without_rewriting_settings(self):
        self.settings.setValue("language", "unsupported")
        self.window._load_settings()
        self.assertEqual(self.window.language_combo.currentData(), "auto")
        self.assertEqual(self.settings.value("language"), "unsupported")
        self.assertIn("Die Auswahl wird gespeichert", self.window.language_notice.text())

    def test_unsaved_language_choice_reports_settings_write_error(self):
        with patch.object(self.settings, "status", return_value=QSettings.Status.AccessError):
            self.window.language_combo.setCurrentIndex(self.window.language_combo.findData("en"))
        self.assertIn("konnte nicht gespeichert werden", self.window.language_notice.text())
        self.assertEqual(language(), "de")

    def test_language_choice_can_be_saved_while_the_application_is_busy(self):
        self.window.set_busy(True)
        self.window.language_combo.setCurrentIndex(self.window.language_combo.findData("en"))
        self.assertEqual(self.settings.value("language"), "en")
        self.assertEqual(language(), "de")
        self.window.set_busy(False)

    def test_prerelease_is_visible_but_cannot_be_selected(self):
        rc = Release("7.3-rc6", "mainline", "2026-10-04", "https://git.kernel.org/snapshot.tar.gz", None)
        self.window.on_releases([release(), rc])
        self.assertEqual(self.window.release_list.count(), 1)
        self.window.show_rc.setChecked(True)
        self.assertEqual(self.window.release_list.count(), 2)
        self.assertFalse(self.window.release_list.item(1).flags() & Qt.ItemFlag.ItemIsEnabled)

    def test_older_versions_are_selectable_in_every_series_and_search_combines_with_filter(self):
        self.window.on_releases([release()])
        versions = ("7.2.8", "7.2.3", "6.1.1", "5.15.3", "4.19.2")
        self.window.on_archive_releases([release(version, "archive") for version in versions])
        self.assertEqual(self.window.hero_version.text(), "Linux 7.2.9")
        for version in versions:
            with self.subTest(version=version):
                major = int(version.split(".")[0])
                self.window.series_filter.setCurrentIndex(self.window.series_filter.findData(major))
                self.window.version_search.setText(version)
                self.assertEqual(self.window.release_list.count(), 1)
                self.assertEqual(self.window.selected_release().version, version)
                self.assertTrue(self.window.next_button.isEnabled())
                self.assertIn("Archiv", self.window.options_summary.text())
        self.window.version_search.setText("7.2.8")
        self.assertEqual(self.window.release_list.count(), 0)
        self.assertFalse(self.window.next_button.isEnabled())
        self.assertFalse(self.window.build_button.isEnabled())
        self.assertIn("Keine passende Version", self.window.release_hint.text())
        self.window.series_filter.setCurrentIndex(0)
        self.assertEqual(self.window.selected_release().version, "7.2.8")
        QTest.mouseClick(self.window.next_button, Qt.MouseButton.LeftButton)
        self.assertIn("Linux 7.2.8", self.window.options_summary.text())

    def test_current_metadata_wins_and_refresh_preserves_archive_selection(self):
        current = release("6.18.55", "longterm")
        self.window.on_releases([release(), current])
        self.window.on_archive_releases([release("7.2.9", "archive"), release("7.2.8", "archive"),
                                         release("6.18.55", "archive")])
        self.assertEqual(len(self.window.releases), 3)
        self.assertEqual(next(r for r in self.window.releases if r.version == current.version), current)
        self.window.release_list.setCurrentRow(1)
        self.assertEqual(self.window.selected_release().version, "7.2.8")
        self.window.on_releases([release("7.2.10"), current])
        self.assertEqual(self.window.selected_release().version, "7.2.8")
        self.assertEqual(self.window.hero_version.text(), "Linux 7.2.10")
        self.assertTrue(any(r.version == "7.2.8" for r in self.window.releases))

    def test_retired_current_feed_entry_becomes_buildable_after_archive_load(self):
        source = release("5.15.3", "longterm")
        retired = Release(source.version, source.channel, source.date, source.source, source.signature, eol=True)
        self.window.on_releases([release(), retired])
        self.window.version_search.setText(retired.version)
        self.assertIsNone(self.window.selected_release())
        self.assertIn("Ältere Versionen laden", self.window.release_hint.text())
        self.window.on_archive_releases([release(retired.version, "archive")])
        self.assertEqual(self.window.selected_release().version, retired.version)
        self.assertTrue(self.window.selected_release().eol)
        self.assertIn("Pflege beendet", self.window.release_list.item(0).text())
        self.assertTrue(self.window.build_button.isEnabled())

    def test_archive_worker_loads_and_releases_controls(self):
        self.window.on_releases([release()])
        ready = threading.Event()

        def archive(network):
            ready.wait(5)
            network.check_cancel()
            return [release("7.2.8", "archive"), release("7.2.3", "archive")]

        with patch("pimpmykernel.gui.Network.archive_releases", autospec=True, side_effect=archive):
            QTest.mouseClick(self.window.archive_button, Qt.MouseButton.LeftButton)
            self.assertFalse(self.window.archive_button.isEnabled())
            self.assertFalse(self.window.next_button.isEnabled())
            self.assertFalse(self.window.version_search.isEnabled())
            ready.set()
            self.wait_for_worker()
        self.assertTrue(self.window.archive_loaded)
        self.assertEqual(self.window.release_list.count(), 3)
        self.assertEqual(self.window.archive_button.text(), "Archiv aktualisieren")
        self.assertTrue(self.window.version_search.isEnabled())
        self.assertTrue(self.window.archive_button.isEnabled())
        self.assertTrue(self.window.next_button.isEnabled())

    def test_archive_failure_keeps_current_releases_and_supports_retry(self):
        self.window.on_releases([release()])
        with patch("pimpmykernel.gui.Network.archive_releases", side_effect=ValueError("archive unavailable")):
            self.window.load_archive_releases()
            self.wait_for_worker()
        self.assertFalse(self.window.archive_loaded)
        self.assertTrue(self.window.release_error.isVisible())
        self.assertIn("Ältere Versionen laden", self.window.release_error.text())
        self.assertEqual(self.window.selected_release().version, "7.2.9")
        self.assertEqual(self.window.hero_version.text(), "Linux 7.2.9")
        self.assertTrue(self.window.archive_button.isEnabled())
        self.assertTrue(self.window.next_button.isEnabled())
        with patch("pimpmykernel.gui.Network.archive_releases", return_value=[release("7.2.8", "archive")]):
            self.window.load_archive_releases()
            self.wait_for_worker()
        self.assertTrue(self.window.archive_loaded)
        self.assertFalse(self.window.release_error.isVisible())
        with patch("pimpmykernel.gui.Network.archive_releases", side_effect=ValueError("refresh unavailable")):
            self.window.load_archive_releases()
            self.wait_for_worker()
        self.assertIn("Archiv aktualisieren", self.window.release_error.text())
        self.assertTrue(any(r.version == "7.2.8" for r in self.window.releases))

    def test_closing_during_archive_load_cancels_worker(self):
        self.window.on_releases([release()])

        def archive(network):
            if not network.cancel.wait(5):
                raise RuntimeError("Test did not cancel archive request")
            network.check_cancel()

        with patch("pimpmykernel.gui.Network.archive_releases", autospec=True, side_effect=archive):
            self.window.load_archive_releases()
            self.window.close()
            self.wait_for_worker()
        self.assertTrue(self.window.close_pending)
        self.assertFalse(self.window.archive_loaded)
        self.assertFalse(self.window.isVisible())

    def test_build_worker_returns_packages_and_reenables_controls(self):
        self.window.on_releases([release()])
        output = self.root / "export"
        output.mkdir()
        package = output / "linux-image-test.deb"
        package.write_bytes(b"fixture")
        result = BuildResult("7.2.9-pmk", output, (package,), self.root / "build.log", None, "Test")
        with patch("pimpmykernel.gui.BuildEngine.run", return_value=result):
            self.window.output_edit.setText(str(output))
            self.window.start_build()
            self.assertFalse(self.window.output_edit.isEnabled())
            deadline = 100
            while self.window.worker and deadline:
                QTest.qWait(10)
                deadline -= 1
        self.assertIsNone(self.window.worker)
        self.assertEqual(self.window.result, result)
        self.assertTrue(self.window.result_card.isVisible())
        self.assertTrue(self.window.output_edit.isEnabled())
        self.assertFalse(self.window.cancel_button.isEnabled())
        self.assertTrue(self.window.install_button.isEnabled())

    def test_installation_requires_deliberate_selection_and_checks_secure_boot(self):
        result = BuildResult("7.2.9-pmk", self.root, (self.root / "image.deb",), self.root / "log", None, "Test")
        dialog = InstallDialog(result, SYSTEM, self.window)
        self.assertFalse(dialog.install.isEnabled())
        dialog.accept_box.setChecked(True)
        self.assertTrue(dialog.install.isEnabled())
        dialog.deleteLater()
        locked_system = SystemInfo("7.2.8", "Ubuntu test", 16, 32, "x86_64", True, "on")
        dialog = InstallDialog(result, locked_system, self.window)
        dialog.accept_box.setChecked(True)
        self.assertFalse(dialog.install.isEnabled())
        dialog.deleteLater()

    def test_release_failure_has_retry_and_no_build(self):
        self.window.worker_kind = "releases"
        self.window.on_failure("network unavailable", False)
        self.assertTrue(self.window.release_error.isVisible())
        self.assertIn("Aktualisieren", self.window.release_error.text())
        self.assertFalse(self.window.build_button.isEnabled())

    def test_latest_running_kernel_notice_handles_local_and_distribution_suffixes(self):
        for running in ("7.2.9", "7.2.9-pmk", "7.2.9-38-generic"):
            with self.subTest(running=running):
                self.window.system = SystemInfo(running, "Ubuntu test", 16, 32, "x86_64", True, "off")
                self.window.on_releases([release()])
                self.assertIn("bereits auf dem neuesten", self.window.hero_status.text())
                self.assertTrue(self.window.hero_status.isVisible())
                self.assertTrue(self.window.build_button.isEnabled())
        self.window.system = SYSTEM
        self.window.on_releases([release()])
        self.assertIn("neueres stabiles Release", self.window.hero_status.text())
        self.window.system = SystemInfo("7.3-rc6-pmk", "Ubuntu test", 16, 32, "x86_64", True, "off")
        self.window.on_releases([release("7.3")])
        self.assertIn("neueres stabiles Release", self.window.hero_status.text())

    def test_download_percentage_reaches_100_only_after_completion_and_stays_visible(self):
        self.window.navigate(2)
        self.window.on_stage(1, "Quellcode herunterladen", "kernel.org")
        self.assertEqual(self.window.download_bar.value(), 0)
        self.window.on_download(64 * 1024**2, 128 * 1024**2)
        self.assertEqual(self.window.download_bar.value(), 50)
        self.assertIn("64,0 von 128,0 MiB", self.window.download_detail.text())
        self.assertIn("kernel.org", self.window.download_detail.text())
        self.window.on_download(128 * 1024**2, 128 * 1024**2)
        self.assertEqual(self.window.download_bar.value(), 99)
        self.window.on_stage(2, "Signatur prüfen", "OpenPGP")
        self.assertEqual(self.window.download_bar.value(), 100)
        self.assertIn("vollständig heruntergeladen", self.window.download_label.text())
        self.window.on_stage(4, "Kernel kompilieren", "make")
        self.assertEqual(self.window.download_bar.value(), 100)
        self.assertTrue(self.window.download_panel.isVisible())

    def test_unknown_download_size_and_cancellation_never_report_false_completion(self):
        self.window.navigate(2)
        self.window.worker_kind = "build"
        self.window.on_stage(1, "Download", "kernel.org")
        self.window.on_download(15 * 1024**2, 0)
        self.assertEqual(self.window.download_bar.maximum(), 0)
        self.assertIn("Gesamtgröße unbekannt", self.window.download_detail.text())
        self.window.on_failure("abgebrochen", True)
        self.assertEqual(self.window.download_label.text(), "Download abgebrochen")
        self.assertNotIn("vollständig", self.window.download_label.text())
        self.window.worker_kind = ""

    def test_worker_progress_preserves_large_byte_counts(self):
        worker = Worker(lambda _: None)
        self.addCleanup(worker.deleteLater)
        worker.progressed.connect(self.window.on_download)
        self.window.throttled_progress(worker)(3 * 1024**3, 6 * 1024**3)
        self.assertEqual(self.window.download_current, 3 * 1024**3)
        self.assertEqual(self.window.download_total, 6 * 1024**3)
        self.assertEqual(self.window.download_bar.value(), 50)

    def show_kernel_selection(self):
        self.window.navigate(PAGE_INSTALLED)
        self.wait_for_worker()
        self.assertEqual(self.window.kernel_list.count(), 2)
        self.window.kernel_list.item(1).setCheckState(Qt.CheckState.Checked)
        self.assertTrue(self.window.remove_kernels_button.isEnabled())

    def test_installed_kernel_selection_protects_active_and_last_kernel(self):
        self.show_kernel_selection()
        active = self.window.kernel_list.item(0)
        self.assertFalse(active.flags() & Qt.ItemFlag.ItemIsUserCheckable)
        self.assertIn("aktiv, geschützt", active.text())
        self.assertIn("Dieser Kernel läuft gerade und kann nicht entfernt werden", active.text())
        self.assertIn("aktuell laufende Kernel", active.toolTip())
        self.assertIn("nicht entfernt werden, solange er läuft", active.toolTip())
        self.assertIn(KERNELS[0].packages[0], active.toolTip())
        active.setCheckState(Qt.CheckState.Checked)
        self.assertEqual(self.window.selected_kernel_releases(), PLAN.releases)
        self.window.on_installed_kernels([InstalledKernel("6.8.0-31-generic", PLAN.packages, 1024, False)])
        self.assertFalse(self.window.kernel_list.item(0).flags() & Qt.ItemFlag.ItemIsUserCheckable)
        self.assertIn("letzter installierter Kernel, geschützt", self.window.kernel_list.item(0).text())
        self.assertIn("letzte installierte Kernel kann nicht entfernt werden", self.window.kernel_list.item(0).toolTip())
        self.assertFalse(self.window.remove_kernels_button.isEnabled())

    def test_remove_dialog_requires_deliberate_selection(self):
        dialog = RemoveKernelsDialog(PLAN, SYSTEM.kernel, self.window)
        self.assertFalse(dialog.remove.isEnabled())
        dialog.accept_box.setChecked(True)
        self.assertTrue(dialog.remove.isEnabled())
        dialog.accept_box.setChecked(False)
        self.assertFalse(dialog.remove.isEnabled())
        dialog.deleteLater()

    def test_rejected_removal_preview_never_runs_a_privileged_command(self):
        self.show_kernel_selection()

        def reject(dialog):
            self.assertIsNone(self.window.worker)
            self.assertFalse(self.window.running)
            return QDialog.DialogCode.Rejected

        with patch("pimpmykernel.gui.plan_kernel_removal", return_value=PLAN) as preview, \
                patch.object(RemoveKernelsDialog, "exec", reject), \
                patch("pimpmykernel.gui.CommandRunner.run") as run:
            self.window.prepare_kernel_removal()
            self.wait_for_worker()
        preview.assert_called_once_with(PLAN.releases)
        run.assert_not_called()
        self.assertIn("nicht gestartet", self.window.footer_status.text())

    def test_confirmed_removal_refreshes_the_installed_kernel_list(self):
        self.show_kernel_selection()

        def accept(dialog):
            dialog.accept_box.setChecked(True)
            self.assertTrue(dialog.remove.isEnabled())
            return QDialog.DialogCode.Accepted

        with patch("pimpmykernel.gui.plan_kernel_removal", return_value=PLAN), \
                patch.object(RemoveKernelsDialog, "exec", accept), \
                patch("pimpmykernel.gui.CommandRunner.run", return_value=(0, "removed")) as run, \
                patch("pimpmykernel.gui.installed_kernels", return_value=KERNELS[:1]) as refresh:
            self.window.prepare_kernel_removal()
            self.wait_for_worker()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][-len(PLAN.packages)-1:], ["remove", *PLAN.packages])
        refresh.assert_called_once()
        self.assertEqual(self.window.stack.currentIndex(), PAGE_INSTALLED)
        self.assertEqual(self.window.kernel_list.count(), 1)
        self.assertFalse(self.window.remove_kernels_button.isEnabled())

    def test_preview_dependency_conflict_stops_before_confirmation_and_removal(self):
        self.show_kernel_selection()
        with patch("pimpmykernel.gui.plan_kernel_removal", side_effect=ValueError("APT würde weitere Pakete entfernen")), \
                patch.object(RemoveKernelsDialog, "exec") as dialog, \
                patch("pimpmykernel.gui.CommandRunner.run") as run:
            self.window.prepare_kernel_removal()
            self.wait_for_worker()
        dialog.assert_not_called()
        run.assert_not_called()
        self.assertTrue(self.window.kernel_error.isVisible())
        self.assertIn("weitere Pakete", self.window.kernel_error.text())
        self.assertFalse(self.window.remove_kernels_button.isEnabled())

    def test_kernel_refresh_is_deferred_until_a_running_worker_finishes(self):
        finished = threading.Event()
        self.window._launch_worker("build", lambda _: finished.wait(5), lambda _: None)
        self.window.set_busy(True)
        with patch("pimpmykernel.gui.installed_kernels", return_value=KERNELS) as refresh:
            self.window.navigate(PAGE_INSTALLED)
            self.assertTrue(self.window.pending_kernel_refresh)
            refresh.assert_not_called()
            finished.set()
            self.wait_for_worker()
        refresh.assert_called_once()
        self.assertEqual(self.window.kernel_list.count(), 2)

    def test_closing_during_preview_does_not_offer_or_start_removal(self):
        self.show_kernel_selection()
        ready = threading.Event()

        def preview(_):
            ready.wait(5)
            return PLAN

        with patch("pimpmykernel.gui.plan_kernel_removal", side_effect=preview), \
                patch.object(RemoveKernelsDialog, "exec") as dialog, \
                patch("pimpmykernel.gui.CommandRunner.run") as run:
            self.window.prepare_kernel_removal()
            self.window.close()
            ready.set()
            self.wait_for_worker()
        self.assertTrue(self.window.close_pending)
        dialog.assert_not_called()
        run.assert_not_called()

    def test_missing_build_dependencies_open_the_system_page(self):
        self.window.on_releases([release()])
        self.window.output_edit.setText(str(self.root / "exports"))
        with patch("pimpmykernel.gui.missing_dependencies", return_value=["gcc"]):
            self.window.start_build()
        self.assertEqual(self.window.stack.currentIndex(), PAGE_SYSTEM)
        self.assertIsNone(self.window.worker)

    def test_menuconfig_is_opt_in_even_with_old_saved_settings(self):
        self.assertFalse(self.window.menuconfig_box.isChecked())
        self.settings.setValue("menuconfig", True)
        another_window = MainWindow(auto_refresh=False)
        try:
            self.assertFalse(another_window.menuconfig_box.isChecked())
            self.assertIn("Nur verwenden", another_window.menuconfig_box.toolTip())
        finally:
            another_window.close()
            another_window.deleteLater()

    def expert_build(self, review):
        self.window.on_releases([release()])
        self.window.output_edit.setText(str(self.root / "exports"))
        self.window.menuconfig_box.setChecked(True)
        package = self.root / "image.deb"
        package.write_bytes(b"test package, never installed")
        result = BuildResult("7.2.9-pmk", self.root, (package,), self.root / "log", None, "Test")

        def build(engine):
            self.assertTrue(engine.options.menuconfig)
            if not engine.review_config('+CONFIG_MENUCONFIG_TEST=y'):
                raise Cancelled("Konfiguration abgelehnt")
            return result

        with patch("pimpmykernel.gui.menuconfig_terminal", return_value="/test/xterm"), \
                patch("pimpmykernel.gui.BuildEngine.run", autospec=True, side_effect=build), \
                patch.object(ConfigReviewDialog, "exec", review):
            self.window.start_build()
            self.wait_for_worker()
        return result

    def test_menuconfig_review_pauses_worker_until_deliberate_continue(self):
        def accept(dialog):
            self.assertIsNotNone(self.window.worker)
            self.assertTrue(self.window.worker.isRunning())
            self.assertFalse(self.window.menuconfig_box.isEnabled())
            self.assertIsNone(self.window.result)
            self.assertTrue(dialog.changes.isReadOnly())
            self.assertIn('+CONFIG_MENUCONFIG_TEST=y', dialog.changes.toPlainText())
            self.assertFalse(dialog.proceed.isDefault())
            return QDialog.DialogCode.Accepted

        result = self.expert_build(accept)
        self.assertEqual(self.window.result, result)
        self.assertTrue(self.window.menuconfig_box.isEnabled())
        self.assertFalse(self.window.cancel_button.isEnabled())

    def test_menuconfig_review_rejection_aborts_and_releases_controls(self):
        self.expert_build(lambda _: QDialog.DialogCode.Rejected)
        self.assertIsNone(self.window.result)
        self.assertEqual(self.window.stage_title.text(), "Build abgebrochen")
        self.assertTrue(self.window.menuconfig_box.isEnabled())
        self.assertFalse(self.window.cancel_button.isEnabled())

    def test_closing_during_menuconfig_review_ends_worker_and_dialog(self):
        original_exec = ConfigReviewDialog.exec

        def close_during_review(dialog):
            QTimer.singleShot(20, self.window.close)
            return original_exec(dialog)

        self.expert_build(close_during_review)
        QTest.qWait(20)
        self.assertIsNone(self.window.result)
        self.assertTrue(self.window.close_pending)
        self.assertFalse(self.window.isVisible())

    def test_missing_menuconfig_terminal_is_reported_before_starting_worker(self):
        self.window.on_releases([release()])
        self.window.output_edit.setText(str(self.root / "exports"))
        self.window.menuconfig_box.setChecked(True)
        with patch("pimpmykernel.gui.menuconfig_terminal", side_effect=KernelError("Für menuconfig fehlt xterm")):
            self.window.start_build()
        self.assertIsNone(self.window.worker)
        self.assertIn("fehlt xterm", self.window.build_notice.text())

    def abort_running_build(self, choice, *, close_window=False):
        self.window.on_releases([release()])
        self.window.output_edit.setText(str(self.root / "exports"))
        work_root = self.root / "jobs"
        workspace = create_workspace(work_root)
        (workspace / "linux.tar.xz").write_bytes(b"download")
        (workspace / "compile").mkdir()
        (workspace / "compile/kernel.o").write_bytes(b"build artifact")
        (workspace / "tmp").mkdir()
        (workspace / "tmp/helper-file").write_text("temporary")
        other_job = create_workspace(work_root)
        (other_job / "keep").write_text("another build")
        log_path = self.root / "saved-build.log"
        log_path.write_text("diagnostic log")
        export = self.root / "exported.deb"
        export.write_bytes(b"exported package")
        ready, stopped = threading.Event(), threading.Event()

        def build(engine):
            engine.work_root = work_root
            engine.workspace = workspace
            engine.log_path = log_path
            ready.set()
            if not engine.cancel.wait(5):
                raise RuntimeError("Test did not cancel the build")
            stopped.set()
            raise Cancelled("Vorgang abgebrochen.")

        def select_cleanup(dialog):
            self.assertTrue(stopped.is_set())
            self.assertIsNone(self.window.worker)
            self.assertFalse(self.window.running)
            self.assertEqual(dialog.remove.text(), "Arbeitsdateien löschen")
            self.assertEqual(dialog.keep.text(), "Zur Fehlersuche behalten")
            return choice

        with patch("pimpmykernel.gui.BuildEngine.run", autospec=True, side_effect=build), \
                patch.object(CancelCleanupDialog, "exec", select_cleanup):
            self.window.start_build()
            for _ in range(100):
                if ready.is_set():
                    break
                QTest.qWait(10)
            self.assertTrue(ready.is_set())
            if close_window:
                with patch.object(CancelCleanupDialog, "exec") as prompt:
                    self.window.close()
                    self.wait_for_worker()
                    prompt.assert_not_called()
            else:
                QTest.mouseClick(self.window.cancel_button, Qt.MouseButton.LeftButton)
                self.wait_for_worker()
        self.assertIsNone(self.window.worker)
        self.assertFalse(self.window.running)
        self.assertEqual((other_job / "keep").read_text(), "another build")
        self.assertEqual(log_path.read_text(), "diagnostic log")
        self.assertEqual(export.read_bytes(), b"exported package")
        return workspace

    def wait_for_worker(self):
        for _ in range(600):
            QTest.qWait(10)
            if self.window.worker is None and not self.window.pending_kernel_refresh:
                break
        self.assertIsNone(self.window.worker)
        self.assertFalse(self.window.pending_kernel_refresh)

    def test_cancel_button_can_delete_job_after_build_processes_stop(self):
        workspace = self.abort_running_build(QDialog.DialogCode.Accepted)
        self.assertFalse(workspace.exists())
        self.assertIsNone(self.window.engine.workspace)
        self.assertFalse(self.window.cleanup_button.isVisible())
        self.assertEqual(self.window.stage_title.text(), "Arbeitsordner gelöscht")

    def test_cancel_button_can_keep_files_and_delete_them_later(self):
        workspace = self.abort_running_build(QDialog.DialogCode.Rejected)
        self.assertTrue(workspace.exists())
        self.assertEqual(self.window.engine.workspace, workspace)
        self.assertTrue(self.window.cleanup_button.isVisible())
        self.assertTrue(self.window.cleanup_button.isEnabled())
        with patch("pimpmykernel.gui.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            QTest.mouseClick(self.window.cleanup_button, Qt.MouseButton.LeftButton)
            self.wait_for_worker()
        self.assertFalse(workspace.exists())
        self.assertIsNone(self.window.engine.workspace)

    def test_closing_window_does_not_show_cancel_button_cleanup_question(self):
        workspace = self.abort_running_build(QDialog.DialogCode.Accepted, close_window=True)
        self.assertTrue(workspace.exists())
        self.assertTrue(self.window.close_pending)


if __name__ == "__main__":
    unittest.main()
