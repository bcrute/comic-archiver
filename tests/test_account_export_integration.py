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

from comic_archive import account_export, completion
from comic_archive.adapters.tapas_http import TapasHTTPAdapter
from comic_archive.archive import episode_archives, sidecar_path, verify
from comic_archive.inventory import Inventory
from comic_archive.errors import AuthenticationError
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
                if key in ('83', '92') or key in self.revoked:
                    raise AssertionError('Denied reader requested')
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


class AccountExportIntegrationTests(unittest.TestCase):
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
        account_export.configure(self.session, self.library, window_budgets={
            'max_batches': 1, 'max_pages': 2, 'batch_pages': 2, 'batch_limit': 1,
            'max_bytes': 1024**2, 'batch_bytes': 1024**2, 'max_seconds': 60,
            'batch_seconds': 10, 'recover_network': False, 'recover_source': False})

    def drive(self, *, stop_before_audit=False, stop_after_lists=False, crash_after_archive_receipt=False):
        for _ in range(120):
            account_export.worker_tick(self.session, self.library)
            completion.worker_tick(self.session, self.library)
            state = account_export.status(self.session, self.library)
            if state['status'] in ('held', 'finished'):
                return state
            if stop_before_audit and state['phase'] == 'verify':
                return state
            if stop_after_lists and state['phase'] == 'series':
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
                self.last_response = response
                self.executed.append(request['operation'])
                if (crash_after_archive_receipt and request['operation'] == 'archive_discovered'
                        and response.get('result', {}).get('archived_episode_ids')):
                    return state  # Simulate death before deleting the completed request.
                path.unlink()
        self.fail('Account workflow did not converge: ' + repr({'account': state, 'completion': completion.status(self.session, self.library), 'last_response': self.last_response}))

    def go(self, speed):
        """The page's Go at one of its speeds, driven to the end of the whole account."""
        from comic_archive.local_library import LOCAL_OWNER, LocalWorkspaceRegistry
        (self.root/'account-export.json').unlink()
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
        self.assertEqual(registry.account_status(LOCAL_OWNER,registry.workspace_id)['audit']['verified_inventory_episode_count'],3)
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

    def test_empty_account_to_mixed_portable_library_across_multiple_windows(self):
        state = self.drive()
        self.assertEqual(state['status'], 'finished', self.last_response)
        self.assertEqual(state['audit']['verified_inventory_episode_count'], 3)
        self.assertEqual(state['audit']['missing_recorded_accessible_episode_count'], 0)
        self.assertFalse(state['account_inventory_complete'])
        # The time budget may end a window early on a busy remote builder.
        # Require the three bounded windows needed for this fixture, while
        # allowing extra windows; archive counts and restore checks prove that
        # they converge without duplicating or losing episodes.
        self.assertGreaterEqual(completion.status(self.session, self.library)['window_count'], 3)
        self.assertEqual(verify(self.library, decode_images=True), [])
        restored = restore(self.library, self.project / 'portable-restore', decode=True)
        self.assertEqual(restored['archive_count'], 3)
        self.assertEqual(restored['novel_chapter_count'], 1)
        self.assertEqual(restored['image_count'], 5)
        original = {str(p): (p.read_bytes(), sidecar_path(p).read_bytes()) for p in library_archives(self.library)}
        requests = len([p for host, p in self.adapter.transport.calls if host == 'us-a.tapas.io'])
        account_export.configure(self.session, self.library)
        self.assertEqual(self.drive()['status'], 'finished')
        self.assertEqual({str(p): (p.read_bytes(), sidecar_path(p).read_bytes()) for p in library_archives(self.library)}, original)
        self.assertEqual(len([p for host, p in self.adapter.transport.calls if host == 'us-a.tapas.io']), requests)

    def test_restart_after_publication_observes_receipt_without_repeating_assets(self):
        self.drive(crash_after_archive_receipt=True)
        completed = list((self.root / 'requests').glob('*.json'))
        self.assertEqual(len(completed), 1)
        receipt = self.root / 'responses' / completed[0].name
        retained = receipt.read_bytes()
        previous_calls = list(self.adapter.transport.calls)
        self.adapter = OfflineAdapter()
        self.assertEqual(self.drive()['status'], 'finished')
        all_calls = previous_calls + self.adapter.transport.calls
        self.assertEqual(len([p for host, p in all_calls if host == 'us-a.tapas.io']), 4)
        self.assertEqual(receipt.read_bytes(), retained)

    def test_login_failure_never_retries_until_explicit_repaired_resume(self):
        self.adapter.transport.account = None
        held = self.drive()
        self.assertEqual(held['status'], 'held')
        task = held['pending_task_id']
        receipt = self.root / 'responses' / (task + '.json')
        original = receipt.read_bytes()
        calls = list(self.adapter.transport.calls)
        self.adapter.transport.account = 11
        for _ in range(4):
            account_export.worker_tick(self.session, self.library)
        self.assertEqual(self.adapter.transport.calls, calls)
        account_export.configure(self.session, self.library, resume=True)
        self.assertEqual(self.drive()['status'], 'finished')
        self.assertEqual(receipt.read_bytes(), original)

    def test_changed_account_stops_before_any_reader_or_image(self):
        with Inventory(self.library) as inventory:
            inventory.bind_account('11')
        self.adapter.transport.account = 22
        self.assertEqual(self.drive()['status'], 'held')
        self.assertEqual(self.adapter.transport.calls, [('tapas.io', '/who-am-i')])
        self.assertEqual(library_archives(self.library), [])

    def test_revoked_access_after_discovery_does_not_fetch_the_reader(self):
        self.drive(stop_after_lists=True)
        with Inventory(self.library, read_only=True) as inventory:
            self.assertIn('82', [row['id'] for row in inventory.pending()])
        self.adapter.transport.revoked.add('82')
        state = self.drive()
        self.assertEqual(state['status'], 'finished')
        self.assertEqual(state['audit']['verified_inventory_episode_count'], 2)
        self.assertNotIn(('tapas.io', '/episode/82'), self.adapter.transport.calls)
        self.assertEqual(len([p for host, p in self.adapter.transport.calls if host == 'us-a.tapas.io']), 2)

    def test_corrupt_saved_archive_prevents_completion_until_repaired(self):
        self.drive(stop_before_audit=True)
        path = library_archives(self.library)[0]
        original = path.read_bytes()
        path.write_bytes(b'corrupt archive')
        self.assertEqual(self.drive()['code'], 'archive_incomplete')
        calls = list(self.adapter.transport.calls)
        path.write_bytes(original)
        account_export.configure(self.session, self.library, resume=True)
        self.assertEqual(self.drive()['status'], 'finished')
        self.assertEqual(self.adapter.transport.calls, calls)
