"""Durable queued-job stops, serialized with worker admission.

A queued stop never removes its request. The worker writes the terminal
receipt without invoking the task's account or source methods.
"""
import json
from pathlib import Path
import re

from .archive import atomic_json
from .archive_paths import directory
from .inventory import Inventory
from .locking import LibraryLock
from .models import ArchiveError


MIN_WORKER_VERSION = 35
OPERATIONS = {'sync', 'archive_discovered', 'archive_watched_series', 'archive_series_url'}


def admission_lock(session):
    return LibraryLock(Path(session) / 'worker/.admission')


def _task(task_id):
    if not isinstance(task_id,str) or not re.fullmatch(r'job-[a-f0-9]{24}',task_id):
        raise ArchiveError('Select an existing queued import task.')
    return task_id


def _read(path, maximum=16384):
    if any(p.is_symlink() for p in (path,*path.parents)):
        raise ArchiveError('Queued stop state is unavailable; existing files are retained.')
    try:
        if not path.is_file() or path.stat().st_size>maximum:raise ValueError
        value=json.loads(path.read_text())
        if not isinstance(value,dict):raise ValueError
        return value
    except (OSError,ValueError,RecursionError):
        raise ArchiveError('Queued stop state is invalid; existing files are retained.') from None


def queued_stop_requested(session,task_id):
    path=Path(session)/'worker/cancellations'/f'{_task(task_id)}.json'
    if any(p.is_symlink() for p in (path,*path.parents)):
        raise ArchiveError('Queued stop state is unavailable; existing files are retained.')
    if not path.exists():return False
    if _read(path,1024)!={'document_type':'queued_comic_import_stop','task_id':task_id}:
        raise ArchiveError('Queued stop state is invalid; existing files are retained.')
    return True


def can_stop_queued(session,task_id):
    task_id=_task(task_id);root=Path(session)/'worker'
    try:
        worker=_read(root/'status.json')
        if type(worker.get('worker_version')) is not int or worker['worker_version']<MIN_WORKER_VERSION:
            return False
        if (root/'responses'/f'{task_id}.json').exists():return False
        if worker.get('state')=='busy' and worker.get('task_id')==task_id:return False
        request=_read(root/'requests'/f'{task_id}.json')
        return (isinstance(request.get('operation'),str) and request['operation'] in OPERATIONS
                and isinstance(request.get('args'),dict))
    except ArchiveError:
        return False


def request_queued_stop(session,task_id):
    task_id=_task(task_id);root=Path(session)/'worker'
    with admission_lock(session):
        from .queue_client import task_status
        value=task_status(session,task_id)
        if value['status'] in {'ok','error'}:
            return {'task_id':task_id,'status':'already_finished'}
        if value['status']=='running':
            return {'task_id':task_id,'status':'already_started'}
        if not can_stop_queued(session,task_id):
            raise ArchiveError('Queued stopping requires a supporting worker and a pending import task.')
        path=root/'cancellations'/f'{task_id}.json'
        directory(path.parent).chmod(0o700)
        queued_stop_requested(session,task_id)  # Refuse an unsafe existing marker.
        atomic_json(path,{'document_type':'queued_comic_import_stop','task_id':task_id})
        path.chmod(0o600)
    return {'task_id':task_id,'status':'cancellation_requested'}


def end_unstarted(session,library,task_id):
    """Give a task that no worker got to the receipt of one stopped while queued; True when it was given.

    Only for when no worker is running. Left as it is, the request would be run by the next worker that starts.
    """
    task_id=_task(task_id);root=Path(session)/'worker'
    with admission_lock(session):
        request=root/'requests'/f'{task_id}.json';response=root/'responses'/f'{task_id}.json'
        if response.exists() or not request.is_file():return False
        operation=_read(request).get('operation')
        # A task that only checks what is saved has nothing to count; its receipt just says it did not run.
        result=(cancelled_result(library,operation) if operation in OPERATIONS
                else {'status':'cancelled','cancelled_before_start':True})
        atomic_json(response,{'task_id':task_id,'status':'ok','result':result})
        response.chmod(0o600)
    return True


def cancelled_result(library,operation):
    """Read queue counts only; do not create/finish an import run."""
    if operation not in OPERATIONS:
        raise ArchiveError('Only pending comic import tasks can be stopped.')
    result={'status':'cancelled','cancelled_before_start':True,'archived_episode_ids':[],
            'downloaded_pages':0,'downloaded_bytes':0,'downloaded_covers':0,
            'pending_episode_count':0,'inventory':{'scan_remaining_series_count':0},
            'inventory_complete':False}
    if (Path(library)/'.archive-state/inventory.sqlite3').is_file():
        with Inventory(library,read_only=True) as inventory:
            result['pending_episode_count']=len(inventory.pending())
            result['inventory']=inventory.status()
            result['inventory'].pop('last_run',None)
    return result
