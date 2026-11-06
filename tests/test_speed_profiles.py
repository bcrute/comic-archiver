"""Named rates are fixed internally; exercise actual transport admission with a fake clock."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from comic_archive import sites
from comic_archive.android_adapter import AndroidTapasAdapter
from comic_archive.adapters.site_reader import SiteReader
from comic_archive.fast_import import relative_policy, FAST_INTERVAL, FAST_METADATA_INTERVAL, FAST_WORKERS
from comic_archive.models import ArchiveError, Page
from comic_archive.transport import Transport
from comic_archive.worker import execute, _EmbeddedFastUnavailable


class OtherReader(SiteReader):
    site = sites.find('webtoon')

    def __init__(self, transport):
        self.transport = transport


class SpeedProfilesTests(unittest.TestCase):
    def test_both_image_rate_and_capacity_follow_the_requested_ratios_for_each_provider(self):
        for other, workers, images, metadata in ((False, FAST_WORKERS, FAST_INTERVAL, FAST_METADATA_INTERVAL),
                                                  (True, 8, 1 / 32, .25)):
            for speed, ratio in (('fast', 1), ('medium', .5), ('slow', .25)):
                policy = relative_policy(speed, other_site=other)
                self.assertEqual(policy['download_workers'], workers * ratio)
                self.assertEqual(policy['image_interval'], images / ratio)
                self.assertEqual(policy['metadata_interval'], metadata / ratio)

    def test_native_http_and_other_readers_use_separate_aggregate_rates_and_restore_after_failures(self):
        for other in (False, True):
            for speed in ('medium', 'slow'):
                for fail in (False, True):
                    with self.subTest(other=other, speed=speed, fail=fail):
                        clock, seen = [100.0], []
                        def response(request):
                            seen.append((clock[0], request.url.host))
                            return httpx.Response(200, content=b'fixture')
                        transport = Transport(client=httpx.Client(transport=httpx.MockTransport(response)),
                                              site=sites.find('webtoon') if other else sites.DEFAULT)
                        self.addCleanup(transport.close)
                        transport._wait = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
                        adapter = OtherReader(transport) if other else AndroidTapasAdapter(transport=transport)
                        policy = relative_policy(speed, other_site=other)
                        def archive(actual, *args, **options):
                            self.assertIs(actual, adapter)
                            self.assertEqual(options['download_workers'], policy['download_workers'])
                            self.assertEqual(adapter.download_profile, 'fast')
                            for number in range(1, 4):
                                page = Page(number, 'https://cdn.tapas.io/image' if not other else
                                            'https://webtoon-phinf.pstatic.net/image', False)
                                adapter.asset_request(page).fetch()
                            transport.get(adapter.site.home if other else 'https://tapas.io/')
                            transport.get(adapter.site.home if other else 'https://tapas.io/')
                            if fail:
                                raise ArchiveError('fixture refusal')
                            return {'status': 'limited'}
                        with patch('comic_archive.cloud.upload_pending'), \
                                patch('comic_archive.jobs.archive_discovered', side_effect=archive), \
                                patch('comic_archive.transport.time.monotonic', side_effect=lambda: clock[0]):
                            request = {'operation': 'archive_discovered', 'args': {'speed': speed}}
                            if fail:
                                with self.assertRaises(ArchiveError):
                                    execute(adapter, request, fast_session=_EmbeddedFastUnavailable())
                            else:
                                self.assertEqual(execute(adapter, request, fast_session=_EmbeddedFastUnavailable()),
                                                 {'status': 'limited'})
                        self.assertEqual(len(seen), 5)
                        self.assertAlmostEqual(seen[1][0] - seen[0][0], policy['image_interval'])
                        self.assertAlmostEqual(seen[2][0] - seen[1][0], policy['image_interval'])
                        self.assertAlmostEqual(seen[4][0] - seen[3][0], policy['metadata_interval'])
                        self.assertEqual(transport.interval, 1.0)
                        self.assertIsNone(transport.image_interval)
                        self.assertEqual(getattr(adapter, 'download_profile', 'normal'), 'normal')

    def test_named_policies_do_not_allow_queue_callers_to_supply_fast_resource_caps(self):
        for speed in ('medium', 'slow'):
            for options in ({'download_workers': 64}, {'request_interval': 1 / 128}, {'speed': 'unknown'}):
                with self.subTest(speed=speed, options=options), patch('comic_archive.cloud.upload_pending'), \
                        self.assertRaises(ArchiveError):
                    execute(SimpleNamespace(interval=1.0), {'operation': 'archive_discovered',
                                                          'args': {'speed': speed} | options})

    def test_metadata_scans_restore_the_native_http_pace_on_success_and_failure(self):
        from comic_archive.jobs import sync
        for speed in ('medium', 'slow'):
            for fail in (False, True):
                transport = Transport(client=httpx.Client(transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, content=b'fixture'))))
                self.addCleanup(transport.close)
                adapter = AndroidTapasAdapter(transport=transport)
                def archive(*args, **options):
                    self.assertEqual(transport.interval, relative_policy(speed)['metadata_interval'])
                    if fail:
                        raise ArchiveError('fixture refusal')
                    return {'status': 'finished'}
                with tempfile.TemporaryDirectory() as folder, \
                        patch('comic_archive.jobs.archive_discovered', side_effect=archive):
                    if fail:
                        with self.assertRaises(ArchiveError):
                            sync(adapter, Path(folder), scan_only=True, metadata_speed=speed)
                    else:
                        sync(adapter, Path(folder), scan_only=True, metadata_speed=speed)
                self.assertEqual(transport.interval, 1.0)
