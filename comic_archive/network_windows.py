"""What kind of connection a Windows computer reaches the internet over, and whether Windows counts it as metered.

Asked of Windows itself, inside this process: no program is started and nothing is sent anywhere.
Only the kind and the metering are read, never an address or the name of a network.
"""
import ctypes
import socket
import struct

# The kinds of network adapter (IANA ifType) that are a connection of a known kind.
KINDS = {6: 'wired', 71: 'wifi', 243: 'cellular', 244: 'cellular'}
UNRESTRICTED, FIXED, VARIABLE = 0x1, 0x2, 0x4
UNKNOWN = {'connection': 'unknown', 'metered': None}
# Addresses on the internet, used only to ask Windows which adapter it would send through.
ROUTED = ((socket.AF_INET, '8.8.8.8'), (socket.AF_INET6, '2001:4860:4860::8888'))


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
    return classify(_adapter_kind(), _cost())


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


def _adapter_kind():
    """The kind of the adapter Windows would send internet traffic through, or None."""
    helper = ctypes.WinDLL('iphlpapi')
    for family, text in ROUTED:
        address = _Address(family=family)
        packed = socket.inet_pton(family, text)
        if family == socket.AF_INET:
            # In SOCKADDR_IN the four bytes of the address follow the port directly.
            address.flow = struct.unpack('=I', packed)[0]
        else:
            address.address = (ctypes.c_ubyte * 16)(*packed)
        index = ctypes.c_uint32()
        if helper.GetBestInterfaceEx(ctypes.byref(address), ctypes.byref(index)):
            continue
        adapter = _Adapter(index=index.value)
        if helper.GetIfEntry2(ctypes.byref(adapter)) == 0:
            return adapter.kind
    return None


def _cost():
    """The cost flags Windows gives the internet connection (INetworkCostManager.GetCost), or None."""
    ole = ctypes.OleDLL('ole32')
    # A thread that already uses COM in another way answers 'changed mode'; COM is ready there all the same.
    try:
        ole.CoInitializeEx(None, 0)
        opened = True
    except OSError as error:
        if error.winerror & 0xffffffff != 0x80010106:
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
            get_cost(manager, ctypes.byref(cost), None)
            return cost.value
        finally:
            ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(methods[2])(manager)
    except OSError:
        return None
    finally:
        if opened:
            ole.CoUninitialize()
