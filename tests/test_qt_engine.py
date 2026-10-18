"""The built-in engine without Qt: its bookkeeping, waiting and error handling, with a stand-in for the page.

What needs the real engine (most of class Surface, Loop.open and Loop.close) is covered by
scripts/qt_engine_smoke.py. Here a fake page answers the engine's scripts by the
constants they are made from, and time moves only when something waits.
"""
import json
import re
import signal
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from comic_archive import qt_engine
from comic_archive.adapters.tapas import BrowserReadTransport, TapasAdapter, finish_route
from comic_archive.errors import AuthenticationError
from comic_archive.network import NetworkPolicyHold
from comic_archive.qt_engine import CookieRecords, Error, Request, Route

UNSET = object()
NO_ANSWER = object()
REQUESTS_HEAD = qt_engine.REQUESTS.split('%s')[0]
TAKE_HEAD = qt_engine.TAKE.split('%d')[0]
GUARD_HEAD = qt_engine.GUARD.split('%s')[0]
ONE_HEAD, ONE_TAIL = qt_engine.ONE.split('%s')
ORIGIN = 'https://tapas.io'
QT_AGENT = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) QtWebEngine/6.11.2 Chrome/140.0.0.0 Safari/537.36'
CHROME = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36'


class Clock:
    """Time that moves only when something waits, so waiting costs nothing. Things can be scheduled to happen at a time."""

    def __init__(self):
        self.now, self.sleeps, self.events = 1000.0, [], []

    def monotonic(self):
        return self.now

    def time(self):
        return 1_800_000_000.0 + self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        for event in [event for event in self.events if event[0] <= self.now]:
            self.events.remove(event)
            event[1]()

    def after(self, seconds, action):
        self.events.append((self.now + seconds, action))


class Loop:
    """Qt's thread stood in for by the caller's own."""

    def call(self, function, seconds=30):
        return function()

    def ask(self, begin, seconds):
        box = []
        begin(lambda value=None: box.append(value))
        if not box:
            raise qt_engine.TimeoutError('The page did not answer.')
        return box[0]


def parse_call(code):
    """(the called source, the locator's steps or None, the argument) of a CALL script."""
    source = re.search(r'const made = \((.*?)\);\n    const result = typeof made', code, re.S)[1]
    arguments = re.search(r"\? made\((.*)\) : made;", code)[1]
    steps = None
    if arguments.startswith('find('):
        steps, end = json.JSONDecoder().raw_decode(arguments, len('find('))
        arguments = arguments[end + 3:]
    return source, steps, json.loads(arguments) if arguments else None


class Site:
    """The page: answers each of the engine's scripts the way the page would, from what a test has set."""

    def __init__(self):
        self.document = {'id': 1.0, 'url': 'about:blank', 'complete': True, 'ready': True, 'status': 0}
        self.rows = []
        self.storage = 'null'
        self.reply = lambda source, steps, argument: {'value': None}
        self.pending = {}
        self.raw, self.silent = UNSET, 0
        self.scripts, self.calls = [], []

    def navigate(self, url, status=200, ready=True, complete=True):
        self.document = {'id': self.document['id'] + 1, 'url': url, 'complete': complete, 'ready': ready, 'status': status}
        self.rows = []

    def __call__(self, code):
        self.scripts.append(code)
        if self.silent:
            self.silent -= 1
            return NO_ANSWER
        if self.raw is not UNSET:
            return self.raw
        if code == qt_engine.WHERE:
            return json.dumps(self.document)
        if code.startswith(REQUESTS_HEAD):
            known, start = re.search(r'timeOrigin === (.+?) \? (\d+) : 0', code).groups()
            start = int(start) if json.loads(known) == self.document['id'] else 0
            return json.dumps({'id': self.document['id'], 'url': self.document['url'], 'status': self.document['status'],
                               'rows': self.rows[start:]})
        if code == qt_engine.STORAGE:
            return self.storage
        if code.startswith(TAKE_HEAD):
            texts = self.pending.get(int(re.search(r'id = (\d+)', code)[1]), [])
            return texts.pop(0) if texts else ''
        assert 'const made = (' in code, code[:200]
        self.calls.append(parse_call(code))
        answer = self.reply(*self.calls[-1])
        return json.dumps(answer) if isinstance(answer, dict) else answer


class Surface:
    """What class Surface offers the rest of the engine, with a record of what it was asked."""

    def __init__(self, clock, site):
        self.site, self.log = site, []
        self.cookies = CookieRecords(clock.monotonic)
        self.failures, self.gone, self.decide = 0, False, None
        self.failure = None
        self.notes = []
        self.on_load, self.keeps_cookies = None, True
        # A load here ends at once unless a test lets it take its time.
        self.ended, self.ends = 0, True
        self.agent = CHROME

    def script(self, code, answer):
        value = self.site(code)
        if value is not NO_ANSWER:
            answer(value)

    def load(self, url):
        self.log.append(('load', url))
        # Loading gives a page whose process has ended a new one.
        self.gone = False
        if self.ends:
            self.ended += 1
        if self.on_load:
            self.on_load(url)

    def pointer(self, kind, x, y):
        self.log.append(('pointer', kind, x, y))

    def wheel(self, x, y, delta_x, delta_y):
        self.log.append(('wheel', x, y, delta_x, delta_y))

    def set_cookie(self, record):
        self.log.append(('cookie', record['name']))
        if self.keeps_cookies:
            # Qt's records always have a path.
            self.cookies.added({**record, 'path': record.get('path') or '/'})

    def restore_storage(self, origins):
        self.log.append(('restore', origins))

    def storage_restored(self):
        self.log.append(('restored',))

    def close(self):
        self.log.append(('close',))

    def loads(self):
        return [entry[1] for entry in self.log if entry[0] == 'load']


class EngineCase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        patcher = patch.object(qt_engine, 'time', self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.site, self.loop = Site(), Loop()
        self.surface = Surface(self.clock, self.site)

    def context(self, state=None, **options):
        self.context_ = qt_engine.Context(self.loop, state, surface=self.surface, **options)
        return self.context_

    def open(self, state=None):
        """A page in a context; the page has loaded its empty first document."""
        self.page = self.context(state).new_page()
        return self.page

    def elapsed(self, since):
        return self.clock.now - since


def request(url=ORIGIN + '/series/1', method='GET', kind='document'):
    return Request(url, method, kind)


class CookieRecordsTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.records = CookieRecords(lambda: self.now)

    def cookie(self, value='1', name='a', path='/', **fields):
        return {'name': name, 'value': value, 'domain': '.tapas.io', 'path': path, **fields}

    def listed(self):
        return [(record['name'], record['value']) for record in self.records.current()]

    def test_a_cookie_that_is_added_is_listed(self):
        self.records.added(self.cookie())
        self.assertEqual(self.listed(), [('a', '1')])

    def test_a_removal_is_held_back_for_the_grace_and_real_once_it_is_over(self):
        self.records.added(self.cookie())
        self.records.removed(self.cookie())
        self.assertEqual(self.listed(), [('a', '1')])
        self.now += self.records.GRACE / 2
        self.assertEqual(self.listed(), [('a', '1')])
        self.now += self.records.GRACE
        self.assertEqual(self.listed(), [])

    def test_a_cookie_removed_twice_with_the_same_value_is_still_there_with_the_newer_record(self):
        # What Qt reports when a site sets a cookie again unchanged.
        self.records.added(self.cookie(expires=10))
        self.records.removed(self.cookie(expires=10))
        self.records.removed(self.cookie(expires=20))
        self.assertEqual(self.records.current(), [self.cookie(expires=20)])
        self.now += 10 * self.records.GRACE
        self.assertEqual(self.records.current(), [self.cookie(expires=20)])

    def test_a_second_removal_after_the_grace_does_not_bring_the_cookie_back(self):
        self.records.added(self.cookie())
        self.records.removed(self.cookie())
        self.now += self.records.GRACE + 0.5
        self.records.removed(self.cookie())
        self.assertEqual(self.listed(), [])
        self.records.added(self.cookie('2'))
        self.records.removed(self.cookie('2'))
        self.records.removed(self.cookie('3'))
        self.now += 10 * self.records.GRACE
        self.assertEqual(self.listed(), [])

    def test_a_removal_is_in_doubt_until_the_grace_is_over_or_a_second_removal_decides_it(self):
        self.assertTrue(self.records.settled())
        self.records.added(self.cookie())
        self.assertTrue(self.records.settled())
        self.records.removed(self.cookie())
        self.assertFalse(self.records.settled())
        self.now += self.records.GRACE / 2
        self.assertFalse(self.records.settled())
        self.now += self.records.GRACE / 2
        self.assertTrue(self.records.settled())
        self.records.added(self.cookie(name='b'))
        self.records.removed(self.cookie(name='b'))
        self.assertFalse(self.records.settled())
        self.records.removed(self.cookie(name='b'))
        self.assertTrue(self.records.settled())

    def test_a_cookie_removed_and_added_with_a_new_value_is_the_new_one_alone(self):
        self.records.added(self.cookie('1'))
        self.records.removed(self.cookie('1'))
        self.records.added(self.cookie('2'))
        self.assertEqual(self.listed(), [('a', '2')])
        self.now += 10 * self.records.GRACE
        self.assertEqual(self.listed(), [('a', '2')])

    def test_a_removal_of_a_cookie_never_seen_is_nothing(self):
        self.records.removed(self.cookie())
        self.assertEqual(self.listed(), [])

    def test_cookies_are_told_apart_by_name_domain_and_path(self):
        for record in (self.cookie(), self.cookie(path='/account'), self.cookie(name='b'), self.cookie() | {'domain': 'tapas.io'}):
            self.records.added(record)
        self.assertEqual(len(self.records.current()), 4)
        self.records.removed(self.cookie(path='/account'))
        self.now += 10 * self.records.GRACE
        self.assertEqual(len(self.records.current()), 3)

    def test_clearing_forgets_removals_that_are_waiting_for_their_second_report(self):
        self.records.added(self.cookie())
        self.records.removed(self.cookie())
        self.records.added(self.cookie(name='b'))
        self.records.clear()
        self.assertEqual(self.records.current(), [])
        self.records.removed(self.cookie())
        self.assertEqual(self.records.current(), [])


class RouteTests(unittest.TestCase):
    def test_a_route_is_settled_once_and_a_second_try_fails_with_the_text_the_adapter_expects(self):
        outcomes = {'abort': 'abort', 'continue_': 'continue', 'fallback': 'fallback'}
        for first in outcomes:
            route = Route(request())
            getattr(route, first)()
            for again in outcomes:
                with self.subTest(first=first, again=again), self.assertRaises(Error) as raised:
                    getattr(route, again)()
                self.assertEqual(str(raised.exception), 'Route is already handled!')
            self.assertEqual(route.outcome, outcomes[first])

    def test_the_adapter_treats_a_second_settling_as_harmless(self):
        route = Route(request())
        finish_route(route, 'abort')
        finish_route(route, 'abort')
        finish_route(route, 'continue_')
        self.assertEqual(route.outcome, 'abort')

    def test_a_request_cannot_be_changed_and_a_change_is_refused_not_ignored(self):
        route = Route(request())
        with self.assertRaises(Error):
            route.continue_(headers={'x': 'y'})
        with self.assertRaises(Error):
            route.fallback(url='https://example.test/')
        route.abort('failed')
        self.assertEqual(route.outcome, 'abort')


class RequestRuleTests(EngineCase):
    def recorder(self, order, name, outcome):
        def handler(route):
            order.append(name)
            if outcome:
                getattr(route, outcome)()
        return handler

    def test_page_rules_come_before_context_rules_and_the_newest_comes_first(self):
        page, order = self.open(), []
        self.context_.route('**/*', self.recorder(order, 'context-old', 'fallback'))
        self.context_.route('**/*', self.recorder(order, 'context-new', 'fallback'))
        page.route('**/*', self.recorder(order, 'page-old', 'fallback'))
        page.route('**/*', self.recorder(order, 'page-new', 'fallback'))
        self.assertFalse(self.context_.refuses(request()))
        self.assertEqual(order, ['page-new', 'page-old', 'context-new', 'context-old'])

    def test_a_rule_that_lets_the_request_go_ends_the_chain_and_one_that_aborts_refuses(self):
        page, order = self.open(), []
        self.context_.route('**/*', self.recorder(order, 'context', 'abort'))
        page.route('**/*', self.recorder(order, 'page', 'continue_'))
        self.assertFalse(self.context_.refuses(request()))
        self.assertEqual(order, ['page'])
        del order[:]
        page.unroute('**/*')
        page.route('**/*', self.recorder(order, 'later', 'fallback'))
        page.route('**/*', self.recorder(order, 'abort', 'abort'))
        self.assertTrue(self.context_.refuses(request()))
        self.assertEqual(order, ['abort'])

    def test_a_rule_that_decides_nothing_refuses_the_request(self):
        self.open()
        self.context_.route('**/*', self.recorder([], 'silent', None))
        self.assertTrue(self.context_.refuses(request()))

    def test_requests_are_allowed_when_every_rule_falls_back_or_there_is_none(self):
        self.context()
        self.assertFalse(self.context_.refuses(request()))
        self.context_.route('**/*', self.recorder([], 'a', 'fallback'))
        self.assertFalse(self.context_.refuses(request()))
        self.assertFalse(self.surface.decide(request()))

    def test_an_allowed_request_that_is_not_a_get_is_remembered_for_reporting_but_a_refused_one_is_not(self):
        self.context()
        self.context_.refuses(request(ORIGIN + '/api/save', 'POST', 'fetch'))
        self.context_.refuses(request(ORIGIN + '/page', 'GET'))
        self.assertEqual(self.context_.methods, {ORIGIN + '/api/save': 'POST'})
        self.context_.route('**/*', lambda route: route.abort())
        self.assertTrue(self.context_.refuses(request(ORIGIN + '/api/other', 'DELETE', 'fetch')))
        self.assertEqual(self.context_.methods, {ORIGIN + '/api/save': 'POST'})

    def test_the_memory_of_methods_is_bounded(self):
        self.context()
        for index in range(600):
            self.context_.refuses(request(f'{ORIGIN}/api/{index}', 'POST'))
        self.assertEqual(len(self.context_.methods), 500)

    def test_rules_apply_to_every_request_and_other_patterns_are_refused(self):
        page = self.open()
        for owner in (self.context_, page):
            with self.assertRaises(Error):
                owner.route('**/api/*', lambda route: None)
            with self.assertRaises(Error):
                owner.unroute('**/api/*')
        first, second = (lambda route: route.fallback()), (lambda route: route.abort())
        self.context_.route('**/*', first)
        self.context_.route('**/*', second)
        self.context_.unroute('**/*', first)
        self.assertEqual(self.context_.handlers, [second])
        self.context_.unroute('**/*', first)
        self.context_.unroute('**/*')
        self.assertEqual(self.context_.handlers, [])
        page.route('**/*', first)
        page.unroute_all(behavior='wait')
        self.assertEqual(page.handlers, [])

    def test_the_adapters_own_rules_decide_as_they_do_in_a_browser(self):
        page = self.open()
        adapter = TapasAdapter(Path('unused-test-session'))
        adapter.network_guard = Mock()
        self.context_.route('**/*', adapter.network_request)
        self.context_.route('**/*', adapter.network_read_only_request)
        with patch('comic_archive.adapters.tapas.Transport'):
            transport = BrowserReadTransport(page, self.context_)
        comic = request(ORIGIN + '/pc/a.jpg', 'GET', 'image')
        site_image = request(ORIGIN + '/static/logo.png', 'GET', 'image')
        post = request(ORIGIN + '/api/save', 'POST', 'fetch')
        for rule in ('reading', 'blocking comic preloads'):
            if rule == 'blocking comic preloads':
                page.route('**/*', transport.block_comic_preload)
            with self.subTest(rule):
                self.assertFalse(self.context_.refuses(request()))
                self.assertFalse(self.context_.refuses(site_image))
                self.assertTrue(self.context_.refuses(post))
                self.assertEqual(self.context_.refuses(comic), rule == 'blocking comic preloads')
        adapter.network_guard.check.side_effect = NetworkPolicyHold('Wi-Fi only: transfers held.')
        transport.network_guard = adapter.network_guard
        self.assertTrue(self.context_.refuses(request()))
        page.unroute_all()
        self.assertTrue(self.context_.refuses(request()))
        adapter.network_guard.check.side_effect = None
        self.assertFalse(self.context_.refuses(request()))

    def test_the_decision_is_made_by_the_context_for_its_surface(self):
        self.context()
        self.context_.route('**/*', lambda route: route.abort())
        self.assertTrue(self.surface.decide(request()))


class RestoreTests(EngineCase):
    def cookie(self, name, expires=UNSET, path=UNSET, domain='.tapas.io', value='v'):
        record = {'name': name, 'value': value, 'domain': domain, 'httpOnly': True, 'secure': True, 'sameSite': 'Lax'}
        if expires is not UNSET:
            record['expires'] = expires
        if path is not UNSET:
            record['path'] = path
        return record

    def handed_over(self):
        return [entry[1] for entry in self.surface.log if entry[0] == 'cookie']

    def test_cookies_that_have_expired_are_not_restored_but_session_cookies_are(self):
        now = self.clock.time()
        self.context({'cookies': [self.cookie('expired', now - 10), self.cookie('long-ago', 0), self.cookie('later', now + 3600),
                                  self.cookie('session', -1), self.cookie('no-expiry')]})
        self.assertEqual(sorted(self.handed_over()), ['later', 'no-expiry', 'session'])

    def test_duplicates_collapse_to_the_last_by_name_domain_and_path(self):
        self.context({'cookies': [self.cookie('a', value='old'), self.cookie('a', path='/', value='new'),
                                  self.cookie('a', path='/x'), self.cookie('a', domain='tapas.io'), self.cookie('b')]})
        self.assertEqual(len(self.handed_over()), 4)
        self.assertEqual(self.surface.cookies[('a', '.tapas.io', '/')]['value'], 'new')

    def test_the_download_does_not_wait_for_a_store_that_already_has_the_cookies(self):
        self.context({'cookies': [self.cookie('a'), self.cookie('b')]})
        self.assertEqual(self.clock.sleeps, [])

    def test_a_store_that_takes_cookies_a_moment_later_is_waited_for_only_that_long(self):
        self.surface.keeps_cookies = False
        self.surface.set_cookie = lambda record: self.clock.after(0.2, lambda: self.surface.cookies.added(record))
        start = self.clock.now
        self.context({'cookies': [self.cookie('a', path='/')]})
        self.assertEqual(len(self.surface.cookies), 1)
        self.assertLess(self.elapsed(start), 0.5)

    def test_a_store_that_refuses_a_cookie_holds_the_download_up_for_only_a_few_seconds(self):
        self.surface.keeps_cookies = False
        start = self.clock.now
        self.context({'cookies': [self.cookie('a')]})
        self.assertGreaterEqual(self.elapsed(start), 5)
        self.assertLess(self.elapsed(start), 5.5)

    def test_saved_storage_goes_to_the_surface_only_when_there_is_any_and_only_good_rows(self):
        self.context()
        self.context({'cookies': [], 'origins': []})
        self.context({'origins': [{'origin': ORIGIN, 'localStorage': []}, 'junk', {'origin': 5, 'localStorage': [{'name': 'a', 'value': 'b'}]},
                                  {'localStorage': [{'name': 'a', 'value': 'b'}]}]})
        self.assertEqual([entry for entry in self.surface.log if entry[0] in {'restore', 'cookie'}], [])
        self.context({'origins': [{'origin': ORIGIN, 'localStorage': [
            {'name': 'theme', 'value': 'dark'}, {'name': 1, 'value': 'x'}, {'name': 'n', 'value': 2}, 'junk']},
            {'origin': 'https://other.example', 'localStorage': [{'name': 'k', 'value': 'v'}]}]})
        self.assertEqual([entry[1] for entry in self.surface.log if entry[0] == 'restore'],
                         [{ORIGIN: [['theme', 'dark']], 'https://other.example': [['k', 'v']]}])

    def test_the_restoring_script_is_dropped_after_the_first_page_of_a_restored_site_and_not_for_a_lookalike(self):
        context = self.context({'origins': [{'origin': ORIGIN, 'localStorage': [{'name': 'a', 'value': 'b'}]}]})
        for url in ('about:blank', 'https://tapas.io.example.test/', 'https://other.example/', ORIGIN + 'x/'):
            context.arrived(url)
        self.assertNotIn(('restored',), self.surface.log)
        context.arrived(ORIGIN + '/series/1')
        context.arrived(ORIGIN)
        self.assertEqual(self.surface.log.count(('restored',)), 1)
        other = self.context({'cookies': [self.cookie('a')]})
        other.arrived(ORIGIN + '/')
        self.assertEqual(self.surface.log.count(('restored',)), 1)

    def test_a_page_that_arrives_at_a_restored_site_ends_the_restore(self):
        self.context({'origins': [{'origin': ORIGIN, 'localStorage': [{'name': 'a', 'value': 'b'}]}]})
        page = self.context_.new_page()
        self.surface.on_load = lambda url: self.site.navigate(url)
        page.goto(ORIGIN + '/', wait_until='domcontentloaded')
        self.assertEqual(self.surface.log.count(('restored',)), 1)


class CookieWaitTests(EngineCase):
    def setUp(self):
        super().setUp()
        self.open()

    def cookie(self, name):
        return {'name': name, 'value': '1', 'domain': '.tapas.io', 'path': '/'}

    def names(self, records):
        return sorted(record['name'] for record in records)

    def test_cookies_and_the_saved_state_leave_out_a_cookie_that_was_really_removed_a_moment_ago(self):
        for name in ('kept', 'dropped'):
            self.surface.cookies.added(self.cookie(name))
        self.surface.cookies.removed(self.cookie('dropped'))
        self.assertEqual(self.names(self.surface.cookies.current()), ['dropped', 'kept'])
        start = self.clock.now
        self.assertEqual(self.names(self.context_.cookies()), ['kept'])
        self.assertGreaterEqual(self.elapsed(start), CookieRecords.GRACE)
        self.assertLess(self.elapsed(start), CookieRecords.GRACE + 0.2)
        self.surface.cookies.added(self.cookie('later'))
        self.surface.cookies.removed(self.cookie('later'))
        self.assertEqual(self.names(self.context_.storage_state()['cookies']), ['kept'])

    def test_a_cookie_set_again_unchanged_is_still_listed_once_the_wait_is_over(self):
        self.surface.cookies.added(self.cookie('kept'))
        self.surface.cookies.removed(self.cookie('kept'))
        self.surface.cookies.removed(self.cookie('kept'))
        start = self.clock.now
        self.assertEqual(self.names(self.context_.cookies()), ['kept'])
        self.assertEqual(self.elapsed(start), 0)

    def test_nothing_is_waited_for_when_no_removal_is_in_doubt(self):
        self.surface.cookies.added(self.cookie('kept'))
        self.surface.cookies.added(self.cookie('old'))
        self.surface.cookies.removed(self.cookie('old'))
        self.clock.now += CookieRecords.GRACE + 1
        self.assertEqual(self.names(self.context_.cookies()), ['kept'])
        self.assertEqual(self.clock.sleeps, [])

    def test_removals_that_keep_coming_are_waited_for_no_longer_than_twice_the_grace(self):
        count = []

        def churn():
            count.append(1)
            name = f'c{len(count)}'
            self.surface.cookies.added(self.cookie(name))
            self.surface.cookies.removed(self.cookie(name))
            self.clock.after(0.3, churn)
        churn()
        start = self.clock.now
        listed = self.names(self.context_.cookies())
        self.assertGreaterEqual(self.elapsed(start), CookieRecords.GRACE * 2)
        self.assertLess(self.elapsed(start), CookieRecords.GRACE * 2 + 0.2)
        # What is still in doubt at the end is listed, not hidden.
        self.assertIn(f'c{len(count)}', listed)


class StorageStateTests(EngineCase):
    def saved(self, origin, *rows):
        return {'origin': origin, 'localStorage': [{'name': name, 'value': value} for name, value in rows]}

    def by_origin(self, state):
        return {entry['origin']: [(row['name'], row['value']) for row in entry['localStorage']] for entry in state['origins']}

    def test_the_cookies_come_from_the_records_as_copies(self):
        self.open()
        record = {'name': 'a', 'value': '1', 'domain': '.tapas.io', 'path': '/'}
        self.surface.cookies.added(record)
        state = self.context_.storage_state()
        self.assertEqual(state['cookies'], [record])
        state['cookies'][0]['value'] = 'changed'
        self.assertEqual(self.surface.cookies.current(), [record])

    def test_the_current_pages_storage_replaces_that_sites_saved_entry_and_others_are_kept(self):
        self.open({'origins': [self.saved(ORIGIN, ('old', '1')), self.saved('https://other.example', ('k', 'v'))]})
        self.site.storage = json.dumps([ORIGIN, [['b', '2'], ['a', '1']]])
        self.assertEqual(self.by_origin(self.context_.storage_state()),
                         {'https://other.example': [('k', 'v')], ORIGIN: [('a', '1'), ('b', '2')]})

    def test_a_site_not_saved_before_is_added(self):
        self.open()
        self.site.storage = json.dumps([ORIGIN, [['a', '1']]])
        self.assertEqual(self.by_origin(self.context_.storage_state()), {ORIGIN: [('a', '1')]})

    def test_an_empty_storage_drops_the_saved_entry_of_that_site(self):
        self.open({'origins': [self.saved(ORIGIN, ('old', '1')), self.saved('https://other.example', ('k', 'v'))]})
        self.site.storage = json.dumps([ORIGIN, []])
        self.assertEqual(self.by_origin(self.context_.storage_state()), {'https://other.example': [('k', 'v')]})

    def test_a_page_whose_storage_cannot_be_read_leaves_the_saved_entries(self):
        self.open({'origins': [self.saved(ORIGIN, ('old', '1'))]})
        for answer in ('null', 'not json', None, NO_ANSWER):
            with self.subTest(answer=answer):
                self.site.raw = self.site.storage = answer
                self.assertEqual(self.by_origin(self.context_.storage_state()), {ORIGIN: [('old', '1')]})

    def test_without_a_page_the_saved_entries_are_all_there_is_and_junk_entries_are_dropped(self):
        self.context({'origins': [self.saved(ORIGIN, ('old', '1')), 'junk', {'origin': 5}]})
        self.assertEqual(self.by_origin(self.context_.storage_state()), {ORIGIN: [('old', '1')]})


class NavigationTests(EngineCase):
    def setUp(self):
        super().setUp()
        self.open()
        self.surface.on_load = lambda url: self.site.navigate(url)

    def test_a_page_with_nothing_to_ask_is_reported_instead_of_waited_for_for_ever(self):
        self.site.raw = NO_ANSWER
        with self.assertRaisesRegex(Error, 'opened no page'):
            qt_engine.Page(self.context_)
        self.assertGreaterEqual(self.clock.now - 1000.0, qt_engine.SCRIPT_SECONDS)
        self.assertEqual(self.surface.loads()[:2], ['about:blank', 'about:blank'])

    def test_goto_gives_the_new_documents_response(self):
        response = self.page.goto(ORIGIN + '/series/1')
        self.assertEqual((response.url, response.status, response.ok, response.request.resource_type),
                         (ORIGIN + '/series/1', 200, True, 'document'))
        self.assertEqual(self.page.url, ORIGIN + '/series/1')
        self.surface.on_load = lambda url: self.site.navigate(url, status=503)
        response = self.page.goto(ORIGIN + '/series/2')
        self.assertEqual((response.status, response.ok), (503, False))

    def test_the_pages_address_is_the_document_that_is_there_not_the_one_asked_for(self):
        self.page.goto(ORIGIN + '/series/1')
        self.surface.on_load = None
        with self.assertRaises(qt_engine.TimeoutError):
            self.page.goto(ORIGIN + '/series/2', timeout=300)
        self.assertEqual(self.page.url, ORIGIN + '/series/1')
        self.surface.on_load = lambda url: self.site.navigate('chrome-error://chromewebdata/', status=0)
        with self.assertRaises(Error):
            self.page.goto(ORIGIN + '/series/3')
        self.assertEqual(self.page.url, 'chrome-error://chromewebdata/')

    def test_the_pages_address_is_blank_when_no_document_answers(self):
        self.page.goto(ORIGIN + '/series/1')
        self.surface.gone = True
        self.assertEqual(self.page.url, 'about:blank')
        self.surface.gone = False
        self.site.raw = NO_ANSWER
        self.assertEqual(self.page.url, 'about:blank')

    def test_a_page_whose_process_has_ended_is_given_a_blank_page_before_the_address_is_loaded(self):
        self.surface.gone = True
        response = self.page.goto(ORIGIN + '/series/1')
        self.assertEqual(self.surface.loads(), ['about:blank', 'about:blank', ORIGIN + '/series/1'])
        self.assertEqual((response.url, response.status), (ORIGIN + '/series/1', 200))
        self.assertFalse(self.surface.gone)

    def test_a_new_blank_page_is_not_taken_for_the_page_that_was_asked_for(self):
        # A new page process answers for an empty document of its own before the blank page has loaded.
        self.surface.gone, self.surface.ends = True, False
        self.clock.events.clear()

        def load(url):
            def end():
                self.site.navigate(url)
                self.surface.ended += 1
            self.clock.after(0.3 if url == 'about:blank' else 0.6, end)
        self.surface.on_load = load
        response = self.page.goto(ORIGIN + '/series/1')
        self.assertEqual((response.url, response.status), (ORIGIN + '/series/1', 200))

    def test_a_page_that_is_still_there_loads_only_the_address(self):
        self.page.goto(ORIGIN + '/series/1')
        self.assertEqual(self.surface.loads(), ['about:blank', ORIGIN + '/series/1'])

    def arrival(self, wait_until, ready_after, complete_after):
        self.clock.events.clear()

        def load(url):
            self.site.navigate(url, ready=False, complete=False)
            self.clock.after(ready_after, lambda: self.site.document.update(ready=True))
            self.clock.after(complete_after, lambda: self.site.document.update(ready=True, complete=True))
        self.surface.on_load = load
        start = self.clock.now
        self.page.goto(ORIGIN + '/', wait_until=wait_until)
        return self.elapsed(start)

    def test_a_page_is_waited_for_until_it_is_ready_or_complete_as_asked(self):
        self.assertAlmostEqual(self.arrival('domcontentloaded', 0.5, 2), 0.5, delta=0.1)
        self.assertAlmostEqual(self.arrival('load', 0.5, 2), 2, delta=0.1)

    def test_a_document_that_was_there_before_does_not_count_as_the_arrival(self):
        self.site.navigate(ORIGIN + '/old')
        self.surface.on_load = lambda url: self.clock.after(0.3, lambda: self.site.navigate(ORIGIN + '/new', status=201))
        response = self.page.goto(ORIGIN + '/new')
        self.assertEqual((response.url, response.status), (ORIGIN + '/new', 201))

    def test_a_page_that_is_not_on_the_web_gives_no_response_and_is_no_failure(self):
        self.page.goto(ORIGIN + '/')
        self.surface.on_load = lambda url: self.site.navigate(url, status=0)
        self.assertIsNone(self.page.goto('about:blank'))

    def test_an_error_page_or_a_web_page_without_a_status_is_a_failure(self):
        for url, status in (('chrome-error://chromewebdata/', 200), ('chrome-error://chromewebdata/', 0), (ORIGIN + '/', 0)):
            with self.subTest(url=url, status=status):
                self.surface.on_load = lambda address, url=url, status=status: self.site.navigate(url, status=status)
                with self.assertRaises(Error) as raised:
                    self.page.goto(ORIGIN + '/')
                self.assertEqual(str(raised.exception), 'The page could not be loaded.')

    def test_no_new_document_in_time_is_a_timeout_named_as_playwright_names_it(self):
        self.surface.on_load = None
        start = self.clock.now
        with self.assertRaises(Error) as raised:
            self.page.goto(ORIGIN + '/', timeout=500)
        self.assertEqual((type(raised.exception).__name__, str(raised.exception)), ('TimeoutError', 'Timeout 500ms exceeded.'))
        self.assertTrue(issubclass(qt_engine.TimeoutError, Error))
        self.assertAlmostEqual(self.elapsed(start), 0.5, delta=0.1)

    def test_a_failed_load_with_no_new_document_for_over_two_seconds_is_an_error_not_a_timeout(self):
        self.surface.on_load = lambda url: setattr(self.surface, 'failures', self.surface.failures + 1)
        start = self.clock.now
        with self.assertRaises(Error) as raised:
            self.page.goto(ORIGIN + '/')
        self.assertNotIsInstance(raised.exception, qt_engine.TimeoutError)
        self.assertEqual(str(raised.exception), 'The page could not be loaded.')
        self.assertTrue(2 <= self.elapsed(start) < 2.2, self.elapsed(start))

    def test_a_failed_load_names_the_engines_error_numbers_for_the_log(self):
        def fail(url):
            self.surface.failures += 1
            self.surface.failure = '1/-105'
            self.site.navigate('chrome-error://chromewebdata/', status=0)
        self.surface.on_load = fail
        with self.assertRaises(Error) as raised:
            self.page.goto(ORIGIN + '/')
        self.assertEqual(str(raised.exception), 'The page could not be loaded (error 1/-105).')

    def test_a_sites_own_error_page_that_follows_a_failed_load_is_read_as_a_document(self):
        def load(url):
            self.surface.failures += 1
            self.clock.after(1.5, lambda: self.site.navigate(url, status=503))
        self.surface.on_load = load
        self.assertEqual(self.page.goto(ORIGIN + '/').status, 503)

    def test_failures_from_before_the_navigation_do_not_make_a_slow_page_an_error(self):
        self.surface.failures = 3
        self.surface.on_load = None
        start = self.clock.now
        with self.assertRaises(qt_engine.TimeoutError):
            self.page.goto(ORIGIN + '/', timeout=5000)
        self.assertGreaterEqual(self.elapsed(start), 5)

    def test_only_the_two_waits_it_can_do_are_accepted(self):
        loads = self.surface.loads()
        for method in (self.page.goto, lambda url, wait_until: self.page.expect_navigation(wait_until=wait_until)):
            for wait_until in ('networkidle', 'commit'):
                with self.assertRaises(Error):
                    method(ORIGIN + '/', wait_until=wait_until)
        self.assertEqual(self.surface.loads(), loads)

    def test_expect_navigation_gives_the_page_an_action_inside_the_block_led_to(self):
        with self.page.expect_navigation(wait_until='domcontentloaded', timeout=1000) as navigation:
            self.site.navigate(ORIGIN + '/next', status=202)
        self.assertEqual((navigation.value.url, navigation.value.status), (ORIGIN + '/next', 202))

    def test_expect_navigation_times_out_without_a_navigation_and_waits_for_nothing_when_the_block_fails(self):
        with self.assertRaises(qt_engine.TimeoutError):
            with self.page.expect_navigation(timeout=300):
                pass
        start = self.clock.now
        with self.assertRaises(ValueError):
            with self.page.expect_navigation():
                raise ValueError('the action failed')
        self.assertEqual(self.elapsed(start), 0)

    def test_a_page_that_gives_no_answer_for_a_moment_is_asked_again_but_not_for_ever(self):
        self.site.silent = 3
        self.assertEqual(self.page.settled()['url'], 'about:blank')
        self.assertEqual(self.clock.sleeps, [qt_engine.STEP] * 3)
        self.site.silent = 5
        with self.assertRaisesRegex(Error, 'gave no answer'):
            self.page.settled()

    def test_where_is_none_for_answers_that_are_not_the_documents(self):
        for raw in (NO_ANSWER, None, 'not json', ''):
            self.site.raw = raw
            self.assertIsNone(self.page.where(), raw)


class EvaluateTests(EngineCase):
    def setUp(self):
        super().setUp()
        self.open()

    def test_a_value_comes_back_and_the_argument_goes_in_as_json(self):
        self.site.reply = lambda source, steps, argument: {'value': argument}
        for argument in (None, 3, 'é"\'', {'a': [1, None, 'x']}):
            self.assertEqual(self.page.evaluate('x => x', argument), argument)
        self.assertEqual(self.site.calls[-1][0], 'x => x')
        self.site.reply = lambda source, steps, argument: {'value': None}
        self.assertIsNone(self.page.evaluate('() => undefined'))

    def test_a_promise_is_collected_when_it_settles(self):
        self.site.reply = lambda source, steps, argument: {'pending': 4}
        self.site.pending[4] = ['', '', json.dumps({'value': 'late'})]
        self.assertEqual(self.page.evaluate('async () => "late"'), 'late')
        self.assertEqual(self.site.scripts[-3:], [qt_engine.TAKE % 4] * 3)
        self.assertEqual(self.clock.sleeps, [qt_engine.STEP] * 2)

    def test_a_page_error_is_named_without_anything_the_page_said(self):
        names = {'TypeError': 'TypeError', 'x' * 40: 'x' * 40, 'x' * 41: 'Error', '': 'Error', 'Error2': 'Error',
                 '<script>alert(1)</script>': 'Error', 'has space': 'Error', 5: 'Error'}
        for name, shown in names.items():
            with self.subTest(name=name):
                self.site.reply = lambda source, steps, argument: {'error': name}
                with self.assertRaises(Error) as raised:
                    self.page.evaluate('() => 1')
                self.assertEqual(str(raised.exception), f'The page script failed ({shown}).')
        self.site.reply = lambda source, steps, argument: {'pending': 8}
        self.site.pending[8] = [json.dumps({'error': 'RangeError'})]
        with self.assertRaisesRegex(Error, r'^The page script failed \(RangeError\)\.$'):
            self.page.evaluate('() => 1')

    def test_a_page_error_also_says_which_requests_this_apps_rules_refused_while_the_script_ran(self):
        # Noted before the script began: another read's trouble, which says nothing about this one.
        self.surface.notes.append((self.clock.monotonic(), 'a request rule failed: KeyError'))
        self.clock.now += 5

        def reply(source, steps, argument):
            self.surface.notes += [(self.clock.monotonic(), text) for text in (
                'a request rule refused a read', 'a request rule refused a read', 'a request rule failed: ValueError')]
            return {'error': 'TypeError'}
        self.site.reply = reply
        with self.assertRaises(Error) as raised:
            self.page.evaluate('() => 1')
        self.assertEqual(str(raised.exception),
                         'The page script failed (TypeError; a request rule refused a read; a request rule failed: ValueError).')
        # Nothing refused during a later script: the error is the page's word alone.
        self.clock.now += 5
        self.site.reply = lambda source, steps, argument: {'error': 'TypeError'}
        with self.assertRaisesRegex(Error, r'^The page script failed \(TypeError\)\.$'):
            self.page.evaluate('() => 1')

    def test_an_answer_that_is_not_a_json_object_is_an_error(self):
        for raw in ('garbage', '[1]', '5', 'null', '"text"', '', None):
            with self.subTest(raw=raw):
                self.site.raw = raw
                with self.assertRaisesRegex(Error, '^The page gave no answer.$'):
                    self.page.evaluate('() => 1')
        self.site.raw = UNSET
        self.site.reply = lambda source, steps, argument: {'pending': 2}
        self.site.pending[2] = ['garbage']
        with self.assertRaisesRegex(Error, 'gave no answer'):
            self.page.evaluate('() => 1')

    def test_a_page_that_does_not_answer_at_all_is_a_timeout(self):
        self.site.raw = NO_ANSWER
        with self.assertRaises(qt_engine.TimeoutError):
            self.page.evaluate('() => 1')

    def test_a_promise_that_never_settles_is_a_timeout_after_the_wait_asked_for_or_the_default(self):
        self.site.reply = lambda source, steps, argument: {'pending': 9}
        for timeout, seconds in ((2000, 2), (None, qt_engine.EVALUATE_SECONDS)):
            start = self.clock.now
            with self.subTest(timeout=timeout), self.assertRaises(qt_engine.TimeoutError):
                self.page.evaluate('() => new Promise(() => {})', timeout=timeout)
            self.assertAlmostEqual(self.elapsed(start), seconds, delta=0.2)

    def test_a_script_whose_document_was_replaced_is_an_error_at_once_not_at_the_deadline(self):
        self.site.reply = lambda source, steps, argument: {'pending': 6}
        self.site.pending[6] = ['', 'gone']
        start = self.clock.now
        with self.assertRaisesRegex(Error, '^The page changed before its script finished.$') as raised:
            self.page.evaluate('async () => 1')
        self.assertNotIsInstance(raised.exception, qt_engine.TimeoutError)
        self.assertLess(self.elapsed(start), 1)

    def test_a_page_whose_process_has_ended_is_an_error_at_once(self):
        self.surface.gone = True
        asked, start = len(self.site.scripts), self.clock.now
        with self.assertRaisesRegex(Error, 'process has ended'):
            self.page.evaluate('() => 1')
        self.assertEqual((len(self.site.scripts), self.elapsed(start)), (asked, 0))


class ReportTests(EngineCase):
    def setUp(self):
        super().setUp()
        self.open()
        self.site.navigate(ORIGIN + '/series/1')
        self.site.rows = [[ORIGIN + '/a.js', 'script', 200], [ORIGIN + '/api/x', 'fetch', 200]]
        self.heard = []
        self.page.on('response', self.heard.append)

    def test_a_listener_is_not_told_about_requests_made_before_it_was_added(self):
        self.page.report()
        self.assertEqual(self.heard, [])
        self.site.rows.append([ORIGIN + '/b.png', 'img', 200])
        self.page.report()
        self.assertEqual([(r.url, r.status, r.request.resource_type, r.request.method) for r in self.heard],
                         [(ORIGIN + '/b.png', 200, 'image', 'GET')])

    def test_each_later_request_is_reported_once(self):
        self.site.rows.append([ORIGIN + '/b.png', 'img', 200])
        self.page.report()
        self.page.report()
        self.site.rows.append([ORIGIN + '/c.png', 'img', 404])
        self.page.report()
        self.page.report()
        self.assertEqual([(r.url, r.status) for r in self.heard], [(ORIGIN + '/b.png', 200), (ORIGIN + '/c.png', 404)])

    def test_the_kind_of_a_request_is_what_started_it_in_playwrights_words(self):
        starters = {'navigation': 'document', 'iframe': 'document', 'script': 'script', 'fetch': 'fetch', 'xmlhttprequest': 'xhr',
                    'img': 'image', 'beacon': 'ping', 'something-new': 'other', '': 'other'}
        self.site.rows += [[f'{ORIGIN}/{index}', starter, 200] for index, starter in enumerate(starters)]
        self.page.report()
        self.assertEqual([r.request.resource_type for r in self.heard], list(starters.values()))

    def test_a_request_with_no_status_is_skipped_and_the_others_still_reported(self):
        self.site.rows += [[ORIGIN + '/refused', 'fetch', 0], [ORIGIN + '/b.png', 'img', 200]]
        self.page.report()
        self.assertEqual([r.url for r in self.heard], [ORIGIN + '/b.png'])

    def test_a_request_that_was_allowed_though_not_a_get_is_reported_with_its_method(self):
        self.context_.refuses(request(ORIGIN + '/api/save', 'POST', 'fetch'))
        self.context_.refuses(request(ORIGIN + '/api/read', 'GET', 'fetch'))
        self.site.rows += [[ORIGIN + '/api/save', 'fetch', 200], [ORIGIN + '/api/read', 'fetch', 200]]
        self.page.report()
        self.assertEqual([(r.url[len(ORIGIN):], r.request.method) for r in self.heard], [('/api/save', 'POST'), ('/api/read', 'GET')])

    def test_a_new_document_reports_its_own_navigation_first_as_a_document(self):
        self.site.navigate(ORIGIN + '/series/2', status=200)
        self.site.rows = [[ORIGIN + '/c.js', 'script', 200]]
        self.page.report()
        self.assertEqual([(r.url, r.status, r.request.resource_type) for r in self.heard],
                         [(ORIGIN + '/series/2', 200, 'document'), (ORIGIN + '/c.js', 200, 'script')])
        self.page.report()
        self.assertEqual(len(self.heard), 2)

    def test_a_navigation_without_a_status_or_not_on_the_web_is_not_reported(self):
        for url, status in ((ORIGIN + '/refused', 0), ('about:blank', 200), ('file:///x', 200)):
            self.site.navigate(url, status=status)
            self.page.report()
        self.assertEqual(self.heard, [])

    def test_a_removed_listener_hears_no_more_and_removing_twice_is_harmless(self):
        other = []
        self.page.on('response', other.append)
        self.page.remove_listener('response', self.heard.append)
        self.site.rows.append([ORIGIN + '/b.png', 'img', 200])
        self.page.report()
        self.assertEqual((len(self.heard), len(other)), (0, 1))
        self.page.remove_listener('response', other.append)
        self.page.remove_listener('response', other.append)
        self.page.remove_listener('response', print)
        self.site.rows.append([ORIGIN + '/c.png', 'img', 200])
        self.page.report()
        self.assertEqual(len(other), 1)

    def test_only_responses_can_be_listened_for(self):
        for event in ('request', 'console', 'load'):
            with self.assertRaises(Error):
                self.page.on(event, print)

    def test_a_page_that_cannot_be_read_reports_nothing(self):
        self.site.raw = 'not json'
        self.assertEqual(self.page.look(), [])
        self.site.raw = NO_ANSWER
        self.assertEqual(self.page.look(), [])
        self.page.report()
        self.assertEqual(self.heard, [])

    def test_waiting_sleeps_the_time_asked_and_then_reports(self):
        self.site.rows.append([ORIGIN + '/b.png', 'img', 200])
        self.page.wait_for_timeout(250)
        self.assertEqual(self.clock.sleeps, [0.25])
        self.assertEqual(len(self.heard), 1)
        self.page.wait_for_timeout(-5)
        self.assertEqual(self.clock.sleeps, [0.25, 0])

    def test_what_the_old_page_requested_is_reported_before_the_next_page_is_loaded(self):
        order = []
        self.page.remove_listener('response', self.heard.append)
        self.page.on('response', lambda response: order.append(('heard', response.url[len(ORIGIN):])))
        self.surface.on_load = lambda url: (order.append(('load', url[len(ORIGIN):])), self.site.navigate(url))
        self.site.rows.append([ORIGIN + '/b.png', 'img', 200])
        self.page.goto(ORIGIN + '/next')
        self.assertEqual(order[:2], [('heard', '/b.png'), ('load', '/next')])


class LocatorTests(EngineCase):
    def setUp(self):
        super().setUp()
        self.open()

    def dom(self, matches, element=lambda action: None, other=lambda source, argument: None):
        """A page with 'matches' elements (a number, or a function of the looks so far), read through 'element'."""
        self.looks = []

        def reply(source, steps, argument):
            if source.startswith(ONE_HEAD):
                self.looks.append(steps)
                count = matches(len(self.looks)) if callable(matches) else matches
                if count == 1:
                    return {'value': {'found': True, 'value': element(source[len(ONE_HEAD):-len(ONE_TAIL)])}}
                return {'value': {'found': False, 'count': count}}
            return {'value': other(source, argument)}
        self.site.reply = reply

    def test_selector_steps_compose_in_the_order_given(self):
        self.site.reply = lambda source, steps, argument: {'value': steps}
        self.assertEqual(self.page.locator('ul.items').locator('li').nth(1).count(), [['css', 'ul.items'], ['css', 'li'], ['nth', 1]])
        self.assertEqual(self.page.locator('.a').locator('..').locator('.b > i').first.count(),
                         [['css', '.a'], ['parent', None], ['css', '.b > i'], ['nth', 0]])
        self.assertEqual(self.page.locator('.a').last.count(), [['css', '.a'], ['nth', -1]])
        self.assertEqual(self.page.locator('..').count(), [['parent', None]])

    def test_selectors_the_engine_cannot_honour_are_refused_rather_than_misread(self):
        refused = ['text=Next', 'xpath=//a', '//a', 'role=button', 'a:has-text("Next")', 'a >> b', 'a:visible', 'id=x', 'css=a',
                   'data-testid=x', 'a:text("x")', 'a:text-is("x")', 'a:nth-match(b, 2)', None, 3]
        for selector in refused:
            with self.subTest(selector=selector), self.assertRaises(Error):
                self.page.locator(selector)
            with self.subTest(chained=selector), self.assertRaises(Error):
                self.page.locator('main').locator(selector)
        self.assertEqual(self.site.calls, [])

    def test_ordinary_css_is_accepted(self):
        for selector in ('ul > li', 'a[href^="/series/"]', 'div:nth-child(2)', 'button.text', '[data-testid=x]', '.has-text', 'input:checked'):
            self.page.locator(selector)

    def test_count_and_evaluate_all_pass_the_steps_the_source_and_the_argument(self):
        self.dom(1, other=lambda source, argument: [source, argument])
        self.assertEqual(self.page.locator('li').evaluate_all('(nodes, n) => nodes.length + n', {'n': 5}),
                         ['(nodes, n) => nodes.length + n', {'n': 5}])
        self.assertEqual(self.site.calls[-1][1], [['css', 'li']])
        self.dom(1, other=lambda source, argument: 4)
        self.assertEqual(self.page.locator('li').count(), 4)

    def test_one_returns_the_value_of_the_single_element(self):
        self.dom(1, element=lambda action: ['read', action])
        self.assertEqual(self.page.locator('a').get_attribute('href'), ['read', 'e => e.getAttribute("href")'])
        self.assertEqual(self.clock.sleeps, [])

    def test_a_name_goes_into_the_script_as_json_text_never_as_code(self):
        self.dom(1, element=lambda action: action)
        name = 'x"); steal(); ("'
        action = self.page.locator('a').get_attribute(name)
        self.assertIn(json.dumps(name), action)
        self.assertNotIn('x"); steal()', action.replace(json.dumps(name), ''))

    def test_several_elements_are_a_strict_mode_violation_at_once_whichever_way_the_element_is_read(self):
        self.dom(3)
        locator = self.page.locator('li')
        for read in (locator.inner_text, lambda: locator.get_attribute('href'), lambda: locator.evaluate('e => 1'), locator.bounding_box,
                     locator.scroll_into_view_if_needed, locator.click):
            with self.subTest(read=read), self.assertRaises(Error) as raised:
                read()
            self.assertEqual((type(raised.exception), str(raised.exception)), (Error, 'strict mode violation: 3 elements'))
        self.assertEqual(self.clock.sleeps, [])

    def test_no_element_in_time_is_a_timeout_after_the_wait_asked_for_or_the_default(self):
        self.dom(0)
        for timeout, seconds in ((1000, 1), (None, 30)):
            start = self.clock.now
            with self.subTest(timeout=timeout), self.assertRaises(qt_engine.TimeoutError) as raised:
                self.page.locator('a').inner_text(timeout=timeout)
            self.assertEqual(str(raised.exception), f'Timeout {seconds * 1000}ms exceeded.')
            self.assertAlmostEqual(self.elapsed(start), seconds, delta=0.2)

    def test_an_element_that_appears_on_a_later_look_is_waited_for(self):
        self.dom(lambda looks: 1 if looks >= 3 else 0, element=lambda action: 'there')
        self.assertEqual(self.page.locator('a').inner_text(), 'there')
        self.assertEqual(len(self.looks), 3)
        self.dom(lambda looks: 0 if looks < 2 else 2 if looks < 3 else 1, element=lambda action: 'late')
        with self.assertRaisesRegex(Error, 'strict mode violation: 2'):
            self.page.locator('a').inner_text()

    def test_visibility_is_false_for_no_element_and_an_error_for_several(self):
        self.dom(0)
        self.assertIs(self.page.locator('a').is_visible(), False)
        self.assertEqual(self.clock.sleeps, [])
        self.dom(2)
        with self.assertRaisesRegex(Error, 'strict mode violation: 2'):
            self.page.locator('a').is_visible()
        for value, shown in ((True, True), (False, False), (1, False), ('yes', False), (None, False)):
            self.dom(1, element=lambda action: value)
            self.assertIs(self.page.locator('a').is_visible(), shown, value)

    def aims(self, *aims, guard=(True,), hits=(True,), on_step=None):
        """The page, one element: aimed at once for each of these, then asked to guard the press (the answers in 'guard') and,
        once pressed, whether it landed (those in 'hits'). The last answer of a list repeats. Each step is noted in the surface's log."""
        pending, guards, landings = list(aims), list(guard), list(hits)
        self.guarded = []

        def next_of(answers):
            return answers.pop(0) if len(answers) > 1 else answers[0]

        def element(action):
            name = 'guard' if action.startswith(GUARD_HEAD) else 'aim'
            self.surface.log.append((name,))
            if on_step:
                on_step(name)
            if name == 'guard':
                self.guarded.append(action)
                return next_of(guards)
            self.assertEqual(action, qt_engine.AIM)
            return next_of(pending)

        def other(source, argument):
            self.assertEqual(source, 'box.hit')
            self.surface.log.append(('landed?',))
            return next_of(landings)
        self.dom(1, element=element, other=other)

    def pointer_events(self):
        return [entry for entry in self.surface.log if entry[0] == 'pointer']

    def steps(self):
        """What happened to the page and the pointer, in order, in short words."""
        return [entry[1] if entry[0] == 'pointer' else entry[0] for entry in self.surface.log if entry[0] != 'load']

    def test_a_click_presses_at_the_aimed_point_after_two_identical_ready_aims_in_the_order_move_guard_press_release(self):
        ready = {'x': 40.5, 'y': 20, 'width': 10, 'height': 4, 'ready': True}
        self.aims({'ready': False}, ready, dict(ready))
        self.page.locator('a').click()
        self.assertEqual(self.pointer_events(), [('pointer', 'move', 40.5, 20), ('pointer', 'press', 40.5, 20), ('pointer', 'release', 40.5, 20)])
        self.assertEqual(self.steps(), ['aim', 'aim', 'aim', 'move', 'guard', 'press', 'release', 'landed?'])
        self.assertEqual(self.guarded, [qt_engine.GUARD % (qt_engine.AIM, json.dumps(ready))])

    def test_a_click_aims_again_while_the_element_moves_and_presses_where_it_stopped(self):
        def aim(x, ready=True):
            return {'x': x, 'y': 5, 'width': 10, 'height': 4, 'ready': ready}
        self.aims(aim(1), aim(2), aim(2, ready=False), aim(3), aim(3))
        self.page.locator('a').click()
        self.assertEqual(self.pointer_events()[1], ('pointer', 'press', 3, 5))
        self.assertEqual(self.steps().count('aim'), 5)
        self.assertEqual(self.guarded, [qt_engine.GUARD % (qt_engine.AIM, json.dumps(aim(3)))])

    def test_a_click_on_an_element_that_never_holds_still_or_is_never_ready_is_a_timeout_and_presses_nothing(self):
        moving = iter(range(1000))
        self.dom(1, element=lambda action: {'x': next(moving), 'y': 5, 'width': 10, 'height': 4, 'ready': True})
        with self.assertRaises(qt_engine.TimeoutError):
            self.page.locator('a').click(timeout=500)
        self.aims({'x': 1, 'y': 5, 'width': 10, 'height': 4, 'ready': False})
        with self.assertRaises(qt_engine.TimeoutError):
            self.page.locator('a').click(timeout=500)
        self.dom(0)
        with self.assertRaises(qt_engine.TimeoutError):
            self.page.locator('a').click(timeout=500)
        self.assertEqual(self.pointer_events(), [])

    def test_a_click_does_not_press_while_the_guard_says_the_element_has_moved_and_presses_once_it_holds(self):
        ready = {'x': 1, 'y': 2, 'width': 3, 'height': 4, 'ready': True}
        self.aims(ready, dict(ready), guard=(False, False, True))
        self.page.locator('a').click()
        again = ['aim', 'aim', 'move', 'guard']
        self.assertEqual(self.steps(), again * 2 + again + ['press', 'release', 'landed?'])

    def test_a_click_whose_guard_never_holds_presses_nothing_and_times_out(self):
        ready = {'x': 1, 'y': 2, 'width': 3, 'height': 4, 'ready': True}
        self.aims(ready, dict(ready), guard=(False,))
        with self.assertRaises(qt_engine.TimeoutError):
            self.page.locator('a').click(timeout=500)
        self.assertTrue(self.guarded)
        self.assertEqual({entry[1] for entry in self.pointer_events()}, {'move'})
        self.assertNotIn('landed?', self.steps())

    def test_a_press_the_page_stopped_is_followed_by_a_whole_new_aim_move_guard_and_press(self):
        ready = {'x': 1, 'y': 2, 'width': 3, 'height': 4, 'ready': True}
        self.aims(ready, dict(ready), hits=('armed', False, True))
        self.page.locator('a').click()
        cycle = ['aim', 'aim', 'move', 'guard', 'press', 'release']
        self.assertEqual(self.steps(), cycle + ['landed?', 'landed?'] + cycle + ['landed?'])
        self.assertEqual(len(self.guarded), 2)

    def test_a_click_reports_what_was_requested_once_before_aiming_and_not_again_before_the_press(self):
        self.site.navigate(ORIGIN + '/')
        self.page.on('response', lambda response: self.surface.log.append(('heard',)))
        self.site.rows.append([ORIGIN + '/b.png', 'img', 200])
        ready = {'x': 1, 'y': 2, 'width': 3, 'height': 4, 'ready': True}
        self.aims(ready, dict(ready), on_step=lambda name: self.site.rows.append([f'{ORIGIN}/{name}{len(self.site.rows)}', 'img', 200]))
        self.page.locator('a').click()
        self.assertEqual(self.steps(), ['heard', 'aim', 'aim', 'move', 'guard', 'press', 'release', 'landed?'])

    def landed(self, *answers):
        """Locator.landed() with the page answering box.hit in turn (the last answer repeats)."""
        pending = list(answers)
        self.dom(1, other=lambda source, argument: pending.pop(0) if len(pending) > 1 else pending[0])
        return self.page.locator('a').landed()

    def test_a_press_landed_unless_the_page_says_it_stopped_it(self):
        for hit, landed in ((True, True), (False, False), (None, True), ('anything else', True)):
            with self.subTest(hit=hit):
                self.assertIs(self.landed(hit), landed)
        self.assertEqual(self.site.calls[-1][0], 'box.hit')
        self.assertEqual(self.clock.sleeps, [])

    def test_a_press_still_armed_is_waited_for_but_for_a_second_at_most(self):
        self.assertIs(self.landed('armed', 'armed', 'armed', False), False)
        self.assertEqual(self.clock.sleeps, [qt_engine.STEP] * 3)
        start = self.clock.now
        self.assertIs(self.landed('armed'), True)
        self.assertTrue(0.99 <= self.elapsed(start) < 1.1, self.elapsed(start))

    def test_a_page_on_its_way_elsewhere_counts_as_a_press_that_landed(self):
        self.surface.gone = True
        self.assertIs(self.page.locator('a').landed(), True)
        self.surface.gone = False
        self.site.reply = lambda source, steps, argument: {'error': 'TypeError'}
        self.assertIs(self.page.locator('a').landed(), True)
        self.site.reply = lambda source, steps, argument: {'pending': 5}
        self.site.pending[5] = ['gone']
        self.assertIs(self.page.locator('a').landed(), True)

    def test_a_page_that_gives_no_word_about_the_press_is_waited_for_only_a_moment(self):
        self.site.reply = lambda source, steps, argument: {'pending': 9}
        start = self.clock.now
        self.assertIs(self.page.locator('a').landed(), True)
        self.assertTrue(1.99 <= self.elapsed(start) < 3, self.elapsed(start))

    def test_the_wheel_turns_where_the_pointer_last_was(self):
        self.page.mouse.wheel(1, 2)
        self.page.mouse.move(5, 6)
        self.page.mouse.wheel(0, 100)
        self.assertEqual([entry for entry in self.surface.log if entry[0] != 'load'],
                         [('wheel', 0, 0, 1, 2), ('pointer', 'move', 5, 6), ('wheel', 5, 6, 0, 100)])

    def test_the_mouse_clicks_at_a_point_with_move_press_release(self):
        self.page.mouse.click(7, 8)
        self.assertEqual(self.pointer_events(), [('pointer', 'move', 7, 8), ('pointer', 'press', 7, 8), ('pointer', 'release', 7, 8)])


class SavedCookieTests(unittest.TestCase):
    @staticmethod
    def qt_cookie(name=b'session', value=b'v', path='/', expires=None, same_site='Lax'):
        return SimpleNamespace(
            name=lambda: SimpleNamespace(data=lambda: name), value=lambda: SimpleNamespace(data=lambda: value),
            domain=lambda: '.tapas.io', path=lambda: path, isSessionCookie=lambda: expires is None,
            expirationDate=lambda: SimpleNamespace(toSecsSinceEpoch=lambda: expires),
            isHttpOnly=lambda: True, isSecure=lambda: True, sameSitePolicy=lambda: SimpleNamespace(name=same_site))

    def test_a_cookie_from_qt_is_in_the_shape_of_the_saved_session(self):
        self.assertEqual(qt_engine.saved_cookie(self.qt_cookie()), {
            'name': 'session', 'value': 'v', 'domain': '.tapas.io', 'path': '/', 'expires': -1,
            'httpOnly': True, 'secure': True, 'sameSite': 'Lax'})
        kept = qt_engine.saved_cookie(self.qt_cookie(b'caf\xc3\xa9', b'\xff', '', 1790893649, 'None_'))
        self.assertEqual((kept['name'], kept['value'], kept['path'], kept['expires'], kept['sameSite']),
                         ('caf\u00e9', '\ufffd', '/', 1790893649, 'None'))
        for qt_name, saved in (('Strict', 'Strict'), ('Lax', 'Lax'), ('None', 'None'), ('Default', 'Lax')):
            self.assertEqual(qt_engine.saved_cookie(self.qt_cookie(same_site=qt_name))['sameSite'], saved)


class QtThing:
    """Any Qt class the real Surface builds: keeps its arguments and answers every call with a mock."""

    def __init__(self, *args):
        self.args = args

    def __getattr__(self, name):
        if name.startswith('__'):
            raise AttributeError(name)
        value = Mock(name=name)
        setattr(self, name, value)
        return value


class QtProfile(QtThing):
    def httpUserAgent(self):
        return QT_AGENT


def qt_loop():
    """A Loop whose Qt is made of stand-ins, enough for the real Surface to be built."""
    core = SimpleNamespace(
        QWebEngineProfile=QtProfile, QWebEnginePage=QtThing, QWebEngineUrlRequestInterceptor=QtThing,
        QWebEngineScript=SimpleNamespace(ScriptWorldId=SimpleNamespace(ApplicationWorld=1)),
        QWebEngineSettings=SimpleNamespace(UnknownUrlSchemePolicy=SimpleNamespace(DisallowUnknownUrlSchemes='disallow')))
    loop = Loop()
    loop.q = SimpleNamespace(QtWebEngineCore=core, QtWebEngineWidgets=SimpleNamespace(QWebEngineView=QtThing))
    loop.app, loop.surfaces = Mock(), []
    return loop


class AgentTests(unittest.TestCase):
    """The name the engine gives itself, which the real Surface sets and the context hands on."""

    def test_only_qts_own_tag_is_taken_out_of_the_name(self):
        self.assertEqual(qt_engine.common_agent(QT_AGENT), CHROME)
        self.assertEqual(qt_engine.common_agent('Mozilla/5.0 Chrome/140.0.0.0 QtWebEngine/6.11.2'), 'Mozilla/5.0 Chrome/140.0.0.0')
        self.assertEqual(qt_engine.common_agent(CHROME), CHROME)
        self.assertEqual(qt_engine.common_agent(''), '')

    def test_a_surface_gives_its_profile_the_name_without_qts_tag(self):
        surface = qt_engine.Surface(qt_loop())
        surface.profile.setHttpUserAgent.assert_called_once_with(CHROME)
        self.assertEqual(surface.agent, CHROME)

    def test_a_context_names_itself_as_its_surface_does(self):
        context = qt_engine.Context(qt_loop())
        context.surface.profile.setHttpUserAgent.assert_called_once_with(CHROME)
        self.assertEqual(context.agent, CHROME)


class SurfaceRefusalTests(unittest.TestCase):
    """Surface.refuses reads Qt's request description and asks the context; it touches nothing else of Qt."""

    @staticmethod
    def info(kind='ResourceTypeXhr', url=b'https://tapas.io/api/x', method=b'POST'):
        return SimpleNamespace(resourceType=lambda: SimpleNamespace(name=kind),
                               requestUrl=lambda: SimpleNamespace(toEncoded=lambda: url), requestMethod=lambda: method)

    def refuses(self, decide, info, refuse_workers=False):
        self.noted = []
        surface = SimpleNamespace(refuse_workers=refuse_workers, decide=decide, note=self.noted.append)
        return qt_engine.Surface.refuses(surface, info)

    def test_a_refused_read_and_a_rule_that_fails_are_noted_for_the_log_and_a_refused_write_is_not(self):
        read = self.info(method=b'GET')
        self.assertTrue(self.refuses(lambda request: True, read))
        self.assertEqual(self.noted, ['a request rule refused a read'])
        # Writes are refused all the time, by design; so are pictures a reader page would load early.
        for info in (self.info(), self.info('ResourceTypeImage', method=b'GET')):
            self.assertTrue(self.refuses(lambda request: True, info))
            self.assertEqual(self.noted, [])
        self.assertFalse(self.refuses(lambda request: False, read))
        self.assertEqual(self.noted, [])

        def broken(request):
            raise KeyError('https://tapas.io/private')
        self.assertTrue(self.refuses(broken, read))
        self.assertEqual(self.noted, ['a request rule failed: KeyError'])

    def test_only_the_latest_few_notes_are_kept(self):
        surface = qt_engine.Surface(qt_loop())
        for number in range(20):
            surface.note(f'note {number}')
        self.assertEqual([text for _, text in surface.notes], [f'note {number}' for number in range(12, 20)])

    def test_the_context_is_asked_with_the_address_method_and_kind_in_playwrights_words(self):
        asked = []
        self.assertFalse(self.refuses(lambda request: asked.append(request) or False, self.info()))
        self.assertEqual([(r.url, r.method, r.resource_type) for r in asked], [('https://tapas.io/api/x', 'POST', 'xhr')])
        self.assertTrue(self.refuses(lambda request: True, self.info()))
        asked.clear()
        self.refuses(lambda request: asked.append(request.resource_type), self.info('ResourceTypeSomethingNew'))
        self.assertEqual(asked, ['other'])

    def test_a_rule_that_fails_has_not_allowed_the_request(self):
        def broken(request):
            raise RuntimeError('rule failed')
        self.assertTrue(self.refuses(broken, self.info()))

    def test_a_request_whose_kind_cannot_be_read_is_refused(self):
        def broken():
            raise RuntimeError('no kind')
        info = SimpleNamespace(resourceType=broken, requestUrl=lambda: SimpleNamespace(toEncoded=lambda: b'https://tapas.io/'),
                               requestMethod=lambda: b'GET')
        self.assertTrue(self.refuses(lambda request: self.fail('the context was asked'), info))

    def test_service_workers_are_refused_only_when_asked_and_without_asking_the_context(self):
        worker = self.info('ResourceTypeServiceWorker', b'https://tapas.io/sw.js', b'GET')
        self.assertTrue(self.refuses(lambda request: self.fail('the context was asked'), worker, refuse_workers=True))
        self.assertFalse(self.refuses(lambda request: False, worker))


class ResponseAndDriverTests(EngineCase):
    def test_a_response_has_an_address_a_status_and_no_contents(self):
        for status, ok in ((199, False), (200, True), (299, True), (300, False), (404, False)):
            self.assertIs(qt_engine.Response('https://tapas.io/', status).ok, ok, status)
        response = qt_engine.Response(ORIGIN + '/x', 200, 'fetch', 'POST')
        self.assertEqual((response.request.url, response.request.method, response.request.resource_type, response.headers),
                         (ORIGIN + '/x', 'POST', 'fetch', {}))
        for read in (response.body, response.text, response.json):
            with self.assertRaisesRegex(Error, 'cannot read a response body'):
                read()

    def test_the_driver_is_its_own_launcher_and_stops_nothing(self):
        driver = qt_engine.Driver()
        self.assertIs(driver.start(), driver)
        self.assertIs(driver.qt, driver)
        with driver as entered:
            self.assertIs(entered, driver)
        with self.assertRaisesRegex(Error, 'only inside the downloader'):
            driver.launch(headless=True)

    def test_the_page_says_that_it_cannot_hand_over_a_visited_pages_text(self):
        self.assertIs(qt_engine.Page.navigation_bodies, False)

    def test_the_tapas_adapter_reads_a_series_page_through_this_page_without_visiting_it(self):
        page = self.open()
        self.site.navigate(ORIGIN + '/')
        self.site.reply = lambda source, steps, argument: {'value': {
            'status': 200, 'text': '<html>series</html>', 'source_response_kind': 'html', 'retry_after': None}}
        with patch('comic_archive.adapters.tapas.Transport'):
            transport = BrowserReadTransport(page, self.context_)
        loads = self.surface.loads()
        self.assertEqual(transport.get(ORIGIN + '/series/my-comic/info'), b'<html>series</html>')
        self.assertEqual(self.surface.loads(), loads)
        source, steps, argument = self.site.calls[-1]
        self.assertIn("redirect:'manual'", source)
        self.assertEqual((argument['accept'], argument['limit']), ('text/html', 8388608))
        self.site.reply = lambda source, steps, argument: {'value': {
            'status': 401, 'text': None, 'source_response_kind': 'other', 'retry_after': None}}
        with self.assertRaises(AuthenticationError):
            transport.get(ORIGIN + '/series/my-comic/info')

    def test_a_browser_makes_a_context_that_blocks_workers_only_when_told_to_and_closes_every_one_once(self):
        made = []

        def surface(loop, refuse_workers=False):
            made.append((refuse_workers, Surface(self.clock, self.site)))
            return made[-1][1]
        with patch.object(qt_engine, 'Surface', side_effect=surface):
            browser = qt_engine.Browser(self.loop)
            blocked = browser.new_context(storage_state=None, service_workers='block')
            allowed = browser.new_context()
        self.assertEqual([refuse for refuse, _ in made], [True, False])
        self.assertEqual(browser.contexts, [blocked, allowed])
        browser.close()
        browser.close()
        self.assertEqual([entry for _, item in made for entry in item.log], [('close',), ('close',)])

    def test_a_context_has_one_page_and_closing_the_page_closes_the_context(self):
        page = self.open()
        with self.assertRaisesRegex(Error, 'one page'):
            self.context_.new_page()
        page.close()
        self.assertIn(('close',), self.surface.log)

    def test_closing_after_the_engine_has_gone_is_quiet_but_other_failures_are_not(self):
        self.context()
        self.loop.call = Mock(side_effect=Error('The built-in engine is not running.'))
        self.context_.close()
        self.loop.call = Mock(side_effect=RuntimeError('real'))
        with self.assertRaises(RuntimeError):
            self.context_.close()


def in_thread(function):
    box = {}

    def run():
        try:
            box['value'] = function()
        except BaseException as error:
            box['error'] = error
    thread = threading.Thread(target=run)
    thread.start()
    thread.join(10)
    assert not thread.is_alive()
    if 'error' in box:
        raise box['error']
    return box.get('value')


class FakeApp:
    def __init__(self):
        self.stop = threading.Event()

    def exec(self):
        self.stop.wait(10)

    def quit(self):
        self.stop.set()


class ServeTests(unittest.TestCase):
    def setUp(self):
        self.apps, self.loops, self.abandoned = [], [], []
        self.close = patch.object(qt_engine.Loop, 'close', autospec=True).start()
        self.opening = patch.object(qt_engine.Loop, 'open', autospec=True, side_effect=self.open).start()
        # Nothing in these tests may end the test process: the work thread would call this where it is patched.
        self.abandon = patch.object(qt_engine, '_abandon', side_effect=lambda outcome: self.abandoned.append(dict(outcome))).start()
        self.addCleanup(patch.stopall)
        self.assertIsNone(qt_engine._serving)

    def tearDown(self):
        for thread in threading.enumerate():
            if thread.name == 'tapas-downloader':
                thread.join(10)
                self.assertFalse(thread.is_alive())
        self.assertEqual(self.abandoned, [])

    def open(self, loop, headless):
        loop.app = FakeApp()
        # The bridge to Qt's thread, run on the spot.
        loop.bridge = SimpleNamespace(run=SimpleNamespace(emit=lambda job: job()))
        self.apps.append(loop.app)
        self.loops.append(loop)

    def test_serve_returns_what_the_work_returns_from_a_thread_of_its_own_and_ends_the_engine(self):
        names = []

        def work():
            names.append(threading.current_thread())
            return qt_engine.Driver().launch(headless=True, timeout=5000).loop.headless
        self.assertIs(qt_engine.serve(work), True)
        self.assertIsNot(names[0], threading.main_thread())
        self.assertTrue(self.apps[0].stop.is_set())
        self.assertIsNone(qt_engine._serving)
        self.close.assert_called_once()
        self.assertEqual(self.loops[0].work.name, 'tapas-downloader')
        self.assertEqual(self.abandoned, [])

    def test_serve_removes_inherited_web_engine_overrides_before_starting(self):
        unsafe = dict.fromkeys(qt_engine.UNSAFE_QT_ENVIRONMENT, 'unsafe')
        with patch.dict(qt_engine.os.environ, unsafe):
            self.assertEqual(qt_engine.serve(lambda: 'done'), 'done')
            for name in unsafe:
                self.assertNotIn(name, qt_engine.os.environ)

    def test_work_that_never_launches_the_engine_never_starts_qt(self):
        self.assertEqual(qt_engine.serve(lambda: 'done'), 'done')
        self.opening.assert_not_called()
        self.close.assert_not_called()
        self.assertEqual(self.abandoned, [])

    def test_the_works_exception_is_raised_to_the_caller_after_the_engine_has_ended(self):
        def work():
            qt_engine.Driver().launch(headless=True, timeout=5000)
            raise ValueError('the work failed')
        with self.assertRaisesRegex(ValueError, 'the work failed'):
            qt_engine.serve(work)
        self.assertTrue(self.apps[0].stop.is_set())
        self.assertIsNone(qt_engine._serving)
        with self.assertRaises(KeyboardInterrupt):
            qt_engine.serve(self.interrupted)
        self.assertEqual(qt_engine.serve(lambda: 1), 1)

    @staticmethod
    def interrupted():
        raise KeyboardInterrupt

    def test_a_second_serve_while_one_runs_is_refused_and_the_first_is_not_disturbed(self):
        seen = []

        def run(loop):
            try:
                qt_engine.serve(lambda: 'second')
            except Error as error:
                seen.append(str(error))
        with patch.object(qt_engine.Loop, 'run', autospec=True, side_effect=run):
            self.assertEqual(qt_engine.serve(lambda: 'first'), 'first')
        self.assertEqual(seen, ['The built-in engine is served once, from the main thread.'])
        self.assertEqual(qt_engine.serve(lambda: 'again'), 'again')

    def test_serve_called_from_another_thread_is_refused_and_leaves_nothing_behind(self):
        with self.assertRaisesRegex(Error, 'once, from the main thread'):
            in_thread(lambda: qt_engine.serve(lambda: 1))
        self.assertIsNone(qt_engine._serving)
        self.assertEqual(qt_engine.serve(lambda: 2), 2)

    def test_the_engine_is_started_hidden_or_in_a_window_as_the_first_caller_decides(self):
        def work():
            driver = qt_engine.Driver()
            first = driver.launch(headless=True, timeout=5000)
            second = driver.launch(headless=True, timeout=5000)
            with self.assertRaisesRegex(Error, '^The built-in engine is already running hidden.$'):
                driver.launch(headless=False, timeout=5000)
            return first.loop is second.loop
        self.assertTrue(qt_engine.serve(work))
        self.assertEqual(self.opening.call_count, 1)
        self.assertEqual(self.opening.call_args.args[1], True)

        def window():
            qt_engine.Driver().launch(headless=False, timeout=5000)
            with self.assertRaisesRegex(Error, 'already running in a window'):
                qt_engine.Driver().launch(headless=True, timeout=5000)
        qt_engine.serve(window)

    def test_a_missing_qt_and_any_other_failure_to_start_are_told_apart_without_the_failures_text(self):
        for failure, message in ((ImportError('PRIVATE'), 'The built-in engine is not installed.'),
                                 (RuntimeError('PRIVATE'), 'The built-in engine could not start.')):
            self.opening.side_effect = failure
            with self.subTest(failure=failure), self.assertRaises(Error) as raised:
                qt_engine.serve(lambda: qt_engine.Driver().launch(headless=True, timeout=5000))
            self.assertEqual(str(raised.exception), message)

    def test_an_engine_that_is_slow_to_start_is_reported_not_waited_for_for_ever(self):
        release = threading.Event()

        def slow(loop, headless):
            release.wait(10)
            self.open(loop, headless)
        self.opening.side_effect = slow

        def work():
            try:
                qt_engine.Driver().launch(headless=True, timeout=100)
            except Error as error:
                return str(error)
            finally:
                release.set()
        self.assertEqual(qt_engine.serve(work), 'The built-in engine did not start in time.')

    def test_a_process_whose_qt_does_not_leave_its_loop_in_time_is_ended_with_what_the_work_earned(self):
        released = threading.Event()

        def abandon(outcome):
            self.abandoned.append(dict(outcome))
            released.set()
        self.abandon.side_effect = abandon

        def stuck(loop):
            released.wait(10)
        failure = ValueError('the work failed')

        def failing():
            raise failure
        with patch.object(qt_engine, 'LINGER', 0.05), patch.object(qt_engine.Loop, 'run', autospec=True, side_effect=stuck):
            self.assertEqual(qt_engine.serve(lambda: 7), 7)
            self.assertEqual(self.abandoned, [{'value': 7}])
            released.clear()
            with self.assertRaises(ValueError):
                qt_engine.serve(failing)
        self.assertEqual(self.abandoned, [{'value': 7}, {'error': failure}])
        del self.abandoned[:]

    def interrupting(self, interrupts, ready):
        """Qt's loop as the main thread runs it: once the work is under way, this many interrupts arrive."""
        def opening(loop, headless):
            self.open(loop, headless)
            loop.q = SimpleNamespace(QtCore=SimpleNamespace(QTimer=SimpleNamespace(singleShot=lambda delay, action: action())))

            def run_qt():
                self.assertTrue(ready.wait(10))
                for _ in range(interrupts):
                    loop.interrupt()
                loop.app.stop.wait(10)
            loop.app.exec = run_qt
        self.opening.side_effect = opening

    def test_the_first_interrupt_goes_to_the_work_which_unwinds_with_the_engine_still_there_and_serve_raises_it(self):
        ready, seen = threading.Event(), []
        self.interrupting(1, ready)

        def work():
            qt_engine.Driver().launch(headless=True, timeout=5000)
            try:
                ready.set()
                for _ in range(1000):
                    time.sleep(0.01)
            finally:
                seen.append(self.apps[0].stop.is_set())
        previous = signal.getsignal(signal.SIGINT)
        with self.assertRaises(KeyboardInterrupt):
            qt_engine.serve(work)
        self.assertEqual(seen, [False])
        self.assertEqual((self.loops[0].interrupted, self.loops[0].forced), (True, False))
        self.assertTrue(self.apps[0].stop.is_set())
        self.assertIs(signal.getsignal(signal.SIGINT), previous)
        self.assertIsNone(qt_engine._serving)

    def test_an_interrupt_that_the_work_took_quietly_does_not_make_serve_raise(self):
        ready = threading.Event()
        self.interrupting(1, ready)

        def work():
            qt_engine.Driver().launch(headless=True, timeout=5000)
            try:
                ready.set()
                for _ in range(1000):
                    time.sleep(0.01)
            except KeyboardInterrupt:
                return 'wound up'
        self.assertEqual(qt_engine.serve(work), 'wound up')
        self.assertFalse(self.loops[0].forced)

    def test_a_second_interrupt_ends_serve_with_a_keyboard_interrupt_whatever_the_work_returned(self):
        ready = threading.Event()
        self.interrupting(2, ready)

        def work():
            qt_engine.Driver().launch(headless=True, timeout=5000)
            try:
                ready.set()
                for _ in range(1000):
                    time.sleep(0.01)
            except KeyboardInterrupt:
                return 'wound up'
        previous = signal.getsignal(signal.SIGINT)
        with self.assertRaises(KeyboardInterrupt):
            qt_engine.serve(work)
        self.assertTrue(self.loops[0].forced)
        self.assertIs(signal.getsignal(signal.SIGINT), previous)
        self.assertIsNone(qt_engine._serving)

    def test_work_that_ended_while_qt_was_still_starting_does_not_enter_qts_loop_but_still_closes(self):
        loop = qt_engine.Loop()

        def opening(loop, headless):
            self.open(loop, headless)
            loop.app.exec = Mock()
            loop.over = True
        self.opening.side_effect = opening
        loop.requests.put(True)
        loop.run()
        loop.app.exec.assert_not_called()
        self.close.assert_called_once_with(loop)
        self.assertTrue(loop.started.is_set())

    def test_a_call_runs_on_the_main_thread_directly_and_from_another_thread_through_the_bridge(self):
        loop = qt_engine.Loop()
        self.assertEqual(loop.call(lambda: 5), 5)
        with self.assertRaisesRegex(Error, 'not running'):
            in_thread(lambda: loop.call(lambda: 1))
        loop.bridge = SimpleNamespace(run=SimpleNamespace(emit=lambda job: job()))
        self.assertEqual(in_thread(lambda: loop.call(lambda: 5)), 5)

        def fail():
            raise ValueError('inside qt')
        with self.assertRaisesRegex(ValueError, 'inside qt'):
            in_thread(lambda: loop.call(fail))
        loop.bridge = SimpleNamespace(run=SimpleNamespace(emit=lambda job: None))
        with self.assertRaisesRegex(qt_engine.TimeoutError, 'did not answer'):
            in_thread(lambda: loop.call(lambda: 1, seconds=0.05))

    def test_ask_returns_the_answer_given_later_and_times_out_without_one(self):
        loop = qt_engine.Loop()
        self.assertEqual(loop.ask(lambda answer: threading.Timer(0.05, answer, ['late']).start(), 5), 'late')
        with self.assertRaisesRegex(qt_engine.TimeoutError, 'The page did not answer.'):
            loop.ask(lambda answer: None, 0.05)


class AbandonTests(unittest.TestCase):
    """What a process leaves with when Qt will not end. The real os._exit is never reachable here."""

    def status(self, outcome):
        with patch.object(qt_engine.os, '_exit') as leave:
            qt_engine._abandon(outcome)
        leave.assert_called_once()
        return leave.call_args.args[0]

    def test_the_status_is_the_works_own_code_one_for_a_failure_and_nothing_wrong_for_no_value(self):
        cases = [({}, 0), ({'value': None}, 0), ({'value': 0}, 0), ({'value': 3}, 3), ({'value': 'text'}, 1),
                 ({'error': ValueError('failed')}, 1), ({'error': KeyboardInterrupt()}, 1), ({'error': ValueError(), 'value': 4}, 1),
                 ({'error': SystemExit(5)}, 5), ({'error': SystemExit()}, 0), ({'error': SystemExit(0)}, 0), ({'error': SystemExit('why')}, 1)]
        for outcome, status in cases:
            with self.subTest(outcome=outcome):
                self.assertEqual(self.status(outcome), status)

    def test_the_streams_are_flushed_before_the_process_ends_and_one_that_cannot_be_flushed_does_not_stop_it(self):
        order = []

        def broken(error):
            def flush():
                order.append('broken')
                raise error
            return flush
        for error in (ValueError('I/O operation on closed file'), OSError('broken pipe')):
            del order[:]
            out, err = SimpleNamespace(flush=broken(error)), SimpleNamespace(flush=lambda: order.append('stderr'))
            with self.subTest(error=error), patch.object(sys, 'stdout', out), patch.object(sys, 'stderr', err), \
                    patch.object(qt_engine.os, '_exit', side_effect=lambda status: order.append(('exit', status))):
                qt_engine._abandon({'value': 2})
            self.assertEqual(order, ['broken', 'stderr', ('exit', 2)])


class LoopTests(unittest.TestCase):
    """The main thread's part on its own, with Qt's application stood in for."""

    @staticmethod
    def queued_quits(loop):
        """Qt's application, and a queue for what is handed to its timer, which runs nothing."""
        queued = []
        loop.app = FakeApp()
        loop.q = SimpleNamespace(QtCore=SimpleNamespace(QTimer=SimpleNamespace(singleShot=lambda delay, action: queued.append((delay, action)))))
        return queued

    def working(self, loop):
        """A work thread that takes each interrupt quietly and carries on. 'got' is set when one has come."""
        release, entered, got = threading.Event(), threading.Event(), threading.Event()

        def work():
            while not release.is_set():
                try:
                    entered.set()
                    while not release.is_set():
                        time.sleep(0.01)
                except KeyboardInterrupt:
                    got.set()
        loop.work = threading.Thread(target=work, daemon=True)
        loop.work.start()
        self.addCleanup(loop.work.join, 5)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(5))
        return got

    def test_the_first_interrupt_is_handed_to_the_live_work_and_the_second_queues_the_end_of_qt(self):
        loop = qt_engine.Loop()
        queued, got = self.queued_quits(loop), self.working(loop)
        loop.interrupt()
        self.assertTrue(got.wait(5))
        self.assertEqual((loop.interrupted, loop.forced, queued, loop.app.stop.is_set()), (True, False, [], False))
        loop.interrupt()
        self.assertEqual((loop.forced, queued), (True, [(0, loop.app.quit)]))
        self.assertFalse(loop.app.stop.is_set())

    def test_before_qt_is_up_the_second_interrupt_raises_where_the_first_went_to_the_work(self):
        loop = qt_engine.Loop()
        got = self.working(loop)
        loop.interrupt()
        self.assertTrue(got.wait(5))
        self.assertFalse(loop.forced)
        with self.assertRaises(KeyboardInterrupt):
            loop.interrupt()
        self.assertTrue(loop.forced)

    def test_an_interrupt_after_the_work_is_over_is_for_qt_not_for_the_thread_that_waits_for_it_to_end(self):
        loop = qt_engine.Loop()
        queued, got = self.queued_quits(loop), self.working(loop)
        loop.over = True
        loop.interrupt()
        self.assertEqual((loop.interrupted, loop.forced, queued), (False, True, [(0, loop.app.quit)]))
        self.assertFalse(got.wait(0.3))

    def test_an_interrupt_with_no_live_work_ends_qt_or_the_wait_at_once(self):
        loop = qt_engine.Loop()
        with self.assertRaises(KeyboardInterrupt):
            loop.interrupt()
        self.assertTrue(loop.forced)
        loop = qt_engine.Loop()
        queued = self.queued_quits(loop)
        loop.work = threading.Thread(target=lambda: None)
        loop.work.start()
        loop.work.join()
        loop.interrupt()
        self.assertEqual((loop.interrupted, loop.forced, queued), (False, True, [(0, loop.app.quit)]))

    def test_finish_asks_qt_to_quit_only_when_it_started_and_waits_for_it_no_longer_than_the_linger(self):
        cases = ((True, None, True, 'started'), (True, RuntimeError('failed'), False, 'failed to start'),
                 (False, None, False, 'still starting'))
        for started, failure, quits, why in cases:
            with self.subTest(why), patch.object(qt_engine, 'LINGER', 0.05):
                loop, emitted = qt_engine.Loop(), []
                loop.headless, loop.app = True, FakeApp()
                loop.bridge = SimpleNamespace(run=SimpleNamespace(emit=emitted.append))
                loop.failure = failure
                if started:
                    loop.started.set()
                begun = time.monotonic()
                loop.finish()
                self.assertLess(time.monotonic() - begun, 5)
                self.assertEqual(emitted, [loop.app.quit] if quits else [])
                self.assertTrue(loop.over)
                self.assertIsNone(loop.requests.get_nowait())

    def test_finish_for_work_that_never_asked_for_qt_waits_for_nothing_and_says_the_work_is_over(self):
        loop = qt_engine.Loop()
        loop.bridge = SimpleNamespace(run=SimpleNamespace(emit=lambda job: self.fail('qt was asked to quit')))
        with patch.object(qt_engine, 'LINGER', 10):
            begun = time.monotonic()
            loop.finish()
        self.assertLess(time.monotonic() - begun, 5)
        self.assertEqual((loop.over, loop.headless), (True, False))
        self.assertIsNone(loop.requests.get_nowait())

    def test_a_call_after_the_engine_has_ended_is_refused_before_it_is_handed_over_and_inside_the_queued_job(self):
        loop, handed, ran = qt_engine.Loop(), [], []
        loop.bridge = SimpleNamespace(run=SimpleNamespace(emit=handed.append))
        loop.ended = True
        with self.assertRaisesRegex(Error, '^The built-in engine is not running.$'):
            in_thread(lambda: loop.call(lambda: ran.append('ran')))
        self.assertEqual((handed, ran), ([], []))

        def end_then_run(job):
            loop.ended = True
            job()
        loop.ended = False
        loop.bridge = SimpleNamespace(run=SimpleNamespace(emit=end_then_run))
        with self.assertRaisesRegex(Error, '^The built-in engine is not running.$'):
            in_thread(lambda: loop.call(lambda: ran.append('ran')))
        self.assertEqual(ran, [])

    def test_ask_spends_one_limit_on_the_hand_over_and_the_wait_for_the_answer(self):
        clock, loop, limits = Clock(), qt_engine.Loop(), []

        def call(function, seconds=30):
            limits.append(seconds)
            clock.now += 3.5
            return function()
        loop.call = call
        begun = time.monotonic()
        with patch.object(qt_engine, 'time', clock), self.assertRaisesRegex(qt_engine.TimeoutError, 'The page did not answer.'):
            loop.ask(lambda answer: None, 3)
        self.assertEqual(limits, [3])
        self.assertLess(time.monotonic() - begun, 2)
        with patch.object(qt_engine, 'time', clock):
            self.assertEqual(loop.ask(lambda answer: answer('quick'), 3), 'quick')


if __name__ == '__main__':
    unittest.main()
