"""The existing site readers with a native live-session transport.

This changes runtime construction, not provider parsers or account access rules.
Native UI/profile/workspace admission is a separate boundary: callers supply one
already selected site's cookie authority, and a job never changes that selection.
"""
from . import sites
from .android_adapter import AndroidTapasAdapter
from .android_transport import ANDROID_TAPAS, AndroidTransport
from .models import ArchiveError
from .errors import AuthenticationError
from .transport import Transport


def reader(site_key, cookie_source, cookie_sink=None, *, client=None, interval=1.0, network_guard=None):
    if not isinstance(site_key, str) or (site := sites.find(site_key)) is None:
        raise ArchiveError('Choose a supported site before starting the reader.')
    native = callable(getattr(cookie_source, 'headerFor', None))
    if native:
        if not callable(getattr(cookie_source, 'apply', None)):
            raise ArchiveError('The site cookie store is not ready.')
    elif not callable(cookie_source) or (cookie_sink is not None and not callable(cookie_sink)):
        raise ArchiveError('The site reader needs its live cookie source and sink.')
    # Tapas's native store keeps its mobile origin. Callable fixtures retain the
    # existing canonical-site transport; the two other sites keep their profiles.
    profile = ANDROID_TAPAS if native and site == sites.DEFAULT else site
    transport = (AndroidTransport(cookie_source, site=profile, client=client, interval=interval, network_guard=network_guard)
                 if native else Transport(cookies=(), site=site, client=client, interval=interval,
                     network_guard=network_guard, cookie_source=cookie_source, cookie_sink=cookie_sink))
    if site == sites.DEFAULT:
        return AndroidTapasAdapter(transport=transport)
    if site.key == 'webtoon':
        from .adapters.webtoon import WebtoonAdapter
        return WebtoonAdapter(transport=transport)
    from .adapters.tappytoon import TappytoonAdapter
    return TappytoonAdapter(transport=transport)


def signed_in_probe(site_key, cookie_source, cookie_sink=None, *, client=None, network_guard=None):
    """Verify the site's own identity without returning or persisting that identity.

    Native profile/workspace admission still belongs to the caller. A cookie,
    public page or reader constructor alone never authenticates an account.
    """
    try:
        adapter = reader(site_key, cookie_source, cookie_sink, client=client,
                         network_guard=network_guard)
        try:
            adapter.account_identity()
        finally:
            adapter.transport.close()
        return {'signed_in': True, 'verified': True, 'reason': None}
    except AuthenticationError:
        return {'signed_in': False, 'verified': False, 'reason': 'not_signed_in'}
    except ArchiveError:
        return {'signed_in': False, 'verified': False, 'reason': 'unavailable'}
