"""Exact, task-local failure accounting without private exception details."""
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class FailureReceipt:
    run_id: str
    status: str
    downloaded_pages: int
    downloaded_bytes: int
    pending_episode_count: int
    archived_episode_ids: tuple[str, ...]
    scan_remaining_series_count: int
    pause_reason: str | None = None
    source_http_status: int | None = None
    retry_after_seconds: int | None = None
    source_response_kind: str | None = None
    source_retry_episode_id: str | None = None


def retain_failure_receipt(error, summary):
    """Called only after this job has written its terminal checkpoint."""
    error._comic_failure_receipt = FailureReceipt(
        summary['run_id'], summary['status'], summary['downloaded_pages'], summary['downloaded_bytes'],
        summary['pending_episode_count'], tuple(summary['archived_episode_ids']),
        summary['inventory']['scan_remaining_series_count'],
        summary.get('pause_reason'), summary.get('source_http_status'), summary.get('retry_after_seconds'),
        summary.get('source_response_kind'), summary.get('source_retry_episode_id'))


def failure_result(error):
    receipt = getattr(error, '_comic_failure_receipt', None)
    if not isinstance(receipt, FailureReceipt):
        return None
    if (not isinstance(receipt.run_id, str) or not re.fullmatch(r'[a-f0-9]{24}', receipt.run_id)
            or receipt.status not in {'login_required', 'paused', 'interrupted'}
            or any(type(value) is not int or value < 0 for value in
                   (receipt.downloaded_pages, receipt.downloaded_bytes, receipt.pending_episode_count,
                    receipt.scan_remaining_series_count))
            or any(not isinstance(key, str) or not re.fullmatch(r'[1-9][0-9]{0,19}', key)
                   for key in receipt.archived_episode_ids)
            or len(set(receipt.archived_episode_ids)) != len(receipt.archived_episode_ids)):
        return None
    result = {'run_id': receipt.run_id, 'status': receipt.status,
            'downloaded_pages': receipt.downloaded_pages, 'downloaded_bytes': receipt.downloaded_bytes,
            'pending_episode_count': receipt.pending_episode_count,
            'archived_episode_ids': list(receipt.archived_episode_ids),
            'inventory': {'scan_remaining_series_count': receipt.scan_remaining_series_count}}
    if receipt.pause_reason == 'source_refused':
        if (receipt.status != 'paused' or type(receipt.source_http_status) is not int
                or receipt.source_http_status not in {403, 429}):
            return None
        result.update(pause_reason='source_refused', source_http_status=receipt.source_http_status)
        from .source_response import source_response_diagnostics
        result.update(source_response_diagnostics(vars(receipt)))
        if (receipt.source_http_status == 403 and receipt.source_response_kind == 'html'
                and isinstance(receipt.source_retry_episode_id, str)
                and re.fullmatch(r'[1-9][0-9]{0,19}', receipt.source_retry_episode_id)):
            result['source_retry_episode_id'] = receipt.source_retry_episode_id
        from .retry_after import MAX_RETRY_AFTER
        if type(receipt.retry_after_seconds) is int and 0 <= receipt.retry_after_seconds <= MAX_RETRY_AFTER:
            result['retry_after_seconds'] = receipt.retry_after_seconds
    return result


def carry_failure_receipt(source, target):
    receipt = getattr(source, '_comic_failure_receipt', None)
    if isinstance(receipt, FailureReceipt):
        target._comic_failure_receipt = receipt
