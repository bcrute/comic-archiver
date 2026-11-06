"""Real encoded fixtures exercise format boundaries, not physical EOF guesses."""
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
import zipfile
import zlib

from PIL import Image

from comic_archive.archive import image_extension
from comic_archive.archive import digest, sidecar_path, verify, verify_series_contents
from comic_archive.image_formats import image_stream_extension
from comic_archive.image_validation import decoded_image_checker
from comic_archive.models import ArchiveError, Episode, Manifest, Page, Series
from comic_archive.publication import PublicationStore, verify_compressed_comic, write_compressed_comic
from scripts.offline_restore import image_stream_extension as standalone_extension
from scripts.offline_restore import restore
from test_archive import MemoryAdapter


def encoded(format, **options):
    output = io.BytesIO()
    Image.new('RGB', (8, 8), 'red').save(output, format, **options)
    return output.getvalue()


def png_chunk(kind, payload):
    return struct.pack('>I', len(payload)) + kind + payload + struct.pack('>I', zlib.crc32(kind + payload))


class ImageFormatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.images = [(encoded('JPEG'), '.jpg'), (encoded('JPEG', progressive=True), '.jpg'),
                      (encoded('PNG'), '.png'), (encoded('GIF'), '.gif'),
                      (encoded('WEBP'), '.webp'), (encoded('WEBP', lossless=True), '.webp')]
        for format, extension in (('PNG', '.png'), ('GIF', '.gif'), ('WEBP', '.webp')):
            output = io.BytesIO()
            frames = [Image.new('RGB', (8, 8), colour) for colour in ('red', 'blue')]
            frames[0].save(output, format, save_all=True, append_images=frames[1:], duration=100, loop=0)
            cls.images.append((output.getvalue(), extension))

    def test_diagnostics_explain_rejection_and_preserved_tails_without_source_secrets(self):
        from comic_archive.archive import image_context
        from comic_archive.image_formats import ImageStructureError, inspect_image_structure
        from comic_archive.support_log import DOWNLOAD_LOG
        context = image_context('webtoon', '42', '81', 3)
        original = encoded('JPEG')
        for tail, kind in ((b'\0' * 61892, 'zero'), (b'PRIVATE?token=secret', 'nonzero'), (b'\0secret', 'mixed')):
            with self.assertLogs(DOWNLOAD_LOG, level='INFO') as logs:
                self.assertEqual(image_extension(original + tail, context=context), '.jpg')
            line = ' '.join(logs.output)
            for expected in ('Image accepted', 'source=webtoon series=42 episode=81 page=3',
                             f'bytes={len(original + tail)}', f'image_end={len(original)}',
                             f'trailing_bytes={len(tail)}', f'trailing_kind={kind}',
                             f'sha256={digest(original + tail)}', 'original_bytes=preserved'):
                self.assertIn(expected, line)
            self.assertNotIn('secret', line)
            self.assertNotIn('PRIVATE', line)
        cases = [(original[:-2], 'jpeg', 'missing_eoi'),
                 (b'\xff\xd8\xff\xe1\xff\xffPRIVATE', 'jpeg', 'invalid_segment_length'),
                 (b'<html>PRIVATE?token=secret</html>', 'unknown', 'unsupported_signature'),
                 (b'RIFF\xff\xff\xff\xffWEBP', 'webp', 'invalid_riff_length')]
        png = bytearray(encoded('PNG')); png[png.index(b'IDAT') + 4] ^= 1
        cases.append((bytes(png), 'png', 'crc_mismatch'))
        for data, kind, reason in cases:
            with self.subTest(reason=reason), self.assertLogs(DOWNLOAD_LOG) as logs:
                with self.assertRaises(ArchiveError) as raised:
                    image_extension(data, context=context)
            line = ' '.join(logs.output)
            for expected in (context, f'format={kind}', f'bytes={len(data)}', 'offset=', f'reason={reason}'):
                self.assertIn(expected, line)
                self.assertIn(expected, str(raised.exception) if expected != context else line)
            self.assertNotIn('PRIVATE', line)
            self.assertNotIn('secret', line)
            with self.assertRaises(ImageStructureError) as raised:
                inspect_image_structure(data)
            self.assertLessEqual(raised.exception.offset, len(data))
        unsafe = image_context('webtoon\ninjected', 'https://private/path', 'token=secret', -1)
        self.assertEqual(unsafe, 'source=unknown series=unknown episode=unknown page=unknown')

    def test_page_download_logs_episode_and_page_context_before_generic_job_handling(self):
        from comic_archive.support_log import DOWNLOAD_LOG
        for direct in (False, True):
            with self.subTest(direct=direct), tempfile.TemporaryDirectory() as folder:
                store = PublicationStore(Path(folder))
                try:
                    if direct:
                        from comic_archive.publication import DirectEpisode
                        store._direct = DirectEpisode(Path(folder) / 'episode.cbz')
                    episode = Episode('81', '42', 'Private title', 1,
                                      tuple(Page(i, f'https://cdn.tapas.io/page{i}', False) for i in range(1, 4)),
                                      'tapas_free')
                    page = Page(3, 'https://cdn.tapas.io/PRIVATE?token=secret', False)
                    with self.assertLogs(DOWNLOAD_LOG) as logs, self.assertRaises(ArchiveError):
                        store.remember_page('tapas', episode, page, b'PRIVATE malformed payload')
                    line = ' '.join(logs.output)
                    self.assertIn('source=tapas series=42 episode=81 page=3', line)
                    self.assertIn('reason=unsupported_signature', line)
                    self.assertNotIn('PRIVATE', line)
                    self.assertNotIn('secret', line)
                finally:
                    store.close()

    def test_cover_format_must_match_its_name_even_when_size_and_hash_match(self):
        data = encoded('JPEG') + b'nonzero tail'
        metadata = {'source': 'tapas', 'source_series_id': '42', 'title': 'Series',
                    'cover_file': 'cover.png', 'cover_size': len(data), 'cover_sha256': digest(data)}
        self.assertEqual(verify_series_contents(metadata, data), ['Cover image format differs from metadata'])
        self.assertEqual(verify_series_contents(metadata | {'cover_file': 'cover.jpg'}, data), [])

    def accepted(self, data, extension, *, decode=True):
        for detector in (image_extension, image_stream_extension, standalone_extension):
            self.assertEqual(detector(data), extension)
        if decode:
            decoded_image_checker()(data)

    def refused(self, data):
        for detector, error in ((image_extension, ArchiveError), (image_stream_extension, ValueError),
                                (standalone_extension, ValueError)):
            with self.assertRaises(error):
                if detector is image_extension:
                    with self.assertLogs('comic_archive.download'):
                        detector(data)
                else:
                    detector(data)

    def test_all_supported_streams_accept_uninterpreted_trailing_bytes_without_rewriting_them(self):
        for data, extension in self.images:
            for tail in (b'', b'\0', b'\0' * 262144, b' \r\n\xff', b'metadata\0not-zero', data):
                with self.subTest(format=extension, tail=tail[:16]):
                    original = data + tail
                    self.accepted(original, extension)
                    self.assertEqual(original, data + tail)

    def test_every_truncation_of_real_streams_is_refused(self):
        for data, extension in self.images:
            for length in range(len(data)):
                with self.subTest(format=extension, length=length):
                    self.refused(data[:length])

    def test_original_tails_survive_caching_publication_hashes_restore_and_compressed_verification(self):
        for encoded_data, extension in self.images:
            with self.subTest(format=extension), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / 'Library'
                data = encoded_data + b'\xff\0source metadata, preserved verbatim\r\n'
                adapter = MemoryAdapter()
                adapter.retrieve = lambda page: data
                series = Series('42', 'Series')
                episode = Episode('81', '42', 'Episode', 1, (Page(1, 'https://cdn.tapas.io/image', False),),
                                  'tapas_free', 'https://tapas.io/episode/81')
                store = PublicationStore(root)
                try:
                    folder = store.archive(Manifest(series, (episode,)), adapter, progress=lambda _: None)
                    store.write_series_metadata(series, cover=data)
                finally:
                    store.close()
                cbz = next(folder.glob('*.cbz'))
                metadata = json.loads(sidecar_path(cbz).read_text())
                member = '0001' + extension
                self.assertEqual(metadata['content_hashes'], [{'file': member, 'size': len(data), 'sha256': digest(data)}])
                with zipfile.ZipFile(cbz) as archive:
                    self.assertEqual(archive.read(member), data)
                self.assertEqual(verify(root, decode_images=True), [])
                restored = Path(temporary) / 'Restored'
                self.assertEqual(restore(root, restored, decode=True)['original_image_bytes'], len(data))
                with zipfile.ZipFile(next(restored.glob('**/*.cbz'))) as archive:
                    self.assertEqual(archive.read(member), data)
                compressed = Path(temporary) / 'episode.tar.zst'
                write_compressed_comic(compressed, metadata, lambda page: io.BytesIO(data))
                with compressed.open('rb') as stream:
                    self.assertEqual(verify_compressed_comic(stream, metadata, image_decoder=decoded_image_checker()), [])

    def test_jpeg_skips_end_markers_in_metadata_and_handles_fill_stuffing_and_multiple_scans(self):
        for data, extension in self.images[:2]:
            payload = b'Comment contains \xff\xd9 and \xff\xda but is not an image end.'
            comment = b'\xff\xfe' + (len(payload) + 2).to_bytes(2, 'big') + payload
            with_comment = data[:2] + comment + data[2:]
            self.accepted(with_comment, extension)
            self.accepted(with_comment[:-2] + b'\xff\xff\xff\xd9tail', extension)
            self.refused(with_comment[:-2])
            # A fake end marker occurs inside a declared metadata payload.
            self.refused(data[:2] + b'\xff\xe1\0\x08abcd\xff\xd9')
            self.refused(data[:2] + b'\xff\xe1\0\x01' + data[2:])
            self.refused(data[:2] + b'\xff\xe1\xff\xff' + data[2:])
        data = encoded('JPEG', restart_marker_blocks=1)
        self.accepted(data + b'tail', '.jpg')
        # Stuffed FF00 is scan payload, never an end marker.
        data = self.images[0][0]
        self.refused(data[:-2] + b'\xff\0\xd9')
        self.refused(b'\xff\xd8\xff\xd9')  # Tables/header alone do not make an image.

    def test_png_validates_chunk_boundaries_and_crcs_instead_of_searching_for_iend(self):
        data = encoded('PNG')
        comment = png_chunk(b'tEXt', b'Comment\0IEND is ordinary text here')
        augmented = data[:-12] + comment + data[-12:]
        self.accepted(augmented + b'tail', '.png')
        self.refused(augmented[:-12])
        broken = bytearray(data)
        broken[data.index(b'IDAT') + 4] ^= 1
        self.refused(bytes(broken))
        self.refused(data[:8] + png_chunk(b'IEND', b''))
        self.refused(data[:-12] + png_chunk(b'IEND', b'not-empty'))
        self.refused(data[:33] + data[:33][8:] + data[33:])

    def test_gif_counts_sub_blocks_and_colour_tables_instead_of_searching_for_a_semicolon(self):
        data = encoded('GIF')
        with_comment = data[:-1] + b'\x21\xfe\x03a;b\0' + data[-1:]
        self.accepted(with_comment + b'tail', '.gif')
        self.refused(with_comment[:-1])
        self.refused(data[:-1] + b'\x21\xfe\xff;')
        broken = bytearray(data)
        broken[10] = 0x87  # Declares a global colour table larger than this file.
        self.refused(bytes(broken))
        self.refused(b'GIF89a' + b'\0' * 10 + b';')

    def test_webp_checks_declared_chunks_and_animation_frames_not_the_enclosing_file_size(self):
        data = encoded('WEBP')
        comment = b'EXIF' + (3).to_bytes(4, 'little') + b'abc\0'
        extended = data[:4] + (len(data) + len(comment) - 8).to_bytes(4, 'little') + data[8:] + comment
        self.accepted(extended + b'tail', '.webp')
        self.refused(extended[:-1])
        broken = bytearray(data)
        broken[16:20] = (len(data) + 1).to_bytes(4, 'little')
        self.refused(bytes(broken))
        broken = bytearray(data)
        broken[4:8] = (len(data) + 1).to_bytes(4, 'little')
        self.refused(bytes(broken))
        self.refused(b'RIFF\x04\0\0\0WEBP')
        animated = self.images[-1][0]
        self.refused(animated[:-2])

    def test_structure_identification_does_not_claim_to_decode_compressed_pixels(self):
        data = encoded('JPEG')
        # Remove all compressed scan bytes, retain one invalid byte and the real EOI.
        scan = data.index(b'\xff\xda') + 2
        start = scan + int.from_bytes(data[scan:scan + 2], 'big')
        broken_pixels = data[:start] + b'\xff\0' + b'\xff\xd9'
        self.accepted(broken_pixels, '.jpg', decode=False)
        # Existing full pixel validation remains a separate, opt-in guarantee.
        with self.assertRaises(ArchiveError):
            decoded_image_checker()(b'\xff\xd8\xffPRIVATE\xff\xd9')


if __name__ == '__main__':
    unittest.main()
