"""The license texts an APK must carry, checked on APKs made here from the repository's own texts."""
import io
from pathlib import Path
import tempfile
import unittest
import zipfile

from scripts import android_release

PROJECT = Path(__file__).resolve().parents[1]
LICENSES = PROJECT / 'packaging/licenses'


def bundle(files):
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as inner:
        for name, text in files.items():
            inner.writestr(name, text)
    return data.getvalue()


class ApkLicenseTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.apk = Path(work.name) / 'app.apk'
        self.assets = {'LICENSE': (PROJECT / 'LICENSE').read_bytes(),
                       'THIRD-PARTY-NOTICES.md': (PROJECT / 'THIRD-PARTY-NOTICES.md').read_bytes()}
        self.assets |= {f'other/{name}.txt': (LICENSES / f'{name}.txt').read_bytes() for name in android_release.LICENSE_TEXTS}
        self.assets |= {path.relative_to(LICENSES / 'android').as_posix(): path.read_bytes()
                        for path in (LICENSES / 'android').rglob('*') if path.is_file()}
        self.packages = {'requirements-common': {'httpx-0.28.1.dist-info/METADATA': 'Name: httpx',
                                                 'httpx-0.28.1.dist-info/licenses/LICENSE.md': self.assets['httpx/LICENSE.md'],
                                                 'httpx/__init__.py': ''},
                         'bootstrap': {'java/_vendor/pyelftools-0.26.dist-info/LICENSE.txt': self.assets['pyelftools/LICENSE.txt'],
                                       'setuptools-68.2.2.dist-info/LICENSE': self.assets['setuptools/LICENSE']}}

    def problems(self):
        with zipfile.ZipFile(self.apk, 'w') as archive:
            archive.writestr('classes.dex', b'dex')
            for path, data in self.assets.items():
                archive.writestr('assets/licenses/' + path, data)
            for name, files in self.packages.items():
                archive.writestr(f'assets/chaquopy/{name}.imy', bundle(files))
        return android_release.license_problems(self.apk, PROJECT)

    def test_an_apk_with_every_text_of_the_repository_passes(self):
        self.assertEqual(self.problems(), [])
        # Apache-2.0 for AndroidX, Compose and Kotlin; Chaquopy's and Python's own; never the desktop's Qt texts.
        self.assertLessEqual({'Apache-2.0', 'chaquopy', 'python-3.13'}, set(android_release.LICENSE_TEXTS))
        self.assertFalse({'LGPL-3.0', 'GPL-3.0', 'python-3.14'} & set(android_release.LICENSE_TEXTS))

    def test_a_missing_changed_or_strange_text_is_named(self):
        del self.assets['other/openssl.txt']
        self.assets['LICENSE'] = b'MIT License\n\nCopyright (c) 2026 Comic Archiver contributors\n'
        self.assets['other/GPL-3.0.txt'] = (LICENSES / 'GPL-3.0.txt').read_bytes()
        self.assertEqual(self.problems(), [
            "The license text LICENSE in the APK differs from the repository's.",
            'The APK lacks the license text other/openssl.txt.',
            'The APK carries other/GPL-3.0.txt, which is no license text of the repository.'])

    def test_a_python_package_without_its_own_license_beside_it_is_named(self):
        self.packages['requirements-common']['anyio-4.20.0.dist-info/licenses/LICENSE'] = b'a new license'
        self.packages['requirements-common']['newpkg-1.0.dist-info/LICENSE.txt'] = b'MIT'
        self.assertEqual(self.problems(), [
            "anyio's own LICENSE is not the one in packaging/licenses/android.",
            'The Python package newpkg in the APK has no license text newpkg/LICENSE.txt beside it.'])

    def test_the_gradle_build_packs_the_same_texts_as_are_checked(self):
        source = (PROJECT / 'android/app/build.gradle.kts').read_text('utf-8')
        packed = source[source.index('val syncLicenses'):source.index('android {')]
        for name in android_release.LICENSE_TEXTS:
            self.assertIn(f'"{name}"', packed)
        self.assertIn('"packaging/licenses/android"', packed)


if __name__ == '__main__':
    unittest.main()
