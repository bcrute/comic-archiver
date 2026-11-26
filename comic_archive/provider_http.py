"""Direct HTTPS sockets for the provider HTTP client."""
import ipaddress
import socket
import time

from .public_destination import private_destination, public_https_url


class DestinationRefused(OSError):
    pass


# A connect that is going to succeed takes well under a second; a slow link still gets all the time left on the last address.
ATTEMPT_SECONDS = 2
ATTEMPT_FLOOR = .25


def _endpoint(family, sockaddr, port):
    try:
        host, actual_port = sockaddr[:2]
        address = ipaddress.ip_address(host)
        if (actual_port == port and not private_destination(host)
                and ((family == socket.AF_INET and address.version == 4 and len(sockaddr) == 2)
                     or (family == socket.AF_INET6 and address.version == 6 and sockaddr[2:] == (0, 0)))):
            parts = (str(address), port)
            return parts if family == socket.AF_INET else (*parts, 0, 0)
    except (TypeError, ValueError):
        pass
    raise DestinationRefused('Provider connection address is outside the public network boundary.')


def _connect_public(host, port, timeout, socket_options):
    if port != 443 or not host or private_destination(host):
        raise DestinationRefused('Provider connection is outside the public HTTPS boundary.')
    end = None if timeout is None else time.monotonic() + timeout
    answers = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM, socket.IPPROTO_TCP)
    # Vet every candidate before opening the first socket. Preserve resolver order.
    endpoints = dict.fromkeys((family, _endpoint(family, sockaddr, port))
                              for family, _, _, _, sockaddr in answers)
    if not endpoints:
        raise DestinationRefused('Provider name has no public connection address.')
    last_error = None
    for tried, (family, endpoint) in enumerate(endpoints):
        remaining = None if end is None else end - time.monotonic()
        if remaining is not None and remaining <= 0:
            raise TimeoutError('Provider connection timed out.')
        # An address that never answers must leave time for the ones after it, with or without a deadline.
        left = len(endpoints) - tried
        attempt = remaining if left == 1 else ATTEMPT_SECONDS if remaining is None else min(
            remaining, ATTEMPT_SECONDS, max(remaining / left, ATTEMPT_FLOOR))
        sock = None
        keep = False
        try:
            sock = socket.socket(family, socket.SOCK_STREAM, socket.IPPROTO_TCP)
            sock.settimeout(attempt)
            for option in socket_options or ():
                sock.setsockopt(*option)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            # Use the canonical numeric sockaddr. The original hostname goes to TLS.
            sock.connect(endpoint)
            if _endpoint(family, sock.getpeername(), port) != endpoint:
                raise DestinationRefused('Provider connection peer differs from the resolved address.')
            keep = True
            return sock
        except DestinationRefused:
            raise
        except OSError as error:
            last_error = error
        finally:
            if sock is not None and not keep:
                sock.close()
    raise last_error


def provider_client(*, workers=None):
    import certifi
    import httpcore
    import httpx
    import ssl
    from httpcore._backends.sync import SyncStream

    class PublicBackend(httpcore.NetworkBackend):
        def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
            if local_address is not None:
                raise httpcore.ConnectError('Provider connections use the system source address.')
            try:
                return SyncStream(_connect_public(host, port, timeout, socket_options))
            except TimeoutError:
                raise httpcore.ConnectTimeout('Provider connection timed out.') from None
            except OSError:
                raise httpcore.ConnectError('Provider connection failed or was refused.') from None

    class ProviderHTTPTransport(httpx.HTTPTransport):
        def __init__(self, limits):
            # HTTPX's synchronous adapter delegates to this pool. Supplying the
            # backend here leaves HTTPcore in charge of SNI and certificate checks.
            self._pool = httpcore.ConnectionPool(
                ssl_context=ssl.create_default_context(cafile=certifi.where()),
                max_connections=limits.max_connections,
                max_keepalive_connections=limits.max_keepalive_connections,
                keepalive_expiry=limits.keepalive_expiry,
                network_backend=PublicBackend(),
            )

        def handle_request(self, request):
            if not public_https_url(str(request.url)):
                raise httpx.UnsupportedProtocol('Provider connections require a public HTTPS authority on port 443.')
            return super().handle_request(request)

    limits = (httpx.Limits(max_connections=100, max_keepalive_connections=20) if workers is None
              else httpx.Limits(max_connections=workers, max_keepalive_connections=workers))
    timeout = 30 if workers is None else httpx.Timeout(10, connect=30)
    return httpx.Client(transport=ProviderHTTPTransport(limits), timeout=timeout,
                        follow_redirects=False, trust_env=False)
