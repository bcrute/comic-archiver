"""Run-scoped cooperative stops; completed exports and cached pages survive."""
import json
from pathlib import Path
import re

from .archive import atomic_json
from .archive_paths import directory
from .errors import RunLimit
from .inventory import Inventory
from .locking import LibraryLock
from .models import ArchiveError


class RunCancelled(RunLimit):
    pass


class RunCancellation:
    def __init__(self,root,run_id):
        if not isinstance(run_id,str) or not re.fullmatch(r'[a-f0-9]{24}',run_id):
            raise ArchiveError('Select the current import run before requesting a stop.')
        self.root=Path(root)
        self.run_id=run_id
        self.path=self.root/'.archive-state/cancellations'/f'{run_id}.json'

    def requested(self):
        if any(p.is_symlink() for p in (self.path,*self.path.parents)):
            raise ArchiveError('Import stop state is unavailable; existing files are retained.')
        if not self.path.exists():
            return False
        try:
            if not self.path.is_file() or self.path.stat().st_size>1024:
                raise ValueError
            value=json.loads(self.path.read_text())
            if value!={'document_type':'comic_import_stop','run_id':self.run_id}:
                raise ValueError
        except (OSError,ValueError):
            raise ArchiveError('Import stop state is invalid; existing files are retained.') from None
        return True

    def check(self):
        if self.requested():
            raise RunCancelled('This import was stopped; completed archives and cached pages are retained.')


def request_stop(root,run_id):
    cancellation=RunCancellation(root,run_id)
    # A separate short lock leaves the active import writer undisturbed.
    with LibraryLock(Path(root)/'.archive-state/.stop-state'):
        with Inventory(root,read_only=True) as inventory:
            last=inventory.db.execute('SELECT id,status FROM runs ORDER BY started_at DESC LIMIT 1').fetchone()
        from .monitoring import _read
        progress=_read(Path(root)/'.archive-state/progress.json')
        if last and last['id']==run_id and last['status']!='running':
            status=last['status'] if last['status'] in {'finished','limited','paused','interrupted','login_required','cancelled'} else 'finished'
            return {'run_id':run_id,'status':status,'stop_requested':False}
        if (not last or last['id']!=run_id or last['status']!='running'
                or progress.get('run_id')!=run_id or progress.get('cancellation_supported') is not True):
            raise ArchiveError('That import is no longer running or needs the updated worker code.')
        directory(cancellation.path.parent).chmod(0o700)
        cancellation.requested()  # Reject an existing unsafe/malformed marker.
        atomic_json(cancellation.path,{'document_type':'comic_import_stop','run_id':run_id})
        cancellation.path.chmod(0o600)
    return {'run_id':run_id,'status':'cancellation_requested'}
