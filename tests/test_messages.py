"""The apps' words are written once: every phrase a screen asks for is in the catalog, with its values."""
import json
import re
import unittest
from pathlib import Path

from comic_archive import messages

ROOT = Path(__file__).resolve().parents[1]
PHONE = ROOT / 'android/app/src/main/java'
CORE = ROOT / 'comic_archive'
PLACE = re.compile(r'\{([a-z][a-z0-9_]*)\}', re.I)


def phone_calls():
    """Each say("code", "name" to value, ...) in the phone's code: (file, code, names)."""
    for path in sorted(PHONE.rglob('*.kt')):
        source = path.read_text(encoding='utf-8')
        for found in re.finditer(r'\bsay\("([^"$]+)"', source):
            depth, at, quoted = 1, found.end(), False
            while depth:
                c = source[at]
                if c == '\\':
                    at += 1
                elif c == '"':
                    quoted = not quoted
                elif not quoted:
                    depth += (c == '(') - (c == ')')
                at += 1
            yield path.name, found[1], set(re.findall(r'"([A-Za-z][A-Za-z0-9_]*)" to ', source[found.end():at]))


def core_calls():
    for path in sorted(CORE.rglob('*.py')):
        for found in re.finditer(r"\bsay\('([^'{]+)'", path.read_text(encoding='utf-8')):
            yield path.name, found[1]


class MessageTests(unittest.TestCase):
    def test_a_phrase_takes_its_named_values_and_an_unknown_code_is_an_error(self):
        self.assertEqual(messages.say('signin.failed', site='Webtoon'), 'Sign-in did not finish. Try Sign in to Webtoon again.')
        self.assertEqual(messages.say('notification.stop'), 'Stop')
        with self.assertRaises(KeyError):
            messages.say('no.such.code')

    def test_the_catalog_is_one_flat_list_of_phrases_in_order(self):
        raw = (CORE / 'messages.json').read_text(encoding='utf-8')
        listed = json.loads(raw)
        self.assertEqual(listed, messages.TEXT)
        self.assertEqual(list(listed), sorted(listed))
        for code, words in listed.items():
            self.assertRegex(code, r'^[a-z0-9_]+(\.[a-z0-9_]+)+$')
            self.assertIsInstance(words, str)
            self.assertTrue(words.strip(), code)

    def test_every_phrase_the_phone_asks_for_is_listed_with_the_values_it_names(self):
        calls = list(phone_calls())
        self.assertGreater(len(calls), 300)
        for file, code, names in calls:
            self.assertIn(code, messages.TEXT, file)
            self.assertEqual(set(PLACE.findall(messages.TEXT[code])), names, f'{file}: {code}')

    def test_every_phrase_the_core_asks_for_is_listed(self):
        for file, code in core_calls():
            self.assertIn(code, messages.TEXT, file)

    def test_every_issue_the_phone_can_show_has_its_notice_and_its_way_out(self):
        models = (PHONE / 'io/github/bcrute/comicarchiver/ui/UiModels.kt').read_text(encoding='utf-8')
        issues = re.findall(r'^    [A-Z_]+\("([a-z_]+)"\)[,;]$', models, re.M)
        self.assertEqual(len(issues), 9)
        for issue in issues:
            self.assertIn(f'notice.{issue}', messages.TEXT)
            self.assertIn(f'notice.{issue}.action', messages.TEXT)

    def test_no_phrase_is_listed_that_nothing_says(self):
        said = {code for _, code, _ in phone_calls()} | {code for _, code in core_calls()}
        families = ('notice.', 'signin.')  # Asked for by a code put together where it is said.
        unsaid = sorted(code for code in messages.TEXT if code not in said and not code.startswith(families))
        self.assertEqual(unsaid, [])

    def test_the_download_menu_and_the_library_choices_are_one_set_of_phrases_on_the_desktop_and_the_phone(self):
        desktop = {code for file, code in core_calls() if file in ('window_library.py', 'window_pages.py')}
        phone = {code for file, code, _ in phone_calls()
                 if file in ('DefaultAppController.kt', 'DownloadsScreen.kt', 'EpisodeLibraryScreen.kt')}
        for code in ('library.applying_comic_choices', 'library.check_a_series_or_episode_to', 'library.check_all',
                     'library.comic_choices_saved_existing_files_are', 'library.could_not_apply_every_choice_review',
                     'library.uncheck_all', 'window.choose_comics_in_library', 'window.clear_episode_list',
                     'window.could_not_clear_the_episode_list', 'window.could_not_save_the_download_mode',
                     'window.download_settings', 'window.episode_files', 'window.episode_list_only',
                     'window.select_episode_list_only_then_press'):
            self.assertIn(code, desktop, code)
            self.assertIn(code, phone, code)


if __name__ == '__main__':
    unittest.main()
