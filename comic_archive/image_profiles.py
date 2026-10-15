"""Explicit, bounded device image copies. Original mode preserves source bytes."""
import io
import warnings
from .models import ArchiveError

PROFILES = {'original': None, 'balanced': (1600, 82), 'small': (1080, 65)}


def validate_profile(profile):
    if not isinstance(profile, str) or profile not in PROFILES:
        raise ArchiveError('Choose original, balanced or small image quality.')
    return profile


def prerequisites(profile):
    validate_profile(profile)
    if profile != 'original':
        try:
            from PIL import features
            if not features.check('webp'):
                raise ImportError
        except ImportError:
            raise ArchiveError('Device compression requires Pillow with WebP support; install the validation extra.') from None


def compress_image(data, profile):
    validate_profile(profile)
    if profile == 'original':
        return data
    prerequisites(profile)
    from PIL import Image
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                # Keep animation, unusual colour modes and oversized images
                # intact instead of flattening or allocating unbounded pixels.
                if getattr(image, 'n_frames', 1) > 1 or image.width * image.height > 24_000_000:
                    return data
                if image.mode not in ('RGB', 'RGBA', 'L', 'LA', 'P'):
                    return data
                if image.width <= 32 and image.height <= 32:
                    return data
                width, quality = PROFILES[profile]
                image.load()
                converted = image.convert('RGBA' if 'A' in image.getbands() or 'transparency' in image.info else 'RGB')
                if converted.width > width:
                    converted = converted.resize((width, max(1, round(converted.height * width / converted.width))), Image.Resampling.LANCZOS)
                if converted.height > 16383:
                    return data
                output = io.BytesIO()
                converted.save(output, 'WEBP', quality=quality, method=4,
                               icc_profile=image.info.get('icc_profile', b''))
                result = output.getvalue()
                # A smaller profile never increases a page's storage cost.
                return result if len(result) < len(data) else data
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ArchiveError('Image cannot be safely decoded for device compression; source bytes remain cached.') from None
