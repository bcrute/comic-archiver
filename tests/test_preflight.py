import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from comic_archive.cli import parser, run
from comic_archive.errors import AuthenticationError
from comic_archive.inventory import Inventory
from comic_archive.models import ArchiveError
from comic_archive.network import NetworkGuard, NetworkPolicyHold, configure
from comic_archive.preflight import preview_series, public_preview
from comic_archive.queue_client import submit_inspection
from comic_archive.storage import NOVEL_BYTES, storage_plan
from comic_archive.worker import execute
from test_jobs import AccountAdapter


class PreviewAdapter(AccountAdapter):
    def __init__(self, novel=False):
        super().__init__()
        self.novel = novel
        self.deadline = None
        self.expected_account_id = None

    def inspect_series(self, key, strict_order=False):
        self.bound_identity_during_inspection = self.expected_account_id
        self.deadline_during_inspection = self.deadline
        result = super().inspect_series('42', strict_order=strict_order)
        result.update(episode_count=len(result['episodes']), series_content_flags={'book':self.novel, 'comic':not self.novel})
        return result


class PreflightTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)/'Library'
        self.url = 'https://tapas.io/series/sample/info'

    def test_new_series_estimate_is_metadata_only_readonly_and_novel_sized(self):
        adapter = PreviewAdapter(novel=True)
        result = preview_series(adapter, self.url, self.root)['storage_preview']
        self.assertEqual(result['planned_episode_count'], 2)
        self.assertEqual(result['content_type'], 'novel')
        self.assertEqual(result['estimated_additional_archive_bytes'], 2*NOVEL_BYTES)
        self.assertEqual(adapter.bound_identity_during_inspection, '11')
        self.assertGreater(adapter.deadline_during_inspection, time.monotonic())
        self.assertLess(adapter.deadline_during_inspection, time.monotonic()+301)
        self.assertIsNone(adapter.expected_account_id); self.assertIsNone(adapter.deadline)
        self.assertEqual(adapter.downloads, []); self.assertEqual(adapter.calls, ['identity', ('series', '42')])
        self.assertFalse(self.root.exists()); self.assertNotIn('source_series_id', result)
        one = preview_series(adapter, self.url, self.root, scope='one')['storage_preview']
        self.assertEqual(one['planned_episode_count'], 1)

    def test_saved_candidates_are_subtracted_without_adding_queue_or_watchlist_rows(self):
        with Inventory(self.root) as inventory:
            inventory.bind_account('11')
            inventory.observe('42', 'Synthetic', [{'id':'81','title':'E','order':1,'access_state':'tapas_free'}], 'fixture')
            inventory.mark('81', 'archived', path=self.root/'Existing.cbz')
            before = list(inventory.db.execute('SELECT id,status,archive_path FROM episodes'))
        result = preview_series(PreviewAdapter(), self.url, self.root)['storage_preview']
        self.assertEqual(result['already_saved_episode_count'], 1)
        self.assertEqual(result['planned_episode_count'], 1)
        with Inventory(self.root, read_only=True) as inventory:
            self.assertEqual(list(inventory.db.execute('SELECT id,status,archive_path FROM episodes')), before)
            self.assertEqual(len(inventory.watched_series()), 1)

    def test_account_change_incomplete_source_and_policy_hold_never_start_an_import(self):
        with Inventory(self.root) as inventory: inventory.bind_account('11')
        adapter = PreviewAdapter(); adapter.identity = '12'
        with self.assertRaises(AuthenticationError): preview_series(adapter, self.url, self.root)
        self.assertEqual(adapter.calls, ['identity']); self.assertEqual(adapter.downloads, [])
        adapter = PreviewAdapter()
        with patch.object(adapter, 'inspect_series', return_value={'authenticated':True, 'complete':False}), self.assertRaises(ArchiveError):
            preview_series(adapter, self.url, self.root)
        self.assertIsNone(adapter.expected_account_id); self.assertIsNone(adapter.deadline)
        adapter = PreviewAdapter(); adapter.network_guard = NetworkGuard(self.root/'Other')
        original = adapter.account_identity
        def identity(): adapter.network_guard.check(); return original()
        adapter.account_identity = identity
        with patch('comic_archive.network.probe', return_value={'connection':'cellular','metered':True}):
            with self.assertRaises(NetworkPolicyHold): preview_series(adapter, self.url, self.root)
            configure(self.root, False)
            self.assertEqual(preview_series(adapter, self.url, self.root)['storage_preview']['planned_episode_count'], 2)
        self.assertEqual(adapter.downloads, [])

    def test_invalid_queue_and_cli_options_are_refused_before_source_or_browser(self):
        queue=self.root/'worker/requests'; queue.mkdir(parents=True)
        for options in ({'url':'https://evil.test/series/sample','scope':'one'}, {'url':self.url,'scope':[]},
                {'url':self.url,'scope':'series','password':'PRIVATE'}, {'url':self.url,'scope':'series','output':'/other'}):
            with self.assertRaises(ArchiveError):submit_inspection(self.root,'preview_series',options)
            with self.assertRaises(ArchiveError):execute(PreviewAdapter(),{'operation':'preview_series','args':options})
        self.assertEqual(list(queue.iterdir()),[])
        with patch('comic_archive.adapters.tapas.TapasAdapter') as browser, self.assertRaises(ArchiveError):
            run(parser().parse_args(['preview-series','https://evil.test/series/sample']))
        browser.assert_not_called()

    def test_public_preview_omits_prose_credentials_paths_and_invalid_metrics(self):
        value=public_preview({'planned_episode_count':True,'free_bytes':-1,'content_type':[],
            'fits_estimate':'PRIVATE','cookie':'PRIVATE','title':'PRIVATE','path':'PRIVATE',
            'accessible_episode_count':2,'reader_or_comic_pages_requested':False,'scope':'one'})
        self.assertEqual(value,{'accessible_episode_count':2,'reader_or_comic_pages_requested':False,'scope':'one'})
        self.assertEqual(storage_plan(self.root,episode_count=3,episode_kind='novel')['estimated_additional_archive_bytes'],3*NOVEL_BYTES)
        for kind in (None, [], 'other'):
            with self.assertRaises(ArchiveError):storage_plan(self.root,episode_count=1,episode_kind=kind)

    def test_cli_and_worker_use_the_same_fixed_preview_contract(self):
        adapter=PreviewAdapter();context=Mock();context.__enter__=Mock(return_value=adapter);context.__exit__=Mock(return_value=False)
        with patch('comic_archive.adapters.tapas.TapasAdapter',return_value=context), patch('builtins.print') as output:
            self.assertEqual(run(parser().parse_args(['preview-series',self.url,'--output',str(self.root),'--scope','one'])),0)
        value=json.loads(output.call_args.args[0])['storage_preview']
        self.assertEqual(value['planned_episode_count'],1);self.assertFalse(self.root.exists())
        with patch('comic_archive.preflight.preview_series',return_value={'storage_preview':value}) as preview:
            self.assertEqual(execute(adapter,{'operation':'preview_series','args':{'url':self.url,'scope':'one'}}),{'storage_preview':value})
            preview.assert_called_once_with(adapter,self.url,Path('Library'),scope='one')

    def test_unknown_kind_is_conservative_and_invalid_classification_or_identity_is_held(self):
        adapter=PreviewAdapter();inspection=adapter.inspect_series('sample');inspection.pop('series_content_flags')
        with patch.object(adapter,'inspect_series',return_value=inspection):
            result=preview_series(adapter,self.url,self.root)['storage_preview']
            self.assertFalse(result['content_type_verified']);self.assertEqual(result['content_type'],'comic')
        for flags in ({'comic':True,'book':True},{'comic':1,'book':False},{},[]):
            with patch.object(adapter,'inspect_series',return_value={**inspection,'series_content_flags':flags}), self.assertRaises(ArchiveError):
                preview_series(adapter,self.url,self.root)
        adapter.identity='PRIVATE'
        with self.assertRaises(AuthenticationError):preview_series(adapter,self.url,self.root)
        self.assertFalse(self.root.exists());self.assertEqual(adapter.downloads,[])
