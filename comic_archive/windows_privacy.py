"""Restrict Windows app state independently of its containing drive's inherited permissions."""
from contextlib import contextmanager
import ctypes
from functools import lru_cache
import os
import logging
from pathlib import Path

from .models import ArchiveError


class PrivateStorageError(ArchiveError):
    pass


WINDOWS = os.name == 'nt'
MESSAGE = ('This location cannot keep sign-in information private. Choose a library folder on a drive '
           'that supports Windows access permissions.')
LOG = logging.getLogger('comic_archive.desktop')
_protected = set()
_plain_noted = False
DWORD, HANDLE, POINTER = ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p


class FileInformation(ctypes.Structure):
    _fields_ = [('attributes', DWORD), ('times', DWORD * 6), ('volume', DWORD),
                ('size_high', DWORD), ('size_low', DWORD), ('links', DWORD),
                ('index_high', DWORD), ('index_low', DWORD)]


@lru_cache(maxsize=1)
def _api():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    security = ctypes.WinDLL('advapi32', use_last_error=True)
    def bind(library, name, result, *arguments):
        function = getattr(library, name)
        function.restype, function.argtypes = result, list(arguments)
        return function
    functions = {
        'process': bind(kernel, 'GetCurrentProcess', HANDLE),
        'close': bind(kernel, 'CloseHandle', ctypes.c_int, HANDLE),
        'free': bind(kernel, 'LocalFree', HANDLE, HANDLE),
        'open': bind(kernel, 'CreateFileW', HANDLE, ctypes.c_wchar_p, DWORD, DWORD, POINTER, DWORD, DWORD, HANDLE),
        'info': bind(kernel, 'GetFileInformationByHandle', ctypes.c_int, HANDLE, POINTER),
        'volume': bind(kernel, 'GetVolumeInformationByHandleW', ctypes.c_int, HANDLE, POINTER, DWORD, POINTER, POINTER, POINTER, POINTER, DWORD),
        'token': bind(security, 'OpenProcessToken', ctypes.c_int, HANDLE, DWORD, POINTER),
        'user': bind(security, 'GetTokenInformation', ctypes.c_int, HANDLE, DWORD, POINTER, DWORD, POINTER),
        'sid': bind(security, 'ConvertSidToStringSidW', ctypes.c_int, POINTER, POINTER),
        'descriptor': bind(security, 'ConvertStringSecurityDescriptorToSecurityDescriptorW', ctypes.c_int, ctypes.c_wchar_p, DWORD, POINTER, POINTER),
        'dacl': bind(security, 'GetSecurityDescriptorDacl', ctypes.c_int, POINTER, POINTER, POINTER, POINTER),
        'set': bind(security, 'SetSecurityInfo', DWORD, HANDLE, DWORD, DWORD, POINTER, POINTER, POINTER, POINTER),
    }
    return functions


def _check(value):
    if not value:
        raise ctypes.WinError(ctypes.get_last_error())


@lru_cache(maxsize=1)
def _user_sid():
    api = _api(); token = HANDLE(); text = ctypes.c_wchar_p()
    _check(api['token'](api['process'](), 0x8, ctypes.byref(token)))  # TOKEN_QUERY
    try:
        size = DWORD()
        api['user'](token, 1, None, 0, ctypes.byref(size))  # TokenUser
        if not size.value:
            raise OSError('No Windows token identity')
        buffer = ctypes.create_string_buffer(size.value)
        _check(api['user'](token, 1, buffer, size, ctypes.byref(size)))
        sid = ctypes.cast(buffer, ctypes.POINTER(POINTER))[0]
        _check(api['sid'](sid, ctypes.byref(text)))
        try:
            return text.value
        finally:
            api['free'](ctypes.cast(text, POINTER))
    finally:
        api['close'](token)


@contextmanager
def _descriptor(directory):
    api = _api(); descriptor = POINTER()
    inheritance = 'OICI' if directory else ''
    rules = ''.join(f'(A;{inheritance};FA;;;{sid})' for sid in (_user_sid(), 'SY', 'BA'))
    _check(api['descriptor']('D:P' + rules, 1, ctypes.byref(descriptor), None))
    try:
        yield descriptor
    finally:
        api['free'](descriptor)


def _refuse(reason):
    LOG.warning('Windows private storage refused: reason=%s', reason)
    raise PrivateStorageError(MESSAGE)


def _secure(path, directory):
    api = _api()
    from .osdep import extended_path
    name = str(extended_path(Path(path).absolute()))
    # Open the entry itself, including directories; never traverse a reparse point at the leaf.
    handle = api['open'](name, 0x60000, 7, None, 3, 0x02200000, None)  # READ_CONTROL | WRITE_DAC
    if handle == HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info = FileInformation()
        _check(api['info'](handle, ctypes.byref(info)))
        if info.attributes & 0x400:
            _refuse('reparse_point')
        if bool(info.attributes & 0x10) != directory:
            _refuse('unexpected_entry_type')
        if not directory and info.links != 1:
            _refuse('hard_link')
        flags = DWORD()
        _check(api['volume'](handle, None, 0, None, None, ctypes.byref(flags), None, 0))
        if not flags.value & 0x8:  # FILE_PERSISTENT_ACLS: FAT/exFAT silently ignore security descriptors.
            # A drive that keeps no permissions has none to set, and none that let another account in or keep
            # it out. A library there works as it did before the app set permissions at all.
            global _plain_noted
            if not _plain_noted:
                _plain_noted = True
                LOG.info('Windows private storage: this drive keeps no access permissions; none are set')
            return
        with _descriptor(directory) as descriptor:
            present, defaulted, dacl = ctypes.c_int(), ctypes.c_int(), POINTER()
            _check(api['dacl'](descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)))
            if not present.value or not dacl.value:
                _refuse('invalid_descriptor')  # Never install a NULL DACL (unrestricted access).
            result = api['set'](handle, 1, 0x80000004, None, None, dacl, None)  # Protected DACL, file object.
            if result:
                raise ctypes.WinError(result)
    finally:
        api['close'](handle)


def file(path):
    if not WINDOWS:
        return
    try:
        _secure(path, False)
    except OSError as error:
        LOG.warning('Windows private storage failed: operation=file error=%s code=%s',
                    type(error).__name__, getattr(error, 'winerror', None) or error.errno)
        raise PrivateStorageError(MESSAGE) from None


def folder(path):
    """Repair existing explicit child grants too, once per opened private tree in this process.

    Archives never pass here. New children inherit the protected directory ACL;
    a copied/replaced directory has a different identity and is repaired again.
    """
    if not WINDOWS:
        return
    path = Path(path)
    try:
        info = path.stat()
        key = (str(path.absolute()), info.st_dev, info.st_ino)
        if key in _protected:
            return
        _secure(path, True)
        def failed(error):
            raise error
        for parent, directories, files in os.walk(path, followlinks=False, onerror=failed):
            for name, directory in [(name, True) for name in directories] + [(name, False) for name in files]:
                try:
                    _secure(Path(parent) / name, directory)
                except FileNotFoundError:
                    continue  # A worker may already have retired a temporary response or journal.
        _protected.add(key)  # A partial repair must be retried, never cached as complete.
    except OSError as error:
        LOG.warning('Windows private storage failed: operation=folder error=%s code=%s',
                    type(error).__name__, getattr(error, 'winerror', None) or error.errno)
        raise PrivateStorageError(MESSAGE) from None
