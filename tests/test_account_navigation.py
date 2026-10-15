import unittest
from comic_archive.adapters.account_navigation import navigation_path


class NavigationTests(unittest.TestCase):
    def test_observed_ink_navigation_keeps_only_tracking_query_in_memory(self):
        self.assertEqual(navigation_path('https://tapas.io/profile/ink?trigger=profile'),'/profile/ink')
        self.assertEqual(navigation_path('https://tapas.io/profile/settings'),'/profile/settings')

    def test_purchase_unlock_external_and_credential_links_are_not_followed(self):
        for href in ('https://tapas.io/ink/popup','https://tapas.io/episode/81/unlock',
                     'https://tapas.io/ink/purchase','https://tapas.io/ink/checkout',
                     'https://tapas.io/account/signout','https://tapas.io/profile/ink?token=secret',
                     'https://tapas.io/profile/ink?trigger=profile&user_id=123',
                     'https://tapas.io/profile/ink?trigger=',
                     'https://evil.example/history','https://user:secret@tapas.io/history',
                     'https://tapas.io:443/history','http://tapas.io/history'):
            with self.subTest(href=href):
                self.assertIsNone(navigation_path(href))
