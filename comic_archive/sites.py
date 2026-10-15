"""The sites Comic Archiver saves from, and everything about each one that the code outside its reader needs.

Tapas and Tappytoon work today; Webtoon is named so people know it is coming.
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

    def keeps_cookie(self, domain):
        return isinstance(domain, str) and domain.lstrip('.') in self.cookie_domains

    def asset_host(self, host):
        return isinstance(host, str) and any(host == domain or host.endswith('.' + domain) for domain in self.asset_domains)


SITES = (
    Site('tapas', 'Tapas', True, 'https://tapas.io', ('tapas.io',), ('tapas.io',), ('tapas.io',), '/account/signin',
         ('subscribed', 'updated', 'recent', 'free_episodes')),
    Site('webtoon', 'Webtoon', False, 'https://www.webtoons.com', ('webtoons.com', 'www.webtoons.com', 'm.webtoons.com'),
         ('www.webtoons.com', 'm.webtoons.com'), ('webtoons.com', 'pstatic.net'), '/member/login', ('subscribed',)),
    Site('tappytoon', 'Tappytoon', True, 'https://www.tappytoon.com', ('tappytoon.com', 'www.tappytoon.com'),
         ('www.tappytoon.com', 'api-global.tappytoon.com', 'content-repository-cdn.tappytoon.com'), ('tappytoon.com',),
         '/en/login', ('unlocked', 'subscribed', 'recent')),
)
# The site the app opens on until another is chosen.
DEFAULT = SITES[0]
KEYS = frozenset(site.key for site in SITES)
FREE = frozenset(site.free for site in SITES)
UNLOCKED = frozenset(site.unlocked for site in SITES)
# Every access mark that lets an episode be saved, whichever site it came from.
ACCESS = FREE | UNLOCKED
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
    """Where one site's library, sign-in and downloader live inside the library folder.

    Tapas keeps the library folder itself, as before sites could be chosen, so nothing already saved moves.
    Every other site has a folder of its own, named after it, inside.
    """
    return root if site.key == DEFAULT.key else root / site.name


def of_session(directory):
    """The site a saved sign-in belongs to. A sign-in saved before sites could be chosen is Tapas's."""
    try:
        key = (directory / 'site').read_text(encoding='ascii').strip()
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
