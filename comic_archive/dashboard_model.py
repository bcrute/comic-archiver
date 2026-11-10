"""What the window's dashboard and download settings say and allow, from what the app last read.

Plain data in, plain data out: no Qt and no files, so every sentence and every locked button is decided here
and tested here. The window only puts the answers on screen.
"""
from dataclasses import dataclass, field
import math

# Each refresh of the dashboard reads the library this often; the clocks are redrawn every second in between.
POLL_SECONDS = 60
# The connection is checked now and then, not on every refresh.
NETWORK_SECONDS = 15
# Saved-file counts are read again at most this often, unless something changed them.
LIBRARY_SECONDS = 3600
# A downloader that has not said it is there this long after a sign-in is offline, not starting.
STARTING_SECONDS = 120
# Updates count as delayed once none has arrived for this long.
DELAYED_SECONDS = 3 * POLL_SECONDS
# A cached progress timestamp must tolerate multiple normal refresh cycles.
NO_PROGRESS_SECONDS = 3 * POLL_SECONDS
# A failure stays on the dashboard this long, so it can be read.
MESSAGE_SECONDS = 30
# The speed shown is the average over the last minute. A second more lets a late refresh count.
SPEED_MIN_SECONDS = 4  # Immediate user actions can request a refresh between scheduled reads.
SPEED_SECONDS = 60 + 1
WORKING = ('starting', 'ready', 'busy')
SPEEDS = ('fast', 'medium', 'slow')
SPEED_NAMES = {'fast': 'Fast', 'medium': 'Medium', 'slow': 'Slow'}
NONE = '—'  # A card's field with nothing to show yet.
ESTIMATING = 'Estimating; known after about five minutes of downloading'
ESTIMATE_TIP = 'A rough guess, based on the download so far.'  # Shown from the ? after a time or space left.

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


def rate(value):
    """A download speed as a person would say it."""
    return f'{value / 1e6:.1f} MB/s' if value >= 999_500 else f'{round(value / 1e3)} KB/s'


def wait_text(seconds):
    return f'about {round(seconds / 60)} minutes' if seconds >= 90 else 'about a minute' if seconds > 45 else 'a few seconds'


def unconfirmed_downloads(estimate):
    """Unchecked series are possible future work, not episodes known to be waiting."""
    return bool(estimate and not number(estimate.get('waiting_episodes'))
                and (number(estimate.get('unchecked_series')) or number(estimate.get('checking_series'))
                     or estimate.get('checking_lists') is True))


def estimate_tip(estimate):
    if unconfirmed_downloads(estimate):
        unchecked = max(number(estimate.get('unchecked_series')), number(estimate.get('checking_series')))
        if not unchecked:
            return 'Your account lists are still being checked. No episodes are currently queued.'
        return (f"No episodes are currently queued. {display(unchecked)} "
                + ('series has' if unchecked == 1 else 'series have')
                + ' no completed check; they may have nothing new to download.')
    return ESTIMATE_TIP


def time_left(estimate, *, background=False):
    waiting, unchecked = number((estimate or {}).get('waiting_episodes')), number((estimate or {}).get('unchecked_series'))
    if not estimate:
        return ''
    if unconfirmed_downloads(estimate):
        unchecked = max(unchecked, number(estimate.get('checking_series')))
        if not unchecked:
            return 'Your account lists are still being checked; download time is unknown until new episodes are found.'
        return (f"{display(unchecked)} series {'has' if unchecked == 1 else 'have'} not been checked yet. "
                'No episodes are currently queued; download time is unknown until new episodes are found.')
    if not (waiting or unchecked):
        return ''
    if type(estimate.get('seconds')) is not int:
        return 'Working out how long this will take. An estimate appears after about five minutes of downloading.'
    left = (f"{display(waiting)} {'episode is' if waiting == 1 else 'episodes are'} waiting"
            + (f" and {display(unchecked)} series {'has' if unchecked == 1 else 'have'} not been checked yet, so this is a rough guess."
               if unchecked else '.'))
    continuation = ('Android may limit background saving. Reopen the app and press Go if saving stops.'
                    if background else 'It only continues while this app is open.')
    return f"About {span(estimate['seconds'])} of downloading left at the pace of the last five minutes. {left} {continuation}"


def space_needed(estimate):
    unchecked = max(number((estimate or {}).get('unchecked_series')), number((estimate or {}).get('checking_series')))
    if not estimate:
        return ''
    if unconfirmed_downloads(estimate):
        free = estimate.get('free_bytes')
        return ('No episodes are currently queued. Download size is unknown until new episodes are found.'
                + (f' This disk has {room(free)} free.' if type(free) is int else ''))
    if not (number(estimate.get('waiting_episodes')) or unchecked):
        free = estimate.get('free_bytes')
        return f'This disk has {room(free)} free.' if type(free) is int else ''
    if type(estimate.get('left_bytes')) is not int:
        return ''
    saved, left, free = number(estimate.get('saved_bytes')), estimate['left_bytes'], estimate.get('free_bytes')
    parts = f': {room(saved)} is saved and about {room(left)} is still to come' if saved else ''
    rough = ' This is a rough guess until every series has been checked.' if unchecked else ''
    disk = ('' if type(free) is not int else f' This disk has only {room(free)} free, which is not enough for the rest.'
            if free < left else f' This disk has {room(free)} free.')
    return f'Your whole library should take about {room(saved + left)} of space{parts}.{rough}{disk}'


# Stable diagnostic categories, also used by Android's count API. Pending downloads are excluded.
NOT_DOWNLOADED = {'scheduled': 'Not released yet',
                  'temporary_access': 'Time-limited, with access the app couldn’t confirm is still current',
                  'novel': 'Novels this site’s download doesn’t support'}


def not_downloaded_counts(status):
    """Raw counts for diagnostic and Android consumers, including ordinary availability states."""
    counts = status.get('episodes_by_status') or {}
    reasons = status.get('unsupported_by_reason') if isinstance(status.get('unsupported_by_reason'), dict) else {}
    known = {key: number(reasons.get(key)) for key in NOT_DOWNLOADED}
    return {'locked': number(counts.get('unavailable'))} | known | {
        'unsupported': max(0, number(counts.get('unsupported')) - sum(known.values())),
        'failed': number(counts.get('failed'))}


def not_downloaded(status, site):
    """Compact exceptions for the desktop. Ordinary account availability stays in diagnostic counts."""
    counts = not_downloaded_counts(status)
    reasons = status.get('unsupported_by_reason')
    reasons = reasons if isinstance(reasons, dict) else {}
    incomplete = sum(number(reasons.get(key)) for key in ('reader_manifest_incomplete', 'novel_manifest_incomplete'))
    ordering = sum(number(reasons.get(key)) for key in ('order_unverified', 'ordering_unverified'))
    rows = [('Download errors', counts['failed']), ('Incomplete content', incomplete),
            ('Page order unknown', ordering), ('Access not confirmed', counts['temporary_access']),
            ('Unsupported format', counts['novel'] + max(0, counts['unsupported'] - incomplete - ordering))]
    return [{'reason': reason, 'count': display(count)} for reason, count in rows if count]


def episode_types(status):
    """Stable rows for recorded downloads, without inventing unknown counts."""
    counts = status.get('episode_types')
    counts = counts if isinstance(counts, dict) else {}
    rows = []
    for key, label in (('free', 'Free'), ('bought', 'Bought / unlocked'),
                       ('limited', 'Limited'), ('unclassified', 'Unknown type')):
        item = counts.get(key)
        item = item if isinstance(item, dict) else {}
        rows.append({'key': key, 'label': label} | {
            field: display(item[field]) if type(item.get(field)) is int and item[field] >= 0 else NONE
            for field in ('saved', 'waiting')})
    return rows


def held_reason(code, site, missing, audit=None):
    """Why a held download stopped, in a few words for its card; what to do is on the card's buttons."""
    if code == 'archive_incomplete':
        audit = audit or {}
        gaps = number(audit.get('series_scan_gap_count'))
        if gaps:
            return f"{display(gaps)} {'series list is' if gaps == 1 else 'series lists are'} incomplete"
        if number(audit.get('inventory_index_problem_count')):
            return 'The saved-file index needs another check'
        cannot = number(audit.get('unsupported_recorded_accessible_episode_count'))
        if not missing and cannot:
            return f"{display(cannot)} {'episode' if cannot == 1 else 'episodes'} could not be saved"
        return (f"{display(missing)} {'episode' if missing == 1 else 'episodes'} could not be saved" if missing
                else 'Completion needs another check')
    reasons = {'source_refused': f'{site} asked the app to slow down',
               'source_recovery_limit': f'{site} asked the app to slow down',
               'source_access_changed': f'{site} still would not send an episode after waiting',
               # While the connection is still held, the card's Connection row says so; this is for after it changed.
               'network_policy': 'The connection was not Wi-Fi or wired',
               'network_unavailable': f'{site} could not be reached several times in a row',
               'network_recovery_limit': f'{site} could not be reached several times in a row',
               'no_progress': 'The last few tries saved nothing new',
               'scan_no_progress': 'The last few tries saved nothing new',
               'cancelled': ''}
    if code in CLOUD_HINTS:
        return f'An upload to cloud storage stopped. {CLOUD_HINTS[code]}'
    return reasons.get(code, 'It stopped unexpectedly; check that this disk has free space')


def time_row(estimate):
    """The card's Time left: about how long, and no more."""
    waiting, unchecked = number((estimate or {}).get('waiting_episodes')), number((estimate or {}).get('unchecked_series'))
    if not estimate:
        return ''
    if unconfirmed_downloads(estimate):
        return 'Unknown until checked'
    if not (waiting or unchecked):
        return ''
    if type(estimate.get('seconds')) is not int:
        return ESTIMATING
    return span(estimate['seconds'])


def space_row(estimate):
    """The card's Space: what is still to come and the disk's free room; and whether that room is too little."""
    waiting, unchecked = number((estimate or {}).get('waiting_episodes')), number((estimate or {}).get('unchecked_series'))
    unchecked = max(unchecked, number((estimate or {}).get('checking_series')))
    if not estimate:
        return '', False
    free = estimate.get('free_bytes')
    if unconfirmed_downloads(estimate):
        return 'No downloads queued' + (f' | {room(free)} free' if type(free) is int else ''), False
    if not (waiting or unchecked):
        return (f'{room(free)} free' if type(free) is int else ''), False
    if type(estimate.get('left_bytes')) is not int:
        return '', False
    left, free = estimate['left_bytes'], estimate.get('free_bytes')
    short = type(free) is int and free < left
    text = f'~{room(left)} estimated' if unchecked else f'{room(left)} to go'
    if type(free) is int:
        text += f' | {room(free)} free'
        if short:
            text += ', may not fit' if unchecked else ', not enough'
    return text, short


def step_progress(p):
    """The step being worked on, as plainly as the dashboard puts it."""
    if not isinstance(p, dict) or p.get('status') != 'running' or p.get('cancellation_requested'):
        return ''
    if p.get('phase') == 'scan' and type(p.get('series_check_target_count')) is int:
        return f"Checked {display(p.get('series_checks_completed'))} of {display(p['series_check_target_count'])} series in this step."
    if p.get('phase') == 'verify':
        stage = p.get('audit_stage')
        if stage == 'discovering':
            return 'Finding saved archives on this device'
        checked = f"{display(p.get('archives_checked'))} / {display(p.get('archives_total'))} archives"
        size = room(p['archive_bytes_checked']) if number(p.get('archive_bytes_checked')) else '0 B'
        return checked + f" | {size}" + (
            f" | {duration(p['audit_elapsed_seconds'])}" if type(p.get('audit_elapsed_seconds')) is int else '') + (
            ' | reconciling saved records' if stage == 'reconciling' else '')
    if p.get('phase') != 'archive':
        return ''
    chapters = number(p.get('downloaded_chapters'))
    images = number(p.get('downloaded_pages')) - chapters
    novels = f" | {display(chapters)} novel {'chapter' if chapters == 1 else 'chapters'}" if chapters else ''
    return f"{display(p.get('completed'))} saved in this step | {display(images)} {'image' if images == 1 else 'images'}{novels}."


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
    library_folder: str = None
    library_root: str = None  # The folder chosen in Settings, which every site's own folder is in.
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
    signin_used: bool = False  # Go has used this sign-in; a later failure outweighs its earlier check.
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
    saved_at: list = field(default_factory=list)  # (time, bytes saved since the app began counting) at each refresh.
    counted: tuple = (None, 0)  # The step last read, and what it had saved then.
    transfer_scope: float = None  # A resumed download begins a fresh speed measurement.

    def site(self):
        """The open site's name; Tapas until the list of sites is known."""
        return next((s['name'] for s in self.sites if s.get('current') is True), 'Tapas')

    def since_account(self, now):
        return max(0, math.floor(now - self.account_at)) if self.account_at else 0

    def received_account(self, value, now):
        self.account, self.account_at, self.refreshed_at = value, now, now
        if (value or {}).get('status') == 'running':
            self.download_started()

    def download_started(self):
        self.signin_used = True

    def received_status(self, value, now, transfer=None):
        self.status = value
        if isinstance(transfer, dict):
            total, scope = transfer.get('downloaded_bytes'), transfer.get('scope')
            if type(total) is int and total >= 0 and type(scope) is float and 0 < scope <= now:
                if self.transfer_scope != scope or self.saved_at and total < self.saved_at[-1][1]:
                    self.saved_at = []
                self.transfer_scope = scope
                self._sample(now, total)
                return
        if self.transfer_scope is not None:
            return  # A missing checkpoint must not turn a cumulative count back into a per-step count.
        last = (value or {}).get('last_run') if isinstance((value or {}).get('last_run'), dict) else {}
        saved = ((value or {}).get('progress') or {}).get('downloaded_bytes')
        # One count across steps, and across checking lists or series: the time between saving images is part of
        # how fast a download goes. A refresh without a count is time in which nothing more was seen saved.
        step, before = self.counted
        total = self.saved_at[-1][1] if self.saved_at else 0
        if type(saved) is int:
            # Each step counts from nothing: what a new one has saved, it saved since the last refresh.
            total += saved - before if step == last.get('id') and saved >= before else saved
            self.counted = (last.get('id'), saved)
        self._sample(now, total)

    def _sample(self, now, total):
        recent = [(at, count) for at, count in self.saved_at if now - at <= SPEED_SECONDS]
        # A scheduled read can arrive late on a busy or sleeping drive. Retain
        # one previous sample so a minute's work still has a measured rate.
        self.saved_at = (recent or self.saved_at[-1:]) + [(now, total)]

    def speed(self):
        """Bytes a second saved over the last minute, or None until two readings are sufficiently separated."""
        (first, start), (last, end) = self.saved_at[0], self.saved_at[-1]
        return (end - start) / (last - first) if last - first >= SPEED_MIN_SECONDS else None

    def received_site_auth(self, value, now):
        if (value or {}).get('status') == 'connected' and (self.site_auth or {}).get('status') != 'connected':
            self.connected_at = now
            self.signin_used = False
        elif (value or {}).get('status') == 'running':
            self.signin_used = False
        self.site_auth = value

    def refreshed(self, now):
        """A whole refresh came back: updates are arriving again."""
        self.refreshed_at, self.offline = now, False
        if self.message is not None and (self.message.polling or (self.message.until is not None and self.message.until < now)):
            self.message = None

    def refresh_failed(self):
        # A failed refresh says it will retry, rather than showing the raw error; the next good refresh clears it.
        self.offline = True
        retry = 'We’ll retry automatically' if updating(self) else 'Start or resume this site to refresh it'
        self.message = Message(f'Could not refresh progress. {retry}; your saved work is retained.',
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
    """Where new episodes go, in a word: the cloud storage chosen, else the folder chosen for every site."""
    chosen = next((t for t in s.cloud_targets() if t.get('connected') and t.get('enabled')), None) if s.cloud_offered else None
    return 'Backblaze B2' if chosen and chosen.get('provider') == 'b2' else 'Cloud storage' if chosen else s.library_root or s.library_folder or ''


CARD_FIELDS = ('Account', 'Downloader', 'Status', 'Step', 'Progress', 'Saved', 'Time left', 'Space',
               'Running for', 'Speed', 'Download speed', 'Note')
DASHBOARD_FIELDS = ('Status', 'Saved', 'Time left', 'Space', 'Speed', 'Download speed')


def card_summary(s=None, v=None):
    """Stable dashboard facts, including estimates; internal step counters stay in diagnostics."""
    if s is None or v is None:
        return {'state': 'Not signed in', 'tone': 'neutral', 'appearance': 'neutral', 'rows': [
            dict(name=name, value='Sign in to get started' if name == 'Status' else NONE, warning=False, tip='')
            for name in DASHBOARD_FIELDS]}
    facts = {row['name']: dict(row) for row in v['rows']}
    label, tone = v['state'], v['tone']
    account = s.account or {}
    running = s.loaded and s.status is not None and account.get('status') == 'running'
    if running and v['state'] not in ('Reconnecting', 'Sign-in needed', 'Signing in'):
        retry = account.get('retry')
        if isinstance(retry, dict):
            text = 'Waiting for Wi-Fi' if v['allow_visible'] else 'Waiting to retry automatically'
            label, tone = 'Waiting', 'working'
        elif v['live_waiting']:
            text, label = 'Waiting for an update', 'Waiting'
        elif account.get('phase') == 'verify':
            text = label = 'Finishing'
        else:
            text = label = 'Working'
        facts['Status'].update(value=text, warning=tone == 'warning')
    elif account.get('status') == 'stopped' and not v['allow_visible'] and v['state'] == 'Paused':
        facts['Status'].update(value='Paused')
    elif account.get('status') == 'finished' and v['state'] == 'Finished':
        purchase = account.get('purchase_history')
        incomplete_history = (s.site() == 'Tapas' and (
            not isinstance(purchase, dict) or purchase.get('status') != 'checked'))
        if incomplete_history:
            facts['Status'].update(value='Finished; some episodes could not be checked', warning=True)
            label, tone = 'Needs attention', 'warning'
        elif number((account.get('audit') or {}).get('unsupported_recorded_accessible_episode_count')):
            facts['Status'].update(value='Finished; some episodes could not be saved', warning=True)
            label, tone = 'Needs attention', 'warning'
    estimate = (s.status or {}).get('estimate') or {}
    if account.get('status') == 'finished' or (account.get('status') == 'held' and account.get('code') == 'archive_incomplete'):
        facts['Time left'].update(value=NONE, tip='')
        facts['Space'].update(value=NONE, tip='', warning=False)
    elif unconfirmed_downloads(estimate):
        for name in ('Time left', 'Space'):
            facts[name].update(value='Estimating…', tip='Estimates become available as episodes are found.', warning=False)
    else:
        time = facts['Time left']['value']
        if time == ESTIMATING:
            facts['Time left']['value'] = 'Estimating…'
        elif time != NONE:
            facts['Time left']['value'] = '~' + time.replace(' minutes', ' min')
        left = estimate.get('left_bytes')
        if type(left) is int and left >= 0 and number(estimate.get('waiting_episodes')):
            tip = ESTIMATE_TIP
            if type(estimate.get('free_bytes')) is int:
                tip += f" {room(estimate['free_bytes'])} free on this disk."
            facts['Space'].update(value='~' + room(left), tip=tip)
        else:
            facts['Space'].update(value=NONE, tip='', warning=False)
    if (label == 'Needs attention' or account.get('status') == 'held'
            or (s.message is not None and s.message.error)):
        appearance = 'attention'
    elif label in ('Waiting', 'Reconnecting', 'Status unavailable', 'Offline'):
        appearance = 'waiting'
    elif label == 'Paused':
        appearance = 'paused'
    elif account.get('status') == 'finished' and label == 'Finished':
        appearance = 'complete'
    elif running:
        appearance = 'active'
    else:
        appearance = 'neutral'
    return {'state': label, 'tone': tone, 'appearance': appearance,
            'rows': [facts[name] for name in DASHBOARD_FIELDS]}


def card_rows(values=None):
    values = values or {}
    return [dict(name=name, value=values.get(name, NONE), warning=False, tip='') for name in CARD_FIELDS]


def unopened_rows():
    """The same permanent fields, including before a site has been opened."""
    return card_rows({'Account': 'Not signed in', 'Downloader': 'Starts after sign-in',
                      'Status': 'Sign in to get started'})


def updating(s):
    """Only activity the person started needs automatic local status reads."""
    return ((s.account or {}).get('status') == 'running' or s.task is not None
            or ((s.status or {}).get('worker') or {}).get('reported_state') == 'busy'
            or (s.site_auth or {}).get('status') == 'running'
            or (s.cloud_auth or {}).get('status') == 'running')


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
    # A fresh sign-in/check can clear an older failure, but never a failure after the next Go.
    auth_status = (s.site_auth or {}).get('status')
    needs_login = not status.get('session_configured') or auth_status == 'expired' or (
        account.get('code') == 'login_required' and (s.signin_used or auth_status not in ('connected', 'verified')))
    since = s.since_account(now)
    go_wait = 0 if running else max(0, number(account.get('go_after_seconds')) - since)
    net = s.network
    network_held = bool(net and net.get('wifi_only') and not net.get('allowed'))
    connection_held = ('this connection is metered' if (net or {}).get('reason') == 'metered_connection'
                       else 'this computer cannot tell whether its connection is metered (a VPN can cause this)')
    v = {'site': site}
    v['go_enabled'] = not (s.busy or s.task or (s.cloud_auth or {}).get('status') == 'running' or s.account is None
                           or needs_login or signing_in or running or worker != 'ready' or go_wait > 0)
    v['pause_visible'] = running
    v['pause_enabled'] = not s.busy
    # One button for what can be done now: signing in and saving never both need doing. A downloader that is still
    # starting is not offline: signing in again would not help it.
    if signing_in or (s.status is not None and not running and (needs_login or worker not in WORKING)):
        v['action'], v['action_text'] = 'signin', 'Signing in…' if signing_in else 'Sign in'
        v['action_enabled'] = not (s.busy or batch_running or signing_in)
    elif running:
        v['action'], v['action_text'], v['action_enabled'] = 'pause', 'Pause', v['pause_enabled']
    else:
        v['action'], v['action_enabled'] = 'go', v['go_enabled']
        v['action_text'] = 'Continue' if account_status in ('stopped', 'held') else 'Start'
    v['allow_visible'] = network_held
    v['allow_enabled'] = not s.busy
    locked = running or batch_running
    # Choosing another site in Settings stops nothing; only a sign-in in progress keeps the window on its site.
    v['site_enabled'] = not (s.busy or signing_in)
    v['folder_enabled'] = not (s.busy or locked or signing_in)
    v['folder_tooltip'] = 'Pause saving before you change the folder.' if locked else ''
    v['folder_lock'] = ('The folder can’t be changed while episodes are downloading. Press Pause on the dashboard first.'
                        if locked else '')
    # Each site's dashboard dropdown changes only that site's pace.
    speeds = {'fast': f'The quickest. If {site} keeps pausing the download, choose a slower speed.',
              'medium': 'About half the Fast speed.',
              'slow': 'About one quarter of the Fast speed.'}
    speed = account.get('speed') if account.get('speed') in speeds else 'fast'
    signed_in = bool(status.get('session_configured'))
    v['speed_visible'] = signed_in
    v['speed_label'] = f'Speed on {site}'
    v['speed'] = speed
    v['speed_enabled'] = not (s.busy or signing_in or s.account is None)
    v['speed_help'] = speeds[speed] + (' Changing speed briefly pauses and resumes this download.' if running else '')
    v['signin_note'] = f'Sign in to {site} to choose how fast it saves.'
    v['signin_note_visible'] = not signed_in
    v['signout_visible'] = signed_in
    v['signout_text'] = f'Sign out of {site}'
    v['signout_enabled'] = not (s.busy or running or batch_running or signing_in)
    v['signout_help'] = ('Pause the download to sign out.' if running or batch_running else
                         f'Comic Archiver keeps your {site} sign-in on this computer so it can download. '
                         f'Signing out removes it from Comic Archiver only. Your {site} account, and anywhere else '
                         'you use it, aren\'t affected. Your saved episodes stay.')
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
                          f'Finish signing in to {site}. We’ll detect it automatically; then return here to start saving.' if signing_in
                          else auth_messages.get(site_auth.get('code'), auth_messages['signin_failed'])
                          if site_auth.get('status') == 'error' else '')
    v['signin_status_error'] = bool((s.message and s.message.error) or site_auth.get('status') == 'error')
    v['settings_status'] = s.message.text if s.message else ''
    v['settings_status_error'] = bool(s.message and s.message.error)
    finished = account_status == 'finished' or (account_status == 'held' and account.get('code') == 'archive_incomplete')
    audit = account.get('audit') if isinstance(account.get('audit'), dict) else {}
    # Episodes in a form the app can't save yet are missing too, but checking again never saves them: they are told apart.
    cannot_save = number(audit.get('unsupported_recorded_accessible_episode_count'))
    missing = max(0, number(audit.get('missing_recorded_accessible_episode_count')) - cannot_save)
    lists = account.get('lists') if isinstance(account.get('lists'), dict) else {}
    list_names = {'unlocked': 'Bought', 'subscribed': 'Subscribed', 'updated': 'Updated', 'recent': 'Recent',
                  'free_episodes': 'Free episodes', 'wait_until_free': 'Wait until free'}
    list_phase = running and account.get('phase') == 'lists'
    active = list_names.get(lists.get('current'))
    counts = status.get('episodes_by_status') or {}
    # Settings › Downloads opens with why checked episodes were not downloaded, once the library is read.
    skipped = not_downloaded(status, site) if s.status is not None else []
    v['not_downloaded_visible'] = bool(skipped) and (bool(status.get('session_configured'))
                                                     or any(row['count'] != '0' for row in skipped))
    v['not_downloaded'] = skipped if v['not_downloaded_visible'] else []
    v['episode_types'] = episode_types(status)
    age = account['progress_age_seconds'] + since if type(account.get('progress_age_seconds')) is int else None
    elapsed = account['elapsed_seconds'] + since if type(account.get('elapsed_seconds')) is int else None
    since_refresh = max(0, math.floor(now - s.refreshed_at)) if s.refreshed_at else None
    disconnected = s.offline or (since_refresh is not None and since_refresh >= DELAYED_SECONDS and updating(s))
    # Between two steps, and while the download itself waits to try again, nothing is overdue.
    between = account['between_steps_seconds'] + since if type(account.get('between_steps_seconds')) is int else None
    retry = account.get('retry') if running and isinstance(account.get('retry'), dict) and type(account['retry'].get('seconds')) is int else None
    retry_in = wait_text(max(0, retry['seconds'] - since) if retry else 0)
    activity_age = age if age is not None else between if between is not None else elapsed
    waiting = running and not retry and activity_age is not None and activity_age >= NO_PROGRESS_SECONDS
    verifying = running and account.get('phase') == 'verify'
    if verifying:
        waiting = False  # Completion bookkeeping is not another download step.
    v['live_waiting'] = waiting or disconnected
    v.update(heading(s, worker, running, signing_in, batch_running, needs_login, disconnected, waiting, since_refresh))
    # The card's facts, a field and its value each. The buttons say what can be done, so the rows only say what is.
    rows = {item['name']: item for item in card_rows({'Status': v['title']})}
    filled = set()

    def row(name, value, warning=False, tip=''):
        if not value:
            return
        if name == 'Continue in':
            value = 'Continue in ' + value
        if name in ('Updates', 'Waiting', 'Stopped', 'Finished', 'Continue in', 'Connection'):
            name = 'Status'
        elif name in ('Lists', 'Series left'):
            value = value.replace(' to check', ' series to check') if name == 'Series left' else value
            name = 'Progress'
        elif name == 'Step' and ' | ' in value:
            value, _, details = value.partition(' | ')
            row('Progress', details)
        value = ' '.join(value.split())
        item = rows[name]
        if name in filled and name in ('Status', 'Progress'):
            value = item['value'] + ' | ' + value
            warning = warning or item['warning']
        item.update(value=value, warning=bool(warning), tip=tip)
        filled.add(name)
    if not s.loaded:
        v['rows'] = card_rows({'Account': 'Checking…', 'Downloader': 'Checking…', 'Status': 'Opening…'})
        v['save_place'] = save_place(s)
        return v
    row('Account', v['session_word'], v['session_tone'] == 'warning')
    row('Downloader', v['worker_word'], v['worker_tone'] == 'warning')
    if running and not disconnected:
        phases = {'existing_backlog': 'Finishing saved downloads', 'lists': 'Finding your series',
                  'series': 'Checking series and downloading', 'archive': 'Downloading episodes',
                  'verify': 'Finishing download'}
        step = '' if list_phase else step_progress(progress).rstrip('.')
        row('Step', ' | '.join(part for part in (phases.get(account.get('phase'), 'Downloading'), step) if part))
        if list_phase:
            matching = (progress.get('status') == 'running' and progress.get('phase') == 'account_list'
                        and progress.get('category') == lists.get('current'))
            row('Lists', f"{display(lists.get('checked'))} of {display(lists.get('total') or 4)} read" + (
                f" | {display(progress['reading_list_page_count'])} pages of {active} | "
                f"{display(progress.get('observed_library_series_count'))} series"
                if matching and type(progress.get('reading_list_page_count')) is int else f' | now {active}' if active else ''))
        elif account.get('phase') == 'series':
            row('Series left', f"{display(status.get('scan_remaining_series_count'))} to check")
        elif account.get('phase') == 'archive' and number(status.get('scan_remaining_series_count')):
            row('Progress', f"{display(status['scan_remaining_series_count'])} series waiting")
    if disconnected and running:
        row('Updates', 'Delayed: reaching the downloader again', True)
    elif retry:
        row('Waiting', (f'{site} asked the app to wait' if retry.get('reason') == 'source'
                        else 'Some images did not arrive' if retry.get('reason') == 'images'
                        else 'The last step saved nothing new' if retry.get('reason') == 'idle'
                        else 'For Wi-Fi or a wired connection' if network_held
                        else f'The connection to {site} was interrupted')
            + ('' if network_held else f'; carries on in {retry_in}'), True)
    elif running and waiting:
        row('Waiting', f'No new progress reported for {duration(activity_age)}', True)
    elif batch_running and not running:
        row('Step', 'Pausing after the image being saved' if progress.get('cancellation_requested')
            else 'Finishing the current step')
    if account_status == 'held' and not needs_login and not running:
        detail = {'series_scan_gap_count': status.get('series_scan_gap_count', 0)} | audit
        reason = held_reason(account.get('code'), site, missing, detail)
        row('Finished' if finished else 'Stopped', reason, True)
    elif account.get('code') == 'login_required' and needs_login:
        row('Stopped', f'{site} signed you out', True)
    if not running and go_wait:
        row('Continue in', wait_text(go_wait))
    if network_held and not running:
        row('Connection', f'{connection_held[0].upper()}{connection_held[1:]}; saving waits for Wi-Fi or a wired connection', True)
    if account_status == 'finished':
        row('Finished', 'Done')
    # Saved, Time left, Space, Speed and Download speed are on every card, so cards side by side read alike; NONE where nothing is known.
    row('Saved', NONE if s.status is None else
        f"{display(counts.get('archived'))} {'episode' if number(counts.get('archived')) == 1 else 'episodes'}")
    estimate = status.get('estimate') if account.get('configured') else None
    if estimate is not None:
        estimate = dict(estimate, checking_lists=list_phase)
    time = time_row(estimate) if not finished else ''
    row('Time left', time or NONE, tip=estimate_tip(estimate) if time and time != ESTIMATING else '')
    space, short = space_row({'free_bytes': estimate.get('free_bytes')} if finished and estimate else estimate)
    estimated_space = estimate and (number(estimate.get('waiting_episodes')) or unconfirmed_downloads(estimate))
    row('Space', space or NONE, short, estimate_tip(estimate) if space and estimated_space and not finished else '')
    if running and elapsed is not None:
        row('Running for', duration(elapsed) + ('' if age is None else
            f" | last {'check update' if verifying else 'saved'} {duration(age)} ago"))
    refused = signed_in and account_status == 'held' and account.get('code') in ('source_refused', 'source_recovery_limit')
    row('Speed', NONE if not signed_in else
        SPEED_NAMES[speed] + (' (a slower speed may help)' if refused and speed != 'slow' else ''), refused)
    # What arrives, apart from Speed, which paces the requests.
    measured = s.speed() if running and s.saved_at else None
    row('Download speed', NONE if measured is None else rate(measured))
    notes = []
    if account_status == 'finished' and cannot_save:
        notes.append(f'{display(cannot_save)} episodes use a format the app can’t save yet')
    purchase = account.get('purchase_history')
    if finished and site == 'Tapas' and not isinstance(purchase, dict):
        notes.append('Purchase history not checked')
    if site == 'Tapas' and isinstance(purchase, dict):
        if purchase.get('status') == 'pending':
            notes.append('Checking purchase history')
        elif purchase.get('status') == 'partial':
            reasons = {'history_view_unverified': 'Purchase-history view could not be verified',
                       'history_rows_unverified': 'Purchase-history entries need another check',
                       'history_page_repeated': 'Purchase-history paging repeated a page',
                       'history_continuation_unverified': 'Purchase-history paging could not be verified',
                       'history_association_unresolved': 'Some purchase-history entries could not be matched to series',
                       'history_checkpoint_unreadable': 'Purchase-history progress could not be read',
                       'history_reader_unavailable': 'Purchase-history checking is unavailable in this reader'}
            notes.append(reasons.get(purchase.get('code'), 'Purchase-history check incomplete'))
    if notes:
        row('Note', ' | '.join(notes))
    v['rows'] = list(rows.values())
    v['save_place'] = save_place(s)
    return v


def heading(s, worker, running, signing_in, batch_running, needs_login, disconnected, waiting, since_refresh):
    """The dashboard's title, its one-word state and the two lines about the account and the downloader."""
    title, label, tone = 'Loading your status…', 'Loading', 'neutral'
    account = s.account or {}
    if s.loaded and s.status is not None and s.account is not None:
        if disconnected:
            title, label, tone = ('Status updates unavailable', 'Reconnecting', 'warning') if updating(s) else (
                'Status unavailable', 'Status unavailable', 'warning')
        elif signing_in:
            title, label, tone = 'Finish signing in', 'Signing in', 'working'
        elif needs_login:
            title = 'Sign in again' if (s.site_auth or {}).get('status') == 'expired' else 'Sign in to get started'
            label, tone = 'Sign-in needed', 'warning'
        elif running:
            phases = {'lists': ('Finding your series', 'Finding series'), 'series': ('Checking your library', 'Checking series'),
                      'archive': ('Saving your episodes', 'Downloading'), 'verify': ('Finishing download', 'Finishing'),
                      'existing_backlog': ('Finishing saved downloads', 'Downloading')}
            (title, label), tone = phases.get(account.get('phase'), ('Saving your library', 'Working')), 'working'
            if waiting:
                title, label, tone = 'Waiting for a progress update', 'Waiting', 'warning'
        elif account.get('status') == 'held' and account.get('code') == 'archive_incomplete':
            title, label, tone = 'Completion needs attention', 'Needs attention', 'warning'
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
               else 'Connected' if (s.site_auth or {}).get('status') in ('connected', 'verified') else 'Session saved')
    # Before the first sign-in the downloader has nothing to start with: that is not a fault to warn about.
    not_yet = needs_login and worker not in WORKING
    downloader = ('Checking…' if s.status is None else 'Status unknown' if disconnected else 'Working' if worker == 'busy'
                  else 'Ready' if worker == 'ready' else 'Starting' if worker == 'starting'
                  else 'Starts after sign-in' if not_yet else 'Offline')
    return {'title': title, 'state': label, 'tone': tone, 'session_word': session, 'worker_word': downloader,
            'session': f'{site} · {session}', 'session_tone': 'warning' if needs_login and s.status is not None else 'neutral',
            'worker': f'Downloader · {downloader}',
            'worker_tone': 'warning' if disconnected or (s.status is not None and not not_yet and worker not in WORKING) else 'neutral',
            'updated': ('Status unavailable · start to refresh' if disconnected and not updating(s) else
                        'Updates delayed · trying to reconnect' if disconnected else 'Checking current status…'
                        if since_refresh is None else 'Updated just now' if since_refresh < 2 else f'Updated {since_refresh}s ago')}
