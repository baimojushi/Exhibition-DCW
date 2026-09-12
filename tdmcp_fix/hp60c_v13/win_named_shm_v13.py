"""
Windows pagefile-backed named shared memory for BRUSH/HP60C v13.

TouchDesigner reader path deliberately avoids multiprocessing.shared_memory so
it does not involve Python's resource_tracker process.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

PAGE_READWRITE = 0x00000004
FILE_MAP_WRITE = 0x0002
FILE_MAP_READ = 0x0004
FILE_MAP_ALL_ACCESS = 0x000F001F
ERROR_ALREADY_EXISTS = 183

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
SYNCHRONIZE = 0x00100000
STILL_ACTIVE = 259

_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

if os.name == "nt":
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _k32.CreateFileMappingW.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR,
    ]
    _k32.CreateFileMappingW.restype = wintypes.HANDLE

    _k32.OpenFileMappingW.argtypes = [
        wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR,
    ]
    _k32.OpenFileMappingW.restype = wintypes.HANDLE

    _k32.MapViewOfFile.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, ctypes.c_size_t,
    ]
    _k32.MapViewOfFile.restype = ctypes.c_void_p

    _k32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
    _k32.UnmapViewOfFile.restype = wintypes.BOOL

    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL

    _k32.OpenProcess.argtypes = [
        wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
    ]
    _k32.OpenProcess.restype = wintypes.HANDLE

    _k32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD),
    ]
    _k32.GetExitCodeProcess.restype = wintypes.BOOL

    _k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.TerminateProcess.restype = wintypes.BOOL


class MappingError(RuntimeError):
    pass


class NamedMapping:
    def __init__(self, handle, address, size, name, writable):
        self.handle = handle
        self.address = int(address)
        self.size = int(size)
        self.name = str(name)
        self.writable = bool(writable)
        self._closed = False

    @classmethod
    def create(cls, name, size):
        if os.name != "nt":
            raise OSError("v13 named mappings require Windows")

        size = int(size)
        hi = (size >> 32) & 0xFFFFFFFF
        lo = size & 0xFFFFFFFF

        ctypes.set_last_error(0)
        handle = _k32.CreateFileMappingW(
            wintypes.HANDLE(_INVALID_HANDLE_VALUE),
            None,
            PAGE_READWRITE,
            hi,
            lo,
            str(name),
        )
        if not handle:
            raise MappingError(
                "CreateFileMappingW({}) failed: {}".format(
                    name, ctypes.get_last_error()
                )
            )

        existed = ctypes.get_last_error() == ERROR_ALREADY_EXISTS
        address = _k32.MapViewOfFile(
            handle, FILE_MAP_ALL_ACCESS, 0, 0, size
        )
        if not address:
            err = ctypes.get_last_error()
            _k32.CloseHandle(handle)
            raise MappingError(
                "MapViewOfFile({}) failed: {}".format(name, err)
            )

        return cls(handle, address, size, name, True), existed

    @classmethod
    def open_existing(cls, name, size, writable=False):
        """Open only an existing mapping. Never creates one."""
        if os.name != "nt":
            return None

        access = FILE_MAP_ALL_ACCESS if writable else FILE_MAP_READ
        handle = _k32.OpenFileMappingW(access, False, str(name))
        if not handle:
            return None

        address = _k32.MapViewOfFile(
            handle, access, 0, 0, int(size)
        )
        if not address:
            _k32.CloseHandle(handle)
            return None

        return cls(handle, address, int(size), name, writable)

    def close(self):
        if self._closed:
            return
        self._closed = True

        if self.address:
            _k32.UnmapViewOfFile(ctypes.c_void_p(self.address))
            self.address = 0

        if self.handle:
            _k32.CloseHandle(self.handle)
            self.handle = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def _check(self, offset, length):
        offset = int(offset)
        length = int(length)
        if offset < 0 or length < 0 or offset + length > self.size:
            raise ValueError(
                "mapping range {}+{} exceeds {}".format(
                    offset, length, self.size
                )
            )

    def zero(self, offset=0, length=None):
        if not self.writable:
            raise PermissionError("mapping is read-only")
        if length is None:
            length = self.size - int(offset)
        self._check(offset, length)
        ctypes.memset(self.address + int(offset), 0, int(length))

    def write_bytes(self, offset, data):
        if not self.writable:
            raise PermissionError("mapping is read-only")
        data = bytes(data)
        self._check(offset, len(data))
        ctypes.memmove(self.address + int(offset), data, len(data))

    def read_bytes(self, offset, length):
        self._check(offset, length)
        return ctypes.string_at(
            self.address + int(offset), int(length)
        )

    def memmove_from(self, offset, src_ptr, length):
        if not self.writable:
            raise PermissionError("mapping is read-only")
        self._check(offset, length)
        ctypes.memmove(
            self.address + int(offset),
            int(src_ptr),
            int(length),
        )


def pid_is_alive(pid):
    if os.name != "nt" or not pid:
        return False

    handle = _k32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE,
        False,
        int(pid),
    )
    if not handle:
        return False

    try:
        code = wintypes.DWORD(0)
        if not _k32.GetExitCodeProcess(
            handle, ctypes.byref(code)
        ):
            return False
        return int(code.value) == STILL_ACTIVE
    finally:
        _k32.CloseHandle(handle)


def terminate_pid(pid, exit_code=13):
    if os.name != "nt" or not pid:
        return False

    handle = _k32.OpenProcess(
        PROCESS_TERMINATE | SYNCHRONIZE,
        False,
        int(pid),
    )
    if not handle:
        return False

    try:
        return bool(_k32.TerminateProcess(
            handle, int(exit_code)
        ))
    finally:
        _k32.CloseHandle(handle)
