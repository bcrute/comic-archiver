import unittest
from comic_archive.adapters.series_metadata import series_details
from comic_archive.errors import MetadataError


HTML='''<script>var seriesId="42"; var accountName="PRIVATE_ACCOUNT";</script>
<ul class="detail-row__body detail-row__body--creator"><li><div class="creator-info__top">
<a class="thumb">Avatar</a><a class="name js-fb-tracking">Publisher &amp; Artist</a></div></li></ul>
<div class="detail-row__body detail-row__body--desc">
<p class="description js-series-description"><span class="description__body">A comic.<br>Second line.</span>Read more</p>
<div class="schedule">Wait Until Free offer: not description</div><p class="colophon">Created by Artist<br>Translated by Translator</p></div>
<ul class="creator-section"><li><div class="creator-info__top"><a class="name">Publisher &amp; Artist</a></div></li></ul>
<p class="js-series-description"><span class="description__body">A comic.<br>Second line.</span></p>
<header><a class="name">PRIVATE_ACCOUNT</a></header>'''


class SeriesMetadataTests(unittest.TestCase):
    def test_scoped_metadata_deduplicates_variants_without_account_or_action_text(self):
        result=series_details(HTML,42)
        self.assertEqual(result,{'creators':('Publisher & Artist',),
            'description':'A comic.\nSecond line.','credits':'Created by Artist\nTranslated by Translator'})
        self.assertNotIn('PRIVATE',repr(result))
        self.assertNotIn('Read more',repr(result))
        self.assertNotIn('Wait Until Free',repr(result))

    def test_wrong_identity_and_conflicting_details_fail_without_private_values(self):
        for html,expected in ((HTML,43),(HTML.replace('seriesId="42"','seriesId="43"'),42),
                (HTML.replace('A comic.<br>Second line.</span></p>','Different comic</span></p>'),42)):
            with self.assertRaises(MetadataError) as error:
                series_details(html,expected)
            self.assertNotIn('PRIVATE',str(error.exception))

    def test_absent_optional_fields_and_foreign_description_are_not_guessed(self):
        result=series_details('<script>seriesId=42;</script><span class="description__body">Unrelated</span>',42)
        self.assertEqual(result,{'creators':(),'description':None,'credits':None})

    def test_oversized_or_non_text_pages_are_refused(self):
        self.assertEqual(series_details(HTML,42)['creators'],('Publisher & Artist',))
        for html in (HTML+' '*(8*1024*1024),b'<script>seriesId=42;</script>',None):
            with self.assertRaisesRegex(MetadataError,'exceeds the supported size'):series_details(html,42)

    def test_series_identity_may_come_from_one_page_attribute_but_not_from_conflicting_ones(self):
        page='<div data-series-id="42"><p class="js-series-description"><span class="description__body">Plain</span></p></div>'
        self.assertEqual(series_details(page,42)['description'],'Plain')
        with self.assertRaises(MetadataError):series_details(page+'<p data-series-id="43"></p>',42)
        with self.assertRaises(MetadataError):series_details(page,0)

    def test_creator_description_and_credit_limits_are_enforced(self):
        def creators(names):
            return '<script>seriesId=42;</script><ul class="creator-section">'+''.join(
                f'<li><div class="creator-info__top"><a class="name">{name}</a></div></li>' for name in names)+'</ul>'
        self.assertEqual(len(series_details(creators(f'Name {i}' for i in range(20)),42)['creators']),20)
        for names in ([f'Name {i}' for i in range(21)],['N'*201]):
            with self.assertRaises(MetadataError):series_details(creators(names),42)
        def description(text):
            return f'<script>seriesId=42;</script><p class="js-series-description"><span class="description__body">{text}</span></p>'
        def credits(text):
            return f'<script>seriesId=42;</script><div class="detail-row__body--desc"><p class="colophon">{text}</p></div>'
        self.assertEqual(len(series_details(description('a'*20000),42)['description']),20000)
        self.assertEqual(len(series_details(credits('a'*20000),42)['credits']),20000)
        for page in (description('a'*20001),credits('a'*20001)):
            with self.assertRaises(MetadataError):series_details(page,42)

    def test_valueless_attributes_are_read_as_empty_not_as_a_parser_failure(self):
        noisy=HTML.replace('<header>','<header class><div data-series-id><span class></span></div>')
        self.assertEqual(series_details('<p class></p><div data-series-id></div>'+noisy,42),series_details(HTML,42))

    def test_stray_closing_tags_do_not_change_the_extracted_details(self):
        noisy=HTML.replace('<p class="colophon">','</aside><p class="colophon"></i>').replace('<header>','</section><header>')
        self.assertEqual(series_details(noisy,42),series_details(HTML,42))
