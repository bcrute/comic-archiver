"""What a person can paste into a message asking for help: the app's version and system, and for each site what its
download is doing, why it stopped, and how many episodes are in each state.

Only the app's own words, codes and counts go in: no series or episode names, no folder names, nothing about the
sign-in but whether there is one. Shared by the desktop and phone apps, so both copy the same details.
"""
import platform
import re

LIMIT = 9000  # Under the 10,000 characters a Reddit message holds, with room for a few words of the person's own.
CODE = re.compile(r'[a-z][a-z0-9_:]{0,63}')


def system():
    """The operating system, as plainly as a support reply needs it."""
    name = platform.system()
    if name == 'Windows':
        # Windows 11 still calls itself release 10; its build number tells the two apart.
        return f"Windows {platform.release()} (build {platform.version().rpartition('.')[2]})"
    return f'{name} {platform.release()}'.strip() or 'Unknown system'


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _counts(value):
    """A {code: count} as 'code 3, other 1', keeping only the app's own codes."""
    if not isinstance(value, dict):
        return ''
    parts = [f'{key} {_count(count)}' for key, count in sorted(value.items())
             if isinstance(key, str) and CODE.fullmatch(key) and _count(count) is not None]
    return ', '.join(parts)


def _word(value):
    return value if isinstance(value, str) and CODE.fullmatch(value) else None


def site_lines(name, s):
    """One site's lines. s is the site's state as the window keeps it."""
    lines = [f'== {name}']
    if s is None or s.status is None:
        return lines + ['Not opened yet']
    status, account = s.status or {}, s.account or {}
    signin = 'signed in' if status.get('session_configured') else 'not signed in'
    if _word((s.site_auth or {}).get('status')):
        signin += f" (sign-in {s.site_auth['status']})"
    lines.append(f'Sign-in: {signin}')
    download = [_word(account.get(key)) for key in ('status', 'phase', 'code')]
    lines.append('Download: ' + (' / '.join(word for word in download if word) or 'not started'))
    worker = _word((status.get('worker') or {}).get('reported_state'))
    if worker:
        lines.append(f'Downloader: {worker}')
    if _word(account.get('speed')):
        lines.append(f"Speed: {account['speed']}")
    rows = (('Episodes', status.get('episodes_by_status')), ('Not downloaded', status.get('unsupported_by_reason')),
            ('Problems', status.get('unsaved_by_code')), ('Last check', account.get('audit')))
    lines += [f'{label}: {_counts(value)}' for label, value in rows if _counts(value)]
    series = [f'{label} {_count(status.get(key))}' for label, key in (
        ('known', 'series_count'), ('to check', 'scan_remaining_series_count'),
        ('with gaps', 'series_scan_gap_count'), ('never checked', 'series_without_scan_checkpoint_count'))
        if _count(status.get(key)) is not None]
    if series:
        lines.append('Series: ' + ', '.join(series))
    last = _word((status.get('last_run') or {}).get('status'))
    if last:
        lines.append(f'Last step: {last}')
    if s.offline:
        lines.append('Status updates: not arriving')
    return lines


def text(version, sites):
    """Every site's details under the app's own. sites is [(name, state)], in the window's order."""
    lines = [f'Comic Archiver {version} on {system()}']
    for name, s in sites:
        lines += ['', *site_lines(name, s)]
    result = '\n'.join(lines)
    return result if len(result) <= LIMIT else result[:LIMIT - 12].rstrip() + '\n(shortened)'
