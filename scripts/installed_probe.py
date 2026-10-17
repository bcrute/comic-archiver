"""Probe an installed wheel from an empty directory, with Python -I.

Executed by validate.py on the fleet. Uses synthetic exports, a fresh browser
and a real loopback listener; never loads an existing account or contacts Tapas.
"""
import http.cookiejar
from importlib import resources
from importlib import metadata
import json
from pathlib import Path
import struct
import subprocess
import sys
import threading
import urllib.request
from wsgiref.simple_server import make_server
import zlib

import comic_archive
from comic_archive.archive import verify
from comic_archive.adapters.local import LocalAdapter
from comic_archive.local_web import LocalApplication, LocalWorkspaceRegistry, LocalServer, PrivateRequestHandler
from comic_archive.models import Episode, Manifest, Series
from comic_archive.publication import PublicationStore


def png():
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b'\0\x40\xa0\x80')) + chunk(b'IEND', b''))


def main():
    package = Path(comic_archive.__file__).resolve()
    if not package.is_relative_to(Path(sys.prefix).resolve()):
        raise RuntimeError('Install probe imported the source checkout instead of the wheel')
    if metadata.distribution('comic-archive').metadata['Name'] != 'comic-archive':
        raise RuntimeError('Installed distribution still uses the old project name')
    for name in ('index.html', 'archive.css', 'archive.js', 'library.html', 'library.js'):
        if not resources.files('comic_archive').joinpath('static', name).read_bytes():
            raise RuntimeError('Installed dashboard resource missing')
    from comic_archive.cloud_auth import SignIn
    from comic_archive.cloud_tools import VERSION
    if SignIn(Path.cwd()/'Library').status() != {'status':'idle'} or not VERSION:
        raise RuntimeError('Installed button sign-in modules are unavailable')
    entry = Path(sys.executable).parent / 'comic-archive'
    help_text = subprocess.check_output([str(entry), '--help'], text=True, timeout=30)
    for command in ('cloud-connect','cloud-test','cloud-upload','cloud-cleanup','storage-plan','preview-series','purge','network-policy','convert-comics','desktop'):
        if command not in help_text:raise RuntimeError('Installed CLI is missing storage/cloud commands')
    space = json.loads(subprocess.check_output([str(entry),'storage-plan','--episodes','2'],text=True,timeout=30))
    if space['image_profile'] != 'original' or space['inventory_complete']:
        raise RuntimeError('Fresh installed storage planning is unsafe')
    cloud_state = json.loads(subprocess.check_output([str(entry),'cloud-status'],text=True,timeout=30))
    if any(t['connected'] for t in cloud_state['targets']):
        raise RuntimeError('Fresh installed cloud state is not isolated')
    if 'extract-account' not in help_text:
        raise RuntimeError('Installed account extraction entry point missing')
    status = json.loads(subprocess.check_output([str(entry), 'account-status'], text=True, timeout=30))
    if status.get('configured') is not False:
        raise RuntimeError('Clean installation unexpectedly reused account state')
    subprocess.run([str(entry), '--browser', 'firefox', '--headless', 'doctor'], check=True, timeout=30)
    image = Path('original.png')
    image.write_bytes(png())
    comic = Series('synthetic-comic', 'Synthetic comic', source='local')
    novel = Series('synthetic-novel', 'Synthetic novel', source='local')
    manifest_path = Path('manifest.json')
    manifest_path.write_text(json.dumps({'series': {'id': comic.id, 'title': comic.title, 'source': 'local'},
                            'episodes': [{'id': 'comic-1', 'title': 'Chapter', 'order': 1,
                                          'pages': [{'order': 1, 'file': image.name}]}]}))
    adapter = LocalAdapter(manifest_path)
    chapter = Manifest(novel, (Episode('novel-1', novel.id, 'Chapter', 1, (), 'local_owned', content_type='novel',
                        text_html='<p>Unicode 尾声<img src="images/0001.png" alt="Art" /></p>', illustrations=(png(),)),))
    store = PublicationStore(Path('Library'))
    try:
        store.archive(adapter.manifest(), adapter, progress=lambda _: None)
        store.archive(chapter, adapter, progress=lambda _: None)
    finally:
        store.close()
    if verify(Path('Library'), decode_images=True):
        raise RuntimeError('Installed package failed portable export verification')
    server = make_server('127.0.0.1', 0, None, server_class=LocalServer, handler_class=PrivateRequestHandler)
    server.set_app(LocalApplication(LocalWorkspaceRegistry(Path.cwd()), server.server_port))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        origin = 'http://127.0.0.1:' + str(server.server_port)
        with client.open(origin + '/', timeout=10) as response:
            body = response.read()
            if response.status != 200 or b'archive.js' not in body or b'Comic Archiver' not in body:
                raise RuntimeError('Installed loopback dashboard failed')
        for path in ('/assets/archive.js', '/assets/archive.css', '/library', '/assets/library.js', '/api/v1/context'):
            with client.open(origin + path, timeout=10) as response:
                if response.status != 200 or not response.read():
                    raise RuntimeError('Installed dashboard asset or context failed')
        context = json.loads(client.open(origin+'/api/v1/context', timeout=10).read())
        if context.get('desktop_only') is not True:
            raise RuntimeError('Installed app did not default to desktop downloads')
        workspace = json.loads(client.open(origin+'/api/v1/workspaces', timeout=10).read())['workspace_ids'][0]
        catalog = json.loads(client.open(origin+'/api/v1/workspaces/'+workspace+'/saved-files?kind=episodes', timeout=10).read())
        if catalog['total'] != 2 or {f['kind'] for f in catalog['files']} != {'cbz','epub'}:
            raise RuntimeError('Installed saved-file table lost a portable episode')
    finally:
        server.shutdown()
        thread.join(timeout=10)
        server.server_close()
    if not Path('.comic-session').is_dir():
        raise RuntimeError('Fresh installed app did not create its session directory')
    print(json.dumps({'wheel_import': True, 'cli': True,
                      'fresh_state': True, 'firefox_startup': True,
                      'comic_and_illustrated_epub': True, 'real_loopback_dashboard': True,
                      'saved_file_table': True,
                      'source_requests': 0}))


if __name__ == '__main__':
    main()
