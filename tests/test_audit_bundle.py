import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.audit_bundle import audit, dependencies


class BundleAuditTests(unittest.TestCase):
    def test_every_shipped_third_party_distribution_is_pinned_and_the_local_project_is_excluded(self):
        self.assertEqual(dependencies('comic-archive==0.1.5\nPySide6==6.11.2\nhttpx==0.28.1\n'),
                         'PySide6==6.11.2\nhttpx==0.28.1\n')
        for text in ('', 'comic-archive==0.1.5', 'httpx>=0.27', '-e /private/project', 'package @ https://example.test/a.whl'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                dependencies(text)

    def test_a_vulnerability_failed_scanner_or_empty_report_stops_packaging(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); requirements = root / 'frozen.txt'; report = root / 'audit.json'
            requirements.write_text('comic-archive==0.1.5\nhttpx==0.28.1\n')
            for vulnerable in (False, True, None):
                def scanner(args, **kwargs):
                    selected = Path(args[-1]).read_text()
                    self.assertEqual(selected, 'httpx==0.28.1\n')
                    self.assertTrue(kwargs['check'])
                    value = {'dependencies': [] if vulnerable is None else [
                        {'name':'httpx', 'version':'0.28.1', 'vulns':[{'id':'synthetic'}] if vulnerable else []}]}
                    report.write_text(json.dumps(value))
                with patch('scripts.audit_bundle.subprocess.run', side_effect=scanner):
                    if vulnerable is False:
                        audit(requirements, report)
                    else:
                        with self.assertRaises(RuntimeError):
                            audit(requirements, report)
            with patch('scripts.audit_bundle.subprocess.run', side_effect=subprocess.CalledProcessError(1, ['audit'])):
                with self.assertRaises(subprocess.CalledProcessError):
                    audit(requirements, report)
