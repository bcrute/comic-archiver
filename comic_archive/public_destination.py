"""Reject written local destinations before provider browser routing."""
import ipaddress
import re
from urllib.parse import urlsplit


_NUMERIC = re.compile(r'[0-9]+|0x[0-9a-f]*')
_PUBLIC_V6 = ipaddress.IPv6Network('2000::/3')
_SPECIAL_V4 = ipaddress.IPv4Network('192.0.0.0/24')


def private_destination(host):
    """Classify a literal or local name without doing a DNS lookup."""
    name = host.strip().strip('[]').rstrip('.').lower()
    if not name:
        return False
    if ':' not in name:
        try:
            name = name.encode('idna').decode('ascii').rstrip('.').lower()
        except UnicodeError:
            return True
    if '%' in name or name.endswith(('.localhost', '.local', '.home.arpa')) or name == 'home.arpa':
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        # Also refuse single-label DNS searches and non-standard numeric literals.
        return ':' in name or '.' not in name or bool(_NUMERIC.fullmatch(name.rsplit('.', 1)[-1]))
    address = getattr(address, 'ipv4_mapped', None) or address
    if address.version == 6 and (address not in _PUBLIC_V6 or address.sixtofour is not None):
        return True
    return not address.is_global or address.is_multicast or address in _SPECIAL_V4


def public_https_url(url):
    """Check the written HTTPS authority. The connection must still vet resolved addresses."""
    if (not isinstance(url, str) or len(url) > 16384
            or '\\' in url or any(ord(character) <= 32 for character in url)):
        return False
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ''
        return (parsed.scheme == 'https' and bool(host) and parsed.username is None and parsed.password is None
                and not parsed.netloc.endswith(':') and parsed.port in {None, 443} and not private_destination(host))
    except ValueError:
        return False


def browser_request_allowed(url):
    """Check resource requests, including inline documents that require no connection."""
    if not isinstance(url, str):
        return False
    scheme, separator, rest = url.partition(':')
    if not separator:
        return False
    if scheme.lower() in {'data', 'blob'}:
        return True
    if scheme.lower() == 'about':
        return rest.partition('#')[0].partition('?')[0] in {'blank', 'srcdoc'}
    return public_https_url(url)
