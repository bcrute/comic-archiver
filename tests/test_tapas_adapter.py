"""Synthetic browser observations for the Tapas adapter's refusals and decisions. They do not validate live Tapas markup."""
import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from comic_archive.adapters.tapas import BrowserReadTransport, TapasAdapter
from comic_archive.errors import AccessDenied, AuthenticationError, NetworkUnavailable, RunLimit
from comic_archive.models import ArchiveError, Episode, Manifest, Page, Series
from comic_archive.network import NetworkPolicyHold

EPISODE = 'https://tapas.io/episode/81'
CHROME = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36'


def lazy_rows(count=2, base='a', signature='TEST'):
    return [{'tag': 'IMG', 'url': 'data:image/gif;base64,x', 'loaded': True,
             'sources': {'data-src': f'https://us-a.tapas.io/pc/{base}/{i}.jpg?sig={signature}'}} for i in range(count)]


class BrowserTransportTests(unittest.TestCase):
    @patch('comic_archive.adapters.tapas.Transport')
    def test_network_policy_hold_aborts_every_browser_request_instead_of_continuing(self, assets):
        guard = Mock()
        guard.check.side_effect = NetworkPolicyHold('Wi-Fi only: transfers held.')
        transport = BrowserReadTransport(Mock(), Mock(), network_guard=guard)
        adapter = TapasAdapter(Path('unused-test-session'))
        adapter.network_guard = guard
        for handler in (transport.read_only_request,
                        adapter.network_request, adapter.network_read_only_request):
            route = Mock()
            route.request.method = 'GET'
            route.request.url = 'https://tapas.io/series/42'
            route.request.resource_type = 'fetch'
            handler(route)
            route.abort.assert_called_once()
            route.continue_.assert_not_called()
            route.fallback.assert_not_called()
        with self.assertRaises(NetworkPolicyHold):
            transport.get('https://tapas.io/series/42')
        guard.check.side_effect = None
        route = Mock()
        adapter.network_request(route)
        route.fallback.assert_called_once()
        route.abort.assert_not_called()

    def test_images_are_fetched_under_the_name_the_browser_has_and_under_none_when_it_has_no_name(self):
        for context, agent in ((Mock(agent=CHROME), CHROME), (Mock(), None), (SimpleNamespace(), None)):
            context.cookies = lambda: []
            with self.subTest(agent=agent):
                transport = BrowserReadTransport(Mock(), context)
                self.addCleanup(transport.assets.client.close)
                # A browser without a name of its own (Playwright's) or one that is not text leaves the client's own.
                self.assertEqual(transport.assets.agent, agent)

    @patch('comic_archive.adapters.tapas.Transport')
    def test_exhausted_job_budget_stops_browser_reads_before_any_request(self, assets):
        page = Mock(url='https://tapas.io/')
        transport = BrowserReadTransport(page, Mock(), deadline=0.0)
        for url in ('https://tapas.io/series/42', 'https://us-a.tapas.io/pc/a.jpg'):
            with self.assertRaises(RunLimit):
                transport.get(url)
        page.evaluate.assert_not_called()
        assets.return_value.get.assert_not_called()

    @patch('comic_archive.adapters.tapas.Transport')
    def test_browser_failure_after_the_budget_ran_out_is_a_time_limit_not_a_network_outage(self, assets):
        page = Mock(url='https://tapas.io/')
        transport = BrowserReadTransport(page, Mock(), deadline=time.monotonic() + 3600)

        def fail(*args, **kwargs):
            transport.deadline = 0.0
            raise TimeoutError('PRIVATE_URL_AND_COOKIE')
        page.evaluate.side_effect = fail
        with self.assertRaises(RunLimit) as caught:
            transport.get('https://tapas.io/series/42')
        self.assertNotIsInstance(caught.exception, NetworkUnavailable)
        self.assertNotIn('PRIVATE', str(caught.exception))

    @patch('comic_archive.adapters.tapas.Transport')
    def test_series_page_reads_use_the_normal_site_first_and_refuse_redirects_and_errors(self, assets):
        page = Mock(url='about:blank')
        landing = {'url': None}
        response = Mock(status=200)
        response.body.return_value = b'<html>series</html>'

        def goto(url, **kwargs):
            page.url = landing['url'] or url
            return response
        page.goto.side_effect = goto
        transport = BrowserReadTransport(page, Mock())
        info = 'https://tapas.io/series/my-comic/info'
        self.assertEqual(transport.get(info), b'<html>series</html>')
        self.assertEqual([call.args[0] for call in page.goto.call_args_list], ['https://tapas.io/', info])
        page.evaluate.assert_not_called()
        landing['url'] = 'https://tapas.io/account/signin'
        with self.assertRaisesRegex(ArchiveError, 'refused or redirected'):
            transport.get(info)
        landing['url'] = None
        response.status = 403
        with self.assertRaisesRegex(ArchiveError, 'refused or redirected'):
            transport.get(info)
        response.status = 200
        page.url = info
        page.goto.side_effect = None
        page.goto.return_value = None
        with self.assertRaisesRegex(ArchiveError, 'refused or redirected'):
            transport.get(info)

    @patch('comic_archive.adapters.tapas.Transport')
    def test_series_page_reads_on_the_built_in_engine_use_the_page_request_and_never_visit_the_page(self, assets):
        page = Mock(url='https://tapas.io/', navigation_bodies=False)
        page.evaluate.return_value = {'status': 200, 'text': '<html>series</html>', 'source_response_kind': 'html', 'retry_after': None}
        transport = BrowserReadTransport(page, Mock())
        info = 'https://tapas.io/series/my-comic/info'
        self.assertEqual(transport.get(info), b'<html>series</html>')
        page.goto.assert_not_called()
        script, argument = page.evaluate.call_args.args
        # A redirect comes back as status 0 instead of being followed to another page.
        self.assertIn("redirect:'manual'", script)
        self.assertNotIn("redirect:'error'", script)
        self.assertEqual((argument['url'], argument['accept'], argument['limit']), (info, 'text/html', 8388608))
        page.evaluate.return_value = dict(page.evaluate.return_value, status=0, text=None)
        with self.assertRaisesRegex(ArchiveError, 'HTTP 0') as raised:
            transport.get(info)
        self.assertNotIsInstance(raised.exception, AuthenticationError)
        page.evaluate.return_value = dict(page.evaluate.return_value, status=401)
        with self.assertRaises(AuthenticationError):
            transport.get(info)

    @patch('comic_archive.adapters.tapas.Transport')
    def test_a_page_that_does_not_say_otherwise_has_its_series_page_visited_as_before(self, assets):
        page = Mock(spec=['url', 'goto', 'evaluate', 'route', 'wait_for_timeout'], url='https://tapas.io/')
        info = 'https://tapas.io/series/my-comic/info'
        page.goto.side_effect = lambda url, **kwargs: setattr(page, 'url', url) or Mock(status=200, **{'body.return_value': b'<html></html>'})
        self.assertEqual(BrowserReadTransport(page, Mock()).get(info), b'<html></html>')
        self.assertEqual([call.args[0] for call in page.goto.call_args_list], [info])
        page.evaluate.assert_not_called()

    @patch('comic_archive.adapters.tapas.Transport')
    def test_failed_guard_installation_is_classified_safely_even_when_cleanup_fails_too(self, assets):
        for message, code in (('Target page has been closed PRIVATE', 'browser_closed'),
                              ('Event loop is closed PRIVATE', 'browser_loop'),
                              ('wrong thread PRIVATE', 'browser_thread'),
                              ('Request interception failed PRIVATE', 'interception'),
                              ('something PRIVATE', 'route_installation')):
            with self.subTest(code=code):
                assets.return_value.close.reset_mock()
                page = Mock()
                page.route.side_effect = RuntimeError(message)
                page.unroute.side_effect = RuntimeError('cleanup PRIVATE')
                with self.assertRaises(ArchiveError) as error:
                    BrowserReadTransport(page, Mock())
                self.assertIn(code, str(error.exception))
                self.assertNotIn('PRIVATE', str(error.exception))
                assets.return_value.close.assert_called_once()


class AdapterBase(unittest.TestCase):
    def setUp(self):
        self.adapter = TapasAdapter(Path('unused-test-session'))
        self.adapter.network_guard = Mock()
        self.adapter.page = Mock()
        self.adapter.page.url = EPISODE
        self.adapter.context = Mock()
        self.adapter.episode_access = Mock(return_value={'source_episode_id': '81', 'access_state': 'tapas_free'})
        self.adapter.page.evaluate.return_value = {'scoped': True, 'bottom': 0, 'viewportBottom': 800}
        self.adapter.page.locator.return_value.evaluate_all.return_value = lazy_rows()


class StartupAndLimitTests(AdapterBase):
    def test_remaining_timeout_enforces_cancellation_hold_and_job_budget(self):
        self.assertEqual(self.adapter.remaining_timeout(), 30000)
        self.adapter.deadline = time.monotonic() + 3600
        self.assertEqual(self.adapter.remaining_timeout(), 30000)
        self.adapter.deadline = 0.0
        with self.assertRaises(RunLimit):
            self.adapter.remaining_timeout()
        with self.assertRaises(RunLimit):
            self.adapter.wait_normal(500)
        self.adapter.page.wait_for_timeout.assert_not_called()
        self.adapter.deadline = None
        self.adapter.cancel_check = Mock(side_effect=RunLimit('cancelled'))
        with self.assertRaises(RunLimit):
            self.adapter.remaining_timeout()
        self.adapter.cancel_check = None
        self.adapter.network_guard.check.side_effect = NetworkPolicyHold('held')
        with self.assertRaises(NetworkPolicyHold):
            self.adapter.wait_normal(500)
        self.adapter.page.wait_for_timeout.assert_not_called()
        self.adapter.network_guard.check.side_effect = None
        self.adapter.wait_normal(500)
        self.adapter.page.wait_for_timeout.assert_called_once_with(500)

    @patch('comic_archive.adapters.tapas.save_session')
    def test_automatic_capture_saves_the_session_only_after_a_verified_manifest(self, save):
        manifest = Manifest(Series('42', 'S'), (Episode('81', '42', 'E', 1, (Page(1, 'https://us-a.tapas.io/pc/a.jpg'),),
                                                       'tapas_free', EPISODE),))
        api = Mock()
        api.capture.return_value = manifest
        self.adapter.automatic_adapter = Mock(return_value=api)
        with patch('builtins.print'):
            self.assertIs(self.adapter.capture_automatic(EPISODE, '42', 'S', 'E', 1), manifest)
        self.assertIs(self.adapter.manifest(), manifest)
        self.assertEqual(self.adapter.series.id, '42')
        self.assertEqual(self.adapter.capture_mode, 'authenticated_html_manifest')
        save.assert_called_once_with(self.adapter.context, self.adapter.session)
        save.reset_mock()
        api.capture.side_effect = AccessDenied('Locked')
        with patch('builtins.print'), self.assertRaises(AccessDenied):
            self.adapter.capture_automatic(EPISODE, '42', 'S', 'E', 1)
        save.assert_not_called()


    @patch('comic_archive.adapters.tapas.Transport')
    def test_threaded_image_reads_share_the_jobs_stop_and_time_limit(self, assets):
        # Threaded image reads go straight to the asset client, never through the browser transport's own checks.
        self.adapter.deadline = time.monotonic() + 3600
        self.adapter.cancel_check = Mock()
        transport = self.adapter.automatic_adapter().transport
        self.assertEqual(transport.assets.deadline, self.adapter.deadline)
        self.assertIs(transport.assets.cancel_check, self.adapter.cancel_check)


class ReaderInspectionTests(AdapterBase):
    def test_reader_scroll_without_a_known_episode_end_is_bounded_to_one_hundred_eighty_steps(self):
        self.adapter.page.evaluate.return_value = {'scoped': True, 'bottom': 10**9, 'viewportBottom': 0}
        self.adapter.scroll_episode(EPISODE)
        self.assertEqual(self.adapter.page.mouse.wheel.call_count, 180)


class ManualCaptureTests(AdapterBase):
    @patch('comic_archive.adapters.tapas.save_session')
    @patch('builtins.input', return_value='y')
    def test_capture_refuses_inexact_episode_urls_and_unclear_confirmation_before_any_access_check(self, prompt, save):
        for url in ('https://tapas.io/series/42', 'https://tapas.io/episode/81abc', 'https://elsewhere.test/episode/81', None):
            with self.assertRaises(ArchiveError):
                self.adapter.capture(url, '42', 'S', 'E', 1)
        with self.assertRaises(ArchiveError):
            self.adapter.capture(EPISODE, '42', 'S', 'E', 1, reader_confirmed='yes')
        self.adapter.episode_access.assert_not_called()
        self.adapter.page.goto.assert_not_called()
        prompt.assert_not_called()
        save.assert_not_called()

    @patch('comic_archive.adapters.tapas.save_session')
    @patch('builtins.input', return_value='y')
    def test_capture_refuses_access_that_is_not_verified_for_this_exact_episode(self, prompt, save):
        for verified in ({'source_episode_id': '82', 'access_state': 'tapas_free'},
                         {'source_episode_id': '81', 'access_state': 'locked'},
                         {'source_episode_id': '81'}):
            self.adapter.episode_access.return_value = verified
            with self.assertRaises(ArchiveError):
                self.adapter.capture(EPISODE, '42', 'S', 'E', 1, reader_confirmed=True)
        self.adapter.page.goto.assert_not_called()
        prompt.assert_not_called()
        save.assert_not_called()
        self.assertIsNone(self.adapter.transport)

    @patch('comic_archive.adapters.tapas.Transport')
    @patch('comic_archive.adapters.tapas.save_session')
    @patch('builtins.input', return_value='y')
    def test_session_lost_while_scrolling_stops_capture_without_saving_or_new_transport(self, prompt, save, transport):
        self.adapter.page.locator.return_value.evaluate_all.return_value = [
            {'tag': 'IMG', 'url': 'https://cdn.tapas.io/page-a.jpg', 'loaded': True},
            {'tag': 'IMG', 'url': 'https://cdn.tapas.io/page-b.jpg', 'loaded': True}]
        self.adapter.scroll_episode = Mock(side_effect=lambda url: setattr(self.adapter.page, 'url', 'https://tapas.io/account/signin'))
        with self.assertRaises(ArchiveError):
            self.adapter.capture(EPISODE, '42', 'S', 'E', 1)
        self.adapter.scroll_episode.assert_called_once_with(EPISODE)
        save.assert_not_called()
        transport.assert_not_called()

    @patch('comic_archive.adapters.tapas.Transport')
    @patch('comic_archive.adapters.tapas.save_session')
    @patch('builtins.input', return_value='y')
    def test_capture_uses_the_sources_provided_after_scrolling_in_page_order(self, prompt, save, transport):
        placeholders = [{'tag': 'IMG', 'url': 'data:image/gif;base64,x', 'loaded': False, 'sources': {}}] * 2
        self.adapter.page.locator.return_value.evaluate_all.side_effect = [placeholders, lazy_rows(2, base='b')]
        self.adapter.scroll_episode = Mock()
        with patch('builtins.print'):
            manifest = self.adapter.capture(EPISODE, '42', 'S', 'E', 1)
        self.adapter.scroll_episode.assert_called_once_with(EPISODE)
        self.assertEqual(self.adapter.capture_mode, 'reader_scrolled_sources')
        self.assertEqual([(p.order, p.location) for p in manifest.episodes[0].pages], [
            (1, 'https://us-a.tapas.io/pc/b/0.jpg?sig=TEST'), (2, 'https://us-a.tapas.io/pc/b/1.jpg?sig=TEST')])
        save.assert_called_once()

    @patch('comic_archive.adapters.tapas.Transport')
    @patch('comic_archive.adapters.tapas.save_session')
    @patch('builtins.input', return_value='y')
    def test_capture_closes_the_previous_transport_and_opens_a_new_one_with_the_refreshed_cookies_network_guard_and_browser_name(self, prompt, save, transport):
        previous = Mock()
        self.adapter.context.agent = CHROME
        self.adapter.transport = previous
        with patch('builtins.print'):
            manifest = self.adapter.capture(EPISODE, '42', 'S', 'E', 1)
        previous.close.assert_called_once()
        self.assertIs(self.adapter.transport, transport.return_value)
        self.assertIs(transport.call_args.args[0], self.adapter.context.cookies.return_value)
        self.assertIs(transport.call_args.kwargs['network_guard'], self.adapter.network_guard)
        self.assertEqual(transport.call_args.kwargs['agent'], CHROME)
        self.assertEqual([p.location for p in manifest.episodes[0].pages], [
            'https://us-a.tapas.io/pc/a/0.jpg?sig=TEST', 'https://us-a.tapas.io/pc/a/1.jpg?sig=TEST'])

    @patch('comic_archive.adapters.tapas.Transport')
    @patch('comic_archive.adapters.tapas.save_session')
    @patch('builtins.input', return_value='y')
    def test_refresh_returns_fresh_links_only_while_the_page_order_is_unchanged(self, prompt, save, transport):
        locator = self.adapter.page.locator.return_value
        with patch('builtins.print'):
            episode = self.adapter.capture(EPISODE, '42', 'S', 'E', 1).episodes[0]
            locator.evaluate_all.return_value = lazy_rows(2, signature='NEW')
            refreshed = self.adapter.refresh(episode)
            self.assertEqual([p.location for p in refreshed.pages], [
                'https://us-a.tapas.io/pc/a/0.jpg?sig=NEW', 'https://us-a.tapas.io/pc/a/1.jpg?sig=NEW'])
            for rows in (lazy_rows(3), lazy_rows(2, base='b')):
                locator.evaluate_all.return_value = rows
                with self.assertRaises(ArchiveError):
                    self.adapter.refresh(episode)
        self.adapter.capture_mode = 'authenticated_html_manifest'
        self.adapter._automatic = Mock()
        self.assertIs(self.adapter.refresh(episode), self.adapter._automatic.refresh.return_value)

    def test_retrieve_refuses_expired_temporary_access_before_any_request(self):
        page = Page(1, 'https://us-a.tapas.io/pc/a/1.jpg?sig=TEST')

        def manifest(expires):
            return Manifest(Series('42', 'S'), (Episode('81', '42', 'E', 1, (page,), 'tapas_unlocked', EPISODE,
                                                         access_expires_at=expires),))
        self.adapter.transport = Mock()
        self.adapter._manifest = manifest('2000-01-01T00:00:00Z')
        with self.assertRaises(AccessDenied):
            self.adapter.retrieve(page)
        self.adapter.transport.get.assert_not_called()
        self.adapter._manifest = manifest('2999-01-01T00:00:00Z')
        self.assertIs(self.adapter.retrieve(page), self.adapter.transport.get.return_value)
        self.adapter.transport.get.assert_called_once_with(page.location)
        self.adapter.retrieve(page, max_bytes=10)
        self.adapter.transport.get.assert_called_with(page.location, max_bytes=10)


class NavigationTests(AdapterBase):
    def locator_table(self, table):
        default = Mock()
        default.evaluate_all.return_value = []
        default.count.return_value = 0
        self.adapter.page.locator.side_effect = lambda selector: table.get(selector, default)

    def account_home(self, close_count=0, anchors=None):
        anchors = anchors or [{'href': 'https://tapas.io/profile/ink', 'label': 'ink'},
                   {'href': 'https://tapas.io/ink/purchase', 'label': 'buy ink'},
                   {'href': 'https://tapas.io/profile/ink?token=PRIVATE', 'label': 'sneaky'},
                   {'href': 'https://elsewhere.test/profile/history', 'label': 'history'},
                   {'href': 'https://tapas.io/series/x', 'label': 'series'}]
        self.adapter.automatic_adapter = Mock()
        self.menu_failing = Mock()
        self.menu_failing.is_visible.return_value = True
        self.menu_failing.bounding_box.return_value = {'x': 0, 'y': 20, 'width': 10, 'height': 10}
        self.menu_failing.click.side_effect = RuntimeError('PRIVATE page detail')
        menu_hidden = Mock()
        menu_hidden.is_visible.return_value = False
        self.menu_working = Mock()
        self.menu_working.is_visible.return_value = True
        self.menu_working.bounding_box.return_value = {'x': 0, 'y': 20, 'width': 10, 'height': 10}
        menu_items = Mock()
        menu_items.evaluate_all.return_value = [{'tag': 'button', 'label': 'ink control'}]
        menus = Mock()
        menus.count.return_value = 3
        menus.nth.side_effect = lambda index: [self.menu_failing, menu_hidden, self.menu_working][index]
        anchor_rows = Mock()
        anchor_rows.evaluate_all.side_effect = lambda code: [dict(row) for row in anchors]
        self.close = Mock()
        self.close.is_visible.return_value = True
        self.close_controls = close_controls = Mock()
        close_controls.count.return_value = close_count
        close_controls.nth.return_value = self.close
        self.history_control = Mock()
        self.history_control.is_visible.return_value = True
        self.history_controls = history_controls = Mock()
        history_controls.count.return_value = 1
        history_controls.nth.return_value = self.history_control
        tabs = Mock()
        tabs.filter.return_value = history_controls
        self.locator_table({'button[aria-expanded][data-headlessui-state]': menus, 'a[href]': anchor_rows,
                            'a,button,[role="menuitem"]': menu_items,
                            'a.close-btn.hide-overlay': close_controls, 'a,button,[role="tab"]': tabs})

    def test_account_navigation_follows_only_the_ink_page_and_opens_history_without_purchase_links(self):
        self.account_home(close_count=1)
        result = self.adapter.inspect_account_navigation()
        self.assertEqual([call.args[0] for call in self.adapter.page.goto.call_args_list],
                         ['https://tapas.io/', 'https://tapas.io/profile/ink'])
        self.assertEqual(result['visited_paths'], ['/profile/ink'])
        self.assertEqual(result['navigation'], [{'path': '/profile/ink', 'label': 'ink'}, {'path': '/series/x', 'label': 'series'}])
        self.assertTrue(result['history_clicked'])
        self.history_control.click.assert_called_once()
        self.assertEqual(result['navigation_issues'], ['navigation_disclosure_obstructed'])
        self.menu_failing.click.assert_called_once()
        self.menu_working.click.assert_called_once()
        self.close.click.assert_called_once()
        self.adapter.page.keyboard.press.assert_called_once_with('Escape')
        self.assertEqual(result['menu_controls'], [{'tag': 'button', 'label': 'ink control'}])
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.adapter.page.remove_listener.assert_called_once()

    def test_account_navigation_falls_back_to_the_account_page_when_no_history_or_ink_link_exists(self):
        self.account_home(anchors=[{'href': 'https://tapas.io/my-account', 'label': 'account'},
                                   {'href': 'https://tapas.io/account/logout', 'label': 'log out'}])
        result = self.adapter.inspect_account_navigation()
        self.assertEqual([call.args[0] for call in self.adapter.page.goto.call_args_list],
                         ['https://tapas.io/', 'https://tapas.io/my-account'])
        self.assertEqual(result['visited_paths'], ['/my-account'])
        self.assertEqual(result['navigation'], [{'path': '/my-account', 'label': 'account navigation'}])
        self.assertFalse(result['history_clicked'])
        self.history_control.click.assert_not_called()

    def test_account_navigation_visits_at_most_three_pages(self):
        self.account_home(anchors=[{'href': f'https://tapas.io/a/history-{name}', 'label': name}
                                   for name in ('one', 'two', 'three', 'four')])
        result = self.adapter.inspect_account_navigation()
        self.assertEqual(result['visited_paths'], ['/a/history-one', '/a/history-two', '/a/history-three'])
        self.assertEqual(self.adapter.page.goto.call_count, 4)

    def test_hidden_overlay_and_history_controls_are_skipped_not_forced(self):
        self.account_home(close_count=2)
        hidden_close, hidden_control = Mock(), Mock()
        hidden_close.is_visible.return_value = False
        hidden_control.is_visible.return_value = False
        self.close_controls.nth.side_effect = [hidden_close, self.close]
        self.history_controls.count.return_value = 2
        self.history_controls.nth.side_effect = [hidden_control, self.history_control]
        self.history_control.click.side_effect = RuntimeError('PRIVATE intercepted')
        self.history_control.bounding_box.return_value = None
        result = self.adapter.inspect_account_navigation()
        hidden_close.click.assert_not_called()
        hidden_control.click.assert_not_called()
        self.close.click.assert_called_once()
        self.assertFalse(result['history_clicked'])
        self.assertIsNone(result['history_obstruction'])
        self.assertIn('history_control_obstructed', result['navigation_issues'])
        self.adapter.page.evaluate.assert_not_called()

    def test_obstructed_history_control_is_reported_without_forcing_the_click(self):
        self.account_home(close_count=1)
        self.close.click.side_effect = RuntimeError('PRIVATE overlay')
        self.history_control.click.side_effect = RuntimeError('PRIVATE intercepted')
        self.history_control.bounding_box.return_value = {'x': 10, 'y': 20, 'width': 100, 'height': 40}
        self.adapter.page.evaluate.return_value = [{'tag': 'div', 'classes': ['modal']}]
        result = self.adapter.inspect_account_navigation()
        self.assertFalse(result['history_clicked'])
        self.assertEqual(result['navigation_issues'], ['navigation_disclosure_obstructed', 'modal_close_obstructed',
                                                       'history_control_obstructed'])
        self.menu_working.click.assert_called_once()
        self.assertEqual(result['history_obstruction'], [{'tag': 'div', 'classes': ['modal']}])
        self.assertEqual(self.adapter.page.evaluate.call_args.args[1], {'x': 60.0, 'y': 40.0})
        self.history_control.click.assert_called_once()
        self.assertNotIn('PRIVATE', json.dumps(result))


class ReadingListTests(AdapterBase):
    FIRST = 'https://tapas.io/reading-list?category=SUBSCRIBED'
    SECOND = 'https://tapas.io/reading-list?pageNumber=2&type=ALL&category=SUBSCRIBED'
    ROW = {'url': 'https://tapas.io/series/first', 'title': 'First', 'ids': ['42']}

    def reading_list(self, *, rows=None, verified=True, start=None, label='All', next_count=1, status=200, landing=None,
                     navigation_error=None):
        adapter = self.adapter
        adapter.page = Mock()
        adapter.page.url = start or self.FIRST
        adapter._inspect_library_details = Mock(return_value={'authenticated': True, 'membership_verified': verified,
                                                              'scope': 'subscribed'})
        self.automatic = Mock()
        adapter.automatic_adapter = Mock(return_value=self.automatic)
        adapter.wait_normal = Mock()
        active = Mock()
        active.count.return_value = 1
        active.inner_text.return_value = label
        self.rows = Mock()
        self.rows.evaluate_all.side_effect = rows if rows is not None else [
            [dict(self.ROW)], [{'url': 'https://tapas.io/series/second', 'title': 'Second', 'ids': ['43']}]]
        self.next = Mock()
        self.next.count.return_value = next_count
        self.next.is_visible.return_value = True
        self.next.get_attribute.side_effect = lambda name: ('paging__button--next' if name == 'class' else self.SECOND)
        self.next.click.side_effect = lambda **kwargs: setattr(adapter.page, 'url', landing or self.SECOND)

        def locator(selector):
            if 'sort-item.on' in selector:
                return active
            if 'paging__button--next' in selector:
                return self.next
            return self.rows
        adapter.page.locator.side_effect = locator
        navigation = MagicMock()
        navigation.__enter__.return_value.value = Mock(status=status) if status is not None else None
        navigation.__exit__.return_value = False
        adapter.page.expect_navigation.return_value = navigation
        if navigation_error is not None:
            self.next.click.side_effect = navigation_error

        def restore(url, **kwargs):
            adapter.page.url = url
            return Mock(status=200)
        adapter.page.goto.side_effect = restore

    def test_reading_list_pager_keeps_only_tapas_series_links_and_stops_at_its_page_budget(self):
        self.reading_list(rows=[[dict(self.ROW),
                                 {'url': 'https://elsewhere.test/series/x', 'title': 'Foreign', 'ids': ['1']},
                                 {'url': 'https://user:pw@tapas.io/series/y', 'title': 'Credentials', 'ids': ['2']},
                                 {'url': 'http://tapas.io/series/z', 'title': 'Plain', 'ids': ['3']},
                                 {'url': 'https://tapas.io/ink/purchase', 'title': 'Buy', 'ids': ['4']},
                                 {'url': 'https://tapas.io/series/two-ids', 'title': 'Ambiguous', 'ids': ['5', '6']}]])
        result = self.adapter.inspect_library_pages('subscribed', max_pages=1)
        self.assertEqual(result['series'], [
            {'source_series_id': '42', 'title': 'First', 'source_url': 'https://tapas.io/series/first'},
            {'source_series_id': None, 'title': 'Ambiguous', 'source_url': 'https://tapas.io/series/two-ids'}])
        self.assertEqual(result['unresolved_series_id_count'], 1)
        self.assertFalse(result['complete'])
        self.assertEqual(len(result['pages']), 1)
        self.next.click.assert_not_called()

    def test_reading_list_pager_validates_its_page_limit_before_any_inspection(self):
        self.adapter._inspect_library_details = Mock()
        for limit in (0, 201, True, '3', None):
            with self.assertRaises(ArchiveError):
                self.adapter.inspect_library_pages(max_pages=limit)
        self.adapter._inspect_library_details.assert_not_called()

    def test_reading_list_pager_never_clicks_from_an_unverified_scope_page_or_type_selection(self):
        for options in ({'verified': False}, {'start': self.SECOND}, {'label': 'Comics'},
                        {'start': 'https://tapas.io/reading-list?category=RECENT'}):
            with self.subTest(options=options):
                self.reading_list(**options)
                result = self.adapter.inspect_library_pages('subscribed')
                self.assertFalse(result['complete'])
                self.assertEqual(result['series'], [])
                self.next.click.assert_not_called()
                self.adapter.page.expect_navigation.assert_not_called()

    def test_reading_list_pager_keeps_page_one_when_the_next_page_is_refused_or_changes_scope(self):
        for options in ({'status': 403}, {'status': None},
                        {'landing': 'https://tapas.io/reading-list?pageNumber=2&type=ALL&category=RECENT'}):
            with self.subTest(options=options):
                self.reading_list(**options)
                result = self.adapter.inspect_library_pages('subscribed', max_pages=2)
                self.assertFalse(result['complete'])
                self.assertFalse(result['boundary_observed'])
                self.assertEqual([s['source_series_id'] for s in result['series']], ['42'])
                self.assertEqual(len(result['pages']), 1)
                self.assertTrue(result['restore_succeeded'])
                self.assertEqual(self.adapter.page.url, self.FIRST)
                self.automatic.check_session.assert_not_called()

    def test_reading_list_pager_failures_keep_discoveries_and_never_hide_a_changed_account(self):
        self.reading_list(navigation_error=TimeoutError('PRIVATE browser detail'))
        result = self.adapter.inspect_library_pages('subscribed', max_pages=2)
        self.assertEqual(result['navigation_failure']['phase'], 'click')
        self.assertEqual(result['navigation_failure']['exception_class'], 'TimeoutError')
        self.assertFalse(result['complete'])
        self.assertEqual([s['source_series_id'] for s in result['series']], ['42'])
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.adapter.page.goto.assert_not_called()
        self.reading_list()
        self.automatic.check_session.side_effect = ArchiveError('The saved login belongs to another account.')
        self.adapter.page.goto.side_effect = RuntimeError('restore PRIVATE')
        with self.assertRaises(ArchiveError) as caught:
            self.adapter.inspect_library_pages('subscribed', max_pages=2)
        self.assertIn('another account', str(caught.exception))
        self.adapter.page.goto.assert_called_once()
        self.adapter.page.remove_listener.assert_called_once()
        self.reading_list()
        self.automatic.check_session.side_effect = AuthenticationError('Login expired.')
        with self.assertRaises(AuthenticationError):
            self.adapter.inspect_library_pages('subscribed', max_pages=2)

    def test_reading_list_pager_reports_a_restore_that_failed_after_a_complete_walk(self):
        self.reading_list()
        self.next.get_attribute.side_effect = lambda name: (
            'paging__button--next' + (' disabled' if self.adapter.page.url == self.SECOND else '') if name == 'class' else self.SECOND)
        self.adapter.page.goto.side_effect = RuntimeError('PRIVATE')
        result = self.adapter.inspect_library_pages('subscribed', max_pages=2)
        self.assertTrue(result['complete'])
        self.assertIs(result['restore_succeeded'], False)
        self.assertEqual([s['source_series_id'] for s in result['series']], ['42', '43'])

    def test_reading_list_boundary_is_not_complete_when_a_series_repeats_or_has_no_single_id(self):
        first, second = dict(self.ROW), {'url': 'https://tapas.io/series/second', 'title': 'Second', 'ids': ['43']}
        third = {'url': 'https://tapas.io/series/third', 'title': 'Third', 'ids': ['44']}
        renamed = dict(first, url='https://tapas.io/series/renamed')
        for rows, duplicates, unresolved in (([[first, second], [second, third]], 1, 0), ([[first], [renamed]], 1, 0),
                                             ([[first, second], [dict(third, ids=[])]], 0, 1)):
            with self.subTest(rows=rows):
                self.reading_list(rows=rows)
                self.next.get_attribute.side_effect = lambda name: (
                    'paging__button--next' + (' disabled' if self.adapter.page.url == self.SECOND else '') if name == 'class' else self.SECOND)
                result = self.adapter.inspect_library_pages('subscribed', max_pages=2)
                self.assertTrue(result['boundary_observed'])
                self.assertFalse(result['complete'])
                self.assertEqual(len(result['pages']), 2)
                self.assertEqual(result['duplicate_series_on_later_pages'], duplicates)
                self.assertEqual(result['unresolved_series_id_count'], unresolved)

    def test_reading_list_pager_stops_on_an_empty_list_or_missing_next_control_and_checkpoints_each_page(self):
        self.reading_list(rows=[[]])
        checkpoint = Mock()
        result = self.adapter.inspect_library_pages('subscribed', on_page=checkpoint)
        self.assertFalse(result['complete'])
        self.assertEqual(result['pages'], [])
        checkpoint.assert_not_called()
        self.reading_list(next_count=0)
        checkpoint = Mock()
        result = self.adapter.inspect_library_pages('subscribed', on_page=checkpoint)
        self.assertFalse(result['complete'])
        self.assertFalse(result['boundary_observed'])
        self.assertEqual(len(result['pages']), 1)
        checkpoint.assert_called_once()
        self.assertEqual([s['source_series_id'] for s in checkpoint.call_args.args[0]['series']], ['42'])
        self.next.click.assert_not_called()


class LibraryDetailTests(AdapterBase):
    def library(self, tab_href_recent='https://tapas.io/reading-list?category=RECENT&token=PRIVATE', links=None, tabs=True,
                script=b'var library = 1;'):
        adapter = self.adapter
        adapter.page.url = 'https://tapas.io/'
        adapter.automatic_adapter = Mock()
        adapter.wait_normal = Mock()
        adapter.transport = Mock()
        adapter.transport.assets.get.return_value = script
        response = Mock()
        response.text.return_value = ('<script src="https://us-a.tapas.io/resources/js/tapas.min.js"></script>'
                                      '<script src="https://evil.test/x.js"></script>')
        adapter.page.goto.return_value = response
        adapter.page.evaluate.return_value = {}
        tables = {
            'a[href]': lambda: links if links is not None else [
                {'url': 'https://tapas.io/reading-list?token=PRIVATE', 'title': 'Reading list'},
                {'url': 'https://elsewhere.test/library', 'title': 'Foreign'}],
            'a,button,[role="tab"]': lambda: [] if not tabs else [
                {'tag': 'a', 'role': None, 'title': 'Subscribed', 'href': 'https://tapas.io/reading-list?category=SUBSCRIBED'},
                {'tag': 'a', 'role': None, 'title': 'Recent', 'href': tab_href_recent}],
            'section.paging-wrap a.paging__button': lambda: [
                {'href': 'https://tapas.io/reading-list?pageNumber=2&category=SUBSCRIBED', 'classes': ['paging__button'],
                 'label': '2', 'display': 'block', 'visibility': 'visible'},
                {'href': 'https://tapas.io/reading-list?pageNumber=3&token=PRIVATE', 'classes': ['paging__button'],
                 'label': 'Next', 'display': 'block', 'visibility': 'visible'}],
            'a[href*="/series/"]': lambda: [
                {'url': 'https://tapas.io/series/ok-comic?sig=PRIVATE', 'title': 'Ok', 'ancestors': []},
                {'url': 'https://elsewhere.test/series/other', 'title': 'Other', 'ancestors': []}]}

        def locator(selector):
            node = Mock()
            node.count.return_value = 0
            if selector in tables:
                node.evaluate_all.side_effect = lambda *args: tables[selector]()
            else:
                node.evaluate_all.return_value = []
            return node
        adapter.page.locator.side_effect = locator

    def test_library_scope_is_verified_only_for_the_list_actually_opened_and_urls_are_sanitized(self):
        for category, verified, scope in (('subscribed', True, 'subscribed'), ('recent', False, 'unknown')):
            with self.subTest(category=category):
                self.library()
                result = self.adapter._inspect_library_details(category)
                self.assertIs(result['membership_verified'], verified)
                self.assertEqual(result['scope'], scope)
                self.assertFalse(result['complete'])
                self.assertEqual([c['url'] for c in result['visible_series_candidates']], ['https://tapas.io/series/ok-comic'])
                self.assertEqual([t['href'] for t in result['visible_library_tabs']], [
                    'https://tapas.io/reading-list?category=SUBSCRIBED', 'https://tapas.io/reading-list?category=RECENT'])
                self.assertEqual([link['url'] for link in result['observed_pager_links']],
                                 ['https://tapas.io/reading-list?pageNumber=2&category=SUBSCRIBED', None])
                self.assertEqual(result['published_script_routes'], [])
                self.assertEqual(result['document_script_assets'], [
                    {'host': 'us-a.tapas.io', 'path': '/resources/js/tapas.min.js', 'has_query': False}])
                self.assertNotIn('PRIVATE', json.dumps(result))
        with self.assertRaises(ArchiveError):
            self.adapter._inspect_library_details('purchased')

    def test_library_without_a_provided_link_or_tab_is_never_guessed_or_verified(self):
        self.library(links=[{'url': 'https://tapas.io/series/x', 'title': 'x'}], tabs=False)
        result = self.adapter._inspect_library_details('subscribed')
        self.assertIsNone(result['visited_library_url'])
        self.assertFalse(result['membership_verified'])
        self.assertEqual(result['scope'], 'unknown')
        self.assertEqual(result['library_links'], [])
        self.assertEqual([call.args[0] for call in self.adapter.page.goto.call_args_list], ['https://tapas.io/'])
        self.library(links=[{'url': 'https://tapas.io/reading-list?a=1', 'title': 'List'},
                            {'url': 'https://tapas.io/reading-list?a=2', 'title': 'List'}], tabs=False)
        result = self.adapter._inspect_library_details('updated')
        self.assertEqual(result['library_links'], [{'url': 'https://tapas.io/reading-list', 'title': 'List'}])
        self.assertTrue(result['membership_verified'])
        self.assertEqual(result['scope'], 'updated')

    def test_published_script_routes_are_read_only_from_scripts_within_the_size_limit(self):
        self.library(script=b'x = "/api/series";')
        self.assertEqual(self.adapter._inspect_library_details('subscribed')['published_script_routes'], ['/api/series'])
        self.library(script=b'x = "/api/series";' + b' ' * (4 * 1024 * 1024))
        self.assertEqual(self.adapter._inspect_library_details('subscribed')['published_script_routes'], [])

    def test_page_only_library_inspection_verifies_scope_from_the_final_address(self):
        for url, verified, scope in (('https://tapas.io/reading-list?category=SUBSCRIBED', True, 'subscribed'),
                                     ('https://tapas.io/reading-list?category=RECENT', False, 'unknown'),
                                     ('https://tapas.io/reading-list?pageNumber=2&category=SUBSCRIBED', False, 'unknown'),
                                     ('https://tapas.io/account/signin', False, 'unknown')):
            with self.subTest(url=url):
                self.library()
                self.adapter.page.url = url
                result = self.adapter._inspect_library_details('subscribed', research=False)
                self.assertIs(result['membership_verified'], verified)
                self.assertEqual(result['scope'], scope)
                self.assertEqual(set(result), {'authenticated', 'membership_verified', 'scope',
                                               'initial_list_count', 'final_list_count'})


class HistoryAnswerTests(AdapterBase):
    def test_history_answer_without_data_or_pagination_stalls_the_cursor_instead_of_crashing(self):
        for answer in (None, [], {'data': None}, {'data': []}, {'data': {'pagination': None}}):
            with self.subTest(answer=answer):
                observation = SimpleNamespace(library_values={'/profile/transactions': answer}, history_status=200, responses=[])
                state = {'advance_clicked': True, 'observation': observation, 'previous_index': 0, 'needs_advance': True}
                self.adapter.wait_normal = Mock()
                with patch('comic_archive.adapters.tapas.time', Mock(monotonic=Mock(side_effect=[0, 100]))):
                    self.assertFalse(self.adapter._advance_history(state))
                self.assertEqual(state['navigation_error'], 'history_cursor_stalled')

    def test_history_page_with_null_rows_or_pagination_is_recorded_without_crashing(self):
        adapter = self.adapter
        adapter.account_identity = Mock(return_value='1')
        adapter.wait_normal = Mock()
        adapter.remaining_timeout = Mock(return_value=30000)
        page = Mock()
        page.is_closed.return_value = False
        adapter.context.new_page.return_value = page
        control = Mock()
        control.count.return_value = 1
        control.get_attribute.return_value = 'disabled'
        control.evaluate_all.return_value = []
        page.locator.return_value = control

        def opened():
            adapter._history_scan['observation'].library_values['/profile/transactions'] = {
                'data': {'transactions': None, 'pagination': None}}
            return {'history_clicked': True}
        adapter.inspect_account_navigation = Mock(side_effect=opened)
        result = adapter.inspect_transaction_history(1)
        self.assertTrue(result['observed_boundary'])
        self.assertEqual((result['pages'][0]['source_types'], result['pages'][0]['server_pagination']), ([], {}))


class FreeFilterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = object.__new__(TapasAdapter)
        self.adapter.page = Mock()
        self.adapter.remaining_timeout = Mock(return_value=30000)
        self.adapter.wait_normal = Mock()
        self.automatic = Mock()
        self.adapter.automatic_adapter = Mock(return_value=self.automatic)
        self.start = 'https://tapas.io/reading-list?category=SUBSCRIBED'
        self.first = 'https://tapas.io/reading-list?category=SUBSCRIBED&type=FREE'
        self.second = 'https://tapas.io/reading-list?pageNumber=2&type=FREE&category=SUBSCRIBED'
        self.adapter.page.url = self.start
        self.control = Mock()
        self.control.count.return_value = 1
        self.control.is_visible.return_value = True
        self.control.inner_text.return_value = 'Free Episodes'
        self.control.get_attribute.return_value = self.first
        parent = Mock()
        parent.get_attribute.side_effect = lambda name: 'FREE' if name == 'data-type' else 'filter-item on'
        self.control.locator.return_value = parent
        self.base = Mock()
        self.base.evaluate_all.return_value = [0]
        self.base.nth.return_value = self.control
        self.pager = Mock()
        self.pager.count.return_value = 1
        self.pager.is_visible.return_value = True
        self.pager.get_attribute.side_effect = lambda name: (
            self.second if name == 'href' else
            'paging__button--next' + (' disabled' if self.adapter.page.url == self.second else '') if name == 'class' else None)
        self.rows = Mock()
        self.rows.evaluate_all.side_effect = [
            [{'url': 'https://tapas.io/series/one', 'title': 'One', 'ids': ['42']}],
            [{'url': 'https://tapas.io/series/two', 'title': 'Two', 'ids': ['43']}]]

        def locator(selector):
            if 'section-filter' in selector:
                return self.base
            if 'paging__button--next' in selector:
                return self.pager
            return self.rows
        self.adapter.page.locator.side_effect = locator
        self.control.click.side_effect = lambda **_: setattr(self.adapter.page, 'url', self.first)
        self.pager.click.side_effect = lambda **_: setattr(self.adapter.page, 'url', self.second)
        self.event = MagicMock()
        self.event.__enter__.return_value.value.status = 200
        self.event.__exit__.return_value = False
        self.adapter.page.expect_navigation.return_value = self.event

        def restore(url, **kwargs):
            self.adapter.page.url = url
            return Mock(status=200)
        self.adapter.page.goto.side_effect = restore
        self.before = {'authenticated': True, 'membership_verified': True, 'scope': 'subscribed'}

    def inspect(self):
        return self.adapter._inspect_free_episode_filter(self.before)

    def test_filter_code_falls_back_to_the_supplied_link_type_when_the_item_has_no_usable_data_type(self):
        for code in (None, 'not a code!', ''):
            with self.subTest(code=code):
                self.setUp()
                self.control.locator.return_value.get_attribute.side_effect = lambda name: (
                    code if name == 'data-type' else 'filter-item on')
                result = self.inspect()
                self.assertTrue(result['complete'])
                self.assertEqual(result['public_type_code'], 'FREE')
                self.assertEqual([s['source_series_id'] for s in result['series']], ['42', '43'])

    def test_refused_filter_navigation_keeps_nothing_and_restores_the_original_list(self):
        self.event.__enter__.return_value.value.status = 403
        result = self.inspect()
        self.assertEqual(result['navigation_failure'], {'phase': 'filter_navigation', 'exception_class': 'ArchiveError'})
        self.assertFalse(result['complete'])
        self.assertFalse(result['membership_verified'])
        self.assertEqual(result['series'], [])
        self.pager.click.assert_not_called()
        self.assertTrue(result['restore_succeeded'])
        self.assertEqual(self.adapter.page.url, self.start)

    def test_filtered_pager_stops_with_page_one_when_rows_repeat_or_next_is_missing_or_changes_scope(self):
        def repeat():
            self.rows.evaluate_all.side_effect = [[{'url': 'https://tapas.io/series/one', 'title': 'One', 'ids': ['42']}]] * 2

        def missing():
            self.pager.count.return_value = 0

        def moved():
            self.pager.click.side_effect = lambda **_: setattr(
                self.adapter.page, 'url', 'https://tapas.io/reading-list?pageNumber=2&type=ALL&category=SUBSCRIBED')
        for name, arrange, clicks in (('repeat', repeat, 1), ('missing', missing, 0), ('moved', moved, 1)):
            with self.subTest(name):
                self.setUp()
                arrange()
                result = self.inspect()
                self.assertFalse(result['complete'])
                self.assertFalse(result['pagination_complete'])
                self.assertEqual([s['source_series_id'] for s in result['series']], ['42'])
                self.assertEqual(self.pager.click.call_count, clicks)
                self.assertTrue(result['restore_succeeded'])

    def test_filtered_rows_keep_only_tapas_series_links(self):
        self.rows.evaluate_all.side_effect = [
            [{'url': 'https://tapas.io/series/one', 'title': 'One', 'ids': ['42']},
             {'url': 'https://elsewhere.test/series/x', 'title': 'Foreign', 'ids': ['1']},
             {'url': 'https://user:pw@tapas.io/series/y', 'title': 'Credentials', 'ids': ['2']},
             {'url': 'https://tapas.io/ink/purchase', 'title': 'Buy', 'ids': ['3']}],
            [{'url': 'https://tapas.io/series/two', 'title': 'Two', 'ids': ['43']}]]
        result = self.inspect()
        self.assertEqual([s['source_url'] for s in result['series']], ['https://tapas.io/series/one', 'https://tapas.io/series/two'])

    def test_empty_filtered_view_is_not_reported_complete(self):
        self.rows.evaluate_all.side_effect = [[]]
        result = self.inspect()
        self.assertFalse(result['complete'])
        self.assertEqual(result['series'], [])
        self.assertEqual(result['pages'], [])
        self.pager.click.assert_not_called()

    def test_failed_restore_is_reported_even_though_the_walk_completed(self):
        self.adapter.page.goto.side_effect = RuntimeError('PRIVATE')
        result = self.inspect()
        self.assertTrue(result['complete'])
        self.assertIs(result['restore_succeeded'], False)
        self.assertNotIn('PRIVATE', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
