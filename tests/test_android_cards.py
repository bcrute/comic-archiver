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
        self.unreadable = False

    def resolve(self, owner, workspace_id):
        return types.SimpleNamespace(session=self.session, library=self.library)

    def status(self, owner, workspace_id):
        if self.unreadable:
            raise ArchiveError('unreadable')
        return {'episodes_by_status': dict(self.counts), 'estimate': None, 'episode_types': dict(self.types),
                'unsupported_by_reason': dict(self.unsupported),
                'progress': self.progress, 'worker': {'reported_state': 'stale'}}

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
        self.assertEqual(said(self.card()), ('Your download is paused', 'Paused', ('Your download is paused', False)))

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


class SiteCardsBridgeTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
