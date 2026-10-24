"""Bash argv regressions for the staging workers.

The build_payload calls in stage.sh and repair.sh are evaluated with a stub
build_payload that only records its arguments, then the recorded argv is given
to the real payload.sh. The worker parses its options before any preflight
check, so reaching "[1/8] Preflight checks" proves the argv was accepted. No
slot, mount or network operation is performed.
"""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
STANDALONE = Path(os.environ.get('DECKY_STANDALONE_DIR', ROOT / 'bin/standalone')).resolve()
BASH = shutil.which('bash')
WORK = '/home/.decky-nvidia-update/prepare-test'
REPAIR_WORK = '/home/.decky-nvidia-repair.test'
KERNEL = '6.11.11-valve24-1-neptune-611'
DRIVER = '580.105.08-4'
HARNESS = r'''
build_payload() { printf '%s\n' "$@"; }
HERE=$1 WORK=$2 KERNEL=$3 DRIVER=$4 PINS=$5 ROOT=$6
kernels=("$3")
eval "$7"
'''


def strip_ansi(text):
    return re.sub(r'\x1b\[[0-9;]*m', '', text)


def call_line(script):
    lines = [line.strip() for line in (STANDALONE / script).read_text().splitlines()
             if line.strip().startswith('build_payload "$HERE/payload.sh"')]
    assert len(lines) == 1, f'{script} must call build_payload exactly once'
    return lines[0]


@unittest.skipUnless(BASH, 'bash is required')
class WorkerArgv(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.here = STANDALONE.as_posix()
        self.payload = self.here + '/payload.sh'

    def worker_argv(self, script, work, pins='', root=''):
        result = subprocess.run([BASH, '-euo', 'pipefail', '-c', HARNESS, '_', self.here, work, KERNEL,
                                 DRIVER, pins, root, call_line(script)],
                                capture_output=True, text=True, cwd=self.temp.name)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.splitlines()

    def run_payload(self, options, workdir=WORK + '/build'):
        # build_payload runs: payload.sh --workdir <work> <options>
        env = {k: v for k, v in os.environ.items() if k != 'DECKY_NVIDIA_BUILD_NS'}
        result = subprocess.run([BASH, self.payload, '--workdir', workdir, *options],
                                capture_output=True, text=True, cwd=self.temp.name, env=env)
        return result.returncode, strip_ansi(result.stdout), strip_ansi(result.stderr)

    def assert_reaches_preflight(self, options):
        rc, out, err = self.run_payload(options)
        self.assertIn('[1/8] Preflight checks', out, err)
        self.assertNotIn('parameter null or not set', err)
        self.assertNotIn('Unknown option', err)
        # Without root and a private PID namespace the worker stops right after.
        self.assertNotEqual(rc, 0)
        self.assertNotIn('[2/8]', out)

    def assert_rejected(self, options, message):
        rc, out, err = self.run_payload(options)
        self.assertNotEqual(rc, 0)
        self.assertNotIn('Preflight checks', out)
        self.assertIn(message, err)

    def test_arch_install_omits_pins(self):
        argv = self.worker_argv('stage.sh', WORK, pins='')
        # A driver change builds with the running system's pacman settings, in the build overlay only.
        self.assertEqual(argv, [self.payload, WORK + '/build', '--target', WORK + '/root', '--build-etc', '/etc',
                                '--kernel', KERNEL, '--driver', DRIVER, '--force', '-y'])
        self.assert_reaches_preflight(argv[2:])

    def test_custom_build_passes_pin_file(self):
        pins = WORK + '/custom-pins.json'
        argv = self.worker_argv('stage.sh', WORK, pins=pins)
        self.assertEqual(argv, [self.payload, WORK + '/build', '--target', WORK + '/root', '--build-etc', '/etc',
                                '--kernel', KERNEL, '--driver', DRIVER, '--pins', pins, '--force', '-y'])
        self.assert_reaches_preflight(argv[2:])

    def test_pin_file_with_spaces_stays_one_argument(self):
        pins = '/home/.decky-nvidia-update/prepare one/custom-pins.json'
        argv = self.worker_argv('stage.sh', WORK, pins=pins)
        self.assertEqual(argv[argv.index('--pins') + 1], pins)

    def test_repair_passes_saved_pins(self):
        root = REPAIR_WORK + '/root'
        argv = self.worker_argv('repair.sh', REPAIR_WORK, root=root)
        self.assertEqual(argv, [self.payload, REPAIR_WORK + '/build', '--target', root,
                                '--kernel', KERNEL, '--driver', DRIVER,
                                '--pins', self.here + '/installed.json', '--force', '-y'])
        self.assert_reaches_preflight(argv[2:])

    def test_empty_pins_value_is_rejected(self):
        self.assert_rejected(['--target', WORK + '/root', '--kernel', KERNEL, '--driver', DRIVER,
                              '--pins', '', '--force', '-y'], '--pins needs an argument')

    def test_missing_pins_value_is_rejected(self):
        self.assert_rejected(['--target', WORK + '/root', '--kernel', KERNEL, '--driver', DRIVER,
                              '--pins'], '--pins needs an argument')

    def test_unknown_option_is_rejected(self):
        self.assert_rejected(['--target', WORK + '/root', '--kernel', KERNEL, '--driver', DRIVER,
                              '--pins=/tmp/custom-pins.json'], 'Unknown option')


if __name__ == '__main__':
    unittest.main()
