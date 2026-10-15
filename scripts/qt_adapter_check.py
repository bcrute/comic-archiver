#!/usr/bin/env python3
"""Run the real Tapas adapter on the app's built-in engine against a local stand-in for tapas.io,
then make the same calls with Playwright's Firefox and compare what the two return.

The Firefox path is the one proven against the real site, so equal results on equal
pages are the evidence that the built-in engine can take its place. Each check prints
PASS or FAIL, each compared call prints EQUAL or DIFFERENT, the last line is
'ADAPTER CHECK n of m passed' and the exit status is 0 only when everything passed.
One thing is told in a NOTE and not counted: under Firefox a page's own write can reach
the site while the adapter exchanges its request rules. The built-in engine is held to none.

Nothing here contacts the internet: every name the built-in engine looks up, and every
connection Firefox opens, ends at the stand-in on this computer, and the image downloader
is stopped from sending anything. Product code is not changed; the script only wraps it.
Needs the desktop and test extras and Playwright's Firefox; run it on the build fleet:

    python -m playwright install firefox && python scripts/qt_adapter_check.py
"""
import argparse
import dataclasses
import html
import http.server
import json
import math
import os
import re
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ORIGIN = 'https://tapas.io'
# A name that exists nowhere: if it arrives at the stand-in, names are not being looked up outside.
PROBE = 'https://standin.invalid/'
# Seconds one engine's run may take before it is stopped.
LIMIT = 660
ACCOUNT = 7001
# Made-up cookie values. They are compared, never printed.
SESSION, KEEP = 'made-up-signed-in-value', 'made-up-unchanged-value'
EXPECTED_COOKIES = {'session', 'keep', 'wide', 'visit'}
# The account's lists: which series each holds and how many a page shows.
LISTS = {'SUBSCRIBED': (range(101, 116), 5), 'RECENT': (range(201, 225), 24),
         'UPDATED': (range(301, 309), 4), 'FREE_EPISODES': (range(401, 407), 3)}
KNOWN = {number for numbers, _ in LISTS.values() for number in numbers} | {999}
DETAILED, EPISODES, CHALLENGED = 101, range(5001, 5026), 9403
# Episodes whose read the stand-in ends without an answer: one for its first second only, one every time.
CUT_ONCE, CUT = 9501, 9502
CUTS = {}
BLANK = 'data:image/gif;base64,R0lGODlhAQABAAAAACw='
STYLE = '''body{margin:0;font:16px sans-serif} nav ul{list-style:none;margin:0;padding:0;display:flex}
nav a{display:inline-block;padding:8px 12px} ul.content-list-wrap{list-style:none;margin:0;padding:0}
li.content-item{height:150px;box-sizing:border-box;border-bottom:1px solid #ccc}
li.content-item a{display:inline-block;padding:8px 12px} section.paging-wrap{padding:12px}
a.paging__button{display:inline-block;min-width:32px;height:32px;border:1px solid #888}'''
# What the site's own scripts do on every page: read storage, write some, start a background
# worker and send a write. The downloader must refuse the last two.
PAGE_SCRIPT = '''const root = document.documentElement;
root.dataset.saved = String(localStorage.getItem('saved-entry'));
localStorage.setItem('page-entry', 'written');
root.dataset.sw = 'pending';
try {
  navigator.serviceWorker.register('/sw.js').then(found => { root.dataset.sw = found ? 'registered' : 'blocked'; },
    error => { root.dataset.sw = 'refused ' + error.name; });
} catch (error) { root.dataset.sw = 'absent'; }
root.dataset.post = 'pending';
fetch('/track', {method: 'POST', body: 'x'}).then(answer => { root.dataset.post = 'sent ' + answer.status; },
  () => { root.dataset.post = 'refused'; });
document.addEventListener('wheel', event => { root.dataset.wheel = String(event.isTrusted); }, true);'''
# Firefox must not look names up or open connections ahead of a visit.
QUIET = {'network.dns.disablePrefetch': True, 'network.prefetch-next': False, 'network.http.speculative-parallel-limit': 0}

ENGINE = ''
# The part of the run now under way, noted beside each request the site sees.
PART = ['setup']
SEEN = []
CHECKS = []
RESULTS = {}
VISITS = [0]
TUNNELLED = []
ASSET_CALLS = []
ACCEPTED = []


def title(number):
    return f'Tea & Cake №{number}' if number % 7 == 5 else f'Story {number}'


def page_html(name, body):
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{html.escape(name)}</title>'
            f'<link rel="icon" href="data:,"><style>{STYLE}</style><script>{PAGE_SCRIPT}</script></head><body>'
            '<header><nav class="global-nav"><a href="/">Home</a><a href="/reading-list">Library</a></nav></header>'
            f'{body}</body></html>')


def home():
    return page_html('Home', '<main><h1>Home</h1><p><a href="/series/story-101/info">Story 101</a></p></main>')


def reading_list(category, number):
    """One page of an account list, with the markup the adapter reads on the real site."""
    numbers, size = LISTS[category]
    shown = list(numbers)[(number - 1) * size:number * size]
    last = math.ceil(len(numbers) / size)

    def link(page):
        return html.escape(f'/reading-list?pageNumber={page}&type=ALL&category={category}')

    # The real pager has pictures for its arrows and no words.
    following = (f'<a class="paging__button paging__button--next" href="{link(number + 1)}"></a>' if number < last
                 else '<a class="paging__button paging__button--next disabled"></a>')
    numbered = ''.join(f'<a class="paging__button" href="{link(page)}">{page}</a>' for page in range(1, last + 1))
    filtered = category == 'FREE_EPISODES'
    note = '<aside class="free-note" data-free-episode-count="6"><p>6 free episodes</p></aside>' if filtered else ''
    items = ''.join(f'<li class="content-item" data-tiara-event-meta-series-id="{series}">'
                    f'<a class="title" href="/series/story-{series}/info">{html.escape(title(series))}</a></li>' for series in shown)
    return page_html('Library', f'''<nav class="section-tab"><a href="/reading-list">Updated</a>
<a href="/reading-list?category=RECENT">Recent</a><a href="/reading-list?category=SUBSCRIBED">Subscribed</a></nav>
<nav class="section-sort"><ul class="sort-wrap"><li class="sort-item on"><a class="item-title" href="#!/reading-list?type=">All</a></li>
<li class="sort-item"><a class="item-title" href="#!/reading-list?type=COMICS">Comics</a></li>
<li class="sort-item"><a class="item-title" href="#!/reading-list?type=BOOKS">Novels</a></li></ul></nav>
<nav class="section-filter"><ul class="filter-wrap">
<li class="filter-item{' on' if filtered else ''}" data-type="FREE_EPISODES"><a class="item-title"
 href="/reading-list?category=FREE_EPISODES&amp;type=ALL">Free Episodes</a></li>
<li class="filter-item" data-type="WAIT_UNTIL_FREE"><a class="item-title"
 href="/reading-list?category=WAIT_UNTIL_FREE&amp;type=ALL">Wait Until Free</a></li></ul></nav>{note}
<ul class="content-list-wrap">{items}</ul>
<section class="paging-wrap"><a class="paging__button paging__button--prev{'' if number > 1 else ' disabled'}"></a>{numbered}{following}</section>''')


def details(number):
    """A series page, with its creator and description given twice as the real page gives them."""
    creator = f'<a class="name" href="/creator-{number}">Studio &amp; Artist {number}</a>'
    return page_html(title(number), f'''<script>var seriesId="{number}";</script>
<ul class="detail-row__body detail-row__body--creator"><li><div class="creator-info__top">
<a class="thumb" href="/creator-{number}">Avatar</a>{creator}</div></li></ul>
<div class="detail-row__body detail-row__body--desc">
<p class="description js-series-description"><span class="description__body">The story of {number}.<br>Its second line.</span>Read more</p>
<p class="colophon">Created by Artist<br>Translated by Translator</p></div>
<ul class="creator-section"><li><div class="creator-info__top">{creator}</div></li></ul>
<p class="js-series-description"><span class="description__body">The story of {number}.<br>Its second line.</span></p>''')


def series(number):
    return {'id': number, 'title': title(number), 'url': f'story-{number}', 'comic': True, 'book': False, 'premium': False,
            'thumb_url': f'https://us-a.tapas.io/sa/cover/{number}.jpg'}


def episode(number):
    """Three free episodes, two unlocked (one for a time), one not yet out, the rest to be paid for."""
    scene = number - EPISODES[0] + 1
    free, unlocked, scheduled = scene <= 3, scene in (4, 5), scene == 7
    return {'id': number, 'title': f'Episode {scene}', 'scene': scene, 'publish_date': '2026-01-05T18:00:00Z', 'free': free,
            'unlocked': unlocked, 'must_pay': not (free or unlocked or scheduled), 'scheduled': scheduled, 'book': False,
            'free_access': False, 'closing_date': '2099-01-01T00:00:00Z' if scene == 4 else None}


def pages_of(number):
    return [f'https://us-a.tapas.io/pc/a1/{number}-{order}.jpg?signature=TEST' for order in (1, 2, 3)]


def reader(number):
    images = ''.join(f'<img class="content__img" src="{BLANK}" data-src="{html.escape(url)}">' for url in pages_of(number))
    return page_html(f'Episode {number}', f'''<script>var episodeViewer = {{seriesId: {DETAILED}, episodeId: {number}}};</script>
<article id="episode-{number}" class="viewer__body" data-episode-id="{number}" data-series-id="{DETAILED}">{images}</article>''')


def envelope(value):
    return json.dumps({'data': value, 'error_details': None}).encode()


class Site(http.server.BaseHTTPRequestHandler):
    timeout = 30

    def log_message(self, *args):
        pass

    def answer(self, status, kind, body, extra=()):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for name, value in extra:
            self.send_header(name, value)
        try:
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            # The browser gave up on this request.
            pass

    def record(self):
        target = urlsplit(self.path)
        cookies = dict(part.split('=', 1) for part in (self.headers.get('Cookie') or '').split('; ') if '=' in part)
        row = {'method': self.command, 'target': self.path, 'path': target.path,
               'query': sorted(name for name, _ in parse_qsl(target.query, keep_blank_values=True)),
               'host': (self.headers.get('Host') or '').split(':')[0], 'cookies': sorted(cookies), 'part': PART[0],
               'signed': cookies.get('session') == SESSION, 'kept': cookies.get('keep') == KEEP,
               'dest': self.headers.get('Sec-Fetch-Dest'), 'mode': self.headers.get('Sec-Fetch-Mode'),
               'site': self.headers.get('Sec-Fetch-Site'), 'user': self.headers.get('Sec-Fetch-User'),
               'referer': self.headers.get('Referer'), 'accept': self.headers.get('Accept')}
        SEEN.append(row)
        return row

    def do_POST(self):
        self.record()
        self.rfile.read(int(self.headers.get('Content-Length') or 0))
        self.answer(200, 'application/json', b'{"written":true}')

    do_PUT = do_DELETE = do_PATCH = do_POST

    def do_GET(self):
        row = self.record()
        target = urlsplit(self.path)
        path, query = target.path, dict(parse_qsl(target.query, keep_blank_values=True))
        found = re.fullmatch(r'/(series|episode)/([A-Za-z0-9_-]+)(/info|/episodes)?', path)
        kind, key, rest = found.groups() if found else (None, '', None)
        number = int(key) if key.isdigit() else None
        if path == '/':
            self.answer(200, 'text/html; charset=utf-8', home())
        elif path == '/reading-list':
            category, page = query.get('category', 'UPDATED'), query.get('pageNumber', '1')
            numbers, size = LISTS.get(category, ((), 1))
            if page.isdigit() and 1 <= int(page) <= math.ceil(len(numbers) / size):
                self.answer(200, 'text/html; charset=utf-8', reading_list(category, int(page)))
            else:
                self.answer(404, 'text/html; charset=utf-8', page_html('Not found', '<p>Not found</p>'))
        elif path == '/who-am-i':
            if not row['signed']:
                self.answer(401, 'application/json', b'{"data":null,"error_details":{"code":401}}')
                return
            VISITS[0] += 1
            # As the real site does: one cookie is set again unchanged, another changes with every answer.
            self.answer(200, 'application/json', envelope({'user': {'id': ACCOUNT, 'uname': 'reader', 'display_name': 'Reader'}}),
                        [('Set-Cookie', f'keep={KEEP}; Path=/; Secure; HttpOnly; SameSite=Lax'),
                         ('Set-Cookie', f'visit={VISITS[0]}; Path=/; Secure; SameSite=Lax')])
        elif path == '/sw.js':
            self.answer(200, 'text/javascript', b'self.addEventListener("fetch", () => {})')
        elif kind == 'series' and rest is None and number in KNOWN:
            self.answer(200, 'application/json', envelope(series(number)))
        elif kind == 'series' and rest == '/episodes' and number in KNOWN:
            listed = [episode(item) for item in EPISODES] if number == DETAILED else []
            page = int(query.get('page', '1')) if query.get('page', '1').isdigit() else 1
            self.answer(200, 'application/json', envelope({
                'episodes': listed[(page - 1) * 20:page * 20],
                'pagination': {'page': page + 1, 'total': len(listed), 'has_next': page * 20 < len(listed), 'limit': 20}}))
        elif kind == 'series' and rest == '/info' and key == 'moved':
            self.answer(302, 'text/html; charset=utf-8', b'', [('Location', '/series/story-999/info')])
        elif kind == 'series' and rest == '/info' and re.fullmatch(r'story-[0-9]+', key) and int(key[6:]) in KNOWN:
            self.answer(200, 'text/html; charset=utf-8', details(int(key[6:])))
        elif kind == 'episode' and rest == '/info' and number == CHALLENGED:
            self.answer(403, 'text/html; charset=utf-8', '<!doctype html><title>Just a moment</title>', [('cf-mitigated', 'challenge')])
        elif kind == 'episode' and number in (CUT_ONCE, CUT) and (
                number == CUT or time.monotonic() - CUTS.setdefault(number, time.monotonic()) < 1):
            self.close_connection = True
        elif kind == 'episode' and rest == '/info' and number == CUT_ONCE:
            self.answer(200, 'application/json', envelope(episode(EPISODES[0]) | {'id': number}))
        elif kind == 'episode' and rest == '/info' and number in EPISODES:
            self.answer(200, 'application/json', envelope(episode(number)))
        elif kind == 'episode' and rest is None and number in EPISODES:
            self.answer(200, 'text/html; charset=utf-8', reader(number))
        else:
            self.answer(404, 'text/plain', b'no')


class Secure(http.server.ThreadingHTTPServer):
    daemon_threads = True
    tls = None

    def finish_request(self, request, client_address):
        # The handshake belongs to the request's own thread: a browser that opens a connection
        # and then waits must not hold the others up.
        try:
            request.settimeout(30)
            secure = self.tls.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError):
            return
        try:
            super().finish_request(secure, client_address)
        finally:
            try:
                secure.close()
            except OSError:
                pass

    def handle_error(self, request, client_address):
        # A browser that drops a connection is not this script's failure.
        pass


def serve_site(work):
    """Start the stand-in with a certificate of its own, made here and now; its port."""
    key, certificate = work / 'key.pem', work / 'cert.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key), '-out', str(certificate),
                    '-days', '2', '-subj', '/CN=tapas.io', '-addext', 'subjectAltName=DNS:tapas.io,DNS:*.tapas.io,DNS:standin.invalid'],
                   check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    server = Secure(('127.0.0.1', 0), Site)
    server.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.tls.load_cert_chain(str(certificate), str(key))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1]


def tunnel(port):
    """A proxy for Firefox that leads every secure connection to the stand-in and refuses the rest; its port."""
    listener = socket.create_server(('127.0.0.1', 0))

    def pump(source, sink):
        try:
            while True:
                data = source.recv(65536)
                if not data:
                    break
                sink.sendall(data)
        except OSError:
            pass
        finally:
            for end in (source, sink):
                try:
                    end.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def carry(client):
        try:
            client.settimeout(60)
            head = b''
            while b'\r\n\r\n' not in head and len(head) < 16384:
                data = client.recv(4096)
                if not data:
                    return
                head += data
            words = head.split(b'\r\n', 1)[0].split()
            if len(words) != 3 or words[0] != b'CONNECT':
                TUNNELLED.append('refused')
                client.sendall(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
                return
            TUNNELLED.append(words[1].decode('ascii', 'replace').rsplit(':', 1)[0])
            with socket.create_connection(('127.0.0.1', port), timeout=60) as site:
                client.sendall(b'HTTP/1.1 200 Connection established\r\n\r\n')
                threading.Thread(target=pump, args=(client, site), daemon=True).start()
                pump(site, client)
        except OSError:
            pass
        finally:
            client.close()

    def accept():
        while True:
            client, _ = listener.accept()
            threading.Thread(target=carry, args=(client,), daemon=True).start()

    threading.Thread(target=accept, daemon=True).start()
    return listener.getsockname()[1]


def through_proxy(port):
    """Have the adapter's Firefox reach every site through the proxy and accept the stand-in's certificate."""
    from comic_archive.adapters import tapas
    from comic_archive.models import ArchiveError
    from comic_archive.session import browser_failure_reason

    class Proxied:
        def __init__(self, browser):
            self.browser = browser

        def new_context(self, **options):
            return self.browser.new_context(ignore_https_errors=True, **options)

        def close(self):
            self.browser.close()

    def launch(driver, preferred='auto', headless=False):
        try:
            browser = driver.firefox.launch(headless=headless, timeout=60000, proxy={'server': f'http://127.0.0.1:{port}'},
                                            firefox_user_prefs=QUIET)
        except Exception as error:
            print('NOTE firefox: launch failed: ' + ' '.join(str(error).split())[:500], flush=True)
            raise ArchiveError('Cannot start Firefox: ' + browser_failure_reason(error)) from None
        return Proxied(browser)

    tapas.launch_browser = launch


def accept_certificates():
    """Have the built-in engine's pages accept the stand-in's certificate, should Qt ask about it."""
    from comic_archive import qt_engine
    made = qt_engine.Surface.__init__

    def accept(error):
        ACCEPTED.append(1)
        error.acceptCertificate()

    def init(self, *args, **kwargs):
        made(self, *args, **kwargs)
        self.page.certificateError.connect(accept)

    qt_engine.Surface.__init__ = init


def block_image_downloads():
    """The image downloader connects to its hosts directly; here it must never send anything."""
    from comic_archive import transport
    from comic_archive.models import ArchiveError

    def refuse(self, url, *args, **kwargs):
        ASSET_CALLS.append(urlsplit(url).hostname)
        raise ArchiveError('This check downloads no images.')

    transport.Transport._request = refuse


def saved_cookies(value):
    """A saved session's cookies: one that expires, one for the visit only, one for the whole domain and other sites."""
    return [{'name': 'session', 'value': value, 'domain': 'tapas.io', 'path': '/', 'expires': int(time.time()) + 30 * 86400,
             'httpOnly': True, 'secure': True, 'sameSite': 'Lax'},
            {'name': 'keep', 'value': KEEP, 'domain': 'tapas.io', 'path': '/', 'expires': -1,
             'httpOnly': True, 'secure': True, 'sameSite': 'Lax'},
            {'name': 'wide', 'value': 'made-up-wide-value', 'domain': '.tapas.io', 'path': '/', 'expires': -1,
             'httpOnly': False, 'secure': True, 'sameSite': 'None'}]


def described(cookie):
    """What a cookie is apart from its value, as a saved session states it."""
    return [cookie['name'], cookie['domain'], cookie['path'], cookie['httpOnly'], cookie['secure'], cookie['sameSite'],
            cookie['expires'] == -1]


def make_project(work):
    """A project folder as the app keeps it: two saved sessions and a library that holds nothing back."""
    project = work / 'project'
    for name, value in (('session', SESSION), ('stale', 'made-up-signed-out-value')):
        state = {'cookies': saved_cookies(value),
                 'origins': [{'origin': ORIGIN, 'localStorage': [{'name': 'saved-entry', 'value': 'kept'}]}]}
        (project / name).mkdir(parents=True, mode=0o700)
        (project / name / 'state.json').write_text(json.dumps(state))
        (project / name / 'state.json').chmod(0o600)
    # The default holds transfers until an unmetered connection is confirmed, which a build machine cannot confirm.
    settings = project / 'Library' / '.archive-state'
    settings.mkdir(parents=True)
    (settings / 'network-policy.json').write_text(json.dumps({'schema_version': 1, 'wifi_only': False}))
    return project


def check(name, passed, detail=''):
    CHECKS.append([name, bool(passed)])
    print(('PASS ' if passed else 'FAIL ') + f'{ENGINE}: {name}' + ('' if passed else f'  [{str(detail)[:700]}]'), flush=True)


def note(text):
    print(f'NOTE {ENGINE}: {text}', flush=True)


def group(name, body):
    PART[0] = name
    try:
        body()
    except Exception as error:
        check(f'{name} ran to its end', False, f'{type(error).__name__}: {error}')


def plain(value):
    """A returned value as plain data, without the network diagnostics that rightly differ between engines."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = dataclasses.asdict(value)
    if isinstance(value, dict):
        return {str(key): plain(item) for key, item in value.items() if key != 'normal_network'}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    if isinstance(value, bytes):
        return f'<{len(value)} bytes>'
    return value


def call(name, action):
    """One adapter call. What it returns is kept for the comparison; an error is kept by kind and raised again."""
    try:
        value = action()
    except Exception as error:
        RESULTS[name] = {'raised': type(error).__name__}
        raise
    RESULTS[name] = plain(value)
    return value


def refused(name, action):
    """One adapter call that is expected to fail: the error it raised, or None."""
    try:
        RESULTS[name] = {'returned': plain(action())}
    except Exception as error:
        RESULTS[name] = {'raised': type(error).__name__, 'status': getattr(error, 'source_http_status', None),
                         'kind': getattr(error, 'source_response_kind', None)}
        return error
    return None


def rows(mark, **wanted):
    return [row for row in SEEN[mark:] if all(row[key] == value for key, value in wanted.items())]


def visits(mark):
    """The pages the site was asked for as page visits, in order."""
    return [row['target'] for row in SEEN[mark:] if row['dest'] == 'document']


def outline(value):
    return {key: item for key, item in value.items() if key not in {'series', 'normal_network'}}


def waited(read, seconds=8):
    """read() once it is no longer 'pending', or whatever it gives when the time is up."""
    deadline = time.monotonic() + seconds
    while True:
        value = read()
        if value != 'pending' or time.monotonic() >= deadline:
            return value
        time.sleep(.1)


def probe(engine):
    """Before anything asks for tapas.io, prove where requests end up, on a page with no rule against anything.

    Also the control for two later checks: unguarded, the stand-in's pages do send
    a write and do fetch a background worker.
    """
    from comic_archive.adapters import tapas
    from comic_archive.session import browser_api
    mark, failure, worker = len(SEEN), '', None
    driver = browser_api(engine)().start()
    try:
        browser = tapas.launch_browser(driver, engine, headless=True)
        try:
            page = browser.new_context().new_page()
            try:
                page.goto(PROBE, wait_until='domcontentloaded', timeout=30000)
                root = page.locator('html')
                waited(lambda: root.get_attribute('data-post'))
                worker = waited(lambda: root.get_attribute('data-sw'))
                # The worker's script is fetched after the page says it is registering.
                deadline = time.monotonic() + 3
                while not rows(mark, path='/sw.js') and time.monotonic() < deadline:
                    time.sleep(.1)
            except Exception as error:
                failure = f'{type(error).__name__}: {error}'
        finally:
            browser.close()
    finally:
        driver.stop()
    reached = bool(rows(mark, path='/', host='standin.invalid'))
    check('a name that exists nowhere arrives at the stand-in', reached, failure)
    if not reached:
        return False
    check("control: unguarded, a page's own write reaches the site", rows(mark, path='/track', method='POST'), failure)
    check("control: unguarded, a page's background worker is fetched", rows(mark, path='/sw.js'), worker)
    return True


def exercise(engine, project):
    """The desktop app's adapter calls, each followed by checks of what came back and of what the site saw."""
    import contextlib
    import io
    from comic_archive.adapters import tapas
    from comic_archive.adapters.tapas import TapasAdapter
    from comic_archive.errors import AuthenticationError, NetworkUnavailable
    from comic_archive.models import ArchiveError
    if not probe(engine):
        return
    start = len(SEEN)
    first = ORIGIN + '/reading-list?category=SUBSCRIBED'

    def numbered(value):
        return [row['source_series_id'] for row in value['series']]

    def expected(category):
        return [str(number) for number in LISTS[category][0]]

    def clicked(found, referers):
        return (len(found) == len(referers) and [row['referer'] for row in found] == referers
                and all(row['user'] == '?1' and row['site'] == 'same-origin' and row['signed'] for row in found))

    def shown(found):
        return [(row['target'], row['user'], row['site'], row['referer']) for row in found]

    def work(adapter):
        root = adapter.page.locator('html')

        def identity():
            mark = len(SEEN)
            value = call('account_identity', adapter.account_identity)
            asked = rows(mark, path='/who-am-i')
            check('the account is read from /who-am-i', value == str(ACCOUNT) and len(asked) == 1, (value, len(asked)))
            row = asked[0]
            check('that read came from inside the page, with the saved cookies',
                  row['signed'] and row['kept'] and 'wide' in row['cookies'] and row['dest'] == 'empty'
                  and row['site'] == 'same-origin' and row['accept'] == 'application/json',
                  {key: row[key] for key in ('cookies', 'signed', 'kept', 'dest', 'site', 'accept')})
            check('the home page is opened first', visits(mark) == ['/'], visits(mark))

        def subscribed():
            mark, handed = len(SEEN), []
            value = call('inspect_library subscribed', lambda: adapter.inspect_library(category='subscribed', on_page=handed.append))
            RESULTS['on_page subscribed'] = plain(handed)
            check('the subscribed list is complete, every series with its id and title',
                  value['complete'] is True and value['boundary_observed'] is True and value['membership_verified'] is True
                  and value['scope'] == 'subscribed' and numbered(value) == expected('SUBSCRIBED')
                  and [row['title'] for row in value['series']] == [title(number) for number in LISTS['SUBSCRIBED'][0]]
                  and value['unresolved_series_id_count'] == 0, outline(value))
            check('each of its three pages is handed over once, as it is read',
                  [len(page['series']) for page in handed] == [5, 5, 5]
                  and value['pages'] == [{'page': page, 'series_count': 5} for page in (1, 2, 3)],
                  ([len(page['series']) for page in handed], value['pages']))
            check('its first page is restored afterwards', value['restore_succeeded'] is True and adapter.page.url == first,
                  (value['restore_succeeded'], adapter.page.url))
            wanted = ['/', '/reading-list', '/reading-list?category=SUBSCRIBED', '/reading-list?pageNumber=2&type=ALL&category=SUBSCRIBED',
                      '/reading-list?pageNumber=3&type=ALL&category=SUBSCRIBED', '/reading-list?category=SUBSCRIBED']
            check('the site saw the home page, the list, its tab and each next page', visits(mark) == wanted, visits(mark))
            found = [row for row in SEEN[mark:] if 'pageNumber' in row['query']]
            check('the next pages were reached by real clicks',
                  clicked(found, [first, ORIGIN + '/reading-list?pageNumber=2&type=ALL&category=SUBSCRIBED']), shown(found))
            check('the account is checked again after each click', len(rows(mark, path='/who-am-i')) == 3,
                  len(rows(mark, path='/who-am-i')))

        def own_scripts():
            check("the page's own scripts find the saved storage", root.get_attribute('data-saved') == 'kept',
                  root.get_attribute('data-saved'))
            sent = [(row['target'], row['part']) for row in SEEN[start:] if row['method'] != 'GET']
            # What reaches the site under Firefox is told at the end of its run.
            check("the page's own write is refused" + (' and never reaches the site' if engine == 'qt' else ''),
                  waited(lambda: root.get_attribute('data-post')) == 'refused' and (engine != 'qt' or not sent),
                  (root.get_attribute('data-post'), sent))
            outcome = str(waited(lambda: root.get_attribute('data-sw')))
            # Playwright refuses by answering the registration with nothing; the built-in engine by refusing the worker's script.
            check("the page's background worker is refused and never fetched",
                  (outcome.startswith('refused') or (engine != 'qt' and outcome == 'blocked')) and not rows(start, path='/sw.js'),
                  (outcome, len(rows(start, path='/sw.js'))))

        def recent():
            mark = len(SEEN)
            value = call('inspect_library recent', lambda: adapter.inspect_library(category='recent'))
            check('the recent list is complete on its one page',
                  value['complete'] is True and value['membership_verified'] is True and numbered(value) == expected('RECENT')
                  and value['pages'] == [{'page': 1, 'series_count': 24}] and value['restore_succeeded'] is None, outline(value))
            moved = adapter.page.evaluate('() => window.scrollY')
            check('the wheel scrolls that list, as a real wheel to the page', moved > 0 and root.get_attribute('data-wheel') == 'true',
                  (moved, root.get_attribute('data-wheel')))
            check('the site saw the recent tab opened', visits(mark) == ['/', '/reading-list', '/reading-list?category=RECENT'], visits(mark))

        def updated():
            mark = len(SEEN)
            value = call('inspect_library updated', lambda: adapter.inspect_library(category='updated'))
            check('the updated list is complete over its two pages',
                  value['complete'] is True and value['membership_verified'] is True and numbered(value) == expected('UPDATED')
                  and value['pages'] == [{'page': 1, 'series_count': 4}, {'page': 2, 'series_count': 4}]
                  and value['restore_succeeded'] is True, outline(value))
            # The updated tab leads to the address the list already has: the same page is opened twice running.
            wanted = ['/', '/reading-list', '/reading-list', '/reading-list?pageNumber=2&type=ALL&category=UPDATED', '/reading-list']
            check('the site saw the updated tab, its next page and the way back', visits(mark) == wanted, visits(mark))

        def free_episodes():
            mark, handed = len(SEEN), []
            value = call('inspect_library free_episodes', lambda: adapter.inspect_library(category='free_episodes', on_page=handed.append))
            RESULTS['on_page free_episodes'] = plain(handed)
            check('the Free Episodes filter is followed to its end',
                  value['complete'] is True and value['filter_clicked'] is True and value['membership_verified'] is True
                  and value['pagination_complete'] is True and numbered(value) == expected('FREE_EPISODES')
                  and value['pages'] == [{'page': 1, 'series_count': 3}, {'page': 2, 'series_count': 3}] and 'blocker' not in value,
                  outline(value))
            check('its selected state and its meaning are read from the page',
                  value.get('selection_structure') == {'classes': ['filter-item', 'on'], 'aria_selected': False, 'aria_current_page': False}
                  and value.get('public_type_code') == 'FREE_EPISODES' and value.get('filter_query_field') == 'category'
                  and value.get('scope') == 'free_episodes' and value.get('matching_control_count') == 1, outline(value))
            check('each filtered page is handed over once',
                  [len(page['series']) for page in handed] == [3, 3] and all(page['scope'] == 'free_episodes' for page in handed),
                  [len(page['series']) for page in handed])
            labels = value.get('view_semantics') or {}
            check('the labels shown on the filtered list are counted',
                  labels.get('available') is True and labels['label_node_counts'].get('free episodes', 0) >= 1
                  and labels['label_node_counts'].get('episode_count_label', 0) >= 1
                  and labels['attribute_value_types'] == {'data-free-episode-count': 'integer'}, labels)
            check('the unfiltered list is restored afterwards', value['restore_succeeded'] is True and adapter.page.url == first,
                  (value['restore_succeeded'], adapter.page.url))
            found = [row for row in SEEN[mark:] if 'FREE_EPISODES' in row['target']]
            check('the filter and its next page were reached by real clicks',
                  clicked(found, [first, ORIGIN + '/reading-list?category=FREE_EPISODES&type=ALL']), shown(found))

        def series_reads():
            mark, before = len(SEEN), adapter.page.url
            value = call('series_identity', lambda: adapter.series_identity('story-101'))
            check('a series is identified from its details page', value == {'source_series_id': '101', 'title': 'Story 101'}, value)
            found = call('series_metadata', lambda: adapter.series_metadata('story-101'))
            check('its creators, description, credits and cover are read',
                  found.creators == ('Studio & Artist 101',) and found.description == 'The story of 101.\nIts second line.'
                  and found.credits == 'Created by Artist\nTranslated by Translator' and found.platform_id == '101'
                  and found.slug == 'story-101' and found.cover_url == 'https://us-a.tapas.io/sa/cover/101.jpg'
                  and found.source_url == ORIGIN + '/series/story-101/info', plain(found))
            reads = rows(mark, path='/series/story-101/info')
            if engine == 'qt':
                check('the details page is read from inside the page, which stays where it was',
                      len(reads) == 3 and all(row['dest'] == 'empty' and row['accept'] == 'text/html' and row['signed'] for row in reads)
                      and adapter.page.url == before, ([(row['dest'], row['accept'], row['signed']) for row in reads], adapter.page.url))
            else:
                check('the details page is visited', len(reads) == 3 and all(row['dest'] == 'document' and row['signed'] for row in reads),
                      [(row['dest'], row['signed']) for row in reads])
            mark = len(SEEN)
            value = call('inspect_series', lambda: adapter.inspect_series(str(DETAILED)))
            listed = value['episodes']
            check('every episode of a series is listed in order with its access',
                  value['complete'] is True and value['episode_count'] == 25 and value['ordering_verified'] is True
                  and value['accessible_episode_count'] == 5 and [row['id'] for row in listed] == [str(number) for number in EPISODES]
                  and [row['access_state'] for row in listed[:5]] == ['tapas_free'] * 3 + ['tapas_unlocked'] * 2
                  and [row['authorization_reason'] for row in listed[5:8]] == ['access_denied', 'scheduled', 'access_denied']
                  and listed[3]['access_expires_at'] == '2099-01-01T00:00:00+00:00',
                  {key: item for key, item in value.items() if key != 'episodes'})
            asked = [row['target'] for row in rows(mark, path=f'/series/{DETAILED}/episodes')]
            check('the episode list is asked for page by page',
                  asked == [f'/series/{DETAILED}/episodes?page={page}&max_limit=20' for page in (1, 2)], asked)
            value = call('episode_access', lambda: adapter.episode_access(f'{ORIGIN}/episode/{EPISODES[0]}'))
            check("an episode's access is read",
                  value == {'source_episode_id': str(EPISODES[0]), 'access_state': 'tapas_free', 'access_expires_at': None}, value)

        def refusals():
            error = refused('episode_access challenged', lambda: adapter.episode_access(f'{ORIGIN}/episode/{CHALLENGED}'))
            check('a visitor check is reported as a browser challenge',
                  type(error) is ArchiveError and 'browser challenge' in str(error) and getattr(error, 'source_http_status', None) == 403
                  and getattr(error, 'source_response_kind', None) == 'challenge', f'{type(error).__name__}: {error}')
            mark = len(SEEN)
            error = refused('series_identity redirected', lambda: adapter.series_identity('moved'))
            followed = rows(mark, path='/series/story-999/info')
            # A page visit follows the redirect before the adapter refuses it; a read from inside the page never follows it.
            check('a details page that redirects is refused' + (', and not followed' if engine == 'qt' else ''),
                  type(error) is ArchiveError and len(rows(mark, path='/series/moved/info')) == 1 and (engine != 'qt' or not followed),
                  (f'{type(error).__name__}: {error}', len(followed)))

        def failed_reads():
            # The pauses between tries are shortened for this check; nothing else of the adapter is changed.
            tapas.REPEAT_PAUSES = (2, 2, 2)
            failed = r'ReadFailed: no answer after \d+ ms \(TypeError; read over [a-z0-9./-]+; site over [a-z0-9./-]+\)'
            said = io.StringIO()
            with contextlib.redirect_stdout(said):
                value = call('episode_access cut off once', lambda: adapter.episode_access(f'{ORIGIN}/episode/{CUT_ONCE}'))
            lines = said.getvalue().splitlines()
            check('a read the site cuts off once is read at a further try', value.get('source_episode_id') == str(CUT_ONCE), value)
            check('the log says how far that read got and over which protocol',
                  len(lines) == 1 and re.fullmatch(f'Browser read failed: {failed}; trying again in 2 s\\.', lines[0]), lines)
            note('a failed read in the log: ' + (lines[0] if lines else 'nothing'))
            said = io.StringIO()
            with contextlib.redirect_stdout(said):
                error = refused('episode_access always cut off', lambda: adapter.episode_access(f'{ORIGIN}/episode/{CUT}'))
            lines = said.getvalue().splitlines()
            check('a read the site always cuts off is given up after three further tries, as an unreachable site',
                  type(error) is NetworkUnavailable and getattr(error, 'browser_failure_code', None) == 'browser_error'
                  and len(lines) == 4 and re.fullmatch(f'Browser request failed: {failed}', lines[-1]),
                  (f'{type(error).__name__}: {error}', lines))

        def capture():
            value = call('capture_automatic', lambda: adapter.capture_automatic(f'{ORIGIN}/episode/{EPISODES[0]}'))
            taken = value.episodes[0]
            check('an episode is captured with its pages in order',
                  [page.location for page in taken.pages] == pages_of(EPISODES[0]) and taken.access == 'tapas_free' and taken.order == 1
                  and taken.content_type == 'comic' and value.series.platform_id == '101' and value.series.slug == 'story-101',
                  (len(taken.pages), taken.access, taken.order, value.series.platform_id))
            saved = json.loads((project / 'session' / 'state.json').read_text())
            cookies = {cookie['name']: cookie for cookie in saved['cookies']}
            stored = {row['name']: row['value'] for entry in saved['origins'] if entry['origin'] == ORIGIN for row in entry['localStorage']}
            check('the session it saves still has every cookie',
                  EXPECTED_COOKIES <= set(cookies) and cookies['session']['value'] == SESSION and cookies['keep']['value'] == KEEP,
                  sorted(cookies))
            before = [described(cookie) for cookie in saved_cookies(SESSION)]
            after = [described(cookies[row[0]]) for row in before if row[0] in cookies]
            check('each cookie it was given is saved as it was: domain, path, limits and lifetime', after == before, after)
            check('the session it saves still has the page storage', stored == {'saved-entry': 'kept', 'page-entry': 'written'}, sorted(stored))
            RESULTS['saved session'] = {'cookies': sorted(described(cookie) for cookie in saved['cookies']),
                                        'storage': sorted(stored.items())}

        def session():
            cookies = {cookie['name']: cookie for cookie in adapter.context.cookies()}
            check('cookies the site set, or set again unchanged, are all still held',
                  EXPECTED_COOKIES <= set(cookies) and cookies['keep']['value'] == KEEP and cookies['session']['value'] == SESSION,
                  sorted(cookies))
            carried = sorted(part.split('=', 1)[0] for part in adapter.transport.assets.cookie_header('tapas.io').split('; ') if part)
            RESULTS['image downloader cookies'] = carried
            check('the image downloader is given those cookies, and was never used here',
                  EXPECTED_COOKIES <= set(carried) and not ASSET_CALLS, (carried, len(ASSET_CALLS)))
            hosts = sorted({row['host'] for row in SEEN[start:]})
            writes = [(row['method'], row['target'], row['part'], row['referer']) for row in SEEN[start:] if row['method'] != 'GET']
            if engine == 'qt':
                check('only reads of tapas.io reached the site', hosts == ['tapas.io'] and not writes, (hosts, writes))
            else:
                check('only tapas.io was asked for', hosts == ['tapas.io'], hosts)
                # A fault of the Firefox path, found by this script and outside what it judges: a write that a page
                # sends just as the adapter exchanges its request rules between two calls is let through.
                note(f"KNOWN FAULT, not counted: {len(writes)} of the pages' own writes reached the site under Firefox"
                     + (': ' + '; '.join(f'{method} {target} from {referer} during {part}' for method, target, part, referer in writes)
                        if writes else ''))

        for name, body in (('identity', identity), ('subscribed', subscribed), ('own scripts', own_scripts), ('recent', recent),
                           ('updated', updated), ('free episodes', free_episodes), ('series reads', series_reads),
                           ('refusals', refusals), ('failed reads', failed_reads), ('capture', capture), ('session', session)):
            group(name, body)

    def signed_out(adapter):
        mark = len(SEEN)
        error = refused('account_identity signed out', adapter.account_identity)
        asked = rows(mark, path='/who-am-i')
        check('a session the site no longer knows is told to sign in again',
              type(error) is AuthenticationError and len(asked) == 1 and not asked[0]['signed'],
              (f'{type(error).__name__}: {error}', len(asked)))

    for name, folder, body in (('the adapter', 'session', work), ('a second adapter', 'stale', signed_out)):
        PART[0] = name
        try:
            with TapasAdapter(project / folder, preferred_browser=engine, headless=True) as adapter:
                body(adapter)
        except Exception as error:
            check(f'{name} opened and closed', False, f'{type(error).__name__}: {error}')
        else:
            check(f'{name} opened and closed', True)
    # The redirect's target is left out: only a page visit follows it, as the check above says. So are the reads
    # the stand-in cut off: how often an engine asks again by itself on a dropped connection is its own affair.
    # Writes are judged above, for each engine by itself.
    RESULTS['reads seen by the site'] = [row['target'] for row in SEEN[start:] if row['method'] == 'GET'
                                         and row['path'] not in {'/favicon.ico', '/series/story-999/info'}
                                         and not re.fullmatch(f'/episode/({CUT_ONCE}|{CUT})(/info)?', row['path'])]
    fast_import(engine, project)


def fast_import(engine, project):
    """The Fast speed: a second signed-in session beside the first, driven from a thread of its own, as in the downloader."""
    from comic_archive.adapters.tapas import TapasAdapter
    from comic_archive.fast_import import FastWorkerSession
    PART[0] = 'the fast importer'
    url = f'{ORIGIN}/episode/{EPISODES[0]}'
    try:
        with TapasAdapter(project / 'session', preferred_browser=engine, headless=True) as adapter, \
                FastWorkerSession(project / 'session', engine, headless=True) as session:
            for batch in ('first', 'second'):
                mark = len(SEEN)
                with session.batch() as fast:
                    account = call(f'fast account_identity, {batch} batch', fast.account_identity)
                    value = call(f'fast capture_automatic, {batch} batch', lambda: fast.capture_automatic(url))
                taken = value.episodes[0]
                check(f'the fast importer reads the account and captures an episode in its {batch} batch',
                      account == str(ACCOUNT) and [page.location for page in taken.pages] == pages_of(EPISODES[0])
                      and taken.access == 'tapas_free' and value.series.platform_id == '101', (account, len(taken.pages), taken.access))
                asked = rows(mark, path='/who-am-i')
                writes = [(row['method'], row['target']) for row in SEEN[mark:] if row['method'] != 'GET']
                check(f'its reads in the {batch} batch were signed, and it wrote nothing',
                      bool(asked) and all(row['signed'] for row in asked) and (engine != 'qt' or not writes), (len(asked), writes))
            mark = len(SEEN)
            check('the first session still answers beside it',
                  adapter.account_identity() == str(ACCOUNT) and all(row['signed'] for row in rows(mark, path='/who-am-i')))
    except Exception as error:
        check('the fast importer opened and closed beside the first session', False, f'{type(error).__name__}: {error}')
    else:
        check('the fast importer opened and closed beside the first session', True)


def child(engine, out):
    """One engine's run, in a process of its own: its own stand-in, its own project folder."""
    global ENGINE
    ENGINE = engine
    with tempfile.TemporaryDirectory(prefix='tapas-adapter-check-', dir='/dev/shm' if os.path.isdir('/dev/shm') else None) as work:
        work = Path(work)
        port = serve_site(work)
        project = make_project(work)
        os.chdir(project)
        block_image_downloads()
        try:
            if engine == 'qt':
                from comic_archive import qt_engine
                accept_certificates()
                # Every name leads to the stand-in, so nothing can leave this computer.
                qt_engine.serve(lambda: exercise(engine, project),
                                arguments=[f'--host-resolver-rules=MAP * 127.0.0.1:{port}', '--ignore-certificate-errors'])
                note(f'certificate questions Qt put to the script: {len(ACCEPTED)}')
            else:
                through_proxy(tunnel(port))
                exercise(engine, project)
                note('names Firefox asked the proxy for: ' + ', '.join(sorted(set(TUNNELLED))))
        except Exception as error:
            check('the run reached its end', False, f'{type(error).__name__}: {error}')
        os.chdir('/')
        Path(out).write_text(json.dumps({'checks': CHECKS, 'results': RESULTS}))
    return 0


def differences(ours, theirs, where=''):
    """Each place where two results differ, with both values."""
    if isinstance(ours, dict) and isinstance(theirs, dict):
        for key in sorted(set(ours) | set(theirs)):
            yield from differences(ours.get(key, '<absent>'), theirs.get(key, '<absent>'), f'{where}.{key}')
    elif isinstance(ours, list) and isinstance(theirs, list) and len(ours) == len(theirs):
        for index, pair in enumerate(zip(ours, theirs)):
            yield from differences(*pair, f'{where}[{index}]')
    elif isinstance(ours, list) and isinstance(theirs, list):
        # Lists of unequal length: where they part, and what follows there.
        index = next((index for index, pair in enumerate(zip(ours, theirs)) if pair[0] != pair[1]), min(len(ours), len(theirs)))
        yield f'{where}[{index}:] of {len(ours)} and {len(theirs)}', ours[index:index + 4], theirs[index:index + 4]
    elif ours != theirs:
        yield where or '.', ours, theirs


def main():
    options = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    options.add_argument('--engine', choices=('qt', 'firefox'), help=argparse.SUPPRESS)
    options.add_argument('--out', help=argparse.SUPPRESS)
    chosen = options.parse_args()
    if chosen.engine:
        return child(chosen.engine, chosen.out)
    runs, passed, total = {}, 0, 0
    with tempfile.TemporaryDirectory(prefix='tapas-adapter-check-', dir='/dev/shm' if os.path.isdir('/dev/shm') else None) as work:
        for engine in ('qt', 'firefox'):
            out = Path(work) / f'{engine}.json'
            # A group of its own, so that a run that hangs is stopped together with the browser it started.
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--engine', engine, '--out', str(out)],
                                       start_new_session=True)
            try:
                ended = f'ended with status {process.wait(timeout=LIMIT)}'
            except subprocess.TimeoutExpired:
                ended = f'was stopped after {LIMIT} seconds'
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
            process.wait()
            runs[engine] = json.loads(out.read_text()) if out.is_file() else None
            finished = runs[engine] is not None
            print(('PASS ' if finished else 'FAIL ') + f'{engine}: the run finished' + ('' if finished else f'  [it {ended}]'), flush=True)
            checks = runs[engine]['checks'] if finished else []
            passed += sum(1 for _, good in checks if good) + finished
            total += len(checks) + 1
    ours, theirs = (runs[engine]['results'] if runs[engine] else {} for engine in ('qt', 'firefox'))
    for name in list(ours) + [name for name in theirs if name not in ours]:
        total += 1
        if name not in ours or name not in theirs:
            print(f'DIFFERENT {name}  [not made on {"qt" if name not in ours else "firefox"}]', flush=True)
            continue
        found = list(differences(ours[name], theirs[name]))
        passed += not found
        print(('DIFFERENT ' if found else 'EQUAL ') + name, flush=True)
        for where, mine, other in found[:8]:
            print(f'    {where}\n      qt:      {json.dumps(mine)[:400]}\n      firefox: {json.dumps(other)[:400]}', flush=True)
    if not ours or not theirs:
        total += 1
        print('DIFFERENT nothing could be compared: ' + ' and '.join(
            engine for engine in ('qt', 'firefox') if not (runs[engine] or {}).get('results')) + ' returned no results', flush=True)
    print(f'ADAPTER CHECK {passed} of {total} passed', flush=True)
    return 0 if passed == total else 1


if __name__ == '__main__':
    raise SystemExit(main())
