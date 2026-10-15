import importlib.util
import io
import random
import sys
import unittest
from unittest.mock import patch

from comic_archive.image_profiles import compress_image, prerequisites, validate_profile
from comic_archive.models import ArchiveError

HAS_PILLOW = importlib.util.find_spec('PIL') is not None


def encoded(image, format='PNG', **options):
    stream = io.BytesIO()
    image.save(stream, format, **options)
    return stream.getvalue()


class ImageProfileTests(unittest.TestCase):
    def test_missing_pillow_is_reported_before_any_page_is_touched(self):
        prerequisites('original')
        with patch.dict(sys.modules, {'PIL': None}):
            for call in (lambda: prerequisites('small'), lambda: compress_image(b'page bytes', 'balanced')):
                with self.assertRaisesRegex(ArchiveError, 'Pillow with WebP support'):
                    call()
            self.assertEqual(compress_image(b'page bytes', 'original'), b'page bytes')
        with self.assertRaises(ArchiveError):
            validate_profile('tiny')

    @unittest.skipUnless(HAS_PILLOW, 'Optional Pillow is not installed')
    def test_pillow_without_webp_support_is_reported_as_a_missing_prerequisite(self):
        with patch('PIL.features.check', return_value=False):
            with self.assertRaisesRegex(ArchiveError, 'WebP'):
                prerequisites('balanced')

    @unittest.skipUnless(HAS_PILLOW, 'Optional Pillow is not installed')
    def test_unusual_colour_modes_and_pages_taller_than_webp_allows_keep_their_original_bytes(self):
        from PIL import Image
        # Detailed enough that converting to WebP would be far smaller, so only the colour-mode refusal keeps the bytes.
        cmyk = encoded(Image.frombytes('CMYK', (512, 512), random.Random(3).randbytes(512 * 512 * 4)), 'JPEG', quality=95)
        self.assertEqual(compress_image(cmyk, 'balanced'), cmyk)
        strip = encoded(Image.new('L', (40, 16384)))
        self.assertEqual(compress_image(strip, 'small'), strip)
