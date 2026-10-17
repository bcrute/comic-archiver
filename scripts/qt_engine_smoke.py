#!/usr/bin/env python3
"""Check the app's built-in engine against a local stand-in site, with the real Qt web engine.

Each check prints PASS or FAIL with its name; the last line is 'SMOKE n of m passed'
and the exit status is 0 only when every check passed. Nothing here contacts
the internet. Needs the desktop extra (PySide6); run it on the build fleet:

    python scripts/qt_engine_smoke.py
"""
import http.server
import json
import os
import re
import signal
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from comic_archive import qt_engine  # noqa: E402
from comic_archive.adapters.tapas import read_only_source_request  # noqa: E402

SEEN = []
RESULTS = []
HOME = '''<!doctype html><html><head><title>home</title>
<script>document.documentElement.dataset.stored = String(localStorage.getItem('saved'));
window.pageGlobal = 1;</script>
<script src="/app.js"></script>
<style>body{margin:0} #next,#far,#covered,#probe{display:block;width:200px;height:40px}
#cover{position:absolute;left:0;top:80px;width:400px;height:60px;background:#888}</style></head><body>
<a id="next" href="/page2">Next</a>
<a id="covered" href="/page3" style="position:absolute;left:0;top:90px">Covered</a><div id="cover"></div>
<button id="probe" style="position:absolute;left:0;top:160px">Probe</button>
<ul id="rows" style="margin-top:220px">
<li data-id="1"><a class="row" href="/series/1">First</a></li>
<li data-id="2"><a class="row" href="/series/2"> Second </a></li>
<li data-id="3"><a class="row" href="/series/3">Third</a></li></ul>
<p id="hidden" style="display:none">hidden</p><p id="invisible" style="visibility:hidden">invisible</p>
<img id="eager" src="/img.png" width="10" height="10">
<div style="height:3000px"></div>
<a id="far" href="/page3">Far</a>
<script>
document.addEventListener('click', e => sessionStorage.setItem('click', JSON.stringify([e.isTrusted, e.target.id])), true);
document.addEventListener('wheel', e => { document.documentElement.dataset.wheel = String(e.isTrusted); }, true);
document.getElementById('probe').addEventListener('click', () => {
  document.documentElement.dataset.ours = typeof window.comicArchive; });
fetch('/json'); fetch('/missing.json');
</script></body></html>'''
PAGE2 = (b'<!doctype html><html><head><title>two</title><script>document.documentElement.dataset.click ='
         b' String(sessionStorage.getItem("click"))</script></head><body><p id="two">second page</p></body></html>')
ALERT = (b'<!doctype html><html><body><script>alert(1); const yes = confirm("x"); const text = prompt("y");'
         b' document.documentElement.dataset.after = "done " + yes + " " + text;</script></body></html>')
WORKER = (b'<!doctype html><html><body><script>document.documentElement.dataset.sw = "pending";'
          b'navigator.serviceWorker.register("/sw.js").then(() => { document.documentElement.dataset.sw = "registered"; },'
          b' e => { document.documentElement.dataset.sw = "refused " + e.name; })</script></body></html>')
LEAVE = (b'<!doctype html><html><body><button id="stay" style="display:block;width:200px;height:40px">Stay</button>'
         b'<script>addEventListener("beforeunload", event => { event.preventDefault(); event.returnValue = "stay"; });'
         b'</script></body></html>')
# The wanted link lies over another and moves away as the pointer reaches it.
SHY = (b'<!doctype html><html><head><style>body{margin:0} a{display:block;position:absolute;left:0;top:0;width:200px;'
       b'height:40px;background:#ccc}</style></head><body><a id="decoy" href="/page3">Decoy</a><a id="shy" href="/page2">Shy</a>'
       b'<script>const shy = document.getElementById("shy");'
       b'shy.addEventListener("mouseenter", () => { shy.style.top = "200px"; }, {once: true});</script></body></html>')
STORE = b'<!doctype html><html><body><script>localStorage.setItem("written", "yes")</script></body></html>'
PNG = bytes.fromhex('89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c626001000000050001'
                    '0d0a2db40000000049454e44ae426082')
READ = """async ({url, redirect}) => {
    const response = await fetch(url, {method: 'GET', credentials: 'same-origin', redirect, headers: {Accept: 'application/json'}});
    const text = await response.text();
    return {status: response.status, text, challenge: response.headers.get('cf-mitigated'),
            retry_after: response.headers.get('Retry-After')};
}"""
SEND = """async method => {
    try { return (await fetch('/mutate', {method, body: method === 'GET' ? undefined : 'x'})).status; }
    catch (error) { return 'refused'; }
}"""
# The writes a page can send without waiting for an answer, and one sent by leaving the page.
QUIET_WRITES = """async () => {
    navigator.sendBeacon('/mutate', 'x');
    fetch('/mutate', {method: 'POST', body: 'x', keepalive: true}).catch(() => {});
    await new Promise(done => setTimeout(done, 400));
    return true;
}"""
FORM = """() => {
    const form = document.createElement('form');
    form.method = 'post';
    form.action = '/mutate';
    document.body.appendChild(form);
    form.submit();
}"""
IMAGE = """name => new Promise(done => {
    const image = new Image();
    image.onload = () => done('loaded');
    image.onerror = () => done('refused');
    image.src = name;
})"""


class Site(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def answer(self, status, kind, body, extra=()):
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        for name, value in extra:
            self.send_header(name, value)
        try:
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            # The engine gave up on this request, as some checks mean it to.
            pass

    def record(self):
        SEEN.append({'method': self.command, 'path': self.path.split('?')[0], 'cookie': self.headers.get('Cookie'),
                     'dest': self.headers.get('Sec-Fetch-Dest'), 'user': self.headers.get('Sec-Fetch-User'),
                     'referer': self.headers.get('Referer'), 'agent': self.headers.get('User-Agent'),
                     'brands': self.headers.get('Sec-CH-UA')})

    def do_POST(self):
        self.record()
        self.rfile.read(int(self.headers.get('Content-Length') or 0))
        self.answer(200, 'application/json', b'{"mutated":true}')

    do_PUT = do_DELETE = do_POST

    def do_GET(self):
        self.record()
        path = self.path.split('?')[0]
        if path == '/':
            self.answer(200, 'text/html', HOME.encode())
        elif path == '/page2':
            self.answer(200, 'text/html', PAGE2)
        elif path == '/page3':
            self.answer(200, 'text/html', b'<!doctype html><title>three</title><p id="three">third page</p>')
        elif path == '/app.js':
            self.answer(200, 'text/javascript', b'window.appLoaded = true;')
        elif path == '/json':
            self.answer(200, 'application/json', json.dumps({'cookie': self.headers.get('Cookie')}).encode(),
                        [('Retry-After', '7'), ('Set-Cookie', 'served=1; Path=/; HttpOnly')])
        elif path == '/forbidden':
            self.answer(403, 'text/html', b'<!doctype html><title>no</title>', [('cf-mitigated', 'challenge')])
        elif path == '/missing':
            self.answer(404, 'text/html', b'<!doctype html><title>gone</title><p>gone</p>')
        elif path == '/redirect':
            self.answer(302, 'text/html', b'', [('Location', '/page2')])
        elif path == '/slow':
            time.sleep(4)
            self.answer(200, 'text/html', b'<!doctype html><title>slow</title>')
        elif path == '/alert':
            self.answer(200, 'text/html', ALERT)
        elif path == '/swpage':
            self.answer(200, 'text/html', WORKER)
        elif path == '/sw.js':
            self.answer(200, 'text/javascript', b'self.addEventListener("fetch", () => {})')
        elif path == '/store':
            self.answer(200, 'text/html', STORE)
        elif path == '/leave':
            self.answer(200, 'text/html', LEAVE)
        elif path == '/shy':
            self.answer(200, 'text/html', SHY)
        elif path == '/cut':
            # The site ends the connection without an answer.
            self.close_connection = True
        elif path == '/drop':
            self.answer(200, 'application/json', b'{}', [('Set-Cookie', 'served=; Path=/; Max-Age=0')])
        elif path.endswith('.png'):
            self.answer(200, 'image/png', PNG)
        else:
            self.answer(404, 'text/plain', b'no')


def check(name, passed, detail=''):
    RESULTS.append(bool(passed))
    print(('PASS ' if passed else 'FAIL ') + name + ('' if passed else f'  [{detail}]'), flush=True)


def raised(action):
    """The name of the engine error an action raises, with its text; '' when it raises none."""
    try:
        action()
    except qt_engine.Error as error:
        return f'{type(error).__name__}: {error}'
    return ''


def settled(read, wanted, seconds=5):
    """read() once it gives the wanted value, or whatever it gives when the time is up."""
    deadline = time.monotonic() + seconds
    while True:
        value = read()
        if value == wanted or time.monotonic() >= deadline:
            return value
        time.sleep(.05)


def group(name, body):
    try:
        body()
    except Exception as error:
        check(f'{name} ran to its end', False, f'{type(error).__name__}: {error}')


def paths(start=0, method=None):
    return [row['path'] for row in SEEN[start:] if method is None or row['method'] == method]


def last(path):
    return next((row for row in reversed(SEEN) if row['path'] == path), {})


def unused_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def work(origin):
    driver = qt_engine.Driver().start()
    browser = driver.qt.launch(headless=True)
    host = urlsplit(origin).hostname
    state = {'cookies': [{'name': 'session', 'value': 'SAVED', 'domain': host, 'path': '/', 'expires': -1,
                          'httpOnly': True, 'secure': False, 'sameSite': 'Lax'},
                         {'name': 'old', 'value': 'EXPIRED', 'domain': host, 'path': '/', 'expires': 1000,
                          'httpOnly': False, 'secure': False, 'sameSite': 'Lax'},
                         # As Firefox saves a cookie whose site named no limit; Chromium refuses that mark unless secure.
                         {'name': 'loose', 'value': 'L', 'domain': host, 'path': '/', 'expires': -1,
                          'httpOnly': False, 'secure': False, 'sameSite': 'None'}],
             'origins': [{'origin': origin, 'localStorage': [{'name': 'saved', 'value': 'RESTORED'}]}]}
    began = time.monotonic()
    context = browser.new_context(storage_state=state, service_workers='block')
    restoring = time.monotonic() - began
    order = []

    def context_rule(route):
        order.append(('context', route.request.method, route.request.resource_type, urlsplit(route.request.url).path))
        route.fallback()

    context.route('**/*', context_rule)
    page = context.new_page()
    home = origin + '/'

    def start():
        check('a new page starts empty', page.url == 'about:blank', page.url)
        response = page.goto(home, wait_until='domcontentloaded')
        first = last('/')
        check('a page load reports its status', response is not None and response.status == 200, response and response.status)
        check('the page address is the loaded one', page.url == home, page.url)
        sent = sorted(str(first.get('cookie')).split('; '))
        check('saved cookies are sent, expired ones are not', sent == ['loose=L', 'session=SAVED'], sent)
        check('a session is restored without waiting for a cookie the engine refuses', restoring < 3, f'{restoring:.1f}s')
        check('a page load is an ordinary navigation', first.get('dest') == 'document', first.get('dest'))
        check('saved page storage is back before the page scripts run',
              page.locator('html').get_attribute('data-stored') == 'RESTORED', page.locator('html').get_attribute('data-stored'))
        agent = str(first.get('agent'))
        print('NOTE agent ' + agent, flush=True)
        print('NOTE brands ' + str(first.get('brands')), flush=True)
        check("the engine names itself as the browser it is built from, without Qt's tag",
              'Qt' not in agent and re.search(r' Chrome/\d+\.[\d.]+ Safari/[\d.]+$', agent) is not None, agent)
        check('nothing else in a page request names Qt', 'qt' not in str(first.get('brands')).lower(), first.get('brands'))
        check('a page script is told the same name', page.evaluate('navigator.userAgent') == agent, page.evaluate('navigator.userAgent'))
        check('requests made beside the engine are given the same name', context.agent == agent, context.agent)

    def statuses():
        check('a missing page reports 404', page.goto(origin + '/missing', wait_until='domcontentloaded').status == 404)
        check('a refused page reports 403', page.goto(origin + '/forbidden', wait_until='domcontentloaded').status == 403)
        response = page.goto(origin + '/redirect', wait_until='domcontentloaded')
        check('a redirect is followed to its page', response.status == 200 and page.url == origin + '/page2', page.url)
        began = time.monotonic()
        nowhere = f'http://127.0.0.1:{unused_port()}/'
        failure = raised(lambda: page.goto(nowhere, wait_until='domcontentloaded'))
        check('a site that cannot be reached is an error, promptly',
              failure.startswith('Error:') and time.monotonic() - began < 10, f'{failure} after {time.monotonic() - began:.1f}s')
        check('a page that failed to load is not where the page is', not page.url.startswith(nowhere), page.url)
        failure = raised(lambda: page.goto(origin + '/slow', wait_until='domcontentloaded', timeout=700))
        check('a page that is too slow is a timeout', failure.startswith('TimeoutError:'), failure)
        check('a page that has yet to arrive is not where the page is', not page.url.startswith(origin + '/slow'), page.url)
        check('a page loads after a failed one', page.goto(home, wait_until='domcontentloaded').status == 200)
        check('a full load waits for the page to complete', page.goto(home).status == 200)

    def scripts():
        check('a script returns its value', page.evaluate('() => 1 + 1') == 2)
        check('a script takes an argument', page.evaluate('({a, b}) => a + b', {'a': 2, 'b': 3}) == 5)
        check('a plain expression is its value', page.evaluate('document.title') == 'home')
        check('no value is None', page.evaluate('() => undefined') is None)
        value = page.evaluate(READ, {'url': origin + '/json', 'redirect': 'error'})
        check('a signed-in read returns status, text and headers',
              value['status'] == 200 and json.loads(value['text'])['cookie'].startswith('session=SAVED') and value['retry_after'] == '7',
              {key: value[key] for key in ('status', 'retry_after')})
        value = page.evaluate(READ, {'url': origin + '/forbidden', 'redirect': 'error'})
        check('a visitor check is told apart', value['status'] == 403 and value['challenge'] == 'challenge', value)
        failure = raised(lambda: page.evaluate(READ, {'url': origin + '/redirect', 'redirect': 'error'}))
        check('a refused redirect is an error naming its kind only', failure == 'Error: The page script failed (TypeError).', failure)
        value = page.evaluate(READ, {'url': origin + '/redirect', 'redirect': 'manual'})
        check('a redirect that is not followed has status 0', value['status'] == 0, value['status'])
        failure = raised(lambda: page.evaluate('() => { throw new RangeError("PRIVATE") }'))
        check('a failing script is an error without its text', failure == 'Error: The page script failed (RangeError).', failure)
        failure = raised(lambda: page.evaluate('() => new Promise(() => {})', timeout=400))
        check('a script that never finishes is a timeout', failure.startswith('TimeoutError:'), failure)
        began = time.monotonic()
        failure = raised(lambda: page.evaluate("() => new Promise(() => { location.href = '/page2'; })"))
        check('a script whose page is replaced is an error, promptly',
              failure.startswith('Error:') and time.monotonic() - began < 10, f'{failure} after {time.monotonic() - began:.1f}s')
        page.goto(home, wait_until='domcontentloaded')
        check('the page cannot be seen into from its own scripts and back',
              page.evaluate('() => typeof window.pageGlobal') == 'undefined')
        page.locator('#probe').click(timeout=5000)
        ours = settled(lambda: page.locator('html').get_attribute('data-ours'), 'undefined')
        check('the page cannot see the engine', ours == 'undefined', ours)

    def rules():
        del order[:]

        def page_rule(route):
            order.append(('page', route.request.method, route.request.resource_type, urlsplit(route.request.url).path))
            route.fallback()

        page.route('**/*', page_rule)
        page.goto(home)
        page.wait_for_timeout(300)
        kinds = {row[2] for row in order}
        check('requests are named by kind', {'document', 'script', 'image', 'xhr'} <= kinds, sorted(kinds))
        first = [row[0] for row in order if row[3] == '/app.js']
        check("the page's rule is asked before the session's", first == ['page', 'context'], first)
        page.unroute('**/*', page_rule)
        page.route('**/*', read_only_source_request)
        mark = len(SEEN)
        check('a write is refused', page.evaluate(SEND, 'POST') == 'refused')
        check('a delete is refused', page.evaluate(SEND, 'DELETE') == 'refused')
        page.evaluate(QUIET_WRITES)
        check('a refused write never reaches the site, a beacon included', '/mutate' not in paths(mark), paths(mark))
        check('a read still passes', page.evaluate(SEND, 'GET') == 404 and paths(mark, 'GET') == ['/mutate'], paths(mark))
        mark = len(SEEN)
        page.evaluate(FORM)
        page.wait_for_timeout(600)
        check('a form that is sent is refused', '/mutate' not in paths(mark), paths(mark))
        page.goto(home, wait_until='domcontentloaded')
        page.unroute_all(behavior='wait')

        def no_images(route):
            route.abort() if route.request.resource_type == 'image' else route.continue_()

        page.route('**/*', no_images)
        mark = len(SEEN)
        check('a request is refused by its kind', page.evaluate(IMAGE, '/other.png') == 'refused' and not paths(mark), paths(mark))
        page.unroute('**/*', no_images)
        check('a removed rule no longer applies', page.evaluate(IMAGE, '/again.png') == 'loaded')

        def broken(route):
            raise RuntimeError('rule failed')

        page.route('**/*', broken)
        mark = len(SEEN)
        check('a rule that fails refuses the request', page.evaluate(SEND, 'GET') == 'refused' and not paths(mark), paths(mark))
        page.unroute_all(behavior='wait')
        page.route('**/*', lambda route: None)
        check('a rule that decides nothing refuses the request', page.evaluate(SEND, 'GET') == 'refused')
        page.unroute_all(behavior='wait')
        # What the log is given when a read fails: whether a rule of this app refused it. The engine has no word for why the network failed.
        failure = raised(lambda: page.evaluate(READ, {'url': origin + '/cut', 'redirect': 'error'}))
        check('a read the site cuts off is an error that blames no rule',
              failure == 'Error: The page script failed (TypeError).', failure)
        page.route('**/*', lambda route: route.abort())
        failure = raised(lambda: page.evaluate(READ, {'url': origin + '/json', 'redirect': 'error'}))
        check('a read a rule refuses is an error saying so',
              failure == 'Error: The page script failed (TypeError; a request rule refused a read).', failure)
        page.unroute_all(behavior='wait')
        mark = len(SEEN)
        check('without a rule a write passes', page.evaluate(SEND, 'POST') == 200 and paths(mark, 'POST') == ['/mutate'], paths(mark))

    def workers():
        mark = len(SEEN)
        page.goto(origin + '/swpage', wait_until='domcontentloaded')
        root, deadline = page.locator('html'), time.monotonic() + 8
        while root.get_attribute('data-sw') == 'pending' and time.monotonic() < deadline:
            page.wait_for_timeout(100)
        outcome = root.get_attribute('data-sw')
        check('a background worker is refused', str(outcome).startswith('refused') and '/sw.js' not in paths(mark), (outcome, paths(mark)))

    def elements():
        page.goto(home, wait_until='domcontentloaded')
        rows = page.locator('ul#rows li a.row')
        check('elements are counted', rows.count() == 3, rows.count())
        check('one is picked by position', rows.nth(1).inner_text().strip() == 'Second', rows.nth(1).inner_text())
        check('the last is picked', rows.last.get_attribute('href') == '/series/3')
        check('the first is picked, and its parent', rows.first.locator('..').get_attribute('data-id') == '1')
        check('a missing attribute is None', rows.first.get_attribute('data-none') is None)
        links = rows.evaluate_all('els => els.map(e => e.href)')
        check('all are read at once, with full addresses', links == [f'{origin}/series/{n}' for n in (1, 2, 3)], links)
        check('a read of all takes an argument', rows.evaluate_all('(els, add) => els.length + add', 4) == 7)
        check('a search inside a search', page.locator('ul#rows').locator('li').locator('a').count() == 3)
        check('nothing matches nothing', page.locator('#absent').count() == 0)
        shown = [page.locator(name).is_visible() for name in ('#next', '#hidden', '#invisible', '#absent')]
        check('visibility is read as a person sees it', shown == [True, False, False, False], shown)
        failure = raised(lambda: rows.inner_text())
        check('one element is needed where several match', 'strict mode violation: 3 elements' in failure, failure)
        failure = raised(lambda: page.locator('#absent').inner_text(timeout=300))
        check('a missing element is a timeout', failure.startswith('TimeoutError:'), failure)
        check('a text selector is refused', raised(lambda: page.locator('text=Next')).startswith('Error:'))
        check('one element is given to a script', page.locator('body').evaluate('body => body.children.length', timeout=2000) > 3)
        box = page.locator('#next').bounding_box()
        check('an element has a box', box and box['width'] == 200 and box['height'] == 40, box)
        check('a hidden element has none', page.locator('#hidden').bounding_box() is None)

    def clicks():
        page.goto(home, wait_until='domcontentloaded')
        with page.expect_navigation(wait_until='domcontentloaded', timeout=10000) as event:
            page.locator('#next').click(timeout=5000)
        request = last('/page2')
        check('a click leads to the next page', event.value is not None and event.value.status == 200 and page.url == origin + '/page2', page.url)
        check('the click is a real one to the page', page.locator('html').get_attribute('data-click') == '[true,"next"]',
              page.locator('html').get_attribute('data-click'))
        check('the click is a real one to the site', request.get('user') == '?1' and request.get('referer') == home,
              (request.get('user'), request.get('referer')))
        page.goto(home, wait_until='domcontentloaded')
        with page.expect_navigation(wait_until='domcontentloaded', timeout=10000) as event:
            page.locator('#far').click(timeout=5000)
        check('a link below the fold is scrolled to and clicked', page.url == origin + '/page3' and event.value.status == 200, page.url)
        page.goto(home, wait_until='domcontentloaded')
        failure = raised(lambda: page.locator('#covered').click(timeout=700))
        check('a covered link is not clicked', failure.startswith('TimeoutError:') and page.url == home, (failure, page.url))
        page.evaluate("() => sessionStorage.removeItem('click')")
        target = page.locator('#next')
        armed = target.one(qt_engine.GUARD % (qt_engine.AIM, json.dumps(target.one(qt_engine.AIM))))
        box = page.locator('#probe').bounding_box()
        page.mouse.click(box['x'] + 5, box['y'] + 5)
        page.wait_for_timeout(500)
        heard = (page.evaluate("() => sessionStorage.getItem('click')"), page.locator('html').get_attribute('data-ours'))
        check('a press that would land on something else is stopped before the page hears of it',
              armed is True and heard == (None, None) and target.landed() is False, (armed, heard))
        page.goto(origin + '/shy', wait_until='domcontentloaded')
        mark = len(SEEN)
        with page.expect_navigation(wait_until='domcontentloaded', timeout=10000):
            page.locator('#shy').click(timeout=8000)
        check('a link that moves away under the pointer is still the one pressed',
              page.url == origin + '/page2' and '/page3' not in paths(mark), (page.url, paths(mark)))

    def no_navigation():
        page.goto(home, wait_until='domcontentloaded')

        def nothing_happens():
            with page.expect_navigation(wait_until='domcontentloaded', timeout=600):
                pass

        failure = raised(nothing_happens)
        check('a navigation that never comes is a timeout', failure.startswith('TimeoutError:'), failure)

    def responses():
        seen = []
        page.goto(origin + '/page2', wait_until='domcontentloaded')
        page.on('response', seen.append)
        page.goto(home)
        page.wait_for_timeout(600)
        rows = [(urlsplit(r.url).path, r.status, r.request.resource_type, r.request.method) for r in seen]
        for row in (('/', 200, 'document', 'GET'), ('/app.js', 200, 'script', 'GET'), ('/json', 200, 'fetch', 'GET'),
                    ('/missing.json', 404, 'fetch', 'GET'), ('/img.png', 200, 'image', 'GET')):
            check(f'a response is reported: {row[0]} {row[1]}', rows.count(row) == 1, rows)
        check('the page before the listener is not reported', not any(row[0] == '/page2' for row in rows), rows)
        count = len(seen)
        page.wait_for_timeout(200)
        check('nothing is reported twice', len(seen) == count, len(seen) - count)
        check('a response has no contents to read', raised(lambda: seen[0].body()).startswith('Error:') and seen[0].headers == {})
        page.remove_listener('response', seen.append)
        page.goto(origin + '/page2', wait_until='domcontentloaded')
        check('a removed listener hears nothing', len(seen) == count, len(seen) - count)

    def scrolling():
        page.goto(home, wait_until='domcontentloaded')
        page.mouse.move(400, 300)
        for _ in range(3):
            page.mouse.wheel(0, 1200)
            page.wait_for_timeout(200)
        moved = page.evaluate('() => window.scrollY')
        check('the wheel scrolls the page', moved > 0, moved)
        check('the wheel is a real one to the page', settled(lambda: page.locator('html').get_attribute('data-wheel'), 'true') == 'true')
        page.evaluate('() => window.scrollTo(0, 0)')
        far = page.locator('#far')
        far.scroll_into_view_if_needed(timeout=2000)
        box = far.bounding_box()
        check('an element is scrolled into view', box and 0 <= box['y'] <= qt_engine.VIEWPORT[1] - box['height'], box)

    def session():
        names = {cookie['name']: cookie for cookie in context.cookies()}
        check('cookies are listed, with one the site set', {'session', 'served'} <= set(names) and 'old' not in names, sorted(names))
        check('a cookie keeps its flags', names.get('session', {}).get('httpOnly') is True and names.get('served', {}).get('httpOnly') is True
              and names.get('session', {}).get('expires') == -1 and names.get('session', {}).get('domain') == host, names.get('session'))
        page.goto(origin + '/store', wait_until='domcontentloaded')
        state = context.storage_state()
        stored = {row['name']: row['value'] for entry in state['origins'] if entry['origin'] == origin for row in entry['localStorage']}
        check('the session to save has cookies and page storage',
              stored == {'saved': 'RESTORED', 'written': 'yes'} and len(state['cookies']) == len(names), stored)
        check('a restored cookie is saved as it was', names.get('loose', {}).get('sameSite') == 'None'
              and names.get('session', {}).get('sameSite') == 'Lax', (names.get('loose'), names.get('session')))
        page.evaluate("() => fetch('/drop').then(response => response.status)")
        left = sorted(cookie['name'] for cookie in context.cookies())
        check('a cookie the site takes away is no longer listed', left == ['loose', 'session'], left)

    def dialogs():
        response = page.goto(origin + '/alert', wait_until='domcontentloaded', timeout=8000)
        after = page.locator('html').get_attribute('data-after')
        check('a page dialog does not hold the page up', response.status == 200 and after == 'done false null', after)
        page.goto(origin + '/leave', wait_until='domcontentloaded')
        page.locator('#stay').click(timeout=5000)
        failure = raised(lambda: page.goto(home, wait_until='domcontentloaded', timeout=8000))
        check('a page that asks its reader to stay is left all the same', failure == '' and page.url == home, (failure, page.url))

    def recovery():
        page.goto(home, wait_until='domcontentloaded')
        pid = context.loop.call(lambda: context.surface.page.renderProcessPid())
        fields = dict(line.split(':', 1) for line in Path(f'/proc/{pid}/status').read_text().splitlines() if ':' in line)
        filtered = fields.get('Seccomp', '').strip()
        try:
            apart = os.readlink(f'/proc/{pid}/ns/pid') != os.readlink('/proc/self/ns/pid')
        except OSError:
            apart = 'unreadable'
        print(f'NOTE sandbox: page process system-call filter {filtered} (2 is on), process space of its own: {apart}', flush=True)
        check('the page process is confined by the engine', filtered == '2', filtered)
        os.kill(pid, signal.SIGKILL)
        check('a page process that ends is noticed', settled(lambda: context.surface.gone, True) is True)
        check('a page without its process is nowhere', page.url == 'about:blank', page.url)
        check('a script there is an error', raised(lambda: page.evaluate('() => 1')).startswith('Error:'))
        response = page.goto(home, wait_until='domcontentloaded')
        title = page.evaluate('document.title')
        check('the next load brings the page back', response is not None and response.status == 200
              and title == 'home' and last('/').get('cookie') is not None,
              (response and response.status, title, last('/').get('cookie')))

    def second_session():
        def other():
            fresh = browser.new_context()
            sheet = fresh.new_page()
            sheet.goto(origin + '/page2', wait_until='domcontentloaded')
            return sheet.locator('#two').inner_text(), [cookie['name'] for cookie in fresh.cookies()]

        with ThreadPoolExecutor(max_workers=1) as pool:
            text, names = pool.submit(other).result(timeout=60)
        check('a second session works from another thread', text == 'second page', text)
        check('sessions share no cookies', names == [] and last('/page2').get('cookie') is None, (names, last('/page2').get('cookie')))
        check('one page for each session', raised(context.new_page).startswith('Error:'))

    for name, body in (('start', start), ('statuses', statuses), ('scripts', scripts), ('rules', rules), ('workers', workers),
                       ('elements', elements), ('clicks', clicks), ('no navigation', no_navigation), ('responses', responses),
                       ('scrolling', scrolling), ('session', session), ('dialogs', dialogs), ('recovery', recovery),
                       ('second session', second_session)):
        group(name, body)
    browser.close()
    driver.stop()
    return 'finished'


def main():
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f'http://127.0.0.1:{server.server_address[1]}'
    check('work without the engine needs no Qt', qt_engine.serve(lambda: 5) == 5)
    try:
        check('the work ran to its end', qt_engine.serve(lambda: work(origin)) == 'finished')
    except Exception as error:
        check('the work ran to its end', False, f'{type(error).__name__}: {error}')
    server.shutdown()
    print(f'SMOKE {sum(RESULTS)} of {len(RESULTS)} passed', flush=True)
    return 0 if all(RESULTS) else 1


if __name__ == '__main__':
    raise SystemExit(main())
