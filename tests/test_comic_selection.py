"""Comic choices constrain real acquisition without deleting saved archives."""
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from comic_archive import account_ui, comic_selection as comics, download_selection, sites
from comic_archive.audit import check_completion
from comic_archive.episode_index import page
from comic_archive.inventory import Inventory
from comic_archive.jobs import archive_discovered, sync
from comic_archive.models import ArchiveError
from test_jobs import AccountAdapter


def episode(key='81', site=sites.DEFAULT):
    return {'id': key, 'title': 'Episode', 'order': 1, 'access_state': site.free}


class ComicSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / 'Library'

    def test_legacy_and_new_inventories_include_all_without_a_migration_write(self):
        with Inventory(self.root) as inventory:
            inventory.observe('42', 'Comic', [episode()], 'fixture')
            self.assertIsNone(inventory.db.execute('SELECT value FROM settings WHERE key=?', (comics.KEY,)).fetchone())
            self.assertEqual(comics.read(inventory.db), {'default': True, 'exceptions': []})
        before = (self.root / '.archive-state/inventory.sqlite3').read_bytes()
        with Inventory(self.root, read_only=True) as inventory:
            self.assertEqual([row['id'] for row in inventory.pending()], ['81'])
            self.assertIsNone(inventory.db.execute('SELECT value FROM settings WHERE key=?', (comics.KEY,)).fetchone())
        self.assertEqual((self.root / '.archive-state/inventory.sqlite3').read_bytes(), before)

    def test_desktop_actions_change_the_owned_library_and_refuse_another_root(self):
        from comic_archive.app_actions import Actions
        from comic_archive.local_library import LOCAL_OWNER, LocalWorkspaceRegistry
        registry = LocalWorkspaceRegistry(self.base)
        runtime = SimpleNamespace(registry=registry, root=self.base, site=sites.DEFAULT)
        actions = Actions(lambda: runtime)
        with Inventory(self.root) as inventory:
            inventory.observe('42', 'Comic', [episode()], 'fixture')
        self.assertEqual(set(actions.choose_comics(False)), {'tapas', 'webtoon', 'tappytoon'})
        self.assertEqual(actions.choose_comics(True, provider='tapas', series_id='42'), ['tapas'])
        self.assertTrue(actions.episode_inventory(provider='tapas')['items'][0]['selected'])
        self.assertFalse(actions.episode_inventory(provider='webtoon')['comic_selection']['default'])
        other = self.base / 'other'
        with self.assertRaises(ArchiveError):
            registry.choose_library_comics(LOCAL_OWNER, registry.workspace_id, other, True)
        self.assertFalse(other.exists())

    def test_a_partial_global_failure_keeps_saved_choices_readable_for_review(self):
        original = account_ui.change_comics
        visited = []
        def change(workspace, chosen, series_id=None, **options):
            visited.append(workspace.root)
            if len(visited) == 2:
                raise ArchiveError('The next provider is busy.')
            return original(workspace, chosen, series_id, **options)
        with patch.object(account_ui, 'change_comics', side_effect=change), self.assertRaises(ArchiveError):
            account_ui.change_library_comics(self.base, False, closing=threading.Event())
        self.assertEqual(len(visited), 2)
        with Inventory(visited[0] / 'Library', read_only=True) as inventory:
            self.assertFalse(comics.read(inventory.db)['default'])
        self.assertFalse((visited[1] / 'Library/.archive-state/inventory.sqlite3').exists())

    def test_individual_choices_and_future_discoveries_follow_the_saved_default(self):
        with Inventory(self.root) as inventory:
            inventory.observe('42', 'One', [episode()], 'fixture')
            inventory.observe('43', 'Two', [episode('82')], 'fixture')
            comics.change(inventory.db, False, '42')
            self.assertEqual([row['id'] for row in inventory.pending()], ['82'])
            inventory.observe('44', 'Later', [episode('83')], 'fixture')
            self.assertEqual({row['id'] for row in inventory.pending()}, {'82', '83'})
            comics.change(inventory.db, False)
            comics.change(inventory.db, True, '42')
            inventory.observe('45', 'Another later comic', [episode('84')], 'fixture')
            self.assertEqual([row['id'] for row in inventory.pending()], ['81'])
        with Inventory(self.root, read_only=True) as inventory:
            self.assertEqual(comics.read(inventory.db), {'default': False, 'exceptions': ['42']})
            self.assertEqual([row['id'] for row in inventory.pending()], ['81'])
        with Inventory(self.root) as inventory:
            comics.change(inventory.db, True)
            self.assertEqual(len(inventory.pending()), 4)

    def test_unselected_comic_never_reaches_reader_or_asset_downloads(self):
        adapter = AccountAdapter()
        with Inventory(self.root) as inventory:
            inventory.observe('42', 'One', [episode()], 'fixture')
            inventory.observe('43', 'Two', [episode('82')], 'fixture')
            comics.change(inventory.db, False, '43')
        result = archive_discovered(adapter, self.root)
        self.assertEqual(result['archived_episode_ids'], ['81'])
        self.assertNotIn(('capture', '82'), adapter.calls)
        self.assertEqual(adapter.downloads, [1, 2])
        self.assertTrue(check_completion(self.root)['completion_check_passed'])

    def test_unchecking_an_archived_comic_preserves_files_and_respects_manual_deletion(self):
        adapter = AccountAdapter()
        with Inventory(self.root) as inventory:
            inventory.observe('42', 'One', [episode()], 'fixture')
        archive_discovered(adapter, self.root)
        with Inventory(self.root) as inventory:
            relative = inventory.db.execute("SELECT archive_path FROM episodes WHERE id='81'").fetchone()[0]
            archive = self.root / relative
            original = archive.read_bytes()
            stamp = archive.stat().st_mtime_ns
            comics.change(inventory.db, False, '42')
        adapter.calls.clear()
        adapter.downloads.clear()
        archive_discovered(adapter, self.root)
        self.assertEqual((archive.read_bytes(), archive.stat().st_mtime_ns), (original, stamp))
        self.assertEqual(adapter.downloads, [])
        archive.unlink()  # Only the user deletes a saved comic.
        archive_discovered(adapter, self.root)
        self.assertFalse(archive.exists())
        self.assertNotIn(('capture', '81'), adapter.calls)
        with Inventory(self.root) as inventory:
            comics.change(inventory.db, True, '42')
        self.assertEqual(archive_discovered(adapter, self.root)['archived_episode_ids'], ['81'])
        self.assertEqual(archive.read_bytes(), original)

    def test_none_selected_discovers_titles_without_inspecting_comics_or_downloading(self):
        adapter = AccountAdapter()
        with Inventory(self.root) as inventory:
            comics.change(inventory.db, False)
        result = sync(adapter, self.root)
        self.assertEqual(result['archived_episode_ids'], [])
        self.assertTrue(page(self.root)['items'])
        self.assertTrue(all(not row['selected'] for row in page(self.root)['items']))
        self.assertFalse(any(isinstance(call, tuple) and call[0] in {'series', 'capture'} for call in adapter.calls))
        self.assertEqual(adapter.downloads, [])
        self.assertTrue(check_completion(self.root)['completion_check_passed'])

    def test_scan_limit_applies_after_selection_and_excluded_gaps_do_not_hold_completion(self):
        with Inventory(self.root) as inventory:
            for key in ('42', '43', '44'):
                inventory.observe(key, 'Comic', [], 'fixture')
            inventory.record_scan_gap('42', 1, 2)
            comics.change(inventory.db, False)
            comics.change(inventory.db, True, '44')
            self.assertEqual([row['id'] for row in inventory.scan_candidates(1)], ['44'])
            self.assertEqual(inventory.scan_gaps(), [])
            self.assertEqual(inventory.status()['scan_remaining_series_count'], 1)
            self.assertEqual(inventory.status()['selected_series_count'], 1)
        self.assertTrue(check_completion(self.root)['completion_check_passed'])

    def test_reselecting_a_previously_checked_comic_requires_a_new_scan(self):
        with Inventory(self.root) as inventory:
            inventory.observe('42', 'Comic', [], 'fixture')
            inventory.scan_candidates()
            inventory.record_series_scan('42')
            inventory.finish_scan_cycle()
            comics.change(inventory.db, False)
            self.assertEqual(inventory.status()['scan_remaining_series_count'], 0)
            comics.change(inventory.db, True, '42')
            self.assertEqual(inventory.status()['scan_remaining_series_count'], 1)
            self.assertEqual([row['id'] for row in inventory.scan_candidates()], ['42'])

    def test_access_filters_and_comic_choices_both_apply_for_all_providers(self):
        for site in sites.SITES:
            if not site.available:
                continue
            with self.subTest(provider=site.key), Inventory(self.base / site.key) as inventory:
                inventory.observe('42', 'Chosen', [episode(site=site)], 'fixture')
                inventory.observe('43', 'Excluded', [episode('82', site)], 'fixture')
                comics.change(inventory.db, False, '43')
                download_selection.save(inventory.db, dict(bought=True, free=False, limited=False, unclassified=False))
                self.assertEqual(inventory.pending(), [])
                download_selection.save(inventory.db, dict(bought=False, free=True, limited=False, unclassified=False))
                self.assertEqual([row['id'] for row in inventory.pending()], ['81'])

    def test_extras_backfill_is_excluded_but_previously_saved_extras_remain(self):
        from comic_archive import extras
        with Inventory(self.root) as inventory:
            inventory.observe('42', 'Comic', [episode()], 'fixture')
            inventory.mark('81', 'archived')
            extras.store(inventory.db, dict.fromkeys(extras.KINDS, True))
            self.assertEqual([row['id'] for row in inventory.pending()], ['81'])
            comics.change(inventory.db, False, '42')
            self.assertEqual(inventory.pending(), [])
            self.assertTrue(all(extras.read(inventory.db).values()))

    def test_corrupt_scope_holds_closed_and_check_all_can_repair_it(self):
        with Inventory(self.root) as inventory:
            inventory.observe('42', 'Comic', [episode()], 'fixture')
            for invalid in ('{private', json.dumps({'default': True, 'exceptions': ['1);DELETE']}),
                            json.dumps({'default': 1, 'exceptions': []}),
                            json.dumps({'default': False, 'exceptions': ['42', '42']})):
                inventory.db.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                                     (comics.KEY, invalid))
                inventory.db.commit()
                with self.assertRaises(ArchiveError):
                    inventory.pending()
            comics.change(inventory.db, True)
            self.assertEqual(len(inventory.pending()), 1)

    def test_invalid_requests_do_not_change_any_setting(self):
        with Inventory(self.root) as inventory:
            inventory.observe('42', 'Comic', [episode()], 'fixture')
            for chosen, key in ((1, None), (None, '42'), (False, 'missing'), (False, '999')):
                with self.subTest(chosen=chosen, key=key), self.assertRaises(ArchiveError):
                    comics.change(inventory.db, chosen, key)
                self.assertEqual(comics.read(inventory.db), {'default': True, 'exceptions': []})
            with self.assertRaises(ValueError):
                comics.clause(inventory.db, 's;DELETE')

    def test_paged_checkbox_choices_include_comics_beyond_the_first_page(self):
        with Inventory(self.root) as inventory:
            for number in range(1, 407):
                inventory.observe(str(number), f'Comic {number:04}', [], 'fixture')
            comics.change(inventory.db, False, '405')
        last = page(self.root, offset=400)
        self.assertEqual(len(last['items']), 6)
        self.assertEqual([row['id'] for row in last['items'] if not row['selected']], ['405'])
        self.assertIsNone(last['next_offset'])

    def test_global_choices_are_persisted_for_unopened_providers_without_starting_workers(self):
        account_ui.change_library_comics(self.base, False, closing=threading.Event())
        for site in sites.SITES:
            if not site.available:
                continue
            library = sites.folder(self.base, site) / 'Library'
            with Inventory(library, read_only=True) as inventory:
                self.assertEqual(comics.read(inventory.db), {'default': False, 'exceptions': []})
            self.assertFalse(list((library.parent / '.comic-session/worker/requests').glob('*.json')))
        account_ui.change_library_comics(self.base, True, 'webtoon', closing=threading.Event())
        with Inventory(sites.folder(self.base, sites.find('webtoon')) / 'Library', read_only=True) as inventory:
            self.assertTrue(comics.read(inventory.db)['default'])
        with Inventory(self.root, read_only=True) as inventory:
            self.assertFalse(comics.read(inventory.db)['default'])

    def test_checkbox_read_is_local_and_does_not_reset_a_downloading_episode(self):
        with Inventory(self.root) as inventory:
            inventory.observe('42', 'Comic', [episode()], 'fixture')
            comics.change(inventory.db, False, '42')
            inventory.mark('81', 'downloading')
        with patch('comic_archive.transport.Transport.get', side_effect=AssertionError('No network')):
            listing = page(self.root)
            self.assertFalse(listing['items'][0]['selected'])
        with Inventory(self.root, read_only=True) as inventory:
            self.assertEqual(inventory.db.execute("SELECT status FROM episodes WHERE id='81'").fetchone()[0], 'downloading')


if __name__ == '__main__':
    unittest.main()
