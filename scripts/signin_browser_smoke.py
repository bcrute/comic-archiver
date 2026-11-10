"""Exercise production sign-in scripts against a cacheable local identity service."""
from dataclasses import replace
import http.server
import json
import os
from pathlib import Path
import sys
import threading
import time
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from comic_archive import qt_engine, sites  # noqa: E402
from comic_archive.desktop_app import ASKS, SIGNIN_READ, signed_in  # noqa: E402


class IdentitySite(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = urlsplit(self.path).path
        active = 'fixture_signed=1' in self.headers.get('Cookie', '')
        content_type = 'application/json'
        if path == '/who-am-i':
            body = json.dumps({'data': {'user': {'id': 11 if active else -1}}, 'error_details': None})
        elif path == '/en/member/userInfo':
            body = json.dumps({'loginUser': active, 'loginId': 'fixture-account' if active else None})
        elif path == '/en/me/library/unlocked/comics':
            content_type = 'text/html'
            state = {'props': {'initialState': {'oauth2': {'user': {'id': 11 if active else None}}}}}
            body = '<script id="__NEXT_DATA__">' + json.dumps(state) + '</script>'
        elif path == '/':
            content_type = 'text/html'
            body = '<!doctype html><title>Local sign-in fixture</title>'
        else:
            self.send_error(404)
            return
        body = body.encode()
        self.server.observed.append(path)
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        # Deliberately cacheable and without Vary: Cookie: the client must ask
        # for a fresh identity even when a previously anonymous page was cached.
        self.send_header('Cache-Control', 'max-age=3600')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def exercise(origin, server):
    observed = {}
    with qt_engine.Driver() as driver:
        browser = driver.qt.launch(headless=True)
        try:
            for site in sites.SITES:
                context = browser.new_context(service_workers='block')

                def local_only(route):
                    url = urlsplit(route.request.url)
                    if (url.scheme == 'http' and url.hostname == '127.0.0.1'
                            and url.port == server.server_port and route.request.method == 'GET'):
                        route.continue_()
                    else:
                        route.abort()

                context.route('**/*', local_only)
                page = context.new_page()
                page.goto(origin + '/', wait_until='domcontentloaded')
                local_site = replace(site, home=origin)
                before = len(server.observed)
                answers = []
                for active in (False, True, False):
                    if answers:
                        page.wait_for_timeout(3100)  # The real three-second polling limit.
                    value = '1' if active else '0'
                    page.evaluate(f"() => {{ document.cookie = 'fixture_signed={value}; Path=/'; }}")
                    page.evaluate(ASKS[site.key])
                    deadline = time.monotonic() + 8
                    while not page.evaluate("typeof window.comicArchive.answer === 'string'"):
                        if time.monotonic() >= deadline:
                            raise RuntimeError(site.key + ': identity response timed out')
                        page.wait_for_timeout(25)
                    result = signed_in(page.evaluate(SIGNIN_READ), local_site)
                    answers.append(result[0] if result else None)
                expected = [None, 'fixture-account' if site.key == 'webtoon' else '11', None]
                if answers != expected:
                    raise RuntimeError(site.key + ': a cached identity survived a sign-in or sign-out')
                requests = [path for path in server.observed[before:] if path != '/favicon.ico']
                expected_path = {'tapas': '/who-am-i', 'webtoon': '/en/member/userInfo',
                                 'tappytoon': '/en/me/library/unlocked/comics'}[site.key]
                if requests != [expected_path] * 3:
                    raise RuntimeError(site.key + ': identity requests did not reach the expected local route')
                observed[site.key] = {'fresh_guest_login_logout': True, 'identity_requests': len(requests)}
                context.close()
        finally:
            browser.close()
    return observed


def main():
    # Never attach to a development desktop, read a saved session or contact a provider.
    for key in ('DISPLAY', 'WAYLAND_DISPLAY'):
        os.environ.pop(key, None)
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), IdentitySite)
    server.observed = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = qt_engine.serve(lambda: exercise('http://127.0.0.1:' + str(server.server_port), server))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    print(json.dumps({'ok': True, 'providers': result}), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
