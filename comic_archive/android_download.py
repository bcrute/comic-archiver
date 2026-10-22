"""Android Go admission and the one-time repair of the prototype's empty list scans."""
from . import account_export, account_ui, batches
from .archive import atomic_json
from .locking import LibraryLock
from .models import ArchiveError
from .queue_client import task_status


def start(workspace):
    # The Android HTTP reader is the only engine. Desktop fast mode would try to
    # launch a second browser, both for metadata and for episode archives.
    if account_ui.speed(workspace) == 'fast':
        account_ui.choose_speed(workspace, 'medium')
    root = batches._root(workspace.session, workspace.library)
    marker = workspace.session / 'android-reader-version.json'
    with LibraryLock(root / '.android-reader-upgrade'):
        if not marker.exists():
            with LibraryLock(root / '.account-export-state'):
                value = account_export._load(root, account_export.lists(workspace.session))
                if (value and value['phase'] == 'lists' and value['status'] in {'held', 'stopped'}
                        and any(not check['complete'] and check['observed_series_count'] == 0
                                for check in value['list_checks'])):
                    # The old anchor-only parser recorded the mobile lists as empty,
                    # then RECENT failed with HTTP400. Re-read those lists on explicit
                    # Go. Keep old task receipts and every inventory/archive intact.
                    pending = value['pending']
                    if pending:
                        response = task_status(workspace.session, pending['task_id'])
                        if response['status'] not in {'ok', 'error'}:
                            raise ArchiveError('The previous download is still stopping; try Go again shortly.')
                        value['last_failed_task_id'] = pending['task_id']
                    value.update(category_index=0, list_checks=[], pending=None)
                    account_export._save(root, value)
            atomic_json(marker, {'version': 1})
            marker.chmod(0o600)
    account_ui.start(workspace)
