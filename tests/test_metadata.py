from comic_archive.archive import sidecar_path
from comic_archive.publication import import_metadata,PublicationStore as Store,validate_public_metadata
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock,patch

from comic_archive.archive import digest,verify,verify_series
from comic_archive.errors import MetadataError,AuthenticationError,RunLimit
from comic_archive.metadata import enrich_series
from comic_archive.models import Episode,Manifest,Page,Series
from comic_archive.inventory import Inventory
from comic_archive.jobs import archive_discovered,archive_watched_series
from test_archive import png
from test_jobs import AccountAdapter


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.work=tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root=Path(self.work.name)/'Library'
        self.series=Series('old-slug','Series',platform_id='42',slug='old-slug')
        self.episode=Episode('81','old-slug','Episode',1,(Page(1,'https://us-a.tapas.io/pc/1.jpg',False),),'tapas_free')
        self.adapter=Mock()
        self.adapter.account_identity.return_value='11'
        self.adapter.retrieve.return_value=png(1)
        self.adapter.retrieve_cover.return_value=png(2)
        self.enriched=replace(self.series,creators=('Artist',),description='A synopsis.',credits='Translated by Translator',
                              source_url='https://tapas.io/series/old-slug/info',
                              cover_url='https://us-a.tapas.io/cover/page.png?signature=PRIVATE')
        self.adapter.series_metadata.return_value=self.enriched
        self.store=Store(self.root)
        self.addCleanup(self.store.close)
        self.folder=self.store.archive(Manifest(self.series,(self.episode,)),self.adapter,progress=lambda _:None)
        self.cbz=next(self.folder.glob('*.cbz'))

    def test_cover_originals_and_credits_preserved_without_rewriting_episode_archive(self):
        before=self.cbz.read_bytes()
        episode_metadata=sidecar_path(self.cbz).read_bytes()
        result=enrich_series(self.adapter,'42',self.root)
        metadata=import_metadata(self.folder/'series.json')
        self.assertEqual(metadata['description'],'A synopsis.')
        self.assertEqual(metadata['creators'],['Artist'])
        self.assertEqual(metadata['credits'],'Translated by Translator')
        self.assertEqual((self.folder/'cover.png').read_bytes(),png(2))
        self.assertEqual(metadata['cover_sha256'],digest(png(2)))
        self.assertEqual(metadata['cover_url'],'https://us-a.tapas.io/cover/page.png')
        self.assertNotIn('PRIVATE',json.dumps(metadata))
        self.assertNotIn('PRIVATE',json.dumps(result))
        self.assertNotIn('PRIVATE',repr(self.enriched))
        self.assertEqual(self.cbz.read_bytes(),before)
        self.assertEqual(sidecar_path(self.cbz).read_bytes(),episode_metadata)
        self.store.archive(Manifest(self.series,(self.episode,)),self.adapter,progress=lambda _:None)
        retained=import_metadata(self.folder/'series.json')
        self.assertEqual(retained['credits'],metadata['credits'])
        self.assertEqual(retained['cover_sha256'],metadata['cover_sha256'])
        self.assertEqual(verify(self.root),[])

    def test_cache_reuses_cover_across_signature_changes_and_repairs_output(self):
        enrich_series(self.adapter,'42',self.root)
        (self.folder/'cover.png').write_bytes(b'corrupt')
        self.assertTrue(verify_series(self.folder/'series.json'))
        self.assertFalse(self.store.metadata_ready(self.series))
        self.adapter.series_metadata.return_value=replace(self.enriched,cover_url='https://us-a.tapas.io/cover/page.png?signature=NEW_PRIVATE')
        enrich_series(self.adapter,'42',self.root)
        self.adapter.retrieve_cover.assert_called_once()
        self.assertTrue(self.store.metadata_ready(self.series))
        self.assertEqual(verify(self.root),[])

    def test_source_numeric_identity_keeps_paths_when_slug_changes(self):
        renamed=replace(self.series,id='new-slug',slug='new-slug',title='Renamed series')
        episode=replace(self.episode,series_id='new-slug')
        folder=self.store.archive(Manifest(renamed,(episode,)),self.adapter,progress=lambda _:None)
        self.assertEqual(folder,self.folder)
        self.assertEqual(list(self.root.glob('*/*.cbz')),[self.cbz])
        self.assertEqual(import_metadata(folder/'series.json')['episode_count'],1)
        self.assertEqual(verify(self.root),[])

    def test_wrong_identity_does_not_retrieve_or_publish_cover(self):
        self.adapter.series_metadata.return_value=replace(self.enriched,platform_id='43')
        with self.assertRaises(MetadataError):
            enrich_series(self.adapter,'42',self.root)
        self.adapter.retrieve_cover.assert_not_called()
        self.assertFalse((self.folder/'cover.png').exists())

    def test_standalone_enrichment_keeps_neutral_public_metadata_and_original_episode_bytes(self):
        archive_before=self.cbz.read_bytes()
        sidecar_before=sidecar_path(self.cbz).read_bytes()
        identity_before=json.loads((self.folder/'series.json').read_text())['export_id']
        enrich_series(self.adapter,'42',self.root)
        public=json.loads((self.folder/'series.json').read_text())
        validate_public_metadata(public,series=True)
        self.assertEqual(public['export_id'],identity_before)
        self.assertEqual(public['episode_count'],1)
        self.assertEqual(public['creators'],['Artist'])
        for key in ('source','source_series_id','source_url','cover_url','description','credits','access_state'):
            self.assertNotIn(key,public)
        self.assertNotIn('tapas',(self.folder/'series.json').read_text().lower())
        private=import_metadata(self.folder/'series.json')
        self.assertEqual(private['source_series_id'],'42')
        self.assertEqual(private['description'],'A synopsis.')
        self.assertEqual(private['credits'],'Translated by Translator')
        self.assertEqual(self.cbz.read_bytes(),archive_before)
        self.assertEqual(sidecar_path(self.cbz).read_bytes(),sidecar_before)
        self.assertEqual(list(self.root.glob('*/*.tar.zst')),[])
        self.assertEqual(verify(self.root),[])

    def test_expired_cover_address_is_replaced_once_from_fresh_series_metadata(self):
        from comic_archive.transport import ExpiredAsset
        fresh=replace(self.enriched,cover_url='https://us-a.tapas.io/cover/page.png?signature=FRESH_PRIVATE')
        self.adapter.series_metadata.side_effect=[self.enriched,fresh]
        self.adapter.retrieve_cover.side_effect=[ExpiredAsset('Expired'),png(2)]
        result=enrich_series(self.adapter,'42',self.root)
        self.assertEqual(self.adapter.series_metadata.call_count,2)
        self.assertEqual([call.args[0].cover_url for call in self.adapter.retrieve_cover.call_args_list],
                         [self.enriched.cover_url,fresh.cover_url])
        self.assertTrue(result['cover_archived'])
        self.assertTrue(self.store.metadata_ready(self.series))
        self.assertEqual(verify(self.root),[])
        self.assertNotIn('PRIVATE',(self.folder/'series.json').read_text())

    def test_refreshed_series_metadata_for_another_identity_is_refused_without_a_cover(self):
        from comic_archive.transport import ExpiredAsset
        self.adapter.series_metadata.side_effect=[self.enriched,replace(self.enriched,platform_id='43')]
        self.adapter.retrieve_cover.side_effect=ExpiredAsset('Expired')
        with self.assertRaises(MetadataError):
            enrich_series(self.adapter,'42',self.root)
        self.assertEqual(self.adapter.retrieve_cover.call_count,1)
        self.assertFalse((self.folder/'cover.png').exists())
        self.assertFalse(self.store.metadata_ready(self.series))


    def test_cover_filename_cannot_escape_series_folder(self):
        metadata=json.loads((self.folder/'series.json').read_text())
        metadata['cover_file']='../outside.png'
        (self.folder/'series.json').write_text(json.dumps(metadata))
        self.assertEqual(verify_series(self.folder/'series.json'),['Invalid cover filename'])


class MetadataJobTests(unittest.TestCase):
    def setUp(self):
        self.work=tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root=Path(self.work.name)/'Library'
        self.root.mkdir()
        self.adapter=AccountAdapter()
        self.adapter.supports_series_metadata=True
        self.adapter.series_metadata=lambda key:replace(self.adapter.series,creators=('Artist',),
            description='A synopsis.',cover_url='https://us-a.tapas.io/cover/page.png')
        def cover(series,max_bytes=None):
            data=png(3)
            if max_bytes is not None and len(data)>max_bytes:
                raise RunLimit('Synthetic byte limit')
            return data
        self.adapter.retrieve_cover=cover
        self.report=self.root/'discovery.json'
        self.report.write_text(json.dumps({'source':'tapas','schema_version':1,
            'document_type':'comic_access_discovery','access_snapshot_only':True,'series':[{
            'source_series_id':'42','title':'S','checked_at':'2026-10-01T00:00:00+00:00',
            'accessible_episodes':[{'id':'81','title':'E81','order':1,'access_state':'tapas_free'}]}]}))

    def saved_series(self):
        self.adapter.access={'81':'tapas_free'}
        archive_discovered(self.adapter,self.root,self.report)
        self.adapter.calls.clear();self.adapter.downloads.clear()
        cbz=next(self.root.glob('*/*.cbz'))
        self.original={cbz:cbz.read_bytes(),sidecar_path(cbz):sidecar_path(cbz).read_bytes()}
        return cbz.parent

    def assert_episodes_reused(self):
        self.assertEqual(self.adapter.downloads,[])
        self.assertFalse(any(isinstance(call,tuple) and call[0]=='capture' for call in self.adapter.calls))
        for path,data in self.original.items():self.assertEqual(path.read_bytes(),data)

    @patch('builtins.print')
    def test_completed_selected_series_updates_details_and_repairs_cover_from_cache(self,output):
        folder=self.saved_series();(folder/'cover.png').unlink()
        self.adapter.series_metadata=Mock(return_value=replace(self.adapter.series,creators=('Artist',),
            description='An updated synopsis.',cover_url='https://us-a.tapas.io/cover/page.png'))
        self.adapter.retrieve_cover=Mock(side_effect=AssertionError('Verified cover cache should be reused'))
        result=archive_watched_series(self.adapter,'42',self.root,max_pages=1,max_bytes=1,max_seconds=60)
        self.assertEqual(result['status'],'finished');self.assertEqual(result['archived_episode_ids'],[])
        self.assertEqual(result['enriched_series_ids'],['42'])
        self.assertEqual((result['downloaded_pages'],result['downloaded_bytes'],result['downloaded_covers']),(0,0,0))
        self.adapter.series_metadata.assert_called_once_with('42');self.adapter.retrieve_cover.assert_not_called()
        self.assertEqual(import_metadata(folder/'series.json')['description'],'An updated synopsis.')
        self.assertEqual((folder/'cover.png').read_bytes(),png(3));self.assert_episodes_reused()
        self.assertEqual(verify(self.root),[])

    @patch('builtins.print')
    def test_completed_series_new_cover_obeys_byte_budget_and_is_accounted(self,output):
        self.saved_series()
        self.adapter.series_metadata=lambda key:replace(self.adapter.series,
            cover_url='https://us-a.tapas.io/cover/new.png')
        limited=archive_watched_series(self.adapter,'42',self.root,max_bytes=len(png(3))-1)
        self.assertEqual(limited['status'],'limited');self.assertEqual(limited['downloaded_bytes'],0)
        self.assertEqual(limited['enriched_series_ids'],[]);self.assert_episodes_reused()
        completed=archive_watched_series(self.adapter,'42',self.root,max_bytes=len(png(3)))
        self.assertEqual(completed['status'],'finished');self.assertEqual(completed['downloaded_bytes'],len(png(3)))
        self.assertEqual(completed['downloaded_covers'],1);self.assertEqual(completed['downloaded_pages'],0)
        self.assertEqual(completed['enriched_series_ids'],['42']);self.assert_episodes_reused()
        self.assertEqual(verify(self.root),[])

    @patch('builtins.print')
    def test_completed_series_metadata_failure_reports_safe_retry_and_retains_queue(self,output):
        self.saved_series()
        self.adapter.series_metadata=Mock(side_effect=MetadataError('PRIVATE_COOKIE_SOURCE_URL'))
        result=archive_watched_series(self.adapter,'42',self.root)
        self.assertEqual(result['metadata_unavailable_series_count'],1)
        self.assertEqual(result['metadata_unavailable_series'],[{'series_id':'42','code':'MetadataError'}])
        self.assertNotIn('PRIVATE',json.dumps(result));self.assertEqual(result['pending_episode_count'],0)
        self.assert_episodes_reused();self.assertEqual(verify(self.root),[])

    @patch('builtins.print')
    def test_completed_series_gateway_failure_pauses_without_requeuing_saved_episode(self,output):
        from comic_archive.errors import NetworkUnavailable
        self.saved_series();failure=NetworkUnavailable('PRIVATE_SOURCE_RESPONSE');failure.source_http_status=503
        self.adapter.series_metadata=Mock(side_effect=failure)
        result=archive_watched_series(self.adapter,'42',self.root)
        self.assertEqual(result['status'],'paused');self.assertEqual(result['source_http_status'],503)
        self.assertNotIn('PRIVATE',json.dumps(result));self.assert_episodes_reused()
        with Inventory(self.root,read_only=True) as inventory:
            self.assertEqual(inventory.status()['episodes_by_status'],{'archived':1})

    @patch('builtins.print')
    def test_completed_series_changed_account_stops_before_series_details(self,output):
        self.saved_series();self.adapter.identity='different-account'
        self.adapter.series_metadata=Mock()
        with self.assertRaises(AuthenticationError):archive_watched_series(self.adapter,'42',self.root)
        self.assertEqual(self.adapter.calls,['identity']);self.adapter.series_metadata.assert_not_called()
        self.assert_episodes_reused()

    @patch('builtins.print')
    def test_completed_series_stop_during_metadata_retains_details_and_comic_bytes(self,output):
        from comic_archive.cancellation import request_stop
        self.saved_series()
        def details(key):
            with Inventory(self.root,read_only=True) as inventory:run_id=inventory.status()['last_run']['id']
            request_stop(self.root,run_id)
            return replace(self.adapter.series,description='Fresh details')
        self.adapter.series_metadata=details
        result=archive_watched_series(self.adapter,'42',self.root)
        self.assertEqual(result['status'],'cancelled');self.assertEqual(result['enriched_series_ids'],['42'])
        self.assert_episodes_reused();self.assertEqual(verify(self.root),[])

    @patch('builtins.print')
    def test_unavailable_series_without_saved_comics_does_not_start_metadata_repair(self,output):
        self.adapter.access={'81':'unavailable_or_unverified'}
        with Inventory(self.root) as inventory:inventory.observe('42','S',[],'fixture')
        self.adapter.series_metadata=Mock();self.adapter.retrieve_cover=Mock()
        result=archive_watched_series(self.adapter,'42',self.root)
        self.assertEqual(result['status'],'finished');self.assertEqual(result['enriched_series_ids'],[])
        self.adapter.series_metadata.assert_not_called();self.adapter.retrieve_cover.assert_not_called()
        self.assertEqual(self.adapter.downloads,[]);self.assertEqual(list(self.root.glob('*/*.cbz')),[])

    @patch('builtins.print')
    def test_job_accounts_for_cover_bytes_and_reports_metadata(self,output):
        result=archive_discovered(self.adapter,self.root,self.report)
        self.assertEqual(result['archived_episode_ids'],['81'])
        self.assertEqual(result['enriched_series_ids'],['42'])
        self.assertEqual(result['downloaded_pages'],2)
        self.assertEqual(result['downloaded_covers'],1)
        self.assertEqual(result['downloaded_bytes'],sum(len(png(i)) for i in (1,2,3)))
        self.assertEqual(verify(self.root),[])

    @patch('builtins.print')
    def test_optional_metadata_failure_keeps_successful_archive(self,output):
        def unavailable(key): raise MetadataError('PRIVATE metadata details')
        self.adapter.series_metadata=unavailable
        result=archive_discovered(self.adapter,self.root,self.report)
        self.assertEqual(result['archived_episode_ids'],['81'])
        self.assertEqual(result['inventory']['episodes_by_status'],{'archived':1})
        self.assertEqual(result['metadata_unavailable_series'],[{'series_id':'42','code':'MetadataError'}])
        self.assertNotIn('PRIVATE',json.dumps(result))

    @patch('builtins.print')
    def test_cover_limit_keeps_completed_episode_marked_archived(self,output):
        result=archive_discovered(self.adapter,self.root,self.report,max_bytes=len(png(1))+len(png(2)))
        self.assertEqual(result['status'],'limited')
        self.assertEqual(result['archived_episode_ids'],['81'])
        self.assertEqual(result['inventory']['episodes_by_status'],{'archived':1})
        self.assertEqual(result['downloaded_covers'],0)
        self.assertEqual(verify(self.root),[])

    @patch('builtins.print')
    def test_login_expiry_after_archive_preserves_it_and_stops_metadata(self,output):
        def expired(key): raise AuthenticationError('Synthetic login required')
        self.adapter.series_metadata=expired
        with self.assertRaises(AuthenticationError):
            archive_discovered(self.adapter,self.root,self.report)
        from comic_archive.inventory import Inventory
        with Inventory(self.root,read_only=True) as inventory:
            self.assertEqual(inventory.status()['episodes_by_status'],{'archived':1})
            self.assertEqual(inventory.status()['last_run']['status'],'login_required')
