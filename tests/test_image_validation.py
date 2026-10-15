import importlib.util
import io
import struct
import sys
import unittest
import zlib
from unittest.mock import patch
from comic_archive.image_validation import decoded_image_checker
from comic_archive.models import ArchiveError


def white_png():
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', 2, 2, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b'\0' + b'\xff' * 6 + b'\0' + b'\xff' * 6))
            + chunk(b'IEND', b''))


class ImageValidationTests(unittest.TestCase):
    def test_missing_optional_dependency_has_actionable_safe_message(self):
        with patch.dict(sys.modules, {'PIL': None}):
            with self.assertRaises(ArchiveError) as error:
                decoded_image_checker()
        self.assertIn('.[validation]', str(error.exception))
        self.assertNotIn('Traceback', str(error.exception))

    @unittest.skipUnless(importlib.util.find_spec('PIL'), 'Optional Pillow is not installed')
    def test_valid_white_image_is_accepted_without_mutation(self):
        data = white_png()
        original = bytes(data)
        decoded_image_checker()(data)
        self.assertEqual(data, original)

    @unittest.skipUnless(importlib.util.find_spec('PIL'), 'Optional Pillow is not installed')
    def test_jpeg_signature_alone_does_not_pass_pixel_decoding(self):
        with self.assertRaises(ArchiveError) as error:
            decoded_image_checker()(b'\xff\xd8\xffPRIVATE_IMAGE_BYTES\xff\xd9')
        self.assertEqual(str(error.exception), 'Image pixels could not be decoded.')
        self.assertNotIn('PRIVATE', str(error.exception))

    @unittest.skipUnless(importlib.util.find_spec('PIL'), 'Optional Pillow is not installed')
    def test_corrupt_png_crc_is_rejected_despite_complete_signature(self):
        data = bytearray(white_png())
        position = data.index(b'IDAT')
        size = int.from_bytes(data[position-4:position], 'big')
        data[position + 4 + size] ^= 1
        with self.assertRaises(ArchiveError):
            decoded_image_checker()(bytes(data))

    @unittest.skipUnless(importlib.util.find_spec('PIL'), 'Optional Pillow is not installed')
    def test_animation_frames_are_all_loaded(self):
        from PIL import Image
        frames = [Image.new('RGB', (2, 2), color) for color in ('white', 'black')]
        output = io.BytesIO()
        frames[0].save(output, format='GIF', save_all=True, append_images=frames[1:])
        checker = decoded_image_checker()
        from PIL import ImageSequence
        iterator = ImageSequence.Iterator
        visited = []
        def tracking(image):
            for frame in iterator(image):
                visited.append(frame.tell())
                yield frame
        with patch('PIL.ImageSequence.Iterator', side_effect=tracking):
            checker(output.getvalue())
        self.assertEqual(visited, [0, 1])
