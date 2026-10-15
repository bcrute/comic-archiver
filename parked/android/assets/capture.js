(async () => {
  const id = '__EPISODE_ID__';
  let stage = 'identity';
  const fail = code => { throw {safeCode: code}; };
  const positive = value => Number.isSafeInteger(value) && value > 0;
  const text = value => typeof value === 'string' && value.trim().length > 0 && value.length <= 1000;
  async function get(path, limit, json = true) {
    const reply = await fetch(path, {method: 'GET', credentials: 'include', redirect: 'error'});
    if (reply.status !== 200) fail('http_' + reply.status);
    const body = await reply.text();
    if (body.length > limit) fail('response_too_large');
    if (!json) return body;
    const parsed = JSON.parse(body);
    if (!parsed || parsed.error_details != null || !parsed.data) fail('response_unverified');
    return parsed.data;
  }
  window.__comicArchiveResult = {state: 'pending'};
  try {
    const identity = await get('/who-am-i', 262144);
    if (!identity.user || !positive(identity.user.id)) fail('login_required');
    stage = 'episode_metadata';
    const episode = await get('/episode/' + id + '/info', 2097152);
    if (!positive(episode.id) || String(episode.id) !== id) fail('episode_mismatch');
    const flags = {};
    for (const key of ['must_pay', 'scheduled', 'book', 'free', 'unlocked']) {
      if (typeof episode[key] !== 'boolean') fail('authorization_unknown');
      flags[key] = episode[key];
    }
    if (!Object.prototype.hasOwnProperty.call(episode, 'closing_date')) fail('authorization_unknown');
    flags.closing_date = episode.closing_date;
    if (flags.must_pay || (!flags.free && !flags.unlocked)) fail('access_denied');
    if (flags.scheduled || flags.book || flags.closing_date !== null) fail('access_unsupported');
    if (!positive(episode.scene) || episode.scene > 2147483647 || !text(episode.title)) fail('order_or_title_unverified');
    // Authorization is established BEFORE the reader or any image is requested.
    stage = 'reader_html';
    const html = await get('/episode/' + id, 8388608, false);
    const document = new DOMParser().parseFromString(html, 'text/html');
    const readers = document.querySelectorAll('#episode-' + id);
    if (readers.length !== 1) fail('reader_scope_unverified');
    const images = [...readers[0].querySelectorAll('img.content__img')];
    if (images.length < 1 || images.length > 500) fail('reader_manifest_incomplete');
    const pages = images.map(image => {
      const source = image.getAttribute('data-src') || image.getAttribute('src');
      if (!source) fail('reader_manifest_incomplete');
      const url = new URL(source, 'https://tapas.io');
      if (url.protocol !== 'https:' || url.username || url.password || url.port || url.hash ||
          !(url.hostname === 'tapas.io' || url.hostname.endsWith('.tapas.io')) ||
          !(url.pathname.startsWith('/pc/') || url.pathname.startsWith('/c/'))) fail('image_boundary_unverified');
      return url.href;
    });
    const paths = pages.map(value => {const url = new URL(value); return url.hostname + url.pathname;});
    if (new Set(paths).size !== paths.length) fail('reader_manifest_duplicates');
    const seriesIds = new Set();
    for (const script of document.querySelectorAll('script:not([src])')) {
      for (const match of script.textContent.matchAll(/\b(?:var|let|const)\s+seriesId\s*=\s*([1-9][0-9]{0,19})\s*;/g)) seriesIds.add(match[1]);
    }
    if (seriesIds.size !== 1) fail('series_id_unverified');
    const seriesId = [...seriesIds][0];
    stage = 'series_metadata';
    const series = await get('/series/' + seriesId, 2097152);
    if (!positive(series.id) || String(series.id) !== seriesId || series.comic !== true || series.book !== false ||
        !text(series.title) || typeof series.url !== 'string' || !/^[A-Za-z0-9_-]{1,100}$/.test(series.url)) fail('series_unverified');
    window.__comicArchiveResult = {state: 'ready', stage, identity: String(identity.user.id), episode_id: id,
      order: episode.scene, title: episode.title, flags, pages,
      series: {id: seriesId, title: series.title, url: series.url}};
  } catch (error) {
    // Never expose HTTP bodies, signed URLs or arbitrary exception messages.
    window.__comicArchiveResult = {state: 'held', stage,
      code: error && /^([a-z_]+|http_[0-9]{3})$/.test(error.safeCode) ? error.safeCode : 'request_failed'};
  }
})();
