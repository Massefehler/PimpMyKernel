import io
import json
import os
import signal
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from support import GermanTestCase
from pathlib import Path

from pimpmykernel.build import cleanup_workspace, create_workspace, extract_sources
from pimpmykernel.models import Cancelled, KernelError
from pimpmykernel.privileged import read_config, validate_packages
from pimpmykernel.runner import CommandRunner


class RunnerTests(GermanTestCase):
    def test_child_temporary_files_use_the_private_job_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "job-tmp"
            directory.mkdir()
            runner = CommandRunner(threading.Event(), lambda _: None, temp_dir=directory)
            script = ("import json,os,tempfile; input(); "
                      "f=tempfile.NamedTemporaryFile(delete=False); f.close(); "
                      "print(json.dumps({'file':f.name,'env':[os.environ[k] for k in ('TMPDIR','TMP','TEMP')]}))")
            _, captured = runner.run([sys.executable, "-c", script], defaults=True)
            details = json.loads(captured)
            self.assertEqual(Path(details["file"]).parent, directory)
            self.assertEqual(details["env"], [str(directory)] * 3)
            self.assertEqual(list(directory.iterdir()), [Path(details["file"])])

    def test_live_output_and_oldconfig_default_answers(self):
        logs = []
        runner = CommandRunner(threading.Event(), logs.append)
        code, captured = runner.run([sys.executable, "-c", "import sys; print('first'); print(repr(input())); print('last')"], defaults=True)
        self.assertEqual(code, 0)
        self.assertIn("''", captured)
        self.assertIn("first", logs)
        self.assertIn("last", logs)

    def test_failure_is_reported_with_output(self):
        runner = CommandRunner(threading.Event(), lambda _: None)
        with self.assertRaisesRegex(KernelError, "meaningful-failure"):
            runner.run([sys.executable, "-c", "import sys; print('meaningful-failure'); sys.exit(7)"])

    def test_large_config_capture_is_complete_and_not_logged(self):
        logs = []
        runner = CommandRunner(threading.Event(), logs.append)
        _, output = runner.run([sys.executable, "-c", "for i in range(12000): print('CONFIG_ITEM_%s=y' % i)"],
                               capture_lines=None, log_output=False, echo=False)
        self.assertEqual(len(output.splitlines()), 12000)
        self.assertEqual(logs, [])

    def test_timeout_terminates_process(self):
        runner = CommandRunner(threading.Event(), lambda _: None)
        start = time.monotonic()
        with self.assertRaisesRegex(KernelError, "Zeitüberschreitung"):
            runner.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.2)
        self.assertLess(time.monotonic() - start, 4)

    def test_cancel_kills_even_a_sigterm_ignoring_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "child.pid"
            event = threading.Event()
            child = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)"
            script = ("import subprocess,sys,time,pathlib; "
                      f"p=subprocess.Popen([sys.executable,'-c',{child!r}]); "
                      f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid)); time.sleep(30)")
            errors = []

            def run():
                try:
                    CommandRunner(event, lambda _: None).run([sys.executable, "-c", script])
                except Exception as exc:
                    errors.append(exc)

            worker = threading.Thread(target=run)
            worker.start()
            deadline = time.monotonic() + 5
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(pid_file.exists())
            pid = int(pid_file.read_text())
            time.sleep(0.15)
            event.set()
            worker.join(6)
            self.assertFalse(worker.is_alive())
            self.assertIsInstance(errors[0], Cancelled)
            # A killed child can briefly remain a zombie until the OS reaps it.
            status = Path(f"/proc/{pid}/status")
            try:
                details = status.read_text()
            except (FileNotFoundError, ProcessLookupError):
                pass  # Reaping can also happen between opening and reading /proc.
            else:
                self.assertIn("Z (zombie)", details)


class FilesystemSafetyTests(GermanTestCase):
    def test_cleanup_is_limited_to_marked_private_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "jobs"
            job = create_workspace(root)
            (job / "file").write_text("temporary")
            outside = Path(tmp) / "keep"
            outside.mkdir()
            (outside / "file").write_text("important")
            with self.assertRaises(KernelError):
                cleanup_workspace(outside, root)
            with self.assertRaises(KernelError):
                cleanup_workspace(root, root)
            cleanup_workspace(job, root)
            self.assertFalse(job.exists())
            self.assertEqual((outside / "file").read_text(), "important")

    def test_cleanup_rejects_symlink_even_with_valid_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "jobs"
            job = create_workspace(root)
            link = root / "pmk-link"
            link.symlink_to(job)
            with self.assertRaises(KernelError):
                cleanup_workspace(link, root)
            self.assertTrue(job.exists())

    def test_archive_rejects_traversal_and_external_links(self):
        cases = [("linux-7.2.9/../../escape", None), ("linux-7.2.9/link", "/etc/passwd"),
                 ("other-root/Makefile", None)]
        for name, link in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                archive = root / "source.tar"
                with tarfile.open(archive, "w") as tar:
                    member = tarfile.TarInfo(name)
                    if link:
                        member.type = tarfile.SYMTYPE
                        member.linkname = link
                        tar.addfile(member)
                    else:
                        member.size = 3
                        tar.addfile(member, io.BytesIO(b"bad"))
                workspace = root / "job"
                workspace.mkdir()
                with self.assertRaises(KernelError):
                    extract_sources(archive, workspace, "7.2.9", threading.Event(), lambda _: None)
                self.assertFalse((root / "escape").exists())

    def test_privileged_helper_rejects_arbitrary_config_and_packages(self):
        with self.assertRaises(ValueError):
            read_config("/etc/shadow")
        with self.assertRaises(ValueError):
            read_config("/boot/../etc/shadow")
        with self.assertRaises(ValueError):
            validate_packages(["--help"])
        with self.assertRaises(ValueError):
            validate_packages([])


if __name__ == "__main__":
    unittest.main()
