import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from support import GermanTestCase
from pathlib import Path
from unittest.mock import patch

from pimpmykernel.menuconfig import menuconfig_terminal, run_menuconfig
from pimpmykernel.models import Cancelled, KernelError
from pimpmykernel.runner import CommandRunner


TERMINAL = r'''
import errno
import os
import pty
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

mode = Path('terminal-mode').read_text()
if mode == 'startup-failure':
    print('fixture terminal could not open display', flush=True)
    sys.exit(1)
command = sys.argv[sys.argv.index('-e') + 1:]
assert 'KCONFIG_CONFIG' not in os.environ and 'MAKEFLAGS' not in os.environ
master, slave = pty.openpty()
child = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave,
                         start_new_session=True, env={**os.environ, 'TERM': 'xterm-256color'})
os.close(slave)
pending = b''
while child.poll() is None:
    if select.select([master], [], [], .05)[0]:
        try:
            chunk = os.read(master, 65536)
        except OSError as exc:
            if exc.errno != errno.EIO:
                raise
            chunk = b''
        if chunk:
            os.write(sys.stdout.fileno(), chunk)
            pending += chunk
            if b'INPUT>' in pending:
                if mode == 'close':
                    os.killpg(child.pid, signal.SIGKILL)
                else:
                    os.write(master, b'y\n')
                pending = b''
child.wait()
os.close(master)
# A terminal launcher returning 0 must not mask make failures or a closed window.
sys.exit(0)
'''

MENU = r'''
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
assert sys.stdin.isatty() and sys.stdout.isatty()
assert os.environ.get('TERM') == 'xterm-256color'
assert 'KCONFIG_CONFIG' not in os.environ and 'invalid' not in os.environ.get('MAKEFLAGS', '')
assert Path(os.environ['TMPDIR']).parent == Path.cwd().parent
mode = Path('terminal-mode').read_text()
if mode == 'failure':
    sys.exit(7)
if mode == 'hang':
    # A child in the terminal group ignores TERM; cancellation must kill it too.
    code = 'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)'
    child = subprocess.Popen([sys.executable, '-c', code])
    time.sleep(.15)
    Path('running.json').write_text(json.dumps({'pid': os.getpid(), 'child': child.pid, 'pgid': os.getpgrp()}))
    time.sleep(60)
else:
    print('INPUT>', flush=True)
    answer = input()
    assert answer == 'y'
    with Path('.config').open('a') as config:
        config.write('CONFIG_MENUCONFIG_TEST=y\n')
'''


@unittest.skipUnless(shutil.which('make'), 'make required')
class MenuconfigTests(GermanTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='pmk-menuconfig-test-')
        self.root = Path(self.tmp.name) / 'job with spaces'
        self.root.mkdir()
        self.tree = self.root / 'linux-test'
        self.tree.mkdir()
        self.temp = self.root / 'tmp'
        self.temp.mkdir()
        (self.tree / '.config').write_text('CONFIG_TEST=y\n')
        (self.tree / 'terminal-mode').write_text('success')
        (self.tree / 'Makefile').write_text('menuconfig:\n\t@' + sys.executable + ' menu.py\n')
        (self.tree / 'menu.py').write_text(MENU)
        self.terminal = self.root / 'terminal fixture'
        self.terminal.write_text('#!' + sys.executable + '\n' + TERMINAL)
        self.terminal.chmod(0o755)
        self.cancel = threading.Event()
        self.logs = []
        self.runner = CommandRunner(self.cancel, self.logs.append, temp_dir=self.temp)
        self.patch = patch('pimpmykernel.menuconfig.menuconfig_terminal', return_value=str(self.terminal))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def run_menu(self, mode='success'):
        (self.tree / 'terminal-mode').write_text(mode)
        with patch.dict(os.environ, {'KCONFIG_CONFIG': '/tmp/should-never-be-used', 'MAKEFLAGS': '-f invalid'}):
            run_menuconfig(self.runner, self.tree, self.root)

    def test_real_tty_and_clean_environment_allow_saved_configuration(self):
        self.run_menu()
        self.assertIn('CONFIG_MENUCONFIG_TEST=y', (self.tree / '.config').read_text())
        status = json.loads((self.root / 'menuconfig/status.json').read_text())
        self.assertEqual(status['phase'], 'finished')
        self.assertEqual(status['returncode'], 0)
        self.assertTrue(any('make menuconfig beendet' in line for line in self.logs))

    def test_missing_terminal_fails_before_creating_session(self):
        with patch('pimpmykernel.menuconfig.shutil.which', return_value=None):
            with self.assertRaisesRegex(KernelError, 'fehlt xterm'):
                menuconfig_terminal()
        self.assertFalse((self.root / 'menuconfig').exists())

    def test_terminal_startup_failure_reports_diagnostics(self):
        with self.assertRaisesRegex(KernelError, 'could not open display'):
            self.run_menu('startup-failure')
        self.assertNotIn('CONFIG_MENUCONFIG_TEST', (self.tree / '.config').read_text())

    def test_make_failure_is_not_masked_by_terminal_exit_status(self):
        with self.assertRaisesRegex(KernelError, 'Fehler 2'):
            self.run_menu('failure')

    def test_closing_terminal_prevents_continuing_the_build(self):
        with self.assertRaisesRegex(KernelError, 'vorzeitig geschlossen'):
            self.run_menu('close')
        self.assertNotIn('CONFIG_MENUCONFIG_TEST', (self.tree / '.config').read_text())

    def test_cancellation_waits_for_terminal_group_and_stubborn_child(self):
        errors = []

        def run():
            try:
                self.run_menu('hang')
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=run)
        worker.start()
        path = self.tree / 'running.json'
        deadline = time.monotonic() + 6
        try:
            while not path.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(path.exists(), 'interactive menu did not start')
            process = json.loads(path.read_text())
        finally:
            self.cancel.set()
            worker.join(8)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], Cancelled)
        for pid in (process['pid'], process['child']):
            stat = Path(f'/proc/{pid}/stat')
            if stat.exists():
                try:
                    state = stat.read_text().rsplit(')', 1)[1].split()[0]
                except FileNotFoundError:
                    continue
                self.assertIn(state, ('Z', 'X'), f'{pid} still running after cancellation')

    def test_cancel_before_launch_never_opens_terminal(self):
        self.cancel.set()
        with self.assertRaises(Cancelled):
            self.run_menu()
        self.assertFalse((self.root / 'menuconfig').exists())


if __name__ == '__main__':
    unittest.main()
