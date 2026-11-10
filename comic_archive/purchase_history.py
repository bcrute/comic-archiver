"""Bounded purchase discovery with an account-bound, transactional inbox.

History discovers candidates. The normal episode reader still verifies access.
Only the normal history pager's verified index may be retained as a cursor.
"""
import hashlib
import json
import logging
import re

from .errors import AuthenticationError, MetadataError
from .inventory import timestamp
from .models import ArchiveError

KEY = 'purchase_history_v1'
LOG = logging.getLogger('comic_archive.download')
LINK = re.compile(r'https://tapas\.io/(?:series/[A-Za-z0-9_-]{1,100}(?:/info)?|episode/[1-9][0-9]{0,19})')
MAX_PAGES = 10000
CODES = {None, 'history_reader_unavailable', 'history_view_unverified', 'history_rows_unverified',
         'history_page_repeated', 'history_continuation_unverified', 'history_association_unresolved'}


def continuation_url(cursor):
    """The provider's normal pager uses an integer index, not a navigable link."""
    if (not isinstance(cursor, dict) or set(cursor) != {'contract', 'index'}
            or cursor['contract'] != 'tapas-transactions-index-v1'
            or type(cursor['index']) is not int or not 0 <= cursor['index'] < MAX_PAGES):
        raise MetadataError('The saved history continuation is invalid.')
    index = cursor['index']
    return f"https://tapas.io/profile/transactions?to_prev={'false' if index == 0 else 'true'}&index={index}"


def state(inventory):
    row = inventory.db.execute('SELECT value FROM settings WHERE key=?', (KEY,)).fetchone()
    if row is None:
        return {}
    try:
        if len(row[0]) > 16384:
            raise ValueError
        value = json.loads(row[0])
        if (not isinstance(value, dict) or value.get('version') != 1
                or value.get('status') not in {'pending', 'partial', 'checked'}
                or not isinstance(value.get('round'), str) or not 0 < len(value['round']) <= 100
                or not isinstance(value.get('binding'), str) or not re.fullmatch(r'[a-f0-9]{64}', value['binding'])
                or any(type(value.get(key)) is not int or value[key] < 0 for key in ('pages', 'discovered', 'unresolved'))
                or type(value.get('unresolved_rows', 0)) is not int or value.get('unresolved_rows', 0) < 0
                or value.get('terminal_status') not in {None, 'partial', 'checked'} or value.get('code') not in CODES):
            raise ValueError
        if value.get('cursor') is not None:
            continuation_url(value['cursor'])
        return value
    except (ValueError, TypeError):
        raise MetadataError('The saved purchase-history checkpoint is invalid; it has been retained.') from None


def pending(inventory):
    return state(inventory).get('status') == 'pending'


def summary(inventory):
    value = state(inventory)
    return {key: value[key] for key in ('status', 'code', 'pages', 'discovered', 'unresolved', 'checked_at') if key in value}


def save(inventory, value):
    inventory.db.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                         (KEY, json.dumps(value, sort_keys=True)))
    inventory.db.commit()


def reconcile(adapter, inventory, round_id, check, *, pages=2, links=20):
    """Commit each page before resolving its links; a fresh worker can resume it.

    A blocked cursor ends this round's history attempt, not the normal downloads.
    Source/network errors propagate to the existing controller's cooldown logic.
    """
    if not isinstance(round_id, str) or not round_id or len(round_id) > 100:
        raise MetadataError('Purchase history needs an explicit download-round identity.')
    if type(pages) is not int or not 1 <= pages <= 10 or type(links) is not int or not 1 <= links <= 100:
        raise MetadataError('Purchase-history page and association budgets must be bounded positive integers.')
    binding = inventory.db.execute("SELECT value FROM settings WHERE key='account_binding'").fetchone()
    if binding is None:
        raise AuthenticationError('Purchase history requires a library bound to the signed-in account.')
    inventory.db.executescript('''CREATE TABLE IF NOT EXISTS purchase_history_links (
        binding TEXT NOT NULL, url TEXT NOT NULL, resolved INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(binding,url));
        CREATE INDEX IF NOT EXISTS purchase_history_inbox ON purchase_history_links(binding,resolved,url);
        CREATE TABLE IF NOT EXISTS purchase_history_pages (
        round_id TEXT NOT NULL, fingerprint TEXT NOT NULL, PRIMARY KEY(round_id,fingerprint));''')
    value = state(inventory)
    if value and value.get('binding') != binding[0]:
        raise AuthenticationError('Purchase-history progress belongs to another account.')
    if value.get('round') != round_id:
        inventory.db.execute('DELETE FROM purchase_history_pages')
        inventory.db.execute('UPDATE purchase_history_links SET resolved=0 WHERE binding=? AND resolved<0', (binding[0],))
        value = {'version': 1, 'binding': binding[0], 'round': round_id, 'status': 'pending',
                 'cursor': None, 'pages': 0, 'discovered': 0, 'unresolved': 0}
        value['discovered'] = inventory.db.execute('SELECT COUNT(*) FROM purchase_history_links WHERE binding=?',
                                                  (binding[0],)).fetchone()[0]
        save(inventory, value)
    if value['status'] != 'pending':
        return summary(inventory) | {'pages_read': 0, 'series_added': 0}
    if not callable(getattr(adapter, 'purchase_history_page', None)):
        value.update(status='partial', code='history_reader_unavailable', checked_at=timestamp())
        save(inventory, value)
        LOG.info('purchase history: status=partial code=history_reader_unavailable')
        return summary(inventory) | {'pages_read': 0, 'series_added': 0}
    pages_read = added = processed = 0

    def resolve():
        nonlocal added, processed
        rows = inventory.db.execute('SELECT url FROM purchase_history_links WHERE binding=? AND resolved=0 LIMIT ?',
                                    (binding[0], links)).fetchall()
        for row in rows:
            check()
            try:
                identity = adapter.purchase_series_identity(row[0])
            except ArchiveError as error:
                if not isinstance(error, MetadataError) and getattr(error, 'source_http_status', None) not in {404, 410}:
                    raise
                LOG.info('purchase history: association=unresolved code=history_association_invalid')
                identity = None
            if identity is None:
                inventory.db.execute('UPDATE purchase_history_links SET resolved=-1 WHERE binding=? AND url=?',
                                     (binding[0], row[0]))
            else:
                key, title = identity.get('source_series_id'), identity.get('title')
                if not isinstance(key, str) or not re.fullmatch(r'[1-9][0-9]{0,19}', key) or not isinstance(title, str):
                    raise MetadataError('Purchase history returned an invalid series association.')
                known = inventory.db.execute('SELECT 1 FROM series WHERE id=?', (key,)).fetchone()
                inventory.observe(key, title, [], 'transaction_history')
                # A newly associated purchase can arrive after this series was
                # checked. Reconcile it once; previously resolved links skip it.
                inventory.db.execute('DELETE FROM series_scans WHERE series_id=?', (key,))
                inventory.db.execute('UPDATE purchase_history_links SET resolved=1 WHERE binding=? AND url=?',
                                     (binding[0], row[0]))
                added += int(known is None)
            inventory.db.commit()
            processed += 1
        return bool(inventory.db.execute('SELECT 1 FROM purchase_history_links WHERE binding=? AND resolved=0',
                                         (binding[0],)).fetchone())

    inbox_pending = resolve()
    if not inbox_pending and value.get('terminal_status'):
        value['status'] = value.pop('terminal_status')
    elif not inbox_pending:
        for _ in range(pages):
            check()
            try:
                page = adapter.purchase_history_page(value['cursor'])
            except MetadataError as error:
                from .adapters.tapas_history import REASONS
                reason = getattr(error, 'history_reason', None)
                reason = reason if isinstance(reason, str) and reason in REASONS else 'metadata_unverified'
                expected_index = 0 if value['cursor'] is None else value['cursor']['index']
                LOG.info('purchase history: page_read=unverified page_index=%s reason=%s committed_pages=%s',
                         expected_index, reason, value['pages'])
                value.update(status='partial', code='history_rows_unverified')
                break
            if not isinstance(page, dict):
                value.update(status='partial', code='history_rows_unverified')
                break
            if page.get('authenticated') is not True:
                raise AuthenticationError('Purchase history could not verify the signed-in account.')
            if page.get('scope_verified') is not True:
                value.update(status='partial', code='history_view_unverified')
                break
            found, identifiers = page.get('links'), page.get('record_ids')
            if (not isinstance(found, list) or len(found) > 1000 or any(not isinstance(url, str) or not LINK.fullmatch(url) for url in found)
                    or not isinstance(identifiers, list) or (not identifiers and page.get('empty_verified') is not True)
                    or len(identifiers) > 1000
                    or any(not isinstance(key, str) or not key or len(key) > 200 for key in identifiers)):
                LOG.info('purchase history: page_read=unverified reason=page_records_unverified '
                         'records=%s links=%s verified_empty=%s committed_pages=%s',
                         len(identifiers) if isinstance(identifiers, list) else -1,
                         len(found) if isinstance(found, list) else -1,
                         page.get('empty_verified') is True, value['pages'])
                value.update(status='partial', code='history_rows_unverified')
                break
            page_identity = page.get('page_identity')
            if not isinstance(page_identity, str) or not re.fullmatch(r'index:(?:0|[1-9][0-9]{0,3})', page_identity):
                LOG.info('purchase history: page_read=unverified reason=page_identity_unverified committed_pages=%s',
                         value['pages'])
                value.update(status='partial', code='history_rows_unverified')
                break
            expected_index = 0 if value['cursor'] is None else value['cursor']['index']
            if page_identity != f'index:{expected_index}':
                value.update(status='partial', code='history_page_repeated')
                break
            fingerprint = hashlib.sha256(json.dumps([page_identity, identifiers]).encode()).hexdigest()
            if inventory.db.execute('SELECT 1 FROM purchase_history_pages WHERE round_id=? AND fingerprint=?',
                                    (round_id, fingerprint)).fetchone():
                value.update(status='partial', code='history_page_repeated')
                break
            cursor = page.get('next_cursor')
            if cursor is not None:
                try:
                    continuation_url(cursor)
                    if cursor['index'] != expected_index + 1:
                        raise MetadataError('History pagination did not advance by one page.')
                except MetadataError:
                    # Retain this page's candidates, but never follow an
                    # unverified continuation or treat a cap as the boundary.
                    cursor = None
                    page = page | {'boundary_verified': False}
            missing = page.get('unresolved_rows', 0)
            if type(missing) is not int or missing < 0 or missing > len(identifiers):
                raise MetadataError('History returned an invalid unresolved-row count.')
            # Inbox and page acknowledgement share a commit. A crash during
            # resolution can repeat an idempotent association, never lose a page.
            inventory.db.execute('INSERT INTO purchase_history_pages VALUES (?,?)', (round_id, fingerprint))
            inserted = inventory.db.executemany('INSERT OR IGNORE INTO purchase_history_links(binding,url) VALUES (?,?)',
                                                [(binding[0], url) for url in found]).rowcount
            value.update(cursor=cursor, pages=value['pages'] + 1)
            value['unresolved_rows'] = value.get('unresolved_rows', 0) + missing
            pages_read += 1
            value['discovered'] += inserted
            if page.get('boundary_verified') is True and cursor is None:
                value.update(status='checked', code=None)
            elif cursor is None or value['pages'] >= MAX_PAGES:
                value.update(status='partial', code='history_continuation_unverified')
            if value['status'] != 'pending':
                # Terminal coverage is published only after the durable inbox
                # drains, including after a crash immediately after this save.
                value['terminal_status'] = value['status']
                value['status'] = 'pending'
            save(inventory, value)
            if resolve() or value.get('terminal_status'):
                break
    unresolved = inventory.db.execute('SELECT COUNT(*) FROM purchase_history_links WHERE binding=? AND resolved<0',
                                      (binding[0],)).fetchone()[0]
    inbox = inventory.db.execute('SELECT COUNT(*) FROM purchase_history_links WHERE binding=? AND resolved=0',
                                 (binding[0],)).fetchone()[0]
    unresolved += value.get('unresolved_rows', 0)
    value['unresolved'] = unresolved
    if inbox and value['status'] != 'pending':
        value['terminal_status'] = value['status']
        value['status'] = 'pending'
    if not inbox and value.get('terminal_status'):
        value['status'] = value.pop('terminal_status')
    if unresolved and value['status'] == 'checked':
        value.update(status='partial', code='history_association_unresolved')
    value['checked_at'] = timestamp()
    save(inventory, value)
    LOG.info('purchase history: status=%s code=%s pages=%s new_pages=%s links=%s unresolved=%s new_series=%s',
             value['status'], value.get('code'), value['pages'], pages_read, value['discovered'], unresolved, added)
    return summary(inventory) | {'pages_read': pages_read, 'series_added': added, 'links_processed': processed}
