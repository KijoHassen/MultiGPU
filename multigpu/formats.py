"""图片格式表与占位文件判定。

扫描已渲染帧与「清除占位文件」共用同一张表，避免两边支持的格式不一致。
"""
from __future__ import annotations

import os
from typing import Callable

# Blender 通过 -o 能写出的、本工具愿意识别的静态图片扩展名。
IMAGE_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".bmp", ".tga", ".tif", ".tiff",
    ".exr", ".hdr", ".dpx", ".cin", ".webp", ".psd", ".jp2", ".j2c",
})


def is_image_name(name: str) -> bool:
    """按扩展名判断是否为受支持的图片文件。"""
    return os.path.splitext(name)[1].lower() in IMAGE_EXTS


def _head(path: str, size: int) -> bytes:
    with open(path, "rb") as fh:
        return fh.read(size)


def _tail(path: str, size: int) -> bytes:
    total = os.path.getsize(path)
    with open(path, "rb") as fh:
        fh.seek(max(0, total - size), os.SEEK_SET)
        return fh.read(size)


def _png_ok(path: str) -> bool:
    if _head(path, 8) != b"\x89PNG\r\n\x1a\n":
        return False
    return _tail(path, 12)[4:8] == b"IEND"


def _jpeg_ok(path: str) -> bool:
    if _head(path, 2) != b"\xff\xd8":
        return False
    return _tail(path, 2) == b"\xff\xd9"


def _bmp_ok(path: str) -> bool:
    return _head(path, 2) == b"BM"


def _tiff_ok(path: str) -> bool:
    head = _head(path, 4)
    return head in (b"II*\x00", b"MM\x00*")


def _exr_ok(path: str) -> bool:
    return _head(path, 4) == b"\x76\x2f\x31\x01"


def _hdr_ok(path: str) -> bool:
    head = _head(path, 10)
    return head.startswith(b"#?RADIANCE") or head.startswith(b"#?RGBE")


def _webp_ok(path: str) -> bool:
    head = _head(path, 12)
    return head[:4] == b"RIFF" and head[8:12] == b"WEBP"


def _dpx_ok(path: str) -> bool:
    return _head(path, 4) in (b"SDPX", b"XPDS")


def _cin_ok(path: str) -> bool:
    return _head(path, 4) in (b"\x80\x2a\x5f\xd7", b"\xd7\x5f\x2a\x80")


def _psd_ok(path: str) -> bool:
    return _head(path, 4) == b"8BPS"


def _jp2_ok(path: str) -> bool:
    return _head(path, 8)[4:8] == b"jP  "


def _j2c_ok(path: str) -> bool:
    return _head(path, 4) == b"\xff\x4f\xff\x51"


def _tga_ok(path: str) -> bool:
    if os.path.getsize(path) < 18:
        return False
    head = _head(path, 18)
    return head[1] in (0, 1) and head[2] in (1, 2, 3, 9, 10, 11)


_VALIDATORS: dict[str, Callable[[str], bool]] = {
    ".png": _png_ok,
    ".jpg": _jpeg_ok,
    ".jpeg": _jpeg_ok,
    ".bmp": _bmp_ok,
    ".tif": _tiff_ok,
    ".tiff": _tiff_ok,
    ".exr": _exr_ok,
    ".hdr": _hdr_ok,
    ".webp": _webp_ok,
    ".dpx": _dpx_ok,
    ".cin": _cin_ok,
    ".psd": _psd_ok,
    ".jp2": _jp2_ok,
    ".j2c": _j2c_ok,
    ".tga": _tga_ok,
}


def is_placeholder_file(path: str) -> bool:
    """判断文件是否为占位/损坏的图片。

    不认识的扩展名一律按占位处理；调用方（扫描）只用它过滤图片扩展名，
    「清除占位文件」也只处理图片扩展名，因此不会误伤其它文件。
    """
    try:
        if os.path.getsize(path) <= 0:
            return True
    except OSError:
        return True
    validator = _VALIDATORS.get(os.path.splitext(path)[1].lower())
    if validator is None:
        return True
    try:
        return not validator(path)
    except OSError:
        return True
