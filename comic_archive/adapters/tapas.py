"""Experimental normal-reader capture. No guessed private API or unlock request."""
import re
import importlib
import time
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urljoin, unquote
from .. import models
# A worker started before this prototype upgrade retains the older dataclasses.
# Migrate only our fixed model module once, preserving the public error class so
# the running worker's exception handler remains valid. Fresh CLI runs skip this.
if "credits" not in models.Series.__dataclass_fields__ or "text_html" not in models.Episode.__dataclass_fields__:
    error_type = models.ArchiveError
    importlib.reload(models)
    models.ArchiveError = error_type
    import sys
    cached = sys.modules.get("comic_archive.adapters.tapas_http")
    if cached is not None:
        importlib.reload(cached)
from ..models import ArchiveError, Episode, Manifest, Page, Series, canonical_url
from ..session import browser_api, launch_browser, load_session, save_session
from ..transport import Transport, asset_url
from .. import errors
if not hasattr(errors, "NetworkUnavailable"):
    # Retain existing exception identities in a live worker while adding new
    # classifications. Its previously imported adapters still raise these types.
    known_errors = {name: value for name, value in vars(errors).items()
                    if isinstance(value, type) and issubclass(value, ArchiveError)}
    importlib.reload(errors)
    for name, value in known_errors.items():
        setattr(errors, name, value)
if not hasattr(errors,'AssetReadUnavailable'):
    # Old resident workers retain their exception identities. Add the new
    # subtype against their current NetworkUnavailable rather than reloading
    # its base and making existing catch clauses miss the new exception.
    errors.AssetReadUnavailable = type('AssetReadUnavailable',(errors.NetworkUnavailable,),
                                      {'__module__':'comic_archive.errors'})
from ..errors import AuthenticationError, MetadataError, NetworkUnavailable, RunLimit


def reload_dependency(module):
    """Keep prototype upgrade failures useful without exposing exception data."""
    try:
        return importlib.reload(module)
    except Exception as error:
        import traceback
        frames = traceback.extract_tb(error.__traceback__)
        locations = ", ".join(f"{Path(f.filename).name}:{f.lineno}:{f.name}" for f in frames[-4:])
        raise ArchiveError(f"Prototype upgrade failed ({type(error).__name__}; {locations}); credential details are suppressed.") from None

from . import reader
from .account_navigation import navigation_path
# The local prototype worker reloads this fixed adapter between tasks. Reload
# its fixed parser dependency too when upgrading a running research worker.
if getattr(reader, "RESEARCH_VERSION", 1) < 18:
    reader = reload_dependency(reader)
from .reader import ReaderHTML, ReaderObservation, ordered_sources, safe_primitives, source_summary, COMIC_PATH_PREFIXES
import sys
cached_transport = sys.modules.get("comic_archive.transport")
if cached_transport is not None and getattr(cached_transport, "TRANSPORT_VERSION", 1) < 10:
    Transport = reload_dependency(cached_transport).Transport
cached_http = sys.modules.get("comic_archive.adapters.tapas_http")
if cached_http is not None and getattr(cached_http, "HTTP_ADAPTER_VERSION", 1) < 22:
    reload_dependency(cached_http)
cached_discovery = sys.modules.get("comic_archive.discovery")
if cached_discovery is not None and getattr(cached_discovery, "DISCOVERY_VERSION", 1) < 4:
    reload_dependency(cached_discovery)
cached_library_navigation=sys.modules.get('comic_archive.adapters.library_navigation')
if cached_library_navigation is not None and getattr(cached_library_navigation,'LIBRARY_NAVIGATION_VERSION',1)<3:
    reload_dependency(cached_library_navigation)
cached_jobs = sys.modules.get("comic_archive.jobs")
cached_queue = sys.modules.get('comic_archive.queue_client')
if cached_queue is not None and getattr(cached_queue,'QUEUE_CLIENT_VERSION',1)<16:
    reload_dependency(cached_queue)
cached_importer = sys.modules.get('comic_archive.importer')
cached_archive = sys.modules.get("comic_archive.archive")
if cached_archive is not None and getattr(cached_archive, "ARCHIVE_VERSION", 1) < 14:
    reload_dependency(cached_archive)
if cached_importer is not None and getattr(cached_importer,'IMPORTER_VERSION',1)<2:
    reload_dependency(cached_importer)
cached_inventory = sys.modules.get("comic_archive.inventory")
if cached_inventory is not None and getattr(cached_inventory,'INVENTORY_VERSION',1)<21:
    reload_dependency(cached_inventory)
if cached_jobs is not None and getattr(cached_jobs, "JOBS_VERSION", 1) < 56:
    reload_dependency(cached_jobs)


def finish_route(route, action):
    """Suppress only a completed route callback; never retry or allow a write."""
    try:
        getattr(route,action)()
    except Exception as error:
        if str(error) in {'Route is already handled!',f'Route.{action}: Route is already handled!'}:
            return
        raise ArchiveError('Read-only browser routing failed; credential details are suppressed.') from None


def history_data(observation):
    """The last observed history answer's data object; {} when it is missing or null."""
    response=observation.library_values.get('/profile/transactions')
    data=response.get('data') if isinstance(response,dict) else None
    return data if isinstance(data,dict) else {}


def read_only_source_request(route):
    method=getattr(route.request,'method','GET')
    if isinstance(method,str) and method.upper() not in {'GET','HEAD','OPTIONS'}:
        finish_route(route,'abort')
    else:
        finish_route(route,'continue_')


# How long a read that got no answer waits before each further try, in seconds.
REPEAT_PAUSES = (2, 6, 20)


class ReadFailed(Exception):
    """A read the page made that ended without a usable answer.

    Its text is put together here from fixed words and numbers the page reported: how far the read got,
    after how long, and over which protocol. Nothing of the address or of an answer is in it.
    """
    def __init__(self, seen):
        seen = seen if isinstance(seen, dict) else {}

        def word(key, pattern):
            value = seen.get(key)
            return value if isinstance(value, str) and re.fullmatch(pattern, value) else 'unknown'
        name = word('name', r'[A-Za-z]{1,40}')
        # The read's own time limit ended it: it has already waited as long as it may.
        self.timed_out = name == 'AbortError'
        stage = {'request': 'no answer', 'answer': 'the answer broke off'}.get(seen.get('stage'), 'failed')
        ms = seen['ms'] if type(seen.get('ms')) is int and 0 <= seen['ms'] <= 3600000 else 'unknown'
        protocol = r'[a-z0-9./-]{1,20}'
        super().__init__(f"{stage} after {ms} ms ({name}; read over {word('protocol', protocol)}; "
                         f"site over {word('site', protocol)})")


class BrowserReadTransport:
    """Observed metadata GETs in the normal signed-in browser; HTTPX for images."""
    def __init__(self, page, context, interval=1.0, deadline=None, metrics=None,
                 metadata_interval=1.0, download_profile='normal', network_guard=None):
        from ..downloads import validate_request_interval
        validate_request_interval(metadata_interval, download_profile)
        self.metadata_interval = metadata_interval
        self.page = page
        self.network_guard = network_guard
        self.assets = Transport(context.cookies(), interval=interval, metrics=metrics, profile=download_profile,
                                network_guard=network_guard, agent=getattr(context, 'agent', None))
        self.last = 0
        self.last_response = None
        self.blocking_preload = False
        self.deadline = deadline
        self.cancel_check=None
        self.blocked_mutations=0
        try:
            self.page.route('**/*',self.read_only_request)
        except Exception as error:
            # Classify fixed Playwright failure vocabulary without displaying
            # URLs, headers, response text or arbitrary exception details.
            message=str(error).lower()
            code=next((code for fragment,code in (
                ('has been closed','browser_closed'),('target closed','browser_closed'),
                ('different loop','browser_loop'),('event loop','browser_loop'),
                ('thread','browser_thread'),('already handled','route_handled'),
                ('intercept','interception'),('timeout','timeout')) if fragment in message),'route_installation')
            try:self.page.unroute('**/*',self.read_only_request)
            except Exception:pass
            self.assets.close()
            raise ArchiveError(f'Cannot install read-only browser routing ({code}); no episode request was made.') from None

    def deny_mutation(self,route):
        method=getattr(route.request,'method','GET')
        if isinstance(method,str) and method.upper() not in {'GET','HEAD','OPTIONS'}:
            # Imported sessions are for retrieval. Interactive login uses a
            # separate browser flow; no source-side write is needed here.
            self.blocked_mutations+=1;finish_route(route,'abort');return True
        return False

    def read_only_request(self,route):
        if self.network_guard is not None:
            try: self.network_guard.check()
            except ArchiveError:
                finish_route(route, 'abort'); return
        if not self.deny_mutation(route):finish_route(route,'continue_')

    def block_comic_preload(self, route):
        if self.network_guard is not None:
            try: self.network_guard.check()
            except ArchiveError:
                finish_route(route, 'abort'); return
        if self.deny_mutation(route):return
        request = route.request
        parsed = urlsplit(request.url)
        if (request.resource_type in {'image','fetch','xhr'} and parsed.scheme == "https" and parsed.hostname
                and (parsed.hostname == "tapas.io" or parsed.hostname.endswith(".tapas.io"))
                and parsed.path.startswith(COMIC_PATH_PREFIXES)):
            finish_route(route,'abort')
        else:
            finish_route(route,'continue_')

    def get(self, url, max_bytes=None):
        if self.network_guard is not None: self.network_guard.check()
        try:
            return self._get(url, max_bytes)
        except ArchiveError:
            raise
        except Exception as error:
            if self.deadline is not None and time.monotonic() >= self.deadline:
                raise RunLimit("The job time budget is exhausted; its queue is retained.") from None
            unavailable=NetworkUnavailable("The browser could not load Tapas. Check its network/VPN connection and open the normal site; credential details are suppressed.")
            unavailable.browser_failure_code='timeout' if self.timed_out(error) else 'browser_error'
            print(f"Browser request failed: {self.described(error)}",flush=True)
            raise unavailable from None

    @staticmethod
    def described(error):
        """For the downloader's log: which failure it was. This app's own messages are fixed texts;
        another engine's can name an address, so of those only the kind is written."""
        ours=type(error).__module__=='comic_archive.qt_engine' or isinstance(error,ReadFailed)
        return f"{type(error).__name__}{': '+str(error) if ours else ''}"

    @staticmethod
    def timed_out(error):
        return type(error).__name__=='TimeoutError' or getattr(error,'timed_out',False) is True

    def patience(self):
        """How long one read may take, in milliseconds."""
        return max(1, min(30000, (self.deadline - time.monotonic()) * 1000)) if self.deadline is not None else 30000

    def read(self, script, arguments):
        """One read from inside the page. The page reports a read that got no usable answer; that is raised here."""
        value = self.page.evaluate(script, arguments)
        if isinstance(value,dict) and 'failed' in value:
            raise ReadFailed(value['failed'])
        return value

    def read_again(self, error, script, arguments):
        """A few more tries for a read the page could not complete, each after a longer pause, as a person
        reloads a page that did not come up before giving up on it.

        The further tries do not follow a redirect, which then answers with status 0, so the log tells a
        redirect from a connection that keeps failing. Either way the job then pauses as it always did.
        """
        manual = script.replace("redirect:'error'","redirect:'manual'")
        for pause in REPEAT_PAUSES:
            if self.timed_out(error) or (self.deadline is not None and time.monotonic()+pause>=self.deadline):
                break
            print(f"Browser read failed: {self.described(error)}; trying again in {pause} s.",flush=True)
            self.page.wait_for_timeout(pause*1000)
            if callable(self.cancel_check):self.cancel_check()
            if not isinstance(self.page.url, str) or not self.page.url.startswith("https://tapas.io/"):
                self.page.goto("https://tapas.io/", wait_until="domcontentloaded")
            self.last = time.monotonic()
            try:
                value = self.read(manual, arguments | {'timeout': self.patience()})
            except Exception as again:
                error = again
                continue
            if manual != script and isinstance(value,dict) and value.get('status') == 0:
                print("Browser read was answered with a redirect.",flush=True)
                break
            return value
        raise error

    def _get(self, url, max_bytes=None):
        if callable(self.cancel_check):self.cancel_check()
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise RunLimit("The job time budget is exhausted; its queue is retained.")
        parsed = urlsplit(asset_url(url))
        if parsed.hostname != "tapas.io":
            self.assets.deadline = self.deadline
            self.assets.cancel_check = self.cancel_check
            return self.assets.get(url, max_bytes=max_bytes) if max_bytes is not None else self.assets.get(url)
        if not re.fullmatch(r"/(?:who-am-i|episode/[0-9]+(?:/info)?|series/[0-9]+(?:/episodes)?|series/[A-Za-z0-9_-]{1,100}/info)", parsed.path):
            raise ArchiveError("Browser metadata request is outside the observed GET routes.")
        if not isinstance(self.page.url, str) or not self.page.url.startswith("https://tapas.io/"):
            self.page.goto("https://tapas.io/", wait_until="domcontentloaded")
        pause = max(0, self.last + self.metadata_interval - time.monotonic())
        if pause:
            self.page.wait_for_timeout(pause * 1000)
        self.last = time.monotonic()
        details = bool(re.fullmatch(r"/series/[A-Za-z0-9_-]{1,100}/info", parsed.path))
        # The built-in engine cannot hand over a visited page's text; there the details page is read as the reader's is.
        if details and getattr(self.page, 'navigation_bodies', True) is not False:
            response = self.page.goto(url, wait_until="domcontentloaded")
            if canonical_url(self.page.url) != canonical_url(url) or response is None or response.status != 200:
                raise ArchiveError("Normal browser reader navigation was refused or redirected.")
            return response.body()
        timeout = self.patience()
        reader = details or bool(re.fullmatch(r'/episode/[0-9]+',parsed.path))
        # The authorized reader's static HTML already carries ordered pages.
        # Read its text through the ordinary signed-in browser GET; executing
        # reader scripts or preloading images would duplicate requests/work.
        script = """async ({url,timeout,accept,limit}) => {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), timeout);
            const began = performance.now();
            let stage = 'request';
            try {
            const response = await fetch(url, {method:'GET', credentials:'same-origin',
                redirect:'error', headers:{Accept:accept},signal:controller.signal});
            stage = 'answer';
            const text = await response.text();
            // Cloudflare documents this fixed marker for Challenge Pages.
            // Only a classification crosses the browser boundary.
            const contentType = (response.headers.get('Content-Type') || '').split(';')[0].trim().toLowerCase();
            const kind = response.headers.get('cf-mitigated') === 'challenge' ? 'challenge' :
                contentType === 'text/html' ? 'html' :
                contentType === 'application/json' ? 'json' :
                contentType === 'text/plain' ? 'text' : 'other';
            return {status:response.status, text:text.length <= limit ? text : null,
                    source_response_kind:kind,
                    retry_after:response.headers.get('Retry-After')};
            } catch (error) {
                // A read without a usable answer. Only how far it got, how long it took and the protocols
                // in use cross the browser boundary; the engine has no word for why the network failed.
                const protocol = entries => String((entries[entries.length - 1] || {}).nextHopProtocol || 'none');
                return {failed:{stage, name:String(error && error.name), ms:Math.round(performance.now() - began),
                    protocol:protocol(performance.getEntriesByName(new URL(url, location.href).href)),
                    site:protocol(performance.getEntriesByType('navigation'))}};
            } finally { clearTimeout(timer); }
        }"""
        if details:
            # A redirect is answered with status 0, not followed: a page visit that is redirected is refused as well.
            script = script.replace("redirect:'error'", "redirect:'manual'")
        arguments = {"url": url, "timeout": timeout,
               'accept':'text/html' if reader else 'application/json',
               'limit':8388608 if reader else 2097152}
        try:
            value = self.read(script, arguments)
        except Exception as error:
            value = self.read_again(error, script, arguments)
        if (not isinstance(value,dict) or type(value.get('status')) is not int or not 0<=value['status']<=599
                or (value.get('text') is not None and not isinstance(value['text'],str))):
            raise ArchiveError('Normal browser metadata response has an unsupported shape; this job has stopped.')
        self.last_response = {"status": value["status"],"path":parsed.path}
        from ..source_response import source_response_diagnostics
        diagnostics = source_response_diagnostics(value)
        self.last_response.update(diagnostics)
        if value["status"] == 401:
            raise AuthenticationError("Tapas requires a fresh browser login; this job has stopped.")
        if value['status'] in {502,503,504}:
            from ..retry_after import retain_retry_after
            unavailable=NetworkUnavailable('Tapas metadata is temporarily unavailable; this job has paused without retrying the request.')
            unavailable.source_http_status=value['status']
            raise retain_retry_after(unavailable, value.get('retry_after'))
        if value['status'] in {404,410} and re.fullmatch(r'/series/[1-9][0-9]{0,19}',parsed.path):
            unavailable=MetadataError('Observed series metadata is unavailable; its source identity is retained.')
            unavailable.source_http_status=value['status']
            unavailable.source_series_id=parsed.path.rsplit('/',1)[1]
            unavailable.source_route=parsed.path
            raise unavailable
        if value["status"] != 200:
            detail = ' (browser challenge)' if diagnostics.get('source_response_kind') == 'challenge' else ''
            refused = ArchiveError(f"Normal browser metadata GET {parsed.path} returned HTTP {value['status']}{detail}; this job has stopped.")
            if value['status'] in {403,429}:
                refused.source_http_status = value['status']
                for key, item in diagnostics.items():
                    setattr(refused, key, item)
                from ..retry_after import retain_retry_after
                retain_retry_after(refused, value.get('retry_after'))
            raise refused
        if value['text'] is None:
            raise ArchiveError('Normal browser metadata exceeds the supported response size; this job has stopped.')
        return value["text"].encode("utf-8")

    def close(self):
        try:
            # Finish active callbacks before replacing this page's transport.
            # The context's independent mutation guard remains installed.
            self.page.unroute_all(behavior='wait')
        finally:
            self.blocking_preload = False
            self.assets.close()


class TapasAdapter:
    supports_novels=True
    supports_expiring_access=True
    supports_comic_series_classification=True
    supports_series_metadata=True
    def __init__(self, session: Path, preferred_browser="auto", headless=False, interval=1.0):
        self.session = session
        self.preferred_browser = preferred_browser
        self.transport = None
        self.headless = headless
        self.interval = interval
        from ..network import NetworkGuard
        self.network_guard = NetworkGuard(Path(session).absolute().parent / 'Library')

    def __enter__(self):
        state = self.session / "state.json"
        if not state.is_file() or any(p.is_symlink() for p in (state, *state.parents)):
            raise ArchiveError("Run login first.")
        storage_state = load_session(self.session)
        try:
            self.driver = browser_api(self.preferred_browser)().start()
        except ArchiveError:
            raise
        except Exception:
            raise ArchiveError("Cannot start the Playwright driver. Check the Python/Playwright installation.") from None
        self.browser = None
        try:
            self.browser = launch_browser(self.driver, self.preferred_browser, headless=self.headless)
        except ArchiveError:
            self.driver.stop()
            raise
        try:
            self.context = self.browser.new_context(storage_state=storage_state,service_workers='block')
            self.context.route('**/*', self.network_request)
            self.page = self.context.new_page()
        except Exception:
            if self.browser:
                self.browser.close()
            self.driver.stop()
            raise ArchiveError("Browser started, but loading the saved session failed. Re-import Firefox cookies or run login; credential details are suppressed.") from None
        return self

    def network_request(self, route):
        try:
            self.network_guard.check()
        except ArchiveError:
            finish_route(route, 'abort'); return
        finish_route(route, 'fallback')

    def network_read_only_request(self, route):
        guard = getattr(self, 'network_guard', None)
        if guard is not None:
            try: guard.check()
            except ArchiveError:
                finish_route(route, 'abort'); return
        read_only_source_request(route)

    def __exit__(self, *args):
        try:
            if self.transport:
                self.transport.close()
        finally:
            try:
                self.browser.close()
            finally:
                self.driver.stop()

    def automatic_adapter(self):
        from .tapas_http import TapasHTTPAdapter
        if not getattr(self,'_read_only_context_installed',False):
            self.context.route('**/*',self.network_read_only_request)
            self._read_only_context_installed=True
        last_metadata_request = self.transport.last if isinstance(self.transport, BrowserReadTransport) else 0.0
        if self.transport:
            self.transport.close()
        self.transport = BrowserReadTransport(self.page, self.context, interval=getattr(self, "interval", 1.0),
                    metadata_interval=getattr(self, 'metadata_interval', 1.0),
                    deadline=getattr(self, "deadline", None), metrics=getattr(self,'asset_metrics',None),
                    network_guard=getattr(self, 'network_guard', None))
        self.transport.last = last_metadata_request
        self.transport.cancel_check=getattr(self,'cancel_check',None)
        # Threaded image reads use the asset client directly, so it needs the same stop and time limit.
        self.transport.assets.deadline=self.transport.deadline
        self.transport.assets.cancel_check=self.transport.cancel_check
        self._automatic = TapasHTTPAdapter(transport=self.transport, expected_account_id=getattr(self, "expected_account_id", None))
        return self._automatic

    def account_identity(self):
        return self.automatic_adapter().account_identity()

    def remaining_timeout(self):
        guard = getattr(self, 'network_guard', None)
        if guard is not None: guard.check()
        cancel_check=getattr(self,'cancel_check',None)
        if callable(cancel_check):cancel_check()
        deadline = getattr(self, "deadline", None)
        if deadline is None:
            return 30000
        remaining = (deadline - time.monotonic()) * 1000
        if remaining <= 0:
            raise RunLimit("The job time budget is exhausted; its queue is retained.")
        return max(1, min(30000, remaining))

    def wait_normal(self, milliseconds):
        self.page.wait_for_timeout(min(milliseconds, self.remaining_timeout()))
        self.remaining_timeout()

    def episode_access(self, url):
        return self.automatic_adapter().episode_access(url)

    def inspect_episode_access(self, url):
        return self.automatic_adapter().inspect_access(url)

    def inspect_episode_page_size(self, key, max_limit=50):
        return self.automatic_adapter().inspect_episode_page_size(key,max_limit)

    def series_identity(self,key):
        return self.automatic_adapter().series_identity(key)

    def series_metadata(self,key):
        return self.automatic_adapter().series_metadata(key)

    def retrieve_cover(self,series,max_bytes=None):
        if getattr(self,'_automatic',None) is None:
            raise ArchiveError('Capture verified series metadata before retrieving its cover.')
        return self._automatic.retrieve_cover(series,max_bytes=max_bytes)

    def episode_url(self,key):
        from .tapas_http import TapasHTTPAdapter
        return TapasHTTPAdapter.episode_url(self,key)

    def capture_automatic(self, url, series_id=None, series_title=None, title=None, order=None):
        adapter = self.automatic_adapter()
        self._manifest = adapter.capture(url, series_id, series_title, title, order)
        self.series = self._manifest.series
        self.capture_mode = "authenticated_html_manifest"
        save_session(self.context, self.session)
        episode = self._manifest.episodes[0]
        content = 'novel chapter text' if episode.content_type == 'novel' else f'{len(episode.pages)} page URLs'
        print(f"Episode {episode.id}: verified access; captured {content} without scrolling.", flush=True)
        return self._manifest

    def inspect_reader_identity(self,url):
        """Inspect authorized HTML identity without requesting candidate series IDs."""
        adapter=self.automatic_adapter()
        verified=adapter.episode_access(url)
        url=adapter.episode_url(verified['source_episode_id'])
        raw=adapter.transport.get(url)
        if len(raw)>8*1024*1024:raise ArchiveError('Reader HTML exceeds the supported size.')
        parser=ReaderHTML(verified['source_episode_id']);parser.feed(raw.decode('utf-8'))
        source_shapes=[]
        for row in parser.images:
            source=row.get('sources',{}).get('data-src') or row.get('sources',{}).get('src')
            try:
                parsed=urlsplit(asset_url(source))
            except (ArchiveError,ValueError,TypeError):
                continue
            parts=parsed.path.strip('/').split('/')
            prefix=parts[0] if parts and re.fullmatch(r'[a-z]{1,16}',parts[0]) and parts[0] not in {'auth','token','session','cookie','secret'} else None
            suffix=Path(parsed.path).suffix.lower()
            source_shapes.append({'host':parsed.hostname,'path_prefix':'/'+prefix+'/' if prefix else None,
                                  'path_segment_count':len(parts),'extension':suffix if suffix in {'.jpg','.jpeg','.png','.gif','.webp'} else None,
                                  'has_query':bool(parsed.query)})
        inline='\n'.join(parser.inline)
        try:inline_series_id=safe_primitives(inline).get('seriesId')
        except MetadataError:inline_series_id=None  # The candidates below show the disagreement.
        ids=sorted({int(m[1]) for m in re.finditer(r'(?<![A-Za-z0-9_])[\"\']?seriesId[\"\']?\s*[:=]\s*[\"\']?([0-9]{1,15})(?![A-Za-z0-9_.])',inline)})
        rendered=self._reader_navigation(url)
        adapter.check_session()
        from ..novels import novel_structure
        novel = novel_structure(raw.decode('utf-8'), verified['source_episode_id'])
        return {'authenticated':True,**verified,'source_url':url,
                'inline_series_id':inline_series_id,
                'inline_series_id_candidates':ids,'data_series_ids':sorted(parser.series_ids),
                'scoped_page_count':len(parser.images),'ordered_sources_available':ordered_sources(parser.images) is not None,
                'scoped_image_source_shapes':source_shapes,
                'provided_episode_navigation':parser.navigation_summary(),
                'rendered_episode_navigation':rendered,
                'series_metadata_requested':False,'comic_images_requested':False,
                **({'novel_structure': novel} if novel['chapter_container_count'] else {})}

    def _reader_navigation(self,url):
        """Observe rendered public reader controls without opening or activating them."""
        result={'available':False,'controls':[],'truncated':False,'controls_activated':False,
                'followed_links':False,'authorization_evidence':False,'series_membership_verified':False}
        if self.page.url!=url:return result
        try:
            self.wait_normal(500)
            observed=self.page.locator('a,button,select,[role="button"],[role="combobox"],[role="listbox"]').evaluate_all("""els=>{
                const rows=[];
                for(const e of els.slice(0,2000)){
                    const text=(e.innerText||e.getAttribute('aria-label')||'').trim().replace(/\\s+/g,' ').toLowerCase();
                    let label=['next','previous','prev','next episode','previous episode','episode list','episodes','chapters'].includes(text)?text:null;
                    if(/^(?:ep\\.?|episode|chapter)\\s*\\d+(?:\\s*[-:]\\s*.+)?$/.test(text))label='episode_number_label';
                    const hint=/episode|chapter/.test(e.className||'');
                    const href=e.tagName==='A'?e.href:null;
                    if(!label&&!hint&&!(href&&/^https:\\/\\/tapas\\.io\\/episode\\/[1-9][0-9]{0,19}$/.test(href)))continue;
                    const fields={};
                    for(const a of [...e.attributes]){
                        if(/^data-[a-z0-9-]{1,60}$/.test(a.name)&&!/auth|session|cookie|token|password|secret|user|email/.test(a.name))
                            fields[a.name]=/^\\d+$/.test(a.value)?'integer':/^(true|false)$/.test(a.value)?'boolean':'string';
                        if(Object.keys(fields).length===20)break;
                    }
                    rows.push({tag:e.tagName.toLowerCase(),role:e.getAttribute('role'),label,
                        episode_class_hint:hint,url:href,episode_id:e.getAttribute('data-episode-id'),
                        attribute_value_types:fields,
                        visible:Boolean(e.getClientRects().length)&&getComputedStyle(e).visibility!=='hidden',
                        option_count:e.tagName==='SELECT'?e.options.length:null});
                    if(rows.length===80)break;
                }
                return {rows,truncated:els.length>2000||rows.length===80};
            }""")
            self.remaining_timeout()
            if self.page.url!=url or not isinstance(observed,dict):return result
            rows=observed.get('rows')
            if not isinstance(rows,list):return result
            labels={'next','previous','prev','next episode','previous episode','episode list','episodes','chapters','episode_number_label'}
            for row in rows[:80]:
                if not isinstance(row,dict) or row.get('tag') not in {'a','button','select','div','span','li'}:continue
                clean={'tag':row['tag'],'visible':row.get('visible') is True,'episode_class_hint':row.get('episode_class_hint') is True}
                if isinstance(row.get('role'),str) and row['role'] in {'button','combobox','listbox'}:clean['role']=row['role']
                if isinstance(row.get('label'),str) and row['label'] in labels:clean['label']=row['label']
                link=row.get('url')
                if isinstance(link,str) and re.fullmatch(r'https://tapas\.io/episode/[1-9][0-9]{0,19}',link):
                    clean['source_url']=link;clean['source_episode_id']=link.rsplit('/',1)[1]
                identity=row.get('episode_id')
                if isinstance(identity,str) and re.fullmatch(r'[1-9][0-9]{0,19}',identity):clean['provided_episode_id']=identity
                count=row.get('option_count')
                if row['tag']=='select' and type(count) is int and 0<=count<=10000:clean['option_count']=count
                fields=row.get('attribute_value_types')
                if isinstance(fields,dict):
                    clean['attribute_value_types']={key:value for key,value in list(fields.items())[:20]
                        if isinstance(key,str) and re.fullmatch(r'data-[a-z0-9-]{1,60}',key)
                        and not re.search(r'auth|session|cookie|token|password|secret|user|email',key)
                        and isinstance(value,str) and value in {'integer','boolean','string'}}
                if clean.get('label') or clean['episode_class_hint'] or clean.get('source_url'):result['controls'].append(clean)
            result.update(available=True,truncated=observed.get('truncated') is True)
            return result
        except (AuthenticationError,RunLimit):raise
        except Exception:return result

    def inspect_series(self, key, strict_order=True, comic_only=False):
        return self.automatic_adapter().inspect_series(key, strict_order=strict_order,comic_only=comic_only)

    def inspect_series_metadata(self,key):
        """Observe public series metadata; never open an episode reader."""
        from .reader import json_shape
        adapter = self.automatic_adapter()
        adapter.check_session()
        info = adapter.resolve_series(key)
        slug = info.get('url')
        if not isinstance(slug,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',slug):
            raise ArchiveError('Series metadata has no verified public slug.')
        raw = adapter.transport.get(f'https://tapas.io/series/{slug}/info')
        if len(raw)>8*1024*1024:
            raise ArchiveError('Series page exceeds the supported metadata size.')
        head = self.page.locator('meta[property^="og:"],meta[name="description"]').evaluate_all("""els=>els.map(e=>({
            field:e.getAttribute('property')||e.getAttribute('name'),
            length:(e.getAttribute('content')||'').length,
            image_host:(e.getAttribute('property')==='og:image' && /^https:\\/\\//.test(e.content)) ? new URL(e.content).hostname : null
        }))""")
        structure = self.page.locator('[class*="desc"],[class*="creator"],[class*="author"],[class*="credit"]').evaluate_all("""els=>els.slice(0,50).map(e=>({
            tag:e.tagName.toLowerCase(),classes:Array.from(e.classList).filter(c=>/^[A-Za-z_][A-Za-z0-9_-]{0,80}$/.test(c)),
            attributes:Array.from(e.attributes).map(a=>a.name),text_length:(e.innerText||'').length,
            children:Array.from(e.children).slice(0,8).map(c=>({tag:c.tagName.toLowerCase(),
                classes:Array.from(c.classList).filter(n=>/^[A-Za-z_][A-Za-z0-9_-]{0,80}$/.test(n)),
                text_length:(c.innerText||'').length,link_count:c.querySelectorAll('a').length}))
        }))""")
        markers = self.page.locator('h2,h3,h4,dt,strong,span,p').evaluate_all("""els=>els.filter(e=>
            /^(?:creator|creators|description|details|credits)$/i.test(e.innerText.trim())
        ).slice(0,15).map(e=>({label:e.innerText.trim().toLowerCase(),ancestors:[e,e.parentElement,e.parentElement?.parentElement]
            .filter(Boolean).map(c=>({tag:c.tagName.toLowerCase(),classes:Array.from(c.classList)
                .filter(n=>/^[A-Za-z_][A-Za-z0-9_-]{0,80}$/.test(n)),attributes:Array.from(c.attributes).map(a=>a.name)}))}))""")
        cover = info.get('thumb_url')
        cover_shape = None
        if isinstance(cover,str):
            parsed = urlsplit(cover)
            if parsed.scheme=='https' and parsed.hostname and parsed.hostname.endswith('.tapas.io'):
                cover_shape = {'host':parsed.hostname,'path':parsed.path,'has_query':bool(parsed.query)}
        return {'authenticated':True,'source_series_id':str(info['id']),'title':info['title'],
                'metadata_shape':json_shape(info),'head_metadata':head,'cover_candidate':cover_shape,
                'series_content_flags':{key:info[key] for key in ('book','comic') if type(info.get(key)) is bool},
                'metadata_structure':structure,'section_markers':markers,
                'reader_or_comic_pages_requested':False}

    def diagnose_headless(self, url):
        """Test the same authenticated HTML flow in a fresh headless Firefox."""
        if not isinstance(url,str) or not re.fullmatch(r'https://tapas\.io/episode/[1-9][0-9]{0,19}',url):
            raise ArchiveError('Background test requires an exact observed episode URL.')
        from .tapas_http import TapasHTTPAdapter
        from ..archive import Store
        from .http_probe import probe_session
        direct_session = probe_session(self.context.cookies(),
                                       expected_account_id=getattr(self,'expected_account_id',None),
                                       network_guard=getattr(self,'network_guard',None))
        browser = None
        try:
            browser = self.driver.firefox.launch(headless=True, timeout=15000)
            context = browser.new_context(storage_state=self.context.storage_state(),service_workers='block')
            context.route('**/*', self.network_read_only_request)
            page = context.new_page()
            with TapasHTTPAdapter(transport=BrowserReadTransport(page, context, network_guard=getattr(self,'network_guard',None)),
                                  expected_account_id=getattr(self,'expected_account_id',None)) as adapter:
                manifest = adapter.capture(url)
                store = Store(Path("Library"))
                try:
                    folder = store.archive(manifest, adapter)
                finally:
                    store.close()
                return {"headless": True, "authenticated": True,
                        "page_count": len(manifest.episodes[0].pages),
                        "access_state": manifest.episodes[0].access,
                        "direct_http_session": direct_session,
                        "folder": str(folder), "browser_comic_preloads_blocked": True}
        except ArchiveError:
            raise
        except Exception:
            raise ArchiveError("Headless browser test failed; raw exceptions suppressed to protect credentials.") from None
        finally:
            if browser is not None:
                browser.close()

    supports_library_checkpoint=True

    def inspect_library(self, details=False, category="subscribed",on_page=None):
        if category=='free_episodes':
            before=self._inspect_library_details('subscribed',research=False)
            return self._inspect_free_episode_filter(before,max_pages=50,on_page=on_page,known_scope_only=True)
        if not details:
            return self.inspect_library_pages(category,200,on_page=on_page)
        observed = self._inspect_library_details(category)
        observed['free_episode_filter']=self._inspect_free_episode_filter(observed,max_pages=50)
        return observed

    def _inspect_free_episode_filter(self,before,max_pages=2,on_page=None,known_scope_only=False):
        """Bounded normal account-list navigation; no reader or unlock action."""
        from .library_navigation import reading_list_location, supplied_filter_location
        if type(max_pages) is not int or not 1<=max_pages<=50:
            raise ArchiveError('Free-filter inspection requires a page limit from 1 to 50.')
        start=self.page.url
        initial=reading_list_location(start)
        result={'authenticated':before.get('authenticated') is True,'complete':False,
                'initial_scope':before.get('scope'),'membership_verified':False,
                'filter_clicked':False,'restore_succeeded':None,'pages':[],
                'series':[],'reader_or_comic_pages_requested':False,'account_inventory_complete':False,
                'account_membership_verified':False,'pagination_complete':False,
                'duplicate_series_on_later_pages':0}
        if (before.get('authenticated') is not True or before.get('membership_verified') is not True
                or initial is None or initial['page']!=1 or initial['category']!=before.get('scope')):
            result['blocker']='Initial account-list scope is unverified; no filter was clicked.';return result
        controls=self.page.locator('nav.section-filter li.filter-item a.item-title')
        indices=controls.evaluate_all("""els=>els.map((e,index)=>({index,
            label:e.innerText.trim().toLowerCase().replace(/\\s+/g,' ')}))
            .filter(e=>e.label==='free episodes').map(e=>e.index)""")
        if (not isinstance(indices,list) or len(indices)>20
                or any(type(index) is not int or not 0<=index<100 for index in indices)):
            raise ArchiveError('Free-filter control structure is unsupported; no click was attempted.')
        result['matching_control_count']=len(indices)
        target=controls.nth(indices[0]) if len(indices)==1 else None
        if (target is None or target.count()!=1 or not target.is_visible()
                or re.sub(r'\s+',' ',target.inner_text().strip().lower())!='free episodes'):
            result['control_visible']=False
            result['blocker']='Free Episodes is hidden or ambiguous; no forced click was attempted.';return result
        result['control_visible']=True
        href=urljoin(start,target.get_attribute('href') or '')
        provided=supplied_filter_location(href)
        parent=target.locator('..');code=parent.get_attribute('data-type')
        if not isinstance(code,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}',code):
            code=provided['type'] if provided is not None else None
        if provided is None or code is None or code.upper()=='ALL' or provided['page']!=1:
            result['blocker']='The provided free-filter route or UI code is unverified; no click was attempted.';return result
        axes=[key for key in ('category','type') if provided[key].upper()==code.upper()]
        if len(axes)!=1:
            result['blocker']='The visible free-filter code does not identify one supplied query field; no click was attempted.';return result
        result['filter_query_field']=axes[0]
        # Only this supplied category/type representation has a live verified
        # navigation proof. Other named-control shapes retain the small probe.
        proven_shape=(axes==['category'] and provided['category']=='free_episodes'
                      and code.upper()=='FREE_EPISODES' and provided['type'].upper()=='ALL')
        if known_scope_only and not proven_shape:
            result['blocker']='The observed Free Episodes category/All route is unavailable; discovery did not activate an alternative.'
            return result
        page_limit=max_pages if proven_shape else min(max_pages,2)
        result['page_limit']=page_limit
        def public_scope(location):
            category=location['category']
            return category if category in {'subscribed','recent','updated','free','free_episodes','wuf','wait_until_free'} else None
        result['supplied_link_scope']=public_scope(provided)
        result['supplied_public_type_code']=code.upper() if code.upper() in {'FREE','FREE_EPISODES','WUF','WAIT_UNTIL_FREE'} else None
        observation=ReaderObservation();self.page.on('response',observation.on_response)
        moved=False;phase='filter_navigation';found={};seen=set()
        def selected():
            if (target.count()!=1 or not target.is_visible()
                    or re.sub(r'\s+',' ',target.inner_text().strip().lower())!='free episodes'):
                return False
            node=target.locator('..');classes=(node.get_attribute('class') or '').split()
            result['selection_structure']={'classes':[value for value in classes[:10]
                if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]{0,79}',value)],
                'aria_selected':node.get_attribute('aria-selected')=='true',
                'aria_current_page':node.get_attribute('aria-current')=='page'}
            return 'on' in classes
        def navigate(control):
            nonlocal moved
            moved=True
            with self.page.expect_navigation(wait_until='domcontentloaded',timeout=self.remaining_timeout()) as event:
                control.click(timeout=min(5000,self.remaining_timeout()))
                if control is target:result['filter_clicked']=True
            response=event.value
            if response is None or response.status!=200:
                raise ArchiveError('Normal account-list navigation was refused; no further page was requested.')
            self.wait_normal(1500)
            self.automatic_adapter().check_session()
            return supplied_filter_location(self.page.url)
        try:
            current=navigate(target)
            if current!=provided or not selected():
                result['blocker']='The free-filter page/selection is unverified; no pager was clicked.';return result
            result.update(membership_verified=True,scope=public_scope(current),
                          verified_filter='free_episodes',
                          public_type_code=code.upper() if code.upper() in {'FREE','FREE_EPISODES','WUF','WAIT_UNTIL_FREE'} else None)
            result['view_semantics']=self._reading_filter_semantics()
            for index in range(page_limit):
                candidates={card['source_series_id'] or card['source_url']:card for card in self._series_cards()}
                signature=tuple(sorted(candidates))
                result['duplicate_series_on_later_pages']+=len(set(candidates)&set(found))
                if not signature or signature in seen:
                    result['blocker']='Filtered rows are empty or repeated; completeness remains unverified.';break
                seen.add(signature);found.update(candidates)
                result['series']=list(found.values());result['observed_series_count']=len(found)
                result['pages'].append({'page':current['page'],'series_count':len(candidates)})
                if on_page is not None and proven_shape:
                    on_page({'authenticated':True,'membership_verified':True,'scope':'free_episodes',
                             'account_membership_verified':False,'account_inventory_complete':False,
                             'complete':False,'series':list(candidates.values()),'pages':list(result['pages'])})
                next_control=self.page.locator('section.paging-wrap .paging__button--next')
                if next_control.count()!=1 or not next_control.is_visible():
                    result['blocker']='The filtered Next control is unavailable or ambiguous.';break
                if 'disabled' in (next_control.get_attribute('class') or '').split() or next_control.get_attribute('aria-disabled')=='true':
                    result.update(complete=result['duplicate_series_on_later_pages']==0,
                                  pagination_complete=True,boundary_observed=True)
                    if not result['complete']:
                        result['blocker']='The view boundary was reached with repeated series across pages; snapshot membership remains unverified.'
                    break
                if index+1>=page_limit:
                    result['blocker']='The bounded filter inspection page limit was reached.';break
                location=supplied_filter_location(urljoin(self.page.url,next_control.get_attribute('href') or ''))
                if location is None or location['category']!=current['category'] or location['type']!=current['type'] or location['page']!=current['page']+1:
                    result['blocker']='Next does not preserve the provided filter/scope and adjacent page; no click was attempted.';break
                phase='filter_pager';actual=navigate(next_control)
                if actual!=location or not selected():
                    result['blocker']='Filtered page scope changed unexpectedly; inspection stopped.';break
                current=actual
            result['series']=list(found.values());result['observed_series_count']=len(found)
            return result
        except (AuthenticationError,RunLimit):raise
        except Exception as error:
            result['navigation_failure']={'phase':phase,'exception_class':type(error).__name__}
            result['blocker']='Normal free-filter navigation failed; no retry was attempted.';return result
        finally:
            result['normal_network']=observation.summary()
            if moved:
                try:
                    response=self.page.goto(start,wait_until='domcontentloaded',timeout=self.remaining_timeout())
                    result['restore_succeeded']=bool(response and response.status==200 and reading_list_location(self.page.url)==reading_list_location(start))
                except Exception:result['restore_succeeded']=False
            self.page.remove_listener('response',observation.on_response)

    def _series_cards(self):
        """Series links on the rendered list, each with its one tiara series ID or None."""
        rows=self.page.locator('ul.content-list-wrap li.content-item a[href*="/series/"]').evaluate_all("""els=>els.map(e=>{
            const ids=[];
            for(let p=e,d=0;p&&d<5;p=p.parentElement,d++) {
                const id=p.getAttribute('data-tiara-event-meta-series-id');if(id&&/^[1-9][0-9]{0,19}$/.test(id))ids.push(id);
            }
            return {url:e.href,title:e.innerText.trim().slice(0,200),ids:Array.from(new Set(ids))};
        })""")
        cards=[]
        for row in rows:
            parsed=urlsplit(row['url'])
            if (parsed.scheme=='https' and parsed.hostname=='tapas.io' and not parsed.username and not parsed.password and not parsed.port
                    and re.fullmatch(r'/series/[A-Za-z0-9_-]{1,100}(?:/info)?',parsed.path)):
                url=f'https://tapas.io{parsed.path}';ids=row.get('ids',[])
                identity=ids[0] if len(ids)==1 and isinstance(ids[0],str) and re.fullmatch(r'[1-9][0-9]{0,19}',ids[0]) else None
                cards.append({'source_series_id':identity,'title':row['title'],'source_url':url})
        return cards

    def _reading_filter_semantics(self):
        """Fixed displayed labels and attribute types only; never activate them."""
        vocabulary={'free episodes','free episode tickets','free tickets','ticket','tickets','claim','redeem',
                    'expires','expired','unlocked','purchased','gift','gifts','episode_count_label','ticket_count_label'}
        try:
            observed=self.page.locator('body').evaluate("""body=>{
                const known=['free episodes','free episode tickets','free tickets','ticket','tickets','claim','redeem',
                             'expires','expired','unlocked','purchased','gift','gifts'];
                const counts={},fields={},nodes=[...body.querySelectorAll('*')];
                const relevant=n=>/^data-[a-z0-9-]{1,60}$/.test(n)&&/ticket|gift|free|expir/.test(n)&&
                    !/auth|session|cookie|token|password|secret|user|email/.test(n);
                for(const e of nodes.slice(0,20000)){
                    for(const a of [...e.attributes])if(relevant(a.name)&&Object.keys(fields).length<40){
                        fields[a.name]=/^\\d+$/.test(a.value)?'integer':/^(true|false)$/.test(a.value)?'boolean':'string';
                    }
                    if(!e.getClientRects().length||getComputedStyle(e).visibility==='hidden')continue;
                    const text=(e.innerText||e.getAttribute('aria-label')||'').trim().replace(/\\s+/g,' ').toLowerCase();
                    let label=known.includes(text)?text:null;
                    if(/^\\d+\\s+(?:free\\s+)?episodes?$/.test(text))label='episode_count_label';
                    if(/^\\d+\\s+(?:free\\s+)?tickets?$/.test(text))label='ticket_count_label';
                    if(label)counts[label]=(counts[label]||0)+1;
                }
                return {node_count:nodes.length,truncated:nodes.length>20000,label_node_counts:counts,
                        attribute_value_types:fields};
            }""",timeout=min(5000,self.remaining_timeout()))
            if not isinstance(observed,dict):return {'available':False}
            result={'available':True,'truncated':observed.get('truncated') is True,
                    'label_node_counts':{},'attribute_value_types':{},'controls_activated':False,
                    'authorization_evidence':False}
            count=observed.get('node_count')
            if type(count) is int and 0<=count<=1000000:result['node_count']=count
            counts=observed.get('label_node_counts')
            if isinstance(counts,dict):
                result['label_node_counts']={key:value for key,value in counts.items()
                    if key in vocabulary and type(value) is int and 0<=value<=20000}
            fields=observed.get('attribute_value_types')
            if isinstance(fields,dict):
                result['attribute_value_types']={key:value for key,value in list(fields.items())[:40]
                    if isinstance(key,str) and re.fullmatch(r'data-[a-z0-9-]{1,60}',key)
                    and re.search(r'ticket|gift|free|expir',key)
                    and not re.search(r'auth|session|cookie|token|password|secret|user|email',key)
                    and isinstance(value,str) and value in {'integer','boolean','string'}}
            return result
        except (AuthenticationError,RunLimit):raise
        except Exception:return {'available':False,'controls_activated':False,'authorization_evidence':False}

    def inspect_library_pages(self,category='subscribed',max_pages=3,on_page=None):
        """Follow visible supplied Next links; list membership grants no access."""
        from .library_navigation import reading_list_location
        if type(max_pages) is not int or not 1<=max_pages<=200:
            raise ArchiveError('Reading-list page limit must be from 1 to 200.')
        before=self._inspect_library_details(category,research=False)
        start=self.page.url;current=reading_list_location(start)
        result={'authenticated':before['authenticated'],'membership_verified':before['membership_verified'],
                'scope':before['scope'],'complete':False,'account_inventory_complete':False,
                'pages':[],'series':[],'boundary_observed':False,'restore_succeeded':None,
                'reader_or_comic_pages_requested':False,'duplicate_series_on_later_pages':0,
                'blocker':'The reading-list page budget was reached.'}
        if not before['membership_verified'] or current is None or current['category']!=category or current['page']!=1:
            result['blocker']='Initial reading-list scope/page is unverified; no pager was clicked.';return result
        active=self.page.locator('nav.section-sort li.sort-item.on a.item-title')
        if active.count()!=1 or active.inner_text().strip().lower()!='all':
            result['blocker']='The initial All type selection is unverified; no pager was clicked.';return result
        found={};fingerprints=set();identities=set();moved=False
        observation=ReaderObservation();self.page.on('response',observation.on_response)
        try:
            for index in range(max_pages):
                candidates={card['source_url']:card for card in self._series_cards()}
                signature=tuple(sorted(candidates))
                if not signature or signature in fingerprints:
                    result['blocker']='The reading list is empty or repeats a prior page; completeness remains unverified.';break
                # A series seen on an earlier page means the list shifted while it was read and may have skipped one.
                result['duplicate_series_on_later_pages']+=sum(url in found or card['source_series_id'] in identities
                                                               for url,card in candidates.items())
                fingerprints.add(signature);found.update(candidates)
                identities.update(card['source_series_id'] for card in candidates.values() if card['source_series_id'])
                result['pages'].append({'page':current['page'],'series_count':len(candidates)})
                if on_page is not None:
                    on_page({'authenticated':True,'membership_verified':True,'scope':category,'complete':False,
                             'series':list(candidates.values()),'pages':list(result['pages'])})
                next_control=self.page.locator('section.paging-wrap .paging__button--next')
                if next_control.count()!=1 or not next_control.is_visible():
                    result['blocker']='The observed Next control is unavailable or ambiguous; no forced click was attempted.';break
                classes=(next_control.get_attribute('class') or '').split()
                if 'disabled' in classes or next_control.get_attribute('aria-disabled')=='true':
                    result['boundary_observed']=True
                    if result['duplicate_series_on_later_pages']:
                        result['blocker']='The boundary was reached with series repeated across pages; the list may have shifted and skipped one.'
                    elif any(card['source_series_id'] is None for card in found.values()):
                        result['blocker']='Some listed series have no single verified series ID; the list is not complete.'
                    else:
                        result.update(complete=True,blocker='Rendered All reading-list boundary observed; purchase/unlock reconciliation remains unverified.')
                    break
                if index+1>=max_pages:break
                href=urljoin(self.page.url,next_control.get_attribute('href') or '');location=reading_list_location(href)
                if location is None or location['category']!=category or location['page']!=current['page']+1:
                    result['blocker']='Next is not a supplied adjacent link in the verified reading-list scope; no click was attempted.';break
                phase='click'
                try:
                    with self.page.expect_navigation(wait_until='domcontentloaded',timeout=self.remaining_timeout()) as navigation:
                        next_control.click(timeout=min(5000,self.remaining_timeout()))
                    response=navigation.value;moved=True
                    phase='document'
                    if response is None or response.status!=200:
                        result['blocker']='Normal reading-list navigation was refused; retained pages are partial.';break
                    phase='load_delay';self.wait_normal(1500);actual=reading_list_location(self.page.url)
                    if actual!=location:
                        result['blocker']='The reading-list page/scope changed unexpectedly; retained pages are partial.';break
                    phase='identity';self.automatic_adapter().check_session();current=actual
                except (AuthenticationError,RunLimit):raise
                except Exception as error:
                    result['navigation_failure']={'phase':phase,'exception_class':type(error).__name__,
                                                  'observed_location':reading_list_location(self.page.url)}
                    if phase=='identity':raise
                    result['blocker']='Normal reading-list navigation failed; retained pages are partial.';break
            result['series']=list(found.values());result['observed_series_count']=len(found)
            result['unresolved_series_id_count']=sum(s['source_series_id'] is None for s in found.values())
            result['normal_network']=observation.summary();return result
        finally:
            try:
                if moved:
                    response=self.page.goto(start,wait_until='domcontentloaded',timeout=self.remaining_timeout())
                    result['restore_succeeded']=bool(response and response.status==200 and reading_list_location(self.page.url)==reading_list_location(start))
            except Exception:result['restore_succeeded']=False
            self.page.remove_listener('response',observation.on_response)

    def _catalog_dom_observation(self):
        """Count public catalog structure and fixed labels without exporting text."""
        frames=getattr(self.page,'frames',None)
        if not isinstance(frames,list):
            return {'available':False}
        result={'available':True,'frame_count':len(frames),'inspected_frames':[],
                'skipped_frame_count':0,'frame_limit':8}
        vocabulary={'free access','free','premium','wait until free','wuf','all'}
        tags={'A','BUTTON','DIV','H1','H2','H3','LABEL','LI','OPTION','P','SPAN'}
        roles={'button','option','tab','link','listitem','heading'}
        for frame in frames:
            try:
                parsed=urlsplit(frame.url)
                eligible=(parsed.scheme=='https' and parsed.hostname=='tapas.io'
                          and not parsed.username and not parsed.password and parsed.port in (None,443)
                          and re.fullmatch(r'/(?:menu/[0-9]{1,8}/subtab/[0-9]{1,8}|free-access|comics)?',parsed.path))
            except (TypeError,ValueError):eligible=False
            if not eligible or len(result['inspected_frames'])>=8:
                result['skipped_frame_count']+=1
                continue
            item={'main_frame':frame is getattr(self.page,'main_frame',None),'inspection_succeeded':False}
            result['inspected_frames'].append(item)
            try:
                observed=frame.locator(':root').evaluate("""root=>{
                    const known=['free access','free','premium','wait until free','wuf','all'];
                    const nodes=[...root.querySelectorAll('body *')], matches=[];
                    let visible=0;
                    for(const e of nodes.slice(0,20000)){
                        if(!e.getClientRects().length||getComputedStyle(e).visibility==='hidden')continue;
                        visible++;
                        const label=(e.innerText||e.getAttribute('aria-label')||'').trim().replace(/\\s+/g,' ').toLowerCase();
                        if(known.includes(label)&&matches.length<30){
                            matches.push({label,tag:e.tagName,role:e.getAttribute('role')});
                        }
                    }
                    return {node_count:nodes.length,visible_sample_node_count:visible,
                            sample_truncated:nodes.length>20000,labels:matches};
                }""",timeout=min(5000,self.remaining_timeout()))
                if not isinstance(observed,dict):continue
                for key in ('node_count','visible_sample_node_count'):
                    value=observed.get(key)
                    if type(value) is int and 0<=value<=1000000:item[key]=value
                item['sample_truncated']=observed.get('sample_truncated') is True
                item['labels']=[]
                rows=observed.get('labels',[])
                for row in rows[:30] if isinstance(rows,list) else []:
                    if not isinstance(row,dict) or row.get('label') not in vocabulary:continue
                    label={'label':row['label'],'tag':row.get('tag') if row.get('tag') in tags else 'other'}
                    if row.get('role') in roles:label['role']=row['role']
                    if label not in item['labels']:item['labels'].append(label)
                item['inspection_succeeded']=True
            except Exception:
                # Detached/unavailable frames are diagnostics, never a reason
                # to retry navigation or disclose browser exception details.
                pass
        return result

    def inspect_free_access(self):
        """Find supplied Home/Comics/Free Access links, never guess a menu ID."""
        automatic=self.automatic_adapter()
        if isinstance(self.transport,BrowserReadTransport) and not self.transport.blocking_preload:
            self.page.route('**/*',self.transport.block_comic_preload)
            self.transport.blocking_preload=True
        automatic.check_session()
        observation=ReaderObservation(catalog_filters=True)
        self.page.on('response',observation.on_response)
        visited=[]
        navigation=[]
        followed=False
        def navigate(url):
            response=self.page.goto(url,wait_until='domcontentloaded',timeout=self.remaining_timeout())
            status=getattr(response,'status',None)
            if status==401:
                raise AuthenticationError('Home navigation requires a fresh login.')
            if type(status) is int and status>=400:
                raise ArchiveError('Normal Home navigation was refused; no alternate route will be probed.')
            if urlsplit(self.page.url).path.startswith('/account/sign'):
                raise AuthenticationError('Home navigation requires a fresh login.')
            actual=urlsplit(self.page.url)
            if actual.scheme!='https' or actual.hostname!='tapas.io' or actual.username or actual.password:
                raise ArchiveError('Normal catalog navigation left Tapas; this inspection stopped.')
            visited.append(f'https://tapas.io{urlsplit(url).path}')
            self.wait_normal(2000)
        def links():
            rows=self.page.locator('a[href]').evaluate_all("""els=>{
                const known=['home','comics','novels','community','free access'];
                const text=e=>(e.innerText||e.getAttribute('aria-label')||'').trim().replace(/\\s+/g,' ').toLowerCase();
                return els.map(e=>{
                    const full=text(e);
                    const leaves=[...new Set([...e.querySelectorAll('span')]
                        .filter(n=>!n.children.length&&n.getClientRects().length).map(text).filter(t=>known.includes(t)))];
                    return {url:e.href,label:known.includes(full)?full:leaves.length===1?leaves[0]:null};
                }).filter(e=>known.includes(e.label));
            }""")
            valid=[]
            for row in rows:
                if not isinstance(row,dict) or row.get('label') not in {'home','comics','novels','community','free access'}:
                    continue
                parsed=urlsplit(row.get('url',''))
                if (parsed.scheme=='https' and parsed.hostname=='tapas.io' and not parsed.username and not parsed.password
                        and parsed.port in (None,443) and not parsed.fragment
                        and re.fullmatch(r'/(?:menu/[0-9]{1,8}/subtab/[0-9]{1,8}|free-access|comics)?',parsed.path)):
                    valid.append(row)
                    projected={'label':row['label'],'url':f'https://tapas.io{parsed.path}'}
                    if projected not in navigation:
                        navigation.append(projected)
            return valid
        try:
            navigate('https://tapas.io/')
            candidates=links()
            first_candidates=list(candidates)
            free=next((r for r in candidates if r['label']=='free access'),None)
            if free is None:
                home=next((r for r in candidates if r['label']=='home' and urlsplit(r['url']).path!='/'),None)
                if home:
                    navigate(home['url'])
                    candidates=links()
                    free=next((r for r in candidates if r['label']=='free access'),None)
            if free is None:
                comics=next((r for r in candidates+first_candidates if r['label']=='comics'
                             and f'https://tapas.io{urlsplit(r["url"]).path}' not in visited),None)
                if comics:
                    navigate(comics['url'])
                    candidates=links()
                    free=next((r for r in candidates if r['label']=='free access'),None)
            series=[]
            access_controls=[]
            if free and f'https://tapas.io{urlsplit(free["url"]).path}' not in visited:
                navigate(free['url'])
                followed=True
                for _ in range(3):
                    self.page.mouse.wheel(0,1200)
                    self.wait_normal(1500)
                rows=self.page.locator('a[href*="/series/"]').evaluate_all("""els=>els.map(e=>({url:e.href,title:e.innerText.trim().slice(0,120)}))""")
                for row in rows:
                    parsed=urlsplit(row.get('url',''))
                    if (parsed.scheme=='https' and parsed.hostname=='tapas.io' and not parsed.username and not parsed.password
                            and re.fullmatch(r'/series/[A-Za-z0-9_-]{1,100}(?:/info)?',parsed.path)):
                        value={'url':f'https://tapas.io{parsed.path}','title':row.get('title','')}
                        if value not in series:
                            series.append(value)
            if not followed:
                # A catalog may expose business-model filters as controls,
                # rather than links. Observe a fixed vocabulary without
                # clicking a filter, offer, unlock or payment control.
                rows=self.page.locator('button, label, option, [role="button"], [role="option"], [role="tab"], span').evaluate_all("""els=>els
                    .filter(e=>e.getClientRects().length&&(e.tagName!=='SPAN'||!e.children.length)).map(e=>{
                        const parent=e.tagName==='SPAN'?e.closest('button,label,[role="button"],[role="option"],[role="tab"]'):e;
                        return {tag:parent?parent.tagName:'SPAN',label_only:!parent,
                            label:(e.innerText||e.getAttribute('aria-label')||'').trim().replace(/\\s+/g,' ').toLowerCase()};
                    })""")
                for row in rows:
                    if isinstance(row,dict) and row.get('label') in {'free access','free','premium','wait until free','wuf','all'}:
                        tag=row.get('tag')
                        control={'label':row['label'],'tag':tag if tag in {'BUTTON','LABEL','OPTION','DIV','A','INPUT','SPAN'} else 'control'}
                        if row.get('label_only') is True:control['label_only']=True
                        if control not in access_controls:access_controls.append(control)
            return {'authenticated':True,'complete':False,'membership_verified':False,
                    'free_access_link_followed':followed,'visited_pages':visited,'navigation_links':navigation,
                    'visible_access_controls':access_controls,
                    'free_access_control_observed':any(c['label']=='free access' and not c.get('label_only') for c in access_controls),
                    'free_access_label_observed':any(c['label']=='free access' for c in access_controls),
                    'catalog_dom_observation':self._catalog_dom_observation(),
                    'visible_series_candidates':series,'normal_network':observation.summary(),
                    'reader_or_comic_pages_requested':False,
                    'blocker':'Free Access membership scope/pagination and episode authorization require validation.' if followed else
                              'The supplied Home/Comics navigation exposed no distinct supported Free Access link; no menu IDs were guessed.'}
        finally:
            self.page.remove_listener('response',observation.on_response)

    def inspect_library_filter(self,category='subscribed',content_type='comics'):
        """Exercise the observed reading-list type controls, then restore selection."""
        if type(category) is not str or type(content_type) is not str or category not in {'subscribed','recent','updated'} or content_type not in {'all','comics','novels'}:
            raise ArchiveError('Use a known reading-list category and content type.')
        before=self._inspect_library_details(category)
        selector='nav.section-sort ul.sort-wrap li.sort-item a.item-title'
        controls=self.page.locator(selector)
        structure=controls.evaluate_all("""els=>els.map(e=>({label:e.innerText.trim().toLowerCase(),
            selected:e.parentElement.classList.contains('on'),
            display:getComputedStyle(e).display,visibility:getComputedStyle(e).visibility,
            parent_display:getComputedStyle(e.closest('nav.section-sort')).display
        })).filter(e=>['all','comics','novels'].includes(e.label))""")
        original=next((row['label'] for row in structure if row['selected']),None)
        result={'authenticated':before['authenticated'],'complete':False,'scope':category,
                'membership_verified':False,'content_type':content_type,'filter_structure':structure,
                'initial_list_count':before['final_list_count'],'filter_clicked':False,
                'filter_restore_succeeded':None,'visible_series_candidates':[]}
        observation=ReaderObservation()
        self.page.on('response',observation.on_response)
        def control(label):
            target=controls.filter(has_text=re.compile('^'+label+'$',re.I))
            if target.count()!=1 or not target.is_visible():
                return None
            parsed=urlsplit(urljoin(self.page.url,target.get_attribute('href') or ''))
            observed_fragment={'all':'!/reading-list?type=','comics':'!/reading-list?type=comics','novels':'!/reading-list?type=books'}[label]
            if (parsed.scheme!='https' or parsed.hostname!='tapas.io' or parsed.username or parsed.password or parsed.port
                    or parsed.path!='/reading-list' or (parsed.fragment and unquote(parsed.fragment).lower()!=observed_fragment)):
                raise ArchiveError('The observed type control is not a normal reading-list link.')
            return target
        try:
            target=control(content_type)
            if target is None or original is None:
                result['blocker']='The observed type control is hidden/ambiguous or its original selection is unknown; no forced click was attempted.'
                return result
            target.click(timeout=min(5000,self.remaining_timeout()))
            result['filter_clicked']=True
            self.wait_normal(2000)
            for _ in range(3):
                self.page.mouse.wheel(0,1200)
                self.wait_normal(1500)
            active=self.page.locator('nav.section-sort ul.sort-wrap li.sort-item.on a.item-title')
            label=active.inner_text().strip().lower() if active.count()==1 else None
            actual=urlsplit(self.page.url)
            actual_category=dict(parse_qsl(actual.query)).get('category','UPDATED').lower()
            result['membership_verified']=(actual.hostname=='tapas.io' and actual.path=='/reading-list'
                                            and actual_category==category and label==content_type)
            result['active_filter']=label if label in {'all','comics','novels'} else None
            free_controls=self.page.locator('nav.section-filter li.filter-item a.item-title').filter(has_text=re.compile('^free episodes$',re.I))
            result['provided_free_episode_links']=[]
            if free_controls.count()==1 and free_controls.is_visible():
                from .library_navigation import supplied_filter_location
                supplied=supplied_filter_location(urljoin(self.page.url,free_controls.get_attribute('href') or ''))
                if supplied is not None and supplied['category']=='free_episodes' and supplied['type'].upper() in {'ALL','COMICS','BOOKS'}:
                    result['provided_free_episode_links']=[{'category':'free_episodes','content_type':supplied['type'].lower(),
                                                          'page':supplied['page'],'activated':False}]
            result['final_list_count']=self.page.locator('ul.content-list-wrap li.content-item').count()
            rows=self.page.locator('ul.content-list-wrap li.content-item a[href*="/series/"]').evaluate_all("""els=>els.map(e=>({
                url:e.href,title:e.innerText.trim().slice(0,120)
            }))""")
            found={}
            for row in rows:
                parsed=urlsplit(row['url'])
                if parsed.scheme=='https' and parsed.hostname=='tapas.io' and not parsed.username and not parsed.password and re.fullmatch(r'/series/[A-Za-z0-9_-]{1,100}(?:/info)?',parsed.path):
                    url=f'https://tapas.io{parsed.path}';found[url]={'url':url,'title':row['title']}
            result['visible_series_candidates']=list(found.values())
            result['normal_network']=observation.summary()
            result['blocker']='Filter scope is observed; complete membership pagination remains unverified.' if result['membership_verified'] else 'The normal click did not establish the requested type scope.'
            return result
        finally:
            try:
                if result['filter_clicked']:
                    try:
                        restore=control(original)
                        if restore is None:
                            raise ArchiveError('The original reading-list filter is unavailable.')
                        restore.click(timeout=min(5000,self.remaining_timeout()))
                        self.wait_normal(1500)
                        active=self.page.locator('nav.section-sort ul.sort-wrap li.sort-item.on a.item-title')
                        result['filter_restore_succeeded']=(active.count()==1 and active.inner_text().strip().lower()==original)
                    except Exception:
                        self.remaining_timeout()
                        result['filter_restore_succeeded']=False
            finally:
                result['normal_network']=observation.summary()
                self.page.remove_listener('response',observation.on_response)

    def inspect_account_navigation(self):
        """Read actual account links and history markup, without action clicks."""
        automatic=self.automatic_adapter()
        automatic.check_session()
        session_structure=getattr(automatic,'_session_structure',None)
        if not isinstance(session_structure,dict):session_structure=None
        observation = ReaderObservation()
        self.page.on("response", observation.on_response)
        visited = []
        links = []
        menu_controls = []
        navigation_issues = []
        history_obstruction = None
        navigation_urls = {}
        try:
            self.page.goto("https://tapas.io/", wait_until="domcontentloaded", timeout=self.remaining_timeout())
            self.wait_normal(2000)
            menu_rows = []
            menus = self.page.locator('button[aria-expanded][data-headlessui-state]')
            for index in range(min(menus.count(), 3)):
                menu = menus.nth(index)
                bounds = menu.bounding_box()
                if not menu.is_visible() or not bounds or not 0 <= bounds['y'] < 160:
                    continue
                # An observed ARIA disclosure toggles navigation only. No Ink,
                # purchase, unlock or payment control is selected.
                try:
                    menu.click(timeout=min(5000,self.remaining_timeout()))
                except Exception:
                    self.remaining_timeout()
                    navigation_issues.append('navigation_disclosure_obstructed')
                    continue
                self.wait_normal(500)
                menu_rows.extend(self.page.locator('a[href]').evaluate_all("""els=>els.map(e=>({
                    href:e.href,label:e.innerText.trim().toLowerCase()
                })).filter(e=>/ink|history|transaction|purchase|reading-list|account/.test(new URL(e.href).pathname))"""))
                menu_controls.extend(self.page.locator('a,button,[role="menuitem"]').evaluate_all("""els=>els.map(e=>{
                    const text=e.innerText.trim().toLowerCase();
                    const exact=['settings','transaction history','history','my account','series'].includes(text);
                    const u=e.tagName==='A'?new URL(e.href):null;
                    return {tag:e.tagName.toLowerCase(),label:exact?text:/ink/.test(text)?'ink control':null,
                            classes:Array.from(e.classList).filter(c=>/^[A-Za-z_][A-Za-z0-9_-]{0,80}$/.test(c)),
                            href_host:u?u.hostname:null,
                            href_path:u && /^\\/[A-Za-z_/-]{1,100}$/.test(u.pathname)?u.pathname:null,
                            query_keys:u?Array.from(u.searchParams.keys()).filter(k=>/^[A-Za-z_]{1,40}$/.test(k)):[],
                            known_query:u?Object.fromEntries(Array.from(u.searchParams).filter(([k,v])=>
                                /^[A-Za-z_]{1,40}$/.test(k) && /^(?:history|transaction|transactions|series|ink|account|settings)$/i.test(v))):{}};
                }).filter(e=>e.label!==null)"""))
                self.page.keyboard.press('Escape')
            for _ in range(3):
                rows = self.page.locator('a[href]').evaluate_all("""els=>els.map(e=>({
                    href:e.href,label:e.innerText.trim().toLowerCase()
                })).filter(e=>/ink|history|transaction|purchase|reading-list|account/.test(new URL(e.href).pathname))""")
                rows += menu_rows
                candidates = []
                for row in rows:
                    path = navigation_path(row['href'])
                    # Only actual UI links; an observed tracking-only trigger
                    # stays in memory. Payment/action URLs are excluded.
                    if path is None:
                        continue
                    label = row['label'] if row['label'] in {'ink','history','transaction history','series','reading list','reading-list'} else 'account navigation'
                    item = {'path': path, 'label': label}
                    if item not in links:
                        links.append(item)
                    navigation_urls[path] = row['href']
                    if path not in visited and re.search(r'history|transaction|ink|my-account', path, re.I):
                        candidates.append(path)
                next_path = next((p for p in candidates if re.search(r'history|transaction', p, re.I)), None)
                if next_path is None and not visited:
                    next_path = next((p for p in candidates if 'ink' in p.lower()), None)
                if next_path is None and not visited:
                    next_path = next((p for p in candidates if p == '/my-account'), None)
                if next_path is None:
                    break
                visited.append(next_path)
                self.page.goto(navigation_urls[next_path], wait_until='domcontentloaded', timeout=self.remaining_timeout())
                self.wait_normal(2000)
            raw = self.page.locator('a[href*="/series/"],a[href*="/episode/"]').evaluate_all("els=>els.map(e=>e.href)")
            history_clicked = False
            if '/profile/ink' in visited:
                # The normal Ink page can display a modal backdrop. Dismiss only
                # its observed close control; never force a click through it.
                close_controls = self.page.locator('a.close-btn.hide-overlay')
                for index in range(min(close_controls.count(), 3)):
                    close = close_controls.nth(index)
                    if close.is_visible():
                        try:
                            close.click(timeout=min(5000,self.remaining_timeout()))
                            self.wait_normal(500)
                        except Exception:
                            self.remaining_timeout()
                            navigation_issues.append('modal_close_obstructed')
                        break
                controls = self.page.locator('a,button,[role="tab"]').filter(
                    has_text=re.compile(r'^\s*(?:transaction\s+history|transactions|history)\s*$',re.I))
                for index in range(min(controls.count(), 3)):
                    control = controls.nth(index)
                    if control.is_visible():
                        try:
                            control.click(timeout=min(5000,self.remaining_timeout()))
                            self.wait_normal(2000)
                            history_clicked = True
                        except Exception:
                            self.remaining_timeout()
                            navigation_issues.append('history_control_obstructed')
                            bounds = control.bounding_box()
                            if bounds:
                                history_obstruction = self.page.evaluate("""({x,y})=>{
                                    const e=document.elementFromPoint(x,y);const chain=[];
                                    for(let p=e,d=0;p && d<4;p=p.parentElement,d++)chain.push({
                                        tag:p.tagName.toLowerCase(),classes:Array.from(p.classList)
                                            .filter(c=>/^[A-Za-z_][A-Za-z0-9_-]{0,80}$/.test(c))});
                                    return chain;
                                }""",{'x':bounds['x']+bounds['width']/2,'y':bounds['y']+bounds['height']/2})
                        break
            raw = self.page.locator('a[href*="/series/"],a[href*="/episode/"]').evaluate_all("els=>els.map(e=>e.href)")
            public_links = sorted({canonical_url(u) for u in raw if re.fullmatch(r'https://tapas\.io/(?:series/[A-Za-z0-9_-]{1,100}(?:/info)?|episode/[0-9]+)',u)})
            return {'authenticated':True, 'complete':False, 'visited_paths':visited,
                    'session_structure':session_structure,
                    'navigation':links, 'public_content_links':public_links,
                    'menu_controls':menu_controls,
                    'history_clicked':history_clicked,
                    'navigation_issues':navigation_issues,
                    'history_obstruction':history_obstruction,
                    'transaction_structure':self.page.locator('.tp-table.transactions').evaluate_all("""els=>els.map(e=>({
                        attributes:Array.from(e.attributes).map(a=>a.name),
                        children:Array.from(e.querySelectorAll('*')).slice(0,45).map(n=>({
                            tag:n.tagName.toLowerCase(),classes:Array.from(n.classList)
                                .filter(c=>/^[A-Za-z_][A-Za-z0-9_-]{0,80}$/.test(c)),
                            attributes:Array.from(n.attributes).map(a=>a.name),
                            has_text:!!n.innerText.trim(),child_count:n.children.length})),
                        public_link_count:e.querySelectorAll('a[href*="/series/"],a[href*="/episode/"]').length,
                        next_disabled:!!e.querySelector('.js-next-page.disabled')
                    }))"""),
                    'transaction_content_links':self.page.locator('.tp-table.transactions .contents .tr a[href]').evaluate_all("""els=>Array.from(new Set(els.map(e=>e.href).filter(u=>
                        /^https:\\/\\/tapas\\.io\\/(?:episode\\/[0-9]+|series\\/[A-Za-z0-9_-]{1,100}(?:\\/info)?)$/.test(u))))"""),
                    'transaction_row_count':self.page.locator('.tp-table.transactions .contents .tr').count(),
                    'history_controls':self.page.locator('a,button,[role="tab"],span').evaluate_all("""els=>els.filter(e=>
                        /^(?:transaction\\s+history|transactions|history|series|my\\s+ink)$/i.test(e.innerText.trim())
                    ).slice(0,15).map(e=>({tag:e.tagName.toLowerCase(),label:e.innerText.trim().toLowerCase(),
                        classes:Array.from(e.classList),attributes:Array.from(e.attributes).map(a=>a.name)}))"""),
                    'history_structure':self.page.locator('[class*="history"],[class*="paging"],[class*="pagination"],.cell.series,.cell.name,[data-type],button').evaluate_all("""els=>els.slice(0,60).map(e=>{
                        const chain=[];for(let p=e,d=0;p && d<3;p=p.parentElement,d++)chain.push({
                            tag:p.tagName.toLowerCase(),classes:Array.from(p.classList).filter(c=>/^[A-Za-z_][A-Za-z0-9_-]{0,80}$/.test(c)),
                            attributes:Array.from(p.attributes).map(a=>a.name),
                            numeric_attributes:Object.fromEntries(Array.from(p.attributes).filter(a=>/page|total|count|has-next|limit/.test(a.name)&&/^(?:true|false|[0-9]+)$/.test(a.value)).map(a=>[a.name,a.value]))});
                        const text=e.innerText.trim().toLowerCase();
                        return {chain,label:['series','history','transaction history','load more','next','previous'].includes(text)?text:null};
                    })"""),
                    'account_controls':self.page.locator('header [class],[class*="header"] [class],[class*="gnb"] [class],button').evaluate_all("""els=>els.slice(0,80).map(e=>({
                        tag:e.tagName.toLowerCase(), classes:Array.from(e.classList).filter(c=>/^[A-Za-z_][A-Za-z0-9_-]{0,80}$/.test(c)),
                        role:e.getAttribute('role'), attributes:Array.from(e.attributes).map(a=>a.name)
                    }))"""),
                    'table_row_count':self.page.locator('table tbody tr').count(),
                    'network':observation.summary(),
                    'blocker':'History coverage and pagination are not established; navigation evidence does not authorize comic retrieval.'}
        finally:
            self.page.remove_listener('response',observation.on_response)

    def inspect_transaction_history(self, max_pages=3,on_page=None,resume=False):
        if type(max_pages) is not int or not 1 <= max_pages <= 500:
            raise ArchiveError('History page limit must be from 1 to 500.')
        if type(resume) is not bool:
            raise ArchiveError('History resume must be an explicit boolean.')
        identity = self.account_identity()
        state = getattr(self,'_history_scan',None)
        if resume:
            if state is None or not state['ready'] or state['page'].is_closed():
                raise ArchiveError('No live history scan is available to resume. Start a fresh scan; restarting the browser loses its private cursor.')
            if state.get('refused_status'):
                raise ArchiveError('Tapas refused the retained history cursor. Start a fresh normal history scan; no retry or cursor replacement will be attempted.')
            if state.get('navigation_error'):
                raise ArchiveError('The retained history view could not navigate normally. Start a fresh history scan; no forced click will be attempted.')
            if identity != state['account_id']:
                raise AuthenticationError('The account changed since history navigation started; this scan cannot resume.')
        else:
            if state is not None and not state['page'].is_closed():
                state['page'].close()
            state = {'account_id':identity,'page':self.context.new_page(),'observation':ReaderObservation(),
                     'pages':[],'links':set(),'seen':set(),'needs_advance':False,
                     'advance_clicked':False,'finished':False,'ready':False,'refused_status':None}
            self._history_scan = state
        main_page, self.page = self.page, state['page']
        observation = state['observation']
        pages, links, seen = state['pages'], state['links'], state['seen']
        start_count = len(pages)
        def result(blocker):
            return {'authenticated':True,'complete':False,'observed_boundary':state['finished'],
                    'resume_available':not state['finished'] and not state.get('refused_status') and not state.get('navigation_error'),
                    'new_page_count':len(pages)-start_count,
                    'pages':list(pages),'content_links':sorted(links),
                    'network':observation.summary(),'blocker':blocker}
        self.page.on('response',observation.on_response)
        try:
            if not resume:
                navigation = self.inspect_account_navigation()
                if navigation.get('history_clicked') is not True:
                    self._history_scan = None
                    self.page.close()
                    raise ArchiveError('The normal transaction-history view could not be opened; inspect account navigation.')
                state['ready'] = True
            if state['finished']:
                return result('A rendered history boundary was observed; server totals and full purchase-inventory semantics remain unverified.')
            for _ in range(max_pages):
                if state['needs_advance'] and not self._advance_history(state):
                    if state.get('navigation_error'):
                        return result('Normal history navigation could not advance within its wait budget; partial discoveries are retained and a fresh scan is required.')
                    if state.get('refused_status'):
                        return result(f"Normal history navigation was refused (HTTP {state['refused_status']}); retained discoveries remain valid, but a fresh scan is required.")
                    return result('Normal history navigation stopped updating; observed series and the private browser cursor are retained.')
                rows = self.page.locator('.tp-table.transactions .contents .tr')
                current = self.page.locator('.tp-table.transactions .contents .tr a[href]').evaluate_all("""els=>Array.from(new Set(els.map(e=>e.href).filter(u=>
                    /^https:\\/\\/tapas\\.io\\/(?:episode\\/[0-9]+|series\\/[A-Za-z0-9_-]{1,100}(?:\\/info)?)$/.test(u))))""")
                prev = self.page.locator('.tp-table.transactions .js-prev-page')
                next_page = self.page.locator('.tp-table.transactions .js-next-page')
                older_disabled = prev.count()!=1 or 'disabled' in (prev.get_attribute('class') or '').split()
                newer_disabled = next_page.count()!=1 or 'disabled' in (next_page.get_attribute('class') or '').split()
                data = history_data(observation)
                private_rows = data.get('transactions') if isinstance(data.get('transactions'),list) else []
                private_ids = tuple(item.get('id') for item in private_rows if isinstance(item,dict))
                # Transaction IDs are used in memory solely to detect a repeated
                # page. Repeated series links alone are legitimate on many pages.
                signature = private_ids if private_ids and all(type(i) is int for i in private_ids) else (tuple(current),rows.count(),older_disabled,newer_disabled)
                if signature in seen:
                    return result('History navigation repeated; no completeness claim.')
                seen.add(signature)
                links.update(current)
                pagination = data.get('pagination')
                safe_pagination = {k:v for k,v in pagination.items() if k in {'page','total','max_limit','index','has_next','to_prev'}
                                   and type(v) in (int,bool)} if isinstance(pagination,dict) else {}
                source_types = sorted({item.get('source_type') for item in private_rows
                                       if isinstance(item,dict) and isinstance(item.get('source_type'),str)
                                       and re.fullmatch(r'[A-Za-z_]{1,40}',item['source_type'])})
                pages.append({'number':len(pages)+1,'row_count':rows.count(),'public_content_link_count':len(current),
                              'older_disabled':older_disabled,'newer_disabled':newer_disabled,
                              'server_pagination':safe_pagination,'source_types':source_types})
                state['finished'] = older_disabled
                state['needs_advance'] = not older_disabled
                state['previous_index'] = safe_pagination.get('index')
                if on_page is not None:
                    on_page({'pages':list(pages),'content_links':sorted(links),'authenticated':True,'complete':False})
                if older_disabled:
                    return result('A rendered history boundary was observed; server totals and full purchase-inventory semantics remain unverified.')
            return result('Bounded normal history navigation stopped at its page limit; resume can continue in this browser.')
        finally:
            self.page.remove_listener('response',observation.on_response)
            self.page = main_page

    def _advance_history(self, state):
        if not state['advance_clicked']:
            state['observation'].history_status = None
            try:
                self.page.locator('.tp-table.transactions .js-prev-page').click(timeout=min(5000,self.remaining_timeout()))
            except ArchiveError:
                raise
            except Exception:
                self.remaining_timeout()
                state['navigation_error'] = 'history_control_unavailable'
                return False
            state['advance_clicked'] = True
            self.wait_normal(2000)
        wait_until = time.monotonic()+8
        while True:
            status = getattr(state['observation'],'history_status',None)
            if status is None and state['advance_clicked']:
                prior = [r for r in state['observation'].responses if r['path']=='/profile/transactions']
                status = prior[-1]['status'] if prior else None
            if status == 401:
                raise AuthenticationError('Tapas requires a fresh login before account history can continue.')
            if type(status) is int and status != 200:
                state['refused_status'] = status
                return False
            pagination = history_data(state['observation']).get('pagination')
            updated = pagination.get('index') if isinstance(pagination,dict) else None
            if updated != state['previous_index'] and type(updated) is int:
                self.wait_normal(250)
                state['needs_advance'] = state['advance_clicked'] = False
                return True
            if time.monotonic() >= wait_until:
                state['navigation_error'] = 'history_cursor_stalled'
                return False
            self.wait_normal(250)

    def _inspect_library_details(self, category="subscribed", *, research=True):
        """Follow only library links the signed-in home page actually provides."""
        if category not in {"subscribed", "recent", "updated"}:
            raise ArchiveError("Use a subscribed, recent or updated reading-list category.")
        adapter = self.automatic_adapter()
        adapter.check_session()
        observation = ReaderObservation()
        self.page.on("response", observation.on_response)
        try:
            response = self.page.goto("https://tapas.io/", wait_until="domcontentloaded", timeout=self.remaining_timeout())
            self.wait_normal(2000)
            links = self.page.locator("a[href]").evaluate_all("""els => els.map(e => ({
                url:e.href,title:e.innerText.trim().slice(0,120)
            }))""")
            library_links = []
            for link in links:
                parsed = urlsplit(link["url"])
                if (parsed.scheme == "https" and parsed.hostname == "tapas.io"
                        and re.search(r"library|subscribed|subscription|reading-list|bookmark|recent|history", parsed.path, re.I)):
                    value = {"url": f"https://tapas.io{parsed.path}", "title": link["title"]}
                    if value not in library_links:
                        library_links.append(value)
            library = next((link for link in library_links if "library" in link["url"]), None)
            if library is None:
                library = next((link for link in library_links if "/reading-list" in link["url"]), None)
            if library is not None:
                response = self.page.goto(library["url"], wait_until="domcontentloaded", timeout=self.remaining_timeout())
                self.wait_normal(2000)
            tabs = self.page.locator('a,button,[role="tab"]').evaluate_all("""els => els.map(e=>({
                tag:e.tagName.toLowerCase(),role:e.getAttribute('role'),title:e.innerText.trim(),
                href:e.tagName==='A' ? e.href : null
            })).filter(e=>/^(updated|subscribed|subscriptions|recent|unlocked|purchased|favorites|all)$/i.test(e.title))""")
            selected_tab = next((tab for tab in tabs if tab["title"].lower() == category and tab.get("href")), None)
            if selected_tab is not None:
                parsed = urlsplit(selected_tab["href"])
                valid_query = parsed.query == f"category={category.upper()}" or (category == "updated" and not parsed.query)
                if parsed.scheme == "https" and parsed.hostname == "tapas.io" and parsed.path == "/reading-list" and valid_query:
                    response = self.page.goto(selected_tab["href"], wait_until="domcontentloaded", timeout=self.remaining_timeout())
                    self.wait_normal(2000)
                    library = {"url": selected_tab["href"]}
            initial_list_count = self.page.locator('ul.content-list-wrap li.content-item').count()
            if (research or category=='recent') and library is not None and urlsplit(library["url"]).path == "/reading-list":
                for _ in range(3):
                    # Normal infinite-list navigation only; no comic readers or
                    # subscription/unlock/payment controls are selected.
                    entries = self.page.locator('ul.content-list-wrap li.content-item')
                    if entries.count():
                        entries.last.scroll_into_view_if_needed(timeout=5000)
                        bounds = entries.last.bounding_box()
                        if bounds:
                            self.page.mouse.move(bounds['x'] + bounds['width'] / 2, bounds['y'] + bounds['height'] / 2)
                    self.page.mouse.wheel(0, 1200)
                    self.wait_normal(1500)
            final_list_count = self.page.locator('ul.content-list-wrap li.content-item').count()
            if not research:
                from .library_navigation import reading_list_location
                location=reading_list_location(self.page.url)
                verified=location is not None and location['category']==category and location['page']==1
                return {'authenticated':True,'membership_verified':verified,
                        'scope':category if verified else 'unknown',
                        'initial_list_count':initial_list_count,'final_list_count':final_list_count}
            library_structure = self.page.locator('ul.content-list-wrap').evaluate_all("""els=>els.map(e=>{
                const nodes=[];
                for(let p=e,d=0;p && d<4;p=p.parentElement,d++) {
                    nodes.push({tag:p.tagName.toLowerCase(),classes:Array.from(p.classList),
                        attribute_names:Array.from(p.attributes).map(a=>a.name),
                        counts:Object.fromEntries(Array.from(p.attributes).filter(a=>
                            /page|total|count|has-next|limit/.test(a.name) && /^(true|false|[0-9]+)$/.test(a.value)
                        ).map(a=>[a.name,a.value]))});
                }
                return nodes;
            })""")
            frameworks = self.page.evaluate("""()=>({angular:typeof window.angular,jquery:typeof window.jQuery,
                directives:Array.from(document.querySelectorAll('[ng-app],[data-ng-app],[ng-controller],[data-ng-controller],[ng-view],[data-ng-view]'))
                    .slice(0,12).map(e=>({tag:e.tagName.toLowerCase(),attributes:Object.fromEntries(Array.from(e.attributes)
                        .filter(a=>/^(?:data-)?ng-(?:app|controller|view)$/.test(a.name) && /^[A-Za-z0-9_ .-]{0,80}$/.test(a.value))
                        .map(a=>[a.name,a.value]))}))})""")
            frontend_bindings = self.page.evaluate("""()=>{
                const globals=Object.keys(window).filter(k=>/tapas|tapastic|panda|library/i.test(k))
                    .filter(k=>/^[A-Za-z_][A-Za-z0-9_]{0,50}$/.test(k));
                const global_keys=Object.fromEntries(globals.map(k=>[k,
                    window[k] && ['object','function'].includes(typeof window[k]) ?
                    Object.keys(window[k]).filter(n=>/^[A-Za-z_][A-Za-z0-9_]{0,50}$/.test(n)).slice(0,40) : []]));
                const library_keys=Object.fromEntries(globals.map(k=>[k,
                    window[k] && ['object','function'].includes(typeof window[k]) ?
                    Object.keys(window[k]).filter(n=>/^[A-Za-z_][A-Za-z0-9_]{0,50}$/.test(n) &&
                        /library|reading|subscr|bookmark|recent/i.test(n)).slice(0,40) : []]));
                const events=[];
                if(window.jQuery && window.jQuery._data) {
                    const nodes=[window,document,...document.querySelectorAll('ul.content-list-wrap,.global-page')];
                    for(const node of nodes) {
                        const bindings=window.jQuery._data(node,'events') || {};
                        events.push({target:node===window?'window':node===document?'document':node.tagName.toLowerCase(),
                            event_names:Object.keys(bindings),
                            handler_names:Object.values(bindings).flat().map(b=>b.handler && b.handler.name || '')
                                .filter(n=>/^[A-Za-z0-9_]{0,50}$/.test(n)).slice(0,20)});
                    }
                }
                return {global_keys,library_keys,events};
            }""")
            library_navigation = self.page.locator('a[href*="reading-list"],button').evaluate_all("""els=>els.map(e=>({
                tag:e.tagName.toLowerCase(),title:e.innerText.trim().slice(0,60),
                href:e.tagName==='A' ? e.href : null,classes:Array.from(e.classList),
                attribute_names:Array.from(e.attributes).map(a=>a.name),
                ancestors:[e.parentElement,e.parentElement?.parentElement,e.parentElement?.parentElement?.parentElement]
                    .filter(Boolean).map(p=>({tag:p.tagName.toLowerCase(),classes:Array.from(p.classList),
                        attribute_names:Array.from(p.attributes).map(a=>a.name),
                        controls:Array.from(p.children).filter(c=>c.tagName==='BUTTON'||c.getAttribute('role')==='button')
                            .map(c=>({tag:c.tagName.toLowerCase(),classes:Array.from(c.classList),
                                attribute_names:Array.from(c.attributes).map(a=>a.name)}))}))
            })).filter(e=>e.tag==='a' || e.classes.some(c=>/page|more|next|prev/i.test(c)))""")
            for link in tabs + library_navigation:
                if link.get("href"):
                    parsed = urlsplit(link["href"])
                    link['href_scheme']=parsed.scheme if parsed.scheme in {'https','http','javascript'} else 'other'
                    link['fragment_present']=bool(parsed.fragment)
                    fragment=parsed.fragment.lower()
                    link['fragment']=fragment if fragment in {'all','comic','comics','novel','novels'} else None
                    from .reader import navigation_fragment_shape
                    link['fragment_shape']=navigation_fragment_shape(parsed.fragment)
                    query = [(key, value) for key, value in parse_qsl(parsed.query)
                             if (key == "category" and value in {"SUBSCRIBED", "RECENT", "UPDATED"})
                             or (re.fullmatch(r"[A-Za-z_]{1,24}", key) and value in {"ALL", "COMIC", "COMICS", "NOVEL", "NOVELS"})
                             or (key in {"page", "max_limit"} and value.isascii() and value.isdigit())]
                    link["href"] = f"https://tapas.io{parsed.path}" + (f"?{urlencode(query)}" if query else "") if parsed.hostname == "tapas.io" else None
            pagination_controls = self.page.locator('button,a,[class*="more"],[class*="paging"]').evaluate_all("""els=>els.filter(e=>
                /load more|show more|next/i.test(e.innerText.trim()) && e.innerText.trim().length<60
            ).slice(0,10).map(e=>({tag:e.tagName.toLowerCase(),title:e.innerText.trim(),
                classes:Array.from(e.classList).filter(c=>/^[A-Za-z_][A-Za-z0-9_-]{0,79}$/.test(c))}))""")
            # Icon-only pagers are absent from text-based discovery. Observe
            # their structure without exposing query values or activating them.
            pagination_structure=self.page.locator('[class*="paging"],[class*="pagination"],[class*="pager"]').evaluate_all("""els=>els.slice(0,12).map(e=>({
                tag:e.tagName.toLowerCase(),classes:Array.from(e.classList).filter(c=>/^[A-Za-z_][A-Za-z0-9_-]{0,79}$/.test(c)),
                attribute_names:Array.from(e.attributes).map(a=>a.name),
                display:getComputedStyle(e).display,visibility:getComputedStyle(e).visibility,
                controls:Array.from(e.querySelectorAll('a,button,[role="button"]')).slice(0,20).map(c=>({
                    tag:c.tagName.toLowerCase(),classes:Array.from(c.classList).filter(n=>/^[A-Za-z_][A-Za-z0-9_-]{0,79}$/.test(n)),
                    attribute_names:Array.from(c.attributes).map(a=>a.name),
                    label_kind:/^(?:next|previous|prev|first|last)$/i.test((c.innerText||'').trim())?(c.innerText||'').trim().toLowerCase():
                        (/^[0-9]+$/.test((c.innerText||'').trim())?'numeric':((c.innerText||'').trim()?'other_text':'icon_or_empty')),
                    disabled:c.disabled===true||c.getAttribute('aria-disabled')==='true'||c.classList.contains('disabled'),
                    display:getComputedStyle(c).display,visibility:getComputedStyle(c).visibility
                }))
            }))""")
            pager_links=[]
            for row in self.page.locator('section.paging-wrap a.paging__button').evaluate_all("""els=>els.map(e=>({
                href:e.href,classes:Array.from(e.classList),label:e.innerText.trim(),
                display:getComputedStyle(e).display,visibility:getComputedStyle(e).visibility
            }))"""):
                parsed=urlsplit(row['href']);query=parse_qsl(parsed.query,keep_blank_values=True)
                safe=[]
                for key,value in query:
                    if ((key=='category' and value in {'SUBSCRIBED','RECENT','UPDATED'}) or
                            (key in {'page','pageNumber','max_limit'} and re.fullmatch(r'[0-9]{1,4}',value)) or
                            (key=='type' and value in {'','ALL','COMICS','BOOKS'})):
                        safe.append((key,value))
                supported=(parsed.scheme=='https' and parsed.hostname=='tapas.io' and not parsed.username and not parsed.password
                           and not parsed.port and parsed.path=='/reading-list' and not parsed.fragment and len(safe)==len(query))
                pager_links.append({'classes':[c for c in row['classes'] if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]{0,79}',c)],
                    'label':row['label'] if re.fullmatch(r'[0-9]{1,4}',row['label']) else None,
                    'display':row['display'],'visibility':row['visibility'],
                    'url':row['href'] if supported else None,
                    'path':parsed.path if re.fullmatch(r'/reading-list/?',parsed.path) else None,
                    'fragment_present':bool(parsed.fragment),
                    'known_query_values':{key:value for key,value in query if
                        (key in {'page','pageNumber','max_limit'} and re.fullmatch(r'[0-9]{1,4}',value)) or
                        (key in {'category','type'} and value.upper() in {'','SUBSCRIBED','RECENT','UPDATED','ALL','COMIC','COMICS','BOOK','BOOKS','NOVEL','NOVELS'})},
                    'query_names':[key for key,value in query if re.fullmatch(r'[A-Za-z_]{1,40}',key)]})
            series_links = self.page.locator('a[href*="/series/"]').evaluate_all("""els => els.map(e => {
                const ancestors=[];
                for(let p=e.parentElement,d=0;p && d<4;p=p.parentElement,d++) {
                    ancestors.push({tag:p.tagName.toLowerCase(),classes:Array.from(p.classList)
                        .filter(c=>/^[A-Za-z_][A-Za-z0-9_-]{0,79}$/.test(c)),
                        numeric_attributes:Object.fromEntries(Array.from(p.attributes)
                            .filter(a=>['data-series-id','data-episode-id','data-tiara-event-meta-series-id'].includes(a.name) && /^[0-9]+$/.test(a.value))
                            .map(a=>[a.name,a.value]))});
                }
                return {url:e.href,title:e.innerText.trim().slice(0,120),ancestors};
            })""")
            candidates = {}
            for link in series_links:
                parsed = urlsplit(link["url"])
                if parsed.hostname == "tapas.io" and re.fullmatch(r"/series/[A-Za-z0-9_-]{1,100}(?:/info)?", parsed.path):
                    safe_url = f"https://tapas.io{parsed.path}"
                    candidates[safe_url] = link | {"url": safe_url}
            routes = []
            inline_routes = []
            inline_modules = []
            document_scripts = []
            script_contexts = []
            if response is not None:
                html = response.text()
                parser = ReaderHTML("unused")
                parser.feed(html)
                from .reader import script_routes, public_script_contexts
                inline_routes = sorted({route for script in parser.inline for route in script_routes(script)})
                inline_modules = sorted({match[0] for script in parser.inline for match in re.finditer(
                    r'\bpandaWeb\.[A-Za-z_][A-Za-z0-9_]{0,50}',script)})[:40]
                for src in parser.scripts[:40]:
                    parsed = urlsplit(src)
                    if parsed.hostname and (parsed.hostname=='tapas.io' or parsed.hostname.endswith('.tapas.io')):
                        document_scripts.append({'host':parsed.hostname,'path':parsed.path,'has_query':bool(parsed.query)})
                scripts = [value for value in parser.scripts if urlsplit(value).hostname == "us-a.tapas.io"
                           and re.match(r"/resources/js/(?:tapas|panda)\.min\.", urlsplit(value).path) and value.endswith(".js")]
                for script_url in scripts[:2]:
                    from .reader import script_routes
                    script = self.transport.assets.get(script_url)
                    if len(script) <= 4 * 1024 * 1024:
                        text = script.decode("utf-8")
                        routes.extend(script_routes(text))
                        script_contexts.extend(public_script_contexts(text,urlsplit(script_url).path))
            visited = urlsplit(library["url"]) if library else None
            actual_scope = "unknown"
            if visited and visited.scheme == "https" and visited.hostname == "tapas.io" and visited.path == "/reading-list":
                actual_scope = {"": "updated", "category=UPDATED": "updated", "category=SUBSCRIBED": "subscribed", "category=RECENT": "recent"}.get(visited.query, "unknown")
            verified_scope = actual_scope == category
            return {"authenticated": True, "complete": False, "membership_verified": verified_scope,
                    "scope": actual_scope if verified_scope else "unknown",
                    "initial_list_count": initial_list_count, "final_list_count": final_list_count,
                    "library_structure": library_structure, "library_navigation": library_navigation,
                    "frontend_frameworks": frameworks,
                    "frontend_bindings": frontend_bindings,
                    "library_links": library_links, "visited_library_url": library["url"] if library else None,
                    "visible_series_candidates": list(candidates.values()),
                    "visible_library_tabs": tabs,
                    "pagination_controls": pagination_controls,
                    "pagination_structure": pagination_structure,
                    "observed_pager_links":pager_links,
                    "normal_network": observation.summary(), "published_script_routes": sorted(set(routes)),
                    "published_inline_script_routes": inline_routes,
                    "published_inline_modules": inline_modules,
                    "document_script_assets": document_scripts,
                    "published_library_script_contexts": script_contexts}
        finally:
            self.page.remove_listener("response", observation.on_response)

    def scroll_episode(self, url):
        """Bounded normal scrolling, restricted to an episode container when found."""
        episode_id = url.rsplit("/", 1)[1]
        images = self.page.locator(f"#episode-{episode_id} img.content__img")
        count = images.count()
        if type(count) is int and 0 < count <= 500:
            # Reader content can scroll inside an element rather than window.
            # Let Playwright scroll each image's actual scroll ancestors.
            for index in range(count):
                images.nth(index).scroll_into_view_if_needed(timeout=5000)
                self.page.wait_for_timeout(350)
            self.page.wait_for_timeout(1500)
            return
        for step in range(180):
            bounds = self.page.evaluate("""episodeId => {
                const names = ['data-episode-id','data-id','data-episode','data-chapter-id','data-eid'];
                let container = null;
                for (const name of names) {
                    const matches = Array.from(document.querySelectorAll(`[${name}="${episodeId}"]`));
                    container = matches.find(e => e.querySelector('img'));
                    if (container) break;
                }
                if (!container) container = Array.from(document.querySelectorAll('[id]')).find(e =>
                    new RegExp('^(?:episode|chapter|content)[_-]?' + episodeId + '$').test(e.id) && e.querySelector('img'));
                return {scoped:!!container,
                    bottom:container ? container.getBoundingClientRect().bottom + window.scrollY : 0,
                    viewportBottom:window.scrollY + window.innerHeight};
            }""", episode_id)
            if bounds["scoped"] and bounds["viewportBottom"] >= bounds["bottom"] - 10:
                break
            if not bounds["scoped"] and step >= 8:
                break  # Need structure evidence before scrolling through adjacent episodes.
            self.page.mouse.wheel(0, 900)
            self.page.wait_for_timeout(350)
        self.page.wait_for_timeout(1500)

    def reader_rows(self, selector):
        return self.page.locator(selector).evaluate_all("""els => els.map(e => ({
            tag:e.tagName,url:e.currentSrc || e.src,loaded:e.complete && e.naturalWidth>0,
            sources:{src:e.getAttribute('src') || '', 'data-src':e.getAttribute('data-src') || ''}
        }))""")

    def observe_initial_reader(self, url):
        """Fresh normal navigation; examine provided sources before scrolling."""
        verified = self.episode_access(url)
        if (verified.get('source_episode_id') != url.rsplit('/',1)[1]
                or verified.get('access_state') not in {'tapas_free','tapas_unlocked'}):
            raise ArchiveError('Reader inspection requires verified access to this exact episode.')
        observer = ReaderObservation()
        self.page.on("response", observer.on_response)
        try:
            response = self.page.goto(url, wait_until="domcontentloaded")
            selector = f"#episode-{url.rsplit('/', 1)[1]} img.content__img"
            # Reader HTML is server-rendered in the observed flow. Give normal
            # scripts a brief opportunity to finish without waiting on trackers.
            self.page.wait_for_timeout(1500)
            rows = self.reader_rows(selector)
            result = {"dom_before_scroll": source_summary(rows), 'access_state':verified['access_state']}
            if response is not None:
                html = response.text()
                if isinstance(html, str) and len(html) <= 8 * 1024 * 1024:
                    parser = ReaderHTML(url.rsplit('/', 1)[1])
                    parser.feed(html)
                    result["initial_html"] = source_summary(parser.images)
                    result['provided_episode_navigation']=parser.navigation_summary()
                    result["episode_data_attributes"] = parser.attributes
                    try:
                        result["safe_inline_primitives"] = safe_primitives("\n".join(parser.inline))
                    except MetadataError:
                        result["safe_inline_primitives"] = {"series_ids_disagree": True}
                    result["script_assets"] = [{"host": urlsplit(value).hostname,
                                                "path": urlsplit(value).path}
                                               for value in parser.scripts if value.endswith(".js")][:30]
            result["normal_network"] = observer.summary()
            result["provided_metadata"] = self.summarize_provided_metadata(observer.values)
            return result
        finally:
            self.page.remove_listener("response", observer.on_response)

    def summarize_provided_metadata(self, values):
        """Allowlisted comic metadata and access flags; no account identity values."""
        result = {}
        for path, response in values.items():
            if not isinstance(response, dict):
                continue
            data = response.get("data")
            if not isinstance(data, dict):
                continue
            if path == "/who-am-i":
                user = data.get("user")
                result["session"] = {"authenticated": isinstance(user, dict) and type(user.get("id")) is int and user["id"] > 0}
            elif re.fullmatch(r"/episode/[0-9]+/info", path):
                result["episode"] = {key: data[key] for key in (
                    "id", "title", "scene", "publish_date", "free", "unlocked", "must_pay", "scheduled", "book", "free_access", "closing_date") if key in data}
            elif re.fullmatch(r"/series/[0-9]+", path):
                result["series"] = {key: data[key] for key in ("id", "title", "url", "comic", "book", "premium", "subscribed") if key in data}
            elif re.fullmatch(r"/series/[0-9]+/episodes", path):
                pagination = data.get("pagination", {})
                result["pagination"] = {key: pagination[key] for key in (
                    "page", "since", "has_next", "sort", "total", "limit", "web", "to_prev", "index") if key in pagination}
                episodes = data.get("episodes", [])
                if isinstance(episodes, list):
                    result["listed_episodes"] = [{key: item[key] for key in (
                        "id", "scene", "title", "free", "unlocked", "must_pay", "scheduled", "book", "free_access") if key in item}
                        for item in episodes[:20] if isinstance(item, dict)]
        return result

    def inspect_episode(self, url, interactive=True, details=False):
        if not interactive and not details:
            adapter = self.automatic_adapter()
            manifest = adapter.capture(url)
            episode = manifest.episodes[0]
            return {'authenticated': True, 'access_verified': True,
                    'access_state': episode.access, 'source_url': episode.source_url,
                    'authorization': dict(adapter.episode_info),
                    'source_series_id': manifest.series.platform_id, 'series_title': manifest.series.title,
                    'source_episode_id': episode.id, 'title': episode.title,
                    'episode_order': episode.order, 'published_at': episode.published_at,
                    'page_count': len(episode.pages), 'page_manifest_available': True,
                    'scrolling_required': False}
        url = canonical_url(url)
        if not re.fullmatch(r"https://tapas\.io/episode/[0-9]+", url or ""):
            raise ArchiveError("Use an exact https://tapas.io/episode/<numeric-id> URL.")
        initial = None
        if not interactive:
            initial = self.observe_initial_reader(url)
            verified = {'access_state':initial['access_state']}
        else:
            verified = self.episode_access(url)
            if (verified.get('source_episode_id') != url.rsplit('/',1)[1]
                    or verified.get('access_state') not in {'tapas_free','tapas_unlocked'}):
                raise ArchiveError('Reader inspection requires verified access to this exact episode.')
            self.page.goto(url)
        if interactive:
            input("Scroll through the episode normally and wait for its images to load. Then press Enter here: ")
        else:
            # Complete lazy sources are enough for inspection. Avoid making the
            # browser fetch every original image just to discover its URL.
            before = self.reader_rows(f"#episode-{url.rsplit('/', 1)[1]} img.content__img")
            if ordered_sources(before) is None:
                self.scroll_episode(url)
                initial["automatic_scroll_used"] = True
            else:
                initial["automatic_scroll_used"] = False
        if canonical_url(self.page.url) != url:
            raise ArchiveError("Reader navigated away from the requested episode.")
        rows = self.page.locator("img").evaluate_all(
            """els => els.map(e => {
                const safeClass = c => /^[A-Za-z_][A-Za-z0-9_-]{0,79}$/.test(c);
                const ancestors = [];
                let parent = e.parentElement;
                for (let depth = 0; parent && depth < 8; depth++, parent = parent.parentElement) {
                    const attributes = {};
                    for (const name of ['data-id','data-episode-id','data-episode','data-chapter-id','data-eid','data-series-id']) {
                        const value = parent.getAttribute(name);
                        if (value && /^[0-9]+$/.test(value)) attributes[name] = value;
                    }
                    if (/^(?:(?:episode|chapter|content)[_-]?)?[0-9]+$/.test(parent.id)) attributes.id = parent.id;
                    ancestors.push({tag:parent.tagName.toLowerCase(),
                        classes:Array.from(parent.classList).filter(safeClass), attributes});
                }
                const sources = {};
                for (const name of ['src','data-src','data-original','data-url']) {
                    const value = e.getAttribute(name);
                    if (value) {
                        try {sources[name] = new URL(value, location.href).href;} catch {}
                    }
                }
                return {url:e.currentSrc || e.src,loaded:e.complete && e.naturalWidth>0,
                    width:e.naturalWidth,height:e.naturalHeight,classes:Array.from(e.classList),ancestors,sources};
            })""")
        candidates = []
        for row in rows:
            parsed = urlsplit(row["url"])
            # This is a candidate heuristic from observed public CDN paths,
            # not authorization evidence or a verified Tapas manifest parser.
            if parsed.path.startswith(COMIC_PATH_PREFIXES):
                try:
                    asset_url(row["url"])
                except ArchiveError:
                    continue
                candidates.append(row)
        class_counts = Counter(c for row in candidates for c in set(row["classes"])
                               if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,79}", c))
        selectors = []
        for cls, count in class_counts.most_common(5):
            selectors.append({"selector": f"img.{cls}", "candidate_matches": count,
                              "all_image_matches": sum(cls in row["classes"] for row in rows)})
        episode_id = url.rsplit("/", 1)[1]
        def belongs_to_requested_episode(row):
            for ancestor in row.get("ancestors", []):
                attrs = ancestor["attributes"]
                if attrs.get("id") in {episode_id, f"episode-{episode_id}", f"episode_{episode_id}"}:
                    return True
                if any(attrs.get(name) == episode_id for name in ("data-id", "data-episode-id", "data-episode", "data-chapter-id", "data-eid")):
                    return True
            return False
        episode_rows = [row for row in rows if belongs_to_requested_episode(row) and "content__img" in row["classes"]]
        source_summaries = {}
        for name in ("current", "src", "data-src", "data-original", "data-url"):
            values = [row["url"] if name == "current" else row.get("sources", {}).get(name, "") for row in episode_rows]
            schemes = Counter(urlsplit(v).scheme or "empty" for v in values)
            hosts = Counter(urlsplit(v).hostname for v in values if urlsplit(v).scheme == "https")
            source_summaries[name] = {"schemes": dict(schemes), "https_hosts": dict(hosts)}
        scoped = {}
        structures = {}
        for row in candidates:
            ancestors = row.get("ancestors", [])
            key = repr(ancestors)
            if key not in structures:
                structures[key] = {"candidate_count": 0, "ancestors": ancestors}
            structures[key]["candidate_count"] += 1
            for ancestor in ancestors:
                for name, value in ancestor["attributes"].items():
                    exact = value == episode_id or (name == "id" and re.fullmatch(rf"(?:episode|chapter|content)[_-]?{episode_id}", value))
                    if not exact or name == "data-series-id":
                        continue
                    prefix = f'[{name}="{value}"]'
                    for cls in class_counts:
                        if cls in row["classes"]:
                            selector = f"{prefix} img.{cls}"
                            if selector not in scoped:
                                matched = self.page.locator(selector).evaluate_all(
                                    "els => ({count:els.length,loaded:els.filter(e=>e.complete && e.naturalWidth>0).length})")
                                scoped[selector] = {"selector": selector, "matched_image_count": matched["count"],
                                                    "loaded_image_count": matched["loaded"],
                                                    "episode_id": episode_id}
        sample_positions = sorted(set(range(min(3, len(candidates)))) |
                                  set(range(max(0, len(candidates) - 3), len(candidates))))
        # Exclude query strings, tokens, cookies and unrelated page/account text.
        return {"source_url": url, "access_verified": True, "access_state":verified['access_state'], "complete": False,
                "initial_reader_observation": initial,
                "candidate_basis": "observed Tapas CDN /pc/ or /c/ path; authorization was checked separately",
                "page_candidate_count": len(candidates),
                "loaded_page_candidate_count": sum(bool(row["loaded"]) for row in candidates),
                "scoped_image_count": len(episode_rows),
                "scoped_source_summaries": source_summaries,
                "selector_candidates": selectors,
                "episode_scoped_selectors": list(scoped.values()),
                "container_structures": sorted(structures.values(), key=lambda s: -s["candidate_count"])[:6],
                "image_samples": [{"position": i + 1, "host": urlsplit(candidates[i]["url"]).hostname,
                            "path": urlsplit(candidates[i]["url"]).path, "loaded": bool(candidates[i]["loaded"]),
                            "width": candidates[i]["width"], "height": candidates[i]["height"]}
                           for i in sample_positions]}

    def capture(self, url, series_id, series_title, title, order, selector=None, reader_confirmed=False):
        url = canonical_url(url)
        match = re.fullmatch(r"https://tapas\.io/episode/([0-9]+)", url or "")
        if not match:
            raise ArchiveError("Use an exact https://tapas.io/episode/<numeric-id> URL.")
        self.series = Series(series_id, series_title)
        self.selector = selector or f"#episode-{match[1]} img.content__img"
        if type(reader_confirmed) is not bool:
            raise ArchiveError("Reader confirmation must be an explicit boolean.")
        if reader_confirmed:
            # Local worker callers may carry forward the user's previous
            # explicit full-reader confirmation for this exact episode URL.
            self._reader_confirmed_url = url
        self.requested = (match[1], title, order, url)
        self._manifest = Manifest(self.series, (self._capture(),))
        return self._manifest

    def _capture(self):
        episode_id, title, order, url = self.requested
        # User confirmation establishes full-reader visibility, never a grant
        # of account access. Refresh this gate before every capture or retry.
        verified = self.episode_access(url)
        if (verified.get('source_episode_id') != episode_id or
                verified.get('access_state') not in {'tapas_free','tapas_unlocked'}):
            raise ArchiveError('Manual capture requires verified access to this exact episode.')
        if self.transport:
            self.transport.close()
            self.transport = None
        self.page.goto(url, wait_until="domcontentloaded")
        if canonical_url(self.page.url) != url:
            raise ArchiveError("Reader navigated away from the requested episode.")
        if getattr(self, "_reader_confirmed_url", None) != url:
            answer = input(
                "Is the FULL episode readable on your account (not just a preview)? [y/N]: ")
            if answer.strip().lower() not in {"y", "yes"}:
                raise ArchiveError("Capture cancelled: full reader access was not confirmed.")
            self._reader_confirmed_url = url
        else:
            print("Reusing your previous full-reader confirmation for this episode.")
        # The normal initial reader HTML already includes the original signed
        # sources in data-src. Image decoding/scrolling is unnecessary when all
        # scoped elements expose real comic URLs. These URLs are not access proof.
        rows = self.reader_rows(self.selector)
        urls = ordered_sources(rows)
        if urls is not None:
            print(f"Captured {len(urls)} ordered page URLs without scrolling.", flush=True)
            self.capture_mode = "reader_lazy_sources"
        else:
            print("Reader sources incomplete; loading pages through normal scrolling...", flush=True)
            self.scroll_episode(url)
            rows = self.reader_rows(self.selector)
            urls = ordered_sources(rows)
            if urls is None:
                if not rows or any(r["tag"] != "IMG" or not r["loaded"] for r in rows):
                    raise ArchiveError("Selector must match only loaded episode IMG elements.")
                urls = [asset_url(r["url"]) for r in rows]
            self.capture_mode = "reader_scrolled_sources"
        if canonical_url(self.page.url) != url:
            raise ArchiveError("Reader navigated away from the requested episode.")
        if len(set(urls)) != len(urls):
            raise ArchiveError("Duplicate image elements; narrow the reader selector.")
        save_session(self.context, self.session)
        if self.transport:
            self.transport.close()
        self.transport = Transport(self.context.cookies(), interval=getattr(self, "interval", 1.0),
                                   network_guard=getattr(self,'network_guard',None), agent=getattr(self.context, 'agent', None))
        return Episode(episode_id, self.series.id, title, order,
                       tuple(Page(i, u, False) for i, u in enumerate(urls, 1)),
                       verified['access_state'], url, access_expires_at=verified.get('access_expires_at'))

    def manifest(self):
        return self._manifest

    def retrieve(self, page, max_bytes=None):
        from ..expiry import check_expiration
        manifest=getattr(self,'_manifest',None)
        if manifest is not None: check_expiration(manifest.episodes[0].access_expires_at)
        return self.transport.get(page.location, max_bytes=max_bytes) if max_bytes is not None else self.transport.get(page.location)

    def asset_request(self, page, max_bytes=None):
        from .tapas_http import TapasHTTPAdapter
        return TapasHTTPAdapter.asset_request(self, page, max_bytes=max_bytes)

    def refresh(self, episode):
        if getattr(self, "capture_mode", None) == "authenticated_html_manifest":
            return self._automatic.refresh(episode)
        new = self._capture()
        from urllib.parse import urlsplit
        if len(new.pages) != len(episode.pages) or any(
            urlsplit(a.location).path != urlsplit(b.location).path
            for a, b in zip(new.pages, episode.pages, strict=True)
        ):
            raise ArchiveError("Reader page manifest changed; restart capture to preserve ordering.")
        return new


# Upgrade the fixed protocol of an already running v2 development worker.
# Its queue loop resolves execute/private_json through this same module object.
# No requested module paths or executable code are accepted from the queue.
running_worker = sys.modules.get("comic_archive.worker")
if (running_worker is not None and hasattr(running_worker, "execute")
        and getattr(running_worker, "WORKER_VERSION", 2) < 34):
    reload_dependency(running_worker)
    print(f"Browser worker task handlers upgraded to v{running_worker.WORKER_VERSION}; restart the worker to update its runtime loop.", flush=True)
