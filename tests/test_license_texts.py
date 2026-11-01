from pathlib import Path
import tempfile
import unittest

from comic_archive import license_texts


class LicenseTextsTests(unittest.TestCase):
    def test_the_apps_licenses_folder_is_found_beside_its_python_and_read_in_order(self):
        with tempfile.TemporaryDirectory() as work:
            app = Path(work)
            (app / 'python').mkdir()
            self.assertIsNone(license_texts.folder(app / 'python'))
            self.assertEqual(license_texts.texts(app / 'python'),
                             [('Comic Archiver (MIT license)', 'LICENSE'), ('Third-party notices', 'THIRD-PARTY-NOTICES.md')])
            for name in ('comic_archive-0.2.5.dist-info/licenses/LICENSE', 'python3.14/LICENSE.txt', 'httpx-0.28.1.dist-info/licenses/LICENSE.md', 'other/python-3.14.txt',
                         'other/LGPL-3.0.txt'):
                (app / 'licenses' / name).parent.mkdir(parents=True, exist_ok=True)
                (app / 'licenses' / name).write_text(name)
            found = license_texts.texts(app / 'python')
            # Qt's and Python's licenses first, where people look for them; then each package's own.
            self.assertEqual([title for title, _ in found],
                             ['Comic Archiver (MIT license)', 'Third-party notices', 'LGPL-3.0', 'python-3.14',
                              'httpx-0.28.1: LICENSE.md', 'python3.14: LICENSE.txt'])
            self.assertEqual([license_texts.read(path) for _, path in found[2:]],
                             ['other/LGPL-3.0.txt', 'other/python-3.14.txt', 'httpx-0.28.1.dist-info/licenses/LICENSE.md',
                              'python3.14/LICENSE.txt'])
            self.assertIsNone(license_texts.read(str(app / 'licenses/other/gone.txt')))

    def test_the_apps_own_license_names_its_author_and_the_notices_come_with_it(self):
        self.assertIn('Copyright (c) 2026 Ben Crute', license_texts.read('LICENSE'))
        self.assertIn('Qt and PySide6 (LGPL-3.0)', license_texts.read('THIRD-PARTY-NOTICES.md'))
        self.assertRegex(license_texts.version(), r'^\d+\.\d+\.\d+|unknown$')


if __name__ == '__main__':
    unittest.main()
