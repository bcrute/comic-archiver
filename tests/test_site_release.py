"""The website and a release's notes are made from the release itself: nothing is updated by hand."""
import contextlib
import io
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
import site_release  # noqa: E402

NEWEST = max(site_release.changes(), key=site_release.order)


def files(version):
    return [f'ComicArchiver-{version}-windows-x86_64-setup.exe', f'ComicArchiver-{version}-windows-x86_64.zip',
            f'ComicArchiver-{version}-linux-x86_64.tar.gz', f'ComicArchiver-{version}-macos-x86_64.zip',
            'SHA256SUMS.txt', 'release-evidence.json']


def release(version, names=None, published='2026-11-20T10:00:00Z', **more):
    return dict({'tag_name': f'v{version}', 'draft': False, 'prerelease': False, 'published_at': published,
                 'assets': [{'name': name} for name in (files(version) if names is None else names)]}, **more)


def beta(version='0.5.0-beta1', published='2026-11-21T10:00:00Z'):
    return release(version, [f'ComicArchiver-{version}-android-universal.apk', 'SHA256SUMS.txt'], published,
                   prerelease=True)


class SiteReleaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.out = self.folder / 'site'

    def run_site(self, latest, releases=None):
        paths = []
        for name, value in (('latest.json', latest), ('releases.json', releases)):
            if value is not None:
                (self.folder / name).write_text(json.dumps(value))
                paths += ['--' + name.split('.')[0].replace('latest', 'release'), str(self.folder / name)]
        error = io.StringIO()
        with contextlib.redirect_stderr(error), contextlib.redirect_stdout(io.StringIO()):
            code = site_release.main(['site', str(self.out), *paths])
        return code, error.getvalue()

    def page(self, name):
        return (self.out / name).read_text(encoding='utf-8')

    def test_the_published_site_takes_its_version_date_and_links_from_the_release(self):
        code, error = self.run_site(release(NEWEST), [beta('0.5.0-beta0', '2026-11-01T00:00:00Z'), release(NEWEST),
                                                      beta(), dict(beta('0.5.0-beta2', '2026-11-22T10:00:00Z'),
                                                                   draft=True)])
        self.assertEqual((code, error), (0, ''))
        download = self.page('download.html')
        self.assertIn(f'Version {NEWEST}, released 20 November 2026.', download)
        links = re.findall(r'releases/download/v([^/]+)/([^"]+)"', download)
        self.assertEqual({tag for tag, _ in links}, {NEWEST})
        self.assertEqual({name for _, name in links} - set(files(NEWEST)), set())
        self.assertIn(f'>Download {NEWEST}</a>', self.page('index.html'))
        # The newest Android release, a prerelease, has its own page.
        android = self.page('android.html')
        self.assertIn('Version 0.5.0-beta1', android)
        self.assertIn('/v0.5.0-beta1/ComicArchiver-0.5.0-beta1-android-universal.apk"', android)
        for page in self.out.glob('*.html'):
            self.assertIsNone(re.search(r'\{[a-z_]+\}', page.read_text(encoding='utf-8')), page.name)
        # The notes files are made into the Changelog and not published themselves.
        self.assertFalse((self.out / 'changes').exists())
        self.assertTrue((self.out / 'style.css').is_file() and (self.out / 'images/dashboard-light.png').is_file())

    def test_the_changelog_lists_every_release_newest_first_with_the_new_one_dated_by_its_release(self):
        self.assertEqual(self.run_site(release(NEWEST))[0], 0)
        changelog = self.page('changelog.html')
        shown = re.findall(r'<article class="release[^"]*" id="v([^"]+)">', changelog)
        self.assertEqual(shown, sorted(site_release.changes(), key=site_release.order, reverse=True))
        self.assertEqual(shown[-1], '0.1.0')
        first = changelog[changelog.index('<article'):changelog.index('</article>')]
        self.assertIn(f'<h2>{NEWEST} <span class="tag">Latest</span></h2>', first)
        self.assertIn('<p class="date">20 November 2026</p>', first)
        self.assertEqual(changelog.count('Latest</span>'), 1)
        self.assertIn('<article class="release" id="v0.4.2">\n  <h2>0.4.2</h2>\n  <p class="date">11 November 2026</p>',
                      changelog)
        self.assertIn('<li><b>One odd series no longer stops a Tapas download.</b> When Tapas promised', changelog)

    def test_a_link_to_a_file_the_release_does_not_have_stops_the_site(self):
        names = [name for name in files(NEWEST) if not name.endswith('macos-x86_64.zip')]
        code, error = self.run_site(release(NEWEST, names))
        self.assertEqual(code, 1)
        self.assertIn(f'download.html links to ComicArchiver-{NEWEST}-macos-x86_64.zip, which release v{NEWEST} does not',
                      error)
        code, error = self.run_site(release(NEWEST), [beta(), release('0.5.0-beta1', ['SHA256SUMS.txt'],
                                                                      '2026-11-19T00:00:00Z', prerelease=True)])
        self.assertEqual(code, 0, error)

    def test_without_an_android_release_there_is_no_android_page(self):
        self.assertEqual(self.run_site(release(NEWEST), [release(NEWEST)])[0], 0)
        self.assertFalse((self.out / 'android.html').exists())
        for page in self.out.glob('*.html'):
            self.assertNotIn('android.html', page.read_text(encoding='utf-8'), page.name)

    def test_only_a_published_full_release_makes_the_site(self):
        for refused in (release(NEWEST, draft=True), release(NEWEST, prerelease=True)):
            code, error = self.run_site(refused)
            self.assertEqual(code, 1)
            self.assertIn('is not a published full release', error)
        # A version with no notes file cannot be published as the newest.
        code, error = self.run_site(release('9.9.9'))
        self.assertEqual(code, 1)
        self.assertIn('website/changes/9.9.9.md is missing', error)

    def test_a_preview_needs_nothing_from_github(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(site_release.main(['site', str(self.out), '--version', NEWEST]), 0)
        self.assertIn(f'Version {NEWEST}, released ', self.page('download.html'))
        self.assertFalse((self.out / 'android.html').exists())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(site_release.main(['site', str(self.out), '--version', NEWEST,
                                                '--android-version', '0.5.0-beta1']), 0)
        self.assertIn('Version 0.5.0-beta1', self.page('android.html'))

    def test_every_notes_file_but_the_newest_is_dated_and_each_says_something(self):
        entries = site_release.changes()
        self.assertEqual([version for version, (date, _) in entries.items() if date is None and version != NEWEST], [])
        for version, (_, text) in entries.items():
            self.assertTrue(text.startswith('- '), version)
        # An older release without its date stops the site rather than showing none.
        folder = self.folder / 'web'
        (folder / 'changes').mkdir(parents=True)
        (folder / 'page.html').write_text('{version} {released}')
        (folder / 'changes/1.0.0.md').write_text('- Old.\n')
        (folder / 'changes/1.1.0.md').write_text('- New.\n')
        with self.assertRaisesRegex(site_release.SiteError, '1.0.0.md needs its date'):
            site_release.build(self.out, '1.1.0', '2026-11-20', folder=folder)
        (folder / 'changes/1.0.0.md').write_text('date: 2026-01-02\n\n- Old.\n')
        site_release.build(self.out, '1.1.0', '2026-11-20', folder=folder)
        self.assertEqual(self.page('page.html'), '1.1.0 20 November 2026')
        # A placeholder the generator does not know stops the site rather than showing braces.
        (folder / 'page.html').write_text('{version} {relased}')
        with self.assertRaisesRegex(site_release.SiteError, r'page.html still has \{relased\}'):
            site_release.build(self.out, '1.1.0', '2026-11-20', folder=folder)
        (folder / 'page.html').write_text('{version}')
        (folder / 'changes/notes.md').write_text('- Misnamed.\n')
        with self.assertRaisesRegex(site_release.SiteError, 'named for its version'):
            site_release.build(self.out, '1.1.0', '2026-11-20', folder=folder)

    def test_the_release_notes_carry_the_same_changes_as_the_changelog(self):
        text = site_release.notes(NEWEST)
        self.assertIn(f'## Changes in {NEWEST}\n\n' + site_release.changes()[NEWEST][1] + '\n\nEach is self-contained',
                      text)
        self.assertNotIn('{', text)
        self.assertTrue(text.startswith('| Your computer | File |'))
        with self.assertRaisesRegex(site_release.SiteError, '9.9.9.md is missing'):
            site_release.notes('9.9.9')

    def test_the_notes_markdown_becomes_safe_html(self):
        self.assertEqual(site_release.inline('**Bold** and `a **b** <c>` <script>&'),
                         '<b>Bold</b> and <code>a **b** &lt;c&gt;</code> &lt;script&gt;&amp;')
        self.assertEqual(site_release.inline('[the site](https://example.com/a) [not](javascript:x)'),
                         '<a href="https://example.com/a">the site</a> [not](javascript:x)')
        self.assertEqual(site_release.markdown('- One\n  two\n- Three\n\nAfter.'),
                         '  <ul>\n    <li>One two</li>\n    <li>Three</li>\n  </ul>\n  <p>After.</p>')
        with self.assertRaisesRegex(site_release.SiteError, 'must start with'):
            site_release.markdown('- One\nnot indented')
        self.assertEqual(site_release.day('2026-03-01T23:00:00Z'), '1 March 2026')
        self.assertLess(site_release.order('0.9.0'), site_release.order('0.10.0'))


if __name__ == '__main__':
    unittest.main()
