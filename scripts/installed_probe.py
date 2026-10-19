"""Probe an installed wheel from an empty directory, with Python -I.

Executed by validate.py on the fleet. Uses synthetic exports and a fresh browser,
and reads the library through the window's own calls; never loads an existing account or contacts Tapas.
"""
from importlib import resources
from importlib import metadata
import json
from pathlib import Path
import re
import struct
import subprocess
import sys
from types import SimpleNamespace
import zlib

import comic_archive
from comic_archive import account_export, cloud, sites, storage
from comic_archive.app_actions import Actions
from comic_archive.archive import verify
from comic_archive.local_library import LocalWorkspaceRegistry
from comic_archive.models import Episode, Manifest, Page, Series
from comic_archive.publication import PublicationStore


def png():
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b'\0\x40\xa0\x80')) + chunk(b'IEND', b''))


class SyntheticSource:
    """Hands the archive its synthetic page from disk; there is nothing to refresh."""
    def retrieve(self, page):
        return Path(page.location).read_bytes()

    def refresh(self, episode):
        raise RuntimeError('Synthetic pages never need refreshing')


def main():
    package = Path(comic_archive.__file__).resolve()
    if not package.is_relative_to(Path(sys.prefix).resolve()):
        raise RuntimeError('Install probe imported the source checkout instead of the wheel')
    if metadata.distribution('comic-archive').metadata['Name'] != 'comic-archive':
        raise RuntimeError('Installed distribution still uses the old project name')
    for name in (f'{shape}-{tone}{size}.png' for shape in ('check', 'arrow') for tone in ('light', 'dark') for size in ('', '@2x')):
        if not resources.files('comic_archive').joinpath('window', name).read_bytes():
            raise RuntimeError('Installed window resource missing')
    from comic_archive.cloud_auth import SignIn
    from comic_archive.cloud_tools import VERSION
    if SignIn(Path.cwd()/'Library').status() != {'status':'idle'} or not VERSION:
        raise RuntimeError('Installed button sign-in modules are unavailable')
    # The app starts its downloader as `python -m comic_archive browser-worker`; that and the builds' doctor are the
    # whole command line. Compare whole names from the help's own list, never substrings of other help text.
    # Before Python 3.14 a wrapped usage line repeats the list on a line of its own, so identical copies count once.
    help_text = subprocess.check_output([sys.executable, '-m', 'comic_archive', '--help'], text=True, timeout=30)
    listed = set(re.findall(r'^\s*\{([a-z0-9,-]+)\}$', help_text, re.M))
    if len(listed) != 1 or set(next(iter(listed)).split(',')) != {'browser-worker', 'doctor'}:
        raise RuntimeError('Installed downloader commands are missing')
    if storage.policy(Path('Library'))['image_profile'] != 'original':
        raise RuntimeError('Fresh installed storage policy is unsafe')
    if any(t['connected'] for t in cloud.status(Path('Library'))['targets']):
        raise RuntimeError('Fresh installed cloud state is not isolated')
    if account_export.status(Path('.comic-session')).get('configured') is not False:
        raise RuntimeError('Clean installation unexpectedly reused account state')
    subprocess.run([sys.executable, '-m', 'comic_archive', '--browser', 'firefox', '--headless', 'doctor'], check=True, timeout=30)
    comic = Series('synthetic-comic', 'Synthetic comic', source='local')
    novel = Series('synthetic-novel', 'Synthetic novel', source='local')
    image = Path('original.png').absolute()
    image.write_bytes(png())
    pages = Manifest(comic, (Episode('comic-1', comic.id, 'Chapter', 1, (Page(1, str(image)),), 'local_owned'),))
    chapter = Manifest(novel, (Episode('novel-1', novel.id, 'Chapter', 1, (), 'local_owned', content_type='novel',
                        text_html='<p>Unicode 尾声<img src="images/0001.png" alt="Art" /></p>', illustrations=(png(),)),))
    store = PublicationStore(Path('Library'))
    try:
        store.archive(pages, SyntheticSource(), progress=lambda _: None)
        store.archive(chapter, SyntheticSource(), progress=lambda _: None)
    finally:
        store.close()
    if verify(Path('Library'), decode_images=True):
        raise RuntimeError('Installed package failed portable export verification')
    registry = LocalWorkspaceRegistry(Path.cwd())
    runtime = SimpleNamespace(registry=registry, site=sites.DEFAULT, folder=Path('Library').absolute(),
                              site_settings=SimpleNamespace(list=list))
    actions = Actions(lambda: runtime)
    if actions.context()['library_folder'] != str(Path('Library').absolute()):
        raise RuntimeError('Installed window calls named another library')
    if actions.poll(cloud=False, network=False)['account'].get('configured') is not False:
        raise RuntimeError('Installed window calls reused account state')
    catalog = actions.saved_files(kind='episodes')
    if catalog['total'] != 2 or {f['kind'] for f in catalog['files']} != {'cbz','epub'}:
        raise RuntimeError('Installed saved-file table lost a portable episode')
    if not Path('.comic-session').is_dir():
        raise RuntimeError('Fresh installed app did not create its session directory')
    print(json.dumps({'wheel_import': True, 'cli': True,
                      'fresh_state': True, 'firefox_startup': True,
                      'comic_and_illustrated_epub': True, 'window_calls': True,
                      'saved_file_table': True,
                      'source_requests': 0}))


if __name__ == '__main__':
    main()
