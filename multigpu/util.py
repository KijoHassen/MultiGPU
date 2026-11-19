"""小工具：路径清理、前缀校验、比例解析与格式化。"""
from __future__ import annotations

import re

_INVALID_PREFIX = re.compile(r'[\\/:*?"<>|#\x00-\x1f]')


def validate_prefix(prefix: str) -> None:
    """命名前缀会进入文件路径，不能包含 Windows 非法字符。"""
    if _INVALID_PREFIX.search(prefix):
        raise ValueError('前缀不能包含 \\ / : * ? " < > | # 或控制字符')


def clean_path_text(text: str) -> str:
    """去掉首尾空白，并剥离用户粘贴时带上的成对引号。"""
    value = (text or "").strip()
    pairs = (('"', '"'), ("'", "'"), ("\u201c", "\u201d"), ("\u2018", "\u2019"))
    for left, right in pairs:
        if len(value) >= 2 and value.startswith(left) and value.endswith(right):
            value = value[1:-1].strip()
            break
    return value


def parse_ratio(text: str) -> float | None:
    """解析 0–100 的比例；不合法返回 None。"""
    try:
        value = float(str(text).strip().rstrip("%").strip())
    except (TypeError, ValueError):
        return None
    if 0.0 <= value <= 100.0:
        return value
    return None


def format_ratio(value: float) -> str:
    """显示成最短形式：40 而不是 40.0。"""
    return f"{value:g}"
