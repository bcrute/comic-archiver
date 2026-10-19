"""Synthetic browser observations. These do not validate live Tapas markup."""
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from comic_archive.adapters.tapas import BrowserReadTransport, TapasAdapter
from comic_archive.models import ArchiveError
from comic_archive.errors import AccessDenied, AuthenticationError, MetadataError


class TapasTests(unittest.TestCase):
    @patch('comic_archive.adapters.tapas.time.sleep')
    @patch('comic_archive.adapters.tapas.Transport')
    def test_only_exact_series_metadata_not_found_is_typed_without_private_response(self,assets,sleep):
        page=Mock();page.url='https://tapas.io/'
        transport=BrowserReadTransport(page,Mock())
        for status in (404,410):
            page.evaluate.return_value={'status':status,'text':'PRIVATE_SESSION_RESPONSE'}
            with self.assertRaises(MetadataError) as caught:transport.get('https://tapas.io/series/42')
            self.assertEqual(caught.exception.source_series_id,'42')
            self.assertEqual(caught.exception.source_route,'/series/42')
            self.assertEqual(caught.exception.source_http_status,status)
            self.assertNotIn('PRIVATE',str(caught.exception))
        page.evaluate.return_value={'status':404,'text':'PRIVATE'}
        with self.assertRaises(ArchiveError) as caught:transport.get('https://tapas.io/series/42/episodes')
        self.assertNotIsInstance(caught.exception,MetadataError)
        page.evaluate.return_value={'status':403,'text':'PRIVATE'}
        with self.assertRaises(ArchiveError) as caught:transport.get('https://tapas.io/series/42')
        self.assertNotIsInstance(caught.exception,MetadataError)
    @patch('comic_archive.adapters.tapas.Transport')
    def test_gateway_errors_pause_once_and_keep_authentication_and_refusals_distinct(self,assets):
        from comic_archive.errors import NetworkUnavailable
        page=Mock();page.url='https://tapas.io/'
        transport=BrowserReadTransport(page,Mock())
        for status in (502,503,504):
            with self.subTest(status=status):
                page.evaluate.reset_mock()
                page.evaluate.return_value={'status':status,'text':'PRIVATE_RESPONSE_AND_COOKIE'}
                with self.assertRaises(NetworkUnavailable) as caught:
                    transport.get('https://tapas.io/series/42')
                self.assertEqual(caught.exception.source_http_status,status)
                self.assertNotIn('PRIVATE',str(caught.exception))
                page.evaluate.assert_called_once();page.goto.assert_not_called()
        for status,error_type in ((401,AuthenticationError),(403,ArchiveError),(429,ArchiveError)):
            with self.subTest(status=status):
                page.evaluate.reset_mock()
                page.evaluate.return_value={'status':status,'text':'PRIVATE_RESPONSE_AND_COOKIE'}
                with self.assertRaises(error_type) as caught:
                    transport.get('https://tapas.io/series/42')
                self.assertNotIsInstance(caught.exception,NetworkUnavailable)
                self.assertNotIn('PRIVATE',str(caught.exception));page.evaluate.assert_called_once()

    @patch('comic_archive.adapters.tapas.Transport')
    def test_browser_failure_classification_suppresses_private_details(self,assets):
        from comic_archive.errors import NetworkUnavailable
        transport=BrowserReadTransport(Mock(),Mock())
        for error,code in ((TimeoutError('PRIVATE_URL_AND_COOKIE'),'timeout'),
                           (RuntimeError('PRIVATE_URL_AND_COOKIE'),'browser_error')):
            transport._get=Mock(side_effect=error)
            with self.assertRaises(NetworkUnavailable) as caught:transport.get('https://tapas.io/episode/81')
            self.assertEqual(caught.exception.browser_failure_code,code)
            self.assertNotIn('PRIVATE',str(caught.exception))

    @patch('comic_archive.adapters.tapas.Transport')
    def test_the_log_names_a_browser_failure_and_only_the_built_in_engines_own_message(self,assets):
        import contextlib, io
        from comic_archive import qt_engine
        from comic_archive.errors import NetworkUnavailable
        transport=BrowserReadTransport(Mock(),Mock())
        for error,line in ((qt_engine.Error('The page changed before its script finished.'),
                            'Browser request failed: Error: The page changed before its script finished.\n'),
                           (RuntimeError('PRIVATE_URL_AND_COOKIE'),'Browser request failed: RuntimeError\n')):
            transport._get=Mock(side_effect=error);written=io.StringIO()
            with contextlib.redirect_stdout(written),self.assertRaises(NetworkUnavailable):
                transport.get('https://tapas.io/episode/81')
            self.assertEqual(written.getvalue(),line)

    def failing_page(self,*outcomes):
        """A page on the site whose reads end as given, one after another."""
        page=Mock(url='https://tapas.io/');page.evaluate.side_effect=list(outcomes)
        return page

    @staticmethod
    def unanswered(**seen):
        """What the page reports of a read that got no usable answer."""
        return {'failed':{'stage':'request','name':'TypeError','ms':31,'protocol':'none','site':'h3'}|seen}

    ANSWER={'status':200,'text':'{"ok":true}','source_response_kind':'json','retry_after':None}

    @patch('comic_archive.adapters.tapas.Transport')
    def test_a_read_without_an_answer_is_tried_again_and_the_log_says_how_far_it_got(self,assets):
        import contextlib, io
        page=self.failing_page(self.unanswered(),self.ANSWER)
        transport=BrowserReadTransport(page,Mock());written=io.StringIO()
        with contextlib.redirect_stdout(written):
            self.assertEqual(transport.get('https://tapas.io/episode/81/info'),b'{"ok":true}')
        self.assertEqual(written.getvalue(),'Browser read failed: ReadFailed: no answer after 31 ms '
                         '(TypeError; read over none; site over h3); trying again in 2 s.\n')
        first,second=[call.args for call in page.evaluate.call_args_list]
        # The further try asks for the same thing after a pause, and does not follow a redirect.
        self.assertEqual(first[1],second[1])
        self.assertIn("redirect:'error'",first[0]);self.assertEqual(second[0],first[0].replace("redirect:'error'","redirect:'manual'"))
        self.assertEqual([call.args for call in page.wait_for_timeout.call_args_list],[(2000,)])
        page.goto.assert_not_called()

    @patch('comic_archive.adapters.tapas.Transport')
    def test_each_further_try_waits_longer_and_after_the_last_the_site_counts_as_unreachable(self,assets):
        from comic_archive.errors import NetworkUnavailable
        last=self.unanswered(stage='answer',ms=1200,protocol='h2',site='h2')
        page=self.failing_page(RuntimeError('PRIVATE'),RuntimeError('PRIVATE'),RuntimeError('PRIVATE'),last,AssertionError('a fifth read'))
        with patch('builtins.print') as said,self.assertRaises(NetworkUnavailable) as caught:
            BrowserReadTransport(page,Mock()).get('https://tapas.io/episode/81')
        self.assertEqual((caught.exception.browser_failure_code,page.evaluate.call_count),('browser_error',4))
        self.assertNotIn('PRIVATE',str(caught.exception))
        self.assertEqual([call.args for call in page.wait_for_timeout.call_args_list],[(2000,),(6000,),(20000,)])
        # The log ends with what the last try saw, not the first.
        self.assertEqual([call.args[0] for call in said.call_args_list],
                         [f'Browser read failed: RuntimeError; trying again in {pause} s.' for pause in (2,6,20)]
                         +['Browser request failed: ReadFailed: the answer broke off after 1200 ms (TypeError; read over h2; site over h2)'])

    def test_what_a_page_reports_of_a_failed_read_reaches_the_log_as_fixed_words_only(self):
        from comic_archive.adapters.tapas import ReadFailed
        nothing='failed after unknown ms (unknown; read over unknown; site over unknown)'
        for seen in ({'stage':'https://tapas.io/PRIVATE','name':'PRIVATE COOKIE','ms':'PRIVATE','protocol':'PRIVATE/1','site':None},
                     {'ms':True,'name':7,'protocol':'h2 PRIVATE','site':'x'*21},{'ms':-1},{'ms':3600001},'PRIVATE',None):
            self.assertEqual(str(ReadFailed(seen)),nothing)
        self.assertEqual(str(ReadFailed({'stage':'request','name':'TypeError','ms':0,'protocol':'http/1.1','site':'h3'})),
                         'no answer after 0 ms (TypeError; read over http/1.1; site over h3)')

    @patch('comic_archive.adapters.tapas.Transport')
    def test_a_redirect_found_by_a_further_try_is_named_in_the_log_and_pauses_the_job_as_before(self,assets):
        from comic_archive.errors import NetworkUnavailable
        redirect={'status':0,'text':'','source_response_kind':'other','retry_after':None}
        page=self.failing_page(RuntimeError('PRIVATE'),redirect,AssertionError('a third read'))
        with patch('builtins.print') as said,self.assertRaises(NetworkUnavailable) as caught:
            BrowserReadTransport(page,Mock()).get('https://tapas.io/episode/81')
        self.assertEqual((caught.exception.browser_failure_code,page.evaluate.call_count),('browser_error',2))
        self.assertEqual([call.args[0] for call in said.call_args_list],
                         ['Browser read failed: RuntimeError; trying again in 2 s.',
                          'Browser read was answered with a redirect.','Browser request failed: RuntimeError'])
        # The series details read never followed redirects: there status 0 is still the refusal it was.
        page=self.failing_page(RuntimeError('PRIVATE'),redirect);page.navigation_bodies=False
        with patch('builtins.print') as said,self.assertRaisesRegex(ArchiveError,'returned HTTP 0') as caught:
            BrowserReadTransport(page,Mock()).get('https://tapas.io/series/my-comic/info')
        self.assertNotIsInstance(caught.exception,NetworkUnavailable)
        self.assertEqual(len(said.call_args_list),1)

    @patch('comic_archive.adapters.tapas.Transport')
    def test_a_read_that_waited_its_full_time_is_not_tried_again(self,assets):
        from comic_archive.errors import NetworkUnavailable
        # The engine gave up waiting, or the read's own time limit ended it inside the page.
        for outcome in (TimeoutError('PRIVATE'),self.unanswered(name='AbortError',ms=30000)):
            page=self.failing_page(outcome,AssertionError('a second read'))
            with patch('builtins.print'),self.assertRaises(NetworkUnavailable) as caught:
                BrowserReadTransport(page,Mock()).get('https://tapas.io/episode/81')
            self.assertEqual((caught.exception.browser_failure_code,page.evaluate.call_count),('timeout',1))
            page.wait_for_timeout.assert_not_called()

    @patch('comic_archive.adapters.tapas.Transport')
    def test_further_tries_stay_inside_the_jobs_time_and_end_when_the_person_pauses(self,assets):
        import time
        from comic_archive.cancellation import RunCancelled
        from comic_archive.errors import NetworkUnavailable
        # No time for even the first pause.
        page=self.failing_page(RuntimeError('PRIVATE'),AssertionError('a second read'))
        with patch('builtins.print'),self.assertRaises(NetworkUnavailable):
            BrowserReadTransport(page,Mock(),deadline=time.monotonic()+1).get('https://tapas.io/episode/81')
        self.assertEqual(page.evaluate.call_count,1);page.wait_for_timeout.assert_not_called()
        # Time for the first pause and not the second; the try in between is given only the time that is left.
        page=self.failing_page(RuntimeError('PRIVATE'),RuntimeError('PRIVATE'),AssertionError('a third read'))
        with patch('builtins.print'),self.assertRaises(NetworkUnavailable):
            BrowserReadTransport(page,Mock(),deadline=time.monotonic()+5).get('https://tapas.io/episode/81')
        self.assertEqual([call.args for call in page.wait_for_timeout.call_args_list],[(2000,)])
        first,second=[call.args[1]['timeout'] for call in page.evaluate.call_args_list]
        self.assertTrue(4000<second<first<=5000,(first,second))
        # Pause arrives while a try waits.
        page=self.failing_page(RuntimeError('PRIVATE'),AssertionError('a second read'))
        transport=BrowserReadTransport(page,Mock())
        page.wait_for_timeout.side_effect=lambda ms:setattr(transport,'cancel_check',Mock(side_effect=RunCancelled('stopped')))
        with patch('builtins.print'),self.assertRaises(RunCancelled):
            transport.get('https://tapas.io/episode/81')
        self.assertEqual(page.evaluate.call_count,1)

    @patch('comic_archive.adapters.tapas.Transport')
    def test_a_further_try_returns_to_the_site_first_when_the_failure_left_the_page_elsewhere(self,assets):
        page=self.failing_page(RuntimeError('PRIVATE'),self.ANSWER)
        page.wait_for_timeout.side_effect=lambda ms:setattr(page,'url','about:blank')
        with patch('builtins.print'):
            self.assertEqual(BrowserReadTransport(page,Mock()).get('https://tapas.io/episode/81/info'),b'{"ok":true}')
        page.goto.assert_called_once_with('https://tapas.io/',wait_until='domcontentloaded')

    def test_default_crawler_uses_the_validated_pager_and_keeps_partial_coverage_explicit(self):
        self.adapter.inspect_library_pages=Mock(return_value={'membership_verified':True,'complete':False,'series':[]})
        checkpoint=Mock()
        result=self.adapter.inspect_library(category='recent',on_page=checkpoint)
        self.adapter.inspect_library_pages.assert_called_once_with('recent',200,on_page=checkpoint)
        self.assertFalse(result['complete'])

    def test_library_pages_follow_adjacent_supplied_links_and_preserve_partial_discoveries(self):
        from unittest.mock import MagicMock
        self.adapter._inspect_library_details=Mock(return_value={'authenticated':True,'membership_verified':True,'scope':'subscribed'})
        self.adapter.automatic_adapter=Mock();self.adapter.wait_normal=Mock()
        first='https://tapas.io/reading-list?category=SUBSCRIBED'
        second='https://tapas.io/reading-list?pageNumber=2&type=ALL&category=SUBSCRIBED'
        self.adapter.page.url=first
        active=Mock();active.count.return_value=1;active.inner_text.return_value='All'
        rows=Mock();rows.evaluate_all.side_effect=[
            [{'url':'https://tapas.io/series/first','title':'First','ids':['42']}],
            [{'url':'https://tapas.io/series/second','title':'Second','ids':['43']}]]
        next_control=Mock();next_control.count.return_value=1;next_control.is_visible.return_value=True
        def attribute(name):
            if name=='class':return 'paging__button--next'+(' disabled' if self.adapter.page.url==second else '')
            if name=='href':return second
        next_control.get_attribute.side_effect=attribute
        next_control.click.side_effect=lambda **_:setattr(self.adapter.page,'url',second)
        def locator(selector):
            if 'sort-item.on' in selector:return active
            if 'paging__button--next' in selector:return next_control
            return rows
        self.adapter.page.locator.side_effect=locator
        navigation=MagicMock();navigation.__enter__.return_value.value.status=200
        self.adapter.page.expect_navigation.return_value=navigation
        def restore(url,**kwargs):
            self.adapter.page.url=url;return Mock(status=200)
        self.adapter.page.goto.side_effect=restore
        result=self.adapter.inspect_library_pages(max_pages=2)
        self.adapter._inspect_library_details.assert_called_once_with('subscribed',research=False)
        self.assertTrue(result['complete']);self.assertTrue(result['boundary_observed'])
        self.assertFalse(result['account_inventory_complete']);self.assertTrue(result['restore_succeeded'])
        self.assertEqual([s['source_series_id'] for s in result['series']],['42','43'])
        next_control.click.assert_called_once();self.assertEqual(self.adapter.page.url,first)
        self.adapter.capture_automatic=Mock()
        rows.evaluate_all.side_effect=None;rows.evaluate_all.return_value=[{'url':'https://tapas.io/series/first','title':'First','ids':['42']}]
        next_control.get_attribute.side_effect=lambda name:'https://tapas.io/ink/purchase' if name=='href' else ''
        next_control.click.reset_mock()
        result=self.adapter.inspect_library_pages(max_pages=2)
        self.assertFalse(result['complete']);self.assertEqual(result['observed_series_count'],1)
        next_control.click.assert_not_called();self.adapter.capture_automatic.assert_not_called()

    @patch('comic_archive.adapters.tapas.Transport')
    def test_metadata_refusal_records_route_and_status_without_response_secrets(self,assets):
        page=Mock(url='https://tapas.io/episode/81')
        page.evaluate.return_value={'status':403,'text':'cookie=PRIVATE_SESSION'}
        transport=BrowserReadTransport(page,Mock())
        with self.assertRaises(ArchiveError) as error:transport.get('https://tapas.io/series/42')
        self.assertIn('/series/42',str(error.exception));self.assertIn('403',str(error.exception))
        self.assertNotIn('PRIVATE',str(error.exception))
        page.evaluate.return_value={'status':'PRIVATE_SESSION','text':None}
        with self.assertRaises(ArchiveError) as error:transport.get('https://tapas.io/series/42')
        self.assertNotIn('PRIVATE',str(error.exception))

    def setUp(self):
        self.adapter = TapasAdapter(Path("unused-test-session"))
        self.adapter.network_guard = Mock()
        self.adapter.page = Mock()
        self.adapter.page.url = "https://tapas.io/episode/81"
        self.adapter.context = Mock()
        self.adapter.episode_access = Mock(return_value={'source_episode_id':'81','access_state':'tapas_free'})
        self.adapter.page.evaluate.return_value = {"scoped": True, "bottom": 0, "viewportBottom": 800}
        rows = json.loads((Path(__file__).parent / "fixtures/reader-snapshot.json").read_text())["loaded"]
        self.adapter.page.locator.return_value.evaluate_all.return_value = rows

    def capture(self):
        return self.adapter.capture("https://tapas.io/episode/81", "42", "Series", "Episode", 1, ".test-episode img")

    @patch("comic_archive.adapters.tapas.Transport")
    @patch("comic_archive.adapters.tapas.save_session")
    @patch("builtins.input", return_value="y")
    def test_capture_normalizes_dom_order(self, prompt, save, transport):
        manifest = self.capture()
        self.assertEqual([p.order for p in manifest.episodes[0].pages], [1, 2])
        self.assertEqual(manifest.episodes[0].access, "tapas_free")
        self.adapter.episode_access.assert_called_once_with('https://tapas.io/episode/81')
        self.assertEqual(self.adapter.page.locator.call_args.args[0], ".test-episode img")
        transport.return_value.get.assert_not_called()

    @patch("comic_archive.adapters.tapas.Transport")
    @patch("comic_archive.adapters.tapas.save_session")
    @patch("builtins.input", return_value="y")
    def test_capture_defaults_to_observed_episode_container(self, prompt, save, transport):
        self.adapter.capture("https://tapas.io/episode/81", "42", "S", "E", 1)
        self.assertEqual(self.adapter.page.locator.call_args.args[0], "#episode-81 img.content__img")

    @patch("comic_archive.adapters.tapas.Transport")
    @patch("comic_archive.adapters.tapas.save_session")
    @patch("builtins.input", return_value=" Y ")
    def test_yes_confirmation_tolerates_case_and_whitespace(self, prompt, save, transport):
        self.assertEqual(self.capture().episodes[0].access, "tapas_free")

    @patch('builtins.input', return_value='y')
    def test_manual_confirmation_cannot_override_metadata_denial_or_session_failure(self, prompt):
        for failure in (AccessDenied('locked'), AuthenticationError('login required'), MetadataError('unknown schema')):
            with self.subTest(error=type(failure).__name__):
                self.adapter.episode_access.side_effect=failure
                with self.assertRaises(type(failure)):
                    self.adapter.capture('https://tapas.io/episode/81','42','S','E',1,reader_confirmed=True)
                self.adapter.page.goto.assert_not_called()
                self.adapter.page.locator.assert_not_called()
                prompt.assert_not_called()

    @patch('comic_archive.adapters.tapas.Transport')
    @patch('comic_archive.adapters.tapas.save_session')
    @patch('builtins.input', return_value='y')
    def test_manual_refresh_rechecks_access_before_loading_the_reader(self, prompt, save, transport):
        episode=self.capture().episodes[0]
        self.adapter.page.goto.reset_mock()
        self.adapter.page.locator.reset_mock()
        self.adapter.episode_access.side_effect=AccessDenied('access expired')
        with self.assertRaises(AccessDenied):
            self.adapter.refresh(episode)
        self.assertEqual(self.adapter.episode_access.call_count,2)
        self.adapter.page.goto.assert_not_called()
        self.adapter.page.locator.assert_not_called()
        transport.return_value.get.assert_not_called()

    @patch("builtins.input", return_value="n")
    def test_no_confirmation_cancels_without_page_capture(self, prompt):
        with self.assertRaises(ArchiveError):
            self.capture()
        self.adapter.page.locator.return_value.evaluate_all.assert_not_called()
        self.assertIsNone(self.adapter.transport)

    @patch("builtins.input", return_value="")
    def test_locked_preview_cancelled_before_selector_or_retrieval(self, prompt):
        with self.assertRaises(ArchiveError):
            self.capture()
        self.adapter.page.locator.return_value.evaluate_all.assert_not_called()
        self.assertIsNone(self.adapter.transport)

    @patch("builtins.input", return_value="y")
    def test_unloaded_duplicate_and_foreign_assets_rejected(self, prompt):
        for rows in [[], [{"tag": "IMG", "url": "https://cdn.tapas.io/x", "loaded": False}],
                     [{"tag": "IMG", "url": "https://elsewhere.test/x", "loaded": True}],
                     [{"tag": "IMG", "url": "https://cdn.tapas.io/x", "loaded": True}] * 2]:
            self.adapter.page.locator.return_value.evaluate_all.return_value = rows
            with self.assertRaises(ArchiveError):
                self.capture()

    @patch("builtins.input", return_value="y")
    def test_login_redirect_rejected(self, prompt):
        self.adapter.page.url = "https://tapas.io/account/signin"
        with self.assertRaises(ArchiveError):
            self.capture()

    def test_worker_scroll_stops_at_episode_boundary(self):
        self.adapter.page.evaluate.return_value = {"scoped": True, "bottom": 1000, "viewportBottom": 1100}
        self.adapter.scroll_episode("https://tapas.io/episode/81")
        self.adapter.page.mouse.wheel.assert_not_called()

    def test_worker_scroll_is_bounded_when_scope_unknown(self):
        self.adapter.page.evaluate.return_value = {"scoped": False, "bottom": 0, "viewportBottom": 800}
        self.adapter.scroll_episode("https://tapas.io/episode/81")
        self.assertEqual(self.adapter.page.mouse.wheel.call_count, 8)

    def test_worker_scroll_visits_each_image_in_nested_scroll_container(self):
        images = self.adapter.page.locator.return_value
        images.count.return_value = 3
        self.adapter.scroll_episode("https://tapas.io/episode/81")
        self.assertEqual(images.nth.call_count, 3)
        self.adapter.page.mouse.wheel.assert_not_called()

    @patch("comic_archive.adapters.tapas.Transport")
    @patch("comic_archive.adapters.tapas.save_session")
    @patch("builtins.input")
    def test_prior_explicit_reader_confirmation_does_not_prompt_again(self, prompt, save, transport):
        self.adapter.capture("https://tapas.io/episode/81", "42", "S", "E", 1,
                             ".test-episode img", reader_confirmed=True)
        prompt.assert_not_called()

    @patch("comic_archive.adapters.tapas.Transport")
    @patch("comic_archive.adapters.tapas.save_session")
    @patch("builtins.input", return_value="y")
    def test_lazy_sources_capture_without_scrolling_or_browser_image_loading(self, prompt, save, transport):
        self.adapter.page.locator.return_value.evaluate_all.return_value = [
            {"tag": "IMG", "url": "data:image/gif;base64,placeholder", "loaded": True,
             "sources": {"data-src": f"https://us-a.tapas.io/pc/a/{i}.jpg?sig=TEST"}}
            for i in range(3)]
        with patch.object(self.adapter, "scroll_episode") as scroll:
            manifest = self.capture()
        scroll.assert_not_called()
        self.assertEqual(len(manifest.episodes[0].pages), 3)
        self.assertEqual(self.adapter.capture_mode, "reader_lazy_sources")
        transport.return_value.get.assert_not_called()

    @patch("comic_archive.adapters.tapas.Transport")
    def test_browser_metadata_gets_use_same_origin_and_images_use_httpx(self, assets):
        page = Mock(url="https://tapas.io/episode/81")
        page.evaluate.return_value = {"status": 200, "text": '{"data":{"free":true}}'}
        transport = BrowserReadTransport(page, Mock())
        self.assertEqual(transport.get("https://tapas.io/episode/81/info"), b'{"data":{"free":true}}')
        script, url = page.evaluate.call_args.args
        self.assertIn("method:'GET'", script)
        self.assertIn("credentials:'same-origin'", script)
        self.assertEqual(url["url"], "https://tapas.io/episode/81/info")
        self.assertEqual(url["timeout"], 30000)
        transport.get("https://us-a.tapas.io/pc/a.jpg?signature=TEST")
        assets.return_value.get.assert_called_once_with("https://us-a.tapas.io/pc/a.jpg?signature=TEST")
        for path in ("/unlock", "/account/delete", "/ink/reward-video", "/series/42/subscribe"):
            with self.assertRaises(ArchiveError):
                transport.get("https://tapas.io" + path)

    @patch("comic_archive.adapters.tapas.Transport")
    def test_browser_metadata_refusal_is_safe_and_does_not_retry(self, assets):
        page = Mock(url="https://tapas.io/episode/81")
        page.evaluate.return_value = {"status": 403, "text": "PRIVATE_SIGNED_URL"}
        transport = BrowserReadTransport(page, Mock())
        with self.assertRaises(ArchiveError) as error:
            transport.get("https://tapas.io/who-am-i")
        self.assertNotIn("PRIVATE", str(error.exception))
        self.assertEqual(page.evaluate.call_count, 1)

    @patch("comic_archive.adapters.tapas.Transport")
    def test_automatic_reader_get_uses_html_text_without_navigation_or_image_preload(self, assets):
        page = Mock(url="https://tapas.io/")
        page.evaluate.return_value={'status':200,'text':'<html>reader</html>'}
        transport = BrowserReadTransport(page, Mock())
        self.assertEqual(transport.get("https://tapas.io/episode/81"),b'<html>reader</html>')
        page.goto.assert_not_called()
        assets.return_value.get.assert_not_called()
        script,options=page.evaluate.call_args.args
        self.assertEqual(options['accept'],'text/html')
        self.assertEqual(options['limit'],8388608)
        self.assertIn("credentials:'same-origin'",script)
        self.assertIn("redirect:'error'",script)
        transport.close()
        page.unroute_all.assert_called_once_with(behavior='wait')

    @patch('comic_archive.adapters.tapas.Transport')
    def test_reader_text_refusal_or_oversize_never_falls_back_to_navigation(self,assets):
        page=Mock(url='https://tapas.io/')
        transport=BrowserReadTransport(page,Mock())
        for status,text in ((403,'PRIVATE'),(429,'PRIVATE'),(200,None)):
            page.evaluate.reset_mock();page.evaluate.return_value={'status':status,'text':text}
            with self.assertRaises(ArchiveError) as caught:
                transport.get('https://tapas.io/episode/81')
            self.assertNotIn('PRIVATE',str(caught.exception))
            page.evaluate.assert_called_once();page.goto.assert_not_called()
            assets.return_value.get.assert_not_called()

    @patch('comic_archive.adapters.tapas.Transport')
    def test_imported_browser_requests_cannot_write_through_the_read_only_route(self,assets):
        transport=BrowserReadTransport(Mock(),Mock())
        for method in ('POST','PUT','PATCH','DELETE'):
            route=Mock();route.request.method=method
            route.request.url='https://tapas.io/observed?token=PRIVATE'
            route.request.resource_type='xhr'
            transport.read_only_request(route)
            route.abort.assert_called_once();route.continue_.assert_not_called()
        self.assertEqual(transport.blocked_mutations,4)

    @patch('comic_archive.adapters.tapas.Transport')
    def test_read_only_browser_route_allows_normal_reads_and_preflights(self,assets):
        page=Mock();transport=BrowserReadTransport(page,Mock())
        page.route.assert_called_once_with('**/*',transport.read_only_request)
        for method in ('GET','HEAD','OPTIONS'):
            route=Mock();route.request.method=method
            transport.read_only_request(route)
            route.continue_.assert_called_once();route.abort.assert_not_called()
        self.assertEqual(transport.blocked_mutations,0)

    @patch('comic_archive.adapters.tapas.Transport')
    def test_read_only_context_guard_survives_transport_replacement_and_manual_fallback(self,assets):
        adapter=TapasAdapter(Path('.comic-session'))
        adapter.network_guard=Mock()
        adapter.context=Mock();adapter.page=Mock()
        adapter.automatic_adapter();adapter.automatic_adapter();adapter.transport.close()
        adapter.context.route.assert_called_once_with('**/*',adapter.network_read_only_request)
        adapter.context.unroute.assert_not_called()
        for method,denied in (('POST',True),('GET',False)):
            route=Mock();route.request.method=method
            adapter.network_read_only_request(route)
            if denied:route.abort.assert_called_once();route.continue_.assert_not_called()
            else:route.continue_.assert_called_once();route.abort.assert_not_called()
        self.assertEqual(adapter.network_guard.check.call_count,2)

    @patch('comic_archive.adapters.tapas.Transport')
    def test_guard_installation_failure_closes_resources_and_suppresses_credentials(self,assets):
        page=Mock();page.route.side_effect=RuntimeError('different loop PRIVATE_COOKIE https://example.test/?session=PRIVATE')
        with self.assertRaises(ArchiveError) as error:BrowserReadTransport(page,Mock())
        self.assertIn('browser_loop',str(error.exception));self.assertNotIn('PRIVATE',str(error.exception))
        assets.return_value.close.assert_called_once()
        page.unroute.assert_called_once();page.goto.assert_not_called();page.evaluate.assert_not_called()

    @patch('comic_archive.adapters.tapas.Transport')
    def test_late_mutation_route_callback_never_continues_or_retries_the_request(self,assets):
        from comic_archive.adapters.tapas import read_only_source_request
        transport=BrowserReadTransport(Mock(),Mock())
        for handler in (read_only_source_request,transport.read_only_request):
            route=Mock();route.request.method='POST'
            route.abort.side_effect=RuntimeError('Route.abort: Route is already handled!')
            handler(route)
            route.abort.assert_called_once()
            route.continue_.assert_not_called()

    @patch('comic_archive.adapters.tapas.Transport')
    def test_unexpected_callback_failure_is_sanitized_and_assets_close_after_cleanup_failure(self,assets):
        page=Mock();transport=BrowserReadTransport(page,Mock())
        route=Mock();route.request.method='POST'
        route.abort.side_effect=RuntimeError('Unexpected Route is already handled! PRIVATE_COOKIE https://example.test/?session=PRIVATE')
        with self.assertRaises(ArchiveError) as error:
            transport.read_only_request(route)
        self.assertNotIn('PRIVATE',str(error.exception))
        route.continue_.assert_not_called()
        page.unroute_all.side_effect=RuntimeError('cleanup failure')
        with self.assertRaises(RuntimeError):transport.close()
        assets.return_value.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
