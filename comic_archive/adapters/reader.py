"""Tapas reader HTML observations; raw HTML and signed URLs stay in memory."""
import json
import re
from collections import Counter
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlsplit, unquote, urljoin
from ..models import ArchiveError
from ..errors import MetadataError
from ..transport import asset_url

RESEARCH_VERSION = 18
COMIC_PATH_PREFIXES = ('/pc/', '/c/')


def comic_asset(value):
    """Only the original comic asset path actually observed in the reader."""
    if not isinstance(value, str):
        return None
    try:
        asset_url(value)
        if urlsplit(value).path.startswith(COMIC_PATH_PREFIXES):
            return value
    except (ArchiveError, ValueError):
        pass
    return None


class ReaderHTML(HTMLParser):
    def __init__(self, episode_id):
        super().__init__(convert_charrefs=True)
        self.target = f"episode-{episode_id}"
        self.stack = []
        self.images = []
        self.scripts = []
        self.inline = []
        self.in_script = False
        self.attributes = {}
        self.series_ids = set()
        self.episode_links = {}
        self.episode_links_truncated = False

    def handle_starttag(self, tag, attrs):
        # A valueless attribute (<img class>) arrives as None; read it as empty.
        attrs = {name: value or "" for name, value in attrs}
        value = attrs.get("data-series-id", "")
        if re.fullmatch(r"[0-9]{1,15}", value):
            self.series_ids.add(int(value))
        scoped = attrs.get("id") == self.target or (self.stack and self.stack[-1][1])
        if tag=='a' and attrs.get('href'):
            try:
                provided=urljoin(f'https://tapas.io/episode/{self.target.removeprefix("episode-")}',attrs['href'])
                location=urlsplit(provided)
                match=re.fullmatch(r'/episode/([1-9][0-9]{0,19})',location.path)
                if (match and location.scheme=='https' and location.hostname=='tapas.io' and not location.port
                        and not location.username and not location.password and not location.query and not location.fragment):
                    if provided not in self.episode_links:
                        if len(self.episode_links)<200:
                            self.episode_links[provided]={'source_url':provided,'source_episode_id':match[1],
                                'relations':[word for word in ('next','prev','previous') if word in (attrs.get('rel') or '').lower().split()],
                                'inside_requested_episode':bool(scoped)}
                        else:self.episode_links_truncated=True
            except ValueError:pass
        if attrs.get("id") == self.target:
            # Numeric values can be account identifiers or credentials too.
            # Record only explicitly identified source-content fields.
            self.attributes = {name: value for name, value in attrs.items()
                               if name in {"data-episode-id", "data-series-id"} and value and
                               re.fullmatch(r"[0-9]{1,15}", value)}
        if tag == "img" and scoped and "content__img" in attrs.get("class", "").split():
            self.images.append({"tag": "IMG", "url": attrs.get("src", ""),
                                "sources": {name: attrs.get(name, "") for name in ("src", "data-src")}})
        if tag == "script":
            self.in_script = True
            if attrs.get("src"):
                self.scripts.append(attrs["src"])
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append((tag, bool(scoped)))

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.in_script:
            self.inline.append(data)

    def navigation_summary(self):
        return {'links':list(self.episode_links.values()),'truncated':self.episode_links_truncated,
                'visibility_verified':False,'series_membership_verified':False,
                'authorization_evidence':False,'followed_links':False}


def source_summary(rows):
    result = {"image_count": len(rows)}
    for name in ("current", "src", "data-src"):
        values = [row.get("url", "") if name == "current" else row.get("sources", {}).get(name, "") for row in rows]
        result[name] = {"comic_asset_count": sum(comic_asset(value) is not None for value in values),
                        "schemes": dict(Counter(urlsplit(value).scheme or "empty" for value in values))}
    return result


def ordered_sources(rows):
    """All-or-nothing DOM order; lazy attributes do not imply account access."""
    if not rows or len(rows) > 500 or any(row.get("tag") != "IMG" for row in rows):
        return None
    urls = []
    for row in rows:
        # data-src is the reader's original lazy source, even while src is a
        # loaded blank placeholder. Do not change signatures or trigger unlocks.
        sources = row.get("sources", {})
        value = next((url for url in (sources.get("data-src"), row.get("url"), sources.get("src"))
                      if comic_asset(url)), None)
        if value is None:
            return None
        urls.append(value)
    if len(set(urls)) != len(urls):
        raise ArchiveError("Duplicate comic pages; narrow the reader selector.")
    return urls


def safe_primitives(text):
    """Small allowlist, never user IDs, account names, cookies or token fields."""
    names = r"(?:seriesId|seriesID|series_id|episodeId|episodeID|episode_id|isFree|is_free|isPurchased|is_purchased|isUnlocked|is_unlocked|isAccessible|is_accessible|isLocked|is_locked|isLoggedIn|is_logged_in)"
    found = {}
    series = set()
    for match in re.finditer(r"(?<![A-Za-z0-9_])(?:[\"']?(" + names + r")[\"']?)\s*[:=]\s*(true|false|[0-9]{1,15})(?![A-Za-z0-9_.])", text):
        value = match[2]
        found[match[1]] = value == "true" if value in {"true", "false"} else int(value)
        if match[1] in {"seriesId", "seriesID", "series_id"}:
            series.add(found[match[1]])
    for match in re.finditer(r"(?<![A-Za-z0-9_])[\"']?(seriesId|seriesID|series_id|episodeId|episodeID|episode_id)[\"']?\s*[:=]\s*[\"']([0-9]{1,15})[\"']", text):
        found[match[1]] = int(match[2])
        if match[1] in {"seriesId", "seriesID", "series_id"}:
            series.add(found[match[1]])
    # Every series ID the page names must agree; never pick one of several.
    if len(series) > 1:
        raise MetadataError("The page names more than one series ID; its series is unverified.")
    return found


def json_shape(value, depth=0):
    """JSON structure only, never response values (including signed URLs)."""
    if depth >= 4:
        return type(value).__name__
    if isinstance(value, dict):
        return {key: json_shape(item, depth + 1) for key, item in list(value.items())[:40]
                if isinstance(key, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,59}", key)
                and not re.search(r"token|cookie|auth|password|secret|session|email", key, re.I)}
    if isinstance(value, list):
        return {"array_length": len(value), "item_shape": json_shape(value[0], depth + 1) if value else None}
    return type(value).__name__


def script_routes(text):
    """Static endpoint literals in scripts; skip queries and dynamic/token paths."""
    routes = set()
    for value in re.findall(r"[\"'](/[^\"'\s]{1,140})[\"']", text):
        path = value.split("?", 1)[0]
        if re.fullmatch(r"/[A-Za-z][A-Za-z0-9_/-]{0,99}", path) and re.search(r"episode|series|library|reading-list|profile|transaction|account|bookmark|subscribe|purchase|ajax|api", path, re.I):
            routes.add(path)
    return sorted(routes)[:80]


def public_script_contexts(text, script_path):
    """Small published-code excerpts, never authenticated inline script values."""
    def scrub(match):
        value = match[0][1:-1]
        if re.fullmatch(r'(?:/?reading-list|SUBSCRIBED|RECENT|page|since|max_limit|has_next|GET|POST|subscribed|subscriptions)', value):
            return match[0]
        if re.fullmatch(r'/[A-Za-z0-9_/-]{1,100}', value):
            return match[0]
        return '"<string>"'
    text = re.sub(r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`''', scrub, text)
    positions = [m.start() for m in re.finditer(r'reading-list|SUBSCRIBED', text)][:8]
    return [{'script_path':script_path,'sanitized_context':text[max(0,p-160):p+240]} for p in positions]


def navigation_fragment_shape(value):
    """Only known view enums and delimiters, never arbitrary fragment values."""
    if not isinstance(value,str) or len(value)>200:
        return {'supported_size':False}
    text=unquote(value).lower()
    allowed={'all','comic','comics','novel','novels','book','books','subscribed','recent','updated','category','type','content_type','sort','asc','desc','content','list','reading','series','episode','episodes'}
    parts=[]
    for token in re.findall(r'[a-z_]+|[0-9]+|[^a-z_0-9]+',text)[:16]:
        if token in allowed:
            parts.append(token)
        elif re.fullmatch(r'[,/=&:?.!_-]{1,4}',token):
            parts.append(token)
        else:
            parts.append('<redacted>')
    return {'supported_size':True,'components':parts}


def public_access_filters(host,path,value):
    """Fixed public filter vocabulary from the catalog responses observed live.

    These labels describe a catalog filter, never an episode entitlement.
    Unknown labels/codes stay redacted and no new request is constructed.
    """
    if not isinstance(value,dict):
        return None
    if host=='story-api.tapas.io' and path=='/cosmos/api/v1/landing/daily':
        data=value.get('data')
        options=data.get('bmList') if isinstance(data,dict) else None
        field='bmList'; label_key='value'; code_key='key'
    elif host=='api.tapas.io' and path=='/app-settings':
        options=value.get('browse_filters')
        field='browse_filters'; label_key='label'; code_key='code'
    else:
        return None
    if not isinstance(options,list):
        return None
    labels={'all','all comics','free','premium','free access','wait until free','wuf','paid'}
    codes={'all','a','free','f','premium','p','free_access','wait_until_free','wuf','paid'}
    known=[]
    for option in options[:20]:
        if not isinstance(option,dict):
            continue
        label=option.get(label_key)
        if not isinstance(label,str) or len(label)>64:
            continue
        label=' '.join(label.lower().split())
        if label not in labels:
            continue
        code=option.get(code_key)
        code=code if isinstance(code,str) and code.lower() in codes else None
        known.append({'label':label,'code':code,'code_recognized':code is not None})
    return {'field':field,'option_count':len(options),'inspected_option_count':min(len(options),20),
            'recognized_options':known,'authorization_evidence':False}


class ReaderObservation:
    """Observe only normal responses; never issue an endpoint probe."""
    def __init__(self,*,catalog_filters=False):
        self.responses = []
        self.routes = set()
        self.scripts = []
        self.body_budget = 4 * 1024 * 1024
        self.values = {}
        self.library_values = {}
        self.history_status = None
        self.catalog_filters = catalog_filters

    def on_response(self, response):
        try:
            parsed = urlsplit(response.url)
            request = response.request
            kind = request.resource_type
            script_asset = (kind == "script" and parsed.hostname == "us-a.tapas.io"
                            and parsed.path.startswith("/resources/js/") and parsed.path.endswith(".js"))
            api_asset = (kind in {"fetch", "xhr"} and parsed.hostname is not None
                         and parsed.hostname.endswith(".tapas.io"))
            if parsed.scheme != "https" or (parsed.hostname != "tapas.io" and not script_asset and not api_asset) or request.method not in {"GET", "POST"}:
                return
            # No queries, fragment, request/response headers or POST bodies.
            path = parsed.path
            if not re.fullmatch(r"/[A-Za-z0-9_./-]{0,160}", path):
                return
            if request.method=='GET' and parsed.hostname=='tapas.io' and kind in {'xhr','fetch'} and path=='/profile/transactions':
                self.history_status = response.status
            if len(self.responses) >= 80:
                # Diagnostic output is bounded; live cursor observation must
                # continue after that cap. Otherwise history appears to repeat
                # at page 81 even though the normal reader keeps advancing.
                if (request.method=='GET' and parsed.hostname=='tapas.io' and kind in {'xhr','fetch'}
                        and path=='/profile/transactions' and response.status==200
                        and 'json' in response.headers.get('content-type','')):
                    size=response.headers.get('content-length','')
                    if not size.isdigit() or int(size)<=2*1024*1024:
                        self.library_values[path]=response.json()
                return
            if kind not in {"xhr", "fetch", "document", "script"}:
                return
            item = {"method": request.method, "path": path, "status": response.status, "kind": kind}
            if path=='/profile/transactions':
                item['request_query_names'] = sorted({key for key,value in parse_qsl(parsed.query)
                    if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,59}',key)
                    and not re.search(r'token|cookie|auth|password|secret|session|email',key,re.I)})
            if parsed.hostname != "tapas.io":
                item["host"] = parsed.hostname
            query = {key: value for key, value in parse_qsl(parsed.query)
                     if key in {"page", "sort", "since", "limit", "init_load", "max_limit", "web", "to_prev", "ep_id"}
                     and not (path=='/profile/transactions' and key=='since')
                     and re.fullmatch(r"(?:-?[0-9]{1,15}|true|false|ASC|DESC|asc|desc)", value)}
            if query:
                item["pagination_query"] = query
            self.responses.append(item)
            content_type = response.headers.get("content-type", "").split(";", 1)[0]
            size = response.headers.get("content-length", "")
            if response.status != 200 or self.body_budget <= 0 or (size.isdigit() and int(size) > self.body_budget):
                return
            if "json" in content_type:
                value = response.json()
                item["shape"] = json_shape(value)
                if self.catalog_filters and request.method=='GET':
                    filters=public_access_filters(parsed.hostname,path,value)
                    if filters is not None:
                        item['public_access_filters']=filters
                if request.method == "GET" and (path == "/who-am-i" or re.fullmatch(r"/(?:episode/[0-9]+/info|series/[0-9]+(?:/episodes)?)", path)):
                    self.values[path] = value
                if re.search(r"library|subscribed|subscription|reading-list|bookmark|recent|history|transaction|unlocked|purchased", path, re.I):
                    self.library_values[path] = value
                # JSON spends the same budget as scripts; without a length, its parsed size stands in.
                self.body_budget -= int(size) if size.isdigit() else len(json.dumps(value).encode())
            elif kind == "script":
                text = response.text()
                self.body_budget -= len(text.encode())
                self.routes.update(script_routes(text))
                self.scripts.append(path)
        except Exception:
            # Browser exceptions can include credentials, so do not propagate.
            return

    def summary(self):
        unique = {(row["method"], row["path"], row["status"]): row for row in self.responses}
        return {"get_responses": [row for row in unique.values() if row['method'] == 'GET'],
                "other_observed_responses": [row for row in unique.values() if row['method'] != 'GET'],
                "static_script_routes": sorted(self.routes)[:80],
                "inspected_script_paths": self.scripts[:20]}
