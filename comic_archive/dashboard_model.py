"""What the window's dashboard and download settings say and allow, from what the app last read.

Plain data in, plain data out: no Qt and no files, so every sentence and every locked button is decided here
and tested here. The window only puts the answers on screen.
"""
from dataclasses import dataclass, field
import math

from .messages import say

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
SPEED_NAMES = {'fast': say('dashboard.fast'), 'medium': say('dashboard.medium'), 'slow': say('dashboard.slow')}
NONE = '—'  # A card's field with nothing to show yet.
ESTIMATING = say('dashboard.estimating_known_after_about_five_minutes')
ESTIMATE_TIP = say('dashboard.a_rough_guess_based_on_the')  # Shown from the ? after a time or space left.

# Recovery hints for cloud uploads, as the window words them.
CLOUD_HINTS = {
    'b2_key_permissions': say('dashboard.create_a_read_and_write_key'),
    'b2_bucket_mismatch': say('dashboard.this_key_is_for_a_different'),
    'b2_private_bucket': say('dashboard.choose_private_for_your_b2_library'),
    'cloud_login_required': say('dashboard.cloud_sign_in_needs_repair_click'),
    'cloud_space': say('dashboard.the_provider_reported_insufficient_cloud_storage'),
    'cloud_cap': say('dashboard.backblaze_stopped_the_upload_your_account'),
    'cloud_rate_limited': say('dashboard.the_provider_limited_requests_wait_before'),
    'cloud_connection': say('dashboard.the_cloud_transfer_was_unavailable_or'),
    'cloud_permission': say('dashboard.the_provider_refused_write_access_check'),
    'cloud_verification': say('dashboard.the_cloud_copy_could_not_be'),
    'cloud_transfer_limit': say('dashboard.a_cloud_transfer_limit_was_reached'),
    'cloud_network_held': say('dashboard.the_network_policy_held_the_cloud'),
    'cloud_unknown': say('dashboard.the_cloud_transfer_failed_for_an'),
}
SAFE = say('dashboard.saved_files_are_safe')


def error_text(code, site='Tapas'):
    """What a refused action says, for the code the app gave it."""
    fixed = {'library_busy': say('dashboard.an_import_is_active_try_again'),
             'service_unavailable': say('dashboard.the_service_is_temporarily_unavailable_try'),
             'request_unavailable': say('dashboard.this_action_is_unavailable_check_the'),
             'login_required': say('dashboard.your_session_needs_a_fresh_login', site=site),
             'source_refused': say('dashboard.refused_a_request_check_in_your', site=site, site2=site),
             'task_failed': say('dashboard.the_task_stopped_completed_archives_and')}
    if code in CLOUD_HINTS:
        return CLOUD_HINTS[code] + say('dashboard.local_episodes_are_retained')
    return fixed.get(code, say('dashboard.the_action_could_not_be_completed'))


def number(value):
    """A count the app reported, or 0 for anything that is not one."""
    return value if type(value) is int and value >= 0 else 0


def display(value):
    return f'{number(value):,}'


def duration(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return None
    if value >= 3600:
        return say('dashboard.h_m', value=math.floor(value / 3600), value2=math.floor(value % 3600 / 60))
    return f'{math.floor(value / 60)}m {math.floor(value % 60)}s'


def span(seconds):
    """A length of time as a person would say it: the estimate is rough, and so is this."""
    hours = seconds / 3600
    days = hours / 24
    if hours < 0.85:
        return f'{max(5, round(seconds / 300) * 5)} minutes'
    if hours < 1.5:
        return say('dashboard.an_hour')
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
    return say('dashboard.about_minutes', value=round(seconds / 60)) if seconds >= 90 else say('dashboard.about_a_minute') if seconds > 45 else say('dashboard.a_few_seconds')


def unconfirmed_downloads(estimate):
    """Nothing waiting and nothing to guess from: only series being checked again, or the account's lists still being
    read. A series never checked is guessed at (estimate.EPISODES_PER_SERIES) whether or not episodes are queued at
    that moment, so the figure stays put as each series' episodes are saved and the next series is read."""
    return bool(estimate and not number(estimate.get('waiting_episodes')) and not number(estimate.get('unchecked_series'))
                and (number(estimate.get('checking_series')) or estimate.get('checking_lists') is True))


def estimate_tip(estimate):
    if unconfirmed_downloads(estimate):
        unchecked = max(number(estimate.get('unchecked_series')), number(estimate.get('checking_series')))
        if not unchecked:
            return say('dashboard.your_account_lists_are_still_being')
        return (say('dashboard.no_episodes_are_currently_queued', n=display(unchecked))
                + (say('dashboard.series_has') if unchecked == 1 else say('dashboard.series_have'))
                + ' no completed check; they may have nothing new to download.')
    return ESTIMATE_TIP


def time_left(estimate, *, background=False):
    waiting, unchecked = number((estimate or {}).get('waiting_episodes')), number((estimate or {}).get('unchecked_series'))
    if not estimate:
        return ''
    if unconfirmed_downloads(estimate):
        unchecked = max(unchecked, number(estimate.get('checking_series')))
        if not unchecked:
            return say('dashboard.your_account_lists_are_still_being_2')
        return (f"{display(unchecked)} series {'has' if unchecked == 1 else 'have'} not been checked yet. "
                'No episodes are currently queued; download time is unknown until new episodes are found.')
    if not (waiting or unchecked):
        return ''
    if type(estimate.get('seconds')) is not int:
        return say('dashboard.working_out_how_long_this_will')
    left = (f"{display(waiting)} {say('dashboard.episode_is') if waiting == 1 else say('dashboard.episodes_are')} waiting"
            + (say('dashboard.and_series_not_been_checked_yet', n=display(unchecked), value='has' if unchecked == 1 else 'have')
               if unchecked else '.'))
    continuation = (say('dashboard.android_may_limit_background_saving_reopen')
                    if background else say('dashboard.it_only_continues_while_this_app'))
    return say('dashboard.about_of_downloading_left_at_the', value=span(estimate['seconds']), left=left, continuation=continuation)


def space_needed(estimate):
    unchecked = max(number((estimate or {}).get('unchecked_series')), number((estimate or {}).get('checking_series')))
    if not estimate:
        return ''
    if unconfirmed_downloads(estimate):
        free = estimate.get('free_bytes')
        return (say('dashboard.no_episodes_are_currently_queued_download')
                + (say('dashboard.this_disk_has_free_2', value=room(free)) if type(free) is int else ''))
    if not (number(estimate.get('waiting_episodes')) or unchecked):
        free = estimate.get('free_bytes')
        return say('dashboard.this_disk_has_free', value=room(free)) if type(free) is int else ''
    if type(estimate.get('left_bytes')) is not int:
        return ''
    saved, left, free = number(estimate.get('saved_bytes')), estimate['left_bytes'], estimate.get('free_bytes')
    parts = say('dashboard.is_saved_and_about_is_still', value=room(saved), value2=room(left)) if saved else ''
    rough = say('dashboard.this_is_a_rough_guess_until') if unchecked else ''
    disk = ('' if type(free) is not int else say('dashboard.this_disk_has_only_free_which', value=room(free))
            if free < left else say('dashboard.this_disk_has_free_2', value=room(free)))
    return say('dashboard.your_whole_library_should_take_about', value=room(saved + left), parts=parts, rough=rough, disk=disk)


# Stable diagnostic categories, also used by Android's count API. Pending downloads are excluded.
NOT_DOWNLOADED = {'scheduled': say('dashboard.not_released_yet'),
                  'temporary_access': say('dashboard.time_limited_with_access_the_app'),
                  'novel': say('dashboard.novels_this_sites_download_doesnt_support')}


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
    rows = [(say('dashboard.download_errors'), counts['failed']), (say('dashboard.incomplete_content'), incomplete),
            (say('dashboard.page_order_unknown'), ordering), (say('dashboard.access_not_confirmed'), counts['temporary_access']),
            (say('dashboard.unsupported_format'), counts['novel'] + max(0, counts['unsupported'] - incomplete - ordering))]
    rows = [{'reason': reason, 'count': display(count)} for reason, count in rows if count]
    gaps = status.get('series_scan_gaps')
    set_aside = sum(isinstance(gap, dict) and gap.get('code') == 'series_set_aside' for gap in gaps) if isinstance(gaps, list) else 0
    if set_aside:  # Counted in series, not episodes: their episodes are not known until the list is read.
        rows.append({'reason': say('dashboard.series_set_aside'), 'count': say('dashboard.n_series', n=display(set_aside))})
    return rows


def episode_types(status):
    """Stable rows for recorded downloads, without inventing unknown counts."""
    counts = status.get('episode_types')
    counts = counts if isinstance(counts, dict) else {}
    rows = []
    for key, label in (('free', say('dashboard.free')), ('bought', 'Bought / unlocked'),
                       ('limited', say('dashboard.limited')), ('unclassified', say('dashboard.unknown_type'))):
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
            return f"{display(gaps)} {say('dashboard.series_list_is') if gaps == 1 else say('dashboard.series_lists_are')} incomplete"
        if number(audit.get('inventory_index_problem_count')):
            return say('dashboard.the_saved_file_index_needs_another')
        cannot = number(audit.get('unsupported_recorded_accessible_episode_count'))
        if not missing and cannot:
            return say('dashboard.could_not_be_saved', n=display(cannot), value='episode' if cannot == 1 else 'episodes')
        return (say('dashboard.could_not_be_saved', n=display(missing), value='episode' if missing == 1 else 'episodes') if missing
                else say('dashboard.completion_needs_another_check'))
    reasons = {'source_refused': say('dashboard.asked_the_app_to_slow_down', site=site),
               'source_recovery_limit': say('dashboard.asked_the_app_to_slow_down', site=site),
               'source_access_changed': say('dashboard.still_would_not_send_an_episode', site=site),
               # While the connection is still held, the card's Connection row says so; this is for after it changed.
               'network_policy': say('dashboard.the_connection_was_not_wi_fi'),
               'network_unavailable': say('dashboard.could_not_be_reached_several_times', site=site),
               'network_recovery_limit': say('dashboard.could_not_be_reached_several_times', site=site),
               'no_progress': say('dashboard.the_last_few_tries_saved_nothing'),
               'scan_no_progress': say('dashboard.the_last_few_tries_saved_nothing'),
               # Shown once signed in again; until then the card says the site signed you out.
               'login_required': say('dashboard.the_sign_in_had_expired'),
               'cancelled': ''}
    if code in CLOUD_HINTS:
        return say('dashboard.an_upload_to_cloud_storage_stopped', value=CLOUD_HINTS[code])
    return reasons.get(code, say('dashboard.it_stopped_unexpectedly_if_it_stops'))


def time_row(estimate):
    """The card's Time left: about how long, and no more."""
    waiting, unchecked = number((estimate or {}).get('waiting_episodes')), number((estimate or {}).get('unchecked_series'))
    if not estimate:
        return ''
    if unconfirmed_downloads(estimate):
        return say('dashboard.unknown_until_checked')
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
        return say('dashboard.no_downloads_queued') + (f' | {room(free)} free' if type(free) is int else ''), False
    if not (waiting or unchecked):
        return (f'{room(free)} free' if type(free) is int else ''), False
    if type(estimate.get('left_bytes')) is not int:
        return '', False
    left, free = estimate['left_bytes'], estimate.get('free_bytes')
    short = type(free) is int and free < left
    text = f'~{room(left)} estimated' if unchecked else say('dashboard.to_go', value=room(left))
    if type(free) is int:
        text += f' | {room(free)} free'
        if short:
            text += say('dashboard.may_not_fit') if unchecked else say('dashboard.not_enough')
    return text, short


def step_progress(p):
    """The step being worked on, as plainly as the dashboard puts it."""
    if not isinstance(p, dict) or p.get('status') != 'running' or p.get('cancellation_requested'):
        return ''
    if p.get('phase') == 'scan' and type(p.get('series_check_target_count')) is int:
        return say('dashboard.checked_of_series_in_this_step', n=display(p.get('series_checks_completed')), n2=display(p['series_check_target_count']))
    if p.get('phase') == 'verify':
        stage = p.get('audit_stage')
        if stage == 'discovering':
            return say('dashboard.finding_saved_archives_on_this_device')
        checked = f"{display(p.get('archives_checked'))} / {display(p.get('archives_total'))} archives"
        size = room(p['archive_bytes_checked']) if number(p.get('archive_bytes_checked')) else '0 B'
        return checked + f" | {size}" + (
            f" | {duration(p['audit_elapsed_seconds'])}" if type(p.get('audit_elapsed_seconds')) is int else '') + (
            say('dashboard.reconciling_saved_records') if stage == 'reconciling' else '')
    if p.get('phase') != 'archive':
        return ''
    chapters = number(p.get('downloaded_chapters'))
    images = number(p.get('downloaded_pages')) - chapters
    novels = say('dashboard.novel', n=display(chapters), value='chapter' if chapters == 1 else 'chapters') if chapters else ''
    return say('dashboard.saved_in_this_step', n=display(p.get('completed')), n2=display(images), value='image' if images == 1 else 'images', novels=novels)


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
    start_read: float = 0  # The sign-in (its connected_at) whose downloader was looked at once its time was up.
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
        retry = say('dashboard.well_retry_automatically') if updating(self) else say('dashboard.start_or_resume_this_site_to')
        self.message = Message(say('dashboard.could_not_refresh_progress_your_saved', retry=retry),
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


def start_overdue(s, now):
    """Whether a sign-in's downloader has had its time to start and has not been heard to say it is there: the
    moment to look once more, before the card says it is not working."""
    reported = ((s.status or {}).get('worker') or {}).get('reported_state')
    return bool(reported not in WORKING and (s.site_auth or {}).get('status') == 'connected' and s.connected_at
                and now - s.connected_at >= STARTING_SECONDS and s.start_read != s.connected_at)


def save_place(s):
    """Where new episodes go, in a word: the cloud storage chosen, else the folder chosen for every site."""
    chosen = next((t for t in s.cloud_targets() if t.get('connected') and t.get('enabled')), None) if s.cloud_offered else None
    return say('cloud.backblaze_b2') if chosen and chosen.get('provider') == 'b2' else say('dashboard.cloud_storage') if chosen else s.library_root or s.library_folder or ''


CARD_FIELDS = (say('dashboard.account'), say('dashboard.downloader'), say('dashboard.status'), say('dashboard.step'), say('dashboard.progress'), say('settings.saved'), say('dashboard.time_left'), say('dashboard.space'),
               say('dashboard.running_for'), say('dashboard.speed'), say('dashboard.download_speed'), say('dashboard.note'))
DASHBOARD_FIELDS = (say('dashboard.status'), say('settings.saved'), say('dashboard.time_left'), say('dashboard.space'), say('dashboard.speed'), say('dashboard.download_speed'))


def pill(state):
    """A card's state in a word short enough for its pill."""
    return {say('dashboard.needs_attention'): say('window.attention'), say('cloud.sign_in_needed'): say('dashboard.sign_in'),
            say('dashboard.finished'): say('dashboard.done'),
            say('dashboard.status_unavailable'): say('dashboard.offline')}.get(state, state)


def download_choice(account):
    """A card's line for the episode types its site downloads, and what each chosen type takes in.

    account is the site's account record, or None for a site not opened: every type is then chosen."""
    from .download_selection import HELP, LABELS, TYPES
    chosen = (account or {}).get('download_types', dict.fromkeys(TYPES, True))
    if not isinstance(chosen, dict):
        return say('window.download_unavailable'), say('window.saved_choices_could_not_be_read')
    count = sum(bool(chosen.get(key)) for key in TYPES)
    value = (say('window.all_types') if count == len(TYPES) else say('window.paid_series') if chosen.get('bought') and count == 1
             else LABELS[next(key for key in TYPES if chosen.get(key))] if count == 1 else say('window.n_types', n=count))
    return say('window.download_value', value=value), '\n'.join(f'{LABELS[key]}: {HELP[key]}' for key in TYPES if chosen.get(key))


def card_summary(s=None, v=None):
    """Stable dashboard facts, including estimates; internal step counters stay in diagnostics."""
    if s is None or v is None:
        return {'state': say('dashboard.not_signed_in'), 'tone': 'neutral', 'appearance': 'neutral', 'rows': [
            dict(name=name, value=say('dashboard.sign_in_to_get_started') if name == say('dashboard.status') else NONE, warning=False, tip='')
            for name in DASHBOARD_FIELDS]}
    facts = {row['name']: dict(row) for row in v['rows']}
    label, tone = v['state'], v['tone']
    account = s.account or {}
    running = s.loaded and s.status is not None and account.get('status') == 'running'
    if running and v['state'] not in (say('dashboard.reconnecting'), say('cloud.sign_in_needed'), say('dashboard.signing_in')):
        retry = account.get('retry')
        if isinstance(retry, dict):
            text = say('dashboard.waiting_for_wi_fi') if v['allow_visible'] else say('dashboard.waiting_to_retry_automatically')
            label, tone = say('dashboard.waiting'), 'working'
        elif v['live_waiting']:
            text, label = say('dashboard.waiting_for_an_update'), say('dashboard.waiting')
        elif account.get('phase') == 'verify':
            text = label = say('dashboard.finishing')
        else:
            text, label = say('dashboard.checking_and_saving_your_episodes'), say('downloads.working')
        facts[say('dashboard.status')].update(value=text, warning=tone == 'warning')
    elif account.get('status') == 'stopped' and not v['allow_visible'] and v['state'] == say('dashboard.paused'):
        facts[say('dashboard.status')].update(value=say('dashboard.paused'))
    elif account.get('status') == 'finished' and v['state'] == say('dashboard.finished'):
        purchase = account.get('purchase_history')
        incomplete_history = (s.site() == 'Tapas' and (
            not isinstance(purchase, dict) or purchase.get('status') != 'checked'))
        if incomplete_history:
            facts[say('dashboard.status')].update(value=say('dashboard.finished_some_episodes_could_not_be'), warning=True)
            label, tone = say('dashboard.needs_attention'), 'warning'
        elif number((account.get('audit') or {}).get('unsupported_recorded_accessible_episode_count')):
            facts[say('dashboard.status')].update(value=say('dashboard.finished_some_episodes_could_not_be_2'), warning=True)
            label, tone = say('dashboard.needs_attention'), 'warning'
        elif facts[say('dashboard.status')]['value'] == say('dashboard.done'):
            facts[say('dashboard.status')].update(value=say('dashboard.this_run_is_complete'))
    estimate = (s.status or {}).get('estimate') or {}
    if account.get('status') == 'finished' or (account.get('status') == 'held' and account.get('code') == 'archive_incomplete'):
        facts[say('dashboard.time_left')].update(value=NONE, tip='')
        facts[say('dashboard.space')].update(value=NONE, tip='', warning=False)
    elif unconfirmed_downloads(estimate):
        for name in (say('dashboard.time_left'), say('dashboard.space')):
            facts[name].update(value=say('dashboard.estimating'), tip=say('dashboard.estimates_become_available_as_episodes_are'), warning=False)
    else:
        time = facts[say('dashboard.time_left')]['value']
        if time == ESTIMATING:
            facts[say('dashboard.time_left')]['value'] = say('dashboard.estimating')
        elif time != NONE:
            facts[say('dashboard.time_left')]['value'] = '~' + time.replace(' minutes', ' min')
        left = estimate.get('left_bytes')
        if type(left) is int and left >= 0 and (number(estimate.get('waiting_episodes')) or number(estimate.get('unchecked_series'))):
            tip = ESTIMATE_TIP
            if type(estimate.get('free_bytes')) is int:
                tip += say('dashboard.free_on_this_disk', value=room(estimate['free_bytes']))
            facts[say('dashboard.space')].update(value='~' + room(left), tip=tip)
        else:
            facts[say('dashboard.space')].update(value=NONE, tip='', warning=False)
    if (label == say('dashboard.needs_attention') or account.get('status') == 'held'
            or (s.message is not None and s.message.error)):
        appearance = 'attention'
    elif label in (say('dashboard.waiting'), say('dashboard.reconnecting'), say('dashboard.status_unavailable'), say('dashboard.offline')):
        appearance = 'waiting'
    elif label == say('dashboard.paused'):
        appearance = 'paused'
    elif account.get('status') == 'finished' and label == say('dashboard.finished'):
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
    return card_rows({say('dashboard.account'): say('dashboard.not_signed_in'), say('dashboard.downloader'): say('dashboard.starts_after_sign_in'),
                      say('dashboard.status'): say('dashboard.sign_in_to_get_started')})


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
    connection_held = (say('dashboard.this_connection_is_metered') if (net or {}).get('reason') == 'metered_connection'
                       else say('dashboard.this_computer_cannot_tell_whether_its_2'))
    v = {'site': site}
    v['go_enabled'] = not (s.busy or s.task or (s.cloud_auth or {}).get('status') == 'running' or s.account is None
                           or needs_login or signing_in or running or worker != 'ready' or go_wait > 0)
    v['pause_visible'] = running
    v['pause_enabled'] = not s.busy
    # One button for what can be done now: signing in and saving never both need doing. A downloader that is still
    # starting is not offline: signing in again would not help it.
    if signing_in or (s.status is not None and not running and (needs_login or worker not in WORKING)):
        v['action'], v['action_text'] = 'signin', say('dashboard.signing_in_2') if signing_in else say('dashboard.sign_in')
        v['action_enabled'] = not (s.busy or batch_running or signing_in)
    elif running:
        v['action'], v['action_text'], v['action_enabled'] = 'pause', say('downloads.pause'), v['pause_enabled']
    else:
        v['action'], v['action_enabled'] = 'go', v['go_enabled']
        v['action_text'] = say('dashboard.continue') if account_status in ('stopped', 'held') else say('dashboard.start')
    v['allow_visible'] = network_held
    v['allow_enabled'] = not s.busy
    locked = running or batch_running
    # Choosing another site in Settings stops nothing; only a sign-in in progress keeps the window on its site.
    v['site_enabled'] = not (s.busy or signing_in)
    v['folder_enabled'] = not (s.busy or locked or signing_in)
    v['folder_tooltip'] = say('dashboard.pause_saving_before_you_change_the') if locked else ''
    v['folder_lock'] = (say('dashboard.the_folder_cant_be_changed_while')
                        if locked else '')
    # Each site's dashboard dropdown changes only that site's pace.
    speeds = {'fast': say('dashboard.the_quickest_if_keeps_pausing_the', site=site),
              'medium': say('dashboard.about_half_the_fast_speed'),
              'slow': say('dashboard.about_one_quarter_of_the_fast')}
    speed = account.get('speed') if account.get('speed') in speeds else 'fast'
    signed_in = bool(status.get('session_configured'))
    v['speed_visible'] = signed_in
    v['speed_label'] = say('dashboard.speed_on', site=site)
    v['speed'] = speed
    v['speed_enabled'] = not (s.busy or signing_in or s.account is None)
    v['speed_help'] = speeds[speed] + (say('dashboard.changing_speed_briefly_pauses_and_resumes') if running else '')
    v['signin_note'] = say('dashboard.sign_in_to_to_choose_how', site=site)
    v['signin_note_visible'] = not signed_in
    v['signout_visible'] = signed_in
    v['signout_text'] = say('dashboard.sign_out_of', site=site)
    v['signout_enabled'] = not (s.busy or running or batch_running or signing_in)
    v['signout_help'] = (say('app.pause_the_download_to_sign_out') if running or batch_running else
                         say('dashboard.comic_archiver_keeps_your_sign_in', site=site, site2=site))
    v['network_visible'] = net is not None
    v['network_checked'] = (net or {}).get('wifi_only') is False
    v['network_enabled'] = not s.busy and net is not None
    v['network_help'] = (
        say('dashboard.saving_uses_any_connection_including_metered')
        if (net or {}).get('wifi_only') is False
        else say('dashboard.off_saving_uses_wi_fi_or') if (net or {}).get('allowed')
        else say('dashboard.this_connection_is_metered_so_saving') if (net or {}).get('reason') == 'metered_connection'
        else say('dashboard.this_computer_cannot_tell_whether_its'))
    auth_messages = {code: say(f'signin.{code}', site=site)
                     for code in ('network_held', 'account_mismatch', 'failed', 'incomplete')}
    site_auth = s.site_auth or {}
    v['signin_status'] = (s.message.text if s.message else
                          say('dashboard.finish_signing_in_to_well_detect', site=site) if signing_in
                          else auth_messages.get(str(site_auth.get('code')).removeprefix('signin_'), auth_messages['failed'])
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
    list_names = {'unlocked': say('downloads.bought'), 'subscribed': say('downloads.subscribed'), 'updated': say('downloads.updated'), 'recent': say('downloads.recent'),
                  'free_episodes': say('downloads.free_episodes'), 'wait_until_free': say('downloads.wait_until_free')}
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
    rows = {item['name']: item for item in card_rows({say('dashboard.status'): v['title']})}
    filled = set()

    def row(name, value, warning=False, tip=''):
        if not value:
            return
        if name == say('dashboard.continue_in'):
            value = say('dashboard.continue_in_2') + value
        if name in (say('dashboard.updates'), say('dashboard.waiting'), say('dashboard.stopped'), say('dashboard.finished'), say('dashboard.continue_in'), say('dashboard.connection')):
            name = say('dashboard.status')
        elif name in (say('dashboard.lists'), say('dashboard.series_left')):
            value = value.replace(' to check', ' series to check') if name == say('dashboard.series_left') else value
            name = say('dashboard.progress')
        elif name == say('dashboard.step') and ' | ' in value:
            value, _, details = value.partition(' | ')
            row(say('dashboard.progress'), details)
        value = ' '.join(value.split())
        item = rows[name]
        if name in filled and name in (say('dashboard.status'), say('dashboard.progress')):
            value = item['value'] + ' | ' + value
            warning = warning or item['warning']
        item.update(value=value, warning=bool(warning), tip=tip)
        filled.add(name)
    if not s.loaded:
        v['rows'] = card_rows({say('dashboard.account'): say('dashboard.checking'), say('dashboard.downloader'): say('dashboard.checking'), say('dashboard.status'): say('library.opening_2')})
        v['save_place'] = save_place(s)
        return v
    row(say('dashboard.account'), v['session_word'], v['session_tone'] == 'warning')
    row(say('dashboard.downloader'), v['worker_word'], v['worker_tone'] == 'warning')
    if running and not disconnected:
        phases = {'existing_backlog': say('dashboard.finishing_saved_downloads'), 'lists': say('dashboard.finding_your_series'),
                  'series': say('dashboard.checking_series_and_downloading'), 'archive': say('dashboard.downloading_episodes'),
                  'verify': say('dashboard.finishing_download')}
        step = '' if list_phase else step_progress(progress).rstrip('.')
        row(say('dashboard.step'), ' | '.join(part for part in (phases.get(account.get('phase'), say('dashboard.downloading')), step) if part))
        if list_phase:
            matching = (progress.get('status') == 'running' and progress.get('phase') == 'account_list'
                        and progress.get('category') == lists.get('current'))
            row(say('dashboard.lists'), say('dashboard.of_read', n=display(lists.get('checked')), n2=display(lists.get('total') or 4)) + (
                say('dashboard.pages_of_series', n=display(progress['reading_list_page_count']), active=active, n2=display(progress.get('observed_library_series_count')))
                if matching and type(progress.get('reading_list_page_count')) is int else say('dashboard.now', active=active) if active else ''))
        elif account.get('phase') == 'series':
            row(say('dashboard.series_left'), say('dashboard.to_check', n=display(status.get('scan_remaining_series_count'))))
        elif account.get('phase') == 'archive' and number(status.get('scan_remaining_series_count')):
            row(say('dashboard.progress'), say('dashboard.series_waiting', n=display(status['scan_remaining_series_count'])))
    if disconnected and running:
        row(say('dashboard.updates'), say('dashboard.delayed_reaching_the_downloader_again'), True)
    elif retry:
        row(say('dashboard.waiting'), (say('dashboard.asked_the_app_to_wait', site=site) if retry.get('reason') == 'source'
                        else say('dashboard.some_images_did_not_arrive') if retry.get('reason') == 'images'
                        else say('dashboard.the_last_step_saved_nothing_new') if retry.get('reason') == 'idle'
                        else say('dashboard.for_wi_fi_or_a_wired') if network_held
                        else say('dashboard.the_connection_to_was_interrupted', site=site))
            + ('' if network_held else f'; carries on in {retry_in}'), True)
    elif running and waiting:
        row(say('dashboard.waiting'), say('dashboard.no_new_progress_reported_for', value=duration(activity_age)), True)
    elif batch_running and not running:
        row(say('dashboard.step'), say('dashboard.pausing_after_the_image_being_saved') if progress.get('cancellation_requested')
            else say('dashboard.finishing_the_current_step'))
    if account_status == 'held' and not needs_login and not running:
        detail = {'series_scan_gap_count': status.get('series_scan_gap_count', 0)} | audit
        reason = held_reason(account.get('code'), site, missing, detail)
        row(say('dashboard.finished') if finished else say('dashboard.stopped'), reason, True)
    elif account.get('code') == 'login_required' and needs_login:
        row(say('dashboard.stopped'), say('dashboard.signed_you_out', site=site), True)
    if not running and go_wait:
        row(say('dashboard.continue_in'), wait_text(go_wait))
    if network_held and not running:
        row(say('dashboard.connection'), f'{connection_held[0].upper()}{connection_held[1:]}; saving waits for Wi-Fi or a wired connection', True)
    if account_status == 'finished':
        row(say('dashboard.finished'), say('dashboard.done'))
    # Saved, Time left, Space, Speed and Download speed are on every card, so cards side by side read alike; NONE where nothing is known.
    row(say('settings.saved'), NONE if s.status is None else
        f"{display(counts.get('archived'))} {'episode' if number(counts.get('archived')) == 1 else 'episodes'}")
    estimate = status.get('estimate') if account.get('configured') else None
    if estimate is not None:
        estimate = dict(estimate, checking_lists=list_phase)
    time = time_row(estimate) if not finished else ''
    row(say('dashboard.time_left'), time or NONE, tip=estimate_tip(estimate) if time and time != ESTIMATING else '')
    space, short = space_row({'free_bytes': estimate.get('free_bytes')} if finished and estimate else estimate)
    estimated_space = estimate and (number(estimate.get('waiting_episodes')) or number(estimate.get('unchecked_series'))
                                    or unconfirmed_downloads(estimate))
    row(say('dashboard.space'), space or NONE, short, estimate_tip(estimate) if space and estimated_space and not finished else '')
    if running and elapsed is not None:
        row(say('dashboard.running_for'), duration(elapsed) + ('' if age is None else
            say('dashboard.last_ago', value=say('dashboard.check_update') if verifying else 'saved', value2=duration(age))))
    refused = signed_in and account_status == 'held' and account.get('code') in ('source_refused', 'source_recovery_limit')
    row(say('dashboard.speed'), NONE if not signed_in else
        SPEED_NAMES[speed] + (say('dashboard.a_slower_speed_may_help') if refused and speed != 'slow' else ''), refused)
    # What arrives, apart from Speed, which paces the requests.
    measured = s.speed() if running and s.saved_at else None
    row(say('dashboard.download_speed'), NONE if measured is None else rate(measured))
    notes = []
    if account_status == 'finished' and cannot_save:
        notes.append(say('dashboard.episodes_use_a_format_the_app', n=display(cannot_save)))
    purchase = account.get('purchase_history')
    if finished and site == 'Tapas' and not isinstance(purchase, dict):
        notes.append(say('dashboard.purchase_history_not_checked'))
    if site == 'Tapas' and isinstance(purchase, dict):
        if purchase.get('status') == 'pending':
            notes.append(say('dashboard.checking_purchase_history'))
        elif purchase.get('status') == 'partial':
            reasons = {'history_view_unverified': say('dashboard.purchase_history_view_could_not_be'),
                       'history_rows_unverified': say('dashboard.purchase_history_entries_need_another_check'),
                       'history_page_repeated': say('dashboard.purchase_history_paging_repeated_a_page'),
                       'history_continuation_unverified': say('dashboard.purchase_history_paging_could_not_be'),
                       'history_association_unresolved': say('dashboard.some_purchase_history_entries_could_not'),
                       'history_checkpoint_unreadable': say('dashboard.purchase_history_progress_could_not_be'),
                       'history_reader_unavailable': say('dashboard.purchase_history_checking_is_unavailable_in')}
            notes.append(reasons.get(purchase.get('code'), say('dashboard.purchase_history_check_incomplete')))
    if notes:
        row(say('dashboard.note'), ' | '.join(notes))
    v['rows'] = list(rows.values())
    v['save_place'] = save_place(s)
    return v


def heading(s, worker, running, signing_in, batch_running, needs_login, disconnected, waiting, since_refresh):
    """The dashboard's title, its one-word state and the two lines about the account and the downloader."""
    title, label, tone = say('dashboard.loading_your_status'), say('dashboard.loading'), 'neutral'
    account = s.account or {}
    if s.loaded and s.status is not None and s.account is not None:
        if disconnected:
            title, label, tone = (say('dashboard.status_updates_unavailable'), say('dashboard.reconnecting'), 'warning') if updating(s) else (
                say('dashboard.status_unavailable'), say('dashboard.status_unavailable'), 'warning')
        elif signing_in:
            title, label, tone = say('dashboard.finish_signing_in'), say('dashboard.signing_in'), 'working'
        elif needs_login:
            title = say('dashboard.sign_in_again') if (s.site_auth or {}).get('status') == 'expired' else say('dashboard.sign_in_to_get_started')
            label, tone = say('cloud.sign_in_needed'), 'warning'
        elif running:
            phases = {'lists': (say('dashboard.finding_your_series'), say('dashboard.finding_series')), 'series': (say('dashboard.checking_your_library'), say('dashboard.checking_series')),
                      'archive': (say('downloads.saving_your_episodes'), say('dashboard.downloading')), 'verify': (say('dashboard.finishing_download'), say('dashboard.finishing')),
                      'existing_backlog': (say('dashboard.finishing_saved_downloads'), say('dashboard.downloading'))}
            (title, label), tone = phases.get(account.get('phase'), (say('notification.title'), say('downloads.working'))), 'working'
            if waiting:
                title, label, tone = say('dashboard.waiting_for_a_progress_update'), say('dashboard.waiting'), 'warning'
        elif account.get('status') == 'held' and account.get('code') == 'archive_incomplete':
            title, label, tone = say('dashboard.completion_needs_attention'), say('dashboard.needs_attention'), 'warning'
        elif account.get('status') in ('held', 'stopped'):
            title, label, tone = say('dashboard.your_download_is_paused'), say('dashboard.paused'), 'warning'
        elif batch_running:
            title, label, tone = say('dashboard.finishing_the_current_step'), say('downloads.working'), 'working'
        elif worker == 'starting':
            title, label, tone = say('dashboard.starting_the_downloader'), say('dashboard.starting'), 'working'
        elif worker != 'ready':
            title, label, tone = say('dashboard.the_downloader_is_offline'), say('dashboard.offline'), 'warning'
        elif account.get('status') == 'finished':
            title, label, tone = say('dashboard.this_run_is_finished'), say('dashboard.finished'), 'success'
        else:
            title, label, tone = say('downloads.ready_to_save'), say('downloads.ready'), 'success'
    site = s.site()
    session = (say('dashboard.checking') if s.status is None else say('dashboard.signing_in') if signing_in else say('cloud.sign_in_needed') if needs_login
               else say('dashboard.connected') if (s.site_auth or {}).get('status') in ('connected', 'verified') else say('dashboard.session_saved'))
    # Before the first sign-in the downloader has nothing to start with: that is not a fault to warn about.
    not_yet = needs_login and worker not in WORKING
    downloader = (say('dashboard.checking') if s.status is None else say('downloads.status_unknown') if disconnected else say('downloads.working') if worker == 'busy'
                  else say('downloads.ready') if worker == 'ready' else say('dashboard.starting') if worker == 'starting'
                  else say('dashboard.starts_after_sign_in') if not_yet else say('dashboard.offline'))
    return {'title': title, 'state': label, 'tone': tone, 'session_word': session, 'worker_word': downloader,
            'session': f'{site} · {session}', 'session_tone': 'warning' if needs_login and s.status is not None else 'neutral',
            'worker': f'Downloader · {downloader}',
            'worker_tone': 'warning' if disconnected or (s.status is not None and not not_yet and worker not in WORKING) else 'neutral',
            'updated': (say('dashboard.status_unavailable_start_to_refresh') if disconnected and not updating(s) else
                        say('dashboard.updates_delayed_trying_to_reconnect') if disconnected else say('dashboard.checking_current_status')
                        if since_refresh is None else say('dashboard.updated_just_now') if since_refresh < 2 else say('dashboard.updated_s_ago', since_refresh=since_refresh))}
