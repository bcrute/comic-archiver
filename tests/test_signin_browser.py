import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


class SignInBrowserTests(unittest.TestCase):
    def test_real_browser_identity_is_fresh_after_login_and_logout_for_every_provider(self):
        root = Path(__file__).resolve().parents[1]
        env = dict(os.environ, QT_QPA_PLATFORM='offscreen')
        env.pop('DISPLAY', None)
        env.pop('WAYLAND_DISPLAY', None)
        done = subprocess.run([sys.executable, str(root / 'scripts/signin_browser_smoke.py')],
                              cwd=root, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        result = json.loads(done.stdout.splitlines()[-1])
        self.assertTrue(result['ok'])
        self.assertEqual(result['providers'], {
            site: {'fresh_guest_login_logout': True, 'identity_requests': 3}
            for site in ('tapas', 'webtoon', 'tappytoon')})
