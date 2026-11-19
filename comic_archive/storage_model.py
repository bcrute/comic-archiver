"""What the window's Storage and Backblaze B2 settings, and the dashboard's library overview, say and allow.

Like dashboard_model: plain data in and out, no Qt, so every sentence is decided and tested here.
"""
import re

from .dashboard_model import CLOUD_HINTS, display, error_text, number, room
from .messages import say

# What Backblaze would charge for this library, at its price as written on the setup page. Its own pages count by the
# day (its price is for 30 days); people speak of a month.
B2_FREE_BYTES = 10e9
B2_DOLLARS_PER_GB_MONTH = 6.95 / 1000
LABELS = {'b2': say('cloud.backblaze_b2')}
BUCKET_PATTERN = r'[A-Za-z0-9-]{6,63}'
KEY_ID_PATTERN = r'[A-Za-z0-9]{8,256}'


def label(provider):
    return LABELS.get(provider, say('dashboard.cloud_storage'))


def dollars(value):
    if value < 0.005:
        return say('storage.less_than_1_cent')
    if value < 0.995:
        cents = round(value * 100)
        return say('storage.about', cents=cents, value='cent' if cents == 1 else 'cents')
    return f'about ${value:.2f}'


def b2_cost(estimate):
    total = number((estimate or {}).get('saved_bytes')) + number((estimate or {}).get('left_bytes'))
    if not total:
        return ''
    rough = say('dashboard.this_is_a_rough_guess_until') if number(estimate.get('unchecked_series')) else ''
    if total <= B2_FREE_BYTES:
        return say('storage.your_library_should_take_about_which', value=room(total), rough=rough)
    month = (total - B2_FREE_BYTES) / 1e9 * B2_DOLLARS_PER_GB_MONTH
    return (say('storage.your_library_should_take_about_once', value=room(total), value2=dollars(month / 30), value3=dollars(month), rough=rough))


def b2_cost_short(estimate):
    """The same, short, for the heading of the cost details; before the library's size is known, the price of 100 GB."""
    total = number((estimate or {}).get('saved_bytes')) + number((estimate or {}).get('left_bytes'))
    if not total:
        return say('storage.free_up_to_10_gb_then')
    if total <= B2_FREE_BYTES:
        return say('storage.free_your_library_fits_in_the')
    month = (total - B2_FREE_BYTES) / 1e9 * B2_DOLLARS_PER_GB_MONTH
    return f'{dollars(month / 30)} a day, {dollars(month)} a month'


def b2_summary(s):
    """The top of the B2 settings says at a glance whether B2 is set up, and what is left to do: (label, tone, text)."""
    target = s.target()
    bucket = say('storage.your_bucket', bucket=target['bucket']) if target and target.get('bucket') else say('storage.your_bucket_2')
    failed = number((target or {}).get('failed'))
    auth = s.cloud_auth or {}
    if auth.get('status') == 'running' and auth.get('provider') == 'b2':
        return say('storage.checking_your_key'), 'working', say('storage.checking_your_key_and_bucket_with')
    if not (target or {}).get('connected'):
        return say('storage.not_set_up'), 'neutral', say('storage.follow_the_steps_below_to_connect')
    if not target.get('tested'):
        return (say('storage.connected_not_checked_yet'), 'warning',
                say('storage.connected_to', bucket=bucket) + (say('storage.press_test_cloud_connection_before_uploads') if _phone()
                                              else say('storage.check_the_connection_step_5_before')))
    if not target.get('enabled'):
        return say('storage.set_up'), 'success', say('storage.set_up_and_checked_uploads_are', bucket=bucket)
    retry = (say('storage.retrying_click_retry_unfinished_uploads_below', n=display(failed), value=say('storage.upload_needs') if failed == 1 else say('storage.uploads_need'))
             if failed else '')
    return (say('storage.uploads_need_attention') if failed else say('storage.uploading_to_b2'), 'warning' if failed else 'success',
            say('storage.set_up_and_checked_new_episodes', bucket=bucket, retry=retry))


def can_import(s, *, can_start_worker=False):
    """Whether a cloud task may start: signed in, the downloader idle and ready, nothing else running."""
    status = s.status or {}
    ready = (status.get('worker') or {}).get('reported_state') == 'ready'
    # Embedded runtimes may start their held worker on this explicit cloud action.
    # They must await real readiness before submitting the task; desktop stays strict.
    return bool(status.get('session_configured') and (ready or can_start_worker)
                and (status.get('progress') or {}).get('status') != 'running'
                and (s.account or {}).get('status') != 'running' and not s.busy and not s.task
                and (s.cloud_auth or {}).get('status') != 'running')


def _phone():
    """Whether these words are for the phone, which has no dashboard, tabs or numbered steps to name."""
    from . import cloud_runtime
    return cloud_runtime._android


def auth_text(auth):
    """What the B2 settings say about the key being checked."""
    auth = auth or {}
    if auth.get('status') == 'running':
        return (say('storage.preparing_cloud_sign_in_this_may') if auth.get('stage') == 'preparing'
                else say('storage.checking_your_b2_key_and_private'))
    if auth.get('status') == 'connected':
        return say('storage.signed_in_next_click_test_cloud')
    if auth.get('status') == 'cancelled':
        return say('storage.sign_in_canceled_your_saved_files')
    if auth.get('code') == 'signin_network_held':
        if _phone():
            return (say('storage.mobile_data_is_off_connect_to'))
        return (say('storage.this_connection_is_metered_or_cannot'))
    if auth.get('status') == 'error':
        return CLOUD_HINTS.get(auth.get('code'), say('storage.sign_in_did_not_finish_check'))
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
    return say('storage.recorded_test_copies_deleted_local_originals'), False


TASK_STARTED = {
    'cloud': say('storage.retrying_up_to_three_pending_episode'),
    'cloud-test': say('storage.testing_two_completed_episodes_upload_read'),
    'cloud-cleanup': say('storage.retrying_deletion_of_the_exact_recorded'),
}
CONNECTING = say('storage.connecting_b2_your_key_is_being')


def storage_choice(s):
    """Where new episodes go: this device, or one storage that is set up, each named and nothing more."""
    configured = [t for t in s.cloud_targets() if t.get('connected')] if s.cloud_offered and s.cloud else []
    chosen = next((t['provider'] for t in configured if t.get('enabled')), 'device')
    options = [] if not configured else [{'key': 'device', 'label': say('libraries.this_device'), 'ready': True}] + [
        {'key': t['provider'], 'label': label(t['provider']), 'ready': t.get('tested') is True} for t in configured]
    unchecked = [label(t['provider']) for t in configured if not t.get('tested')]
    signing_in = (s.cloud_auth or {}).get('status') == 'running'
    running = (s.account or {}).get('status') == 'running'
    locked = s.busy or signing_in or bool(s.task) or running or not s.cloud
    for option in options:
        option['enabled'] = not locked and option['ready']
        option['checked'] = option['key'] == chosen
    # Said beside the choice, so a locked choice does not look broken.
    why = (say('storage.this_cant_be_changed_while_episodes')
           + (say('storage.pause_the_download_first') if _phone() else say('storage.press_pause_on_the_dashboard_first')) if running
           else say('storage.this_cant_be_changed_while_you') if signing_in
           else say('storage.this_cant_be_changed_until_the') if s.task else '')
    return {'visible': bool(configured), 'options': options, 'chosen': chosen, 'lock': why,
            'help': (say('storage.each_new_episode_is_uploaded_to', value=label(chosen))
                     if chosen != 'device' else say('storage.can_be_chosen_once_its_connection', value=' and '.join(unchecked), value2='' if _phone() else say('storage.on_its_own_tab'))
                     if unchecked else say('storage.new_episodes_stay_in_the_save')),
            'entry_visible': not configured and s.cloud_offered and s.cloud is not None}


def storage(s):
    """The Storage settings: each place episodes can be kept, what it holds and the way to it."""
    held = {group.get('id'): group for group in s.locations}

    def stored(group):
        episodes = number(group.get('episodes'))
        return f"{display(episodes)} {'episode' if episodes == 1 else 'episodes'} stored · {room(number(group.get('bytes')))}"
    target = s.target() or {}
    pill, tone, _ = b2_summary(s)
    return {'device_stored': stored(held['device']) if 'device' in held else say('storage.counting'),
            'b2_pill': pill, 'b2_tone': tone,
            'b2_text': (say('storage.your_own_backblaze_account', value=', bucket ' + target['bucket'] if target.get('bucket') else '')
                        if target.get('connected') else
                        say('storage.your_own_backblaze_account_backblaze_bills')),
            'b2_stored': stored(held['b2']) if 'b2' in held else '',
            'b2_links_visible': bool(target.get('connected')),
            'b2_setup': 'B2 settings' if target.get('connected') else say('storage.set_up')}


def key_problem(bucket, key_id, application_key):
    """What is wrong with a B2 key as typed, before it is sent to be checked; '' when nothing is."""
    if not re.fullmatch(BUCKET_PATTERN, bucket):
        return say('storage.type_a_bucket_name_of_6')
    if not re.fullmatch(KEY_ID_PATTERN, key_id) or not 8 <= len(application_key) <= 256:
        return say('storage.paste_the_keyid_and_applicationkey_exactly')
    return ''


def bucket_name(draft, saved, workspace_id):
    """What the person typed wins until a connection saves it; then the saved name; first, a suggestion."""
    return draft if draft is not None else saved if saved else 'comic-library-' + workspace_id[:12]


def b2(s, steps_open=False, *, can_start_worker=False):
    """The Backblaze B2 settings: its state, what it costs, and the controls that set it up."""
    target = s.target() or {}
    value = s.cloud or {}
    pill, tone, text = b2_summary(s)
    signing_in = (s.cloud_auth or {}).get('status') == 'running'
    running = (s.account or {}).get('status') == 'running'
    worker_busy = ((s.status or {}).get('worker') or {}).get('reported_state') == 'busy'
    estimate = (s.status or {}).get('estimate')
    hints = [say('storage.failed_uploads', n=display(target["failure_reasons"][code]), message=message) for code, message in CLOUD_HINTS.items()
             if number((target.get('failure_reasons') or {}).get(code)) > 0]
    repair = (' '.join(hints) + say('storage.local_episodes_are_retained_these_are')
              if hints else '')
    cleanup = []
    for record in value.get('pending_test_cleanup') or []:
        code = record.get('error_code')
        if isinstance(code, str) and code in CLOUD_HINTS:
            repair += say('storage.test_cleanup_pending_local_originals_are', value=CLOUD_HINTS[code])
        cleanup.append({'test_id': record.get('test_id'), 'text': say('storage.retry_test_cleanup', value=label(record.get('provider'))),
                        'enabled': can_import(s, can_start_worker=can_start_worker)})
    connected = target.get('connected') is True
    sharing = value.get('sharing_storage', value.get('storage'))
    mixed = sharing == 'mixed'
    # A connected B2 needs only its check and the choice on the dashboard; the steps that made it fold away.
    folded = connected and not steps_open
    return {
        'pill': pill, 'tone': tone, 'text': text,
        'status': (say('storage.checking_cloud_connections') if s.cloud is None else
                   f"{say('storage.automatic_uploads_enabled') if target.get('enabled') else say('storage.uploads_disabled')}. "
                   f"{display(target.get('verified'))} verified upload receipts; {display(target.get('pending'))} pending; "
                   f"{display(target.get('failed'))} need retry. Cloud availability has not been rechecked."
                   if target.get('tested') else say('storage.connection_configured_complete_the_small_upload')
                   if connected else say('storage.no_connection_configured_for_this_provider')),
        'cost_short': b2_cost_short(estimate), 'cost': b2_cost(estimate),
        'bucket': target.get('bucket'),
        # One Backblaze setup serves every site, unless this site keeps its own.
        'sharing_visible': value.get('storage') in ('shared', 'separate'),
        'shared': sharing == 'shared',
        'sharing_mixed': mixed,
        'sharing_enabled': not (s.busy or signing_in),
        'sharing_help': ('Your providers currently use different sharing choices. Choose one sharing mode for all providers.'
                         if mixed else say('storage.every_site_you_save_from_uses')
                         if sharing == 'shared'
                         else 'Each provider has its own Backblaze settings. Choose a provider below.'),
        'folded': folded, 'fold_visible': connected,
        'fold_text': say('storage.hide_the_setup_steps') if steps_open else say('storage.show_the_setup_steps'),
        'connect_text': say('storage.replace_b2_connection') if connected else say('storage.connect_b2'),
        'connect_enabled': not (mixed or s.busy or s.task or signing_in or running or worker_busy or s.cloud is None),
        'cancel_visible': signing_in, 'cancel_enabled': not s.busy,
        'auth': auth_text(s.cloud_auth),
        'repair': repair, 'cleanup': cleanup,
        'retry_enabled': not mixed and can_import(s, can_start_worker=can_start_worker) and any(t.get('enabled') for t in s.cloud_targets()),
        'test_enabled': (not mixed and can_import(s, can_start_worker=can_start_worker) and connected
                         and not any(r.get('provider') == 'b2' for r in value.get('pending_test_cleanup') or [])),
    }


def combined(summaries):
    """Every site's saved-file counts as one: None while any site is still counting, False when none could be
    counted. A site whose counts could not be read is left out of the rest."""
    if any(summary is None for summary in summaries) or not summaries:
        return None
    read = [summary for summary in summaries if isinstance(summary, dict)]
    if not read:
        return False
    places = {}
    for summary in read:
        for group in summary.get('locations') or []:
            kept = places.setdefault(group.get('id'), dict(group, episodes=0, files=0, connected=False))
            kept['episodes'] += number(group.get('episodes'))
            kept['files'] += number(group.get('files'))
            kept['connected'] = kept['connected'] or bool(group.get('connected'))
    return {'episode_count': sum(number(summary.get('episode_count')) for summary in read),
            'file_count': sum(number(summary.get('file_count')) for summary in read), 'locations': list(places.values())}


THIS_COMPUTER = say('storage.this_computer')


def library_overview(summary, cloud_offered, *, device_name=THIS_COMPUTER):
    """The dashboard's saved-library card: its counts, its storage places and a sentence about them."""
    if summary is None:
        names = [say('storage.saved_episodes'), say('library.saved_files')]
        if cloud_offered:
            names += [say('storage.episodes_on_this_device'), say('storage.cloud_episode_copies')]
        return {'metrics': [('—', name) for name in names], 'locations': [],
                'summary': say('storage.counting_your_saved_files')}
    locations = summary.get('locations') or []
    device = next((g for g in locations if g.get('id') == 'device'), {})
    copies = sum(number(g.get('episodes')) for g in locations if g.get('id') != 'device')

    def shown(value):
        return display(value) if type(value) is int and value >= 0 else '—'
    metrics = ([(say('storage.saved_episodes'), device.get('episodes')), (say('library.saved_files'), device.get('files'))] if not cloud_offered else
               [(say('storage.saved_episodes'), summary.get('episode_count')), (say('library.saved_files'), summary.get('file_count')),
                (say('storage.episodes_on_this_device'), device.get('episodes')), (say('storage.cloud_episode_copies'), copies)])
    places = [{'id': g.get('id'), 'label': g.get('label'),
               'counts': f"{display(g.get('episodes'))} episodes · {display(g.get('files'))} files"
                         + (say('library.not_connected') if g.get('id') != 'device' and not g.get('connected') else '')}
              for g in locations if cloud_offered or g.get('id') == 'device']
    return {'metrics': [(shown(count), name) for name, count in metrics], 'locations': places,
            'summary': (say('storage.saved_episodes_on', n=display(device.get('episodes')), device_name=device_name)
                        if not cloud_offered else
                        say('storage.saved_episodes_view_all_files_or', n=display(summary.get('episode_count'))))}


LIBRARY_UNAVAILABLE = say('storage.saved_file_counts_are_unavailable')
