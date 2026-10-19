"""What the window's Storage and Backblaze B2 settings, and the dashboard's library overview, say and allow.

Like dashboard_model: plain data in and out, no Qt, so every sentence is decided and tested here.
"""
import re

from .dashboard_model import CLOUD_HINTS, display, error_text, number, room

# What Backblaze would charge for this library, at its price as written on the setup page. Its own pages count by the
# day (its price is for 30 days); people speak of a month.
B2_FREE_BYTES = 10e9
B2_DOLLARS_PER_GB_MONTH = 6.95 / 1000
LABELS = {'b2': 'Backblaze B2'}
BUCKET_PATTERN = r'[A-Za-z0-9-]{6,63}'
KEY_ID_PATTERN = r'[A-Za-z0-9]{8,256}'


def label(provider):
    return LABELS.get(provider, 'Cloud storage')


def dollars(value):
    if value < 0.005:
        return 'less than 1 cent'
    if value < 0.995:
        cents = round(value * 100)
        return f"about {cents} {'cent' if cents == 1 else 'cents'}"
    return f'about ${value:.2f}'


def b2_cost(estimate):
    total = number((estimate or {}).get('saved_bytes')) + number((estimate or {}).get('left_bytes'))
    if not total:
        return ''
    rough = ' This is a rough guess until every series has been checked.' if number(estimate.get('unchecked_series')) else ''
    if total <= B2_FREE_BYTES:
        return f'Your library should take about {room(total)}, which fits in Backblaze’s free 10 GB.{rough}'
    month = (total - B2_FREE_BYTES) / 1e9 * B2_DOLLARS_PER_GB_MONTH
    return (f'Your library should take about {room(total)}. Once all of it is stored, Backblaze would charge '
            f'{dollars(month / 30)} a day, {dollars(month)} a month.{rough}')


def b2_cost_short(estimate):
    """The same, short, for the heading of the cost details; before the library's size is known, the price of 100 GB."""
    total = number((estimate or {}).get('saved_bytes')) + number((estimate or {}).get('left_bytes'))
    if not total:
        return 'free up to 10 GB, then about 2 cents a day, about 70 cents a month, for each 100 GB more'
    if total <= B2_FREE_BYTES:
        return 'free, your library fits in the free 10 GB'
    month = (total - B2_FREE_BYTES) / 1e9 * B2_DOLLARS_PER_GB_MONTH
    return f'{dollars(month / 30)} a day, {dollars(month)} a month'


def b2_summary(s):
    """The top of the B2 settings says at a glance whether B2 is set up, and what is left to do: (label, tone, text)."""
    target = s.target()
    bucket = f"your bucket {target['bucket']}" if target and target.get('bucket') else 'your bucket'
    failed = number((target or {}).get('failed'))
    auth = s.cloud_auth or {}
    if auth.get('status') == 'running' and auth.get('provider') == 'b2':
        return 'Checking your key', 'working', 'Checking your key and bucket with Backblaze. Nothing is uploaded yet.'
    if not (target or {}).get('connected'):
        return 'Not set up', 'neutral', 'Follow the steps below to connect your own Backblaze account.'
    if not target.get('tested'):
        return ('Connected, not checked yet', 'warning',
                f'Connected to {bucket}. Check the connection (step 5) before uploads can start.')
    if not target.get('enabled'):
        return 'Set up', 'success', f'Set up and checked: {bucket}. Uploads are off; choose Backblaze B2 on the Storage tab.'
    retry = (f" {display(failed)} {'upload needs' if failed == 1 else 'uploads need'} retrying: click Retry unfinished uploads below."
             if failed else '')
    return ('Uploads need attention' if failed else 'Uploading to B2', 'warning' if failed else 'success',
            f'Set up and checked: {bucket}. New episodes are uploaded to it as they are saved.{retry}')


def can_import(s):
    """Whether a cloud task may start: signed in, the downloader idle and ready, nothing else running."""
    status = s.status or {}
    return bool(status.get('session_configured') and (status.get('worker') or {}).get('reported_state') == 'ready'
                and (status.get('progress') or {}).get('status') != 'running'
                and (s.account or {}).get('status') != 'running' and not s.busy and not s.task
                and (s.cloud_auth or {}).get('status') != 'running')


def auth_text(auth):
    """What the B2 settings say about the key being checked."""
    auth = auth or {}
    if auth.get('status') == 'running':
        return ('Preparing cloud sign-in. This may take a moment the first time.' if auth.get('stage') == 'preparing'
                else 'Checking your B2 key and private library bucket…')
    if auth.get('status') == 'connected':
        return 'Signed in. Next, click Test cloud connection.'
    if auth.get('status') == 'cancelled':
        return 'Sign-in cancelled. Your saved files are safe.'
    if auth.get('code') == 'signin_network_held':
        return ('Mobile data is off. Connect to Wi-Fi or tick Also save on metered and mobile connections in Settings, '
                'then try again.')
    if auth.get('status') == 'error':
        return CLOUD_HINTS.get(auth.get('code'), 'Sign-in did not finish. Check your details and connection, then try again.')
    return ''


def task_text(kind, result, site='Tapas'):
    """(text, error) when a cloud task has ended, said beside the cloud buttons."""
    if result.get('status') == 'error':
        return error_text(result.get('code') or 'task_failed', site), True
    values = result.get('result') or {}
    if kind == 'cloud':
        return (f"{display(values.get('uploaded_episodes'))} uploads verified; "
                f"{display(values.get('local_archives_deleted'))} local archives removed."), False
    if kind == 'cloud-test':
        return f"{display(values.get('verified_episodes'))} episodes verified; test copies deleted. Local originals retained.", False
    return 'Recorded test copies deleted. Local originals retained.', False


TASK_STARTED = {
    'cloud': 'Retrying up to three pending episode uploads.',
    'cloud-test': 'Testing two completed episodes: upload, read back and verify, then delete test copies. Local originals are retained.',
    'cloud-cleanup': 'Retrying deletion of the exact recorded test files. Local originals are retained.',
}
CONNECTING = 'Connecting B2. Your key is being checked; no episodes are uploaded during setup.'


def storage_choice(s):
    """Where new episodes go: this device, or one storage that is set up, each named and nothing more."""
    configured = [t for t in s.cloud_targets() if t.get('connected')] if s.cloud_offered and s.cloud else []
    chosen = next((t['provider'] for t in configured if t.get('enabled')), 'device')
    options = [] if not configured else [{'key': 'device', 'label': 'This device', 'ready': True}] + [
        {'key': t['provider'], 'label': label(t['provider']), 'ready': t.get('tested') is True} for t in configured]
    unchecked = [label(t['provider']) for t in configured if not t.get('tested')]
    signing_in = (s.cloud_auth or {}).get('status') == 'running'
    running = (s.account or {}).get('status') == 'running'
    locked = s.busy or signing_in or bool(s.task) or running or not s.cloud
    for option in options:
        option['enabled'] = not locked and option['ready']
        option['checked'] = option['key'] == chosen
    # Said beside the choice, so a locked choice does not look broken.
    why = ('This can’t be changed while episodes are downloading. Press Pause on the dashboard first.' if running
           else 'This can’t be changed while you sign in to cloud storage.' if signing_in
           else 'This can’t be changed until the cloud task finishes.' if s.task else '')
    return {'visible': bool(configured), 'options': options, 'chosen': chosen, 'lock': why,
            'help': (f'Each new episode is uploaded to {label(chosen)} as it downloads. None is kept on this device once its upload is checked.'
                     if chosen != 'device' else f"{' and '.join(unchecked)} can be chosen once its connection check passes, on its own tab."
                     if unchecked else 'New episodes stay in the save folder.'),
            'entry_visible': not configured and s.cloud_offered and s.cloud is not None}


def storage(s):
    """The Storage settings: each place episodes can be kept, what it holds and the way to it."""
    held = {group.get('id'): group for group in s.locations}

    def stored(group):
        episodes = number(group.get('episodes'))
        return f"{display(episodes)} {'episode' if episodes == 1 else 'episodes'} stored · {room(number(group.get('bytes')))}"
    target = s.target() or {}
    pill, tone, _ = b2_summary(s)
    return {'device_stored': stored(held['device']) if 'device' in held else 'Counting…',
            'b2_pill': pill, 'b2_tone': tone,
            'b2_text': (f"Your own Backblaze account{', bucket ' + target['bucket'] if target.get('bucket') else ''}."
                        if target.get('connected') else
                        'Your own Backblaze account. Backblaze bills you itself; the first 10 GB is free.'),
            'b2_stored': stored(held['b2']) if 'b2' in held else '',
            'b2_links_visible': bool(target.get('connected')),
            'b2_setup': 'B2 settings' if target.get('connected') else 'Set up'}


def key_problem(bucket, key_id, application_key):
    """What is wrong with a B2 key as typed, before it is sent to be checked; '' when nothing is."""
    if not re.fullmatch(BUCKET_PATTERN, bucket):
        return 'Type a bucket name of 6 to 63 letters, numbers or hyphens, as in step 2.'
    if not re.fullmatch(KEY_ID_PATTERN, key_id) or not 8 <= len(application_key) <= 256:
        return 'Paste the keyID and applicationKey exactly as Backblaze shows them.'
    return ''


def bucket_name(draft, saved, workspace_id):
    """What the person typed wins until a connection saves it; then the saved name; first, a suggestion."""
    return draft if draft is not None else saved if saved else 'comic-library-' + workspace_id[:12]


def b2(s, steps_open=False):
    """The Backblaze B2 settings: its state, what it costs, and the controls that set it up."""
    target = s.target() or {}
    value = s.cloud or {}
    pill, tone, text = b2_summary(s)
    signing_in = (s.cloud_auth or {}).get('status') == 'running'
    running = (s.account or {}).get('status') == 'running'
    worker_busy = ((s.status or {}).get('worker') or {}).get('reported_state') == 'busy'
    estimate = (s.status or {}).get('estimate')
    hints = [f'{display(target["failure_reasons"][code])} failed uploads: {message}' for code, message in CLOUD_HINTS.items()
             if number((target.get('failure_reasons') or {}).get(code)) > 0]
    repair = (' '.join(hints) + ' Local episodes are retained. These are recorded repair hints; cloud availability has not been rechecked.'
              if hints else '')
    cleanup = []
    for record in value.get('pending_test_cleanup') or []:
        code = record.get('error_code')
        if isinstance(code, str) and code in CLOUD_HINTS:
            repair += f' Test cleanup pending: {CLOUD_HINTS[code]} Local originals are retained.'
        cleanup.append({'test_id': record.get('test_id'), 'text': f"Retry {label(record.get('provider'))} test cleanup",
                        'enabled': can_import(s)})
    connected = target.get('connected') is True
    # A connected B2 needs only its check and the choice on the dashboard; the steps that made it fold away.
    folded = connected and not steps_open
    return {
        'pill': pill, 'tone': tone, 'text': text,
        'status': ('Checking cloud connections…' if s.cloud is None else
                   f"{'Automatic uploads enabled' if target.get('enabled') else 'Uploads disabled'}. "
                   f"{display(target.get('verified'))} verified upload receipts; {display(target.get('pending'))} pending; "
                   f"{display(target.get('failed'))} need retry. Cloud availability has not been rechecked."
                   if target.get('tested') else 'Connection configured. Complete the small upload-and-delete test before enabling uploads.'
                   if connected else 'No connection configured for this provider.'),
        'cost_short': b2_cost_short(estimate), 'cost': b2_cost(estimate),
        'bucket': target.get('bucket'),
        # One Backblaze setup serves every site, unless this site keeps its own.
        'sharing_visible': value.get('storage') in ('shared', 'separate'),
        'shared': value.get('storage') == 'shared',
        'sharing_enabled': not (s.busy or signing_in),
        'sharing_help': ('Every site you save from uses it, each in a folder of its own in your bucket.'
                         if value.get('storage') == 'shared'
                         else f'{s.site()} has a Backblaze setup of its own. Your other sites keep the shared one.'),
        'folded': folded, 'fold_visible': connected,
        'fold_text': 'Hide the setup steps' if steps_open else 'Show the setup steps',
        'connect_text': 'Replace B2 connection' if connected else 'Connect B2',
        'connect_enabled': not (s.busy or s.task or signing_in or running or worker_busy or s.cloud is None),
        'cancel_visible': signing_in, 'cancel_enabled': not s.busy,
        'auth': auth_text(s.cloud_auth),
        'repair': repair, 'cleanup': cleanup,
        'retry_enabled': can_import(s) and any(t.get('enabled') for t in s.cloud_targets()),
        'test_enabled': (can_import(s) and connected
                         and not any(r.get('provider') == 'b2' for r in value.get('pending_test_cleanup') or [])),
    }


def library_overview(summary, cloud_offered):
    """The dashboard's saved-library card: its counts, its storage places and a sentence about them."""
    if summary is None:
        return {'metrics': [('—', 'Saved episodes'), ('—', 'Saved files')], 'locations': [],
                'summary': 'Counting your saved files…'}
    locations = summary.get('locations') or []
    device = next((g for g in locations if g.get('id') == 'device'), {})
    copies = sum(number(g.get('episodes')) for g in locations if g.get('id') != 'device')

    def shown(value):
        return display(value) if type(value) is int and value >= 0 else '—'
    metrics = ([('Saved episodes', device.get('episodes')), ('Saved files', device.get('files'))] if not cloud_offered else
               [('Saved episodes', summary.get('episode_count')), ('Saved files', summary.get('file_count')),
                ('Episodes on this device', device.get('episodes')), ('Cloud episode copies', copies)])
    places = [{'id': g.get('id'), 'label': g.get('label'),
               'counts': f"{display(g.get('episodes'))} episodes · {display(g.get('files'))} files"
                         + (' · Not connected' if not g.get('connected') and not g.get('files') else '')}
              for g in locations if cloud_offered or g.get('id') == 'device']
    return {'metrics': [(shown(count), name) for name, count in metrics], 'locations': places,
            'summary': (f"{display(device.get('episodes'))} saved episodes on this computer. View saved files for the complete list."
                        if not cloud_offered else
                        f"{display(summary.get('episode_count'))} saved episodes. View all files, or choose a storage location below.")}


LIBRARY_UNAVAILABLE = 'Saved file counts are unavailable. Open View saved files to retry.'
