"""Bounded image structure checks, without decoding or changing source bytes.

An image stream can end before its enclosing file does. Walk the format's
markers/chunks/blocks to its logical end; never search payloads for a footer or
infer completeness from physical EOF. These checks do not validate compressed
pixel data: image_validation supplies the separate, optional decoder check.
"""
import zlib


def _jpeg(data):
    offset, frame, scan, pixels, height = 2, False, False, False, False
    entropy = False
    while offset < len(data):
        if entropy:
            marker = data.find(b'\xff', offset)
            if marker < 0:
                return False
            pixels |= marker > offset
            offset = marker
        if data[offset] != 255:
            return False
        while offset < len(data) and data[offset] == 255:
            offset += 1
        if offset == len(data):
            return False
        code, offset = data[offset], offset + 1
        if entropy and code == 0:  # A stuffed FF byte, not a marker.
            pixels = True
            continue
        if entropy and (0xD0 <= code <= 0xD7 or code == 1):
            continue  # Restart and TEM markers have no length field.
        entropy = False
        if code == 0xD9:
            return frame and scan and pixels and height
        if code == 1:
            continue
        if code < 0xC0 or code == 0xD8 or 0xD0 <= code <= 0xD7 or offset + 2 > len(data):
            return False
        size = int.from_bytes(data[offset:offset + 2], 'big')
        end = offset + size
        if size < 2 or end > len(data):
            return False
        if 0xC0 <= code <= 0xCF and code not in (0xC4, 0xC8, 0xCC):
            if size < 8 or not data[offset + 7] or size != 8 + 3 * data[offset + 7]:
                return False
            if not int.from_bytes(data[offset + 5:offset + 7], 'big'):
                return False
            frame = True
            height = bool(int.from_bytes(data[offset + 3:offset + 5], 'big'))
        elif code == 0xDA:
            if not frame or size < 6 or not 1 <= data[offset + 2] <= 4 or size != 6 + 2 * data[offset + 2]:
                return False
            scan, entropy = True, True
        elif code == 0xDC:  # DNL supplies a frame height initially recorded as zero.
            if size != 4 or not scan or not int.from_bytes(data[offset + 2:end], 'big'):
                return False
            height = True
        offset = end
    return False


def _png(data):
    offset, header, image, palette, closed = 8, False, False, False, False
    colour = None
    while offset + 12 <= len(data):
        size = int.from_bytes(data[offset:offset + 4], 'big')
        kind = data[offset + 4:offset + 8]
        end = offset + 8 + size
        if size > 0x7fffffff or end + 4 > len(data):
            return False
        if zlib.crc32(memoryview(data)[offset + 4:end]) != int.from_bytes(data[end:end + 4], 'big'):
            return False
        if not header:
            if kind != b'IHDR' or size != 13:
                return False
            if not all(0 < int.from_bytes(data[start:start + 4], 'big') <= 0x7fffffff
                       for start in (offset + 8, offset + 12)):
                return False
            depth, colour, compression, filtering, interlace = data[offset + 16:offset + 21]
            depths = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
            if depth not in depths.get(colour, ()) or compression or filtering or interlace not in (0, 1):
                return False
            header = True
        elif kind == b'IHDR':
            return False
        elif kind == b'IEND':
            return size == 0 and image and (colour != 3 or palette)
        elif kind == b'IDAT':
            if closed or (colour == 3 and not palette):
                return False
            image |= size > 0
        else:
            closed |= image
            if kind == b'PLTE':
                if image or palette or colour in (0, 4) or not 0 < size <= 768 or size % 3:
                    return False
                palette = True
            elif not kind or not kind[0] & 32:  # Unknown critical chunks need a decoder that understands them.
                return False
        offset = end + 4
    return False


def _gif_blocks(data, offset):
    while offset < len(data):
        size, offset = data[offset], offset + 1
        if not size:
            return offset
        offset += size
        if offset > len(data):
            break
    return None


def _gif(data):
    if len(data) < 13 or not int.from_bytes(data[6:8], 'little') or not int.from_bytes(data[8:10], 'little'):
        return False
    packed = data[10]
    offset = 13 + (3 * (2 << (packed & 7)) if packed & 128 else 0)
    image = False
    while offset < len(data):
        code, offset = data[offset], offset + 1
        if code == 0x3B:
            return image
        if code == 0x21:
            if offset >= len(data):
                return False
            label, offset = data[offset], offset + 1
            if label == 0xF9 and (offset + 6 > len(data) or data[offset] != 4 or data[offset + 5] != 0):
                return False
        elif code == 0x2C:
            if offset + 9 > len(data) or not int.from_bytes(data[offset + 4:offset + 6], 'little') or not int.from_bytes(data[offset + 6:offset + 8], 'little'):
                return False
            packed = data[offset + 8]
            offset += 9 + (3 * (2 << (packed & 7)) if packed & 128 else 0)
            if offset + 1 >= len(data) or not 2 <= data[offset] <= 8 or not data[offset + 1]:
                return False
            offset += 1  # LZW minimum code size, followed by counted data sub-blocks.
            image = True
        else:
            return False
        offset = _gif_blocks(data, offset)
        if offset is None:
            return False
    return False


def _webp_chunks(data, offset, end, animation=True):
    image, extended, control = False, False, False
    while offset + 8 <= end:
        kind = data[offset:offset + 4]
        size = int.from_bytes(data[offset + 4:offset + 8], 'little')
        start = offset + 8
        following = start + size + (size & 1)
        if following > end or (size & 1 and data[following - 1] != 0):
            return False
        if kind == b'VP8 ':
            if size < 10 or data[start] & 1 or data[start + 3:start + 6] != b'\x9d\x01\x2a':
                return False
            if not all(int.from_bytes(data[p:p + 2], 'little') & 0x3fff for p in (start + 6, start + 8)):
                return False
            image = True
        elif kind == b'VP8L':
            if size < 5 or data[start] != 0x2f or data[start + 4] & 0xe0:
                return False
            image = True
        elif kind == b'VP8X':
            if not animation or size != 10:
                return False
            extended = True
        elif kind == b'ANIM':
            if not animation or not extended or size != 6:
                return False
            control = True
        elif kind == b'ANMF':
            if not animation or not control or size < 16 or not _webp_chunks(data, start + 16, start + size, False):
                return False
            image = True
        offset = following
    return image and offset == end


def image_stream_extension(data):
    """Identify a complete supported image structure; raise ValueError otherwise.

    Data after the first logical image/container end is preserved, not interpreted
    as image data. Compressed pixels must be checked separately when requested.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError('Image data must be bytes.')
    if data.startswith(b'\xff\xd8\xff') and _jpeg(data):
        return '.jpg'
    if data.startswith(b'\x89PNG\r\n\x1a\n') and _png(data):
        return '.png'
    if data.startswith((b'GIF87a', b'GIF89a')) and _gif(data):
        return '.gif'
    if len(data) >= 12 and data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        end = int.from_bytes(data[4:8], 'little') + 8
        if 12 <= end <= len(data) and _webp_chunks(data, 12, end):
            return '.webp'
    raise ValueError('Unsupported or incomplete image structure.')
