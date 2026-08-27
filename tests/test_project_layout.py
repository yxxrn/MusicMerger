"""The checked-out application must be runnable as a package without pip install."""
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PackageLaunchTests(unittest.TestCase):
    def test_public_and_worker_entry_points_boot_without_loading_models(self):
        for module in ('musicmerger', 'musicmerger.renderer', 'musicmerger.acoustic', 'musicmerger.sync'):
            with self.subTest(module=module):
                result = subprocess.run([sys.executable, '-B', '-m', module, '--help'],
                                        cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('usage:', result.stdout)
