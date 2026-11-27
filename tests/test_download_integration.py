"""Whole account workflow with the network replaced by exact synthetic responses.

Controllers, queue files, worker dispatch, HTTP normalization, authorization,
inventory, archive publication, verification and standalone restore are real.
"""
import base64
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

from comic_archive import download_control
from comic_archive.adapters.tapas_http import TapasHTTPAdapter
from comic_archive.archive import episode_archives, sidecar_path, verify
from comic_archive.inventory import Inventory
from comic_archive.errors import AccessDenied, AuthenticationError
from comic_archive.models import ArchiveError
from comic_archive.run_receipts import failure_result
from comic_archive.worker import execute, private_json
from scripts.offline_restore import restore
from test_archive import png


def envelope(value):
    return json.dumps({'data': value, 'error_details': None}).encode()


def library_archives(root):
    return [p for folder in sorted(root.iterdir()) if folder.is_dir() and not folder.name.startswith('.')
            for p in episode_archives(folder)]


class SyntheticTransport:
    def __init__(self):
        self.account = 11
        self.calls = []
        self.revoked = set()
        self.episodes = {
            '81': {'id': 81, 'scene': 1, 'title': 'Free comic', 'book': False, 'free': True},
            '82': {'id': 82, 'scene': 2, 'title': 'Unlocked comic', 'book': False, 'unlocked': True},
            '83': {'id': 83, 'scene': 3, 'title': 'Locked comic', 'book': False},
            '91': {'id': 91, 'scene': 1, 'title': 'Illustrated novel', 'book': True, 'free': True},
            '92': {'id': 92, 'scene': 2, 'title': 'Scheduled novel', 'book': True, 'scheduled': True},
        }

    def info(self, key):
        value = {'free': False, 'unlocked': False, 'must_pay': False, 'scheduled': False,
                 'closing_date': None, **self.episodes[key]}
        if key in self.revoked:
            value.update(free=False, unlocked=False)
        return value

    def get(self, url, **options):
        value = urlsplit(url)
        self.calls.append((value.hostname, value.path))
        if value.hostname == 'us-a.tapas.io' and value.path.startswith('/pc/'):
            return png(1)
        if value.hostname != 'tapas.io':
            raise AssertionError('Unexpected offline transport destination')
        if value.path == '/who-am-i':
            return envelope({'user': {'id': self.account}} if self.account else {'user': None})
        parts = value.path.strip('/').split('/')
        if len(parts) >= 2 and parts[0] == 'series' and parts[1] in ('42', '43'):
            novel = parts[1] == '43'
            if len(parts) == 2:
                return envelope({'id': int(parts[1]), 'title': 'Novel' if novel else 'Comic',
                                 'url': 'novel' if novel else 'comic', 'comic': not novel, 'book': novel})
            if parts[2:] == ['episodes']:
                keys = ('91', '92') if novel else ('81', '82', '83')
                return envelope({'episodes': [self.info(k) for k in keys],
                                 'pagination': {'page': 1, 'total': len(keys), 'has_next': False}})
        if len(parts) >= 2 and parts[0] == 'episode' and parts[1] in self.episodes:
            key = parts[1]
            if parts[2:] == ['info']:
                return envelope(self.info(key))
            if len(parts) == 2:
                if key in self.revoked:
                    raise AccessDenied('The provider refused the requested reader.')
                if key in ('83', '92'):
                    raise AssertionError('Unowned catalogue reader requested')
                if key == '91':
                    image = base64.b64encode(png(1)).decode()
                    # A real supplied type-label mismatch must retain the PNG bytes.
                    return (f'<script>var seriesId=43;</script><div id="episode-91">'
                            f'<div id="epub-91" class="ep-epub-content"><p>Unicode 尾声 '
                            f'<strike>old</strike><img src="data:image/jpeg;base64,{image}" alt="Art" />'
                            '</p></div></div>').encode()
                return (f'<script>var seriesId=42;</script><div id="episode-{key}">'
                        f'<img class="content__img" src="https://us-a.tapas.io/pc/{key}-1.png">'
                        f'<img class="content__img" src="https://us-a.tapas.io/pc/{key}-2.png">'
                        '</div>').encode()
        raise AssertionError('Unexpected offline transport path')


class OfflineAdapter(TapasHTTPAdapter):
    supports_novels = True

    def __init__(self):
        super().__init__(transport=SyntheticTransport())

    def inspect_library(self, category):
        self.check_session()
        return {'authenticated': True, 'membership_verified': True, 'scope': category,
                'complete': True, 'boundary_observed': True,
                'series': [{'source_series_id': k, 'title': t} for k, t in (('42', 'Comic'), ('43', 'Novel'))]}

    def episode_url(self, key):
        return 'https://tapas.io/episode/' + key

    def capture_automatic(self, url):
        return self.capture(url)


class DownloadIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        previous = Path.cwd()
        os.chdir(self.project)
        self.addCleanup(os.chdir, previous)
        self.session = self.project / '.comic-session'
        self.library = self.project / 'Library'
        self.root = self.session / 'worker'
        (self.root / 'requests').mkdir(parents=True)
        (self.root / 'responses').mkdir()
        private_json(self.root / 'status.json', {'state': 'ready'})
        with Inventory(self.library):
            pass
        self.adapter = OfflineAdapter()
        self.fast_session = None
        self.executed = []
        self.last_response = None
        self.last_task = None
        # The ordinary downloader's pace; Go at each speed is driven below.
        (self.session / 'download-speed').write_text('medium\n')

    def start(self):
        download_control.start(self.session, self.library)

    def drive(self, *, stop=None, crash_after_archive_receipt=False):
        for _ in range(200):
            download_control.worker_tick(self.session, self.library)
            state = download_control.status(self.session, self.library)
            if state.get('status') in ('held', 'finished'):
                return state
            if stop is not None and stop(state):
                return state
            for path in sorted((self.root / 'requests').glob('*.json')):
                receipt = self.root / 'responses' / path.name
                if receipt.exists():
                    path.unlink()
                    continue
                request = json.loads(path.read_text())
                try:
                    with patch('builtins.print'):
                        result = execute(self.adapter, request, fast_session=self.fast_session)
                    response = {'task_id': path.stem, 'status': 'ok', 'result': result}
                except ArchiveError as error:
                    response = {'task_id': path.stem, 'status': 'error', 'error': str(error),
                                'error_code': 'login_required' if isinstance(error, AuthenticationError) else 'job_failed'}
                    result = failure_result(error)
                    if result is not None:
                        response['result'] = result
                private_json(receipt, response)
                self.last_response, self.last_task = response, path.stem
                self.executed.append(request['operation'])
                if (crash_after_archive_receipt and request['operation'] == 'archive_discovered'
                        and response.get('result', {}).get('archived_episode_ids')):
                    return state  # Simulate death before deleting the completed request.
                path.unlink()
        self.fail('The download did not converge: ' + repr({'state': state, 'last_response': self.last_response}))

    def images(self, calls=None):
        return len([p for host, p in (self.adapter.transport.calls if calls is None else calls) if host == 'us-a.tapas.io'])

    def go(self, speed):
        """The page's Go at one of its speeds, driven to the end of the whole account."""
        from comic_archive.local_library import LOCAL_OWNER, LocalWorkspaceRegistry
        private_json(self.session/'state.json',{'cookies':[],'origins':[]})
        registry=LocalWorkspaceRegistry(self.project)
        # The synthetic HTTP adapter captures immediately; browser lookahead
        # is covered by the real pooled-worker regressions elsewhere.
        self.adapter.prefetch_episode=lambda url:None
        if speed == 'fast':
            # The fast importer's second browser session is this same synthetic adapter.
            self.adapter.download_profile='fast'
            self.fast_session=SimpleNamespace(batch=lambda:nullcontext(self.adapter))
        self.assertEqual(registry.choose_speed(LOCAL_OWNER,registry.workspace_id,speed)['speed'],speed)
        self.assertEqual(registry.start_account(LOCAL_OWNER,registry.workspace_id)['status'],'running')
        state=self.drive()
        self.assertEqual(state['status'],'finished',self.last_response)
        self.assertEqual(registry.account_status(LOCAL_OWNER,registry.workspace_id)['audit']['recorded_archived_episode_count'],3)
        self.assertEqual({p.suffix for p in library_archives(self.library)},{'.cbz','.epub'})
        self.assertFalse(any(path in ('/episode/83','/episode/92') for _,path in self.adapter.transport.calls))
        self.assertEqual(verify(self.library,decode_images=True),[])
        self.assertFalse(registry.account_status(LOCAL_OWNER,registry.workspace_id)['coverage_verified'])

    def test_go_at_the_fast_speed_saves_the_whole_account_comic_and_novel(self):
        self.go('fast')

    def test_go_at_the_medium_speed_saves_the_whole_account_comic_and_novel(self):
        self.go('medium')

    def test_go_at_the_slow_speed_saves_the_whole_account_comic_and_novel(self):
        self.go('slow')

    def test_removed_episode_does_not_hold_controller_and_readable_missing_info_is_saved(self):
        from comic_archive.errors import MetadataError
        self.start()
        self.assertEqual(self.drive()['status'], 'finished')
        before = {path: path.read_bytes() for path in library_archives(self.library)}
        transport = self.adapter.transport
        transport.episodes.update({
            '84': {'id': 84, 'scene': 4, 'title': 'Removed', 'book': False, 'free': True},
            '85': {'id': 85, 'scene': 5, 'title': 'Readable', 'book': False, 'free': True}})
        normal = transport.get
        def get(url, **options):
            path = urlsplit(url).path
            if path == '/series/42/episodes':
                data = json.loads(normal(url, **options))['data']
                data['episodes'] += [transport.info(key) for key in ('84', '85')]
                data['pagination']['total'] += 2
                return envelope(data)
            if path in ('/episode/84/info', '/episode/85/info', '/episode/84'):
                transport.calls.append((urlsplit(url).hostname, path))
                error = MetadataError('PRIVATE_PROVIDER_RESPONSE')
                error.source_http_status = 404
                raise error
            if path == '/episode/85':
                self.assertTrue(options.get('reader_json'))
                html = normal(url).decode().replace('id="episode-85"', 'id="episode-85" data-series-id="42"')
                return envelope({'episode': transport.info('85'), 'html': html})
            return normal(url, **options)
        transport.get = get
        self.start()
        state = self.drive()
        self.assertEqual(state['status'], 'finished', self.last_response)
        self.assertEqual(state['audit']['recorded_archived_episode_count'], 4)
        self.assertEqual(verify(self.library, decode_images=True), [])
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        with Inventory(self.library, read_only=True) as inventory:
            removed = inventory.db.execute("SELECT status,error_code FROM episodes WHERE id='84'").fetchone()
            self.assertEqual(tuple(removed), ('unavailable', 'access_denied'))
            self.assertEqual(inventory.db.execute("SELECT status FROM episodes WHERE id='85'").fetchone()[0], 'archived')
        # There is no timed account-wide retry or failed task for a missing reader.
        self.assertFalse(any(item['outcome'] == 'error' for item in self.controller_tasks()))

    def unreadable(self, keys=None):
        """The site answers for these episodes (all of them when none are named) with data the reader cannot use:
        nothing about the account or the connection. Returns the call that puts the site right again."""
        transport = self.adapter.transport
        normal = transport.get

        def get(url, **options):
            parts = urlsplit(url).path.strip('/').split('/')
            if len(parts) == 3 and parts[0] == 'episode' and parts[2] == 'info' and (keys is None or parts[1] in keys):
                transport.calls.append((urlsplit(url).hostname, urlsplit(url).path))
                return envelope(transport.info(parts[1]) | {'book': None})
            return normal(url, **options)
        transport.get = get

        def repaired():
            transport.get = normal
        return repaired

    def moving_clock(self):
        """The controller's clock, moved past each wait it shows, so that no test sleeps: (the stop to drive
        with, the reasons of the waits it passed)."""
        from datetime import datetime, timedelta, timezone
        now = [datetime.now(timezone.utc)]
        clock = patch('comic_archive.download_control._now', side_effect=lambda: now[0])
        clock.start()
        self.addCleanup(clock.stop)
        waits = []

        def waited(state):
            if 'retry' in state:
                waits.append(state['retry']['reason'])
                now[0] += timedelta(seconds=state['retry']['seconds'] + 1)
            return False
        return waited, waits

    def episode_rows(self, *keys):
        with Inventory(self.library, read_only=True) as inventory:
            return [tuple(inventory.db.execute('SELECT status,error_code,attempts FROM episodes WHERE id=?', (key,)).fetchone())
                    for key in keys]

    def test_a_series_whose_episodes_all_fail_never_stops_the_other_series_and_the_end_says_how_many_were_not_saved(self):
        repaired = self.unreadable(('81', '82'))
        waited, waits = self.moving_clock()
        self.start()
        state = self.drive(stop=waited)
        # The other series is saved in the same run: no other Go, no failed step, and nothing waited for.
        self.assertEqual({path.suffix for path in library_archives(self.library)}, {'.epub'})
        self.assertFalse(any(item['outcome'] == 'error' for item in self.controller_tasks()))
        self.assertEqual(waits, [])
        # The run ends held, counting what it could not save; each such episode had its tries and is held as failed.
        self.assertEqual((state['status'], state['code']), ('held', 'archive_incomplete'), self.last_response)
        self.assertEqual((state['audit']['recorded_archived_episode_count'],
                          state['audit']['missing_recorded_accessible_episode_count']), (1, 2))
        self.assertEqual(self.episode_rows('81', '82'), [('failed', 'job_failed', 3)] * 2)
        with Inventory(self.library, read_only=True) as inventory:
            self.assertEqual(inventory.unsaved_codes(), {'failed:job_failed': 2})
        self.assertEqual(verify(self.library, decode_images=True), [])
        # Once the site answers properly, Go saves them.
        repaired()
        self.start()
        state = self.drive(stop=waited)
        self.assertEqual(state['status'], 'finished', self.last_response)
        self.assertEqual(state['audit']['recorded_archived_episode_count'], 3)
        self.assertEqual(verify(self.library, decode_images=True), [])

    def test_the_same_failure_in_series_after_series_of_a_waiting_queue_stops_and_holds_and_sets_no_episode_aside(self):
        # Four more series with one free episode each, so that episodes of six series wait, as after a download
        # that was interrupted; then the site answers for every episode with data the reader cannot use.
        others = {'44': '84', '45': '85', '46': '86', '47': '87'}
        transport = self.adapter.transport
        transport.episodes.update({key: {'id': int(key), 'scene': 1, 'title': 'Other comic', 'book': False, 'free': True}
                                   for key in others.values()})
        normal, normal_list = transport.get, self.adapter.inspect_library

        def get(url, **options):
            parts = urlsplit(url).path.strip('/').split('/')
            if len(parts) >= 2 and parts[0] == 'series' and parts[1] in others:
                transport.calls.append((urlsplit(url).hostname, urlsplit(url).path))
                if len(parts) == 2:
                    return envelope({'id': int(parts[1]), 'title': 'Comic ' + parts[1], 'url': 'comic-' + parts[1],
                                     'comic': True, 'book': False})
                return envelope({'episodes': [transport.info(others[parts[1]])],
                                 'pagination': {'page': 1, 'total': 1, 'has_next': False}})
            if len(parts) == 2 and parts[0] == 'episode' and parts[1] in others.values():
                transport.calls.append((urlsplit(url).hostname, urlsplit(url).path))
                series = next(series for series, key in others.items() if key == parts[1])
                return (f'<script>var seriesId={series};</script><div id="episode-{parts[1]}">'
                        f'<img class="content__img" src="https://us-a.tapas.io/pc/{parts[1]}-1.png">'
                        f'<img class="content__img" src="https://us-a.tapas.io/pc/{parts[1]}-2.png">'
                        '</div>').encode()
            return normal(url, **options)

        def library(category):
            result = normal_list(category)
            result['series'] += [{'source_series_id': key, 'title': 'Comic ' + key} for key in others]
            return result
        transport.get, self.adapter.inspect_library = get, library
        with Inventory(self.library) as inventory:
            inventory.observe('42', 'Comic', [{'id': '81', 'title': 'Free comic', 'order': 1, 'access_state': 'tapas_free'},
                                              {'id': '82', 'title': 'Unlocked comic', 'order': 2,
                                               'access_state': 'tapas_unlocked'}], 'subscribed')
            inventory.observe('43', 'Novel', [{'id': '91', 'title': 'Illustrated novel', 'order': 1,
                                               'access_state': 'tapas_free'}], 'subscribed')
            for series, key in others.items():
                inventory.observe(series, 'Comic ' + series, [{'id': key, 'title': 'Other comic', 'order': 1,
                                                               'access_state': 'tapas_free'}], 'subscribed')
        repaired = self.unreadable()
        waited, waits = self.moving_clock()
        self.start()
        state = self.drive(stop=waited)
        # One episode of each of three series failed the same way and the step stopped; so did the one try it was
        # given after a wait. The download holds for the person instead of going through every episode's tries.
        self.assertEqual((state['status'], state['code']), ('held', 'task_failed'), self.last_response)
        self.assertEqual([item['outcome'] for item in self.controller_tasks()], ['error', 'error'])
        self.assertEqual(waits, ['idle'])
        self.assertEqual(self.episode_rows('82', '91', '84', '85', '86', '87'), [('failed', 'job_failed', 1)] * 6)
        self.assertEqual(self.episode_rows('81'), [('queued', None, 0)])
        self.assertEqual(library_archives(self.library), [])
        # Nothing was set aside, so once the site answers properly Go saves everything.
        repaired()
        self.start()
        state = self.drive(stop=waited)
        self.assertEqual(state['status'], 'finished', self.last_response)
        self.assertEqual(state['audit']['recorded_archived_episode_count'], 7)
        self.assertEqual(verify(self.library, decode_images=True), [])

    def controller_tasks(self):
        import sqlite3
        with sqlite3.connect((self.root / 'download.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute('SELECT outcome FROM tasks')]

    def test_empty_account_to_mixed_portable_library_across_many_steps_then_a_second_round_saves_nothing_again(self):
        self.start()
        # One episode a step, so the account takes several steps that must add up without loss or repetition.
        with patch.object(download_control, 'DOWNLOAD', download_control.DOWNLOAD | {'limit': 1}):
            state = self.drive()
        self.assertEqual(state['status'], 'finished', self.last_response)
        self.assertEqual(state['audit']['recorded_archived_episode_count'], 3)
        self.assertEqual(state['audit']['missing_recorded_accessible_episode_count'], 0)
        self.assertGreaterEqual(self.executed.count('archive_discovered'), 3)
        self.assertEqual(verify(self.library, decode_images=True), [])
        restored = restore(self.library, self.project / 'portable-restore', decode=True)
        self.assertEqual(restored['archive_count'], 3)
        self.assertEqual(restored['novel_chapter_count'], 1)
        self.assertEqual(restored['image_count'], 5)
        original = {str(p): (p.read_bytes(), sidecar_path(p).read_bytes()) for p in library_archives(self.library)}
        images = self.images()
        self.start()  # Go after a finished round: the lists are read again; nothing saved is fetched again.
        self.assertEqual(self.drive()['status'], 'finished')
        self.assertEqual({str(p): (p.read_bytes(), sidecar_path(p).read_bytes()) for p in library_archives(self.library)}, original)
        self.assertEqual(self.images(), images)

    def test_wuf_only_comic_and_novel_with_missing_headers_are_discovered_and_published(self):
        from comic_archive.errors import MetadataError
        transport = self.adapter.transport
        normal_get = transport.get
        missing = {'/series/42': 404, '/series/43': 410}
        def get(url, **options):
            path = urlsplit(url).path
            if path in missing:
                transport.calls.append((urlsplit(url).hostname, path))
                error = MetadataError('Missing series header')
                error.source_http_status = missing[path]
                raise error
            return normal_get(url, **options)
        transport.get = get
        categories = []
        normal_list = self.adapter.inspect_library
        def library(category):
            categories.append(category)
            result = normal_list(category)
            if category != 'wait_until_free':
                result['series'] = []
            return result
        self.adapter.inspect_library = library
        self.start()
        state = self.drive()
        self.assertEqual(state['status'], 'finished', self.last_response)
        self.assertEqual(categories, list(self.adapter.site.lists))
        self.assertEqual(state['audit']['recorded_archived_episode_count'], 3)
        self.assertEqual({path.suffix for path in library_archives(self.library)}, {'.cbz', '.epub'})
        self.assertEqual(verify(self.library, decode_images=True), [])
        self.assertFalse(any(path in ('/episode/83', '/episode/92') for _, path in transport.calls))
        # A second round retains existing archives rather than duplicating or fetching them again.
        before = {str(path): path.read_bytes() for path in library_archives(self.library)}
        images = self.images()
        self.start()
        self.assertEqual(self.drive()['status'], 'finished', self.last_response)
        self.assertEqual({str(path): path.read_bytes() for path in library_archives(self.library)}, before)
        self.assertEqual(self.images(), images)

    def test_unavailable_series_is_held_while_other_series_still_publish(self):
        from comic_archive.errors import MetadataError
        transport = self.adapter.transport
        normal_get, normal_list = transport.get, self.adapter.inspect_library
        def get(url, **options):
            if urlsplit(url).path in ('/series/44', '/series/44/episodes'):
                error = MetadataError('Missing metadata')
                error.source_http_status = 404
                raise error
            return normal_get(url, **options)
        def library(category):
            result = normal_list(category)
            if category == 'wait_until_free':
                result['series'].append({'source_series_id': '44', 'title': 'Unavailable'})
            return result
        transport.get, self.adapter.inspect_library = get, library
        self.start()
        state = self.drive()
        self.assertEqual(state['status'], 'held', self.last_response)
        self.assertEqual(state['audit']['recorded_archived_episode_count'], 3)
        with Inventory(self.library, read_only=True) as inventory:
            self.assertEqual([row['source_series_id'] for row in inventory.scan_gaps()], ['44'])
        self.assertEqual({path.suffix for path in library_archives(self.library)}, {'.cbz', '.epub'})

    def test_restart_after_publication_observes_receipt_without_repeating_assets(self):
        self.start()
        self.drive(crash_after_archive_receipt=True)
        completed = list((self.root / 'requests').glob('*.json'))
        self.assertEqual(len(completed), 1)
        receipt = self.root / 'responses' / completed[0].name
        retained = receipt.read_bytes()
        previous_calls = list(self.adapter.transport.calls)
        self.adapter = OfflineAdapter()
        self.assertEqual(self.drive()['status'], 'finished')
        self.assertEqual(self.images(previous_calls + self.adapter.transport.calls), 4)
        self.assertEqual(receipt.read_bytes(), retained)

    def test_login_failure_never_retries_until_go_after_signing_in(self):
        self.adapter.transport.account = None
        self.start()
        held = self.drive()
        self.assertEqual((held['status'], held['code']), ('held', 'login_required'))
        receipt = self.root / 'responses' / (self.last_task + '.json')
        original = receipt.read_bytes()
        calls = list(self.adapter.transport.calls)
        self.adapter.transport.account = 11
        for _ in range(4):
            download_control.worker_tick(self.session, self.library)
        self.assertEqual(self.adapter.transport.calls, calls)
        self.assertFalse(list((self.root / 'requests').glob('*.json')))
        self.start()
        self.assertEqual(self.drive()['status'], 'finished')
        self.assertEqual(receipt.read_bytes(), original)

    def test_changed_account_stops_before_any_reader_or_image(self):
        with Inventory(self.library) as inventory:
            inventory.bind_account('11')
        self.adapter.transport.account = 22
        self.start()
        self.assertEqual(self.drive()['status'], 'held')
        self.assertEqual(self.adapter.transport.calls, [('tapas.io', '/who-am-i')])
        self.assertEqual(library_archives(self.library), [])

    def test_revoked_access_after_discovery_does_not_fetch_the_reader(self):
        def found(state):
            with Inventory(self.library, read_only=True) as inventory:
                return '82' in [row['id'] for row in inventory.pending()]
        self.start()
        self.drive(stop=found)
        self.adapter.transport.revoked.add('82')
        state = self.drive()
        self.assertEqual(state['status'], 'finished')
        self.assertEqual(state['audit']['recorded_archived_episode_count'], 2)
        self.assertEqual(self.adapter.transport.calls.count(('tapas.io', '/episode/82')), 1)
        self.assertEqual(self.images(), 2)

    def test_corrupt_saved_archive_prevents_completion_until_repaired(self):
        self.start()
        self.drive(stop=lambda state: state.get('phase') == 'verify')
        path = library_archives(self.library)[0]
        original = path.read_bytes()
        path.write_bytes(b'corrupt archive')
        completed = self.drive()
        self.assertEqual(completed['status'], 'finished')
        self.assertNotIn('verified_inventory_episode_count', completed['audit'])
        self.assertTrue(verify(self.library, decode_images=True), 'Explicit verification must detect the damaged file')
        calls = list(self.adapter.transport.calls)
        path.write_bytes(original)
        self.start()
        self.assertEqual(self.drive()['status'], 'finished')
        self.assertEqual(self.images(), self.images(calls))
        self.assertEqual(verify(self.library, decode_images=True), [])


if __name__ == '__main__':
    unittest.main()
