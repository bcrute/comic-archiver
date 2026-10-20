"""What each site's profile decides outside its reader: whose cookies are kept and sent where, which access marks queue."""
import dataclasses
import json
import stat
import tempfile
import unittest
from pathlib import Path

from support import PRIVATE_FILE
from test_transport_session import Client, Response
from comic_archive import account_export, desktop_app, sites, worker
from comic_archive.adapters.tapas import TapasAdapter
from comic_archive.errors import MetadataError
from comic_archive.inventory import Inventory
from comic_archive.models import ArchiveError, Episode, Page
from comic_archive.session import load_session, store_session
from comic_archive.transport import Transport
from comic_archive.window_signin import storage_state

WEBTOON, TAPPYTOON = sites.named('webtoon'), sites.named('tappytoon')


def cookie(name, domain):
    return {'name': name, 'value': 'PRIVATE_' + name, 'domain': domain, 'path': '/', 'expires': -1,
            'httpOnly': True, 'secure': True, 'sameSite': 'Lax'}


class SiteProfileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.session = Path(temporary.name) / '.comic-session'

    def test_a_sign_in_keeps_only_its_own_sites_cookies_and_storage_and_records_whose_it_is(self):
        state = {'cookies': [cookie('session', '.webtoons.com'), cookie('page', 'www.webtoons.com'), cookie('provider', '.webtoon.com'),
                             cookie('tapas', '.tapas.io'), cookie('google', '.google.com')],
                 'origins': [{'origin': 'https://www.webtoons.com', 'localStorage': [{'name': 'kept', 'value': '1'}]},
                             {'origin': 'https://tapas.io', 'localStorage': [{'name': 'dropped', 'value': '1'}]}]}
        store_session(state, self.session, WEBTOON)
        saved = load_session(self.session)
        self.assertEqual(sorted(c['name'] for c in saved['cookies']), ['page', 'session'])
        self.assertEqual([o['origin'] for o in saved['origins']], ['https://www.webtoons.com'])
        self.assertEqual(stat.S_IMODE((self.session / 'site').stat().st_mode), PRIVATE_FILE)
        self.assertIs(sites.of_session(self.session), WEBTOON)
        self.assertEqual(account_export.lists(self.session), WEBTOON.lists)
        # Signing in again to Tapas in the same place makes it Tapas's, with only Tapas's cookies.
        store_session({'cookies': [cookie('tapas', '.tapas.io'), cookie('session', '.webtoons.com')], 'origins': []}, self.session)
        self.assertEqual([c['name'] for c in load_session(self.session)['cookies']], ['tapas'])
        self.assertIs(sites.of_session(self.session), sites.DEFAULT)

    def test_a_sign_in_saved_before_sites_existed_is_tapass_and_an_unknown_site_is_refused(self):
        self.assertIs(sites.of_session(self.session), sites.DEFAULT)
        self.assertEqual(account_export.lists(self.session), ('subscribed', 'updated', 'recent', 'free_episodes'))
        self.session.mkdir()
        for text in ('somewhere', '', 'Webtoon'):
            (self.session / 'site').write_text(text)
            with self.subTest(text=text), self.assertRaises(ArchiveError):
                sites.of_session(self.session)

    def test_the_window_hands_over_only_the_open_sites_cookies_and_storage(self):
        state = storage_state([cookie('session', '.webtoons.com'), cookie('tapas', '.tapas.io')], [('kept', '1')], WEBTOON)
        self.assertEqual(([c['name'] for c in state['cookies']], state['origins']),
                         (['session'], [{'origin': 'https://www.webtoons.com', 'localStorage': [{'name': 'kept', 'value': '1'}]}]))
        with self.assertRaisesRegex(ArchiveError, 'no Webtoon cookies'):
            storage_state([cookie('tapas', '.tapas.io')], (), WEBTOON)

    def test_the_window_asks_only_the_sites_own_page_and_never_takes_a_site_it_cannot_ask_as_signed_in(self):
        self.assertTrue(desktop_app.on_site('https', 'www.webtoons.com', WEBTOON))
        for scheme, host in (('https', 'account.webtoon.com'), ('https', 'tapas.io'), ('http', 'www.webtoons.com')):
            self.assertFalse(desktop_app.on_site(scheme, host, WEBTOON))
        # A Tapas-shaped answer from the Webtoon page proves nothing: Webtoon names its account its own way.
        answer = json.dumps({'origin': 'https://www.webtoons.com', 'answer': '{"data":{"user":{"id":11}}}', 'storage': []})
        self.assertTrue(desktop_app.page_spoke(answer, WEBTOON))
        self.assertIsNone(desktop_app.signed_in(answer, WEBTOON))
        named = json.dumps({'origin': 'https://www.webtoons.com', 'answer': '{"loginUser":true,"loginId":"reader"}', 'storage': []})
        self.assertEqual(desktop_app.signed_in(named, WEBTOON), ('reader', []))
        self.assertIsNone(desktop_app.signed_in(named.replace('true', 'false'), WEBTOON))
        # The page asks what Webtoon's own pages ask, instead of Tapas's question.
        self.assertIn("fetch('/en/member/userInfo'", desktop_app.ASKS['webtoon'])
        self.assertNotIn('/who-am-i', desktop_app.ASKS['webtoon'])
        self.assertIsNone(desktop_app.signed_in(answer.replace('https://www.webtoons.com', 'https://tapas.io'), WEBTOON))
        self.assertEqual(desktop_app.signed_in(answer.replace('https://www.webtoons.com', 'https://tapas.io')), ('11', []))

    def test_a_tappytoon_page_names_its_account_as_the_downloader_reads_it(self):
        def answer(value, origin='https://www.tappytoon.com'):
            return json.dumps({'origin': origin, 'answer': json.dumps(value), 'storage': [['kept', '1']]})
        self.assertEqual(desktop_app.signed_in(answer({'oauth2': {'user': {'id': 123456789}}}), TAPPYTOON), ('123456789', [['kept', '1']]))
        for value in ({'oauth2': {'user': {'id': None}}}, {'oauth2': {'user': {'id': '5'}}}, {'data': {'user': {'id': 5}}}):
            with self.subTest(value=value):
                self.assertIsNone(desktop_app.signed_in(answer(value), TAPPYTOON))
        self.assertIsNone(desktop_app.signed_in(answer({'oauth2': {'user': {'id': 5}}}, 'https://tapas.io'), TAPPYTOON))
        # The page asks its library page, which names no account to a visitor, instead of Tapas's question.
        self.assertIn("fetch('/en/me/library/unlocked/comics'", desktop_app.ASKS['tappytoon'])
        self.assertNotIn('/who-am-i', desktop_app.ASKS['tappytoon'])

    def test_the_downloader_reads_with_the_sites_own_reader_and_will_not_start_without_one(self):
        self.assertIs(worker.reader(sites.DEFAULT), TapasAdapter)
        with self.assertRaisesRegex(ArchiveError, 'Elsewhere'):
            worker.reader(dataclasses.replace(TAPPYTOON, key='elsewhere', name='Elsewhere'))


class SiteDownloadTests(unittest.TestCase):
    COOKIES = [cookie('CloudFront-Policy', '.tappytoon.com'), cookie('page', 'www.tappytoon.com'), cookie('tapas', '.tapas.io')]

    def test_a_sites_cookies_go_only_to_its_named_hosts_that_the_cookie_belongs_to_with_its_own_referer(self):
        client = Client([Response(), Response(), Response()])
        transport = Transport(self.COOKIES, client=client, site=TAPPYTOON)
        for url in ('https://content-repository-cdn.tappytoon.com/67/1/a.jpg', 'https://www.tappytoon.com/x',
                    'https://image-repository-cdn.tappytoon.com/series/1/c.jpg'):
            transport.get(url)
        (_, content), (_, page), (_, cover) = client.calls
        self.assertEqual(content['Cookie'], 'CloudFront-Policy=PRIVATE_CloudFront-Policy')
        self.assertEqual(page['Cookie'], 'CloudFront-Policy=PRIVATE_CloudFront-Policy; page=PRIVATE_page')
        self.assertNotIn('Cookie', cover)
        self.assertEqual({headers['Referer'] for _, headers in client.calls}, {'https://www.tappytoon.com/'})

    def test_pages_from_outside_a_sites_hosts_are_refused_before_any_request(self):
        client = Client([Response(), Response()])
        webtoon = Transport(self.COOKIES, client=client, site=WEBTOON)
        webtoon.get('https://webtoon-phinf.pstatic.net/2020/a.png')
        self.assertEqual(client.calls[0][1], {'Referer': 'https://www.webtoons.com/'})
        for site, url in ((WEBTOON, 'https://content-repository-cdn.tappytoon.com/a.jpg'), (WEBTOON, 'http://webtoon-phinf.pstatic.net/a.png'),
                          (TAPPYTOON, 'https://webtoon-phinf.pstatic.net/a.png'), (TAPPYTOON, 'https://tappytoon.com.example.com/a.jpg'),
                          (sites.DEFAULT, 'https://www.tappytoon.com/a.jpg')):
            with self.subTest(url=url), self.assertRaisesRegex(ArchiveError, site.name):
                Transport(client=client, site=site).get(url)
        self.assertEqual(len(client.calls), 1)
        # Tapas as before: its cookies to tapas.io itself, none to its image hosts.
        tapas = Client([Response(), Response()])
        transport = Transport(self.COOKIES, client=tapas)
        transport.get('https://tapas.io/x'); transport.get('https://us-a.tapas.io/a.png')
        self.assertEqual([headers.get('Cookie') for _, headers in tapas.calls], ['tapas=PRIVATE_tapas', None])


class SiteAccessTests(unittest.TestCase):
    def test_every_sites_free_and_unlocked_episodes_queue_with_unlocked_first_and_nothing_else_does(self):
        with tempfile.TemporaryDirectory() as directory, Inventory(Path(directory) / 'Library') as inventory:
            inventory.observe('7', 'Series', [
                {'id': '71', 'title': 'Free', 'order': 1, 'access_state': 'webtoon_free'},
                {'id': '72', 'title': 'Bought', 'order': 2, 'access_state': 'webtoon_unlocked'},
                {'id': '73', 'title': 'Locked', 'order': 3, 'access_state': 'unavailable_or_unverified'}], 'fixture')
            self.assertEqual([row['id'] for row in inventory.pending()], ['72', '71'])
            self.assertEqual([row['id'] for row in inventory.pending(free_only=True)], ['71'])
            with self.assertRaises(MetadataError):
                inventory.observe('7', 'Series', [{'id': '74', 'title': 'Rented', 'order': 4, 'access_state': 'webtoon_rented'}], 'fixture')

    def test_an_episode_is_saved_only_with_a_known_sites_access_mark(self):
        page = (Page(1, 'https://www.tappytoon.com/a.jpg'),)
        self.assertEqual(Episode('1', '2', 'Chapter', 1, page, 'tappytoon_unlocked').access, 'tappytoon_unlocked')
        for access in ('tappytoon_rented', 'unlocked', 'unavailable_or_unverified'):
            with self.subTest(access=access), self.assertRaises(ArchiveError):
                Episode('1', '2', 'Chapter', 1, page, access)


if __name__ == '__main__':
    unittest.main()
