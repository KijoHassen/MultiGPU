"""Windows 相关：回收站、单实例互斥、DPI 感知。"""
from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes

_IS_WINDOWS = sys.platform == "win32"

_FO_DELETE = 3
_FOF_ALLOWUNDO = 0x0040
_FOF_NOCONFIRMATION = 0x0010
_FOF_SILENT = 0x0004
_FOF_NOERRORUI = 0x0400
_FOF_FLAGS = _FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT | _FOF_NOERRORUI

_ERROR_ALREADY_EXISTS = 183


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", ctypes.c_void_p),
        ("pTo", ctypes.c_void_p),
        ("fFlags", wintypes.WORD),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def ensure_supported_platform() -> None:
    if not _IS_WINDOWS:
        raise RuntimeError("MultiGPU 只支持 Windows：需要 PowerShell 和系统回收站。")


def recycle_paths(paths: list[str]) -> bool:
    """把一批文件移入回收站；返回整批是否成功。"""
    if not _IS_WINDOWS or not paths:
        return False
    joined = "\x00".join(os.path.abspath(path) for path in paths) + "\x00\x00"
    buffer = ctypes.create_unicode_buffer(joined, len(joined))
    operation = _SHFILEOPSTRUCTW()
    operation.hwnd = None
    operation.wFunc = _FO_DELETE
    operation.pFrom = ctypes.addressof(buffer)
    operation.pTo = None
    operation.fFlags = _FOF_FLAGS
    operation.lpszProgressTitle = None
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    return result == 0 and not operation.fAnyOperationsAborted


class SingleInstance:
    """用命名互斥量防止同一个用户重复启动。"""

    def __init__(self, name: str = "Local\\MultiGPU_SingleInstance") -> None:
        self._name = name
        self._handle = None
        self.already_running = False

    def acquire(self) -> bool:
        if not _IS_WINDOWS:
            return True
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, self._name)
        if not handle:
            return True
        self._handle = handle
        self.already_running = kernel32.GetLastError() == _ERROR_ALREADY_EXISTS
        return not self.already_running

    def release(self) -> None:
        if self._handle:
            ctypes.windll.kernel32.CloseHandle(self._handle)
            self._handle = None


def enable_dpi_awareness() -> None:
    """启动时声明 DPI 感知，避免高分屏字体发虚。"""
    if not _IS_WINDOWS:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass
