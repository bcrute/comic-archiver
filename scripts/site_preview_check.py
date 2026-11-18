"""Check generated website navigation and responsive downloads on the build machines."""
import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
from urllib.parse import urlsplit

AGENTS = {
    'android': 'Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 Chrome/153.0.0.0 Mobile Safari/537.36',
    'ios': 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1',
    'ipad': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1',
    'windows': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/153.0.0.0 Safari/537.36',
    'linux': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/153.0.0.0 Safari/537.36',
    'mac': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 13_0) AppleWebKit/605.1.15 Version/18.0 Safari/605.1.15',
    'unknown': 'Website preview browser',
}


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def bounds(page):
    result = page.evaluate('''() => ({width: innerWidth, content: document.documentElement.scrollWidth,
      outside: [...document.querySelectorAll('a,button,summary')].filter(el => {
        const r = el.getBoundingClientRect();
        return r.width && r.height && (r.left < -1 || r.right > innerWidth + 1);
      }).map(el => el.textContent.trim()),
      header: document.querySelector('header.site').getBoundingClientRect().height,
      targets: [...document.querySelectorAll('header.site a,header.site button')].map(el => {
        const r = el.getBoundingClientRect();
        return {text: el.textContent.trim() || el.getAttribute('aria-label'), width: r.width, height: r.height};
      })})''')
    if result['content'] > result['width'] + 1 or result['outside']:
        raise RuntimeError('Content extends outside the viewport: ' + json.dumps(result))
    if page.evaluate('matchMedia("(pointer: coarse)").matches'):
        if any(target['height'] and target['height'] < 44 for target in result['targets']):
            raise RuntimeError('A phone header target is too short: ' + json.dumps(result))
    return result


def allowed(route, port):
    address = urlsplit(route.request.url)
    if address.scheme == 'http' and address.hostname == '127.0.0.1' and address.port == port:
        route.continue_()
    else:
        route.abort()


def main():
    if os.environ.get('CI') != 'true':
        raise RuntimeError('Run website browser checks through the fleet helper.')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('site', type=Path)
    args = parser.parse_args()
    from playwright.sync_api import sync_playwright

    output = Path(os.environ['CI_ARTIFACTS_DIR']) / 'browser'
    output.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(args.site)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = 'http://127.0.0.1:' + str(server.server_port)
    reports = {'layout': [], 'platforms': [], 'keyboard': False, 'physical_device_test': False}
    try:
        with sync_playwright() as driver:
            browser = driver.chromium.launch(channel='chromium')
            try:
                # Include a narrow phone and the CSS viewport of a 1440px window at 200% zoom.
                for width, height in ((320, 740), (375, 812), (390, 844), (720, 450), (768, 1024),
                                      (844, 390), (1024, 768), (1440, 900)):
                    for theme in ('light', 'dark'):
                        context = browser.new_context(viewport={'width': width, 'height': height}, color_scheme=theme,
                                                      user_agent=AGENTS['android'] if width <= 390 else AGENTS['windows'],
                                                      has_touch=width <= 390 or width in (768, 844))
                        try:
                            context.route('**/*', lambda route: allowed(route, server.server_port))
                            page = context.new_page()
                            for file in sorted(args.site.glob('*.html')):
                                response = page.goto(origin + '/' + file.name)
                                if response.status != 200:
                                    raise RuntimeError('Page did not load: ' + file.name)
                                measurement = bounds(page)
                                reports['layout'].append({'page': file.name, 'width': width, 'theme': theme,
                                                          'measurement': measurement})
                                for link in page.locator('a[href]').evaluate_all('(els) => els.map(el => el.getAttribute("href"))'):
                                    target = urlsplit(link)
                                    if not target.scheme and target.path:
                                        local = args.site / target.path
                                        if not local.exists():
                                            raise RuntimeError('Broken local link: ' + file.name + ': ' + link)
                                if width in (320, 375, 1440) and file.name in ('index.html', 'download.html'):
                                    if file.name == 'index.html':
                                        page.locator('.shot img:visible').scroll_into_view_if_needed()
                                        page.locator('.shot img:visible').evaluate('el => el.decode()')
                                        page.evaluate('window.scrollTo(0, 0)')
                                    page.screenshot(path=str(output / (file.stem + '-' + str(width) + '-' + theme + '.png')),
                                                    full_page=True)
                        finally:
                            context.close()
                for key, agent in AGENTS.items():
                    for javascript in (True, False):
                        context = browser.new_context(viewport={'width': 375, 'height': 812}, user_agent=agent,
                                                      java_script_enabled=javascript, has_touch=key in ('android', 'ios', 'ipad'))
                        try:
                            context.route('**/*', lambda route: allowed(route, server.server_port))
                            context.add_init_script("Object.defineProperty(navigator, 'userAgentData', {value: undefined});")
                            if key == 'ipad':
                                context.add_init_script("Object.defineProperty(navigator, 'maxTouchPoints', {value: 5});")
                            page = context.new_page()
                            page.goto(origin + '/index.html')
                            bounds(page)
                            page.locator('a[data-download]').click()
                            page.wait_for_url('**/download.html')
                            bounds(page)
                            expected = ('ios' if key == 'ipad' else key) if javascript else None
                            selected = page.locator('.panel.yours').get_attribute('data-os') if page.locator('.panel.yours').count() else None
                            wanted = expected if expected in ('windows', 'linux', 'mac', 'android') else None
                            if selected != wanted:
                                raise RuntimeError('Incorrect download selected for ' + key)
                            if wanted and page.locator('.panels > .panel').first.get_attribute('data-os') != wanted:
                                raise RuntimeError('The matching download is not first for ' + key)
                            if page.locator('.panel.download:visible').count() != 4:
                                raise RuntimeError('Some platform downloads are inaccessible.')
                            if javascript and expected == 'ios' and not page.locator('.for-device[data-os="ios"]').is_visible():
                                raise RuntimeError('The unsupported-device explanation is missing.')
                            reports['platforms'].append({'agent': key, 'javascript': javascript, 'selected': selected})
                        finally:
                            context.close()
                context = browser.new_context(viewport={'width': 375, 'height': 812})
                try:
                    context.route('**/*', lambda route: allowed(route, server.server_port))
                    page = context.new_page()
                    page.goto(origin + '/index.html')
                    button = page.get_by_role('button', name='Theme: System', exact=True)
                    button.focus()
                    page.keyboard.press('Enter')
                    if not page.locator('.theme-menu').is_visible():
                        raise RuntimeError('The theme menu cannot be opened with the keyboard.')
                    page.get_by_role('button', name='Dark', exact=True).focus()
                    page.keyboard.press('Enter')
                    if page.locator('html').get_attribute('data-theme') != 'dark':
                        raise RuntimeError('The theme cannot be changed with the keyboard.')
                    page.get_by_role('button', name='Theme: Dark', exact=True).click()
                    page.keyboard.press('Escape')
                    if page.locator('.theme-menu').is_visible() or not page.locator('button.theme').evaluate('el => el === document.activeElement'):
                        raise RuntimeError('Escape does not restore theme-button focus.')
                    reports['keyboard'] = True
                finally:
                    context.close()
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        (output / 'report.json').write_text(json.dumps(reports, indent=2) + '\n')
    print(json.dumps({'ok': True, 'layouts': len(reports['layout']), 'platform_cases': len(reports['platforms']),
                      'keyboard': reports['keyboard'], 'physical_device_test': False}), flush=True)


if __name__ == '__main__':
    main()
