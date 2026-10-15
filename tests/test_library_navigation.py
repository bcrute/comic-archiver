import unittest
from comic_archive.adapters.library_navigation import reading_list_location, supplied_filter_location


class LibraryNavigationTests(unittest.TestCase):
    def test_supplied_filter_code_is_private_navigation_data_and_does_not_relax_all_scope(self):
        url='https://tapas.io/reading-list?pageNumber=2&type=FREE&category=SUBSCRIBED'
        self.assertEqual(supplied_filter_location(url),{'page':2,'category':'subscribed','type':'FREE'})
        self.assertIsNone(reading_list_location(url))
        self.assertEqual(supplied_filter_location('https://tapas.io/reading-list?type=provided_code')['type'],'provided_code')

    def test_filter_navigation_rejects_credentials_other_routes_and_ambiguous_or_secret_parameters(self):
        for url in ('https://user:PRIVATE@tapas.io/reading-list?type=FREE',
                    'https://elsewhere.test/reading-list?type=FREE','https://tapas.io:444/reading-list?type=FREE',
                    'https://tapas.io/ink/purchase?type=FREE','https://tapas.io/reading-list?type=FREE&token=PRIVATE',
                    'https://tapas.io/reading-list?type=FREE&type=WUF','https://tapas.io/reading-list?type=FREE#PRIVATE',
                    'https://tapas.io/reading-list?type=FREE&category=FREE%20PRIVATE',
                    'https://tapas.io/reading-list?type=FREE&pageNumber=0',
                    'https://tapas.io/reading-list?type=FREE%20PRIVATE'):
            with self.subTest(url=url):self.assertIsNone(supplied_filter_location(url))

    def test_provided_category_filter_is_private_and_does_not_relax_all_crawler(self):
        url='https://tapas.io/reading-list?pageNumber=2&category=PROVIDED_CODE&type=ALL'
        self.assertEqual(supplied_filter_location(url),{'page':2,'category':'provided_code','type':'ALL'})
        self.assertIsNone(reading_list_location(url))
    def test_supplied_all_pager_and_first_page_have_explicit_scope(self):
        self.assertEqual(reading_list_location('https://tapas.io/reading-list?pageNumber=2&type=ALL&category=SUBSCRIBED'),{'page':2,'category':'subscribed','type':'all'})
        self.assertEqual(reading_list_location('https://tapas.io/reading-list'),{'page':1,'category':'updated','type':'all'})

    def test_credentials_payment_paths_ambiguous_parameters_and_other_scopes_are_rejected(self):
        for url in ('https://tapas.io/ink/purchase?pageNumber=2','https://user:secret@tapas.io/reading-list',
                    'https://elsewhere.test/reading-list','https://tapas.io:444/reading-list',
                    'https://tapas.io/reading-list?pageNumber=2&token=PRIVATE',
                    'https://tapas.io/reading-list?pageNumber=2&pageNumber=3',
                    'https://tapas.io/reading-list?pageNumber=0','https://tapas.io/reading-list?type=UNKNOWN',
                    'https://tapas.io/reading-list?category=PURCHASED','https://tapas.io/reading-list#!/reading-list?type=COMICS'):
            with self.subTest(url=url):self.assertIsNone(reading_list_location(url))

    def test_non_text_and_unparseable_links_are_ignored_instead_of_raising(self):
        for function in (supplied_filter_location,reading_list_location):
            for url in (None,42,b'https://tapas.io/reading-list','https://[tapas.io/reading-list',
                        'https://tapas.io:99999/reading-list'):
                with self.subTest(function=function.__name__,url=url):self.assertIsNone(function(url))


if __name__=='__main__':unittest.main()
