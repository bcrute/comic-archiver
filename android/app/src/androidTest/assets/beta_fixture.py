"""Disposable-emulator site stand-in; never packaged in the application's APK.

Only HTTPX's socket boundary is replaced. Native cookie authority, Transport,
Tapas parsers, worker, publication and archive bytes remain production code.
Unknown requests fail locally; no request can reach a real provider or account.
"""
import hashlib
import json
import struct
import threading
import zlib

import httpx

_lock = threading.Lock()
_release = threading.Event()
_blocked = threading.Event()
_installed = False
_did_block = False
_requests = set()
_asset_requests = 0
_asset_cookie_leaks = 0
_unexpected = 0
_browser_agent_seen = False
_fast_asset_clients = 0


def png(seed):
    """Real, deterministic incompressible pages, large enough to block a SAF pipe."""
    width = height = 512
    pixels = hashlib.shake_256(seed).digest(width * height * 3)
    scanlines = b''.join(b'\0' + pixels[i * width * 3:(i + 1) * width * 3] for i in range(height))

    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))

    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(scanlines, level=0)) + chunk(b'IEND', b''))


PAGES = (png(b'android-beta-page-one'), png(b'android-beta-page-two'))
ASSETS = tuple(f'https://us-a.tapas.io/pc/integration/{i}.png?signature=FIXTURE' for i in (1, 2))
NOVEL_TEXT = 'A silver fixture chapter survives a cold restart.'


def episode(key, *, novel=False, locked=False):
    return {'id': key, 'title': 'Silver Chapter One' if novel else ('Locked Gate' if locked else 'Golden Open'),
            'scene': 2 if locked else 1,
            'must_pay': locked, 'scheduled': False, 'book': novel, 'free': not novel and not locked,
            'unlocked': novel and not locked, 'closing_date': None}


def series(key):
    novel = key == 2002
    return {'id': key, 'title': 'Silver Novel' if novel else 'Golden Comic',
            'url': 'silver-novel' if novel else 'golden-comic', 'comic': not novel, 'book': novel}


def list_page(category, page):
    key, slug, title = (1001, 'golden-comic', 'Golden Comic') if page == 1 else (2002, 'silver-novel', 'Silver Novel')
    kind = '&amp;type=ALL' if category == 'FREE_EPISODES' else ''
    next_page = 2 if page == 1 else page
    more = (f'<section class="paging-wrap"><a class="paging__button--next{ " disabled" if page == 2 else ""}" '
            f'href="/reading-list?category={category}{kind}&amp;pageNumber={next_page}">Next</a></section>')
    tabs = ''.join(f'<a href="/reading-list?category={key}">{key.title()}</a>'
                   for key in ('SUBSCRIBED', 'UPDATED', 'RECENT'))
    filters = ('<nav class="section-filter"><ul><li data-type="FREE_EPISODES" '
               f'class="filter-item{ " on" if category == "FREE_EPISODES" else ""}">'
               '<a class="item-title" href="/reading-list?category=FREE_EPISODES&amp;type=ALL">Free Episodes</a>'
               '</li></ul></nav><nav class="section-sort"><ul><li class="sort-item on">'
               '<a class="item-title">All</a></li></ul></nav>')
    return ('<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<style>body{margin:16px}a{display:inline-block;padding:12px}</style></head>'
            f'<body><a href="/reading-list">Library</a>{tabs}{filters}<ul class="content-list-wrap"><li class="content-item" '
            f'data-tiara-event-meta-series-id="{key}"><a href="/series/{slug}">{title}</a>'
            f'</li></ul>{more}</body></html>').encode()


class PageStream(httpx.SyncByteStream):
    def __init__(self, data, block):
        self.data, self.block = data, block

    def __iter__(self):
        for offset in range(0, len(self.data), 65536):
            yield self.data[offset:offset + 65536]
            if self.block and offset == 0:
                _blocked.set()
                try:
                    if not _release.wait(120):
                        raise httpx.ReadTimeout('The stand-in transfer was not released.')
                finally:
                    _blocked.clear()


def serve(request):
    global _asset_requests, _asset_cookie_leaks, _unexpected, _browser_agent_seen, _did_block
    host, path = request.url.host, request.url.path
    with _lock:
        _requests.add((host, path, str(request.url.query, 'ascii')))
        _browser_agent_seen |= request.headers.get('User-Agent', '').startswith('Mozilla/')
    if request.method != 'GET':
        with _lock:
            _unexpected += 1
        return httpx.Response(405)
    if host == 'us-a.tapas.io' and path in {'/pc/integration/1.png', '/pc/integration/2.png'}:
        with _lock:
            _asset_requests += 1
            if request.headers.get('Cookie'):
                _asset_cookie_leaks += 1
                return httpx.Response(403)
            block = path.endswith('/1.png') and not _did_block
            if block:
                _did_block = True
        return httpx.Response(200, headers={'Content-Type': 'image/png'},
                              stream=PageStream(PAGES[0 if path.endswith('/1.png') else 1], block))
    if host in {'m.tapas.io', 'tapas.io'}:
        authenticated = any(value.strip() == 'fixture_session=READY'
                            for value in request.headers.get('Cookie', '').split(';'))
        if path == '/who-am-i':
            return httpx.Response(200, json={'data': {'user': {'id': 4242}} if authenticated else {}})
        if not authenticated:
            return httpx.Response(401)
        if path == '/' and host == 'tapas.io':
            return httpx.Response(200, text='<html><body><a href="/reading-list">Library</a></body></html>')
        if path == '/reading-list':
            category = request.url.params.get('category', 'UPDATED')
            page = request.url.params.get('pageNumber', '1')
            if category in {'SUBSCRIBED', 'UPDATED', 'RECENT', 'FREE_EPISODES'} and page in {'1', '2'}:
                return httpx.Response(200, content=list_page(category, int(page)),
                                      headers={'Content-Type': 'text/html'})
        for key in (1001, 2002):
            info = series(key)
            if path == f'/series/{key}':
                return httpx.Response(200, json={'data': info})
            if path == f'/series/{key}/episodes':
                episodes = [episode(9100, novel=True)] if key == 2002 else [episode(9001), episode(9003, locked=True)]
                return httpx.Response(200, json={'data': {'pagination': {'total': len(episodes), 'has_next': False},
                                                         'episodes': episodes}})
            if path in {f'/series/{info["url"]}', f'/series/{info["url"]}/info'}:
                return httpx.Response(200, text=f'<html><script>var seriesId={key};</script>'
                                      '<div class="js-series-description"><span class="description__body">'
                                      'Offline integration fixture.</span></div></html>')
        for key in (9001, 9003, 9100):
            if path == f'/episode/{key}/info':
                return httpx.Response(200, json={'data': episode(key, novel=key == 9100, locked=key == 9003)})
        if path == '/episode/9001':
            images = ''.join(f'<img class="content__img" data-src="{url}">' for url in ASSETS)
            return httpx.Response(200, text='<script>var seriesId=1001;</script>'
                                  f'<div id="episode-9001"><article>{images}</article></div>')
        if path == '/episode/9100':
            return httpx.Response(200, text='<script>var seriesId=2002;</script>'
                                  f'<div id="episode-9100"><div id="epub-9100" class="ep-epub-content"><p>{NOVEL_TEXT}</p>'
                                  '<p><em>Comics and novels use the real writers.</em></p></div></div>')
    with _lock:
        _unexpected += 1
    return httpx.Response(404, text='Unmapped stand-in request; external networking is disabled.')


def client():
    return httpx.Client(transport=httpx.MockTransport(serve), follow_redirects=False)


def browser_asset_client(fast):
    global _fast_asset_clients
    if fast:
        with _lock:
            _fast_asset_clients += 1
    return client()


def browser_response(url, cookie, method='GET'):
    """Only the native test WebView calls this network-response seam.

    Cookie is read from its actual bound profile, not installed by the fixture.
    This qualifies profile authority and browser execution, not a TLS wire capture.
    """
    parsed = httpx.URL(url)
    if parsed.scheme != 'https' or parsed.host != 'tapas.io':
        return json.dumps({'status': 403, 'type': 'text/plain', 'body': 'External networking is disabled.'})
    response = serve(httpx.Request(method, url, headers={'Cookie': cookie or ''}))
    response.read()
    return json.dumps({'status': response.status_code,
        'type': response.headers.get('Content-Type', 'text/plain').split(';')[0], 'body': response.text})


def install():
    """Called by the emulator-only test Application, after real native bootstrap."""
    global _installed
    if _installed:
        return
    from comic_archive import android_adapter, android_browser, android_readers, android_transport
    base = android_transport.AndroidTransport

    class FixtureTransport(base):
        def __init__(self, *args, **kwargs):
            if kwargs.get('client') is None:
                kwargs['client'] = client()
            super().__init__(*args, **kwargs)

    android_transport.AndroidTransport = FixtureTransport
    android_adapter.AndroidTransport = FixtureTransport
    android_readers.AndroidTransport = FixtureTransport
    android_browser._asset_client = browser_asset_client
    _installed = True


def release_transfer():
    _release.set()


def pause_requested(root):
    """Observe the production Pause action's durable cancellation; never create it."""
    from pathlib import Path
    from comic_archive import task_cancellation, worker_host
    session = Path(root) / '.comic-session'
    task = worker_host.status(session).get('task_id')
    return isinstance(task, str) and task_cancellation.queued_stop_requested(session, task)


def observation():
    with _lock:
        return json.dumps({'blocked_transfer': _blocked.is_set(), 'asset_requests': _asset_requests,
                           'asset_cookie_leaks': _asset_cookie_leaks, 'unexpected_requests': _unexpected,
                           'browser_agent_seen': _browser_agent_seen,
                           'fast_asset_clients': _fast_asset_clients,
                           'locked_reader_requested': any(path == '/episode/9003' for host, path, query in _requests),
                           'subscribed_pages': sorted({query for host, path, query in _requests
                                                       if path == '/reading-list' and 'SUBSCRIBED' in query}),
                           'recent_pages': sorted({query for host, path, query in _requests
                                                   if path == '/reading-list' and 'RECENT' in query}),
                           'free_pages': sorted({query for host, path, query in _requests
                                                 if path == '/reading-list' and 'FREE_EPISODES' in query}),
                           'page_hashes': [hashlib.sha256(page).hexdigest() for page in PAGES],
                           'novel_text': NOVEL_TEXT})


def working_library_facts(root):
    """Test-only readback; no account salt, binding or source URL leaves the target."""
    from pathlib import Path
    from comic_archive.inventory import Inventory
    with Inventory(Path(root) / 'Library', read_only=True) as inventory:
        binding = dict(inventory.db.execute("SELECT key,value FROM settings WHERE key IN ('account_salt','account_binding')"))
        counts = dict(inventory.db.execute('SELECT status,COUNT(*) FROM episodes GROUP BY status'))
    return json.dumps({'binding_fields': len(binding),
                       'binding_digest': hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest(),
                       'archived': counts.get('archived', 0),
                       'not_archived': sum(count for status, count in counts.items() if status != 'archived')})
