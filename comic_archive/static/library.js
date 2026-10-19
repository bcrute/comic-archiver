'use strict';
// The launch address's secret has done its work once the page is here; it is not kept in the address bar or history.
if (location.search.startsWith('?launch=')) history.replaceState(null, '', location.pathname);
const $ = id => document.getElementById(id);
const state = {workspace:null, desktopOnly:true, location:'device', offset:0, total:0, busy:false, request:0, locations:[], cloud:false};
const count = n => Number.isInteger(n) && n >= 0 ? n.toLocaleString() : '0';
const sizes = n => {
  if (!Number.isFinite(n) || n < 0) return '—';
  // Counted in 1024s, as Windows shows file sizes.
  const units = ['B','KB','MB','GB','TB']; let index = 0;
  while (n >= 1024 && index < units.length-1) {n /= 1024; index++;}
  return `${index ? n.toFixed(1) : n} ${units[index]}`;
};
async function api(path, body, failed='This file could not be opened. Refresh files and try again.') {
  const options = {credentials:'same-origin', cache:'no-store'};
  if (body !== undefined) {
    const context = await api('/api/v1/context');
    options.method = 'POST'; options.headers = {'Content-Type':'application/json', 'X-CSRF-Token':context.csrf_token};
    options.body = JSON.stringify(body);
  }
  let response;
  try {response = await fetch(path, options);} catch {throw new Error('Connection lost. Try again.');}
  let result;
  try {result = await response.json();} catch {throw new Error('The app returned an unexpected response. Reload this page and try again.');}
  if (!response.ok) {
    throw new Error(response.status === 401 ? 'Your session expired. Close and reopen the app, or open the full dashboard address again.'
      : body !== undefined ? result.code === 'device_file_open_unavailable'
        ? 'No app could open this file. Choose a default app for this file type on your device, then try again.'
        : failed
      : 'Saved files could not be loaded. Try Refresh files.');
  }
  return result;
}
async function openFile(button, feedback, url) {
  button.disabled = true; button.textContent = 'Opening…'; button.setAttribute('aria-busy','true');
  feedback.textContent = 'Opening in your device’s default app…'; feedback.classList.remove('error');
  try {
    await api(url, {action:'open'});
    feedback.textContent = 'Sent to your device’s default app.';
  } catch (error) {
    feedback.textContent = error.message;
    feedback.classList.add('error');
  } finally {
    button.disabled = false; button.textContent = 'Open'; button.setAttribute('aria-busy','false');
  }
}
function notice(message, error=false) { $('notice').textContent = message; $('notice').classList.toggle('error', error); }
function buttons() {
  for (const id of ['refresh-files','search-files','clear-search','file-search','file-kind','file-sort','page-size','file-page','go-page']) $(id).disabled = state.busy || !state.workspace;
  $('previous-page').disabled = state.busy || state.offset === 0;
  $('next-page').disabled = state.busy || state.offset + Number($('page-size').value) >= state.total;
  for (const button of $('library-locations').querySelectorAll('button')) button.disabled = state.busy;
  $('saved-files').setAttribute('aria-busy', String(state.busy));
}
function hash() {
  const filters = new URLSearchParams({q:$('file-search').value.trim(), kind:$('file-kind').value,
    sort:$('file-sort').value, limit:$('page-size').value, offset:String(state.offset)});
  history.replaceState(null, '', '#'+state.workspace+'/'+state.location+'?'+filters);
}
function locations(value) {
  state.locations = value.locations;
  $('library-locations').replaceChildren();
  const groups = state.desktopOnly ? value.locations.filter(g => g.id === 'device') : [{id:'all', label:'All storage', files:value.file_count, episodes:value.episode_count}, ...value.locations];
  for (const group of groups) {
    const button = document.createElement('button'); button.type = 'button'; button.className = 'location-card secondary';
    button.dataset.location = group.id; button.setAttribute('aria-pressed', String(group.id === state.location));
    const title = document.createElement('strong'); title.textContent = group.label;
    const description = document.createElement('span'); description.textContent = `${count(group.files)} files · ${count(group.episodes)} episodes${group.id !== 'all' && !group.connected && !group.files ? ' · Not connected' : ''}`;
    button.append(title, description); button.addEventListener('click', () => {state.location = group.id; state.offset = 0; hash(); loadFiles();});
    $('library-locations').append(button);
  }
  const device = value.locations.find(g => g.id === 'device');
  // Where nothing is in cloud storage, every file is on this computer: there is nothing to tell apart.
  state.cloud = !state.desktopOnly && value.locations.some(g => g.id !== 'device' && g.files > 0);
  $('storage-heading').hidden = !state.cloud;
  $('library-summary').textContent = state.desktopOnly ? `${count(device?.episodes)} saved episodes across ${count(device?.files)} files on this computer.` : `${count(value.episode_count)} saved episodes across ${count(value.file_count)} files${state.cloud ? ' and cloud copies' : ''}.`;
}
function rows(value) {
  $('files').replaceChildren();
  const types = {cbz:'Comic', epub:'Novel', 'tar.zst':'Compressed comic', episode_metadata:'Data file', series_metadata:'Data file', cover:'Cover'};
  for (const file of value.files) {
    const row = document.createElement('tr');
    const storage = state.locations.find(g => g.id === file.location)?.label || 'Unknown storage';
    for (const content of [file.series, file.title || '—', file.filename, types[file.kind] || '—', sizes(file.size)]) {
      const cell = document.createElement('td'); cell.textContent = content; row.append(cell);
    }
    const cell = document.createElement('td');
    const saved = new Date(file.saved_at);
    if (file.saved_at && !Number.isNaN(saved.getTime())) {
      const stamp = document.createElement('time'); stamp.dateTime = file.saved_at;
      stamp.textContent = saved.toLocaleString(); stamp.title = saved.toISOString(); cell.append(stamp);
      if (file.date_source === 'file_modified') {const note = document.createElement('small'); note.textContent = 'File time'; cell.append(note);}
    } else cell.textContent = '—';
    row.append(cell);
    const destination = document.createElement('td'); destination.textContent = storage; destination.hidden = !state.cloud; row.append(destination);
    const action = document.createElement('td');
    // Only comics and novels are offered to open: those are what a reading app opens.
    const opens = ['cbz','epub'].includes(file.kind);
    if (file.location === 'device' && opens && typeof file.id === 'string' && /^[a-f0-9]{64}$/.test(file.id)) {
      const url = '/api/v1/workspaces/'+state.workspace+'/exports/'+file.id;
      const button = document.createElement('button'); button.type = 'button'; button.className = 'secondary file-action';
      button.textContent = 'Open'; button.setAttribute('aria-label', 'Open '+file.filename);
      const feedback = document.createElement('small'); feedback.className = 'file-action-status'; feedback.setAttribute('role','status');
      button.addEventListener('click', () => openFile(button, feedback, url)); action.append(button, feedback);
    } else if (file.location !== 'device') action.textContent = 'Cloud copy';
    row.append(action); $('files').append(row);
  }
}
async function loadFiles(refresh=false) {
  if (!state.workspace) return;
  hash();
  const request = ++state.request; state.busy = true; buttons(); notice('Loading saved files…');
  const parameters = new URLSearchParams({location:state.location, q:$('file-search').value.trim(), kind:$('file-kind').value,
    sort:$('file-sort').value, offset:String(state.offset), limit:$('page-size').value, refresh:refresh ? '1':'0'});
  try {
    const value = await api('/api/v1/workspaces/'+state.workspace+'/saved-files?'+parameters);
    if (request !== state.request) return;
    state.offset = value.offset; state.total = value.total; hash(); locations(value); rows(value);
    const pages = Math.max(1, Math.ceil(value.total / value.limit));
    $('file-page').value = String(Math.floor(value.offset / value.limit)+1); $('file-page').max = String(pages);
    $('page-count').textContent = 'of '+count(pages);
    // With nothing saved yet the notice says so; that no file matches would say it twice.
    $('file-range').textContent = value.total ? `Showing ${count(value.offset+1)}–${count(value.offset+value.files.length)} of ${count(value.total)} files.` : value.file_count ? 'No saved files match these filters.' : '';
    notice(value.total ? 'Saved files loaded.' : value.file_count ? 'Try another location or clear the search.' : `No files have been saved yet. Use ${$('save-nav').textContent} to start.`);
  } catch (error) {
    if (request === state.request) {state.total = 0; $('files').replaceChildren(); $('file-range').textContent = ''; notice(error.message, true);}
  } finally {if (request === state.request) {state.busy = false; buttons();}}
}
$('file-filters').addEventListener('submit', event => {event.preventDefault(); state.offset = 0; loadFiles();});
for (const id of ['file-kind','file-sort','page-size']) $(id).addEventListener('change', () => {state.offset = 0; loadFiles();});
$('clear-search').addEventListener('click', () => {$('file-search').value = ''; $('file-kind').value = 'episodes'; state.offset = 0; loadFiles();});
$('previous-page').addEventListener('click', () => {state.offset -= Number($('page-size').value); loadFiles();});
$('next-page').addEventListener('click', () => {state.offset += Number($('page-size').value); loadFiles();});
$('page-jump').addEventListener('submit', event => {event.preventDefault(); if (!$('page-jump').reportValidity()) return; state.offset = (Number($('file-page').value)-1)*Number($('page-size').value); loadFiles();});
$('refresh-files').addEventListener('click', () => loadFiles(true));
$('open-folder').addEventListener('click', async () => {
  // The app's own window opens its save folder; no path comes from this page.
  $('open-folder').disabled = true;
  try {
    await api('/api/v1/location', {action:'open'}, 'The folder could not be opened. Try again.');
    notice('Your save folder opens in its own window.');
  } catch (error) {notice(error.message, true);}
  finally {$('open-folder').disabled = false;}
});
let keepsTheme = false;
document.addEventListener('comic-archive-theme', async event => {
  // Only the desktop app keeps the choice; in a browser the page's own storage does.
  if (!keepsTheme) return;
  try { await api('/api/v1/theme', {theme:event.detail}); } catch { /* The page already shows the choice; it lasts until the app closes. */ }
});
async function openLibrary() {
  try {
    const context = await api('/api/v1/context');
    keepsTheme = typeof context.theme === 'string';
    // Each site has a library of its own; pages without a list of sites are Tapas's.
    const site = (Array.isArray(context.sites) ? context.sites : []).find(item => item.current === true)?.name || 'Tapas';
    $('library-title').textContent = `Your saved ${site} library`;
    $('open-folder').hidden = typeof context.library_folder !== 'string';
    // Only this computer's files where cloud saving is not offered: a computer the app has no helper for.
    state.desktopOnly = context.cloud_offered === false;
    const [workspace, location, filters] = locationHash();
    state.workspace = context.workspace_ids.includes(workspace) ? workspace : context.workspace_ids[0];
    if (!state.workspace) {notice('This library is unavailable. Close and reopen the app.', true); buttons(); return;}
    state.location = state.desktopOnly ? 'device' : /^(all|device|b2|b2-[a-f0-9]{16})$/.test(location || '') ? location : 'all';
    $('file-search').value = (filters.get('q') || '').slice(0,200);
    $('file-kind').value = ['all','episodes','metadata','covers'].includes(filters.get('kind')) ? filters.get('kind') : 'episodes';
    $('file-sort').value = ['saved_desc','saved_asc','series'].includes(filters.get('sort')) ? filters.get('sort') : 'saved_desc';
    $('page-size').value = ['50','100','200'].includes(filters.get('limit')) ? filters.get('limit') : '50';
    const offset = filters.get('offset') || '0';
    state.offset = /^[0-9]{1,8}$/.test(offset) && Number(offset) <= 10000000 ? Number(offset) : 0;
    hash(); await loadFiles();
  } catch (error) {notice(error.message, true);}
}
openLibrary();
function locationHash() {
  const [path, query=''] = window.location.hash.slice(1).split('?');
  const [workspace, location] = path.split('/');
  return [workspace, location, new URLSearchParams(query)];
}
