import os
from contextlib import contextmanager


class InstanceAlreadyRunning(RuntimeError):
    pass


@contextmanager
def windows_instance(name):
    if os.name != "nt":
        yield
        return

    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateMutexW
    create.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL

    ctypes.set_last_error(0)
    handle = create(None, False, name)
    error = ctypes.get_last_error()
    if not handle:
        raise ctypes.WinError(error)
    try:
        if error == 183:  # ERROR_ALREADY_EXISTS
            raise InstanceAlreadyRunning(name)
        yield
    finally:
        close(handle)
