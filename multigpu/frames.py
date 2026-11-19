"""帧扫描、分配与续传缺口计算。"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from .formats import is_image_name, is_placeholder_file

_DIGITS = re.compile(r"^\d+$")


@dataclass(frozen=True)
class Segment:
    """一个闭区间的帧范围。"""

    start: int
    end: int

    @property
    def count(self) -> int:
        return self.end - self.start + 1

    def __str__(self) -> str:
        if self.start == self.end:
            return str(self.start)
        return f"{self.start}-{self.end}"


def frame_from_name(name: str, prefix: str) -> int | None:
    """从文件名解析帧号。

    前缀非空时，去掉前缀后的剩余部分必须全是数字（大小写不敏感）；
    前缀为空时，整个文件名主干必须全是数字。
    """
    stem = os.path.splitext(name)[0]
    if prefix:
        if not stem.lower().startswith(prefix.lower()):
            return None
        rest = stem[len(prefix):]
    else:
        rest = stem
    if not _DIGITS.match(rest):
        return None
    try:
        return int(rest)
    except ValueError:
        return None


def scan_rendered_frames(out_dir: str, prefix: str) -> set[int]:
    """扫描输出目录中属于本工程、且内容不是占位的帧号集合。"""
    rendered: set[int] = set()
    with os.scandir(out_dir) as entries:
        for entry in entries:
            try:
                if not entry.is_file():
                    continue
            except OSError:
                continue
            if not is_image_name(entry.name):
                continue
            frame = frame_from_name(entry.name, prefix)
            if frame is None:
                continue
            if is_placeholder_file(entry.path):
                continue
            rendered.add(frame)
    return rendered


def find_gaps(rendered: set[int], start: int, end: int) -> list[Segment]:
    """返回 [start, end] 内所有连续缺失帧区间。"""
    gaps: list[Segment] = []
    gap_start: int | None = None
    for frame in range(start, end + 1):
        if frame in rendered:
            if gap_start is not None:
                gaps.append(Segment(gap_start, frame - 1))
                gap_start = None
        elif gap_start is None:
            gap_start = frame
    if gap_start is not None:
        gaps.append(Segment(gap_start, end))
    return gaps


def split_frames(start: int, end: int, ratio_first: float) -> tuple[Segment | None, Segment | None]:
    """按第一张卡的百分比切分帧区间；0/100 时允许一张卡全包。"""
    total = end - start + 1
    first_count = int(total * ratio_first / 100.0 + 0.5)
    first_count = max(0, min(total, first_count))
    first = Segment(start, start + first_count - 1) if first_count > 0 else None
    second = Segment(start + first_count, end) if first_count < total else None
    return first, second


@dataclass
class ProjectPlan:
    out_dir: str
    prefix: str
    start: int
    end: int
    ratio_first: float
    rendered: set[int] = field(default_factory=set)
    first_frames: Segment | None = None
    second_frames: Segment | None = None
    first_gaps: list[Segment] = field(default_factory=list)
    second_gaps: list[Segment] = field(default_factory=list)

    @property
    def missing(self) -> int:
        return sum(gap.count for gap in self.first_gaps) + sum(gap.count for gap in self.second_gaps)


def plan_project(
    out_dir: str,
    prefix: str,
    start: int,
    end: int,
    ratio_first: float,
    rendered: set[int] | None = None,
) -> ProjectPlan:
    """计算某工程在两张卡上仍需渲染的连续区间。"""
    if rendered is None:
        rendered = scan_rendered_frames(out_dir, prefix)
    first, second = split_frames(start, end, ratio_first)
    plan = ProjectPlan(
        out_dir=out_dir,
        prefix=prefix,
        start=start,
        end=end,
        ratio_first=ratio_first,
        rendered=rendered,
        first_frames=first,
        second_frames=second,
    )
    if first is not None:
        plan.first_gaps = find_gaps(rendered, first.start, first.end)
    if second is not None:
        plan.second_gaps = find_gaps(rendered, second.start, second.end)
    return plan


def make_output_path(out_dir: str, prefix: str) -> str:
    """Blender -o 参数：目录 + 前缀 + #### 占位符。"""
    return os.path.join(out_dir, prefix + "####")
