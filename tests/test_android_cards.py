"""The phone's site cards: the desktop's words over the phone's own records. Private fixtures only."""
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from comic_archive import android_session, sites
from comic_archive.android_cards import Cards
from comic_archive.models import ArchiveError
from test_app_bridge import app_bridge

NOW = 1_000_000.0


class Registry:
    workspace_id = 'w'

    def __init__(self, root):
        self.session, self.library = root / '.comic-session', root / 'Library'
        self.account = {'configured': False, 'speed': 'medium'}
        self.counts = {'archived': 3}
        self.types, self.unsupported, self.progress = {}, {}, {'status': 'running'}
        self.unreadable, self.listed = False, {}

    def resolve(self, owner, workspace_id):
        return types.SimpleNamespace(session=self.session, library=self.library)

    def status(self, owner, workspace_id):
        if self.unreadable:
            raise ArchiveError('unreadable')
        return {'episodes_by_status': dict(self.counts), 'estimate': None, 'episode_types': dict(self.types),
                'unsupported_by_reason': dict(self.unsupported),
                'progress': self.progress, 'worker': {'reported_state': 'stale'}, **self.listed}

    def account_status(self, owner, workspace_id):
        return dict(self.account)

    def network_status(self, owner, workspace_id):
        return {'wifi_only': True, 'allowed': True}


def said(card):
    return card['title'], card['state'], {row['name']: (row['value'], row['warning']) for row in card['rows']}['Status']


class AndroidCardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.registry, self.cards, self.worker = Registry(self.root), Cards(), {'running': False}
        self.at = NOW

    def card(self, **options):
        self.at += 10
        with patch('comic_archive.worker_host.status', side_effect=lambda session: dict(self.worker)):
            return self.cards.card(self.registry, now=self.at, **options)

    def sign_in(self):
        android_session.mark_signed_in(self.registry.session, sites.named('webtoon'))

    def test_a_card_says_what_the_desktop_would_from_sign_in_to_saving(self):
        first = self.card()
        self.assertEqual(said(first), ('Sign in to get started', 'Sign-in needed', ('Sign in to get started', False)))
        self.assertEqual((first['site'], first['name'], first['appearance'], first['message']),
                         ('tapas', 'Tapas', 'neutral', ''))
        self.assertEqual(first['download'], '')
        self.assertEqual([row['name'] for row in first['rows']],
                         ['Status', 'Saved', 'Time left', 'Space', 'Speed', 'Download speed'])
        self.assertEqual(first['rows'][1], {'name': 'Saved', 'value': '3 episodes', 'warning': False})
        self.sign_in()
        ready = self.card()
        # A downloader that is not running is ready to start with the download, not offline.
        self.assertEqual(said(ready), ('Ready to save', 'Ready', ('Ready to save', False)))
        self.assertEqual((ready['site'], ready['name']), ('webtoon', 'Webtoon'))
        self.assertEqual(ready['rows'][4]['value'], 'Medium')
        self.registry.account = {'configured': True, 'status': 'running', 'phase': 'archive', 'speed': 'medium'}
        for state in ('busy', 'stopping'):
            self.worker = {'running': True, 'state': state}
            saving = self.card()
            self.assertEqual(said(saving), ('Saving your episodes', 'Working', ('Checking and saving your episodes', False)))
            self.assertEqual(saving['appearance'], 'active')
        self.worker = {'running': True, 'state': 'starting'}
        self.assertEqual(self.card()['title'], 'Saving your episodes')
        # A downloader finishing its last step after the download ended is working, not offline.
        self.registry.account, self.registry.progress = {'configured': False, 'speed': 'medium'}, None
        self.worker = {'running': True, 'state': 'stopping'}
        self.assertEqual(self.card()['title'], 'Finishing the current step')

    def test_a_card_carries_the_desktop_cards_pill_button_sign_out_speed_and_episode_types(self):
        keys = ('pill', 'action', 'action_text', 'action_enabled', 'signout', 'signout_enabled', 'speed', 'speed_enabled',
                'selection')
        speeds = [{'key': 'fast', 'label': 'Fast'}, {'key': 'medium', 'label': 'Medium'}, {'key': 'slow', 'label': 'Slow'}]

        def parts():
            card = self.card()
            self.assertEqual(card['speeds'], speeds)
            self.assertEqual((card['status'], card['status_error']), ('', False))
            return tuple(card[key] for key in keys)

        # Signed out: the card's one button signs in, and there is no sign-in to end or speed to change.
        self.assertEqual(parts(), ('Sign in', 'signin', 'Sign in', True, False, True, '', False, 'Download: All types'))
        self.sign_in()
        self.assertEqual(parts(), ('Ready', 'go', 'Start', True, True, True, 'medium', True, 'Download: All types'))
        self.registry.account = {'configured': True, 'status': 'running', 'phase': 'archive', 'speed': 'medium'}
        self.worker = {'running': True, 'state': 'busy'}
        # While it saves the button pauses, and the sign-in stays until it has.
        self.assertEqual(parts(), ('Working', 'pause', 'Pause', True, True, False, 'medium', True, 'Download: All types'))
        self.registry.account = {'configured': True, 'status': 'stopped', 'speed': 'slow', 'download_types': {'bought': True}}
        self.worker = {'running': False}
        self.assertEqual(parts(), ('Paused', 'go', 'Continue', True, True, True, 'slow', True, 'Download: Paid series'))
        self.registry.account = {'configured': True, 'status': 'finished', 'speed': 'slow', 'download_types': 'x'}
        self.assertEqual(parts(), ('Done', 'go', 'Start', True, True, True, 'slow', True, 'Download: Unavailable'))
        self.assertEqual(self.card()['tone'], 'success')

    def test_a_card_carries_the_downloads_own_word_for_the_phones_top_card(self):
        self.sign_in()
        self.assertEqual(self.card()['download'], '')
        self.registry.account = {'configured': True, 'status': 'running', 'phase': 'archive', 'speed': 'medium'}
        # A download whose downloader is not running waits for Start: the card reads it as stopped.
        self.assertEqual(self.card()['download'], 'stopped')
        self.worker = {'running': True, 'state': 'busy'}
        self.assertEqual(self.card()['download'], 'running')
        for status, word in (('finished', 'finished'), ('stopped', 'stopped'), ('not a word', ''), (7, '')):
            self.registry.account = {'configured': True, 'status': status, 'speed': 'medium'}
            self.assertEqual(self.card()['download'], word, status)

    def test_a_card_carries_the_download_issues_and_episode_types_settings_lists(self):
        self.sign_in()
        empty = self.card()
        self.assertEqual(empty['issues'], [])
        self.assertEqual(empty['types'], [{'label': label, 'saved': '—', 'waiting': '—'} for label in (
            'Free', 'Bought / unlocked', 'Limited', 'Unknown type')])
        self.registry.counts = {'archived': 1204, 'failed': 2, 'unsupported': 5, 'unavailable': 40}
        self.registry.unsupported = {'order_unverified': 3}
        self.registry.types = {'free': {'saved': 1200, 'waiting': 7}, 'bought': {'saved': 4, 'waiting': 0}}
        card = self.card()
        self.assertEqual(card['issues'], [{'reason': 'Download errors', 'count': '2'},
                                          {'reason': 'Page order unknown', 'count': '3'},
                                          {'reason': 'Unsupported format', 'count': '2'}])
        self.assertEqual(card['types'][:2], [{'label': 'Free', 'saved': '1,200', 'waiting': '7'},
                                             {'label': 'Bought / unlocked', 'saved': '4', 'waiting': '0'}])

    def test_a_download_whose_downloader_is_gone_waits_for_start_as_a_paused_one_does(self):
        self.sign_in()
        self.registry.account = {'configured': True, 'status': 'running', 'phase': 'archive', 'speed': 'medium',
                                 'retry': {'seconds': 30, 'reason': 'source'}}
        self.worker = {'running': False}
        gone = self.card()
        self.assertEqual(said(gone), ('Your download is paused', 'Paused', ('Paused', False)))
        self.assertEqual(gone['appearance'], 'paused')
        # Whether the downloader runs is not known: the record is shown as it stands.
        self.worker = {'running': None}
        self.assertEqual(self.card()['state'], 'Waiting')

    def test_a_refused_sign_in_asks_for_one_until_the_site_is_signed_in_again(self):
        self.sign_in()
        self.registry.account = {'configured': True, 'status': 'running', 'phase': 'archive', 'speed': 'medium'}
        self.worker = {'running': True, 'state': 'busy'}
        self.card()
        self.registry.account = {'configured': True, 'status': 'held', 'code': 'login_required', 'speed': 'medium'}
        self.worker = {'running': False}
        refused = self.card()
        self.assertEqual(said(refused), ('Sign in to get started', 'Sign-in needed', ('Webtoon signed you out', True)))
        self.assertEqual(refused['appearance'], 'attention')
        # The same sign-in read again changes nothing.
        self.assertEqual(said(self.card(signed_in=True)), said(refused))
        self.sign_in()
        self.assertEqual(said(self.card()), ('Your download is paused', 'Paused', ('The sign-in had expired', True)))

    def test_the_shown_site_whose_saved_sign_in_stopped_working_says_sign_in_again(self):
        self.sign_in()
        self.assertEqual(self.card(signed_in=True)['title'], 'Ready to save')
        self.assertEqual(said(self.card(signed_in=False))[:2], ('Sign in again', 'Sign-in needed'))
        self.assertEqual(self.card()['title'], 'Ready to save')
        android_session.forget(self.registry.session)
        self.assertEqual(self.card(signed_in=True)['title'], 'Sign in to get started')

    def test_a_card_that_cannot_be_read_says_so_and_keeps_what_it_last_read(self):
        self.sign_in()
        self.registry.account = {'configured': True, 'status': 'held', 'code': 'source_refused', 'speed': 'medium'}
        held = self.card()
        self.assertEqual(said(held), ('Your download is paused', 'Paused', ('Webtoon asked the app to slow down', True)))
        self.assertEqual(held['rows'][4], {'name': 'Speed', 'value': 'Medium (a slower speed may help)', 'warning': True})
        self.registry.unreadable = True
        lost = self.card()
        self.assertEqual(said(lost), ('Status unavailable', 'Status unavailable', ('Webtoon asked the app to slow down', True)))
        self.assertEqual(lost['message'], 'Could not refresh progress. Start or resume this site to refresh it; '
                                          'your saved work is retained.')
        self.registry.unreadable = False
        self.assertEqual((self.card()['title'], self.card()['message']), ('Your download is paused', ''))
        # Each site's readings are its own, and forgetting one starts it afresh.
        self.assertEqual(len(self.cards._states), 1)
        self.cards.forget(self.registry.session)
        self.assertEqual((self.cards._states, self.cards._records), ({}, {}))


class OpenedLibrary(unittest.TestCase):
    """A working library opened through the bridge, as the phone opens one."""

    def setUp(self):
        self.before = Path.cwd()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.clean)
        self.root = Path(self.tmp.name)
        self.reset()
        self.assertTrue(self.call('prepare_workspace', root=str(self.root))['ok'])

    def reset(self):
        app_bridge._workspace.update(root=None, base=None, site_key='tapas')
        for kept in (app_bridge._open, app_bridge._site_seams, app_bridge._cloud_controllers,
                     app_bridge._catalog_registries):
            kept.clear()
        app_bridge._signin_attempts.clear()
        app_bridge._cards.update(kept=None)

    def clean(self):
        import os
        from comic_archive import network_android
        network_android.configure(None)
        os.chdir(self.before)
        self.reset()
        self.tmp.cleanup()

    def call(self, operation, **args):
        return json.loads(app_bridge.dispatch(json.dumps({'v': 1, 'op': operation, 'args': args})))


class SiteCardsBridgeTests(OpenedLibrary):
    def test_site_cards_gives_a_card_for_each_opened_site_and_tells_only_the_shown_one_its_sign_in(self):
        other = self.root / 'Webtoon'
        with patch('comic_archive.account_ui.reopen', return_value=False):
            self.assertTrue(self.call('open_site', root=str(other), base_root=str(self.root), site_key='webtoon')['ok'])
        asked = []

        class Kept:
            def card(self, registry, *, signed_in=None, now=None):
                if registry.root == other and len(asked) == 1:
                    raise ArchiveError('unreadable')
                asked.append((registry.root, signed_in))
                return {'site': registry.root.name}

        registries = {str(root): types.SimpleNamespace(root=root) for root in app_bridge._open}
        with patch.dict(app_bridge._catalog_registries, registries), patch.dict(app_bridge._cards, kept=Kept()):
            answer = self.call('site_cards', base_root=str(self.root), shown='webtoon', signed_in=False)
            # A site whose card cannot be read is left out; the rest are still given.
            self.assertEqual(answer['result'], {'cards': [{'site': self.root.name}]})
            asked.clear()
            asked.append(None)
            asked.append(None)
            answer = self.call('site_cards', base_root=str(self.root), shown='webtoon', signed_in=False)
            self.assertEqual(asked[2:], [(self.root, None), (other, False)])
            self.assertEqual(len(answer['result']['cards']), 2)
            self.assertEqual(self.call('site_cards', base_root=str(other))['result'], {'cards': []})
            for bad in ({'root': str(self.root)}, {'shown': 3}, {'signed_in': 'yes'}):
                self.assertFalse(self.call('site_cards', base_root=str(self.root), **bad)['ok'], bad)
        # With nothing kept yet, the real reader is made and words each site's card.
        with patch('comic_archive.worker_host.status', return_value={'running': False}):
            real = self.call('site_cards', base_root=str(self.root))
        self.assertEqual([(card['site'], card['title']) for card in real['result']['cards']],
                         [('tapas', 'Sign in to get started'), ('webtoon', 'Sign in to get started')])


class EpisodeInventoryBridgeTests(OpenedLibrary):
    """The phone's Library lists what the desktop's does, in the same words."""

    def seed(self, library):
        from comic_archive.inventory import Inventory
        with Inventory(library) as inventory:
            inventory.observe('42', 'Series\nname', [
                {'id': str(80 + number), 'title': f'Episode {number}', 'order': number,
                 'access_state': 'tapas_free' if number == 1 else 'unavailable_or_unverified'}
                for number in range(1, 4)], 'fixture')
            inventory.mark('81', 'archived')
            inventory.observe('43', 'Unenumerated series', [], 'fixture')

    def test_a_sites_series_and_a_series_episodes_are_listed_as_on_the_desktop(self):
        from comic_archive import window_library
        from comic_archive.episode_index import page
        library = self.root / 'Library'
        self.seed(library)
        listed = self.call('episode_inventory', base_root=str(self.root), site='tapas')
        self.assertTrue(listed['ok'], listed)
        recorded = page(library)['items']
        self.assertEqual(listed['result'], {'next_offset': None, 'empty': '',
            'comic_selection': {'default': True, 'exceptions': []}, 'items': [
            dict(zip(('title', 'status', 'format', 'access'), window_library.series_columns(row), strict=True), id=row['id'], selected=True)
            for row in recorded]})
        self.assertEqual([(row['title'], row['status']) for row in listed['result']['items']],
                         [('Series name', '1 saved · 3 listed'), ('Unenumerated series', 'No episodes listed yet')])
        episodes = self.call('episode_inventory', base_root=str(self.root), site='tapas', series_id='42')['result']
        self.assertEqual(episodes['items'], [
            dict(zip(('title', 'status', 'format', 'access'), window_library.episode_columns(row), strict=True), id=row['id'], selected=True)
            for row in page(library, series_id='42')['items']])
        self.assertEqual([(row['title'], row['status'], row['access']) for row in episodes['items']],
                         [('1 · Episode 1', 'Saved', 'Free'), ('2 · Episode 2', 'Not saved', 'Unknown type'),
                          ('3 · Episode 3', 'Not saved', 'Unknown type')])
        self.assertEqual(self.call('episode_inventory', base_root=str(self.root), site='tapas',
                                   series_id='43')['result'],
                         {'items': [], 'next_offset': None, 'empty': 'No episodes recorded',
                          'comic_selection': {'default': True, 'exceptions': []}})

    def test_a_long_list_is_read_a_page_at_a_time_and_only_its_first_page_says_it_is_empty(self):
        with patch('comic_archive.episode_index.page', return_value={
                'initialized': True, 'next_offset': 200, 'comic_selection': {'default': True, 'exceptions': []}, 'items': [
                    {'id': '7', 'title': 'One', 'episodes': 2, 'saved': 2, 'formats': ['CBZ'], 'access_kinds': ['free'], 'selected': True}]}) as read:
            first = self.call('episode_inventory', base_root=str(self.root), site='tapas', offset=0)['result']
        self.assertEqual(first['next_offset'], 200)
        self.assertEqual(first['items'], [{'id': '7', 'title': 'One', 'status': '2 saved · 2 listed', 'format': 'CBZ', 'access': 'Free', 'selected': True}])
        self.assertEqual(read.call_args.kwargs, {'series_id': None, 'offset': 0})
        with patch('comic_archive.episode_index.page',
                   return_value={'initialized': True, 'next_offset': None, 'items': [],
                                 'comic_selection': {'default': True, 'exceptions': []}}) as read:
            later = self.call('episode_inventory', base_root=str(self.root), site='tapas', series_id='42', offset=200)
        self.assertEqual(later['result'], {'items': [], 'next_offset': None, 'empty': '',
                                         'comic_selection': {'default': True, 'exceptions': []}})
        self.assertEqual(read.call_args.kwargs, {'series_id': '42', 'offset': 200})

    def test_unopened_provider_metadata_is_read_only_and_invalid_requests_are_refused(self):
        import shutil
        shutil.rmtree(self.root / 'Library' / '.archive-state', ignore_errors=True)
        fresh = self.call('episode_inventory', base_root=str(self.root), site='tapas')
        self.assertEqual(fresh['result'], {'items': [], 'next_offset': None, 'empty': 'Not indexed yet',
                                         'comic_selection': {'default': True, 'exceptions': []}})
        self.assertFalse((self.root / 'Library' / '.archive-state' / 'inventory.sqlite3').exists())
        unopened = self.call('episode_inventory', base_root=str(self.root), site='webtoon')
        self.assertTrue(unopened['ok'])
        self.assertFalse((self.root / 'Webtoon/Library/.archive-state/inventory.sqlite3').exists())
        for args in ({'site': 'unknown'}, {'site': 'tapas', 'series_id': 'x'}, {'site': 'tapas', 'offset': -1},
                     {'site': 'tapas', 'offset': '0'}, {'site': 7}, {'site': 'tapas', 'extra': 1}):
            refused = self.call('episode_inventory', base_root=str(self.root), **args)
            self.assertFalse(refused['ok'], args)
        with patch('comic_archive.episode_index.page', side_effect=OSError('PRIVATE path')):
            failed = self.call('episode_inventory', base_root=str(self.root), site='tapas')
        self.assertFalse(failed['ok'])
        self.assertNotIn('PRIVATE', json.dumps(failed))

    def test_missing_open_provider_storage_is_reported_and_cannot_be_recreated_by_an_action(self):
        session = self.root / '.comic-session'
        session.rename(self.root / 'restored-later')
        for op, args in [('site_cards', dict(base_root=str(self.root))),
                         ('prepare_workspace', dict(root=str(self.root))),
                         ('list_preference', dict(base_root=str(self.root), site='tapas', action='list')),
                         ('choose_comics', dict(base_root=str(self.root), chosen=False))]:
            with self.subTest(operation=op):
                refused = self.call(op, **args)
                self.assertEqual(refused['code'], 'workspace_unavailable')
                self.assertFalse(session.exists())
        (self.root / 'restored-later').rename(session)
        self.assertTrue(self.call('site_cards', base_root=str(self.root))['ok'])

    def test_list_mode_clear_and_completed_refresh_keep_saved_files_and_provider_scope(self):
        from comic_archive import android_download
        from comic_archive.workspace import Workspace
        workspace = Workspace(id="default", root=self.root)
        self.seed(workspace.library)
        args = dict(base_root=str(self.root), site='tapas')
        self.assertTrue(self.call('list_preference', **args, action='list')['ok'])
        self.assertTrue(android_download.list_only(workspace))
        self.assertFalse(self.call('list_preference', base_root=str(self.root.parent), site='tapas', action='files')['ok'])
        self.assertTrue(self.call('list_preference', **args, action='clear')['ok'])
        rows = self.call('episode_inventory', **args, series_id='42')['result']['items']
        self.assertEqual([row['id'] for row in rows], ['81'])
        for folder in ('requests', 'responses'):
            (workspace.session / 'worker' / folder).mkdir(parents=True, exist_ok=True)
        with patch('comic_archive.account_ui.start') as start:
            android_download.start(workspace)
        start.assert_called_once_with(workspace, list_only=True)
        fetch = {'list_only': True, 'status': 'running', 'episode_list_ready': False, 'lists': {'checked': 2, 'total': 5}}
        self.assertTrue(android_download.list_hidden(workspace, fetch))
        self.assertFalse(android_download.list_hidden(workspace, fetch | {'status': 'stopped', 'episode_list_ready': True}))
        self.assertEqual(len(self.call('episode_inventory', **args, series_id='42')['result']['items']), 3)
        self.assertTrue(self.call('list_preference', **args, action='files')['ok'])
        self.assertFalse(android_download.list_only(workspace))

    def recorded(self, **account):
        """The seeded library and a stand-in for its download's record, which its card is read from."""
        root = next(iter(app_bridge._open))
        self.seed(root / 'Library')
        registry = Registry(root)
        registry.listed = {'series_count': 2, 'episode_count': 3}
        registry.account = {'configured': True, 'speed': 'medium', 'list_only': True, **account}
        return registry

    def clear(self, mode='list'):
        for action in (mode, 'clear'):
            self.assertTrue(self.call('list_preference', base_root=str(self.root), site='tapas', action=action)['ok'])

    def start(self):
        """Start is pressed and admitted, in the download mode chosen."""
        from comic_archive import android_download
        from comic_archive.workspace import Workspace
        workspace = Workspace(id="default", root=self.root)
        for folder in ('requests', 'responses'):
            (workspace.session / 'worker' / folder).mkdir(parents=True, exist_ok=True)
        with patch('comic_archive.account_ui.start'):
            android_download.start(workspace)

    def shown(self, registry):
        """After one reading of the card: the episodes the Library lists under the series, whether comics can be
        chosen, and whether there is a list to clear."""
        with patch.dict(app_bridge._catalog_registries, {str(root): registry for root in app_bridge._open}), \
                patch('comic_archive.worker_host.status', return_value={'running': False}):
            card = self.call('site_cards', base_root=str(self.root))['result']['cards'][0]
        rows = self.call('episode_inventory', base_root=str(self.root), site='tapas', series_id='42')['result']['items']
        return len(rows), card['has_episodes'], card['has_list']

    def test_a_cleared_list_is_shown_again_once_its_fetch_is_paused_with_series_listed(self):
        registry = self.recorded(status='running', phase='lists', lists={'checked': 1, 'total': 5, 'since_start': 1})
        self.clear()
        self.assertEqual(self.shown(registry), (1, False, False))
        self.start()
        # Its record still says running, so the list stays hidden: even on a card read before the downloader is up.
        self.assertEqual(self.shown(registry), (1, False, False))
        registry.account.update(status='stopped')
        self.assertEqual(self.shown(registry), (3, True, True))

    def test_a_cleared_list_is_shown_again_once_its_fetch_stops_on_a_problem_with_series_listed(self):
        registry = self.recorded(status='held', code='source_refused', phase='lists', lists={'checked': 2, 'total': 5, 'since_start': 2})
        self.clear()
        self.start()
        self.assertEqual(self.shown(registry), (3, True, True))

    def test_a_cleared_list_stays_hidden_when_its_fetch_stopped_before_listing_anything(self):
        registry = self.recorded(status='stopped', phase='lists', lists={'checked': 2, 'total': 5, 'since_start': 0})
        self.clear()
        self.start()
        self.assertEqual(self.shown(registry), (1, False, False))
        registry.account.update(status='held', code='source_refused')
        self.assertEqual(self.shown(registry), (1, False, False))

    def test_a_card_that_cannot_be_read_leaves_a_cleared_list_hidden(self):
        registry = self.recorded(status='stopped', phase='archive', episode_list_ready=True, lists={'checked': 5, 'total': 5})
        self.assertEqual(self.shown(registry), (3, True, True))
        self.clear()
        self.start()
        registry.unreadable = True
        # The card keeps its last good reading, of the list as it was before the clear: that says nothing of the new fetch.
        self.assertEqual(self.shown(registry), (1, False, False))

    def test_a_start_that_lands_while_a_card_is_read_does_not_show_the_cleared_list(self):
        registry = self.recorded(status='stopped', phase='archive', episode_list_ready=True, lists={'checked': 5, 'total': 5})
        self.clear()
        read = registry.network_status

        def network_status(owner, workspace_id):
            self.start()  # Pressed after this card read the download, which still described the list before the clear.
            return read(owner, workspace_id)
        registry.network_status = network_status
        self.assertEqual(self.shown(registry), (1, False, False))
        from comic_archive import android_download
        self.assertTrue(android_download.list_awaited(registry.resolve(None, None)))

    def test_an_ordinary_start_after_clearing_the_list_shows_it_again(self):
        registry = self.recorded(status='stopped', phase='archive', episode_list_ready=True, lists={'checked': 5, 'total': 5})
        self.clear('files')
        self.assertEqual(self.shown(registry), (1, False, False))
        self.start()
        self.assertEqual(self.shown(registry), (3, True, True))

    def test_choose_comics_updates_only_future_acquisitions_and_never_starts_a_worker(self):
        self.seed(self.root / 'Library')
        saved = self.root / 'Library' / 'saved.cbz'
        saved.write_bytes(b'original comic')
        answer = self.call('choose_comics', base_root=str(self.root), site='tapas', series_id='42', chosen=False)
        self.assertTrue(answer['ok'], answer)
        self.assertEqual(answer['result']['choices'], {'tapas': {'default': True, 'exceptions': ['42']}})
        self.assertEqual(self.call('site_cards', base_root=str(self.root))['result']['cards'][0]['bulk'], 'mixed')
        rows = self.call('episode_inventory', base_root=str(self.root), site='tapas')['result']['items']
        self.assertEqual([row['selected'] for row in rows], [False, True])
        self.assertEqual(saved.read_bytes(), b'original comic')
        self.assertFalse(list((self.root / '.comic-session/worker/requests').glob('*.json')))
        picked = self.call('choose_comics', base_root=str(self.root), site='tapas', series_id='42',
                           episode_id='82', chosen=True)
        self.assertEqual(picked['result']['choices']['tapas']['episodes'], {'82': ['42', True]})
        self.assertTrue(self.call('episode_inventory', base_root=str(self.root), site='tapas')['result']['items'][0]['mixed'])
        episodes = self.call('episode_inventory', base_root=str(self.root), site='tapas', series_id='42')
        self.assertEqual([row['selected'] for row in episodes['result']['items']], [False, True, False])
        all_off = self.call('choose_comics', base_root=str(self.root), chosen=False)
        self.assertTrue(all_off['ok'], all_off)
        self.assertEqual(self.call('site_cards', base_root=str(self.root))['result']['cards'][0]['bulk'], 'off')
        self.assertEqual(set(all_off['result']['choices']), {'tapas', 'webtoon', 'tappytoon'})
        self.assertTrue(all(not value['default'] and not value['exceptions']
                            for value in all_off['result']['choices'].values()))
        wrong = self.call('choose_comics', base_root=str(self.root.parent / 'different'), chosen=True)
        self.assertFalse(wrong['ok'])
        for args in ({'chosen': 1}, {'chosen': True, 'site': 'bad'}, {'chosen': False, 'series_id': '42'},
                     {'chosen': True, 'site': 'tapas', 'series_id': '999'}, {'chosen': True, 'extra': 1}):
            self.assertFalse(self.call('choose_comics', base_root=str(self.root), **args)['ok'], args)


if __name__ == '__main__':
    unittest.main()
