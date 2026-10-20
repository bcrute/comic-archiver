"""What the window's dashboard and download settings say and allow, from what the app last read.

Plain data in, plain data out: no Qt and no files, so every sentence and every locked button is decided here
and tested here. The window only puts the answers on screen.
"""
from dataclasses import dataclass, field
import math

# Each refresh of the dashboard reads the library this often; the clocks are redrawn every second in between.
POLL_SECONDS = 5
# The connection is checked now and then, not on every refresh.
NETWORK_SECONDS = 15
# Saved-file counts are read again at most this often, unless something changed them.
LIBRARY_SECONDS = 30
# A downloader that has not said it is there this long after a sign-in is offline, not starting.
STARTING_SECONDS = 120
# Updates count as delayed once none has arrived for this long.
DELAYED_SECONDS = 15
# A failure stays on the dashboard this long, so it can be read.
MESSAGE_SECONDS = 30
WORKING = ('starting', 'ready', 'busy')
SPEEDS = ('fast', 'medium', 'slow')
SPEED_NAMES = {'fast': 'Fast', 'medium': 'Medium', 'slow': 'Slow'}

# Recovery hints for cloud uploads, as the window words them.
CLOUD_HINTS = {
    'b2_key_permissions': 'Create a Read and Write key restricted to your library bucket. Leave the file prefix and duration blank, then copy keyID and applicationKey again.',
    'b2_bucket_mismatch': 'This key is for a different bucket than the bucket name in step 2. Type the name exactly as Backblaze’s Buckets page shows it, or create the key again for that bucket.',
    'b2_private_bucket': 'Choose Private for your B2 library bucket in Backblaze, then connect again.',
    'cloud_login_required': 'Cloud sign-in needs repair. Click Sign in again for this provider, then retry.',
    'cloud_space': 'The provider reported insufficient cloud storage. Free space through your provider or choose another destination, then retry.',
    'cloud_cap': 'Backblaze stopped the upload: your account has used its free 10 GB, or reached a spending limit set on its Caps & Alerts page. Add a card on Backblaze’s Billing page or raise the limit, then retry.',
    'cloud_rate_limited': 'The provider limited requests. Wait before retrying.',
    'cloud_connection': 'The cloud transfer was unavailable or timed out. Check your internet connection, then retry.',
    'cloud_permission': 'The provider refused write access. Check the selected account and connection permissions, then retry.',
    'cloud_verification': 'The cloud copy could not be verified. Retry the upload before removing any files.',
    'cloud_transfer_limit': 'A cloud transfer limit was reached. Check the connection settings before retrying.',
    'cloud_network_held': 'The network policy held the cloud transfer. Reconnect to an allowed network, then retry.',
    'cloud_unknown': 'The cloud transfer failed for an unrecognized reason. Check sign-in, connection and cloud space, then retry.',
}
SAFE = 'Saved files are safe.'


def error_text(code, site='Tapas'):
    """What a refused action says, for the code the app gave it."""
    fixed = {'library_busy': 'An import is active. Try again after this batch finishes.',
             'service_unavailable': 'The service is temporarily unavailable. Try again shortly.',
             'request_unavailable': 'This action is unavailable. Check the connection and selected library.',
             'login_required': f'Your {site} session needs a fresh login.',
             'source_refused': f'{site} refused a request. Check {site} in your usual browser before resuming. '
                               'Completed archives and cached images are retained.',
             'task_failed': 'The task stopped. Completed archives and cached images are retained.'}
    if code in CLOUD_HINTS:
        return CLOUD_HINTS[code] + ' Local episodes are retained.'
    return fixed.get(code, 'The action could not be completed.')


def number(value):
    """A count the app reported, or 0 for anything that is not one."""
    return value if type(value) is int and value >= 0 else 0


def display(value):
    return f'{number(value):,}'


def duration(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return None
    if value >= 3600:
        return f'{math.floor(value / 3600)}h {math.floor(value % 3600 / 60)}m'
    return f'{math.floor(value / 60)}m {math.floor(value % 60)}s'


def span(seconds):
    """A length of time as a person would say it: the estimate is rough, and so is this."""
    hours = seconds / 3600
    days = hours / 24
    if hours < 0.85:
        return f'{max(5, round(seconds / 300) * 5)} minutes'
    if hours < 1.5:
        return 'an hour'
    if hours < 36:
        return f'{round(hours)} hours'
    if days < 14:
        return f'{round(days)} days'
    if days < 63:
        return f'{round(days / 7)} weeks'
    return f'{round(days / 30)} months'


def room(value):
    """An amount of disk space as a person would say it; like the time, it is a rough figure."""
    gb = value / 1e9
    if gb >= 1000:
        return f'{gb / 1000:.1f} TB'
    if gb >= 10:
        return f'{round(gb)} GB'
    if gb >= 1:
        return f'{gb:.1f} GB'
    return f'{max(1, round(value / 1e6))} MB'


def wait_text(seconds):
    return f'about {round(seconds / 60)} minutes' if seconds >= 90 else 'about a minute' if seconds > 45 else 'a few seconds'


def time_left(estimate):
    waiting, unchecked = number((estimate or {}).get('waiting_episodes')), number((estimate or {}).get('unchecked_series'))
    if not estimate or not (waiting or unchecked):
        return ''
    if type(estimate.get('seconds')) is not int:
        return 'Working out how long this will take. An estimate appears after about five minutes of downloading.'
    left = (f"{display(waiting)} {'episode is' if waiting == 1 else 'episodes are'} waiting"
            + (f" and {display(unchecked)} series {'has' if unchecked == 1 else 'have'} not been checked yet, so this is a rough guess."
               if unchecked else '.'))
    return (f"About {span(estimate['seconds'])} of downloading left at the pace of the last five minutes. {left} "
            'It only continues while this app is open.')


def space_needed(estimate):
    unchecked = number((estimate or {}).get('unchecked_series'))
    if not estimate or not (number(estimate.get('waiting_episodes')) or unchecked):
        return ''
    if type(estimate.get('left_bytes')) is not int:
        return ''
    saved, left, free = number(estimate.get('saved_bytes')), estimate['left_bytes'], estimate.get('free_bytes')
    parts = f': {room(saved)} is saved and about {room(left)} is still to come' if saved else ''
    rough = ' This is a rough guess until every series has been checked.' if unchecked else ''
    disk = ('' if type(free) is not int else f' This disk has only {room(free)} free, which is not enough for the rest.'
            if free < left else f' This disk has {room(free)} free.')
    return f'Your whole library should take about {room(saved + left)} of space{parts}.{rough}{disk}'


def held_message(code, site, go_wait, missing):
    """Why a held download stopped, and what the person can do about it, in one sentence each."""
    if code == 'archive_incomplete':
        if missing:
            return (f"Finished, but {display(missing)} {'episode' if missing == 1 else 'episodes'} could not be saved. "
                    f'{SAFE} Hit Go to check again.')
        return f'Finished, but some saved files need another check. {SAFE} Hit Go to check again.'
    if code in ('source_refused', 'source_recovery_limit'):
        return (f'Paused: {site} asked the app to slow down. {SAFE} '
                + (f'Go works again in {wait_text(go_wait)}.' if go_wait
                   else 'Hit Go to try again. If this keeps happening, choose a slower speed in Settings.'))
    if code == 'source_access_changed':
        return (f'Paused: {site} still would not send an episode after waiting. {SAFE} '
                f'Check that you can read it on {site}, then hit Go to continue.')
    # While the connection is still held, the caller's own message names it; this one is for after it changed.
    if code == 'network_policy':
        return f'Paused while the connection was not Wi-Fi or wired. {SAFE} Hit Go to continue.'
    if code in ('network_unavailable', 'network_recovery_limit'):
        return (f'Paused: {site} could not be reached several times in a row. {SAFE} '
                'Check your internet connection, then hit Go to continue.')
    if code in ('no_progress', 'scan_no_progress'):
        return (f'Paused: the last few tries saved nothing new. {SAFE} '
                f'Hit Go to try again; if it pauses again, {site} may be having trouble, so try later.')
    if code == 'cancelled':
        return 'Paused. Hit Go to continue from saved progress.'
    if code in CLOUD_HINTS:
        return f'Paused: an upload to cloud storage stopped. {CLOUD_HINTS[code]} {SAFE} Hit Go to continue.'
    return f'Paused: the download stopped unexpectedly. {SAFE} Check that this disk has free space, then hit Go to try again.'


def step_progress(p):
    """The step being worked on, as plainly as the dashboard puts it."""
    if not isinstance(p, dict) or p.get('status') != 'running' or p.get('cancellation_requested'):
        return ''
    if p.get('phase') == 'scan' and type(p.get('series_check_target_count')) is int:
        return f"Checked {display(p.get('series_checks_completed'))} of {display(p['series_check_target_count'])} series in this step."
    if p.get('phase') != 'archive':
        return ''
    chapters = number(p.get('downloaded_chapters'))
    images = number(p.get('downloaded_pages')) - chapters
    novels = f" · {display(chapters)} novel {'chapter' if chapters == 1 else 'chapters'}" if chapters else ''
    return f"{display(p.get('completed'))} saved in this step · {display(images)} {'image' if images == 1 else 'images'}{novels}."


@dataclass
class Message:
    """A line the dashboard shows above its own: a failure, or what an action is doing."""
    text: str
    error: bool = False
    until: float = None  # When a failure may go, once updates are arriving again.
    polling: bool = False  # Said because a refresh failed: the next good one clears it.


@dataclass
class State:
    """What the window last read from the app, and what it is doing itself. Times are time.time() seconds."""
    sites: list = field(default_factory=list)
    site_settings: list = field(default_factory=list)
    library_folder: str = None
    cloud_offered: bool = True
    status: dict = None
    account: dict = None
    site_auth: dict = None
    network: dict = None
    cloud: dict = None
    cloud_auth: dict = None
    locations: list = field(default_factory=list)
    task: str = None
    task_kind: str = None
    message: Message = None
    busy: bool = False
    offline: bool = False
    network_saving: bool = False
    account_at: float = 0  # When the account's state arrived, to count on from.
    refreshed_at: float = 0
    connected_at: float = 0
    network_at: float = 0
    library_at: float = 0
    loaded: bool = False  # Whether the library was read at least once.
    summary: object = None  # The saved files' counts: None until read, False when they could not be.
    library_refresh: bool = False  # Something changed the saved files: the next refresh counts them again.
    cloud_message: Message = None  # Said beside the cloud buttons: what a cloud action is doing or did.
    choice_message: str = ''  # Said beside the storage choice when it could not be changed.
    bucket_draft: str = None  # The bucket name typed for B2, until a connection saves it.
    steps_open: bool = False  # Whether a connected B2's setup steps are shown again.
    workspace_id: str = None  # The open library's name, from which a bucket name is suggested.

    def site(self):
        """The open site's name; Tapas until the list of sites is known."""
        return next((s['name'] for s in self.sites if s.get('current') is True), 'Tapas')

    def since_account(self, now):
        return max(0, math.floor(now - self.account_at)) if self.account_at else 0

    def received_account(self, value, now):
        self.account, self.account_at, self.refreshed_at = value, now, now

    def received_site_auth(self, value, now):
        if (value or {}).get('status') == 'connected' and (self.site_auth or {}).get('status') != 'connected':
            self.connected_at = now
        self.site_auth = value

    def refreshed(self, now):
        """A whole refresh came back: updates are arriving again."""
        self.refreshed_at, self.offline = now, False
        if self.message is not None and (self.message.polling or (self.message.until is not None and self.message.until < now)):
            self.message = None

    def refresh_failed(self):
        # A failed refresh says it will retry, rather than showing the raw error; the next good refresh clears it.
        self.offline = True
        self.message = Message('Could not refresh progress. We’ll retry automatically; your saved work is retained.',
                               error=True, polling=True)

    def failed(self, text, now):
        """A refused action, said on the dashboard and in Settings for half a minute of good updates."""
        self.message = Message(text, error=True, until=now + MESSAGE_SECONDS)

    def cloud_targets(self):
        return (self.cloud or {}).get('targets') or []

    def target(self, provider='b2'):
        return next((t for t in self.cloud_targets() if t.get('provider') == provider), None)


def worker_state(s, now):
    """The downloader as the dashboard speaks of it. Right after a sign-in is kept, the downloader can take a minute
    to say it is there: it is starting, not offline."""
    reported = ((s.status or {}).get('worker') or {}).get('reported_state')
    if (reported not in WORKING and (s.site_auth or {}).get('status') == 'connected'
            and now - s.connected_at < STARTING_SECONDS):
        return 'starting'
    return reported


def save_place(s):
    """Where new episodes go, in a word: the cloud storage chosen, else the library folder."""
    chosen = next((t for t in s.cloud_targets() if t.get('connected') and t.get('enabled')), None) if s.cloud_offered else None
    return 'Backblaze B2' if chosen and chosen.get('provider') == 'b2' else 'Cloud storage' if chosen else s.library_folder or ''


def view(s, now):
    """Everything the dashboard and the Downloads settings show, from the state the window holds."""
    site = s.site()
    account = s.account or {}
    status = s.status or {}
    account_status = account.get('status')
    running = account_status == 'running'
    signing_in = (s.site_auth or {}).get('status') == 'running'
    worker = worker_state(s, now)
    progress = status.get('progress') if isinstance(status.get('progress'), dict) else {}
    batch_running = worker == 'busy' or progress.get('status') == 'running'
    needs_login = not status.get('session_configured') or (
        account.get('code') == 'login_required' and (s.site_auth or {}).get('status') != 'connected')
    since = s.since_account(now)
    go_wait = 0 if running else max(0, number(account.get('go_after_seconds')) - since)
    net = s.network
    network_held = bool(net and net.get('wifi_only') and not net.get('allowed'))
    connection_held = ('this connection is metered' if (net or {}).get('reason') == 'metered_connection'
                       else 'this computer cannot tell whether its connection is metered (a VPN can cause this)')
    v = {'site': site}
    # A downloader that is still starting is not offline: signing in again would not help it.
    v['signin_visible'] = not (not needs_login and worker in WORKING and not signing_in and s.message is None)
    v['signin_enabled'] = not (s.busy or running or batch_running or signing_in)
    v['signin_text'] = 'Signing in…' if signing_in else f'Sign in to {site}'
    v['go_enabled'] = not (s.busy or s.task or (s.cloud_auth or {}).get('status') == 'running' or s.account is None
                           or needs_login or signing_in or running or worker != 'ready' or go_wait > 0)
    v['go_text'] = 'Working…' if running else 'Go — continue' if account_status in ('stopped', 'held') else 'Go'
    v['pause_visible'] = running
    v['pause_enabled'] = not s.busy
    v['allow_visible'] = network_held
    v['allow_enabled'] = not s.busy
    locked = running or batch_running
    v['site_enabled'] = not (s.busy or locked or signing_in)
    v['site_tooltip'] = 'Pause saving to change the site.' if locked else ''
    v['folder_enabled'] = not (s.busy or locked or signing_in)
    v['folder_tooltip'] = 'Pause saving before you change the folder.' if locked else ''
    v['folder_lock'] = ('The folder can’t be changed while episodes are downloading. Press Pause on the dashboard first.'
                        if locked else '')
    # Settings lists the speed of each site there is a sign-in for; one that is not signed in has none to choose.
    speeds = {'fast': f'The quickest. If {site} keeps pausing the download, choose a slower speed.',
              'medium': 'About four images a second.',
              'slow': 'About one image a second. A large library can take months at this speed.'}
    speed = account.get('speed') if account.get('speed') in speeds else 'fast'
    others = [item for item in s.site_settings if not item.get('current')]
    signed_in = bool(status.get('session_configured'))
    v['speed_visible'] = signed_in
    v['speed_label'] = f'Speed on {site}' if others else 'Speed'
    v['speed'] = speed
    v['speed_enabled'] = not (s.busy or running or signing_in or s.account is None)
    v['speed_help'] = 'Pause to change the speed.' if running else speeds[speed]
    v['other_speeds'] = [{'key': item['key'], 'label': f"Speed on {item['name']}",
                          'speed': item.get('speed') if item.get('speed') in SPEEDS else None,
                          'help': f"Used the next time you hit Go on {item['name']}.", 'enabled': not s.busy}
                         for item in others]
    v['signin_note_visible'] = not signed_in and not others
    v['signout_visible'] = signed_in
    v['signout_text'] = f'Sign out of {site}'
    v['signout_enabled'] = not (s.busy or running or batch_running or signing_in)
    v['signout_help'] = ('Pause the download to sign out.' if running or batch_running else
                         f'Comic Archiver keeps your {site} sign-in on this computer so it can download. '
                         'Signing out removes it. Your saved episodes stay.')
    v['network_visible'] = net is not None
    v['network_checked'] = (net or {}).get('wifi_only') is False
    v['network_enabled'] = not s.busy and net is not None
    v['network_help'] = (
        'Saving uses any connection, including metered and mobile ones. This may use your data allowance.'
        if (net or {}).get('wifi_only') is False
        else 'Off: saving uses Wi-Fi or a wired connection, and waits on a metered or mobile one.' if (net or {}).get('allowed')
        else 'This connection is metered, so saving waits for Wi-Fi or a wired connection. Tick this to save on it anyway; '
             'this may use your data allowance.' if (net or {}).get('reason') == 'metered_connection'
        else 'This computer cannot tell whether its connection is metered (a VPN can cause this), so saving waits. '
             'Tick this to save on it anyway.')
    auth_messages = {'network_held': 'Connect to Wi-Fi or a wired connection, or press Allow this connection, then sign in again.',
                     'account_mismatch': f'That {site} account does not match this saved library. Sign in to the original account.',
                     'signin_failed': f'Sign-in did not finish. Try Sign in to {site} again.',
                     'signin_incomplete': f'Sign-in was not completed. Click Sign in to {site} to try again.'}
    site_auth = s.site_auth or {}
    v['signin_status'] = (s.message.text if s.message else
                          f'Finish signing in to {site}. We’ll detect it automatically; then return here and hit Go.' if signing_in
                          else auth_messages.get(site_auth.get('code'), auth_messages['signin_failed'])
                          if site_auth.get('status') == 'error' else '')
    v['signin_status_error'] = bool((s.message and s.message.error) or site_auth.get('status') == 'error')
    v['settings_status'] = s.message.text if s.message else ''
    v['settings_status_error'] = bool(s.message and s.message.error)
    v['footnote'] = 'Saving runs only while this window is open. Closing it pauses, and Go carries on later.'
    phases = {'existing_backlog': 'Finishing saved downloads…', 'lists': f'Finding series in your {site} account…',
              'series': 'Checking your series and downloading accessible episodes…', 'archive': 'Downloading your episodes…',
              'verify': 'Checking the completed library…'}
    finished = account_status == 'finished' or (account_status == 'held' and account.get('code') == 'archive_incomplete')
    missing = number((account.get('audit') or {}).get('missing_recorded_accessible_episode_count'))
    if not s.loaded:
        message = 'Connecting to your library…'
    elif signing_in:
        message = f'Finish signing in to {site} in the window that opened.'
    elif needs_login:
        message = (f'Paused: {site} signed you out. Saved files are safe. Sign in to {site} again, then hit Go to continue.'
                   if account.get('code') == 'login_required' else f'Sign in to {site} to get started.')
    elif running:
        message = phases.get(account.get('phase'), 'Downloading your library…')
    elif account_status == 'held' and network_held:
        message = (f'Paused: {connection_held}, and the app saves only on Wi-Fi or a wired connection. Saved files are safe. '
                   'Connect to Wi-Fi, or press Allow this connection, then hit Go to continue.')
    elif account_status == 'held':
        message = held_message(account.get('code'), site, go_wait, missing)
    elif batch_running:
        message = ('Pausing after the image being saved. Your saved files are safe.' if progress.get('cancellation_requested')
                   else 'Finishing the current step. Your saved files are safe.')
    elif worker == 'starting':
        message = 'Starting the downloader…'
    elif worker != 'ready':
        message = f'The downloader is offline. Click Sign in to {site} to reconnect.'
    elif network_held:
        message = (f'Saving would wait: {connection_held}, and the app saves only on Wi-Fi or a wired connection. '
                   'Connect to Wi-Fi, or press Allow this connection.')
    elif account_status == 'stopped':
        message = (f'Paused. {site} asked the app to wait: Go works again in {wait_text(go_wait)}.' if go_wait
                   else 'Paused. Hit Go to continue from saved progress.')
    elif account_status == 'finished':
        message = ('Finished. Every episode your account can read is saved. Hit Go any time to check for new ones.'
                   if number((status.get('episodes_by_status') or {}).get('archived'))
                   else f'Finished. No episodes your {site} account can save were found. Episodes you have bought, or can read '
                        'for free, are saved when there are any. Hit Go to check again.')
    else:
        message = f'Ready. Hit Go to save your {site} library.'
    lists = account.get('lists') if isinstance(account.get('lists'), dict) else {}
    list_names = {'unlocked': 'Bought', 'subscribed': 'Subscribed', 'updated': 'Updated', 'recent': 'Recent',
                  'free_episodes': 'Free episodes'}
    list_phase = running and account.get('phase') == 'lists'
    active = list_names.get(lists.get('current'))
    if list_phase and active:
        message = f"Finding series — {active} list ({display(number(lists.get('checked')) + 1)} of {display(lists.get('total'))})."
    v['status'] = message
    # The choices a held download needs are in Settings: the dashboard leads straight to them.
    v['settings_link_visible'] = (not network_held and account_status == 'held'
                                  and account.get('code') in ('source_refused', 'source_recovery_limit'))
    counts = status.get('episodes_by_status') or {}
    step = '' if list_phase else step_progress(progress)
    older = ' Tapas’s account lists may not include every older purchase.' if finished and site == 'Tapas' else ''
    v['detail'] = ('Loading saved-episode count…' if s.status is None else
                   f"{display(counts.get('archived'))} {'episode' if number(counts.get('archived')) == 1 else 'episodes'} saved."
                   + (' ' + step if step else '') + older)
    v['estimate'] = time_left(status.get('estimate')) if account.get('configured') and not finished else ''
    v['space'] = space_needed(status.get('estimate')) if v['estimate'] else ''
    v['live_visible'] = running
    v['meter_visible'] = v['list_help_visible'] = list_phase
    v['meter_value'] = number(lists.get('checked'))
    v['meter_max'] = number(lists.get('total')) or 4
    if list_phase:
        matching = (progress.get('status') == 'running' and progress.get('phase') == 'account_list'
                    and progress.get('category') == lists.get('current'))
        v['list_count'] = (f"{display(lists.get('checked'))} of {display(lists.get('total') or 4)} lists checked. "
                           + (f"{display(progress['reading_list_page_count'])} pages read · "
                              f"{display(progress.get('observed_library_series_count'))} series listed in {active}."
                              if matching and type(progress.get('reading_list_page_count')) is int
                              else 'Checking series details before moving to the next list.'
                              if progress.get('status') == 'running' and progress.get('phase') == 'scan'
                              else f"Opening the {active or 'next'} list and waiting for {site}."))
    else:
        v['list_count'] = (f"{display(status.get('scan_remaining_series_count'))} known series still awaiting checks."
                           if running and account.get('phase') == 'series' else '')
    age = account['progress_age_seconds'] + since if type(account.get('progress_age_seconds')) is int else None
    elapsed = account['elapsed_seconds'] + since if type(account.get('elapsed_seconds')) is int else None
    since_refresh = max(0, math.floor(now - s.refreshed_at)) if s.refreshed_at else None
    v['clock'] = ' · '.join(part for part in (
        None if elapsed is None else f'Elapsed: {duration(elapsed)}',
        None if age is None else f'Last recorded activity: {duration(age)} ago',
        None if since_refresh is None else f'Status checked {duration(since_refresh)} ago') if part)
    disconnected = s.offline or (since_refresh is not None and since_refresh >= DELAYED_SECONDS)
    # Between two steps, and while the download itself waits to try again, nothing is overdue.
    between = account['between_steps_seconds'] + since if type(account.get('between_steps_seconds')) is int else None
    retry = account.get('retry') if running and isinstance(account.get('retry'), dict) and type(account['retry'].get('seconds')) is int else None
    retry_in = wait_text(max(0, retry['seconds'] - since) if retry else 0)
    waiting = not retry and (age >= 60 if age is not None else between >= 60 if between is not None
                             else elapsed is not None and elapsed >= 60)
    v['live_waiting'] = waiting or disconnected
    if not running:
        activity = ''
    elif disconnected:
        activity = 'Progress updates are delayed. The app is trying to reach its downloader again; what it is doing right now is unknown.'
    elif retry:
        activity = (f'{site} asked the app to wait. The download continues by itself in {retry_in}.' if retry.get('reason') == 'source'
                    else f'Some images did not arrive. The download tries them again by itself in {retry_in}.' if retry.get('reason') == 'images'
                    else f'Waiting: {connection_held}, and the app saves only on Wi-Fi or a wired connection. The download carries on '
                         'by itself on Wi-Fi, or press Allow this connection.' if network_held
                    else f'The connection to {site} was interrupted. The download tries again by itself in {retry_in}.')
    elif waiting:
        activity = f'No new progress for over a minute; {site} may be slow. You can wait, or press Pause.'
    elif (status.get('worker') or {}).get('reported_state') == 'ready':
        activity = 'Working. Getting the next step ready.'
    else:
        activity = 'Working. Progress updates every five seconds.'
    v['activity'] = activity
    v.update(heading(s, worker, running, signing_in, batch_running, needs_login, disconnected, waiting, since_refresh))
    v['save_place'] = save_place(s)
    return v


def heading(s, worker, running, signing_in, batch_running, needs_login, disconnected, waiting, since_refresh):
    """The dashboard's title, its one-word state and the two lines about the account and the downloader."""
    title, label, tone = 'Loading your status…', 'Loading', 'neutral'
    account = s.account or {}
    if s.loaded and s.status is not None and s.account is not None:
        if disconnected:
            title, label, tone = 'Status updates unavailable', 'Reconnecting', 'warning'
        elif signing_in:
            title, label, tone = 'Finish signing in', 'Signing in', 'working'
        elif needs_login:
            title, label, tone = 'Sign in to get started', 'Sign-in needed', 'warning'
        elif running:
            phases = {'lists': ('Finding your series', 'Finding series'), 'series': ('Checking your library', 'Checking series'),
                      'archive': ('Saving your episodes', 'Downloading'), 'verify': ('Checking saved files', 'Checking files'),
                      'existing_backlog': ('Finishing saved downloads', 'Downloading')}
            (title, label), tone = phases.get(account.get('phase'), ('Saving your library', 'Working')), 'working'
            if waiting:
                title, label, tone = 'Waiting for a progress update', 'Waiting', 'warning'
        elif account.get('status') == 'held' and account.get('code') == 'archive_incomplete':
            title, label, tone = 'Finished, with episodes missing', 'Finished', 'warning'
        elif account.get('status') in ('held', 'stopped'):
            title, label, tone = 'Your download is paused', 'Paused', 'warning'
        elif batch_running:
            title, label, tone = 'Finishing the current step', 'Working', 'working'
        elif worker == 'starting':
            title, label, tone = 'Starting the downloader', 'Starting', 'working'
        elif worker != 'ready':
            title, label, tone = 'The downloader is offline', 'Offline', 'warning'
        elif account.get('status') == 'finished':
            title, label, tone = 'This run is finished', 'Finished', 'success'
        else:
            title, label, tone = 'Ready to save', 'Ready', 'success'
    site = s.site()
    session = ('Checking…' if s.status is None else 'Signing in' if signing_in else 'Sign-in needed' if needs_login
               else 'Connected' if (s.site_auth or {}).get('status') == 'connected' else 'Session saved')
    # Before the first sign-in the downloader has nothing to start with: that is not a fault to warn about.
    not_yet = needs_login and worker not in WORKING
    downloader = ('Checking…' if s.status is None else 'Status unknown' if disconnected else 'Working' if worker == 'busy'
                  else 'Ready' if worker == 'ready' else 'Starting' if worker == 'starting'
                  else 'Starts after sign-in' if not_yet else 'Offline')
    return {'title': title, 'state': label, 'tone': tone,
            'session': f'{site} · {session}', 'session_tone': 'warning' if needs_login and s.status is not None else 'neutral',
            'worker': f'Downloader · {downloader}',
            'worker_tone': 'warning' if disconnected or (s.status is not None and not not_yet and worker not in WORKING) else 'neutral',
            'updated': ('Updates delayed · trying to reconnect' if disconnected else 'Checking current status…'
                        if since_refresh is None else 'Updated just now' if since_refresh < 2 else f'Updated {since_refresh}s ago')}
