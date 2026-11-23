"""What kind of connection a Windows computer reaches the internet over, and whether Windows counts it as metered.

Asked of Windows itself, inside this process: no program is started and nothing is sent anywhere.
Route addresses are used internally and never reported.
"""
import ctypes
from dataclasses import dataclass
from ipaddress import IPv4Network, IPv6Network, ip_network
import socket
import struct

# The kinds of network adapter (IANA ifType) that are a connection of a known kind.
KINDS = {6: 'wired', 71: 'wifi', 243: 'cellular', 244: 'cellular'}
UNRESTRICTED, FIXED, VARIABLE = 0x1, 0x2, 0x4
UNKNOWN = {'connection': 'unknown', 'metered': None}
# Addresses on the internet, used only to ask Windows which adapter it would send through.
ROUTED = ((socket.AF_INET, '8.8.8.8'), (socket.AF_INET6, '2001:4860:4860::8888'))
# On-link routes to local addresses alone do not identify an Internet egress.
LOCAL_PREFIXES = {
    socket.AF_INET: tuple(map(ip_network, ('0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8',
                                          '169.254.0.0/16', '172.16.0.0/12', '192.168.0.0/16',
                                          '224.0.0.0/4', '240.0.0.0/4'))),
    socket.AF_INET6: tuple(map(ip_network, ('::1/128', 'fc00::/7', 'fe80::/10', 'ff00::/8'))),
}


@dataclass(frozen=True)
class _Route:
    family: int
    index: int
    luid: int
    prefix: IPv4Network | IPv6Network
    gateway: bytes
    scope: int
    metric: int

    @property
    def on_link(self):
        return not any(self.gateway)


def classify(kind, cost):
    """The answer the connection rule works with, from an adapter kind and the cost Windows gives the connection.

    Anything else than a wired, Wi-Fi or mobile adapter (a VPN, a tunnel) is unknown, as is a cost
    Windows cannot tell: the rule then holds transfers, as it does on Linux.
    """
    connection = KINDS.get(kind)
    if connection is None:
        return dict(UNKNOWN)
    if connection == 'cellular':
        return {'connection': 'cellular', 'metered': True}
    if type(cost) is not int or not cost & (UNRESTRICTED | FIXED | VARIABLE):
        return dict(UNKNOWN)
    return {'connection': connection, 'metered': bool(cost & (FIXED | VARIABLE))}


def probe():
    routes = _routes()
    interfaces = {(route.index, route.luid) for route in routes}
    if len(interfaces) != 1:
        return dict(UNKNOWN)
    index, _luid = next(iter(interfaces))
    gateways = {
        family: {(route.gateway, route.scope) for route in routes
                 if route.family == family and route.prefix.prefixlen == 0}
        for family, _text in ROUTED
    }
    if any(not gateways[route.family] or len(gateways[route.family]) > 1
           or (not route.on_link and (route.gateway, route.scope) not in gateways[route.family])
           for route in routes):
        return dict(UNKNOWN)
    kinds = []
    for family, text in ROUTED:
        if not gateways[family]:
            continue
        destination = _route_address(family, text)
        kind = _adapter_kind(destination, index)
        kinds.append(classify(kind, _cost(destination)))
    # Read the table again so a transition during the queries cannot authorize
    # transfers using the cost sampled on the old adapter.
    if not kinds or _routes() != routes:
        return dict(UNKNOWN)
    for metered in (True, None):
        for value in kinds:
            if value['metered'] is metered:
                return value
    return kinds[0]


class _Guid(ctypes.Structure):
    _fields_ = [('a', ctypes.c_uint32), ('b', ctypes.c_uint16), ('c', ctypes.c_uint16), ('d', ctypes.c_ubyte * 8)]

    @classmethod
    def of(cls, text):
        a, b, c, d, e = text.split('-')
        return cls(int(a, 16), int(b, 16), int(c, 16), (ctypes.c_ubyte * 8)(*bytes.fromhex(d + e)))


class _Address(ctypes.Structure):
    """A socket address large enough for either family (SOCKADDR_IN6)."""
    _fields_ = [('family', ctypes.c_ushort), ('port', ctypes.c_ushort), ('flow', ctypes.c_uint32),
                ('address', ctypes.c_ubyte * 16), ('scope', ctypes.c_uint32)]


class _Adapter(ctypes.Structure):
    """The start of MIB_IF_ROW2, up to the adapter kind; the rest is room for Windows to fill."""
    _fields_ = [('luid', ctypes.c_uint64), ('index', ctypes.c_uint32), ('guid', _Guid),
                ('alias', ctypes.c_wchar * 257), ('description', ctypes.c_wchar * 257),
                ('physical_length', ctypes.c_uint32), ('physical', ctypes.c_ubyte * 32),
                ('permanent', ctypes.c_ubyte * 32), ('mtu', ctypes.c_uint32), ('kind', ctypes.c_uint32),
                ('rest', ctypes.c_ubyte * 1024)]


class _Prefix(ctypes.Structure):
    _fields_ = [('address', _Address), ('length', ctypes.c_ubyte)]


class _ForwardRow(ctypes.Structure):
    _fields_ = [('luid', ctypes.c_uint64), ('index', ctypes.c_uint32),
                ('prefix', _Prefix), ('next_hop', _Address), ('site_prefix_length', ctypes.c_ubyte),
                ('valid_lifetime', ctypes.c_uint32), ('preferred_lifetime', ctypes.c_uint32),
                ('metric', ctypes.c_uint32), ('protocol', ctypes.c_uint32),
                ('loopback', ctypes.c_ubyte), ('autoconfigure_address', ctypes.c_ubyte),
                ('publish', ctypes.c_ubyte), ('immortal', ctypes.c_ubyte),
                ('age', ctypes.c_uint32), ('origin', ctypes.c_uint32)]


class _ForwardTable(ctypes.Structure):
    _fields_ = [('count', ctypes.c_uint32), ('rows', _ForwardRow * 1)]


def _packed(address):
    if address.family == socket.AF_INET:
        return struct.pack('=I', address.flow)
    if address.family == socket.AF_INET6:
        return bytes(address.address)
    raise ValueError('Unexpected Windows route address family.')


def _routes():
    """Snapshot the interfaces and gateways that could route a public transfer."""
    helper = ctypes.WinDLL('iphlpapi')
    table = ctypes.POINTER(_ForwardTable)()
    if helper.GetIpForwardTable2(socket.AF_UNSPEC, ctypes.byref(table)) or not table:
        return frozenset()
    try:
        count = table.contents.count
        if count > 16384:
            return frozenset()
        rows = (_ForwardRow * count).from_address(ctypes.addressof(table.contents) + _ForwardTable.rows.offset)
        routes = set()
        for row in rows:
            if row.loopback or not row.valid_lifetime:
                continue
            address = row.prefix.address
            network = ip_network((_packed(address), row.prefix.length), strict=False)
            gateway = _packed(row.next_hop)
            if address.family != row.next_hop.family or not row.index or not row.luid:
                return frozenset()
            route = _Route(address.family, row.index, row.luid, network, gateway, row.next_hop.scope, row.metric)
            if route.on_link and any(network.subnet_of(local) for local in LOCAL_PREFIXES[address.family]):
                continue
            routes.add(route)
        return frozenset(routes)
    finally:
        helper.FreeMibTable(table)


def _route_address(family, text):
    address = _Address(family=family)
    packed = socket.inet_pton(family, text)
    if family == socket.AF_INET:
        # In SOCKADDR_IN the four bytes of the address follow the port.
        address.flow = struct.unpack('=I', packed)[0]
    else:
        address.address = (ctypes.c_ubyte * 16)(*packed)
    return address


def _adapter_kind(address, expected_index):
    """Check that the chosen route agrees with the default-route snapshot."""
    helper = ctypes.WinDLL('iphlpapi')
    index = ctypes.c_uint32()
    if helper.GetBestInterfaceEx(ctypes.byref(address), ctypes.byref(index)) or index.value != expected_index:
        return None
    adapter = _Adapter(index=index.value)
    if helper.GetIfEntry2(ctypes.byref(adapter)) == 0:
        return adapter.kind
    return None


def _cost(address):
    """The first-hop cost for the same destination used for adapter selection."""
    ole = ctypes.OleDLL('ole32')
    # A thread that already uses COM in another way answers 'changed mode'; COM is ready there all the same.
    try:
        ole.CoInitializeEx(None, 0)
        opened = True
    except OSError as error:
        if getattr(error, 'winerror', 0) & 0xffffffff != 0x80010106:
            return None
        opened = False
    try:
        manager = ctypes.c_void_p()
        ole.CoCreateInstance(ctypes.byref(_Guid.of('DCB00C01-570F-4A9B-8D69-199FDBA5723B')), None, 0x17,
                             ctypes.byref(_Guid.of('DCB00008-570F-4A9B-8D69-199FDBA5723B')), ctypes.byref(manager))
        methods = ctypes.cast(manager, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        try:
            cost = ctypes.c_uint32()
            get_cost = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32),
                                          ctypes.c_void_p)(methods[3])
            destination = (ctypes.c_ubyte * 128)()  # NLM_SOCKADDR contains a zero-padded socket address.
            ctypes.memmove(destination, ctypes.byref(address), ctypes.sizeof(address))
            result = get_cost(manager, ctypes.byref(cost), ctypes.byref(destination))
            if result == 0:
                return cost.value
            return None
        finally:
            ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(methods[2])(manager)
    except OSError:
        return None
    finally:
        if opened:
            ole.CoUninitialize()
