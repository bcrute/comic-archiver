"""Read-only authenticated Tapas GET flow observed in the normal web reader."""
import json
import re
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from ..models import ArchiveError, Episode, Manifest, Page, Series, canonical_url
from ..session import load_session
from ..transport import Transport, asset_url
from .reader import ReaderHTML, ordered_sources, safe_primitives, json_shape
from ..errors import AccessDenied, AuthenticationError, MetadataError, UnsupportedContent
from ..expiry import expiration, check_expiration
from .pagination import EpisodeCountMismatch
HTTP_ADAPTER_VERSION = 23


def unsupported(message, code):
    error = UnsupportedContent(message)
    error.code = code
    return error


def access_state(info):
    """WUF/preview availability and must_pay=false alone never grant access."""
    if any(type(info.get(key)) is not bool for key in ("must_pay", "scheduled", "book", "free", "unlocked")) or "closing_date" not in info:
        raise MetadataError("Episode authorization fields are missing or unexpected; inspection is required.")
    if info["must_pay"] is True:
        raise AccessDenied("Episode requires an unlock; no reader or purchase action will be requested.")
    if info["scheduled"] is True:
        raise unsupported('Scheduled episodes are not supported by this importer.','scheduled')
    if info["closing_date"] is not None:
        check_expiration(expiration(info['closing_date']))
    if info.get("free") is True:
        return "tapas_free"
    if info.get("unlocked") is True:
        return "tapas_unlocked"
    raise AccessDenied("Episode is neither free nor already unlocked; it will not be retrieved.")


class TapasHTTPAdapter:
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
        info = self.json_get(f"/episode/{match[1]}/info")
        if type(info.get("id")) is not int or str(info["id"]) != match[1]:
            raise MetadataError("Episode metadata identifies a different episode.")
        access=access_state(info)
        return {"source_episode_id": match[1], "access_state": access,
                'access_expires_at':expiration(info['closing_date'])}

    def inspect_access(self, url):
        """Metadata-only diagnosis, including unsupported access states."""
        url=canonical_url(url)
        match=re.fullmatch(r'https://tapas\.io/episode/([1-9][0-9]{0,19})',url or '')
        if match is None:
            raise ArchiveError('Use an exact observed Tapas episode URL.')
        self.check_session()
        info=self.json_get(f'/episode/{match[1]}/info')
        if type(info.get('id')) is not int or str(info['id'])!=match[1]:
            raise MetadataError('Episode metadata identifies a different episode.')
        result={'authenticated':True,'source_episode_id':match[1],'source_url':url,
                'reader_or_comic_pages_requested':False,
                'authorization_flags':{key:info[key] for key in ('free','unlocked','must_pay','scheduled','book') if type(info.get(key)) is bool},
                'closing_date_field_present':'closing_date' in info,
                'closing_date_non_null':info.get('closing_date') is not None,
                'closing_date_kind':type(info.get('closing_date')).__name__ if type(info.get('closing_date')) in {str,int,float,type(None)} else 'unsupported',
                'positive_order_reported':type(info.get('scene')) is int and info['scene']>0}
        try:
            result.update(access_state=access_state(info),authorization_eligible=True,reason=None,
                          access_expires_at=expiration(info['closing_date']))
        except AccessDenied:
            result.update(access_state='unavailable_or_unverified',authorization_eligible=False,reason='access_denied')
        except UnsupportedContent as error:
            result.update(access_state='unavailable_or_unverified',authorization_eligible=False,reason=error.code)
        except MetadataError:
            result.update(access_state='unavailable_or_unverified',authorization_eligible=False,reason='authorization_unknown')
        return result

    def inspect_episode_page_size(self, key, max_limit=50):
        """One bounded metadata GET tests the already-observed page-size key."""
        if type(max_limit) is not int or max_limit not in {20,50,100}:
            raise ArchiveError('The metadata page-size experiment accepts 20, 50 or 100.')
        self.check_session()
        series=self.resolve_series(key)
        data=self.json_get(f"/series/{series['id']}/episodes?"+urlencode({'page':1,'max_limit':max_limit}))
        rows=data.get('episodes');pagination=data.get('pagination')
        if not isinstance(rows,list) or not isinstance(pagination,dict) or len(rows)>100:
            raise MetadataError('The metadata page-size response has an unexpected shape.')
        ids=[row.get('id') for row in rows if isinstance(row,dict)]
        if len(ids)!=len(rows) or any(type(key) is not int or key<1 for key in ids) or len(set(ids))!=len(ids):
            raise MetadataError('The metadata page-size response has invalid or repeated episode identities.')
        flags={key:value for key,value in pagination.items() if key in {'page','total','max_limit','limit','original_max_limit','has_next'} and type(value) in {int,bool}}
        return {'authenticated':True,'source_series_id':str(series['id']),'requested_max_limit':max_limit,
                'returned_episode_count':len(rows),'episode_ids':[str(key) for key in ids],
                'pagination':flags,'complete':False,'reader_or_comic_pages_requested':False}

    def series_identity(self, key):
        self.check_session()
        series = self.resolve_series(key)
        return {'source_series_id':str(series['id']),'title':series['title']}

    def series_metadata(self,key):
        from .series_metadata import series_details
        self.check_session()
        info=self.resolve_series(key)
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
        return Series(slug,info['title'],source_url=url,platform_id=str(info['id']),slug=slug,
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
            parser = ReaderHTML("unused")
            parser.feed(html.decode("utf-8"))
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
        series = self.resolve_series(key)
        if comic_only and series.get('book') is True and series.get('comic') is False:
            return {'authenticated':True,'complete':False,'source_series_id':str(series['id']),
                    'title':series['title'],'episodes':[], 'episode_enumeration_skipped':True,
                    'unsupported_series_reason':'novel','series_content_flags':{'book':True,'comic':False}}
        try:
            episodes = self.enumerate_episodes(series["id"], strict_order=strict_order)
        except EpisodeCountMismatch as error:
            summaries=getattr(error,'partial_episode_summaries',None)
            if isinstance(summaries,list):
                error.partial_inspection={'complete':False,'authenticated':True,
                    'source_series_id':str(series['id']),'title':series['title'],
                    'episode_count':len(summaries),'episodes':summaries}
            raise
        summaries=self._episode_summaries(episodes)
        return {"complete": True, "authenticated": True, "source_series_id": str(series["id"]),
                "title": series["title"], "episode_count": len(summaries),
                "ordering_verified": all(item["order"] is not None for item in summaries),
                "accessible_episode_count": sum(item["access_state"] != "unavailable_or_unverified" for item in summaries),
                "series_content_flags": {key:series[key] for key in ('book','comic') if type(series.get(key)) is bool},
                "episodes": summaries}

    def _episode_summaries(self,episodes):
        scenes = [item.get("scene") for item in episodes]
        summaries = []
        for item in episodes:
            reason=None
            try:
                access = access_state(item)
            except AccessDenied:
                access = "unavailable_or_unverified"
                reason='access_denied'
            except UnsupportedContent as error:
                access = 'unavailable_or_unverified'
                reason=error.code
            scene = item.get("scene")
            order = scene if type(scene) is int and scene > 0 and scenes.count(scene) == 1 else None
            summaries.append({"id": str(item["id"]), "title": item["title"],
                              "order": order, "source_scene": scene if type(scene) is int else None, "access_state": access,
                              'authorization_reason':reason,
                              'access_expires_at':expiration(item['closing_date']) if access in {'tapas_free','tapas_unlocked'} else None,
                              "source_url": f"https://tapas.io/episode/{item['id']}"})
        return summaries

    def capture(self, url, series_id=None, series_title=None, title=None, order=None):
        url = canonical_url(url)
        match = re.fullmatch(r"https://tapas\.io/episode/([0-9]+)", url or "")
        if not match:
            raise ArchiveError("Use an exact https://tapas.io/episode/<numeric-id> URL.")
        self.stage = "session"
        self.check_session()
        self.stage = "episode_metadata"
        info = self.json_get(f"/episode/{match[1]}/info")
        if type(info.get("id")) is not int or str(info["id"]) != match[1]:
            raise ArchiveError("Tapas episode metadata identifies a different episode.")
        access = access_state(info)  # Gate before requesting the reader or images.
        if order is None and (type(info.get("scene")) is not int or info["scene"] < 1):
            raise unsupported("Episode order is unverified; its reader will not be retrieved.",'order_unverified')
        self.stage = "reader_html"
        check_expiration(expiration(info['closing_date']))
        try:
            html_bytes = self.transport.get(url)
        except ArchiveError as error:
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
        if type(platform_id) is not int or platform_id < 1:
            raise ArchiveError("Reader does not expose a verified numeric series ID.")
        self.stage = "series_metadata"
        series_info = self.json_get(f"/series/{platform_id}")
        if (type(series_info.get('id')) is not int or series_info['id'] != platform_id
                or series_info.get('book') is not info['book']
                or series_info.get('comic') is not (not info['book'])):
            raise ArchiveError("Tapas series metadata does not identify the requested content type.")
        slug = series_info.get("url")
        if not isinstance(slug, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", slug):
            raise ArchiveError("Tapas returned an unexpected series slug.")
        if series_id is not None and series_id not in {slug, str(platform_id)}:
            raise ArchiveError("Supplied series ID does not match the reader's series.")
        self.series = Series(series_id or slug, series_title or series_info["title"],
                             source_url=f"https://tapas.io/series/{slug}/info",
                             platform_id=str(platform_id), slug=slug)
        self.episode_info = {key: info.get(key) for key in ("id", "free", "unlocked", "must_pay", "scheduled", "book", "closing_date")}
        episode = Episode(match[1], self.series.id, title or info["title"],
                          order if order is not None else info["scene"],
                          tuple(Page(i, value, False) for i, value in enumerate(urls, 1)),
                          access, url, info.get("publish_date"),
                          content_type='novel' if info['book'] else 'comic', text_html=text,
                          access_expires_at=expiration(info['closing_date']), illustrations=illustrations)
        self._manifest = Manifest(self.series, (episode,))
        return self._manifest

    def manifest(self):
        return self._manifest

    def retrieve(self, page, max_bytes=None):
        self.stage = "page_asset"
        manifest=getattr(self,'_manifest',None)
        if manifest is not None: check_expiration(manifest.episodes[0].access_expires_at)
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
        manifest=getattr(self,'_manifest',None)
        expires_at=manifest.episodes[0].access_expires_at if manifest is not None else None
        def fetch():
            check_expiration(expires_at)
            return transport.get(page.location,max_bytes=max_bytes)
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
