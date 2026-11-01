"""The license texts the desktop app carries, for the About tab of its settings.

Comic Archiver's own license and the third-party notices come with its package. The built app keeps every other
text in its licenses folder, beside its Python: one folder per package, and the texts no package brings in other/.
"""
from importlib import metadata
from pathlib import Path
import sys

OWN = (('LICENSE', 'Comic Archiver (MIT license)'), ('THIRD-PARTY-NOTICES.md', 'Third-party notices'))
SOURCE = Path(__file__).resolve().parents[1]


def version():
    try:
        return metadata.version('comic-archive')
    except metadata.PackageNotFoundError:
        return 'unknown'


def folder(prefix=None):
    """The app's licenses folder, or None where there is none, as when it runs from its source."""
    place = Path(sys.prefix if prefix is None else prefix).parent / 'licenses'
    return place if place.is_dir() else None


def title(relative):
    """What a text in the licenses folder is called: other/LGPL-3.0.txt is LGPL-3.0, a package's text bears its name."""
    parts = Path(relative).parts
    if parts[0] == 'other':
        return Path(relative).stem
    return f"{parts[0].removesuffix('.dist-info')}: {parts[-1]}"


def own(name):
    """Comic Archiver's own license or notices, from its installed package or, run from the source, from there."""
    try:
        text = metadata.distribution('comic-archive').read_text('licenses/' + name)
    except metadata.PackageNotFoundError:
        text = None
    if text is None and (SOURCE / name).is_file():
        text = (SOURCE / name).read_text('utf-8')
    return text


def texts(prefix=None):
    """Every text the app carries, as (title, path) pairs in reading order; the path of the app's own two is their name."""
    found = [(heading, name) for name, heading in OWN]
    place = folder(prefix)
    if place is not None:
        # Comic Archiver's own package brings its license there too; it is already first.
        files = sorted(path.relative_to(place) for path in place.rglob('*')
                       if path.is_file() and not path.relative_to(place).parts[0].startswith('comic_archive-'))
        # The texts no package brings come first: Qt's licenses and Python's, the ones people look for.
        files.sort(key=lambda path: path.parts[0] != 'other')
        found += [(title(path), str(place / path)) for path in files]
    return found


def read(path):
    """The text at a path from texts(), or None if it cannot be read."""
    if path in dict((name, name) for name, _ in OWN):
        return own(path)
    try:
        return Path(path).read_text('utf-8', 'replace')
    except OSError:
        return None
