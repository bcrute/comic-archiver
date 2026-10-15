"""Optional local pixel decoding; never changes or re-encodes image bytes."""
import io
import shutil
import warnings
from .models import ArchiveError


def decoded_image_checker():
    try:
        from PIL import Image, ImageSequence
    except ImportError:
        command = "uv pip install -e '.[validation]'" if shutil.which('uv') else "python -m pip install -e '.[validation]'"
        raise ArchiveError('Image decoding requires Pillow. Install it with: ' + command) from None

    def check(data):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as image:
                    image.verify()
                with Image.open(io.BytesIO(data)) as image:
                    for frame in ImageSequence.Iterator(image):
                        frame.load()
                        if frame.width < 1 or frame.height < 1:
                            raise ValueError('Invalid image dimensions')
        except Exception:
            raise ArchiveError('Image pixels could not be decoded.') from None

    return check
