"""End-to-end: echte Signatur, make und kleine .debs, aber kein echter Kernel-Build."""

import json
import lzma
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from support import GermanTestCase
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from pimpmykernel.build import (BuildEngine, BuildResult, cleanup_workspace, create_workspace,
                               install_packages, sha256)
from pimpmykernel.models import BuildOptions, Cancelled, KernelError
from pimpmykernel.privileged import PACKAGES, validate_packages
from pimpmykernel.models import BUILD_PACKAGES
from pimpmykernel.runner import CommandRunner
from test_models_network import release


CONFIG_SCRIPT = '''#!/usr/bin/python3
import pathlib, sys
path=pathlib.Path('.config')
lines=path.read_text().splitlines()
args=iter(sys.argv[1:])
for action in args:
    symbol=next(args)
    lines=[line for line in lines if not line.startswith('CONFIG_'+symbol+'=') and line != '# CONFIG_'+symbol+' is not set']
    if action=='--set-str':
        lines.append('CONFIG_'+symbol+'="'+next(args)+'"')
    elif action=='--disable':
        lines.append('# CONFIG_'+symbol+' is not set')
    elif action=='--enable':
        lines.append('CONFIG_'+symbol+'=y')
path.write_text('\\n'.join(lines)+'\\n')
'''

PACKAGE_SCRIPT = '''import pathlib, subprocess
root=pathlib.Path.cwd()
for name in ['linux-image-7.2.9-pmk','linux-headers-7.2.9-pmk']:
    staging=root/'test-debs'/name
    control=staging/'DEBIAN'
    control.mkdir(parents=True)
    (control/'control').write_text('Package: '+name+'\\nVersion: 7.2.9-1\\nArchitecture: amd64\\nMaintainer: Test <test@example.invalid>\\nDescription: Local kernel workflow fixture\\n')
    payload=staging/'usr/share/pimpmykernel-test'
    payload.mkdir(parents=True)
    (payload/name).write_text('test payload, never installed')
    subprocess.run(['dpkg-deb','--build',str(staging),str(root.parent/(name+'_7.2.9-1_amd64.deb'))],check=True)
'''

MAKEFILE = '''.PHONY: all oldconfig olddefconfig bindeb-pkg
all:
\t@printf 'fixture kernel compilation complete\\n'
oldconfig:
\t@python3 -c 'input("New option [default] ")'
olddefconfig:
\t@printf 'fixture config dependency resolution complete\\n'
bindeb-pkg:
\t@python3 fixture_packages.py
'''


@unittest.skipUnless(shutil.which("gpg") and shutil.which("make") and shutil.which("dpkg-deb"), "Build tools required")
class BuildIntegrationTests(GermanTestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_tmp = tempfile.TemporaryDirectory(prefix="pmk-fixture-")
        cls.fixture = Path(cls.fixture_tmp.name)
        keyring = cls.fixture / "signer"
        keyring.mkdir(mode=0o700)
        base = ["gpg", "--batch", "--homedir", str(keyring), "--pinentry-mode", "loopback", "--passphrase", ""]
        subprocess.run(base + ["--quick-generate-key", "PMK Test <test@example.invalid>", "ed25519", "sign", "0"],
                       check=True, capture_output=True)
        listing = subprocess.run(base + ["--with-colons", "--list-keys"], capture_output=True, text=True, check=True)
        cls.fingerprint = next(line.split(":")[9] for line in listing.stdout.splitlines() if line.startswith("fpr:"))
        cls.public_key = cls.fixture / "test-key.asc"
        cls.public_key.write_bytes(subprocess.run(base + ["--armor", "--export", cls.fingerprint],
                                                  capture_output=True, check=True).stdout)
        tree = cls.fixture / "linux-7.2.9"
        (tree / "scripts").mkdir(parents=True)
        (tree / "Makefile").write_text(MAKEFILE)
        (tree / "scripts/config").write_text(CONFIG_SCRIPT)
        (tree / "scripts/config").chmod(0o755)
        (tree / "fixture_packages.py").write_text(PACKAGE_SCRIPT)
        cls.tar_path = cls.fixture / "linux-7.2.9.tar"
        with tarfile.open(cls.tar_path, "w") as archive:
            archive.add(tree, arcname=tree.name)
        cls.archive = cls.fixture / "linux-7.2.9.tar.xz"
        cls.archive.write_bytes(lzma.compress(cls.tar_path.read_bytes()))
        cls.signature = cls.fixture / "linux-7.2.9.tar.sign"
        subprocess.run(base + ["--armor", "--detach-sign", "--output", str(cls.signature), str(cls.tar_path)],
                       check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.fixture_tmp.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pmk-test-build-")
        self.root = Path(self.tmp.name)
        self.config = self.root / "config"
        self.config.write_text(
            'CONFIG_TEST=y\nCONFIG_DEBUG_INFO=y\n'
            'CONFIG_SYSTEM_TRUSTED_KEYS="debian/canonical-certs.pem"\n'
            'CONFIG_SYSTEM_REVOCATION_KEYS="debian/canonical-revoked-certs.pem"\n'
            'CONFIG_MODULE_SIG=y\nCONFIG_MODULE_SIG_ALL=y\n'
            'CONFIG_MODULE_SIG_KEY="certs/signing_key.pem"\n')
        self.output = self.root / "packages with spaces"
        self.logs = []
        self.stages = []
        self.options = BuildOptions(release(), self.config, self.output, 1, 1)
        self.engine = BuildEngine(self.options, threading.Event(), self.logs.append,
                                  lambda *stage: self.stages.append(stage), lambda *_: None,
                                  work_root=self.root / "jobs")
        self.patches = [
            patch("pimpmykernel.build.SIGNERS", {self.fingerprint: "PMK Test"}),
            patch("pimpmykernel.build.SIGNER_EMAILS", {self.fingerprint: "test@example.invalid"}),
            patch("pimpmykernel.build.state_home", return_value=self.root / "state"),
            patch("pimpmykernel.build.missing_dependencies", return_value=[]),
        ]
        for item in self.patches:
            item.start()
        original_run = self.engine.runner.run

        def command(args, *a, **kw):
            if "--locate-keys" in args:
                home = args[args.index("--homedir") + 1]
                return original_run(["gpg", "--batch", "--homedir", home, "--import", str(self.public_key)], *a, **kw)
            return original_run(args, *a, **kw)

        self.engine.runner.run = command

        def download(url, path, progress, **kwargs):
            shutil.copyfile(self.signature if url.endswith(".sign") else self.archive, path)
            progress(path.stat().st_size, path.stat().st_size)

        self.engine.network.download = download

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def test_complete_signed_workflow_exports_real_packages_before_cleanup(self):
        result = self.engine.run()
        self.assertEqual(result.version, "7.2.9-pmk")
        self.assertEqual(len(result.packages), 2)
        self.assertEqual(result.signer, "PMK Test")
        self.assertIsNone(result.workspace)
        self.assertFalse(self.engine.workspace.exists())
        self.assertEqual([stage[0] for stage in self.stages], list(range(9)))
        manifest = json.loads((result.directory / "manifest.json").read_text())
        for item in manifest["packages"]:
            self.assertEqual(item["sha256"], sha256(result.directory / item["file"]))
        self.assertTrue((result.directory / "build.log").is_file())
        self.assertIn("Download und Arbeitsordner gelöscht", (result.directory / "build.log").read_text())
        config = (result.directory / "kernel.config").read_text()
        self.assertIn('CONFIG_SYSTEM_TRUSTED_KEYS=""', config)
        self.assertIn('CONFIG_SYSTEM_REVOCATION_KEYS=""', config)
        self.assertIn('CONFIG_MODULE_SIG_KEY="certs/signing_key.pem"', config)
        self.assertIn('CONFIG_MODULE_SIG=y', config)
        self.assertIn('CONFIG_MODULE_SIG_ALL=y', config)
        self.assertTrue(any('"debian/canonical-certs.pem" → ""' in line for line in self.logs))
        self.assertTrue(any('"debian/canonical-revoked-certs.pem" → ""' in line for line in self.logs))
        self.assertIn("CONFIG_SYSTEM_TRUSTED_KEYS", (result.directory / "config-changes.diff").read_text())
        self.assertEqual((result.directory / "original.config").read_text(), self.config.read_text())
        self.assertEqual(validate_packages([str(p) for p in result.packages]), [str(p) for p in result.packages])
        self.assertTrue(any("make oldconfig" in line for line in self.logs))
        self.assertTrue(any("make -j1" in line for line in self.logs))
        self.assertTrue(any("make bindeb-pkg -j1" in line for line in self.logs))

    def test_present_certificate_file_is_preserved(self):
        self.config.write_text(self.config.read_text().replace("debian/canonical-certs.pem", "certs/custom.pem"))
        original_configure = self.engine.configure

        def configure_with_certificate(tree):
            (tree / "certs").mkdir()
            (tree / "certs/custom.pem").write_text("certificate fixture")
            original_configure(tree)

        self.engine.configure = configure_with_certificate
        result = self.engine.run()
        config = (result.directory / "kernel.config").read_text()
        self.assertIn('CONFIG_SYSTEM_TRUSTED_KEYS="certs/custom.pem"', config)
        self.assertIn('CONFIG_SYSTEM_REVOCATION_KEYS=""', config)
        self.assertIn('CONFIG_MODULE_SIG_KEY="certs/signing_key.pem"', config)

    def test_certificate_cleanup_can_be_disabled(self):
        self.engine.options = BuildOptions(release(), self.config, self.output, 1, 1, repair_certificates=False)
        result = self.engine.run()
        config = (result.directory / "kernel.config").read_text()
        self.assertIn('CONFIG_SYSTEM_TRUSTED_KEYS="debian/canonical-certs.pem"', config)
        self.assertIn('CONFIG_SYSTEM_REVOCATION_KEYS="debian/canonical-revoked-certs.pem"', config)
        self.assertIn('CONFIG_MODULE_SIG_KEY="certs/signing_key.pem"', config)

    def prepare_expert_configuration(self, runner, tree, workspace):
        config = (tree / ".config").read_text()
        self.assertIn('# CONFIG_DEBUG_INFO_BTF is not set', config)
        self.assertIn('CONFIG_SYSTEM_TRUSTED_KEYS=""', config)
        config = config.replace('# CONFIG_DEBUG_INFO_BTF is not set', 'CONFIG_DEBUG_INFO_BTF=y')
        config = config.replace('CONFIG_LOCALVERSION=""', 'CONFIG_LOCALVERSION="-custom"')
        (tree / ".config").write_text(config + 'CONFIG_MENUCONFIG_TEST=y\n')

    def test_menuconfig_changes_are_reviewed_built_and_exported_before_cleanup(self):
        self.engine.options = replace(self.options, menuconfig=True)
        self.engine.review_config = Mock(return_value=True)
        with patch("pimpmykernel.build.menuconfig_terminal", return_value="/test/xterm"), \
                patch("pimpmykernel.build.run_menuconfig", side_effect=self.prepare_expert_configuration):
            result = self.engine.run()
        self.engine.review_config.assert_called_once()
        self.assertIn('+CONFIG_MENUCONFIG_TEST=y', self.engine.review_config.call_args.args[0])
        final_config = (result.directory / "kernel.config").read_text()
        self.assertIn('CONFIG_DEBUG_INFO_BTF=y', final_config)
        self.assertIn('CONFIG_MENUCONFIG_TEST=y', final_config)
        self.assertIn('CONFIG_LOCALVERSION=""', final_config)
        self.assertNotIn('-custom', final_config)
        before = (result.directory / "before-menuconfig.config").read_text()
        self.assertNotIn('CONFIG_MENUCONFIG_TEST', before)
        self.assertIn('# CONFIG_DEBUG_INFO_BTF is not set', before)
        self.assertIn('+CONFIG_MENUCONFIG_TEST=y', (result.directory / "menuconfig-changes.diff").read_text())
        manifest = json.loads((result.directory / "manifest.json").read_text())
        self.assertTrue(manifest['menuconfig'])
        self.assertNotIn('CONFIG_MENUCONFIG_TEST', self.config.read_text())
        self.assertEqual(len(result.packages), 2)
        self.assertFalse(self.engine.workspace.exists())

    def test_rejected_menuconfig_review_stops_before_compilation_and_keeps_job(self):
        self.engine.options = replace(self.options, menuconfig=True)
        self.engine.review_config = Mock(return_value=False)
        with patch("pimpmykernel.build.menuconfig_terminal", return_value="/test/xterm"), \
                patch("pimpmykernel.build.run_menuconfig", side_effect=self.prepare_expert_configuration):
            with self.assertRaises(Cancelled):
                self.engine.run()
        self.engine.review_config.assert_called_once()
        self.assertFalse(any(line.startswith("$ make -j") for line in self.logs))
        self.assertTrue((self.engine.workspace / "menuconfig-changes.diff").exists())
        self.assertTrue(self.engine.log_path.exists())
        self.assertEqual(list(self.output.iterdir()), [])

    def test_menuconfig_terminal_error_never_reviews_or_compiles(self):
        self.engine.options = replace(self.options, menuconfig=True)
        self.engine.review_config = Mock(return_value=True)
        with patch("pimpmykernel.build.menuconfig_terminal", return_value="/test/xterm"), \
                patch("pimpmykernel.build.run_menuconfig", side_effect=KernelError("terminal closed")):
            with self.assertRaisesRegex(KernelError, "terminal closed"):
                self.engine.run()
        self.engine.review_config.assert_not_called()
        self.assertFalse(any(line.startswith("$ make -j") for line in self.logs))
        self.assertTrue(self.engine.workspace.exists())

    def test_invalid_menuconfig_file_stops_before_review_and_compilation(self):
        self.engine.options = replace(self.options, menuconfig=True)
        self.engine.review_config = Mock(return_value=True)

        def invalid_config(runner, tree, workspace):
            (tree / ".config").write_text('not a kernel configuration\n')

        with patch("pimpmykernel.build.menuconfig_terminal", return_value="/test/xterm"), \
                patch("pimpmykernel.build.run_menuconfig", side_effect=invalid_config):
            with self.assertRaisesRegex(KernelError, "Konfiguration ist ungültig"):
                self.engine.run()
        self.engine.review_config.assert_not_called()
        self.assertFalse(any(line.startswith("$ make -j") for line in self.logs))
        self.assertTrue(self.engine.workspace.exists())

    def test_bad_signature_retains_workspace_and_never_runs_make(self):
        original_download = self.engine.network.download

        def corrupt_download(url, path, progress, **kwargs):
            original_download(url, path, progress, **kwargs)
            if not url.endswith(".sign"):
                path.write_bytes(lzma.compress(self.tar_path.read_bytes() + b"tampered"))

        self.engine.network.download = corrupt_download
        with self.assertRaisesRegex(KernelError, "OpenPGP"):
            self.engine.run()
        self.assertTrue(self.engine.workspace.exists())
        self.assertFalse(any(line.startswith("$ make") for line in self.logs))
        self.assertEqual(list(self.output.iterdir()), [])

    def test_export_failure_retains_workspace_and_no_incomplete_output(self):
        self.engine.export = Mock(side_effect=KernelError("export-disk-error"))
        with self.assertRaisesRegex(KernelError, "export-disk-error"):
            self.engine.run()
        self.assertTrue(self.engine.workspace.exists())
        self.assertTrue(list((self.engine.workspace / "compile").glob("*.deb")))
        self.assertEqual(list(self.output.iterdir()), [])

    def test_cancel_keeps_build_for_diagnosis(self):
        original_configure = self.engine.configure
        temporary_files = []

        def configure_and_cancel(tree):
            original_configure(tree)
            _, path = self.engine.runner.run([
                sys.executable, "-c",
                "import tempfile; f=tempfile.NamedTemporaryFile(delete=False); f.close(); print(f.name)"])
            temporary_files.append(Path(path))
            self.engine.cancel.set()

        self.engine.configure = configure_and_cancel
        with self.assertRaises(Cancelled):
            self.engine.run()
        self.assertTrue(self.engine.workspace.exists())
        self.assertFalse(any(line.startswith("$ make -j") for line in self.logs))
        self.assertEqual(temporary_files[0].parent, self.engine.workspace / "tmp")
        self.assertTrue(temporary_files[0].exists())
        cleanup_workspace(self.engine.workspace, self.engine.work_root)
        self.assertFalse(self.engine.workspace.exists())
        self.assertFalse(temporary_files[0].exists())
        self.assertTrue(self.engine.log_path.is_file())

    def test_cancel_during_export_removes_incomplete_package_directory(self):
        def cancel_export(path, cancel=None):
            if path.parent.name.startswith(".pmk-export-"):
                self.engine.cancel.set()
            return sha256(path, cancel)

        with patch("pimpmykernel.build.sha256", side_effect=cancel_export):
            with self.assertRaises(Cancelled):
                self.engine.run()
        self.assertTrue(self.engine.workspace.exists())
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertTrue(self.engine.log_path.is_file())

    def test_install_checks_manifest_and_blocks_secure_boot(self):
        result = self.engine.run()
        runner = Mock(spec=CommandRunner)
        with patch("pimpmykernel.build.secure_boot_state", return_value="on"):
            with self.assertRaisesRegex(KernelError, "Secure Boot"):
                install_packages(result, runner)
        runner.run.assert_not_called()
        with patch("pimpmykernel.build.secure_boot_state", return_value="off"):
            saved_packages = {path: sha256(path) for path in result.packages}
            install_packages(result, runner)
            runner.run.assert_called_once()
            self.assertEqual({path: sha256(path) for path in result.packages}, saved_packages)
            self.assertTrue((result.directory / "manifest.json").exists())
            runner.run.reset_mock()
            result.packages[0].write_bytes(b"modified")
            with self.assertRaisesRegex(KernelError, "verändert"):
                install_packages(result, runner)
        runner.run.assert_not_called()

    def test_cleanup_disabled_keeps_sources_but_exports_packages(self):
        self.engine.options = BuildOptions(release(), self.config, self.output, 1, 1, cleanup=False)
        result = self.engine.run()
        self.assertEqual(result.workspace, self.engine.workspace)
        self.assertTrue(result.workspace.exists())
        self.assertTrue(all(p.is_file() for p in result.packages))

    def test_dependency_lists_match_between_ui_and_privileged_helper(self):
        self.assertEqual(BUILD_PACKAGES, PACKAGES)


if __name__ == "__main__":
    unittest.main()
