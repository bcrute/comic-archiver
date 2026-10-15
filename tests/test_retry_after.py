import unittest
from datetime import datetime, timezone

from comic_archive.retry_after import retry_after_seconds


class RetryAfterTests(unittest.TestCase):
    def test_a_date_without_a_known_timezone_is_ignored_instead_of_guessed(self):
        now = datetime(2026, 10, 8, 3, 0, tzinfo=timezone.utc)
        self.assertEqual(retry_after_seconds('Thu, 08 Oct 2026 03:02:00 GMT', now), 120)
        self.assertIsNone(retry_after_seconds('Thu, 08 Oct 2026 03:02:00 -0000', now))


if __name__ == '__main__':
    unittest.main()
