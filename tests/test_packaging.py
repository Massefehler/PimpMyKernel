"""Exercise the real .deb lifecycle in an isolated, unprivileged directory."""

import os
import py_compile
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PYTHON = "/usr/bin/python3"
PRIVATE_LIBRARY = "/usr/lib/pimpmykernel"


@unittest.skipUnless(all(shutil.which(tool) for tool in ("dpkg-deb", "py3clean", "py3compile")),
                     "Debian packaging tools required")
class DebianPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.package_tmp = tempfile.TemporaryDirectory(prefix="pmk-package-")
        cls.addClassCleanup(cls.package_tmp.cleanup)
        output = Path(cls.package_tmp.name)
        subprocess.run([PYTHON, str(ROOT / "packaging/build_deb.py"), "--output", str(output)],
                       check=True, capture_output=True, text=True)
        cls.archive, = output.glob("*.deb")
        cls.staging = output / "extracted"
        subprocess.run(["dpkg-deb", "--raw-extract", str(cls.archive), str(cls.staging)],
                       check=True, capture_output=True, text=True)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pmk-installed app-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.library = self.root / "private library"
        shutil.copytree(self.staging / PRIVATE_LIBRARY.lstrip("/"), self.library)
        self.module = self.library / "pimpmykernel/__init__.py"
        self.launcher = self.root / "pimpmykernel"
        self.launcher.write_text((self.staging / "usr/bin/pimpmykernel").read_text().replace(
            PRIVATE_LIBRARY, str(self.library)))

    def run_launcher(self, *, optimized=False):
        command = [PYTHON, "-I"]
        if optimized:
            command.append("-O")
        result = subprocess.run([*command, str(self.launcher), "--version"],
                                check=True, capture_output=True, text=True)
        return result.stdout.strip()

    def run_script(self, name, *arguments):
        # Only relocate the private library; execute the shipped script with the
        # real Debian tools, without touching /usr or the host's package database.
        script = self.root / name
        script.write_text((self.staging / "DEBIAN" / name).read_text().replace(
            PRIVATE_LIBRARY, str(self.library)))
        subprocess.run(["/bin/sh", str(script), *arguments],
                       check=True, capture_output=True, text=True)

    def test_archive_version_matches_configured_launcher_and_project(self):
        metadata = subprocess.run(["dpkg-deb", "--field", str(self.archive), "Version"],
                                  check=True, capture_output=True, text=True).stdout.strip()
        self.assertEqual(metadata, tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"])
        self.run_script("postinst", "configure")
        self.assertEqual(self.run_launcher(), f"PimpMyKernel {metadata}")
        self.assertTrue(list(self.library.rglob("*.pyc")))
        for name in ("postinst", "prerm"):
            with self.subTest(script=name):
                self.assertEqual((self.staging / "DEBIAN" / name).stat().st_mode & 0o777, 0o755)

    def test_upgrade_refreshes_cache_despite_identical_source_size_and_timestamp(self):
        old_source = '__version__ = "0.1.8"\n'
        new_source = '__version__ = "0.1.9"\n'
        self.assertEqual(len(old_source), len(new_source))
        self.module.write_text(old_source)
        epoch = 1791158400
        os.utime(self.module, (epoch, epoch))
        for optimization in (0, 1):
            py_compile.compile(str(self.module), doraise=True, optimize=optimization,
                               invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)
        self.module.write_text(new_source)
        os.utime(self.module, (epoch, epoch))
        for optimized in (False, True):
            with self.subTest(before_upgrade=True, optimized=optimized):
                self.assertEqual(self.run_launcher(optimized=optimized), "PimpMyKernel 0.1.8")

        self.run_script("postinst", "configure", "0.1.8")

        for optimized in (False, True):
            with self.subTest(after_upgrade=True, optimized=optimized):
                self.assertEqual(self.run_launcher(optimized=optimized), "PimpMyKernel 0.1.9")

    def test_removal_and_upgrade_clean_program_bytecode_and_preserve_other_files(self):
        export = self.root / "exports/kernel.deb"
        export.parent.mkdir()
        export.write_bytes(b"exported kernel package")
        source = self.module.read_bytes()
        for action in ("remove", "upgrade", "deconfigure"):
            with self.subTest(action=action):
                self.run_script("postinst", "configure")
                self.assertTrue(list(self.library.rglob("*.pyc")))
                self.run_script("prerm", action)
                self.assertEqual(list(self.library.rglob("*.pyc")), [])
                self.assertEqual(self.module.read_bytes(), source)
                self.assertEqual(export.read_bytes(), b"exported kernel package")

    def test_aborted_lifecycle_does_not_compile_or_clean(self):
        self.run_script("postinst", "abort-upgrade")
        self.assertEqual(list(self.library.rglob("*.pyc")), [])
        self.run_script("postinst", "configure")
        caches = {path: path.read_bytes() for path in self.library.rglob("*.pyc")}
        self.run_script("prerm", "failed-upgrade")
        self.assertEqual({path: path.read_bytes() for path in self.library.rglob("*.pyc")}, caches)
