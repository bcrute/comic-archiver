"""Normalized records. Remote credentials and signed URLs never enter metadata."""
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from . import sites


class ArchiveError(Exception):
    """An error safe to display without raw network/browser exception text."""


def identifier(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 100:
        raise ArchiveError("IDs must be nonempty strings of at most 100 characters.")
    if not all(c.isascii() and (c.isalnum() or c in "_-") for c in value):
        raise ArchiveError("IDs may contain only ASCII letters, numbers, '_' and '-'.")
    return value


def canonical_url(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ArchiveError("Source links must be HTTPS URLs without embedded credentials or ports.")
    try:
        u = urlsplit(value)
        if u.scheme != "https" or not u.hostname or u.username is not None or u.password is not None or u.port:
            raise ValueError
    except (ValueError, TypeError):
        raise ArchiveError("Source links must be HTTPS URLs without embedded credentials or ports.") from None
    return f"https://{u.netloc.lower()}{u.path}"


@dataclass(frozen=True)
class Page:
    order: int
    location: str
    local: bool = True

    def __post_init__(self):
        if type(self.order) is not int or self.order < 1 or not self.location:
            raise ArchiveError("Pages need a positive integer order and a location.")


@dataclass(frozen=True)
class Series:
    id: str
    title: str
    creators: tuple[str, ...] = ()
    description: str | None = None
    source_url: str | None = None
    source: str = "tapas"
    platform_id: str | None = None
    slug: str | None = None
    credits: str | None = None
    cover_url: str | None = field(default=None,repr=False)

    def __post_init__(self):
        identifier(self.id)
        identifier(self.source)
        if not isinstance(self.title, str) or not self.title.strip():
            raise ArchiveError("Series title is required.")
        # Like creators and descriptions, titles are bounded: the source controls
        # them, and an unbounded one could outgrow the archives' verification caps.
        if len(self.title) > 500:
            raise ArchiveError('Series titles must be at most 500 characters.')
        canonical_url(self.source_url)
        if self.platform_id is not None:
            identifier(self.platform_id)
        if self.slug is not None:
            identifier(self.slug)
        if not isinstance(self.creators,tuple) or len(self.creators)>20 or any(not isinstance(name,str) or not name.strip() or len(name)>200 for name in self.creators):
            raise ArchiveError('Creator names must be a bounded tuple of nonempty strings.')
        if any(value is not None and (not isinstance(value,str) or len(value)>20000) for value in (self.description,self.credits)):
            raise ArchiveError('Series descriptions and credits must be bounded text.')
        canonical_url(self.cover_url)


@dataclass(frozen=True)
class Episode:
    id: str
    series_id: str
    title: str
    order: int
    pages: tuple[Page, ...]
    access: str
    source_url: str | None = None
    published_at: str | None = None
    content_type: str = "comic"
    text_html: str | None = field(default=None, repr=False)
    access_expires_at: str | None = None
    illustrations: tuple[bytes, ...] = field(default=(), repr=False)

    def __post_init__(self):
        identifier(self.id)
        identifier(self.series_id)
        if not isinstance(self.title, str) or not self.title.strip():
            raise ArchiveError("Episode title is required.")
        if len(self.title) > 500:
            raise ArchiveError('Episode titles must be at most 500 characters.')
        if type(self.order) is not int or self.order < 1:
            raise ArchiveError("Episode order must be a positive integer.")
        if self.access not in {"local_owned", "reader_confirmed"} | sites.ACCESS:
            raise ArchiveError("Episode access is denied or unverified.")
        if self.content_type == "novel":
            if self.pages or not isinstance(self.text_html, str) or not self.text_html.strip():
                raise ArchiveError("Novel chapters require text and no comic pages.")
            from .novels import validate_fragment
            validate_fragment(self.text_html, self.illustrations)
        elif self.content_type == "comic" and self.text_html is None and not self.illustrations:
            orders = sorted(p.order for p in self.pages)
            if not orders or orders != list(range(1, len(orders) + 1)):
                raise ArchiveError("Page orders must be unique and contiguous starting at 1.")
        else:
            raise ArchiveError("Episode content type is unsupported or inconsistent.")
        canonical_url(self.source_url)
        if self.access_expires_at is not None:
            from .expiry import expiration
            expiration(self.access_expires_at)


@dataclass(frozen=True)
class Manifest:
    series: Series
    episodes: tuple[Episode, ...]

    def __post_init__(self):
        ids = [e.id for e in self.episodes]
        orders = [e.order for e in self.episodes]
        if len(set(ids)) != len(ids) or len(set(orders)) != len(orders):
            raise ArchiveError("Episode IDs and orders must be unique within a series.")
        if any(e.series_id != self.series.id for e in self.episodes):
            raise ArchiveError("Episode belongs to a different series.")
