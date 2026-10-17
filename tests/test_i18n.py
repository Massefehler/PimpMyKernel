import ast
import json
import os
import string
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QLocale, QSettings, Qt
from PyQt6.QtWidgets import QApplication, QLabel

from pimpmykernel import i18n
from pimpmykernel.gui import ConfigReviewDialog, MainWindow, RemoveKernelsDialog, format_number
from pimpmykernel.kernel_packages import create_removal_plan
from pimpmykernel.models import KernelError
from pimpmykernel.runner import CommandRunner, privileged_command
import test_gui as gui_fixture
import test_kernel_packages as package_fixture
import test_menuconfig as menu_fixture

ROOT = Path(__file__).resolve().parent.parent


def locale_environment(lang, preferences="", *, config_home=None):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("LC_") and key not in {"LANG", "LANGUAGE"}}
    env.update(LANG=lang, LANGUAGE=preferences, QT_QPA_PLATFORM="offscreen")
    if config_home is not None:
        env["XDG_CONFIG_HOME"] = str(config_home)
    return env


class TranslationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pmk-language-settings-test-")
        self.addCleanup(temporary.cleanup)
        self.settings_home = Path(temporary.name)

    def test_regional_variants_and_preference_order(self):
        for preferences, expected in [(["de-AT", "en"], "de"), (["DE_ch.UTF-8"], "de"),
                                      (["en_GB.UTF-8", "de"], "en"),
                                      (["es-ES", "en"], "es"), (["es_MX.UTF-8"], "es"),
                                      (["ES_ar.UTF-8"], "es"),
                                      (["fr-FR", "es-CO", "en"], "es"),
                                      (["fr-FR", "de-DE", "en"], "de"),
                                      (["zh-Hant-TW", "en-US"], "en")]:
            with self.subTest(preferences=preferences):
                self.assertEqual(i18n.choose_language(preferences), expected)

    def test_unknown_or_empty_system_language_falls_back_to_english(self):
        for preferences in [[], [""], ["fr_FR"], ["C"], ["POSIX"], ["../../de"]]:
            self.assertEqual(i18n.choose_language(preferences), "en")

    def test_catalog_covers_all_application_messages_and_keeps_placeholders(self):
        messages = set()
        for source in [*ROOT.glob("pimpmykernel/*.py"), ROOT / "install-desktop.py"]:
            for node in ast.walk(ast.parse(source.read_text())):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                    continue
                arguments = node.args[:1] if node.func.id == "tr" else node.args[:2] if node.func.id == "plural" else []
                for argument in arguments:
                    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                        messages.add(argument.value)
        formatter = string.Formatter()
        def placeholders(text):
            return sorted((field, spec, conversion) for _, field, spec, conversion
                          in formatter.parse(text) if field is not None)
        for code in i18n.SUPPORTED_LANGUAGES:
            if code == "de":
                continue
            catalog = json.loads((ROOT / f"pimpmykernel/assets/i18n/{code}.json").read_text())
            with self.subTest(language=code):
                self.assertEqual(messages, set(catalog))
            for source, translation in catalog.items():
                with self.subTest(language=code, message=source):
                    self.assertTrue(translation.strip())
                    self.assertEqual(placeholders(source), placeholders(translation))
                    self.assertEqual(source.count("\n"), translation.count("\n"))

    def test_dynamic_values_are_inserted_without_translating_or_interpreting_them(self):
        for code, expected in [("en", "Selected: Linux 7.2.8-{name}"),
                               ("es", "Seleccionado: Linux 7.2.8-{name}")]:
            with patch.object(i18n, "_language", code):
                self.assertEqual(i18n.tr("Ausgewählt: Linux {version}", version="7.2.8-{name}"), expected)

    def test_singular_and_plural_forms_in_all_languages(self):
        for code, expected in [("de", ["1 Paket", "0 Pakete", "2 Pakete"]),
                               ("en", ["1 package", "0 packages", "2 packages"]),
                               ("es", ["1 paquete", "0 paquetes", "2 paquetes"])]:
            with patch.object(i18n, "_language", code):
                self.assertEqual([i18n.plural("{count} Paket", "{count} Pakete", n) for n in [1, 0, 2]], expected)

    def test_real_process_startup_uses_desktop_language_every_time(self):
        script = ("from pimpmykernel.i18n import initialize_language,tr; "
                  "print(initialize_language()); print(tr('Build abbrechen'))")
        for lang, preferences, expected in [("de_DE.UTF-8", "de_CH", "de\nBuild abbrechen"),
                                            ("en_US.UTF-8", "en_GB", "en\nCancel build"),
                                            ("es_ES.UTF-8", "es_ES", "es\nCancelar compilación"),
                                            ("es_MX.UTF-8", "es_AR", "es\nCancelar compilación"),
                                            ("fr_FR.UTF-8", "", "en\nCancel build"),
                                            ("de_DE.UTF-8", "en_US:de_DE", "en\nCancel build"),
                                            ("C", "", "en\nCancel build")]:
            with self.subTest(lang=lang, preferences=preferences):
                result = subprocess.run([sys.executable, "-c", script], cwd=ROOT,
                                        env=locale_environment(lang, preferences, config_home=self.settings_home),
                                        capture_output=True, text=True, check=True, timeout=10)
                self.assertEqual(result.stdout.strip(), expected)

    def test_cli_help_changes_language_and_check_keeps_json_keys(self):
        for lang, preferences, expected in [("de_DE.UTF-8", "de_DE", "Systemcheck ohne GUI"),
                                            ("en_US.UTF-8", "en_US", "Print system check"),
                                            ("es_ES.UTF-8", "es_ES", "Mostrar la comprobación del sistema")]:
            env = locale_environment(lang, preferences, config_home=self.settings_home)
            help_result = subprocess.run([sys.executable, "-m", "pimpmykernel", "--help"],
                                         cwd=ROOT, env=env, capture_output=True, text=True,
                                         check=True, timeout=10)
            self.assertIn(expected, help_result.stdout)
            check = subprocess.run([sys.executable, "-m", "pimpmykernel", "--check"],
                                   cwd=ROOT, env=env, capture_output=True, text=True,
                                   check=True, timeout=15)
            self.assertEqual(set(json.loads(check.stdout)),
                             {"kernel", "distro", "cpus", "ram_gib", "architecture", "debian",
                              "secure_boot", "missing_dependencies", "configs"})

    def test_saved_choice_survives_restart_and_automatic_mode_follows_the_system(self):
        save = ("import sys; from PyQt6.QtCore import QSettings; "
                "settings=QSettings('PimpMyKernel','PimpMyKernel'); "
                "settings.setValue('language',sys.argv[1]); settings.sync()")
        start = ("from pimpmykernel.i18n import initialize_language,tr; "
                 "print(initialize_language()); print(tr('Build abbrechen'))")
        for choice, locale, expected in [(None, "es_ES.UTF-8", "es\nCancelar compilación"),
                                         ("de", "es_ES.UTF-8", "de\nBuild abbrechen"),
                                         (None, "en_US.UTF-8", "de\nBuild abbrechen"),
                                         ("en", "es_ES.UTF-8", "en\nCancel build"),
                                         (None, "es_MX.UTF-8", "en\nCancel build"),
                                         ("es", "de_DE.UTF-8", "es\nCancelar compilación"),
                                         (None, "en_US.UTF-8", "es\nCancelar compilación"),
                                         ("auto", "es_MX.UTF-8", "es\nCancelar compilación"),
                                         ("auto", "de_DE.UTF-8", "de\nBuild abbrechen"),
                                         (None, "en_US.UTF-8", "en\nCancel build"),
                                         ("unsupported", "de_DE.UTF-8", "de\nBuild abbrechen")]:
            with self.subTest(choice=choice, locale=locale):
                env = locale_environment(locale, config_home=self.settings_home)
                if choice is not None:
                    subprocess.run([sys.executable, "-c", save, choice], cwd=ROOT, env=env,
                                   capture_output=True, text=True, check=True, timeout=10)
                result = subprocess.run([sys.executable, "-c", start], cwd=ROOT, env=env,
                                        capture_output=True, text=True, check=True, timeout=10)
                self.assertEqual(result.stdout.strip(), expected)

    def test_invalid_saved_choices_use_automatic_mode(self):
        settings = QSettings(str(self.settings_home / "settings.ini"), QSettings.Format.IniFormat)
        for value in [None, "", "../../en", "fr", "EN", "ES", 5, ["en"]]:
            settings.setValue("language", value)
            self.assertEqual(i18n.language_preference(settings), "auto")
        for value in ["auto", *i18n.SUPPORTED_LANGUAGES]:
            settings.setValue("language", value)
            self.assertEqual(i18n.language_preference(settings), value)

    def test_privileged_command_propagates_language_without_changing_action_arguments(self):
        for code in i18n.SUPPORTED_LANGUAGES:
            with patch.object(i18n, "_language", code), patch("shutil.which", return_value="/usr/bin/pkexec"):
                command = privileged_command("install", "/tmp/packages with spaces/kernel.deb")
            self.assertEqual(command[:3], ["pkexec", "/usr/bin/python3", "-I"])
            self.assertEqual(command[4:], ["--language", code, "install", "/tmp/packages with spaces/kernel.deb"])

    @unittest.skipIf(os.geteuid() == 0, "requires an unprivileged development user")
    def test_isolated_privileged_helper_translates_authentication_error_without_qt(self):
        helper = ROOT / "pimpmykernel/privileged.py"
        env = locale_environment("C")
        for code, expected in [("de", "Administratorfreigabe"), ("en", "administrator authentication"),
                               ("es", "autenticación gráfica de un administrador")]:
            result = subprocess.run([sys.executable, "-I", str(helper), "--language", code, "dependencies"],
                                    env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertIn(expected, result.stderr)
        script = ("import runpy,sys; runpy.run_path(sys.argv[1], run_name='helper_import_test'); "
                  "assert not any(name.startswith('PyQt6') for name in sys.modules)")
        subprocess.run([sys.executable, "-I", "-c", script, str(helper)], check=True, env=env, timeout=10)

    def test_invalid_helper_language_is_rejected_before_any_action(self):
        from pimpmykernel import privileged
        with patch.object(sys, "argv", ["privileged.py", "--language", "../../en", "dependencies"]), \
                patch("pimpmykernel.privileged.subprocess.run") as run:
            self.assertEqual(privileged.main(), 2)
            run.assert_not_called()

    def test_backend_errors_use_the_application_language(self):
        for code, protected, command_error in [("en", "running kernel cannot be uninstalled", "exited with error 7"),
                                               ("es", "no se puede desinstalar", "terminó con el error 7")]:
            with patch.object(i18n, "_language", code):
                with self.assertRaisesRegex(ValueError, protected):
                    create_removal_plan([package_fixture.RUNNING], package_fixture.PACKAGES, package_fixture.RUNNING)
                runner = CommandRunner(threading.Event(), lambda line: None)
                with self.assertRaisesRegex(KernelError, command_error):
                    runner.run([sys.executable, "-c", "raise SystemExit(7)"])

    @unittest.skipUnless(menu_fixture.shutil.which("make"), "make required")
    def test_menuconfig_terminal_keeps_language_despite_clean_c_environment(self):
        for code, instruction, review in [("en", "Save changes to .config and exit menuconfig.",
                                           "review the changes in PimpMyKernel"),
                                          ("es", "Guarda los cambios en .config y sal de menuconfig.",
                                           "revisa los cambios en PimpMyKernel")]:
            fixture = menu_fixture.MenuconfigTests()
            fixture.setUp()
            self.addCleanup(fixture.tearDown)
            with patch.object(i18n, "_language", code):
                fixture.run_menu()
            terminal_log = (fixture.root / "menuconfig/terminal.log").read_text()
            self.assertIn(instruction, terminal_log)
            self.assertIn(review, terminal_log)
            self.assertEqual(fixture.runner.environment()["LC_ALL"], "C")


class EnglishGuiTests(unittest.TestCase):
    language = "en"
    locale = "en_US"

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix=f"pmk-{self.language}-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        config = self.root / "config-7.2.8"
        config.write_text("CONFIG_TEST=y\n")
        settings = QSettings(str(self.root / "settings.ini"), QSettings.Format.IniFormat)
        settings.setValue("language", self.language)
        for item in [patch.object(i18n, "_language", self.language),
                     patch("pimpmykernel.gui.QLocale", side_effect=lambda: QLocale(self.locale)),
                     patch("pimpmykernel.gui.SystemInfo.detect", return_value=gui_fixture.SYSTEM),
                     patch("pimpmykernel.gui.missing_dependencies", return_value=[]),
                     patch("pimpmykernel.gui.boot_configs", return_value=[config]),
                     patch("pimpmykernel.gui.QSettings", return_value=settings)]:
            item.start()
            self.addCleanup(item.stop)
        self.window = MainWindow(auto_refresh=False)
        self.addCleanup(self.window.deleteLater)
        self.addCleanup(self.window.close)

    def test_all_pages_and_expert_warning_are_english(self):
        expected = ["Your next kernel", "Everything for your build", "From source to kernel",
                    "Your installed kernels", "Your system is the foundation"]
        for index, title in enumerate(expected):
            # Avoid a real worker when navigating to installed kernels.
            with patch.object(self.window, "refresh_installed_kernels"):
                self.window.navigate(index)
            self.assertEqual(self.window.page_title.text(), title)
        texts = [label.text() for label in self.window.findChildren(QLabel)]
        self.assertIn("For Linux experts and enthusiasts", texts)
        self.assertTrue(any("incorrect options can remove drivers" in text for text in texts))
        self.assertEqual(self.window.refresh_button.text(), "↻  Refresh")
        self.assertIn("Recommended", self.window.build_jobs.currentText())

    def test_language_choices_are_understandable_and_saved_in_english(self):
        combo = self.window.language_combo
        self.assertEqual([combo.itemText(index) for index in range(combo.count())],
                         ["Automatic (system language)", "Deutsch", "English", "Español"])
        self.assertEqual(combo.currentData(), "en")
        combo.setCurrentIndex(combo.findData("de"))
        self.assertIn("Language choice saved", self.window.language_notice.text())
        self.assertIn("next time you start the app", self.window.language_notice.text())
        self.assertEqual(i18n.language(), "en")

    def test_installed_kernel_protection_and_removal_dialog_remain_effective(self):
        self.window.on_installed_kernels(gui_fixture.KERNELS)
        active = self.window.kernel_list.item(0)
        self.assertIn("currently running and cannot be removed", active.text())
        self.assertIn("cannot be removed while running", active.toolTip())
        self.assertFalse(active.flags() & Qt.ItemFlag.ItemIsUserCheckable)
        self.assertEqual(self.window.kernel_count.text(), "2 installed kernels")
        dialog = RemoveKernelsDialog(gui_fixture.PLAN, gui_fixture.SYSTEM.kernel, self.window)
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.windowTitle(), "Uninstall kernel")
        self.assertIn("including their configuration files", " ".join(
            label.text() for label in dialog.findChildren(QLabel)))
        self.assertFalse(dialog.remove.isEnabled())
        dialog.accept_box.setChecked(True)
        self.assertTrue(dialog.remove.isEnabled())
        residual = gui_fixture.InstalledKernel("6.11.0-21-generic",
                                               ("linux-modules-6.11.0-21-generic",),
                                               0, False, installed=False)
        self.window.on_installed_kernels([*gui_fixture.KERNELS, residual])
        self.assertIn("Configuration leftovers", self.window.kernel_list.item(2).text())
        self.assertEqual(self.window.kernel_count.text(),
                         "2 installed kernels · 1 entry with configuration leftovers")

    def test_review_dialog_and_latest_kernel_notice_are_english(self):
        self.window.on_releases([gui_fixture.release("7.2.8")])
        self.assertIn("already running the latest stable kernel", self.window.hero_status.text())
        dialog = ConfigReviewDialog("CONFIG_TEST=y", self.window)
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.windowTitle(), "menuconfig · Review configuration")
        self.assertEqual(dialog.proceed.text(), "Continue build")
        self.assertTrue(dialog.changes.isReadOnly())

    def test_numbers_and_dates_follow_locale_in_both_languages(self):
        self.window.on_download(64 * 1024**2, 128 * 1024**2)
        self.assertIn("64.0 of 128.0 MiB", self.window.download_detail.text())
        self.assertEqual(MainWindow.format_date("2026-10-06"), "10/6/2026")
        with patch("pimpmykernel.gui.QLocale", side_effect=lambda: QLocale("de_DE")):
            self.assertEqual(format_number(64), "64,0")
            self.assertEqual(MainWindow.format_date("2026-10-06"), "06.10.2026")

    def test_qt_standard_buttons_are_translated_on_german_and_spanish_startup(self):
        script = ("from pimpmykernel.i18n import initialize_language,install_qt_translations; "
                  "from PyQt6.QtWidgets import QApplication,QDialogButtonBox; "
                  "initialize_language(); app=QApplication([]); "
                  "assert install_qt_translations(app); "
                  "box=QDialogButtonBox(QDialogButtonBox.StandardButton.Close); "
                  "print(box.button(QDialogButtonBox.StandardButton.Close).text())")
        for locale, text in [("de_DE", "Schließen"), ("es_ES", "Cerrar")]:
            result = subprocess.run([sys.executable, "-c", script], cwd=ROOT,
                                    env=locale_environment(locale + ".UTF-8", locale, config_home=self.root),
                                    capture_output=True, text=True, check=True, timeout=10)
            self.assertIn(text, result.stdout)


class SpanishGuiTests(unittest.TestCase):
    language = "es"
    locale = "es_ES"
    setUp = EnglishGuiTests.setUp

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_all_pages_and_expert_warning_are_spanish(self):
        titles = ["Tu próximo kernel", "Todo para tu compilación", "Del código fuente al kernel",
                  "Tus kernels instalados", "Tu sistema es la base"]
        for index, title in enumerate(titles):
            with patch.object(self.window, "refresh_installed_kernels"):
                self.window.navigate(index)
            self.assertEqual(self.window.page_title.text(), title)
        texts = [label.text() for label in self.window.findChildren(QLabel)]
        self.assertIn("Para expertos y entusiastas de Linux", texts)
        self.assertTrue(any("las opciones incorrectas pueden eliminar controladores" in text for text in texts))
        self.assertEqual(self.window.refresh_button.text(), "↻  Actualizar")
        self.assertIn("Recomendado", self.window.build_jobs.currentText())

    def test_english_can_be_selected_and_persisted_from_spanish(self):
        combo = self.window.language_combo
        self.assertEqual([combo.itemText(index) for index in range(combo.count())],
                         ["Automático (idioma del sistema)", "Deutsch", "English", "Español"])
        self.assertEqual(combo.currentData(), "es")
        combo.setCurrentIndex(combo.findData("en"))
        self.assertIn("Selección de idioma guardada", self.window.language_notice.text())
        self.assertIn("la próxima vez que inicies la aplicación", self.window.language_notice.text())
        saved = QSettings(str(self.root / "settings.ini"), QSettings.Format.IniFormat)
        self.assertEqual(saved.value("language"), "en")
        self.assertEqual(i18n.language(), "es")
        self.assertEqual(self.window.page_title.text(), "Tu próximo kernel")

    def test_running_kernel_is_protected_and_removal_requires_confirmation(self):
        self.window.on_installed_kernels(gui_fixture.KERNELS)
        active = self.window.kernel_list.item(0)
        self.assertIn("se está ejecutando y no se puede eliminar", active.text())
        self.assertIn("No se puede eliminar mientras esté en ejecución", active.toolTip())
        self.assertFalse(active.flags() & Qt.ItemFlag.ItemIsUserCheckable)
        self.assertEqual(self.window.kernel_count.text(), "2 kernels instalados")
        dialog = RemoveKernelsDialog(gui_fixture.PLAN, gui_fixture.SYSTEM.kernel, self.window)
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.windowTitle(), "Desinstalar kernel")
        self.assertIn("archivos de configuración", " ".join(
            label.text() for label in dialog.findChildren(QLabel)))
        self.assertFalse(dialog.remove.isEnabled())
        dialog.accept_box.setChecked(True)
        self.assertTrue(dialog.remove.isEnabled())
        residual = gui_fixture.InstalledKernel("6.11.0-21-generic",
                                               ("linux-modules-6.11.0-21-generic",),
                                               0, False, installed=False)
        self.window.on_installed_kernels([*gui_fixture.KERNELS, residual])
        self.assertIn("Restos de configuración", self.window.kernel_list.item(2).text())
        self.assertEqual(self.window.kernel_count.text(),
                         "2 kernels instalados · 1 entrada con restos de configuración")

    def test_menuconfig_review_and_latest_kernel_notice_are_spanish(self):
        self.window.on_releases([gui_fixture.release("7.2.8")])
        self.assertIn("Estable", self.window.release_list.item(0).text())
        self.assertIn("ya ejecuta el último kernel estable", self.window.hero_status.text())
        dialog = ConfigReviewDialog("CONFIG_TEST=y", self.window)
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.windowTitle(), "menuconfig · Revisar configuración")
        self.assertEqual(dialog.proceed.text(), "Continuar compilación")
        self.assertTrue(dialog.changes.isReadOnly())

    def test_download_progress_uses_spanish_and_system_number_format(self):
        self.window.on_download(64 * 1024**2, 128 * 1024**2)
        self.assertIn("64,0 de 128,0 MiB", self.window.download_detail.text())
        self.assertEqual(self.window.download_bar.value(), 50)
        with patch("pimpmykernel.gui.QLocale", side_effect=lambda: QLocale("en_US")):
            self.window.on_download(64 * 1024**2, 128 * 1024**2)
        self.assertIn("64.0 de 128.0 MiB", self.window.download_detail.text())


if __name__ == "__main__":
    unittest.main()
