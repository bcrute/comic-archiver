'use strict';
// The launch address's secret has done its work once the page is here; it is not kept in the address bar or history.
if (location.search.startsWith('?launch=')) history.replaceState(null, '', location.pathname);
const $ = id => document.getElementById(id);
const state = { context: null, account: null, siteAuth: null, accountMessage: null, workspace: null, status: null, cloud: null, network: null, auth: null, storage: null, series: [], task: null, taskKind: null, taskInfo: null, taskLabel: null, connectionCheck: null, busy: false, polling: false, seriesKey: null, auditUrl: null, previewUrl: null, preview: null, bucketDrafts: {}, b2StepsOpen: false, locations: [] };
const errors = { website_login_required: 'Sign in to this website to open your library.',
  local_session_required: 'This page’s session expired. Close and reopen the app, or open the full dashboard address it showed again.',
  same_origin_required: 'Open the local dashboard directly at its displayed address.',
  loopback_required: 'Use the displayed 127.0.0.1 address on this computer.',
  origin_required: 'Open this page at the configured website address.',
  csrf_required: 'This page’s session changed. Close and reopen the app, or reload this page in a browser.',
  library_busy: 'An import is active. Try again after this batch finishes.',
  service_unavailable: 'The service is temporarily unavailable. Try again shortly.',
  request_unavailable: 'This action is unavailable. Check the connection and selected library.',
  get login_required() { return `Your ${siteName()} session needs a fresh login.`; },
  get source_refused() { return `${siteName()} refused a request. Check ${siteName()} in your usual browser before resuming. Completed archives and cached images are retained.`; },
  task_failed: 'The task stopped. Completed archives and cached images are retained.' };
const cloudErrors = {
  b2_key_permissions: 'Create a Read and Write key restricted to your library bucket. Leave the file prefix and duration blank, then copy keyID and applicationKey again.',
  b2_bucket_mismatch: 'This key is for a different bucket than the bucket name in step 2. Type the name exactly as Backblaze’s Buckets page shows it, or create the key again for that bucket.',
  b2_private_bucket: 'Choose Private for your B2 library bucket in Backblaze, then connect again.',
  cloud_login_required: 'Cloud sign-in needs repair. Click Sign in again for this provider, then retry.',
  cloud_space: 'The provider reported insufficient cloud storage. Free space through your provider or choose another destination, then retry.',
  cloud_cap: 'Backblaze stopped the upload: your account has used its free 10 GB, or reached a spending limit set on its Caps & Alerts page. Add a card on Backblaze’s Billing page or raise the limit, then retry.',
  cloud_rate_limited: 'The provider limited requests. Wait before retrying.',
  cloud_connection: 'The cloud transfer was unavailable or timed out. Check your internet connection, then retry.',
  cloud_permission: 'The provider refused write access. Check the selected account and connection permissions, then retry.',
  cloud_verification: 'The cloud copy could not be verified. Retry the upload before removing any files.',
  cloud_transfer_limit: 'A cloud transfer limit was reached. Check the connection settings before retrying.',
  cloud_network_held: 'The network policy held the cloud transfer. Reconnect to an allowed network, then retry.',
  cloud_unknown: 'The cloud transfer failed for an unrecognized reason. Check sign-in, connection and cloud space, then retry.'
};
for (const [code, message] of Object.entries(cloudErrors)) errors[code] = message+' Local episodes are retained.';
const number = value => Number.isInteger(value) && value >= 0 ? value : 0;
const display = value => number(value).toLocaleString();
const duration = value => !(typeof value === 'number' && Number.isFinite(value) && value >= 0) ? null
  : value >= 3600 ? `${Math.floor(value/3600)}h ${Math.floor(value%3600/60)}m` : `${Math.floor(value/60)}m ${Math.floor(value%60)}s`;
// A length of time as a person would say it: the estimate is rough, and so is this.
const span = seconds => { const hours = seconds/3600, days = hours/24;
  return hours < 0.85 ? `${Math.max(5, Math.round(seconds/300)*5)} minutes` : hours < 1.5 ? 'an hour' : hours < 36 ? `${Math.round(hours)} hours`
    : days < 14 ? `${Math.round(days)} days` : days < 63 ? `${Math.round(days/7)} weeks` : `${Math.round(days/30)} months`; };
function timeLeft(estimate) {
  const waiting = number(estimate?.waiting_episodes), unchecked = number(estimate?.unchecked_series);
  if (!estimate || !(waiting || unchecked)) return '';
  if (!Number.isInteger(estimate.seconds)) return 'Working out how long this will take. An estimate appears after about five minutes of downloading.';
  const left = `${display(waiting)} ${waiting === 1 ? 'episode is' : 'episodes are'} waiting`+(unchecked ? ` and ${display(unchecked)} series ${unchecked === 1 ? 'has' : 'have'} not been checked yet, so this is a rough guess.` : '.');
  return `About ${span(estimate.seconds)} of downloading left at the pace of the last five minutes. ${left} It only continues while this app is open.`;
}
// An amount of disk space as a person would say it; like the time, it is a rough figure.
const room = value => { const gb = value/1e9;
  return gb >= 1000 ? `${(gb/1000).toFixed(1)} TB` : gb >= 10 ? `${Math.round(gb)} GB` : gb >= 1 ? `${gb.toFixed(1)} GB` : `${Math.max(1, Math.round(value/1e6))} MB`; };
function spaceNeeded(estimate) {
  const unchecked = number(estimate?.unchecked_series);
  if (!estimate || !(number(estimate.waiting_episodes) || unchecked)) return '';
  if (!Number.isInteger(estimate.left_bytes)) return '';
  const saved = number(estimate.saved_bytes), left = estimate.left_bytes, free = estimate.free_bytes;
  const parts = saved ? `: ${room(saved)} is saved and about ${room(left)} is still to come` : '';
  const rough = unchecked ? ' This is a rough guess until every series has been checked.' : '';
  const disk = !Number.isInteger(free) ? '' : free < left ? ` This disk has only ${room(free)} free, which is not enough for the rest.` : ` This disk has ${room(free)} free.`;
  return `Your whole library should take about ${room(saved+left)} of space${parts}.${rough}${disk}`;
}
// What Backblaze would charge for this library, at its price as written on the setup page. Its own pages count by the
// day (its price is for 30 days); people speak of a month.
const B2_FREE_BYTES = 10e9, B2_DOLLARS_PER_GB_MONTH = 6.95/1000;
const dollarsText = dollars => dollars < 0.005 ? 'less than 1 cent' : 'about '+(dollars < 0.995 ? `${Math.round(dollars*100)} ${Math.round(dollars*100) === 1 ? 'cent' : 'cents'}` : `$${dollars.toFixed(2)}`);
function b2Cost(estimate) {
  const total = number(estimate?.saved_bytes) + number(estimate?.left_bytes);
  if (!total) return '';
  const rough = number(estimate.unchecked_series) ? ' This is a rough guess until every series has been checked.' : '';
  if (total <= B2_FREE_BYTES) return `Your library should take about ${room(total)}, which fits in Backblaze’s free 10 GB.${rough}`;
  const month = (total - B2_FREE_BYTES)/1e9 * B2_DOLLARS_PER_GB_MONTH;
  return `Your library should take about ${room(total)}. Once all of it is stored, Backblaze would charge ${dollarsText(month/30)} a day, ${dollarsText(month)} a month.${rough}`;
}
// The same, short, for the heading of the cost details; before the library's size is known, the price of 100 GB.
function b2CostShort(estimate) {
  const total = number(estimate?.saved_bytes) + number(estimate?.left_bytes);
  if (!total) return 'free up to 10 GB, then about 2 cents a day, about 70 cents a month, for each 100 GB more';
  if (total <= B2_FREE_BYTES) return 'free, your library fits in the free 10 GB';
  const month = (total - B2_FREE_BYTES)/1e9 * B2_DOLLARS_PER_GB_MONTH;
  return `${dollarsText(month/30)} a day, ${dollarsText(month)} a month`;
}
function renderCost() {
  const estimate = state.status?.estimate, text = b2Cost(estimate);
  $('b2-cost-short').textContent = b2CostShort(estimate);
  $('b2-cost-estimate').textContent = text; $('b2-cost-estimate').hidden = !text;
}
function notice(message, error = false) {
  $('notice').hidden = simpleMode(); $('notice').textContent = message; $('notice').classList.toggle('error', error);
  // The simple page hides the notice bar, so a failure is shown on the card and in Settings instead.
  // It stays for half a minute of good updates, long enough to read.
  if (error && simpleMode()) { state.accountMessage = {message, error:true, until:Date.now()+30000}; renderAccount(); }
}
function renderConnectionCheck() {
  $('connection-check-status').textContent = state.connectionCheck?.message || '';
  $('connection-check-status').classList.toggle('error', state.connectionCheck?.error === true);
  $('check').textContent = state.connectionCheck?.pending ? 'Checking Tapas connection…' : 'Check Tapas connection';
  $('check').setAttribute('aria-busy', String(state.connectionCheck?.pending === true));
}
async function api(path, body) {
  const options = { credentials: 'same-origin', cache: 'no-store' };
  if (body !== undefined) {
    state.context = await api('/api/v1/context');
    options.method = 'POST';
    options.headers = { 'Content-Type': 'application/json', 'X-CSRF-Token': state.context.csrf_token };
    options.body = JSON.stringify(body);
  }
  let response;
  try { response = await fetch(path, options); } catch { throw new Error('Connection lost. Completed archives are retained.'); }
  let result;
  try { result = await response.json(); } catch { throw new Error('The website returned an unexpected response. Reload or try again.'); }
  if (!response.ok) throw new Error(errors[result.code] || 'The action could not be completed.');
  return result;
}
const simpleMode = () => state.context?.simple_mode !== false;
const desktopOnly = () => state.context?.desktop_only !== false;
// The desktop app names its folder; its own window opens it or asks for another.
const ownWindow = () => typeof state.context?.library_folder === 'string';
// Cloud saving runs on the person's own computer (in the app, or in the local browser version) where its helper is offered.
const cloudOffered = () => state.context?.cloud_offered !== false;
const cloudAvailable = () => cloudOffered() && (!desktopOnly() || state.context?.interface_mode === 'local');
const cloudLabel = provider => ({b2:'Backblaze B2', google:'Google Drive', dropbox:'Dropbox', proton:'Proton Drive'}[provider] || 'Cloud storage');
// Firefox restores select values on reload; start with the visible provider.
// An active connection attempt is restored separately by cloudAuth().
$('cloud-provider').value = 'b2';
function cloudAction(work) { return action(async () => { try { await work(); } catch (error) { cloudNotice(error.message, true); } }); }
function cloudNotice(message, error = false) { $('cloud-action-status').textContent = message; $('cloud-action-status').classList.toggle('error', error); notice(message, error); }
const base = () => '/api/v1/workspaces/' + state.workspace;
function canImport() { return Boolean(state.status?.session_configured && state.status?.worker?.reported_state === 'ready' && state.status?.progress?.status !== 'running' && state.account?.status !== 'running' && !state.busy && !state.task && state.auth?.status !== 'running'); }
function validSeriesLink() { return Boolean($('series-url').value.trim() && $('series-url').validity.valid); }
function reviewKey() {
  return JSON.stringify([state.workspace, $('series-url').value.trim(), $('series-amount').value,
    $('image-profile').value, cloudAvailable() ? $('allow-mobile-data').checked : null,
    cloudAvailable() ? (state.cloud?.targets || []).filter(t => t.enabled).map(t => t.provider).sort() : []]);
}
function reviewed() { return Boolean(state.preview && state.previewUrl?.key === reviewKey()); }
function renderGuidance() {
  const local = state.context?.interface_mode === 'local';
  let message = 'Paste a series link in Step 1 to get started.';
  if (!state.workspace) message = 'Open a library to get started.';
  else if (state.auth?.status === 'running') message = 'Finish cloud sign-in, then review your download.';
  else if (state.taskKind === 'preview') message = 'Checking your access and available space. No episodes are being downloaded.';
  else if (state.task) message = state.taskKind === 'import' ? 'Your download is queued or running. Follow Download progress below.' : 'Another action is in progress. You can review this download when it finishes.';
  else if (state.status?.worker?.reported_state === 'busy' || state.status?.progress?.status === 'running') message = 'A batch is already running. Follow Download progress below; it will continue if you reload this page.';
  else if (!state.status?.session_configured) message = local ? 'Connect your Tapas account using the setup instructions above.' : 'This library needs a Tapas account connection before downloading.';
  else if (!['ready', 'busy'].includes(state.status?.worker?.reported_state)) message = local ? 'Restart the downloader using the setup instructions above.' : 'The downloader is offline. Your saved files are retained.';
  else if (validSeriesLink()) {
    if (!local) message = 'Ready to download. Current access and storage are checked during the download.';
    else if (!reviewed()) message = desktopOnly() ? 'Choose image quality, then click Review download. Nothing downloads until you click Download.' : 'Choose your storage options, then click Review download. Nothing downloads until you click Download.';
    else if (!number(state.preview.planned_episode_count)) message = 'No new accessible episodes were found for this selection. Check your saved library or choose another series.';
    else if (!state.preview.fits_estimate) message = desktopOnly() ? 'Space is limited. Try one episode, choose smaller images or free space on this computer. Downloads still check actual free space.' : 'Space is limited. Try one episode, choose cloud storage or use a device with more space. Downloads still check actual free space.';
    else if (state.network?.wifi_only && !state.network.allowed) message = 'The download is reviewed. Mobile data is off. Transfers wait for Wi-Fi or an unmetered wired connection.';
    else message = 'Ready. Click Download to save the reviewed episodes.';
  }
  $('next-step').textContent = message;
  $('preview-series').hidden = !local;
  $('preview-series').classList.toggle('secondary', reviewed());
  $('import-link').textContent = $('series-amount').value === 'one' ? 'Download one episode' : 'Download available episodes';
  const enabled = !cloudAvailable() ? [] : (state.cloud?.targets || []).filter(t => t.enabled);
  $('save-destination').textContent = enabled.length ? `Completed episodes go to ${enabled.map(t => cloudLabel(t.provider)).join(' and ')}. Verified uploads free local episode space.` : state.storage?.storage_location === 'server' ? 'Completed episodes stay in this library on the server.' : 'Completed episodes stay on this computer.';
}
function buttons() {
  $('create').disabled = state.busy || !state.context || state.context.workspace_ids.length >= state.context.max_workspaces;
  $('workspace').disabled = state.busy || !state.context?.workspace_ids.length;
  for (const id of ['check','watch','discover','archive','save-schedule','save-priority']) $(id).disabled = !canImport();
  $('import-link').disabled = !canImport() || !validSeriesLink() || (state.context?.interface_mode === 'local' && (!reviewed() || !number(state.preview.planned_episode_count)));
  $('downloads').disabled = state.busy || !state.workspace;
  $('audit').disabled = state.busy || !state.workspace;
  for (const id of ['save-storage','refresh-storage']) $(id).disabled = state.busy || !state.workspace;
  $('image-profile').disabled = state.busy || !state.workspace;
  $('allow-mobile-data').disabled = !cloudAvailable() || state.busy || !state.network?.supported_on_this_interface;
  $('purge-cache').disabled = !canImport();
  const signingIn = state.auth?.status === 'running';
  $('connect-cloud').disabled = state.busy || Boolean(state.task) || signingIn || state.account?.status === 'running' || state.status?.worker?.reported_state === 'busy' || !state.cloud?.supported_on_this_interface;
  $('cloud-provider').disabled = !cloudAvailable() || state.busy || signingIn;
  $('submit-cloud-login').disabled = !cloudAvailable() || state.busy || signingIn;
  $('connect-b2').disabled = $('connect-cloud').disabled;
  for (const id of ['b2-key-id', 'b2-application-key']) $(id).disabled = $('connect-cloud').disabled;
  $('cancel-cloud-signin').hidden = !signingIn;
  $('cancel-cloud-signin').disabled = state.busy;
  const cloudTarget = state.cloud?.targets?.find(t => t.provider === $('cloud-provider').value);
  const choiceLocked = state.busy || signingIn || Boolean(state.task) || state.account?.status === 'running' || !state.cloud?.supported_on_this_interface;
  for (const input of $('storage-options').querySelectorAll('input')) input.disabled = choiceLocked || input.dataset.ready !== 'true';
  $('retry-cloud').disabled = !canImport() || !state.cloud?.supported_on_this_interface || !state.cloud?.targets?.some(t => t.enabled);
  $('preview-series').disabled = !canImport() || !validSeriesLink() || state.context?.interface_mode !== 'local';
  $('test-cloud').disabled = !canImport() || !state.cloud?.supported_on_this_interface || !cloudTarget?.connected || state.cloud?.pending_test_cleanup?.some(t => t.provider === $('cloud-provider').value);
  for (const button of $('cloud-cleanup').querySelectorAll('button')) button.disabled = !canImport();
  for (const id of ['save-network','refresh-network']) $(id).disabled = state.busy || !state.workspace || !state.network?.supported_on_this_interface;
  $('stop').disabled = state.busy || state.status?.last_run?.status !== 'running' || !state.status?.progress?.cancellation_supported || state.status?.progress?.cancellation_requested;
  $('stop-queued').disabled = state.busy || state.taskKind !== 'import' || state.taskInfo?.status !== 'queued' || !state.taskInfo?.cancellation_supported || state.taskInfo?.cancellation_requested;
  renderGuidance();
  renderActivity();
  renderConnectionCheck();
  renderAccount();
}
function renderSeries() {
    if (simpleMode()) return;
    const query = $('search').value.toLocaleLowerCase();
    const unlocked = $('unlocked-only').checked;
    const rows = state.series.filter(row => typeof row.title === 'string' && row.title.toLocaleLowerCase().includes(query) && (!unlocked || number(row.observed_unlocked_episode_count)>0));
    const key = JSON.stringify([rows, query, unlocked, canImport()]);
    if (key === state.seriesKey) return;
    state.seriesKey = key;
  $('series').replaceChildren();
  $('series-count').textContent = `${display(rows.length)} of ${display(state.series.length)} observed series. Missing episode observations still need a scan.`;
  for (const row of rows) {
    const tr = document.createElement('tr');
    const counts = row.episodes_by_status || {};
    for (const text of [row.title, display(row.observed_free_episode_count), display(row.observed_unlocked_episode_count), display(counts.archived), display(number(counts.queued) + number(counts.failed)), display(counts.unsupported)]) {
      const td = document.createElement('td'); td.textContent = text; tr.append(td);
    }
    const td = document.createElement('td'); const button = document.createElement('button');
    button.type = 'button'; button.textContent = 'Check & archive'; button.disabled = !canImport();
    button.addEventListener('click', () => action(() => queue('archive_watched_series', { id: row.source_series_id })));
    td.append(button); tr.append(td); $('series').append(tr);
  }
}
function renderActivity() {
  const queued = state.taskKind === 'import' && state.taskInfo?.status !== 'running';
  const running = state.status?.progress?.status === 'running';
  const busy = state.status?.worker?.reported_state === 'busy';
  const message = queued ? `${state.taskLabel || 'Download'} queued. Waiting for the downloader; progress updates every five seconds.`
    : running ? $('progress').textContent
    : busy ? 'The downloader is working. Waiting for its next progress update.'
    : state.taskLabel || 'No account scan is running.';
  $('account-progress').textContent = message;
  $('watch').textContent = queued && state.taskLabel === 'Known series check' ? 'Check queued…'
    : running && state.status.progress.phase === 'scan' ? 'Checking known series…' : 'Check known series';
  $('account-progress').setAttribute('aria-busy', String(queued || running || busy));
}
function renderStatus() {
  const s = state.status;
  if (!s) return;
  const counts = s.episodes_by_status || {};
  $('coverage').textContent = s.inventory_complete === true ? 'The recorded inventory is complete.' : `Account inventory is incomplete. ${display(s.series_without_episode_observations_count)} observed series have no saved episode observations; ${display(s.series_scan_gap_count)} series have unresolved scan gaps; ${display(counts.unavailable)} episodes are unavailable or unverified.`;
  const freshness = s.scan_freshness;
  $('scan-freshness').textContent = freshness ? `${display(freshness.checked_within_24h)} of ${display(s.series_count)} series successfully checked in the last 24 hours. ${display(freshness.older_than_24h)} have older checks; ${display(freshness.never_checked)} have no recorded complete check.${number(freshness.invalid_timestamp_count) ? ` ${display(freshness.invalid_timestamp_count)} check timestamps need repair.` : ''} This measures episode-list freshness; downloads check access again.` : 'Series check freshness is unavailable.';
  const worker = s.worker?.reported_state;
  const local = state.context.interface_mode === 'local';
  $('local-login').hidden = !local || s.session_configured;
  $('local-worker').hidden = !local || !s.session_configured || ['ready','busy'].includes(worker);
  showPage();
  $('local-setup').hidden = simpleMode() || !local || (s.session_configured && ['ready','busy'].includes(worker));
  $('connection').textContent = !s.session_configured ? 'Your Tapas account needs connecting.' : !['ready','busy'].includes(worker) ? 'The downloader is offline. Your saved files remain available.' : 'Your Tapas session is saved and the downloader is online. Each episode is checked before downloading.';
  const p = s.progress;
  const phases = { scan: 'Checking known series', account_list: 'Refreshing account lists', archive: 'Archiving episodes', metadata: 'Updating series details and cover', authentication: 'Checking account authentication', history: 'Reading account history', history_series: 'Associating observed history links' };
  const transfers = number(p?.downloaded_chapters) ? `${display(number(p.downloaded_pages) - number(p.downloaded_chapters))} comic images · ${display(p.downloaded_chapters)} novel chapters` : `${display(p?.downloaded_pages)} images downloaded`;
  $('progress').textContent = p?.cancellation_requested ? 'Stop requested. The importer will pause at its next safe checkpoint; completed archives and cached images are retained.' : p?.status === 'running' ? `${phases[p.phase] || 'Import in progress'} · ${display(p.completed)} saved this batch · ${transfers}` : s.last_run?.status === 'cancelled' ? 'Batch stopped. Completed archives and cached images are retained; resume with a new batch.' : 'Completed downloads remain available between batches.';
  if (p?.status === 'running' && !p.cancellation_requested && p.phase === 'scan' && Number.isInteger(p.series_check_target_count))
    $('progress').textContent = `Checking known series · ${display(p.series_checks_completed)} of ${display(p.series_check_target_count)} checks completed this batch · ${display(p.successful_series_checks)} complete episode lists · ${display(p.held_series_checks)} held checks`;
  if (s.source_recovery)
    $('progress').textContent = s.source_recovery.automatic ? `Tapas reader cooldown · retry ${display(s.source_recovery.attempts)} of ${display(s.source_recovery.limit)} at ${s.source_recovery.retry_at}. The importer will check account and episode access again, then continue automatically.` : 'Reader cooldown saved. The import controller needs resuming to continue.';
  buttons(); renderSeries();
}
async function refresh() {
  if (!state.workspace) return;
  const workspace = state.workspace;
  const [status, series] = await Promise.all(simpleMode() ? [api(base()), Promise.resolve({series:[]})] : [api(base()), api(base()+'/series')]);
  if (workspace !== state.workspace) return;
  state.status = status; state.series = Array.isArray(series.series) ? series.series : [];
  renderStatus(); renderCost();
}
async function daily() {
  const workspace = state.workspace;
  const s = await api('/api/v1/workspaces/'+workspace+'/daily');
  if (workspace !== state.workspace) return;
  $('enabled').checked = s.enabled === true;
  if (s.configured) { $('at').value = s.at; $('zone').value = s.timezone; }
  $('daily-status').textContent = s.pause_reason ? 'Daily checks paused. Review the last batch and Tapas connection, then save the schedule again.' : s.enabled ? `Daily checks enabled. Next due: ${s.next_due}. The importer must remain online.` : 'Daily checks are disabled.';
}
async function select(workspace) {
  if (state.auditUrl) URL.revokeObjectURL(state.auditUrl);
  state.auditUrl = null; $('audit-download').hidden = true; $('audit-download').removeAttribute('href'); $('audit-status').textContent = '';
  state.workspace = workspace; state.account = null; state.siteAuth = null; state.accountMessage = null; state.task = null; state.taskKind = null; state.taskInfo = null; state.taskLabel = null; state.status = null; state.series = []; state.storage = null; state.cloud = null; state.network = null; state.auth = null; state.preview = null;
  state.accountReceivedAt = 0; state.lastAccountRefreshAt = 0; state.accountOffline = false;
  state.connectionCheck = null; renderConnectionCheck();
  $('batch-timing').textContent = '';
  state.previewUrl = null;
  $('series-storage').textContent = "We'll check which episodes you can save and estimate the space they need.";
  $('library-locations').replaceChildren(); $('metrics').replaceChildren(); $('search').value = '';
  await Promise.all(simpleMode() ? [refresh(), account(), siteAuth(), cloud(), network(), cloudAuth(), librarySummary()] : [refresh(), daily(), storage(), cloud(), network(), cloudAuth(), librarySummary()]); notice(state.status?.worker?.reported_state === 'busy' || state.status?.progress?.status === 'running' ? 'Your batch is still running. Progress appears below and beside the account scan buttons.' : 'Library loaded. Choose a series to get started.');
  $('priority').value = state.status?.queue_priority || 'expiring_first';
}
// The open site's name, for the words the page shows; pages without a list of sites are Tapas's.
function siteName() {
  return (state.context?.sites || []).find(site => site.current === true)?.name || 'Tapas';
}

function showSites(sites) {
  // Only the desktop app lists sites. Choosing one asks the window to open that site's own library.
  const list = Array.isArray(sites) ? sites : [];
  $('site-choice').hidden = !list.length;
  $('site').replaceChildren(...list.map(site => {
    const option = document.createElement('option');
    option.value = site.key;
    option.textContent = site.available ? site.name : site.name + ' — coming later';
    option.disabled = !site.available;
    option.selected = site.current === true;
    return option;
  }));
}
$('site').addEventListener('change', () => accountAction(async () => {
  // The window opens the other site's library behind this page; the page then reads it, without reloading.
  const chosen = $('site').selectedOptions[0]?.textContent || 'the site';
  if (!confirm(`Switch to ${chosen}? Your ${siteName()} episodes stay saved, and you can switch back any time.`)) {
    showSites(state.context?.sites); return;
  }
  await api('/api/v1/site', {site:$('site').value});
  state.accountMessage = {message:`Switching to ${chosen}…`}; renderAccount();
  while ((await api('/api/v1/context')).site_switching === true) await new Promise(done => setTimeout(done, 300));
  state.accountMessage = null;
  await load();
}));
document.addEventListener('comic-archive-theme', async event => {
  // Only the desktop app keeps the choice; in a browser the page's own storage does.
  if (typeof state.context?.theme !== 'string') return;
  try { await api('/api/v1/theme', {theme:event.detail}); } catch { /* The page already shows the choice; it lasts until the app closes. */ }
});
async function load(preferred = state.workspace) {
  state.context = await api('/api/v1/context');
  showSites(state.context.sites);
  // The window's folder is on the Storage tab of Settings, with the ways to open and change it.
  for (const id of ['save-location', 'open-folder', 'change-folder']) $(id).hidden = !ownWindow();
  if (!simpleMode()) $('notice').after($('site-choice'));
  $('save-folder').textContent = state.context.library_folder || '';
  renderSaveSummary();
  for (const id of ['download-options','advanced','advanced-nav','batch-progress']) $(id).hidden = simpleMode();
  $('dashboard-heading').hidden = !simpleMode();
  $('dashboard-grid').classList.toggle('dashboard-grid', simpleMode());
  $('account-download').hidden = !simpleMode();
  $('cloud-entry').hidden = true;  // Until the cloud connections are read (renderStorageChoice).
  $('cloud-entry-steps').hidden = simpleMode() || !cloudAvailable();
  $('library-locations').hidden = simpleMode() && !cloudAvailable();
  $('notice').hidden = simpleMode();
  document.body.dataset.desktopOnly = String(desktopOnly());
  // The simple dashboard has its own mobile data choice beside Go (see renderAccount).
  for (const id of ['mobile-options','network-options']) $(id).hidden = !cloudAvailable() || simpleMode();
  showPage();
  $('local-setup').hidden = simpleMode() || state.context.interface_mode !== 'local';
  $('speed').value = state.context.interface_mode === 'local' ? 'fast' : 'normal';
  $('create').hidden = state.context.interface_mode === 'local';
  $('library-picker').hidden = state.context.interface_mode === 'local';
  $('workspace').replaceChildren();
  state.context.workspace_ids.forEach((id, index) => { const option = document.createElement('option'); option.value = id; option.textContent = `Library ${index + 1}`; $('workspace').append(option); });
  const limits = state.context.limits;
  $('budget').textContent = `Each batch can save up to ${display(limits.limit)} episodes, ${display(limits.max_pages)} new images and ${Math.round(number(limits.max_bytes)/1048576)} MiB, for up to ${Math.round(number(limits.max_seconds)/60)} minutes.`;
  buttons();
  if (state.context.workspace_ids.length) {
    const selected = state.context.workspace_ids.includes(preferred) ? preferred : state.context.workspace_ids[0];
    $('workspace').value = selected; await select(selected);
  }
  else notice('Create a library to begin. Hosted Tapas account connection setup is not yet available.');
}
async function queue(operation, options = {}) {
  await storage(false);
  if (['archive_discovered', 'archive_watched_series', 'archive_series_url'].includes(operation))
    options = {...options, speed: $('speed').value};
  const value = await api(base()+'/jobs', { operation, options }); state.task = value.task_id; state.taskKind = 'import'; state.taskInfo = {status:'queued'};
  state.taskLabel = operation === 'sync' ? (options.refresh_lists === false ? 'Known series check' : 'Account list scan') : 'Download';
  state.preview = null;
  $('series-storage').textContent = 'Review access and space again before starting another download.';
  notice('Import queued. Current account access will be checked before downloading.');
  $('progress').textContent = `${state.taskLabel} queued. Waiting for the downloader.`; buttons(); renderSeries();
}
const bytes = value => {
  const n = number(value);
  for (const [unit, scale] of [['GB', 1e9], ['MB', 1e6], ['KB', 1e3]])
    if (n >= scale) return `${(n/scale).toFixed(2)} ${unit}`;
  return `${n} B`;
};
async function storage(selectProfile = true) {
  const workspace = state.workspace;
  const value = await api(base()+'/storage');
  if (workspace !== state.workspace) return value;
  state.storage = value;
  if (selectProfile) $('image-profile').value = value.image_profile;
  const location = value.storage_location === 'this_device' ? 'this device' : 'the library server';
  $('storage-space').textContent = `${bytes(value.free_bytes)} free on ${location}. ${bytes(value.saved_archive_bytes)} of saved archives.`;
  $('storage-estimate').textContent = `${display(value.known_pending_episode_count)} known queued episodes: roughly ${bytes(value.estimated_additional_archive_bytes)} ${!desktopOnly() && value.storage_destination === 'cloud' ? 'to cloud storage; this device needs temporary episode space and a 1 GB reserve.' : 'more, plus temporary space and a 1 GB reserve.'} ${value.fits_estimate ? 'The current estimate fits.' : desktopOnly() ? 'Space is limited; choose smaller images or free space on this computer.' : 'Space is limited; connect cloud storage, choose smaller images or use a PC.'}`;
  const size = n => n >= 1e6 ? `${(n/1e6).toFixed(2)} MB` : `${(n/1e3).toFixed(1)} KB`;
  $('storage-per-episode').textContent = ['comic','novel'].map(kind => {
    const stats = value.episode_size_statistics?.[kind];
    if (!stats) return '';
    return stats.sample_count ? `${kind === 'comic' ? 'Comics' : 'Novels'}: average ${size(stats.mean_bytes)} per episode from ${display(stats.sample_count)} saved files; middle 80% ${size(stats.p10_bytes)}–${size(stats.p90_bytes)}.` : `${kind === 'comic' ? 'Comics' : 'Novels'}: rough reference ${size(stats.mean_bytes)} per episode; no saved sample yet.`;
  }).filter(Boolean).join(' ');
  $('storage-advice').textContent = `This estimate covers known episodes, not unseen purchases. Smaller-image savings depend on the artwork and are not guaranteed.${desktopOnly() ? ' If the collection will not fit, choose smaller images or free space on this computer.' : value.storage_location === 'server' ? ' Server free space does not measure your phone. Check the receiving device before transferring files.' : ' If the collection will not fit, connect cloud storage, choose smaller images, or use a PC/external drive.'}`;
  buttons();
  return value;
}
async function cloud() {
  if (!cloudAvailable()) return;
  const workspace = state.workspace;
  const value = await api(base()+'/cloud');
  if (workspace !== state.workspace) return;
  state.cloud = value; renderCloud(); buttons();
}
async function network(selectChoice = true) {
  // Saving on this computer waits for an unmetered connection too, so the app asks with or without cloud storage.
  if (!cloudAvailable() && state.context?.interface_mode !== 'local') return;
  if (!state.workspace) return;
  const workspace = state.workspace;
  const value = await api(base()+'/network');
  if (workspace !== state.workspace) return;
  state.network = value; state.networkCheckedAt = Date.now();
  if (selectChoice) $('allow-mobile-data').checked = value.wifi_only === false;
  $('network-status').textContent = !value.supported_on_this_interface ? 'Network choices are available in the local archiver on your device.' : !value.wifi_only ? 'All connections allowed, including mobile data.' : value.allowed ? 'Mobile data is off. This Wi-Fi or wired connection is allowed.' : 'Mobile data is off. Waiting for Wi-Fi or an unmetered wired connection.';
  buttons();
}
$('save-network').addEventListener('click', () => action(async () => {
  await api(base()+'/network', {wifi_only:!$('allow-mobile-data').checked});
  await network(); notice('Network choice saved. Held work keeps its recovery data; retry after reconnecting.');
}));
$('refresh-network').addEventListener('click', () => action(network));
function renderCloud() {
  const value = state.cloud;
  $('cloud-cleanup').replaceChildren();
  $('cloud-repair').textContent = '';
  renderStorageChoice(); renderStorage();
  if (!value?.supported_on_this_interface) {
    $('cloud-status').textContent = 'Cloud uploads run in the archiver on your device. This hosted dashboard cannot connect your device to cloud storage.';
    $('cloud-setup').textContent = 'Open the local archiver to sign in and upload directly from your device.';
    return;
  }
  const target = value.targets?.find(t => t.provider === $('cloud-provider').value);
  $('cloud-status').textContent = target?.tested ? `${target.enabled ? 'Automatic uploads enabled' : 'Uploads disabled'}. ${display(target.verified)} verified upload receipts; ${display(target.pending)} pending; ${display(target.failed)} need retry. Cloud availability has not been rechecked.` : target?.connected ? 'Connection configured. Complete the small upload-and-delete test before enabling uploads.' : 'No connection configured for this provider.';
  const provider = $('cloud-provider').value;
  const b2 = provider === 'b2';
  for (const node of document.querySelectorAll('.b2-only')) node.hidden = !b2;
  $('cloud-connect-title').textContent = b2 ? 'Paste the key and connect.' : 'Connect your account.';
  renderCost();
  $('connect-cloud').hidden = b2;
  $('cloud-setup').hidden = b2;
  // What the person typed wins until a connection saves it; then the saved name; first, a suggestion.
  const name = state.bucketDrafts[state.workspace] ?? b2Target()?.bucket ?? 'comic-library-' + state.workspace.slice(0, 12);
  if ($('b2-bucket-name').value !== name) $('b2-bucket-name').value = name;
  $('b2-key-bucket').textContent = name;
  renderB2State();
  // One Backblaze setup serves every site, unless this site keeps its own.
  $('b2-sharing').hidden = !b2 || !['shared', 'separate'].includes(value.storage);
  $('b2-shared').checked = value.storage === 'shared';
  $('b2-shared').disabled = state.busy || state.auth?.status === 'running';
  $('b2-sharing-help').textContent = value.storage === 'shared'
    ? 'Every site you save from uses it, each in a folder of its own in your bucket.'
    : `${siteName()} has a Backblaze setup of its own. Your other sites keep the shared one.`;
  // A connected B2 needs only its check and the choice on the dashboard; the steps that made it fold away.
  const folded = b2 && target?.connected === true && !state.b2StepsOpen;
  $('cloud-options').classList.toggle('b2-folded', folded);
  $('cloud-steps').start = folded ? 5 : 1;
  $('b2-steps-fold').hidden = !b2 || !target?.connected;
  $('b2-steps-toggle').textContent = state.b2StepsOpen ? 'Hide the setup steps' : 'Show the setup steps';
  $('b2-steps-toggle').setAttribute('aria-expanded', String(!folded));
  $('connect-b2').textContent = target?.connected ? 'Replace B2 connection' : 'Connect B2';
  const hints = Object.entries(cloudErrors).filter(([code]) => number(target?.failure_reasons?.[code]) > 0)
    .map(([code, message]) => `${display(target.failure_reasons[code])} failed uploads: ${message}`);
  if (hints.length) $('cloud-repair').textContent = hints.join(' ')+' Local episodes are retained. These are recorded repair hints; cloud availability has not been rechecked.';
  for (const record of value.pending_test_cleanup || []) {
    const button = document.createElement('button');
    button.type = 'button'; button.textContent = `Retry ${cloudLabel(record.provider)} test cleanup`;
    if (typeof record.error_code === 'string' && Object.hasOwn(cloudErrors, record.error_code)) $('cloud-repair').textContent += ` Test cleanup pending: ${cloudErrors[record.error_code]} Local originals are retained.`;
    button.addEventListener('click', () => cloudAction(async () => {
      const task = await api(base()+'/cloud/cleanup', {test_id:record.test_id});
      state.task = task.task_id; state.taskKind = 'cloud-cleanup'; state.taskInfo = null;
      cloudNotice('Retrying deletion of the exact recorded test files. Local originals are retained.');
    }));
    $('cloud-cleanup').append(button);
  }
  $('connect-cloud').textContent = `${target?.connected ? 'Sign in again to' : 'Sign in to'} ${cloudLabel(provider)}`;
  $('cloud-setup').textContent = provider !== 'proton' ? `Click Sign in, choose your ${cloudLabel(provider)} account and allow access. Your sign-in is saved for future runs.` : 'Click Sign in and enter your Proton account details on this device. This connection is experimental and is not officially supported by Proton.';
}
$('cloud-provider').addEventListener('change', () => {clearCloudLogin(); renderCloud(); buttons();});
const b2Target = () => state.cloud?.targets?.find(t => t.provider === 'b2');
// Where new episodes go: this device, or one storage that is set up, each named and nothing more.
function renderStorageChoice() {
  const configured = cloudAvailable() && state.cloud?.supported_on_this_interface ? (state.cloud.targets || []).filter(t => t.connected) : [];
  $('cloud-choice').hidden = !configured.length;
  $('cloud-entry').hidden = Boolean(configured.length) || !cloudAvailable() || !state.cloud?.supported_on_this_interface;
  const chosen = configured.find(t => t.enabled)?.provider || 'device';
  const options = !configured.length ? [] : [{key:'device', label:'This device', ready:true}, ...configured.map(t => ({key:t.provider, label:cloudLabel(t.provider), ready:t.tested === true}))];
  $('storage-options').replaceChildren(...options.map(option => {
    const label = document.createElement('label'), input = document.createElement('input');
    input.type = 'radio'; input.name = 'storage-choice'; input.value = option.key;
    input.checked = option.key === chosen; input.dataset.ready = String(option.ready); input.disabled = !option.ready;
    label.append(input, option.label);
    return label;
  }));
  const unchecked = configured.filter(t => !t.tested).map(t => cloudLabel(t.provider));
  $('cloud-choice-help').textContent = chosen !== 'device'
    ? `Each new episode is uploaded to ${cloudLabel(chosen)} as it downloads. None is kept on this device once its upload is checked.`
    : unchecked.length ? `${unchecked.join(' and ')} can be chosen once its connection check passes, on its own tab.` : 'New episodes stay in the save folder.';
  renderSaveSummary();
}
// The dashboard says in one line where new episodes go; they are chosen in Settings.
function renderSaveSummary() {
  const chosen = cloudAvailable() && state.cloud?.supported_on_this_interface ? (state.cloud.targets || []).find(t => t.connected && t.enabled) : null;
  const place = chosen ? cloudLabel(chosen.provider) : ownWindow() ? state.context.library_folder : '';
  $('save-summary-place').textContent = place;
  $('save-summary').hidden = !simpleMode() || !place;
}
// The Storage page: each place episodes can be kept, what it holds and the way to it.
function renderStorage() {
  const held = id => state.locations.find(group => group.id === id);
  const stored = group => group ? `${display(group.episodes)} ${group.episodes === 1 ? 'episode' : 'episodes'} stored · ${room(number(group.bytes))}` : '';
  const device = held('device');
  $('storage-device-stored').textContent = device ? stored(device) : 'Counting…';
  $('storage-device-files').href = '/library#'+state.workspace+'/device';
  const target = b2Target(), [label, tone] = b2Summary(), b2 = held('b2');
  $('storage-b2-pill').textContent = label; $('storage-b2-pill').dataset.tone = tone;
  $('storage-b2-text').textContent = target?.connected
    ? `Your own Backblaze account${target.bucket ? `, bucket ${target.bucket}` : ''}.`
    : 'Your own Backblaze account. Backblaze bills you itself; the first 10 GB is free.';
  $('storage-b2-stored').textContent = stored(b2);
  $('storage-b2-stored').hidden = !b2;
  for (const id of ['storage-b2-files', 'storage-b2-open']) $(id).hidden = !target?.connected;
  $('storage-b2-files').href = '/library#'+state.workspace+'/b2';
  $('storage-b2-setup').textContent = target?.connected ? 'B2 settings' : 'Set up';
}
// The top of the B2 page says at a glance whether B2 is set up, and what is left to do.
function b2Summary() {
  const target = b2Target(), bucket = target?.bucket ? `your bucket ${target.bucket}` : 'your bucket';
  const failed = number(target?.failed);
  return state.auth?.status === 'running' && state.auth.provider === 'b2'
    ? ['Checking your key', 'working', 'Checking your key and bucket with Backblaze. Nothing is uploaded yet.']
    : !target?.connected ? ['Not set up', 'neutral', 'Follow the steps below to connect your own Backblaze account.']
    : !target.tested ? ['Connected, not checked yet', 'warning', `Connected to ${bucket}. Check the connection (step 5) before uploads can start.`]
    : !target.enabled ? ['Set up', 'success', `Set up and checked: ${bucket}. Uploads are off; choose Backblaze B2 on the Storage tab.`]
    : [failed ? 'Uploads need attention' : 'Uploading to B2', failed ? 'warning' : 'success',
       `Set up and checked: ${bucket}. New episodes are uploaded to it as they are saved.${failed ? ` ${display(failed)} ${failed === 1 ? 'upload needs' : 'uploads need'} retrying: click Retry unfinished uploads below.` : ''}`];
}
function renderB2State() {
  const [label, tone, text] = b2Summary();
  $('b2-state-pill').textContent = label;
  $('b2-state-pill').dataset.tone = $('b2-state').dataset.tone = tone;
  $('b2-state-text').textContent = text;
}
// The tabs Settings has here: how the app on this computer downloads, where episodes are kept, and B2's setup.
function settingsTabs() {
  return [['downloads', simpleMode() && state.context?.interface_mode === 'local'], ['storage', ownWindow() || cloudAvailable()],
    ['b2', cloudAvailable()]].filter(([, shown]) => shown).map(([tab]) => tab);
}
// Settings (#settings/<tab>) is a page of its own, its tabs down the side; the browser's Back returns from it.
function showPage() {
  // Earlier addresses of the Storage and B2 pages lead to their tabs.
  const moved = {'#storage': '#settings/storage', '#b2': '#settings/b2'}[location.hash];
  if (moved) history.replaceState(null, '', location.pathname + location.search + moved);
  const tabs = settingsTabs(), [route, wanted] = location.hash.slice(1).split('/');
  const tab = route === 'settings' && tabs.length ? (tabs.includes(wanted) ? wanted : tabs[0]) : '';
  const page = tab ? 'settings' : 'dashboard';
  for (const [id, current] of [['save-nav', !tab], ['settings-nav', Boolean(tab)]])
    if (current) $(id).setAttribute('aria-current', 'page'); else $(id).removeAttribute('aria-current');
  for (const key of ['downloads', 'storage', 'b2']) {
    const link = $('settings-tab-' + key);
    link.hidden = !tabs.includes(key);
    if (key === tab) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current');
  }
  if (document.body.dataset.page === page && document.body.dataset.settingsTab === tab) return;
  const opened = document.body.dataset.page !== page;
  document.body.dataset.page = page; document.body.dataset.settingsTab = tab;
  // Typed keys last only while the B2 tab is open.
  if (tab !== 'b2') $('b2-login').reset();
  if (opened) scrollTo(0, 0);
}
window.addEventListener('hashchange', showPage);
window.addEventListener('popstate', showPage);
$('b2-steps-toggle').addEventListener('click', () => {state.b2StepsOpen = !state.b2StepsOpen; renderCloud();});
$('settings-back').addEventListener('click', () => {history.pushState(null, '', location.pathname + location.search); showPage();});
$('cloud-done').addEventListener('click', () => {location.hash = 'settings/storage';});
function clearCloudLogin() {
  $('cloud-login').reset(); $('proton-signin').hidden = true;
  $('b2-login').reset();
}
async function cloudAuth() {
  if (!cloudAvailable()) return;
  if (!state.workspace || state.context?.interface_mode !== 'local') return;
  const workspace = state.workspace;
  const value = await api(base()+'/cloud/signin');
  if (workspace !== state.workspace) return;
  const previous = state.auth?.status;
  if (value.status === 'running' && ['google', 'dropbox', 'proton', 'b2'].includes(value.provider)
      && $('cloud-provider').value !== value.provider) {
    $('cloud-provider').value = value.provider;
    clearCloudLogin(); renderCloud();
  }
  state.auth = value;
  renderB2State();
  $('cloud-auth-status').textContent = value.status === 'running' ? (value.stage === 'preparing' ? 'Preparing cloud sign-in. This may take a moment the first time.' : value.provider === 'b2' ? 'Checking your B2 key and private library bucket…' : value.stage === 'checking_saved_session' ? `Checking your saved ${cloudLabel(value.provider)} sign-in…` : value.provider !== 'proton' ? `Complete ${cloudLabel(value.provider)} sign-in in your browser. Your files stay on this device while connecting.` : 'Signing in to Proton…')
    : value.status === 'connected' ? (value.reused_session ? 'Your saved sign-in is ready. You don’t need to sign in again.' : 'Signed in. Next, click Test cloud connection.')
    : value.status === 'cancelled' ? 'Sign-in cancelled. Your saved files are safe.'
    : value.code === 'signin_network_held' ? 'Mobile data is off. Connect to Wi-Fi or turn on Allow mobile data, then try again.'
    : value.code === 'cloud_client_required' ? `${cloudLabel(value.provider)} setup needs an update from the app maintainer. Your saved sign-in and files are safe.`
    : value.code === 'cloud_rate_limited' ? `${cloudLabel(value.provider)} is limiting this app’s requests. Your sign-in is saved; signing in again won’t fix the request limit.`
    : value.status === 'error' ? cloudErrors[value.code] || 'Sign-in did not finish. Check your details and connection, then try again.' : '';
  $('cloud-auth-link').hidden = value.status !== 'running' || !value.auth_url;
  if (value.auth_url) $('cloud-auth-link').href = value.auth_url;
  else $('cloud-auth-link').removeAttribute('href');
  if (previous !== 'connected' && value.status === 'connected') {delete state.bucketDrafts[workspace]; $('b2-login').reset(); await cloud(); await librarySummary(true);}
  buttons();
}
async function connectCloud(credentials = {}) {
  const provider = $('cloud-provider').value;
  try {
    if (state.network?.supported_on_this_interface && state.network.wifi_only === $('allow-mobile-data').checked) {
      await api(base()+'/network', {wifi_only:!$('allow-mobile-data').checked}); await network(false);
    }
    state.auth = await api(base()+'/cloud/signin', {provider, credentials});
    cloudNotice(provider === 'b2' ? 'Connecting B2. Your key is being checked; no episodes are uploaded during setup.' : 'Cloud sign-in started. Follow the instructions beside the sign-in button.');
    await cloudAuth();
    if (state.auth?.status === 'connected') await cloud();
  } finally { if (provider !== 'b2') clearCloudLogin(); }
}
$('connect-cloud').addEventListener('click', () => {
  if ($('cloud-provider').value === 'proton') {
    $('proton-signin').hidden = false; $('cloud-username').focus();
  } else action(() => connectCloud());
});
$('cloud-login').addEventListener('submit', event => {
  event.preventDefault();
  if (!$('cloud-login').reportValidity()) return;
  const credentials = {username:$('cloud-username').value, password:$('cloud-password').value,
    code:$('cloud-code').value, mailbox_password:$('cloud-mailbox-password').value};
  clearCloudLogin(); action(() => connectCloud(credentials));
});
// A copied key often carries a space or line break at its ends, which the fields' patterns would refuse.
for (const id of ['b2-key-id', 'b2-application-key', 'b2-bucket-name']) $(id).addEventListener('input', () => {
  const value = $(id).value.replace(/\s+/g, ''); if (value !== $(id).value) $(id).value = value; });
$('b2-bucket-name').addEventListener('input', () => {
  state.bucketDrafts[state.workspace] = $('b2-key-bucket').textContent = $('b2-bucket-name').value;});
$('b2-login').addEventListener('submit', event => {
  event.preventDefault();
  if (!$('b2-bucket-name').reportValidity() || !$('b2-login').reportValidity() || $('connect-b2').disabled) return;
  // The name is kept as typed, so a refused key leaves it there to correct.
  const bucket = state.bucketDrafts[state.workspace] = $('b2-bucket-name').value.trim();
  const credentials = {key_id:$('b2-key-id').value.trim(), application_key:$('b2-application-key').value.trim(), bucket};
  // The keys stay in their fields until they connect, so a refused one can be corrected rather than pasted again.
  cloudAction(() => connectCloud(credentials));
});
$('cancel-cloud-signin').addEventListener('click', () => action(async () => {
  await api(base()+'/cloud/cancel-signin', {}); await cloudAuth();
}));
$('test-cloud').addEventListener('click', () => cloudAction(async () => {
  const value = await api(base()+'/cloud/test', {provider:$('cloud-provider').value});
  state.task = value.task_id; state.taskKind = 'cloud-test'; state.taskInfo = null;
  cloudNotice('Testing two completed episodes: upload, read back and verify, then delete test copies. Local originals are retained.');
}));
$('storage-options').addEventListener('change', event => {
  const chosen = event.target.value;
  if (state.busy) { renderCloud(); return; }  // Another action is running; show the saved choice again.
  action(async () => {
    try {
      // One place at a time: whatever was chosen before is turned off first.
      for (const target of (state.cloud?.targets || []).filter(t => t.enabled && t.provider !== chosen))
        await api(base()+'/cloud', {provider:target.provider, enabled:false});
      if (chosen !== 'device') await api(base()+'/cloud', {provider:chosen, enabled:true});
      await cloud(); $('cloud-choice-status').textContent = '';
    } catch (error) { $('cloud-choice-status').textContent = error.message; await cloud().catch(renderCloud); }
  });
});
$('b2-shared').addEventListener('change', event => {
  const storage = event.target.checked ? 'shared' : 'separate';
  action(async () => { state.cloud = await api(base()+'/cloud/storage', {storage}); }).then(renderCloud);
});
$('retry-cloud').addEventListener('click', () => cloudAction(async () => {
  const value = await api(base()+'/cloud/upload', {});
  state.task = value.task_id; state.taskKind = 'cloud'; state.taskInfo = null;
  cloudNotice('Retrying up to three pending episode uploads.');
}));
$('save-storage').addEventListener('click', () => action(async () => {
  await api(base()+'/storage', {image_profile: $('image-profile').value});
  await storage(); notice('Quality choice saved for future comic imports. Existing files remain unchanged.');
}));
$('refresh-storage').addEventListener('click', () => action(() => storage()));
$('purge-cache').addEventListener('click', () => action(async () => {
  const value = await api(base()+'/storage/purge', {});
  state.task = value.task_id; state.taskKind = 'purge'; state.taskInfo = null;
  notice('Verified cache cleanup queued. Incomplete downloads remain.');
}));
async function action(callback) {
  if (state.busy) return;
  state.busy = true; buttons(); renderSeries();
  try { await callback(); } catch (error) { notice(error.message, true); }
  finally { state.busy = false; buttons(); renderSeries(); }
}
$('import-series').addEventListener('submit', event => {
  event.preventDefault();
  if (!canImport() || !validSeriesLink()) return;
  if (state.context?.interface_mode === 'local' && !reviewed()) { action(reviewDownload); return; }
  if (state.context?.interface_mode === 'local' && !number(state.preview.planned_episode_count)) return;
  const options = {url: $('series-url').value.trim(), download_workers: 2, request_interval: 0.25};
  if ($('series-amount').value === 'one') options.limit = 1;
  action(async () => {
    if (state.context?.interface_mode !== 'local') await applyDownloadChoices();
    await queue('archive_series_url', options);
  });
});
$('series-url').addEventListener('keydown', event => {
  if (event.key === 'Enter' && state.context?.interface_mode === 'local' && !reviewed()) {
    event.preventDefault();
    if (canImport() && $('series-url').reportValidity()) $('import-series').requestSubmit();
  }
});
async function applyDownloadChoices() {
    const key = reviewKey();
    const profile = $('image-profile').value, wifiOnly = !$('allow-mobile-data').checked;
    // Apply the visible choices before calculating the review. No separate
    // settings-save clicks are needed in the first-download flow.
    if (profile !== state.storage?.image_profile) {
      await api(base()+'/storage', {image_profile:profile});
      await storage(false);
    }
    if (cloudAvailable() && state.network?.supported_on_this_interface && wifiOnly !== state.network.wifi_only) {
      await api(base()+'/network', {wifi_only:wifiOnly});
      await network(false);
    }
    if (key !== reviewKey()) throw new Error('Your choices changed while saving options. Click Review download again.');
}
async function reviewDownload() {
    if (!$('series-url').reportValidity()) return;
    const key = reviewKey(), url = $('series-url').value.trim();
    const scope = $('series-amount').value === 'one' ? 'one' : 'series';
    state.preview = null;
    await applyDownloadChoices();
    const task = await api(base()+'/storage/preview', {url, scope});
    state.previewUrl = {url, scope, key}; state.task = task.task_id; state.taskKind = 'preview'; state.taskInfo = null;
    $('series-storage').textContent = 'Checking current access and available space. Episode images are not downloaded.';
    notice('Series storage check queued. No import was started.');
}
$('preview-series').addEventListener('click', () => {
  if (!canImport() || !$('series-url').reportValidity()) return;
  action(reviewDownload);
});
for (const id of ['series-url','series-amount','image-profile','allow-mobile-data']) $(id).addEventListener('input', () => {
  state.preview = null;
  $('series-storage').textContent = 'Your choices changed. Click Review download to check this selection.';
  if (id === 'allow-mobile-data') $('network-status').textContent = `Mobile data will be ${$('allow-mobile-data').checked ? 'allowed' : 'off'} when you review this download.${$('allow-mobile-data').checked ? ' This may use your data allowance.' : ''}`;
  buttons();
});
function renderPreview(value) {
  const scope = $('series-amount').value === 'one' ? 'one' : 'series';
  if (!value || state.previewUrl?.url !== $('series-url').value.trim() || state.previewUrl?.scope !== scope || state.previewUrl?.key !== reviewKey()) {
    state.preview = null;
    $('series-storage').textContent = 'The check finished for a previous selection. Check this selection again.'; return;
  }
  state.preview = value;
  $('series-storage').textContent = `${display(value.planned_episode_count)} new ${number(value.planned_episode_count) === 1 ? 'episode' : 'episodes'} to save; ${display(value.already_saved_episode_count)} already saved. Roughly ${bytes(value.estimated_additional_archive_bytes)} ${!desktopOnly() && value.storage_destination === 'cloud' ? 'in cloud storage' : 'on this computer'}, with ${bytes(value.free_bytes)} free. ${value.fits_estimate ? 'The current estimate fits.' : desktopOnly() ? 'Space is limited: choose smaller images, a one-episode sample, or free space on this computer.' : 'Space is limited: choose cloud storage, smaller images, a one-episode sample, or use a PC.'} ${value.content_type_verified ? (value.content_type === 'novel' ? 'Based on novel sizes.' : 'Based on comic sizes.') : 'Comic sizes are a conservative fallback.'} Smaller-image savings depend on the artwork.`;
}
async function poll() {
  if (state.busy || state.polling || !state.workspace) return;
  state.polling = true;
  const workspace = state.workspace, task = state.task, kind = state.taskKind;
  try {
    if (simpleMode() && !state.task) {
      await Promise.all([refresh(), account(), siteAuth(), cloud(), cloudAuth()]);
      // The connection can change under a running download; check it now and then, not every poll.
      if (Date.now() - (state.networkCheckedAt || 0) > 15000) await network(false);
      state.lastAccountRefreshAt = Date.now(); state.accountOffline = false;
      // The saved summary is at most 15 seconds old; reading every file again is left to a refresh someone asked for.
      if (Date.now() - (state.libraryRefreshedAt || 0) > 30000) await librarySummary();
      if (state.accountMessage?.polling || state.accountMessage?.until < Date.now()) state.accountMessage = null;
      return;
    }
    if (state.task) {
      const result = await api(base()+'/tasks/'+state.task);
      if (workspace !== state.workspace || task !== state.task) return;
      state.taskInfo = result;
      if (['ok','error'].includes(result.status)) {
        state.task = null; state.taskKind = null; state.taskInfo = null;
        if (kind === 'session') {
          const verified = result.status === 'ok' && result.result?.authenticated === true;
          const message = result.status === 'error' ? errors[result.code] || 'Could not check your Tapas connection. Try again.'
            : verified ? result.result.account_binding === 'matches' ? 'Tapas authentication verified and account matches this library.' : 'Tapas authentication verified. This library will be bound on its first import.'
            : 'Your Tapas connection could not be verified. Sign in to Tapas again, then retry.';
          state.connectionCheck = { message, error: !verified, pending: false }; renderConnectionCheck();
          notice(message, !verified);
        }
        else if (result.status === 'error') {
          if (kind?.startsWith('cloud')) cloudNotice(errors[result.code] || errors.task_failed, true);
          else notice(errors[result.code] || errors.task_failed, true);
        }
        else if (result.result?.authenticated === true) notice(result.result.account_binding === 'matches' ? 'Tapas authentication verified and account matches this library.' : 'Tapas authentication verified. This library will be bound on its first import.');
        else if (kind === 'cloud') cloudNotice(`${display(result.result?.uploaded_episodes)} uploads verified; ${display(result.result?.local_archives_deleted)} local archives removed.`);
        else if (kind === 'cloud-test') cloudNotice(`${display(result.result?.verified_episodes)} episodes verified; test copies deleted. Local originals retained.`);
        else if (kind === 'cloud-cleanup') cloudNotice('Recorded test copies deleted. Local originals retained.');
        else if (kind === 'preview') {renderPreview(result.result?.storage_preview); notice('Series storage check complete. Choose quality and storage before importing.');}
        else if (kind === 'purge') notice(`${display(result.result?.cache_files)} verified cached images removed.`);
        else if (kind === 'schedule') notice('Daily schedule updated. Check its next due time below.');
        else if (result.result?.pause_reason === 'network_unavailable') notice('Tapas is temporarily unavailable. Import paused; saved archives and cached images are retained.', true);
        else if (result.result?.cancelled_before_start === true) notice('Queued import stopped before it started. No images were downloaded.');
        else if (result.result?.status === 'cancelled') notice(`Batch stopped. ${display(result.result?.archived_episode_ids?.length)} episodes saved; cached images are ready to resume.`);
        else if (kind === 'settings') notice('Queue order saved. Future batches will use this order.');
        else if (number(result.result?.metadata_unavailable_series_count) > 0) notice(`Batch finished. ${display(result.result?.archived_episode_ids?.length)} episodes saved. Some series details need a retry; saved archives are retained.`);
        else if (number(result.result?.archived_episode_ids?.length) === 0 && number(result.result?.enriched_series_ids?.length) > 0) notice('Series details updated. Saved episodes were reused.');
        else notice(`Batch finished. ${display(result.result?.archived_episode_ids?.length)} episodes saved. Check the remaining queue and coverage below.`);
        if (kind === 'import' && result.status === 'ok') {
          const total = duration(result.result?.elapsed_seconds), scan = duration(result.result?.scan_elapsed_seconds);
          $('batch-timing').textContent = total ? `Last batch: ${total} total${scan ? `; ${scan} checking lists and series` : ''}. Fresh episode access is checked before each download.` : '';
        }
        if (kind === 'import' && result.status === 'ok' && result.result?.refresh) {
          const scan = result.result.refresh;
          notice(`${$('notice').textContent} Known series: ${display(scan.series_checks_completed)} of ${display(scan.series_check_target_count)} checked; ${display(scan.successful_series_checks)} complete episode lists; ${display(scan.held_series_checks)} held checks; ${display(scan.remaining_series_count)} remaining in this scan cycle.`);
        }
        if (kind === 'import') state.taskLabel = $('notice').textContent;
        if (!simpleMode()) { await daily(); await storage(false); }
        await cloud(); await librarySummary(true);
      }
    }
    if (workspace === state.workspace) {
      await refresh(); await cloudAuth();
      if (Date.now() - (state.libraryRefreshedAt || 0) > 60000) await librarySummary();
    }
  } catch (error) {
    if (kind === 'session' && workspace === state.workspace && task === state.task) {
      state.connectionCheck = { message: `${error.message} Waiting to retry the connection check status.`, error: true, pending: true };
      renderConnectionCheck();
    }
    notice(error.message, true);
    // A failed refresh says it will retry, rather than showing the raw error; the next good refresh clears it.
    if (simpleMode()) {
      state.accountOffline = true;
      state.accountMessage = {message:'Could not refresh progress. We’ll retry automatically; your saved work is retained.', error:true, polling:true};
    }
  }
  finally { state.polling = false; buttons(); renderSeries(); }
}
// The speed of each other site there is a sign-in for, which its next Go uses; that site's downloader is stopped.
function renderOtherSpeeds(others) {
  const box = $('other-site-speeds');
  for (const row of [...box.children]) if (!others.some(item => item.key === row.dataset.site)) row.remove();
  for (const item of others) {
    let row = [...box.children].find(node => node.dataset.site === item.key);
    if (!row) {
      row = document.createElement('div'); row.className = 'setting'; row.dataset.site = item.key;
      const label = document.createElement('label'), select = document.createElement('select'), help = document.createElement('p');
      select.id = 'site-speed-' + item.key; label.htmlFor = select.id; help.className = 'muted';
      for (const [value, text] of [['fast', 'Fast'], ['medium', 'Medium'], ['slow', 'Slow']]) select.add(new Option(text, value));
      select.addEventListener('change', async () => {
        // Read the choice first: action() redraws Settings, which shows the saved speed until this one is kept.
        const speed = select.value;
        await action(async () => {
          // api() fetches the context anew before it posts, so the answer goes on that one.
          const {site_settings} = await api('/api/v1/site-speed', {site:item.key, speed});
          state.context.site_settings = site_settings;
        });
        renderAccount();
      });
      row.append(label, ' ', select, help); box.append(row);
    }
    const [label, select, help] = [row.querySelector('label'), row.querySelector('select'), row.querySelector('p')];
    label.textContent = `Speed on ${item.name}`;
    if (select.value !== item.speed && ['fast', 'medium', 'slow'].includes(item.speed)) select.value = item.speed;
    select.disabled = state.busy;
    help.textContent = `Used the next time you hit Go on ${item.name}.`;
  }
}
function renderAccount() {
  if (!simpleMode()) return;
  const local = state.context?.interface_mode === 'local';
  const accountRunning = state.account?.status === 'running';
  const signingIn = state.siteAuth?.status === 'running';
  const reported = state.status?.worker?.reported_state;
  // Right after a sign-in is kept, the downloader can take a minute to say it is there: it is starting, not offline.
  const worker = !['starting','ready','busy'].includes(reported) && state.siteAuth?.status === 'connected'
    && Date.now() - (state.connectedAt || 0) < 120000 ? 'starting' : reported;
  const batchRunning = worker === 'busy' || state.status?.progress?.status === 'running';
  const needsLogin = !state.status?.session_configured || (state.account?.code === 'login_required' && state.siteAuth?.status !== 'connected');
  // A downloader that is still starting is not offline: signing in again would not help it.
  $('site-signin').hidden = !local || (!needsLogin && ['starting','ready','busy'].includes(worker) && !signingIn && !state.accountMessage);
  $('site-signin').disabled = state.busy || accountRunning || batchRunning || signingIn || !state.workspace;
  const site = siteName();
  $('site-signin').textContent = signingIn ? 'Signing in…' : `Sign in to ${site}`;
  $('dashboard-intro').textContent = `Sign in to ${site}, then hit Go to save the episodes your account can read.`;
  const goWait = accountRunning ? 0 : Math.max(0, number(state.account?.go_after_seconds) - sinceAccount());
  $('account-go').disabled = !local || state.busy || Boolean(state.task) || state.auth?.status === 'running' || !state.account || needsLogin || signingIn || accountRunning || worker !== 'ready' || goWait > 0;
  $('account-go').textContent = accountRunning ? 'Working…' : state.account?.status === 'stopped' || state.account?.status === 'held' ? 'Go — continue' : 'Go';
  $('account-pause').hidden = !accountRunning;
  $('change-folder').disabled = state.busy || accountRunning || batchRunning || signingIn;
  $('change-folder').title = accountRunning || batchRunning ? 'Pause saving before you change the folder.' : '';
  $('site').disabled = state.busy || accountRunning || batchRunning || signingIn;
  $('site').title = accountRunning || batchRunning ? 'Pause saving to change the site.' : '';
  $('account-pause').disabled = state.busy;
  const speeds = {fast:`The quickest. If ${site} keeps pausing the download, choose a slower speed.`, medium:'About four images a second.', slow:'About one image a second. A large library can take months at this speed.'};
  const speed = speeds[state.account?.speed] ? state.account.speed : 'fast';
  // Settings lists the speed of each site there is a sign-in for; one that is not signed in has none to choose.
  const others = (state.context?.site_settings || []).filter(item => !item.current);
  const signedIn = Boolean(state.status?.session_configured);
  $('account-speed-row').hidden = !local || !signedIn;
  $('account-speed-label').textContent = others.length ? `Speed on ${site}` : 'Speed';
  if ($('account-speed').value !== speed) $('account-speed').value = speed;
  $('account-speed').disabled = !local || state.busy || accountRunning || signingIn || !state.account;
  $('account-speed-help').textContent = accountRunning ? 'Pause to change the speed.' : speeds[speed];
  renderOtherSpeeds(local ? others : []);
  $('downloads-signin-note').hidden = !local || signedIn || others.length > 0;
  $('account-download').setAttribute('aria-busy', String(accountRunning || signingIn));
  const net = state.network;
  const networkHeld = Boolean(local && net?.supported_on_this_interface && net.wifi_only && !net.allowed);
  $('account-network').hidden = !local || !net?.supported_on_this_interface;
  // A tick being saved stays as the person left it until the saved choice comes back.
  if (!state.networkSaving) $('account-mobile-data').checked = net?.wifi_only === false;
  $('account-mobile-data').disabled = state.busy || !net?.supported_on_this_interface;
  $('account-network-help').textContent = net?.wifi_only === false ? 'Saving uses any connection, including metered and mobile ones. This may use your data allowance.'
    : net?.allowed ? 'Off: saving uses Wi-Fi or a wired connection, and waits on a metered or mobile one.'
    : net?.reason === 'metered_connection' ? 'This connection is metered, so saving waits for Wi-Fi or a wired connection. Tick this to save on it anyway; this may use your data allowance.'
    : 'This computer cannot tell whether its connection is metered (a VPN can cause this), so saving waits. Tick this to save on it anyway.';
  const connectionHeld = net?.reason === 'metered_connection' ? 'this connection is metered' : 'this computer cannot tell whether its connection is metered (a VPN can cause this)';
  $('account-allow-network').hidden = !networkHeld;
  $('account-allow-network').disabled = state.busy;
  const authMessages = {network_held:'Connect to Wi-Fi or a wired connection, or press Allow this connection, then sign in again.',
    account_mismatch:`That ${site} account does not match this saved library. Sign in to the original account.`,
    signin_failed:`Sign-in did not finish. Try Sign in to ${site} again.`,
    signin_incomplete:`Sign-in was not completed. Click Sign in to ${site} to try again.`};
  $('site-signin-status').textContent = state.accountMessage?.message || (signingIn ? `Finish signing in to ${site}. We’ll detect it automatically; then return here and hit Go.` : state.siteAuth?.status === 'error' ? authMessages[state.siteAuth.code] || authMessages.signin_failed : '');
  $('site-signin-status').classList.toggle('error', Boolean(state.accountMessage?.error || state.siteAuth?.status === 'error'));
  $('settings-status').textContent = state.accountMessage?.message || '';
  $('settings-status').classList.toggle('error', Boolean(state.accountMessage?.error));
  $('dashboard-footnote').textContent = ownWindow() ? 'Saving runs only while this window is open. Closing it pauses, and Go carries on later.'
    : 'Saved episodes stay safe. You can close this page and return later.';
  const phases = {existing_backlog:'Finishing saved downloads…', lists:`Finding series in your ${site} account…`,
    series:'Checking your series and downloading accessible episodes…', archive:'Downloading your episodes…', verify:'Checking the completed library…'};
  const finished = state.account?.status === 'finished' || (state.account?.status === 'held' && state.account.code === 'archive_incomplete');
  const missing = number(state.account?.audit?.missing_recorded_accessible_episode_count);
  let message = !state.workspace ? 'Connecting to your library…' : !local ? `Use the local archiver to connect your ${site} account.`
    : signingIn ? `Finish signing in to ${site} in the window that opened.`
    : needsLogin ? (state.account?.code === 'login_required' ? `Paused: ${site} signed you out. Saved files are safe. Sign in to ${site} again, then hit Go to continue.` : `Sign in to ${site} to get started.`)
    : accountRunning ? phases[state.account.phase] || 'Downloading your library…'
    : state.account?.status === 'held' && networkHeld ? `Paused: ${connectionHeld}, and the app saves only on Wi-Fi or a wired connection. Saved files are safe. Connect to Wi-Fi, or press Allow this connection, then hit Go to continue.`
    : state.account?.status === 'held' ? heldMessage(state.account.code, site, goWait, missing)
    : batchRunning ? (state.status?.progress?.cancellation_requested ? 'Pausing after the image being saved. Your saved files are safe.' : 'Finishing the current step. Your saved files are safe.')
    : worker === 'starting' ? 'Starting the downloader…'
    : worker !== 'ready' ? `The downloader is offline. Click Sign in to ${site} to reconnect.`
    : networkHeld ? `Saving would wait: ${connectionHeld}, and the app saves only on Wi-Fi or a wired connection. Connect to Wi-Fi, or press Allow this connection.`
    : state.account?.status === 'stopped' ? (goWait ? `Paused. ${site} asked the app to wait: Go works again in ${waitText(goWait)}.` : 'Paused. Hit Go to continue from saved progress.')
    : state.account?.status === 'finished' ? (number(state.status?.episodes_by_status?.archived) ? 'Finished. Every episode your account can read is saved. Hit Go any time to check for new ones.'
      : `Finished. No episodes your ${site} account can save were found. Episodes you have bought, or can read for free, are saved when there are any. Hit Go to check again.`)
    : `Ready. Hit Go to save your ${site} library.`;
  const lists = state.account?.lists;
  const listNames = {unlocked:'Bought',subscribed:'Subscribed',updated:'Updated',recent:'Recent',free_episodes:'Free episodes'};
  const listPhase = accountRunning && state.account.phase === 'lists';
  const activeList = listNames[lists?.current];
  if (listPhase && activeList) message = `Finding series — ${activeList} list (${display(number(lists.checked)+1)} of ${display(lists.total)}).`;
  $('account-status').textContent = message;
  // The choices a held download needs are in Settings: the card leads straight to them.
  $('account-settings-link').hidden = !(local && !networkHeld && state.account?.status === 'held' && ['source_refused','source_recovery_limit'].includes(state.account.code));
  const counts = state.status?.episodes_by_status || {};
  const saved = display(counts.archived);
  const p = state.status?.progress;
  const step = !listPhase ? stepProgress(p) : '';
  $('account-detail').textContent = !state.status ? 'Loading saved-episode count…' : `${saved} ${number(counts.archived) === 1 ? 'episode' : 'episodes'} saved.${step ? ' '+step : ''}${finished && site === 'Tapas' ? ' Tapas’s account lists may not include every older purchase.' : ''}`;
  const left = local && state.account?.configured && !finished ? timeLeft(state.status?.estimate) : '';
  $('account-estimate').textContent = left; $('account-estimate').hidden = !left;
  const space = left ? spaceNeeded(state.status?.estimate) : '';
  $('account-space').textContent = space; $('account-space').hidden = !space;
  $('account-live-progress').hidden = !accountRunning;
  $('account-list-meter').hidden = !listPhase;
  $('account-list-help').hidden = !listPhase;
  if (listPhase) {
    $('account-list-meter').value = number(lists?.checked);
    $('account-list-meter').max = number(lists?.total) || 4;
    const matchingList = p?.status === 'running' && p.phase === 'account_list' && p.category === lists?.current;
    $('account-list-count').textContent = `${display(lists?.checked)} of ${display(lists?.total || 4)} lists checked. `+(matchingList && Number.isInteger(p.reading_list_page_count)
      ? `${display(p.reading_list_page_count)} pages read · ${display(p.observed_library_series_count)} series listed in ${activeList}.`
      : p?.status === 'running' && p.phase === 'scan' ? 'Checking series details before moving to the next list.' : `Opening the ${activeList || 'next'} list and waiting for ${site}.`);
  } else $('account-list-count').textContent = accountRunning && state.account.phase === 'series'
    ? `${display(state.status?.scan_remaining_series_count)} known series still awaiting checks.` : '';
  const sinceSample = sinceAccount();
  const age = Number.isInteger(state.account?.progress_age_seconds) ? state.account.progress_age_seconds + sinceSample : null;
  const elapsed = Number.isInteger(state.account?.elapsed_seconds) ? state.account.elapsed_seconds + sinceSample : null;
  const sinceRefresh = state.lastAccountRefreshAt ? Math.max(0, Math.floor((Date.now()-state.lastAccountRefreshAt)/1000)) : null;
  $('account-clock').textContent = [elapsed === null ? null : `Elapsed: ${duration(elapsed)}`,age === null ? null : `Last recorded activity: ${duration(age)} ago`,sinceRefresh === null ? null : `Status checked ${duration(sinceRefresh)} ago`].filter(Boolean).join(' · ');
  const disconnected = state.accountOffline || (sinceRefresh !== null && sinceRefresh >= 15);
  // Between two steps, and while the download itself waits to try again, nothing is overdue.
  const between = Number.isInteger(state.account?.between_steps_seconds) ? state.account.between_steps_seconds + sinceSample : null;
  const retry = accountRunning && Number.isInteger(state.account?.retry?.seconds) ? state.account.retry : null;
  const retryLeft = retry ? Math.max(0, retry.seconds - sinceSample) : 0;
  const retryIn = waitText(retryLeft);
  const waiting = !retry && (age !== null ? age >= 60 : between !== null ? between >= 60 : elapsed !== null && elapsed >= 60);
  $('account-live-progress').classList.toggle('waiting', waiting || disconnected);
  $('account-activity').textContent = !accountRunning ? '' : disconnected
    ? 'Progress updates are delayed. The app is trying to reach its downloader again; what it is doing right now is unknown.'
    : retry ? (retry.reason === 'source' ? `${site} asked the app to wait. The download continues by itself in ${retryIn}.`
      : retry.reason === 'images' ? `Some images did not arrive. The download tries them again by itself in ${retryIn}.`
      : networkHeld ? `Waiting: ${connectionHeld}, and the app saves only on Wi-Fi or a wired connection. The download carries on by itself on Wi-Fi, or press Allow this connection.`
      : `The connection to ${site} was interrupted. The download tries again by itself in ${retryIn}.`)
    : waiting ? `No new progress for over a minute; ${site} may be slow. You can wait, or press Pause.`
    : state.status?.worker?.reported_state === 'ready' ? 'Working. Getting the next step ready.'
    : 'Working. Progress updates every five seconds.';
  renderDashboard({local,accountRunning,signingIn,worker,batchRunning,needsLogin,disconnected,waiting,sinceRefresh});
}
// Why a held download stopped, and what the person can do about it, in one sentence each.
function heldMessage(code, site, goWait, missing) {
  const safe = 'Saved files are safe.';
  if (code === 'archive_incomplete') return missing
    ? `Finished, but ${display(missing)} ${missing === 1 ? 'episode' : 'episodes'} could not be saved. ${safe} Hit Go to check again.`
    : `Finished, but some saved files need another check. ${safe} Hit Go to check again.`;
  if (['source_refused','source_recovery_limit'].includes(code)) return `Paused: ${site} asked the app to slow down. ${safe} `
    + (goWait ? `Go works again in ${waitText(goWait)}.` : 'Hit Go to try again. If this keeps happening, choose a slower speed in Settings.');
  if (code === 'source_access_changed') return `Paused: ${site} still would not send an episode after waiting. ${safe} Check that you can read it on ${site}, then hit Go to continue.`;
  // While the connection is still held, the caller's own message names it; this one is for after it changed.
  if (code === 'network_policy') return `Paused while the connection was not Wi-Fi or wired. ${safe} Hit Go to continue.`;
  if (['network_unavailable','network_recovery_limit'].includes(code)) return `Paused: ${site} could not be reached several times in a row. ${safe} Check your internet connection, then hit Go to continue.`;
  if (['no_progress','scan_no_progress'].includes(code)) return `Paused: the last few tries saved nothing new. ${safe} Hit Go to try again; if it pauses again, ${site} may be having trouble, so try later.`;
  if (code === 'cancelled') return 'Paused. Hit Go to continue from saved progress.';
  return `Paused: the download stopped unexpectedly. ${safe} Check that this disk has free space, then hit Go to try again.`;
}
const waitText = seconds => seconds >= 90 ? `about ${Math.round(seconds/60)} minutes` : seconds > 45 ? 'about a minute' : 'a few seconds';
const sinceAccount = () => state.accountReceivedAt ? Math.max(0, Math.floor((Date.now()-state.accountReceivedAt)/1000)) : 0;
// The step being worked on, as plainly as the card puts it; the full detail is on the advanced page.
function stepProgress(p) {
  if (p?.status !== 'running' || p.cancellation_requested) return '';
  if (p.phase === 'scan' && Number.isInteger(p.series_check_target_count))
    return `Checked ${display(p.series_checks_completed)} of ${display(p.series_check_target_count)} series in this step.`;
  if (p.phase !== 'archive') return '';
  const chapters = number(p.downloaded_chapters), images = number(p.downloaded_pages) - chapters;
  return `${display(p.completed)} saved in this step · ${display(images)} ${images === 1 ? 'image' : 'images'}${chapters ? ` · ${display(chapters)} novel ${chapters === 1 ? 'chapter' : 'chapters'}` : ''}.`;
}
function renderDashboard({local,accountRunning,signingIn,worker,batchRunning,needsLogin,disconnected,waiting,sinceRefresh}) {
  let title = 'Loading your status…', label = 'Loading', tone = 'neutral';
  if (state.workspace && state.status && (!local || state.account)) {
    if (disconnected) {title='Status updates unavailable';label='Reconnecting';tone='warning';}
    else if (!local) {title='Your saved library';label='Local archiver needed';}
    else if (signingIn) {title='Finish signing in';label='Signing in';tone='working';}
    else if (needsLogin) {title='Sign in to get started';label='Sign-in needed';tone='warning';}
    else if (accountRunning) {
      const phases = {lists:['Finding your series','Finding series'],series:['Checking your library','Checking series'],
        archive:['Saving your episodes','Downloading'],verify:['Checking saved files','Checking files'],
        existing_backlog:['Finishing saved downloads','Downloading']};
      [title,label] = phases[state.account.phase] || ['Saving your library','Working'];tone='working';
      if (waiting) {title='Waiting for a progress update';label='Waiting';tone='warning';}
    } else if (state.account?.status === 'held' && state.account.code === 'archive_incomplete') {
      title='Finished, with episodes missing';label='Finished';tone='warning';
    } else if (state.account?.status === 'held' || state.account?.status === 'stopped') {
      title='Your download is paused';label='Paused';tone='warning';
    } else if (batchRunning) {title='Finishing the current step';label='Working';tone='working';}
    else if (worker === 'starting') {title='Starting the downloader';label='Starting';tone='working';}
    else if (worker !== 'ready') {title='The downloader is offline';label='Offline';tone='warning';}
    else if (state.account?.status === 'finished') {title='This run is finished';label='Finished';tone='success';}
    else {title='Ready to save';label='Ready';tone='success';}
  }
  $('account-save-title').textContent=title;
  $('dashboard-state').textContent=label; $('dashboard-state').dataset.tone=tone;
  $('account-download').dataset.tone=tone;
  const site = siteName();
  $('dashboard-session').textContent=`${site} · ` + (!state.status ? 'Checking…' : !local ? 'Local sign-in needed'
    : signingIn ? 'Signing in' : needsLogin ? 'Sign-in needed'
    : state.siteAuth?.status === 'connected' ? 'Connected' : 'Session saved');
  $('dashboard-session').dataset.tone=needsLogin && state.status ? 'warning' : 'neutral';
  // Before the first sign-in the downloader has nothing to start with: that is not a fault to warn about.
  const notYet = needsLogin && !['starting','ready','busy'].includes(worker);
  $('dashboard-worker').textContent=!state.status ? 'Downloader · Checking…' : disconnected ? 'Downloader · Status unknown'
    : worker === 'busy' ? 'Downloader · Working' : worker === 'ready' ? 'Downloader · Ready' : worker === 'starting' ? 'Downloader · Starting'
    : notYet ? 'Downloader · Starts after sign-in' : 'Downloader · Offline';
  $('dashboard-worker').dataset.tone=disconnected || (state.status && !notYet && !['starting','ready','busy'].includes(worker)) ? 'warning' : 'neutral';
  $('dashboard-updated').textContent=disconnected ? 'Updates delayed · trying to reconnect'
    : sinceRefresh === null ? 'Checking current status…' : sinceRefresh < 2 ? 'Updated just now' : `Updated ${sinceRefresh}s ago`;
}
async function account() {
  if (!simpleMode() || !state.workspace || state.context?.interface_mode !== 'local') return;
  const workspace = state.workspace;
  const value = await api(base()+'/account');
  if (workspace !== state.workspace) return;
  const previous = state.account?.status;
  state.account = value;
  state.accountReceivedAt = Date.now(); state.lastAccountRefreshAt = Date.now();
  if (previous !== value.status && value.status === 'finished') await librarySummary(true);
  renderAccount();
}
async function siteAuth() {
  if (!simpleMode() || !state.workspace || state.context?.interface_mode !== 'local') return;
  const workspace = state.workspace;
  const value = await api(base()+'/session/signin');
  if (workspace !== state.workspace) return;
  if (value.status === 'connected' && state.siteAuth?.status !== 'connected') state.connectedAt = Date.now();
  state.siteAuth = value; renderAccount();
}
async function accountAction(callback) {
  if (state.busy) return;
  state.accountMessage = null;
  await action(async () => {
    try {await callback();}
    catch (error) {state.accountMessage = {message:error.message, error:true};}
  });
}
$('site-signin').addEventListener('click', () => accountAction(async () => {
  state.siteAuth = await api(base()+'/session/signin', {}); renderAccount();
}));
$('account-go').addEventListener('click', () => accountAction(async () => {
  state.account = await api(base()+'/account', {}); state.accountReceivedAt = Date.now(); state.lastAccountRefreshAt = Date.now(); state.accountOffline = false; renderAccount();
}));
for (const [id, action] of [['open-folder','open'], ['change-folder','choose']]) {
  $(id).addEventListener('click', () => accountAction(async () => { await api('/api/v1/location', {action}); }));
}
$('account-speed').addEventListener('change', event => {
  const speed = event.target.value;
  accountAction(async () => { state.account = await api(base()+'/account/speed', {speed}); state.accountReceivedAt = Date.now(); state.lastAccountRefreshAt = Date.now(); renderAccount(); });
});
$('account-mobile-data').addEventListener('change', event => {
  const allowed = event.target.checked;
  if (state.busy) { renderAccount(); return; }  // Another action is running; show the saved choice again.
  state.networkSaving = true;
  accountAction(async () => {
    try { await api(base()+'/network', {wifi_only:!allowed}); await network(); }
    finally { state.networkSaving = false; renderAccount(); }
  });
});
$('account-allow-network').addEventListener('click', () => accountAction(async () => {
  await api(base()+'/network', {wifi_only:false}); await network(); renderAccount();
}));
$('account-pause').addEventListener('click', () => accountAction(async () => {
  state.account = await api(base()+'/account/pause', {}); await refresh(); renderAccount();
}));
$('workspace').addEventListener('change', () => action(() => select($('workspace').value)));
$('create').addEventListener('click', () => action(async () => { const value = await api('/api/v1/workspaces', {}); await load(value.workspace_id); }));
$('search').addEventListener('input', renderSeries);
$('watch').addEventListener('click', () => action(() => queue('sync', { refresh_lists: false })));
 $('save-priority').addEventListener('click', () => action(async () => {
  const value=await api(base()+'/priority',{priority:$('priority').value});
  state.task=value.task_id;state.taskKind='settings';state.taskInfo=null;
  notice('Queue order change queued.');
 }));
$('discover').addEventListener('click', () => action(() => queue('sync')));
$('archive').addEventListener('click', () => action(() => queue('archive_discovered')));
$('stop').addEventListener('click', () => action(async () => {
  const result = await api(base()+'/stop', { run_id: state.status.last_run.id });
  notice(result.status === 'cancellation_requested' ? 'Stop requested. Completed archives and cached images will be retained.' : 'That batch has already stopped. Saved archives are retained.'); await refresh();
}));
$('stop-queued').addEventListener('click', () => action(async () => {
  const result = await api(base()+'/tasks/'+state.task, { action: 'stop_queued' });
  if (result.status === 'cancellation_requested') {
    state.taskInfo = { status: 'queued', cancellation_requested: true };
    notice('Queued stop requested. The importer will skip this batch and keep saved archives.');
  } else notice(result.status === 'already_started' ? 'The batch has started. Use Stop current batch to pause it.' : 'That batch has already finished. Saved archives are retained.');
  await refresh();
}));
$('check').addEventListener('click', () => action(async () => {
  const workspace = state.workspace;
  state.connectionCheck = { message: 'Checking your Tapas connection…', pending: true }; renderConnectionCheck();
  try {
    const value = await api(base()+'/session/check', {});
    if (workspace !== state.workspace) return;
    state.task = value.task_id; state.taskKind = 'session'; state.taskInfo = { status: 'queued' };
    state.connectionCheck = { message: 'Connection check queued. Waiting for the downloader; the result will appear here.', pending: true }; renderConnectionCheck();
    notice('Checking your Tapas session…');
  } catch (error) {
    if (workspace === state.workspace) {
      state.connectionCheck = { message: error.message, error: true, pending: false }; renderConnectionCheck();
    }
    throw error;
  }
}));
$('schedule').addEventListener('submit', event => { event.preventDefault(); action(async () => { const value = await api(base()+'/daily', { enabled: $('enabled').checked, at: $('at').value, timezone: $('zone').value }); state.task = value.task_id; state.taskKind = 'schedule'; notice('Schedule update queued. Daily preservation checks explicitly free episodes.'); }); });
async function librarySummary(refresh = false) {
  if (!state.workspace) return;
  state.libraryRefreshedAt = Date.now();
  const workspace = state.workspace;
  try {
    const value = await api(base()+'/saved-files?summary=1'+(refresh ? '&refresh=1' : ''));
    if (workspace !== state.workspace) return;
    $('metrics').replaceChildren(); $('library-locations').replaceChildren();
    state.locations = value.locations; renderStorage();
    const device = value.locations.find(g => g.id === 'device');
    const cloudCopies = value.locations.filter(g => g.id !== 'device').reduce((n,g) => n+number(g.episodes), 0);
    $('metrics').classList.toggle('desktop-metrics', !cloudAvailable());
    const metrics = !cloudAvailable() ? [['Saved episodes', device?.episodes], ['Saved files', device?.files]] : [['Saved episodes', value.episode_count], ['Saved files', value.file_count], ['Episodes on this device', device?.episodes], ['Cloud episode copies', cloudCopies]];
    for (const [label, count] of metrics) {
      if (label === 'Episodes on this device' && state.context.interface_mode !== 'local') continue;
      const cell = document.createElement('div'); cell.className = 'metric'; const number = document.createElement('strong');
      number.textContent = Number.isInteger(count) ? display(count) : '—'; cell.append(number, document.createTextNode(label)); $('metrics').append(cell);
    }
    for (const group of value.locations.filter(g => cloudAvailable() || g.id === 'device')) {
      const link = document.createElement('a'); link.className = 'location-card'; link.href = '/library#'+workspace+'/'+group.id;
      const title = document.createElement('strong'); title.textContent = group.label;
      const counts = document.createElement('span'); counts.textContent = `${display(group.episodes)} episodes · ${display(group.files)} files${!group.connected && !group.files ? ' · Not connected' : ''}`;
      link.append(title, counts); $('library-locations').append(link);
    }
    $('library-summary').textContent = !cloudAvailable() ? `${display(device?.episodes)} saved episodes on this computer. View saved files for the complete list.` : `${display(value.episode_count)} saved episodes. View all files, or choose a storage location below.`;
  } catch (error) { if (workspace === state.workspace) $('library-summary').textContent = 'Saved file counts are unavailable. Open View saved files to retry.'; }
}
$('downloads').addEventListener('click', () => {window.location.href = '/library#'+state.workspace+'/'+(cloudAvailable() ? 'all' : 'device');});
$('audit').addEventListener('click', () => action(async () => {
  const workspace = state.workspace;
  $('audit-status').textContent = 'Verifying saved images and reconciling the recorded inventory…';
  try {
    const report = await api(base()+'/audit');
    if (workspace !== state.workspace) return;
    if (state.auditUrl) URL.revokeObjectURL(state.auditUrl);
    state.auditUrl = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)+'\n'], { type: 'application/json' }));
    $('audit-download').href = state.auditUrl; $('audit-download').textContent = 'Download JSON report ('+report.audited_at+')'; $('audit-download').hidden = false;
    $('audit-status').textContent = `${display(report.verified_inventory_episode_count)} inventoried episodes verified. ${display(report.missing_recorded_accessible_episode_count)} recorded accessible episodes still need verified files. ${report.archive_integrity_passed ? 'Saved file integrity passed.' : 'Some files or metadata require repair; see the report.'} Account coverage remains unverified. Audited at ${report.audited_at}.`;
  } catch (error) { $('audit-status').textContent = error.message; }
}));
action(load);
$('unlocked-only').addEventListener('change', renderSeries);
setInterval(poll, 5000);
setInterval(renderAccount, 1000);
