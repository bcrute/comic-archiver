"""The sites Comic Archiver saves from, and everything about each one that the code outside its reader needs.

Tapas, Webtoon and Tappytoon all work today. A site that is named but not yet working is shown as coming later.
"""
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Site:
    # 'key' is the source written into saved files; 'name' is what people call the site.
    key: str
    name: str
    available: bool
    # The address of the site's own pages; sign-in keeps what the browser stores for it.
    home: str
    # Cookie domains a sign-in keeps (compared without a leading dot); every other site's cookies are dropped.
    cookie_domains: tuple[str, ...]
    # The hosts the downloader sends those cookies to. Pages may come from other hosts; they get none.
    cookie_hosts: tuple[str, ...]
    # Pages and covers may come from these hosts and any host inside them, over HTTPS only.
    asset_domains: tuple[str, ...]
    signin_path: str
    # The account lists the Go button reads, in order.
    lists: tuple[str, ...]
    # Hosts the site's own sign-in journey may travel besides home and cookie_hosts (its
    # mobile mirror): the sign-in surface may finish pages there. NOT cookie targets — the
    # downloader never sends cookies to a host outside cookie_hosts.
    signin_hosts: tuple[str, ...]
    # Page parts the Android sign-in screen hides (CSS id/class selectors): the site's cookie
    # notice covers its own login links on a phone. Hidden only, never answered or clicked.
    signin_hidden: tuple[str, ...] = ()

    @property
    def host(self):
        return urlsplit(self.home).hostname

    @property
    def referer(self):
        return self.home + '/'

    @property
    def signin_url(self):
        return self.home + self.signin_path

    @property
    def free(self):
        """The access mark of an episode anyone may read."""
        return self.key + '_free'

    @property
    def unlocked(self):
        """The access mark of an episode the account bought or unlocked."""
        return self.key + '_unlocked'

    @property
    def limited(self):
        """A reported rental or other time-limited unlock; the reader decides access."""
        return self.key + '_limited'

    @property
    def readable(self):
        """A reader candidate or response without a verified free/paid classification."""
        return self.key + '_readable'

    def keeps_cookie(self, domain):
        return isinstance(domain, str) and domain.lstrip('.') in self.cookie_domains

    def asset_host(self, host):
        return isinstance(host, str) and any(host == domain or host.endswith('.' + domain) for domain in self.asset_domains)

    def account_host(self, host):
        """Whether this host answers for the account (the site's pages or its API), not for one page image."""
        return host == self.host or host in API_HOSTS.get(self.key, ())


# Hosts besides a site's home that serve its lists and episode details. A read that fails
# there says the site is unreachable, not that one image is.
API_HOSTS = {'webtoon': frozenset({'m.webtoons.com'}), 'tappytoon': frozenset({'api-global.tappytoon.com'})}

SITES = (
    Site('tapas', 'Tapas', True, 'https://tapas.io', ('tapas.io',), ('tapas.io',), ('tapas.io',), '/account/signin',
         ('subscribed', 'updated', 'recent', 'free_episodes', 'wait_until_free'), ('m.tapas.io',), ('#gdpr-cookie-message',)),
    Site('webtoon', 'Webtoon', True, 'https://www.webtoons.com', ('webtoons.com', 'www.webtoons.com', 'm.webtoons.com'),
         ('www.webtoons.com', 'm.webtoons.com'), ('webtoons.com', 'pstatic.net'), '/member/login', ('subscribed', 'unlocked'), ()),
    Site('tappytoon', 'Tappytoon', True, 'https://www.tappytoon.com', ('tappytoon.com', 'www.tappytoon.com'),
         ('www.tappytoon.com', 'api-global.tappytoon.com', 'content-repository-cdn.tappytoon.com'), ('tappytoon.com',),
         '/en/login', ('unlocked', 'subscribed', 'recent'), ()),
)
# The site the app opens on until another is chosen.
DEFAULT = SITES[0]
KEYS = frozenset(site.key for site in SITES)
FREE = frozenset(site.free for site in SITES)
UNLOCKED = frozenset(site.unlocked for site in SITES)
# Every access mark that lets an episode be saved, whichever site it came from.
READABLE = frozenset(site.readable for site in SITES)
LIMITED = frozenset(site.limited for site in SITES)
ACCESS = FREE | UNLOCKED | READABLE | LIMITED
# Every account list Go can read, whichever site it belongs to.
LISTS = frozenset(name for site in SITES for name in site.lists)


def sql_list(marks):
    """Fixed access marks as an SQL list; they are this module's own words, never input."""
    return '(' + ','.join(f"'{mark}'" for mark in sorted(marks)) + ')'


def find(key):
    """The site with this key that can be chosen, or None."""
    return next((site for site in SITES if site.key == key and site.available), None)


def named(key):
    """The site with this key, chosen or not, or None."""
    return next((site for site in SITES if site.key == key), None)


def folder(root, site):
    """Normalized desktop roots use sibling folders; unmoved legacy workspaces remain addressable."""
    if site.key == DEFAULT.key:
        from .site_layout import known
        if not known(root):
            return root
    return root / site.name


def of_session(directory):
    """The site a saved sign-in belongs to. A sign-in saved before sites could be chosen is Tapas's."""
    try:
        from .osdep import read_text
        key = read_text(directory / 'site', encoding='ascii').strip()
    except FileNotFoundError:
        return DEFAULT
    except (OSError, UnicodeError):
        key = None
    site = named(key)
    if site is None:
        from .models import ArchiveError
        raise ArchiveError('The saved sign-in names a site this app does not know. Sign in again.')
    return site


def choices(current=DEFAULT):
    """What the dashboard shows in its list of sites."""
    return [{'key': site.key, 'name': site.name, 'available': site.available, 'current': site.key == current.key} for site in SITES]
