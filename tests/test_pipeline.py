import threading
import unittest
from collections import Counter
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

from comic_archive.cancellation import RunCancelled
from comic_archive.errors import AccessDenied, AuthenticationError, RunLimit, UnsupportedContent
from comic_archive.models import ArchiveError, Episode, Manifest, Page, Series
from comic_archive.pipeline import MetadataPipeline

URL = 'https://tapas.io/episode/81'
OTHER = 'https://tapas.io/episode/82'


def reader(url, pages):
    key = url.rsplit('/', 1)[1]
    return Manifest(Series('s', 'S', platform_id='42'),
                    (Episode(key, 's', 'E', int(key) - 80, tuple(pages), 'tapas_free', url),))


class Browser:
    """Records which thread made each call and what job state travelled with it."""

    def __init__(self, case):
        self.case = case
        self.calls = []
        self.failures = {}
        self.pages = (Page(1, 'https://us-a.tapas.io/pc/1.png?signature=A', False),)
        self.transport = SimpleNamespace(assets=SimpleNamespace(get=self.get))
        self.deadline = self.cancel_check = self.asset_metrics = self.expected_account_id = None

    def record(self, name, *args, **kwargs):
        self.calls.append(SimpleNamespace(
            name=name, thread=threading.get_ident(), args=args, kwargs=kwargs, deadline=self.deadline,
            cancel_check=self.cancel_check, metrics=self.asset_metrics, account=self.expected_account_id))

    def __enter__(self):
        self.record('enter')
        return self

    def __exit__(self, *arguments):
        self.record('exit')

    def get(self, location, max_bytes=None):
        self.record('assets.get', location, max_bytes=max_bytes)
        return b'asset'

    def capture_automatic(self, url, **kwargs):
        self.record('capture_automatic', url, **kwargs)
        if url in self.failures:
            raise self.failures[url]
        return reader(url, self.pages)

    def retrieve(self, page, max_bytes=None):
        self.record('retrieve', page.location, max_bytes=max_bytes)
        return b'browser asset'

    def account_identity(self):
        self.record('account_identity')
        return '11'

    def series_metadata(self, key):
        self.record('series_metadata', key)
        return 'metadata ' + key

    def series_identity(self, key):
        self.record('series_identity', key)
        return 'identity ' + key

    def inspect_series(self, key, **kwargs):
        self.record('inspect_series', key, **kwargs)
        return {'source_series_id': key}

    def episode_access(self, url):
        self.record('episode_access', url)
        return 'access ' + url

    def retrieve_cover(self, series, max_bytes=None):
        self.record('retrieve_cover', series, max_bytes=max_bytes)
        return b'cover'


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.browsers = []

    def factory(self):
        browser = Browser(self)
        self.browsers.append(browser)
        return browser

    @contextmanager
    def pipeline(self, profile=None):
        with MetadataPipeline(self.factory, profile) as pipeline:
            yield pipeline

    def settle(self, pipeline):
        """Calls run in order on the one owner thread, so a finished call means earlier work is done."""
        pipeline.account_identity()

    def test_failed_browser_start_is_reported_and_leaves_no_owner_thread(self):
        def broken():
            raise AuthenticationError('Login needed')
        pipeline = MetadataPipeline(broken, None)
        before = set(threading.enumerate())
        with self.assertRaises(AuthenticationError):
            pipeline.__enter__()
        self.assertEqual([t for t in threading.enumerate() if t not in before], [])
        del pipeline  # held until now: a collected pool would let a leaked worker exit by itself

    def test_every_browser_call_runs_on_the_one_owner_thread_and_carries_the_job_state(self):
        marker = object()
        with self.pipeline() as pipeline:
            pipeline.deadline, pipeline.cancel_check = 123.0, marker
            pipeline.asset_metrics, pipeline.expected_account_id = 'metrics', '11'
            page = Page(1, 'https://tapas.io/pc/1.png', False)
            self.assertEqual(pipeline.account_identity(), '11')
            self.assertEqual(pipeline.series_metadata('42'), 'metadata 42')
            self.assertEqual(pipeline.series_identity('42'), 'identity 42')
            self.assertEqual(pipeline.inspect_series('42', strict_order=False), {'source_series_id': '42'})
            self.assertEqual(pipeline.episode_access(URL), 'access ' + URL)
            self.assertEqual(pipeline.retrieve_cover('series', max_bytes=7), b'cover')
            self.assertEqual(pipeline.retrieve(page, max_bytes=9), b'browser asset')
        browser = self.browsers[0]
        names = [call.name for call in browser.calls]
        self.assertEqual(names, ['enter', 'account_identity', 'series_metadata', 'series_identity', 'inspect_series',
                                 'episode_access', 'retrieve_cover', 'retrieve', 'exit'])
        self.assertEqual(len({call.thread for call in browser.calls}), 1)
        self.assertNotIn(threading.get_ident(), {call.thread for call in browser.calls})
        for call in browser.calls[1:-1]:
            self.assertEqual((call.deadline, call.cancel_check, call.metrics, call.account),
                             (123.0, marker, 'metrics', '11'))
        by_name = {call.name: call for call in browser.calls}
        self.assertEqual(by_name['inspect_series'].kwargs, {'strict_order': False})
        self.assertEqual(by_name['retrieve_cover'].kwargs, {'max_bytes': 7})
        self.assertEqual(by_name['retrieve'].kwargs, {'max_bytes': 9})

    def test_images_from_other_hosts_use_the_persistent_asset_client_not_the_browser(self):
        with self.pipeline() as pipeline:
            data = pipeline.retrieve(Page(1, 'https://us-a.tapas.io/pc/1.png?signature=A', False), max_bytes=50)
        self.assertEqual(data, b'asset')
        calls = [call for call in self.browsers[0].calls if call.name in ('assets.get', 'retrieve')]
        self.assertEqual([(call.name, call.args, call.kwargs) for call in calls],
                         [('assets.get', ('https://us-a.tapas.io/pc/1.png?signature=A',), {'max_bytes': 50})])
        self.assertEqual(calls[0].thread, threading.get_ident())

    def test_prefetched_episode_is_used_once_and_only_for_the_url_it_was_prepared_for(self):
        phases = []
        profile = SimpleNamespace(calls=Counter(), phase=lambda name: phases.append(name) or _Null())
        with self.pipeline(profile) as pipeline:
            pipeline.prefetch_episode(URL)
            with self.assertRaises(ArchiveError):
                pipeline.prefetch_episode(OTHER)
            self.assertEqual(pipeline.capture_automatic(URL).episodes[0].id, '81')
            pipeline.prefetch_episode(URL)
            with self.assertRaises(ArchiveError):
                pipeline.capture_automatic(OTHER)
            self.assertEqual(pipeline.capture_automatic(OTHER).episodes[0].id, '82')
        self.assertEqual(profile.calls['prefetch_submitted'], 2)
        self.assertEqual(phases, ['capture_wait'] * 3)
        captures = [call.args[0] for call in self.browsers[0].calls if call.name == 'capture_automatic']
        self.assertEqual(captures, [URL, URL, OTHER])

    def test_new_batch_needs_the_previous_work_finished_and_the_browser_still_trustworthy(self):
        with self.pipeline() as pipeline:
            pipeline.deadline, pipeline.expected_account_id = 5.0, '11'
            pipeline.begin_batch()
            self.assertEqual((pipeline.deadline, pipeline.cancel_check, pipeline.asset_metrics,
                              pipeline.expected_account_id), (None, None, None, None))
            pipeline.prefetch_episode(URL)
            with self.assertRaises(ArchiveError):
                pipeline.begin_batch()
            pipeline.finish_batch()
            pipeline.begin_batch()
            pipeline.finish_batch(ArchiveError('Reader refused'))
            self.assertFalse(pipeline.reusable)
            with self.assertRaises(ArchiveError):
                pipeline.begin_batch()

    def test_batch_end_decides_which_late_prefetch_failure_is_reported_and_whether_the_browser_is_reused(self):
        cases = (
            ('cancellation, clean batch', RunCancelled('Stopped'), None, RunCancelled, False),
            ('cancellation after a limit', RunCancelled('Stopped'), RunLimit('Page limit'), RunCancelled, False),
            ('cancellation after another failure', RunCancelled('Stopped'), ArchiveError('Earlier'), None, False),
            ('access refusal', AccessDenied('Locked'), None, None, True),
            ('limit', RunLimit('Page limit'), None, None, True),
            ('unsupported content', UnsupportedContent('Novel'), None, None, True),
            ('other failure, clean batch', ArchiveError('Late'), None, ArchiveError, False),
            ('other failure after a limit', ArchiveError('Late'), RunLimit('Page limit'), ArchiveError, False),
            ('other failure after another failure', ArchiveError('Late'), AuthenticationError('Earlier'), None, False),
        )
        for label, late, problem, raised, reusable in cases:
            with self.subTest(label), self.pipeline() as pipeline:
                self.browsers[-1].failures[URL] = late
                pipeline.prefetch_episode(URL)
                if raised is None:
                    pipeline.finish_batch(problem)
                else:
                    with self.assertRaises(raised) as caught:
                        pipeline.finish_batch(problem)
                    self.assertIs(caught.exception, late)
                self.assertEqual(pipeline.reusable, reusable)
                pipeline.finish_batch()

    def test_failure_of_the_prepared_episode_stops_new_images_unless_it_is_only_an_access_refusal(self):
        page = Page(1, 'https://us-a.tapas.io/pc/1.png', False)
        for label, late, stops in (('network failure', ArchiveError('Gateway'), True),
                                   ('login expired', AuthenticationError('Login'), True),
                                   ('locked', AccessDenied('Locked'), False),
                                   ('novel', UnsupportedContent('Novel'), False)):
            with self.subTest(label), self.pipeline() as pipeline:
                self.browsers[-1].failures[URL] = late
                pipeline.prefetch_episode(URL)
                self.settle(pipeline)
                if stops:
                    with self.assertRaises(type(late)):
                        pipeline.retrieve(page)
                    with self.assertRaises(type(late)):
                        pipeline.asset_request(page)
                    with self.assertRaises(type(late)):
                        pipeline.finish_batch()
                    self.assertFalse(pipeline.reusable)
                else:
                    self.assertEqual(pipeline.retrieve(page), b'asset')
                    pipeline.finish_batch()
                    self.assertTrue(pipeline.reusable)

    def test_refresh_returns_new_addresses_but_refuses_a_changed_page_manifest(self):
        old = reader(URL, (Page(1, 'https://us-a.tapas.io/pc/1.png?signature=OLD', False),
                           Page(2, 'https://us-a.tapas.io/pc/2.png?signature=OLD', False))).episodes[0]
        same = (Page(1, 'https://us-a.tapas.io/pc/1.png?signature=NEW', False),
                Page(2, 'https://us-a.tapas.io/pc/2.png?signature=NEW', False))
        changes = {
            'fewer pages': same[:1],
            'more pages': same + (Page(3, 'https://us-a.tapas.io/pc/3.png', False),),
            'another file': (same[0], replace(same[1], location='https://us-a.tapas.io/pc/9.png')),
            'another host': (same[0], replace(same[1], location='https://evil.example/pc/2.png')),
            'reordered': same[::-1],
        }
        with self.pipeline() as pipeline:
            browser = self.browsers[0]
            browser.pages = same
            refreshed = pipeline.refresh(old)
            self.assertEqual([p.location for p in refreshed.pages], [p.location for p in same])
            call = browser.calls[-1]
            self.assertEqual((call.args, call.kwargs), ((URL,), {'series_id': 's', 'title': 'E', 'order': 1}))
            for label, pages in changes.items():
                with self.subTest(label):
                    browser.pages = pages
                    with self.assertRaises(ArchiveError):
                        pipeline.refresh(old)


class _Null:
    def __enter__(self):
        return self

    def __exit__(self, *arguments):
        return False


if __name__ == '__main__':
    unittest.main()
