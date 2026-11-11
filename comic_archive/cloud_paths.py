"""Repair app-owned cloud connection paths after the enclosing library folder moves."""
from pathlib import Path

from .models import ArchiveError
from .storage import safe_root


def relocate(library, previous, shared):
    """Move references, never files. External configs retain the exact paths the person chose.

    The caller holds the library and shared-settings locks and updates the link
    last. Repeating this after an interrupted repair is safe.
    """
    library, previous, shared = safe_root(library), Path(previous), safe_root(shared)
    mappings = {previous / 'cloud': shared / 'cloud'}
    try:
        relative = library.relative_to(shared.parent)
    except ValueError:
        pass  # A changed link without moving the library is not a library relocation.
    else:
        mappings[previous.parent / relative / '.archive-state/cloud'] = library / '.archive-state/cloud'
    _rewrite(mappings, [shared / 'cloud/targets.json', library / '.archive-state/cloud/targets.json'], library)


def moved(library, before):
    """A library was renamed from 'before' with all it holds: the connections it keeps itself name their
    private files where they are now. Nothing may be writing in the library meanwhile; repeating is safe."""
    library = safe_root(library)
    _rewrite({Path(before) / '.archive-state/cloud': library / '.archive-state/cloud'},
             [library / '.archive-state/cloud/targets.json'], library)


def _rewrite(mappings, paths, library):
    from .cloud import _config, _json, _write

    def connection(value):
        if not isinstance(value, dict) or not isinstance(value.get('config'), str):
            return value
        old = Path(value['config'])
        destination = mappings.get(old.parent)
        if destination is None or destination == old.parent:
            return value
        candidate = destination / old.name
        if candidate.is_symlink():
            raise ArchiveError('Cloud authentication must not be symlinked.')
        if not candidate.is_file():
            return value  # A missing connection still needs sign-in; do not adopt another file.
        return value | {'config': str(_config(candidate, (destination,)))}

    for path in paths:
        if path.is_symlink():
            raise ArchiveError('Cloud settings must not be symlinked.')
        if path.is_file():
            value = _json(path)
            targets = value.get('targets')
            if isinstance(targets, dict):
                changed = value | {'targets': {name: connection(target) for name, target in targets.items()}}
                if changed != value:
                    _write(path, changed)
    tests = safe_root(library / '.archive-state/cloud/tests')
    for path in tests.glob('*.json'):
        try:
            value = _json(path)
        except ArchiveError:
            continue  # A receipt that cannot be read names no connection to repair, and must not hold up the rest.
        if isinstance(value.get('connection'), dict):
            changed = value | {'connection': connection(value['connection'])}
            if changed != value:
                _write(path, changed)
