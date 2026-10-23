"""Native seam replay, not WebView execution. The emulator test qualifies the real world/window."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import httpx

from comic_archive import android_session, qt_engine as dom, sites
from comic_archive.android_browser import (
    AndroidBrowserContext, AndroidBrowserTransport, AndroidFastImportAdapter,
    AndroidFastWorkerSession, AndroidTapasBrowserAdapter, interrupt_contexts,
    _CONTROLLED, _SERVICE_WORKERS, _WORKER_GUARD_HEAD, _WORKER_GUARD_TAIL,
)
from comic_archive.cancellation import RunCancelled
from comic_archive.errors import AuthenticationError
from comic_archive.fast_import import FAST_INTERVAL, FAST_METADATA_INTERVAL
from comic_archive.models import ArchiveError
from comic_archive.network import NetworkPolicyHold
from test_qt_engine import ONE_HEAD, ONE_TAIL, Site, parse_call
import test_tapas_http as golden_http


class Store:
    def __init__(self):
        self.value, self.reads, self.writes = 'session=FIRST', [], []

    def headerFor(self, url):
        self.reads.append(url)
        return self.value

    def apply(self, url, values):
        self.writes.append((url, values))


class Native:
    def __init__(self):
        self.site, self.calls = Site(), []
        self.closed = False
        self.on_release = self.on_execute = None
        self.workers = {'registered': 0, 'controlled': False, 'cleared': True}
        self.worker_reads = []

    def state(self):
        return json.dumps({'v': 1, 'url': self.site.document['url'], 'ended': int(self.site.document['id']),
            'failures': 0, 'failure': None, 'gone': False, 'closed': self.closed,
            'agent': 'Mozilla/5.0 NativeFixture'})

    def load(self, url):
        self.calls.append(('load', url))
        self.site.navigate(url)

    def prepareNavigation(self):
        self.calls.append(('prepare',))

    def execute(self, source, bound):
        self.calls.append(('execute', bound))
        guarded = source.startswith(_WORKER_GUARD_HEAD)
        if guarded:
            if self.workers['controlled']:
                return json.dumps(_CONTROLLED)
            source = source[len(_WORKER_GUARD_HEAD):-len(_WORKER_GUARD_TAIL)]
        if source.startswith(dom.CALL.split('%')[0]):
            script, _, argument = parse_call(source)
            if script == _SERVICE_WORKERS:
                self.worker_reads.append(argument)
                return json.dumps(json.dumps({'value': self.workers.copy()}))
        if self.on_execute is not None:
            return self.on_execute(source, bound)
        return json.dumps(self.site(source))

    def pointer(self, kind, x, y):
        self.calls.append(('pointer', kind, x, y))
        if kind == 'release' and self.on_release is not None:
            self.on_release()

    def wheel(self, *args):
        self.calls.append(('wheel', *args))

    def close(self):
        self.calls.append(('close',))
        self.closed = True

    def interrupt(self):
        self.calls.append(('interrupt',))


class Factory:
    def __init__(self):
        self.native, self.store, self.opens = Native(), Store(), []
        self.enabled = True

    def available(self):
        return self.enabled

    def open(self, policy):
        if self.native.closed:
            self.native = Native()
        self.opens.append(policy)
        return self.native

    def cookieStore(self):
        return self.store

    def interrupt(self):
        self.native.interrupt()
        self.native.close()

    def closeAll(self):
        self.native.close()
        return True


class BrowserFacadeTests(unittest.TestCase):
    def setUp(self):
        self.factory = Factory()
        self.context = AndroidBrowserContext(self.factory, lambda: None)
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.native = self.factory.native

    def test_blank_page_needs_no_script_authority_and_promises_use_the_existing_facade(self):
        self.assertEqual(self.page.url, 'about:blank')
        self.assertFalse(self.native.site.scripts)
        self.page.goto('https://tapas.io/', wait_until='domcontentloaded')
        self.native.site.reply = lambda source, steps, argument: {'pending': 1}
        self.native.site.pending[1] = [json.dumps({'value': {'answer': 42}})]
        self.assertEqual(self.page.evaluate('async n => ({answer:n})', 42), {'answer': 42})
        self.assertEqual(self.native.site.calls[-1], ('async n => ({answer:n})', None, 42))
        self.assertTrue(all(1 <= call[1] <= 45000 for call in self.native.calls if call[0] == 'execute'))

    def test_visible_guarded_pager_uses_native_pointer_and_keeps_dom_reads_until_navigation(self):
        self.page.goto('https://tapas.io/reading-list?category=RECENT', wait_until='domcontentloaded')
        aim = {'ready': True, 'x': 30, 'y': 40, 'width': 20, 'height': 20}

        def reply(source, steps, argument):
            if source.startswith(ONE_HEAD):
                action = source[len(ONE_HEAD):-len(ONE_TAIL)]
                result = True if "box.hit = 'armed'" in action else aim
                return {'value': {'found': True, 'value': result}}
            return {'value': True}

        self.native.site.reply = reply
        target = 'https://tapas.io/reading-list?category=RECENT&pageNumber=2'
        self.native.on_release = lambda: self.native.site.navigate(target)
        with self.page.expect_navigation(wait_until='domcontentloaded', timeout=1000) as expected:
            self.assertTrue(self.page.evaluate('() => true'))
            self.page.locator('a.paging__button--next').click(timeout=1000)
        self.assertEqual((expected.value.url, expected.value.status), (target, 200))
        controls = [call for call in self.native.calls if call[0] in {'prepare', 'pointer'}]
        self.assertEqual(controls, [('prepare',), ('pointer', 'move', 30.0, 40.0),
            ('pointer', 'press', 30.0, 40.0), ('pointer', 'release', 30.0, 40.0)])

    def test_mutations_untrusted_schemes_and_malformed_authorities_are_refused(self):
        for url, method in [('https://tapas.io/unlock', 'POST'), ('https://tapas.io/', 'PUT'),
                ('https://tapas.io/', 'DELETE'), ('http://tapas.io/', 'GET'), ('data:text/plain,a', 'GET'),
                ('https://person:secret@tapas.io/', 'GET'), ('https://tapas.io:8443/', 'GET'),
                ('https://tapas.io:bad/', 'GET')]:
            with self.subTest(method=method):
                self.assertFalse(self.context.allowRequest(url, method, 'xhr'))
        self.assertTrue(self.context.allowRequest('https://tapas.io/who-am-i', 'GET', 'fetch'))
        self.context.close()
        self.assertFalse(self.context.allowRequest('https://tapas.io/', 'GET', 'document'))

    def test_page_rules_precede_context_rules_and_no_decision_is_not_permission(self):
        calls = []
        def fallback(route):
            calls.append('context')
            route.fallback()
        def denied(route):
            calls.append('page')
            route.abort()
        self.context.route('**/*', fallback)
        self.page.route('**/*', denied)
        self.assertFalse(self.context.allowRequest('https://tapas.io/', 'GET', 'document'))
        self.assertEqual(calls, ['page'])
        self.page.unroute('**/*', denied)
        self.assertTrue(self.context.allowRequest('https://tapas.io/', 'GET', 'document'))
        self.context.route('**/*', lambda route: None)
        self.assertFalse(self.context.allowRequest('https://tapas.io/', 'GET', 'document'))
        with self.assertRaises(dom.Error):
            self.page.route('https://tapas.io/*', denied)

    def test_request_thread_policy_hold_survives_a_native_script_failure_as_a_typed_hold(self):
        held = NetworkPolicyHold('Wi-Fi only: held.')
        self.context.check_callback = lambda: (_ for _ in ()).throw(held)
        result = []
        thread = threading.Thread(target=lambda: result.append(
            self.context.allowRequest('https://tapas.io/', 'GET', 'document')))
        thread.start()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [False])
        self.context.check_callback = lambda: None
        with self.assertRaises(NetworkPolicyHold) as caught:
            self.page.evaluate('() => 1')
        self.assertIs(caught.exception, held)

    def test_cancellation_replaces_a_raw_native_error_without_exposing_it(self):
        def broken(source, bound):
            self.context.check_callback = lambda: (_ for _ in ()).throw(RunCancelled('Stopped.'))
            raise RuntimeError('PRIVATE_URL_AND_SCRIPT')
        self.native.on_execute = broken
        with self.assertRaises(RunCancelled) as caught:
            self.page.evaluate('() => 1')
        self.assertNotIn('PRIVATE', str(caught.exception))

    def test_bad_native_state_closes_partial_allocation_and_no_storage_state_can_be_saved(self):
        other = Factory()
        other.native.state = lambda: json.dumps({'v': True, 'agent': 'PRIVATE'})
        with self.assertRaises(ArchiveError) as caught:
            AndroidBrowserContext(other, lambda: None)
        self.assertTrue(other.native.closed)
        self.assertNotIn('PRIVATE', str(caught.exception))
        self.assertEqual(self.context.cookies(), [])
        with self.assertRaises(ArchiveError):
            self.context.storage_state()

    def test_explicit_pause_is_sticky_and_scoped_to_its_own_factory(self):
        other = AndroidBrowserContext(Factory(), lambda: None)
        self.addCleanup(other.close)
        interrupt_contexts(self.factory)
        for _ in range(2):
            with self.assertRaises(RunCancelled):
                self.page.evaluate('() => 1')
        other.check()
        self.assertIn(('interrupt',), self.native.calls)


class BrowserAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.session = Path(self.temporary.name) / '.comic-session'
        android_session.mark_signed_in(self.session, compatibility_state=True)
        self.factory = Factory()
        self.guard = Mock()

    def test_capture_reuses_the_real_http_access_and_reader_rules_without_a_session_copy(self):
        fixture = golden_http.TapasHTTPTests()
        fixture.setUp()
        fixture.responses()
        expected = fixture.adapter.capture('https://tapas.io/episode/81')
        responses = {'https://tapas.io/who-am-i': golden_http.envelope({'user': {'id': 123}}),
            'https://tapas.io/episode/81/info': golden_http.envelope(fixture.info),
            'https://tapas.io/episode/81': fixture.html,
            'https://tapas.io/series/42': golden_http.envelope({'id': 42, 'title': 'Series',
                'url': 'series-slug', 'comic': True, 'book': False})}
        requests = []
        def answer(source, steps, argument):
            requests.append(argument['url'])
            return {'value': {'status': 200, 'text': responses[argument['url']].decode()}}
        self.factory.native.site.reply = answer
        before = {p.name: p.read_bytes() for p in self.session.iterdir() if p.is_file()}
        with patch('comic_archive.adapters.tapas.browser_api', side_effect=AssertionError('Desktop startup')), \
                patch('comic_archive.adapters.tapas.save_session', side_effect=AssertionError('Session copy')):
            with AndroidTapasBrowserAdapter(self.session, self.factory, network_guard=self.guard) as adapter:
                adapter.page.wait_for_timeout = lambda milliseconds: None
                adapter.expected_account_id = '123'
                actual = adapter.capture_automatic('https://tapas.io/episode/81')
                self.assertEqual(actual, expected)
        self.assertEqual(requests, list(responses))
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.session.iterdir() if p.is_file()})
        self.assertTrue(self.factory.native.closed)

    def test_live_native_cookies_are_requested_per_asset_and_never_become_an_httpx_jar(self):
        heard = []
        def response(request):
            heard.append((request.url.host, request.headers.get('cookie')))
            return httpx.Response(200, content=b'asset', headers={'set-cookie': 'poison=never-reuse; Path=/'})
        client = httpx.Client(transport=httpx.MockTransport(response))
        context = AndroidBrowserContext(self.factory, lambda: None)
        self.addCleanup(context.close)
        context.new_page()
        transport = AndroidBrowserTransport(context, fast=True, interval=FAST_INTERVAL,
            metadata_interval=FAST_METADATA_INTERVAL, network_guard=self.guard, client=client)
        self.addCleanup(transport.close)
        transport.assets.get('https://tapas.io/asset')
        self.factory.store.value = 'session=SECOND'
        transport.assets.get('https://tapas.io/asset')
        transport.assets.get('https://us-a.tapas.io/pc/asset')
        self.assertEqual(heard, [('tapas.io', 'session=FIRST'), ('tapas.io', 'session=SECOND'),
            ('us-a.tapas.io', None)])
        self.assertEqual(len(self.factory.store.reads), 2)
        self.assertEqual(list(client.cookies.jar), [])
        self.assertEqual((transport.assets.interval, transport.metadata_interval),
                         (FAST_INTERVAL, FAST_METADATA_INTERVAL))

    def test_wrong_site_missing_capability_and_manual_capture_never_start_a_desktop_browser(self):
        android_session.mark_signed_in(self.session, site=sites.find('webtoon'), compatibility_state=True)
        with self.assertRaises(AuthenticationError):
            AndroidTapasBrowserAdapter(self.session, self.factory).__enter__()
        self.assertFalse(self.factory.opens)
        android_session.mark_signed_in(self.session, compatibility_state=True)
        self.factory.enabled = False
        with self.assertRaises(ArchiveError):
            AndroidTapasBrowserAdapter(self.session, self.factory).__enter__()
        self.assertFalse(self.factory.opens)
        with self.assertRaises(ArchiveError):
            AndroidTapasBrowserAdapter(self.session, self.factory).capture('https://tapas.io/episode/81')

    def test_held_native_adapter_is_lazy_and_fast_uses_the_existing_single_owner_pipeline(self):
        held = AndroidTapasBrowserAdapter(self.session, self.factory, network_guard=self.guard)
        self.assertFalse(self.factory.opens)
        held.close()
        self.assertFalse(self.factory.opens)
        with AndroidFastImportAdapter(self.session, self.factory, self.guard) as pipeline:
            self.assertEqual(pipeline.download_profile, 'fast')
            self.assertTrue(pipeline._inner.fast)
            self.assertEqual(pipeline._inner.interval, FAST_INTERVAL)
            self.assertIs(pipeline._inner.network_guard, self.guard)
        self.assertTrue(self.factory.native.closed)

    def test_fast_pool_uses_native_factory_and_closes_it_when_android_sign_in_is_forgotten(self):
        (self.session / 'state.json').unlink()  # Android's own marker/site are the authority.
        with patch('comic_archive.android_browser.AndroidFastImportAdapter') as constructor:
            adapter = constructor.return_value
            adapter.reusable = True
            with AndroidFastWorkerSession(self.session, self.factory, self.guard) as pool:
                with pool.batch() as first:
                    self.assertIs(first, adapter)
                with pool.batch() as second:
                    self.assertIs(second, adapter)
                constructor.assert_called_once_with(self.session, self.factory, self.guard)
                self.assertEqual(adapter.begin_batch.call_count, 2)
                android_session.forget(self.session)
                with self.assertRaises(AuthenticationError):
                    with pool.batch():
                        self.fail('Forgotten sign-in admitted a new batch')
                self.assertIsNone(pool.adapter)
            adapter.__exit__.assert_called_once()

    def test_only_a_new_task_admission_discards_the_cancelled_reading_document(self):
        adapter = AndroidTapasBrowserAdapter(self.session, self.factory, network_guard=self.guard)
        self.addCleanup(adapter.close)
        adapter.__enter__()
        old = adapter.context
        interrupt_contexts(self.factory)
        with self.assertRaises(RunCancelled):
            old.check()
        adapter.begin_task()
        self.assertTrue(old.closed)
        self.assertIsNone(adapter.context)
        self.assertEqual(len(self.factory.opens), 1)  # Admission still does not load a page.
        adapter.__enter__()
        self.assertIsNot(adapter.context, old)
        adapter.context.check()

    def test_cached_worker_is_removed_then_its_entire_document_is_replaced(self):
        old = self.factory.native
        old.workers = {'registered': 2, 'controlled': True, 'cleared': True}
        before = {p.name: p.read_bytes() for p in self.session.iterdir() if p.is_file()}
        with AndroidTapasBrowserAdapter(self.session, self.factory, network_guard=self.guard) as adapter:
            self.assertTrue(old.closed)
            self.assertIsNot(adapter.context.surface.native, old)
            self.assertEqual(old.worker_reads, [True])
            self.assertEqual(self.factory.native.worker_reads, [False])
            self.assertEqual(len(self.factory.opens), 2)
            self.assertTrue(adapter.context.workers_ready)
            self.assertEqual([call for call in self.factory.native.calls if call[0] == 'load'],
                             [('load', 'https://tapas.io/')])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.session.iterdir() if p.is_file()})

    def test_failed_worker_cleanup_closes_the_reader_before_any_metadata_request(self):
        self.factory.native.workers['cleared'] = False
        self.factory.native.site.reply = Mock(side_effect=AssertionError('Provider metadata consumed'))
        with self.assertRaisesRegex(ArchiveError, 'cached reading worker'):
            AndroidTapasBrowserAdapter(self.session, self.factory, network_guard=self.guard).__enter__()
        self.assertTrue(self.factory.native.closed)
        self.factory.native.site.reply.assert_not_called()

    def test_a_controller_remaining_in_the_replacement_window_refuses_the_whole_open(self):
        self.factory.native.workers['controlled'] = True
        open_native = self.factory.open
        def still_controlled(policy):
            native = open_native(policy)
            native.workers['controlled'] = True
            return native
        with patch.object(self.factory, 'open', side_effect=still_controlled):
            with self.assertRaisesRegex(ArchiveError, 'still active'):
                AndroidTapasBrowserAdapter(self.session, self.factory, network_guard=self.guard).__enter__()
        self.assertEqual(len(self.factory.opens), 2)
        self.assertTrue(self.factory.native.closed)

    def test_new_controller_after_preflight_refuses_the_value_and_closes_the_reader(self):
        with AndroidTapasBrowserAdapter(self.session, self.factory, network_guard=self.guard) as adapter:
            self.factory.native.workers['controlled'] = True
            self.factory.native.site.reply = Mock(side_effect=AssertionError('Stale data consumed'))
            with self.assertRaisesRegex(ArchiveError, 'controlled by a cached worker'):
                adapter.page.evaluate('() => "stale"')
            self.factory.native.site.reply.assert_not_called()
            self.assertTrue(self.factory.native.closed)
            self.assertTrue(adapter.context.closed)
