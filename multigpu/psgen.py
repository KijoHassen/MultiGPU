"""生成每张显卡独立运行的 PowerShell 脚本。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PsJob:
    """一条 Blender 渲染命令。"""

    blend: str
    out: str
    start: int
    end: int
    label: str


def ps_quote(value: str) -> str:
    """把字符串转成 PowerShell 单引号字面量（' -> ''）。"""
    return "'" + value.replace("'", "''") + "'"


def build_ps(exe_path: str, window_title: str, jobs: list[PsJob], chcp_utf8: bool = False) -> str:
    """生成脚本：逐条调用 Blender，失败汇总；结束后保留窗口等待回车。"""
    lines: list[str] = []
    if chcp_utf8:
        lines.append("chcp 65001 > $null")
    lines.append("$ErrorActionPreference = 'Continue'")
    lines.append(f"$exe = {ps_quote(exe_path)}")
    lines.append(f"try {{ $Host.UI.RawUI.WindowTitle = {ps_quote(window_title)} }} catch {{ }}")
    lines.append("$failed = $false")

    total = len(jobs)
    for index, job in enumerate(jobs, 1):
        lines.append(f"$blend = {ps_quote(job.blend)}")
        lines.append(f"$out = {ps_quote(job.out)}")
        message = f"[{index}/{total}] 工程: {job.label} | 帧: {job.start} - {job.end}"
        lines.append(f"Write-Host {ps_quote(message)}")
        lines.append(f"& $exe -b $blend -o $out -s {job.start} -e {job.end} -a")
        failure = f"[失败] 工程: {job.label} | 帧: {job.start} - {job.end} | 退出码: "
        lines.append("if ($LASTEXITCODE -ne 0) {")
        lines.append(f"    Write-Host ({ps_quote(failure)} + $LASTEXITCODE) -ForegroundColor Red")
        lines.append("    $failed = $true")
        lines.append("}")

    lines.append("if ($failed) {")
    lines.append("    Write-Host '有渲染任务失败，请向上滚动查看错误信息。' -ForegroundColor Red")
    lines.append("} else {")
    lines.append("    Write-Host '全部渲染任务已完成。' -ForegroundColor Green")
    lines.append("}")
    lines.append("Read-Host '按回车关闭此窗口'")
    return "\r\n".join(lines) + "\r\n"
