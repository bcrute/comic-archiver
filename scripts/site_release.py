"""Make the website, and a release's notes, from the release itself.

The pages in website/ name what changes from release to release in braces: {version}, {released}, {changelog}
and {android_version}. Text between <!-- if android --> and <!-- else --> (or <!-- end if -->) is kept only when
there is an Android release; text between <!-- else --> and <!-- end if --> only when there is none. A page that
still names {android_version} after that exists only with an Android release.

Each release's changes are written once, in website/changes/<version>.md, and become both the release's notes
on GitHub and its entry in the site's Changelog.

The Roadmap, {roadmap}, comes from website/roadmap.json: what works today, then everything planned. A planned item
names the version it is in once its work is done ("in": "0.5.3"); it shows as coming in the next release until
that version is the one published, and then leaves the Roadmap, since the Changelog says it is done. A planned item
that has begun is "started"; any other is on the wishlist.

    python3 scripts/site_release.py site OUT --release latest.json --releases releases.json
        The site as published: the newest release and the newest Android release, as GitHub's API describes
        them, fill in the pages, and every download link must name a file the release has.
    python3 scripts/site_release.py site OUT [--version 0.5.0] [--android-version 0.5.1-beta1]
        A preview on this computer: the version in pyproject.toml unless one is named, and no Android page
        unless an Android version is named.
    python3 scripts/site_release.py notes 0.5.0
        The release's notes: packaging/release-notes.md with that version and its changes filled in.
"""
import argparse
import datetime
import html
import json
from pathlib import Path
import re
import shutil
import sys
import tomllib

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / 'website'
CHANGES = SITE / 'changes'
NOTES = ROOT / 'packaging' / 'release-notes.md'
VERSION = re.compile(r'\d+\.\d+\.\d+(?:-[a-z]+\d+)?')
ANDROID = '-android-universal.apk'
DOWNLOAD = re.compile(r'https://github\.com/bcrute/comic-archiver/releases/download/v([^/"]+)/([^"#?]+)')
TOKEN = re.compile(r'\{[a-z_]+\}')
ROADMAP = SITE / 'roadmap.json'
WORKS = {'works': ('works', 'Works'), 'beta': ('progress', 'Beta')}
IF_ANDROID = re.compile(r'<!-- if android -->\n?(.*?)(?:<!-- else -->\n?(.*?))?<!-- end if -->\n?', re.S)


class SiteError(Exception):
    pass


def order(version):
    """A version as numbers, for sorting: 0.10.0 after 0.9.0, and 0.5.0-beta1 after 0.4.2 but before 0.5.0."""
    number, _, test = version.partition('-')
    numbers = tuple(int(part) for part in number.split('.'))
    return numbers + ((0, int(re.sub(r'\D', '', test))) if test else (1, 0))


def day(value):
    """'2026-11-11' or a GitHub time as the site says a date: '11 November 2026'."""
    date = datetime.date.fromisoformat(value[:10])
    return f'{date.day} {date:%B} {date.year}'


def inline(text):
    """One line of Markdown as HTML: `code`, **bold** and [links](https://...); everything else as text."""
    out = []
    for index, part in enumerate(re.split(r'(`[^`]+`)', text)):
        if index % 2:
            out.append('<code>' + html.escape(part[1:-1], quote=False) + '</code>')
            continue
        part = html.escape(part, quote=False)
        part = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', part)
        part = re.sub(r'\[([^\]]+)\]\((https://[^)\s"]+)\)', r'<a href="\2">\1</a>', part)
        out.append(part)
    return ''.join(out)


def markdown(text):
    """The Markdown the changes are written in, as HTML: paragraphs, and lists of '- ' items."""
    blocks = []
    for block in re.split(r'\n\s*\n', text.strip()):
        lines = block.splitlines()
        if lines[0].startswith('- '):
            items = []
            for line in lines:
                if line.startswith('- '):
                    items.append(line[2:].strip())
                elif items and line.startswith(' '):
                    items[-1] += ' ' + line.strip()
                else:
                    raise SiteError(f'A list line must start with "- " or continue an item: {line!r}')
            blocks.append('  <ul>\n' + ''.join(f'    <li>{inline(item)}</li>\n' for item in items) + '  </ul>')
        else:
            blocks.append('  <p>' + inline(' '.join(line.strip() for line in lines)) + '</p>')
    return '\n'.join(blocks)


def changes(folder=CHANGES):
    """{version: (date or None, Markdown)} from the changes folder. A file may start with 'date: YYYY-MM-DD'."""
    found = {}
    for path in folder.glob('*.md'):
        if not VERSION.fullmatch(path.stem):
            raise SiteError(f'A changes file is named for its version: {path.name}')
        text = path.read_text(encoding='utf-8')
        date = None
        if match := re.match(r'date: (\d{4}-\d{2}-\d{2})\n', text):
            date, text = match[1], text[match.end():]
        if not text.strip():
            raise SiteError(f'{path.name} says nothing')
        found[path.stem] = (date, text.strip())
    return found


def changelog(entries, version, released):
    """Every release up to version, newest first, as the Changelog's articles. version's own date is released."""
    shown = sorted((v for v in entries if order(v) <= order(version)), key=order, reverse=True)
    if not shown or shown[0] != version:
        raise SiteError(f'website/changes/{version}.md is missing')
    articles = []
    for v in shown:
        date, text = entries[v]
        if v == version:
            date = released
        elif date is None:
            raise SiteError(f'website/changes/{v}.md needs its date: line')
        latest = v == version
        mark, tag = (' latest', ' <span class="tag">Latest</span>') if latest else ('', '')
        articles.append(f'<article class="release{mark}" id="v{v}">\n  <h2>{v}{tag}</h2>\n'
                        f'  <p class="date">{day(date)}</p>\n{markdown(text)}\n</article>')
    return '\n'.join(articles)


def _items(rows):
    return '  <ul class="items">\n' + ''.join(
        f'      <li><b>{inline(name)}</b><span>{inline(text)}</span><em class="pill {kind}">{pill}</em></li>\n'
        for name, text, kind, pill in rows) + '    </ul>'


def roadmap(version, path=ROADMAP):
    """The Roadmap's sections for the site of version: works today, being worked on, and the wishlist."""
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise SiteError(f'{path.name} cannot be read: {error}') from None
    if not isinstance(data, dict) or set(data) != {'works', 'planned'} or not all(
            isinstance(data[key], list) for key in data):
        raise SiteError(f'{path.name} has "works" and "planned" lists, and nothing else')

    def item(row, allowed):
        if (not isinstance(row, dict) or not {'name', 'text'} <= set(row) <= allowed
                or not all(isinstance(value, (str, bool)) for value in row.values())
                or not (isinstance(row['name'], str) and row['name'].strip())
                or not (isinstance(row['text'], str) and row['text'].strip())):
            raise SiteError(f'{path.name} has an item that is not a name, a text and {sorted(allowed - {"name", "text"})}: {row!r}')
        return row

    works = []
    for row in data['works']:
        row = item(row, {'name', 'text', 'pill'})
        if row.get('pill') not in WORKS:
            raise SiteError(f'{path.name}: "pill" is one of {sorted(WORKS)} for {row["name"]!r}')
        works.append((row['name'], row['text'], *WORKS[row['pill']]))
    coming, started, wished = [], [], []
    for row in data['planned']:
        row = item(row, {'name', 'text', 'in', 'started'})
        if 'started' in row and row['started'] is not True:
            raise SiteError(f'{path.name}: "started" is only ever true, for {row["name"]!r}')
        if 'in' in row:
            if not isinstance(row['in'], str) or not VERSION.fullmatch(row['in']) or 'started' in row:
                raise SiteError(f'{path.name}: "in" names one version, and no "started", for {row["name"]!r}')
            if order(row['in']) > order(version):
                coming.append((row['name'], row['text'], 'progress', 'Next release'))
        elif row.get('started'):
            started.append((row['name'], row['text'], 'progress', 'Started'))
        else:
            wished.append((row['name'], row['text'], 'wish', 'Not started'))
    if not works:
        raise SiteError(f'{path.name} names what works today')
    working = coming + started
    sections = [('Works today', 'In the current version.', _items(works)),
                ('Being worked on', 'Started, and coming in a future version.',
                 _items(working) if working else
                 '  <p>Nothing right now. Everything finished is in the <a href="changelog.html">Changelog</a>.</p>'),
                ('Wishlist', 'Wanted, but not started and not promised.',
                 _items(wished) if wished else '  <p>Nothing right now.</p>')]
    return '\n'.join(f'<section class="block">\n  <h2>{title}</h2>\n  <p class="sub">{sub}</p>\n{body}\n</section>'
                      for title, sub, body in sections)


def assets(release):
    return {item['name'] for item in release.get('assets', [])}


def android_release(releases):
    """The newest published release with the Android app in it, or None."""
    found = [item for item in releases if not item.get('draft') and any(
        name.endswith(ANDROID) for name in assets(item))]
    return max(found, key=lambda item: item['published_at'], default=None)


def version_manifest(desktop, releases):
    """Only published stable versions; Android may have a different latest release."""
    candidates = [r for r in releases if not r.get('draft') and not r.get('prerelease')
                  and re.fullmatch(r'v\d+\.\d+\.\d+', r.get('tag_name', ''))
                  and any(name.endswith(ANDROID) for name in assets(r))]
    android = max(candidates, key=lambda r: order(r['tag_name'][1:]), default=None)
    return {'schema_version': 1, 'desktop': desktop,
            'android': android['tag_name'][1:] if android else None}


def size_suffix(release_sizes, version, filename):
    """Decimal MB from the selected release asset, omitted when no reliable size is given."""
    size = (release_sizes or {}).get(version, {}).get(filename)
    if type(size) is not int or not 0 < size < 10 ** 12:
        return ''
    amount = f'{size / 1_000_000:.1f}'.removesuffix('.0')
    return f' · {amount} MB'


def build(out, version, released=None, android=None, offered=None, folder=SITE, release_sizes=None):
    """Write the site to out. released is the release's date: for a preview, its changes file's date or today.
    offered is {version: set of file names} when every download link must be checked."""
    entries = changes(folder / 'changes')
    released = released or entries.get(version, (None,))[0] or datetime.date.today().isoformat()
    values = {'{version}': version, '{released}': day(released),
              '{changelog}': changelog(entries, version, released)}
    for token, suffix in (('windows_size', 'windows-x86_64-setup.exe'), ('windows_zip_size', 'windows-x86_64.zip'),
                          ('linux_size', 'linux-x86_64.tar.gz'), ('mac_size', 'macos-x86_64.zip')):
        values['{' + token + '}'] = size_suffix(release_sizes, version, f'ComicArchiver-{version}-{suffix}')
    values['{android_size}'] = size_suffix(release_sizes, android, f'ComicArchiver-{android}-android-universal.apk')
    if any('{roadmap}' in page.read_text(encoding='utf-8') for page in folder.glob('*.html')):
        values['{roadmap}'] = roadmap(version, folder / 'roadmap.json')
    if android:
        values['{android_version}'] = android
    out = Path(out)
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(folder, out, ignore=shutil.ignore_patterns('changes', 'roadmap.json', '.*'))
    for page in sorted(out.glob('*.html')):
        text = IF_ANDROID.sub(lambda found: found[1] if android else found[2] or '', page.read_text(encoding='utf-8'))
        if '<!-- if android' in text or '<!-- else' in text or '<!-- end if' in text:
            raise SiteError(f'{page.name} has an unmatched <!-- if android --> block')
        if '{android_version}' in text and not android:
            page.unlink()  # No Android release yet: no Android page.
            continue
        for token, value in values.items():
            text = text.replace(token, value)
        if left := TOKEN.findall(text):
            raise SiteError(f'{page.name} still has {", ".join(sorted(set(left)))}')
        for tag, name in DOWNLOAD.findall(text):
            if offered is not None and name not in offered.get(tag, set()):
                raise SiteError(f'{page.name} links to {name}, which release v{tag} does not have')
        page.write_text(text, encoding='utf-8')
    return out


def notes(version, template=NOTES, folder=CHANGES):
    """A release's notes for GitHub: the template with its version and its changes."""
    entry = changes(folder).get(version)
    if entry is None:
        raise SiteError(f'website/changes/{version}.md is missing')
    return template.read_text(encoding='utf-8').replace('{changes}', entry[1]).replace('{version}', version)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest='command', required=True)
    site = commands.add_parser('site')
    site.add_argument('out')
    site.add_argument('--release', help="GitHub's description of the newest release (gh api .../releases/latest)")
    site.add_argument('--releases', help="GitHub's list of releases, to find the newest Android one")
    site.add_argument('--version', help='Preview: the version to show')
    site.add_argument('--android-version', help='Preview: the Android version to show')
    given = commands.add_parser('notes')
    given.add_argument('version')
    args = parser.parse_args(argv)
    try:
        if args.command == 'notes':
            sys.stdout.write(notes(args.version))
            return 0
        if args.release:
            release = json.loads(Path(args.release).read_text(encoding='utf-8'))
            if release.get('draft') or release.get('prerelease'):
                raise SiteError(f"{release.get('tag_name')} is not a published full release")
            version = release['tag_name'].removeprefix('v')
            offered = {version: assets(release)}
            release_sizes = {version: {item['name']: item.get('size') for item in release.get('assets', [])}}
            android = None
            published_releases = [release]
            if args.releases:
                published_releases = json.loads(Path(args.releases).read_text(encoding='utf-8'))
                found = android_release(published_releases)
                if found:
                    android = found['tag_name'].removeprefix('v')
                    offered[android] = assets(found)
                    release_sizes[android] = {item['name']: item.get('size') for item in found.get('assets', [])}
            out = build(args.out, version, release['published_at'], android, offered, release_sizes=release_sizes)
            (out / 'version.json').write_text(json.dumps(version_manifest(version, published_releases))+'\n', encoding='utf-8')
        else:
            version = args.version or tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['version']
            out = build(args.out, version, android=args.android_version)
    except SiteError as error:
        print(f'site_release: {error}', file=sys.stderr)
        return 1
    print(f'site_release: wrote {out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
