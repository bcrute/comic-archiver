import unittest
from datetime import datetime, timezone

from comic_archive.models import ArchiveError
from comic_archive.retry_after import MAX_RETRY_AFTER, retain_retry_after, retry_after_seconds


class RetryAfterTests(unittest.TestCase):
    def test_a_date_without_a_known_timezone_is_ignored_instead_of_guessed(self):
        now = datetime(2026, 10, 8, 3, 0, tzinfo=timezone.utc)
        self.assertEqual(retry_after_seconds('Thu, 08 Oct 2026 03:02:00 GMT', now), 120)
        self.assertIsNone(retry_after_seconds('Thu, 08 Oct 2026 03:02:00 -0000', now))

    def test_a_number_of_seconds_is_read_even_with_spaces_around_it(self):
        for value, seconds in (('0', 0), ('120', 120), (' 120 ', 120), ('\t7', 7), ('007', 7)):
            with self.subTest(value=value):
                self.assertEqual(retry_after_seconds(value), seconds)

    def test_a_negative_fractional_signed_or_foreign_number_is_ignored(self):
        for value in ('-5', '1.5', '+5', '1e3', '0x10', '5 s', '\u0663', '', '   ', None, 5, b'5'):
            with self.subTest(value=value):
                self.assertIsNone(retry_after_seconds(value))

    def test_a_wait_beyond_any_deadline_saturates_instead_of_overflowing(self):
        self.assertEqual(retry_after_seconds(str(MAX_RETRY_AFTER)), MAX_RETRY_AFTER)
        self.assertEqual(retry_after_seconds('9' * 200), MAX_RETRY_AFTER)
        now = datetime(1970, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(retry_after_seconds('Fri, 31 Dec 9999 23:59:59 GMT', now), MAX_RETRY_AFTER)

    def test_an_over_long_value_is_not_read_at_all(self):
        self.assertIsNone(retry_after_seconds('1' + ' ' * 256))
        self.assertIsNone(retry_after_seconds('9' * 257))

    def test_a_date_already_past_means_no_wait(self):
        now = datetime(2026, 10, 8, 3, 0, tzinfo=timezone.utc)
        self.assertEqual(retry_after_seconds('Thu, 08 Oct 2026 02:00:00 GMT', now), 0)
        self.assertEqual(retry_after_seconds('Thu, 08 Oct 2026 03:00:00 GMT', now), 0)

    def test_https_older_date_forms_are_read_as_gmt(self):
        now = datetime(1994, 11, 6, 8, 49, tzinfo=timezone.utc)
        self.assertEqual(retry_after_seconds('Sun, 06 Nov 1994 08:49:37 GMT', now), 37)
        self.assertEqual(retry_after_seconds('Sunday, 06-Nov-94 08:49:37 GMT', now), 37)
        self.assertEqual(retry_after_seconds('Sun Nov  6 08:49:37 1994', now), 37)
        # Only the exact asctime form is taken as GMT; another date without a zone is still not guessed.
        self.assertIsNone(retry_after_seconds('Sun Nov 6 08:49:37 1994', now))
        self.assertIsNone(retry_after_seconds('Sun, 06 Nov 1994 08:49:37', now))
        self.assertIsNone(retry_after_seconds('not a date', now))

    def test_only_a_wait_that_was_read_is_kept_with_the_error(self):
        self.assertEqual(retain_retry_after(ArchiveError('x'), '30').retry_after_seconds, 30)
        self.assertFalse(hasattr(retain_retry_after(ArchiveError('x'), 'soon'), 'retry_after_seconds'))


if __name__ == '__main__':
    unittest.main()
