"""Read-only authenticated Tapas GET flow observed in the normal web reader."""
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from ..sites import DEFAULT
from ..models import ArchiveError, Episode, Manifest, Page, Series, canonical_url
from ..session import load_session
from ..transport import Transport, asset_url
from .reader import ReaderHTML, ordered_sources, safe_primitives, json_shape
from ..errors import AccessDenied, AuthenticationError, MetadataError, UnsupportedContent
from ..expiry import observed_expiration
from .pagination import EpisodeCountMismatch


def unsupported(message, code):
    error = UnsupportedContent(message)
    error.code = code
    return error


def access_state(info):
    """Bulk discovery hints only; the normal reader decides actual access."""
    if info.get('free') is True:
        return 'tapas_free'
    if info.get('unlocked') is True:
        return 'tapas_limited' if info.get('closing_date') is not None else 'tapas_unlocked'
    return 'unavailable_or_unverified'


def checked_series_seed(key, title):
    from ..discovery import observed_id
    if not observed_id(key) or not isinstance(title, str) or not title.strip() or len(title) > 500:
        raise MetadataError('Series fallback needs an observed numeric identity and bounded title.')
    return title


def title_of(info):
    """The source's title text; a missing or non-text title is a metadata change."""
    value = info.get("title")
    if not isinstance(value, str):
        raise MetadataError("Tapas metadata has no usable title; inspection is required.")
    return value


def published_at(value):
    """A short ISO-8601 publish time as sent; anything else is dropped."""
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None
    return value


class TapasHTTPAdapter:
    site = DEFAULT
    supports_series_seed = True
    supports_novels = True
    supports_expiring_access = True
    def __init__(self, session: Path | None = None, transport=None, interval=1.0, expected_account_id=None):
        if transport is None:
            state = load_session(session)
            from ..network import NetworkGuard
            transport = Transport(state["cookies"], interval=interval,
                                  network_guard=NetworkGuard(Path(session).absolute().parent / 'Library'))
        self.transport = transport
        self.episode_info = None
        self.stage = "session"
        self.expected_account_id = expected_account_id
        self.verified_slugs = {}
        self.series_seeds = {}
        self.unavailable_headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.transport.close()

    def json_get(self, path):
        data = self.transport.get(f"https://tapas.io{path}")
        if len(data) > 2 * 1024 * 1024:
            raise ArchiveError("Tapas metadata exceeds the supported size.")
        try:
            response = json.loads(data)
            result = response["data"]
            if not isinstance(result, dict) or response.get("error_details") is not None:
                raise ValueError
            return result
        except (ValueError, KeyError, TypeError):
            raise MetadataError("Tapas returned unexpected metadata; reopen the normal browser session.") from None

    def check_session(self):
        self._session_structure={}
        data=self.json_get("/who-am-i")
        user = data.get("user")
        if not isinstance(user, dict) or type(user.get("id")) is not int or user["id"] < 1:
            raise AuthenticationError("Tapas does not report an authenticated account; run login again.")
        if self.expected_account_id is not None and str(user["id"]) != self.expected_account_id:
            raise AuthenticationError("The signed-in account changed; this job has stopped.")
        self._account_id = str(user["id"])
        # Shallow schema only: no scalar values or nested account-derived keys.
        self._session_structure={'fields':json_shape(user,depth=3),
                                 'field_count':len(user),'truncated':len(user)>40,
                                 'data_fields':json_shape(data,depth=3),'data_truncated':len(data)>40}
        # Account identifiers/names remain in memory only.
        return True

    def account_identity(self):
        self.check_session()
        return self._account_id

    def episode_access(self, url):
        url = canonical_url(url)
        match = re.fullmatch(r"https://tapas\.io/episode/([0-9]+)", url or "")
        if match is None:
            raise ArchiveError("Use an exact https://tapas.io/episode/<numeric-id> URL.")
        self.check_session()
        try:
            info = self.json_get(f"/episode/{match[1]}/info")
        except ArchiveError as error:
            if type(getattr(error, 'source_http_status', None)) is not int or error.source_http_status not in {404, 410}:
                raise
            info, _ = self._reader_document(url, match[1], error.source_http_status)
        if type(info.get("id")) is not int or str(info["id"]) != match[1]:
            raise MetadataError("Episode metadata identifies a different episode.")
        access=access_state(info)
        return {"source_episode_id": match[1], "access_state": access,
                'access_expires_at':observed_expiration(info.get('closing_date'))}

    def _reader_missing(self, key, error, metadata_status=None):
        status = getattr(error, 'source_http_status', None)
        if (type(status) is not int or status not in {404, 410}
                or getattr(error, 'source_response_kind', None) == 'challenge'):
            raise error
        logging.getLogger('comic_archive.download').info(
            'reader access: provider=tapas episode=%s result=unavailable stage=reader '
            'http_status=%s metadata_http_status=%s reason=reader_missing retry=next_scan_round_or_changed_hint',
            key, status, metadata_status)
        unavailable = AccessDenied('Tapas does not currently provide this episode reader; other episodes can continue.')
        unavailable.source_http_status = status
        raise unavailable from None

    def _reader_document(self, url, key, metadata_status):
        """The observed website JSON reader supplies both metadata and HTML."""
        self.stage = 'reader_document'
        logging.getLogger('comic_archive.download').info(
            'tapas metadata: episode=%s stage=episode_info http_status=%s fallback=website_reader_document',
            key, metadata_status)
        try:
            raw = self.transport.get(url, max_bytes=8 * 1024 * 1024, reader_json=True)
        except ArchiveError as error:
            self._reader_missing(key, error, metadata_status)
        if len(raw) > 8 * 1024 * 1024:
            raise MetadataError('Tapas reader document exceeds the supported size.')
        try:
            response = json.loads(raw)
            data = response['data']
            if (not isinstance(response, dict) or type(response.get('code', 200)) is not int
                    or response.get('code', 200) != 200 or response.get('error_details') is not None
                    or response.get('errorDetails') is not None or not isinstance(data, dict)
                    or not isinstance(data.get('episode'), dict) or not isinstance(data.get('html'), str)
                    or type(data['episode'].get('id')) is not int or str(data['episode']['id']) != key):
                raise ValueError
            html = data['html'].encode('utf-8')
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise MetadataError('Tapas reader document does not identify the exact requested episode.') from None
        return data['episode'], html

    def seed_series(self, key, title):
        """Account-bound inventory metadata supplies a fallback, never episode access."""
        self.series_seeds[key] = checked_series_seed(key, title)
        self.unavailable_headers.pop(key, None)

    def _series_header(self, key):
        if key in self.unavailable_headers and key in self.series_seeds:
            return {'id': int(key), 'title': self.series_seeds[key], '_header_unavailable': True}
        try:
            return dict(self.resolve_series(key), _header_unavailable=False)
        except ArchiveError as error:
            if getattr(error, 'source_http_status', None) not in {404, 410} or key not in self.series_seeds:
                raise
            self.unavailable_headers[key] = error.source_http_status
            logging.getLogger('comic_archive.download').info(
                'tapas metadata: series=%s stage=series_header http_status=%s fallback=account_observation',
                key, error.source_http_status)
            return {'id': int(key), 'title': self.series_seeds[key], '_header_unavailable': True}

    def series_identity(self, key):
        self.check_session()
        series = self._series_header(key)
        return {'source_series_id':str(series['id']),'title':title_of(series)}

    def series_metadata(self,key):
        from .series_metadata import series_details
        self.check_session()
        info=self._series_header(key)
        if info.get('_header_unavailable') is True:
            raise MetadataError('The series header is unavailable; optional metadata enrichment is deferred.')
        if (any(type(info.get(flag)) is not bool for flag in ('comic', 'book'))
                or (info['comic'], info['book']) not in {(True, False), (False, True)}):
            raise UnsupportedContent('Series content classification is unsupported.')
        slug=info.get('url')
        if not isinstance(slug,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',slug):
            raise MetadataError('Series metadata has no verified public slug.')
        url=f'https://tapas.io/series/{slug}/info'
        raw=self.transport.get(url)
        if len(raw)>8*1024*1024:
            raise MetadataError('Series metadata HTML exceeds the supported size.')
        try:
            details=series_details(raw.decode('utf-8'),info['id'])
        except UnicodeError:
            raise MetadataError('Series metadata HTML has an unsupported encoding.') from None
        cover=info.get('thumb_url')
        if cover is not None:
            if not isinstance(cover,str):
                raise MetadataError('Series cover has an unexpected value.')
            asset_url(cover)
        return Series(slug,title_of(info),source_url=url,platform_id=str(info['id']),slug=slug,
                      cover_url=cover,**details)

    def retrieve_cover(self,series,max_bytes=None):
        if not series.cover_url:
            return None
        return self.transport.get(asset_url(series.cover_url),max_bytes=min(max_bytes,10*1024*1024) if max_bytes is not None else 10*1024*1024)

    def episode_url(self,key):
        if not isinstance(key,str) or not re.fullmatch(r'[1-9][0-9]{0,19}',key):
            raise MetadataError('Use an observed numeric episode identity.')
        return f'https://tapas.io/episode/{key}'

    def resolve_series(self, key):
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", key):
            raise ArchiveError("Use the series's numeric ID or exact public slug.")
        if key.isdigit():
            platform_id = int(key)
        else:
            html = self.transport.get(f"https://tapas.io/series/{key}/info")
            if len(html) > 8 * 1024 * 1024:
                raise MetadataError("Series page exceeds the supported size.")
            try:
                html = html.decode("utf-8")
            except UnicodeError:
                raise MetadataError("Series page has an unsupported encoding.") from None
            parser = ReaderHTML("unused")
            parser.feed(html)
            values = safe_primitives("\n".join(parser.inline))
            platform_id = next((values[name] for name in ("seriesId", "seriesID", "series_id") if name in values), None)
            if platform_id is None and len(parser.series_ids) == 1:
                platform_id = next(iter(parser.series_ids))
        if type(platform_id) is not int or platform_id < 1:
            raise ArchiveError("Series page does not expose a verified numeric series ID.")
        info = self.json_get(f"/series/{platform_id}")
        if type(info.get("id")) is not int or info["id"] != platform_id:
            raise ArchiveError("Tapas returned inconsistent series metadata.")
        if not key.isdigit() and info.get("url") != key:
            raise ArchiveError("Series metadata does not match the requested slug.")
        slug = info.get("url")
        if isinstance(slug, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", slug):
            self.verified_slugs[slug] = str(platform_id)
        return info

    def enumerate_episodes(self, platform_id, strict_order=True):
        if type(platform_id) is not int or platform_id < 1:
            raise ArchiveError("Episode enumeration needs an observed numeric series ID.")
        episodes = {}
        page = 1
        total = None
        for _ in range(500):
            query = urlencode({"page": page, "max_limit": 20})
            data = self.json_get(f"/series/{platform_id}/episodes?{query}")
            pagination = data.get("pagination")
            items = data.get("episodes")
            if not isinstance(pagination, dict) or not isinstance(items, list):
                raise ArchiveError("Tapas episode pagination has an unexpected structure.")
            count = pagination.get("total")
            if type(count) is not int or count < 0 or (total is not None and count != total):
                raise ArchiveError("Series episode count changed during enumeration; try again.")
            total = count
            for item in items:
                if not isinstance(item, dict) or type(item.get("id")) is not int or item["id"] < 1:
                    raise ArchiveError("Tapas episode list contains an invalid episode ID.")
                if item["id"] in episodes:
                    raise ArchiveError("Tapas pagination repeated an episode; completeness is unverified.")
                episodes[item["id"]] = item
            if pagination.get("has_next") is False:
                if len(episodes) != total:
                    error=EpisodeCountMismatch(platform_id,len(episodes),total)
                    error.partial_episode_summaries=self._episode_summaries(list(episodes.values()))
                    raise error
                result = list(episodes.values())
                if any(type(item.get("scene")) is not int or item["scene"] < 1 for item in result) or len({item["scene"] for item in result}) != len(result):
                    if strict_order:
                        raise ArchiveError("Episode orders are invalid or ambiguous.")
                    return result
                return sorted(result, key=lambda item: item["scene"])
            next_page = pagination.get("page")
            if pagination.get("has_next") is not True or not items or type(next_page) is not int or next_page <= page:
                raise ArchiveError("Tapas returned an invalid next-page cursor.")
            page = next_page
        raise ArchiveError("Series pagination exceeds the conservative 500-page limit.")

    def inspect_series(self, key, strict_order=True, comic_only=False):
        if type(strict_order) is not bool or type(comic_only) is not bool:
            raise ArchiveError("Episode ordering validation must be an explicit boolean.")
        self.check_session()
        series = self._series_header(key)
        title = title_of(series)
        if comic_only and series.get('book') is True and series.get('comic') is False:
            return {'authenticated':True,'complete':False,'source_series_id':str(series['id']),
                    'title':title,'episodes':[], 'episode_enumeration_skipped':True,
                    'unsupported_series_reason':'novel','series_content_flags':{'book':True,'comic':False}}
        try:
            episodes = self.enumerate_episodes(series["id"], strict_order=strict_order)
        except EpisodeCountMismatch as error:
            summaries=getattr(error,'partial_episode_summaries',None)
            if isinstance(summaries,list):
                error.partial_inspection={'complete':False,'authenticated':True,
                    'source_series_id':str(series['id']),'title':title,
                    'episode_count':len(summaries),'episodes':summaries}
            raise
        except ArchiveError as error:
            status = getattr(error, 'source_http_status', None)
            if series.get('_header_unavailable') is not True or status not in {404, 410}:
                raise
            unavailable = MetadataError('The observed series header and episode list are unavailable.')
            unavailable.source_http_status = status
            unavailable.source_series_id = str(series['id'])
            unavailable.source_route = f"/series/{series['id']}/episodes"
            logging.getLogger('comic_archive.download').info(
                'tapas metadata: series=%s stage=episode_list http_status=%s fallback=unavailable', series['id'], status)
            raise unavailable from None
        summaries=self._episode_summaries(episodes)
        return {"complete": True, "authenticated": True, "source_series_id": str(series["id"]),
                "title": title, "episode_count": len(summaries),
                "ordering_verified": all(item["order"] is not None for item in summaries),
                "accessible_episode_count": sum(item["access_state"] != "unavailable_or_unverified" for item in summaries),
                "series_content_flags": {key:series[key] for key in ('book','comic') if type(series.get(key)) is bool},
                "episodes": summaries}

    def _episode_summaries(self,episodes):
        scenes = [item.get("scene") for item in episodes]
        summaries = []
        for item in episodes:
            reason=None
            access = access_state(item)
            scene = item.get("scene")
            order = scene if type(scene) is int and scene > 0 and scenes.count(scene) == 1 else None
            summaries.append({"id": str(item["id"]), "title": title_of(item),
                              "order": order, "source_scene": scene if type(scene) is int else None, "access_state": access,
                              'authorization_reason':reason,
                              'access_expires_at':observed_expiration(item.get('closing_date')),
                              "source_url": f"https://tapas.io/episode/{item['id']}"})
        return summaries

    def capture(self, url, series_id=None, series_title=None, title=None, order=None):
        url = canonical_url(url)
        match = re.fullmatch(r"https://tapas\.io/episode/([0-9]+)", url or "")
        if not match:
            raise ArchiveError("Use an exact https://tapas.io/episode/<numeric-id> URL.")
        # The sign-in is checked when the step starts (account_identity), not before each episode: every request
        # to tapas.io counts toward the pace Tapas allows, and a refusal says so itself.
        self.stage = "episode_metadata"
        reader_html = None
        try:
            info = self.json_get(f"/episode/{match[1]}/info")
        except ArchiveError as error:
            if type(getattr(error, 'source_http_status', None)) is not int or error.source_http_status not in {404, 410}:
                raise
            info, reader_html = self._reader_document(url, match[1], error.source_http_status)
        if type(info.get("id")) is not int or str(info["id"]) != match[1]:
            raise ArchiveError("Tapas episode metadata identifies a different episode.")
        access = access_state(info)
        if type(info.get('book')) is not bool:
            raise MetadataError('Episode content type is missing or unsupported.')
        if order is None and (type(info.get("scene")) is not int or info["scene"] < 1):
            raise unsupported("Episode order is unverified; its reader will not be retrieved.",'order_unverified')
        self.stage = "reader_html"
        try:
            html_bytes = self.transport.get(url) if reader_html is None else reader_html
        except ArchiveError as error:
            if getattr(error, 'source_http_status', None) in {404, 410}:
                self._reader_missing(match[1], error)
            # Only a reader refused AFTER fresh session/access/order checks is
            # eligible for a bounded cooldown probe. Metadata and CDN errors
            # must never acquire this capability from their HTTP status alone.
            if (getattr(error, 'source_http_status', None) == 403
                    and getattr(error, 'source_response_kind', None) == 'html'):
                error.source_retry_episode_id = match[1]
            raise
        if len(html_bytes) > 8 * 1024 * 1024:
            raise ArchiveError("Reader HTML exceeds the supported size.")
        try:
            html = html_bytes.decode("utf-8")
        except UnicodeError:
            raise ArchiveError("Reader HTML has an unsupported encoding.") from None
        parser = ReaderHTML(match[1])
        parser.feed(html)
        text = None
        illustrations = ()
        if info['book']:
            from ..novels import NovelHTML
            novel = NovelHTML(match[1])
            novel.feed(html)
            text = novel.text()
            illustrations = tuple(novel.illustrations)
            urls = ()
        else:
            urls = ordered_sources(parser.images)
            if urls is None:
                raise unsupported("Normal reader HTML lacks a complete ordered comic manifest; use browser inspection.",'reader_manifest_incomplete')
        platform_id = safe_primitives("\n".join(parser.inline)).get("seriesId")
        if reader_html is not None:
            if len(parser.series_ids) != 1:
                raise MetadataError('Reader document lacks one consistent numeric series identity.')
            observed_series = next(iter(parser.series_ids))
            if platform_id is not None and platform_id != observed_series:
                raise MetadataError('Reader document contains conflicting series identities.')
            platform_id = observed_series
        if type(platform_id) is not int or platform_id < 1:
            raise ArchiveError("Reader does not expose a verified numeric series ID.")
        self.stage = "series_metadata"
        series_info = self._series_header(str(platform_id))
        if series_info.get('_header_unavailable') is True:
            if (series_id is not None and series_id != str(platform_id)
                    and self.verified_slugs.get(series_id) != str(platform_id)):
                raise MetadataError('Fallback series identity does not match the requested capture.')
            self.series = Series(series_id or str(platform_id), title_of(series_info), platform_id=str(platform_id))
        else:
            if (type(series_info.get('id')) is not int or series_info['id'] != platform_id
                    or series_info.get('book') is not info['book']
                    or series_info.get('comic') is not (not info['book'])):
                raise ArchiveError("Tapas series metadata does not identify the requested content type.")
            slug = series_info.get("url")
            if not isinstance(slug, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", slug):
                raise ArchiveError("Tapas returned an unexpected series slug.")
            if series_id is not None and series_id not in {slug, str(platform_id)}:
                raise ArchiveError("Supplied series ID does not match the reader's series.")
            self.series = Series(series_id or slug, series_title or series_info.get("title"),
                                 source_url=f"https://tapas.io/series/{slug}/info",
                                 platform_id=str(platform_id), slug=slug)
        self.episode_info = {key: info.get(key) for key in ("id", "free", "unlocked", "must_pay", "scheduled", "book", "closing_date")}
        episode = Episode(match[1], self.series.id, title or info.get("title"),
                          order if order is not None else info["scene"],
                          tuple(Page(i, value, False) for i, value in enumerate(urls, 1)),
                          access if access in {'tapas_free', 'tapas_unlocked', 'tapas_limited'} else 'tapas_readable', url, published_at(info.get("publish_date")),
                          content_type='novel' if info['book'] else 'comic', text_html=text,
                          access_expires_at=observed_expiration(info.get('closing_date')), illustrations=illustrations)
        logging.getLogger('comic_archive.download').info(
            'reader access: provider=tapas result=complete metadata_access=%s images=%s', access, len(episode.pages))
        self._manifest = Manifest(self.series, (episode,))
        return self._manifest

    def manifest(self):
        return self._manifest

    def retrieve(self, page, max_bytes=None):
        self.stage = "page_asset"
        from ..transport import Transport
        if isinstance(self.transport, Transport):
            return self.transport.get(page.location, max_bytes=max_bytes, image=True)
        return self.transport.get(page.location, max_bytes=max_bytes) if max_bytes is not None else self.transport.get(page.location)

    def asset_request(self, page, max_bytes=None):
        """Prepare an authorized CDN GET without handing browser objects to threads."""
        from ..downloads import AssetDownload
        from ..transport import Transport, asset_url
        asset_url(page.location)
        if page.local or urlsplit(page.location).hostname == 'tapas.io':
            return None
        transport = getattr(self.transport, 'assets', self.transport)
        if not isinstance(transport, Transport):
            return None
        self.stage = 'page_asset'
        def fetch():
            return transport.get(page.location,max_bytes=max_bytes,image=True)
        return AssetDownload(fetch)

    def refresh(self, episode):
        new = self.capture(episode.source_url, self.series.id, self.series.title,
                           episode.title, episode.order).episodes[0]
        if len(new.pages) != len(episode.pages) or any(
            (urlsplit(a.location).hostname, urlsplit(a.location).path) !=
            (urlsplit(b.location).hostname, urlsplit(b.location).path)
            for a, b in zip(new.pages, episode.pages, strict=True)
        ):
            raise ArchiveError("Reader page manifest changed; restart capture to preserve ordering.")
        return new
