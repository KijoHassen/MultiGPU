"""Tkinter 图形界面与启动入口。"""
from __future__ import annotations

import logging
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .config import config_path, load_config, save_config, setup_logging, temp_dir
from .formats import is_image_name, is_placeholder_file
from .frames import Segment, frame_from_name, make_output_path, plan_project, scan_rendered_frames
from .psgen import PsJob, build_ps
from .util import clean_path_text, format_ratio, parse_ratio, validate_prefix
from .win32 import SingleInstance, enable_dpi_awareness, ensure_supported_platform, recycle_paths

log = logging.getLogger(__name__)


class _QueueLogHandler(logging.Handler):
    """把日志转交 Tk 主线程（经队列），避免工作线程直接碰控件。"""

    def __init__(self, target: "queue.Queue") -> None:
        super().__init__()
        self._target = target

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:  # noqa: BLE001 - 日志失败不能影响主流程
            self.handleError(record)
            return
        self._target.put(("log", message))


@dataclass
class RenderJob:
    proj: str
    out_dir: str
    prefix: str
    start: int
    end: int


def _program_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _resource_path(relative: str) -> Path:
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        base = Path(__file__).resolve().parent.parent
    return base / relative


def _format_gaps(gaps: list[Segment]) -> str:
    return "、".join(str(gap) for gap in gaps) if gaps else "无"


def _count_gaps(gaps: list[Segment]) -> int:
    return sum(gap.count for gap in gaps)


class MultiGPUApp(tk.Tk):
    def __init__(self, instance: SingleInstance | None = None) -> None:
        super().__init__()
        self.instance = instance
        self.rows: list[dict] = []
        self._queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self._processes: list[subprocess.Popen] = []
        self._busy = False
        self._running = False
        self._stop_requested = False
        self._closed = False
        self._script_dir: Path | None = None
        self._ratio_guard = False
        self._exe_first = ""
        self._exe_second = ""
        self._ratio = 60.0

        self.title(f"MultiGPU {__version__} - Blender 双卡渲染调度")
        self._setup_dpi()

        self.var_exe_first = tk.StringVar()
        self.var_exe_second = tk.StringVar()
        self.var_ratio_first = tk.StringVar(value="60")
        self.var_ratio_second = tk.StringVar(value="40")
        self.var_fix_encoding = tk.BooleanVar(value=True)

        self._build_ui()
        self._attach_log_handler()
        self._load_icon()
        self._cleanup_stale_temp()
        self._load_config()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._apply_initial_geometry)
        self.after(100, self._poll_ui_queue)
        log.info("MultiGPU %s 启动，程序目录：%s", __version__, _program_root())

    def _setup_dpi(self) -> None:
        try:
            dpi = float(self.winfo_fpixels("1i"))
        except tk.TclError:
            dpi = 96.0
        self._scale = max(1.0, dpi / 96.0)
        try:
            self.tk.call("tk", "scaling", dpi / 72.0)
        except tk.TclError:
            pass

    def _build_ui(self) -> None:
        self._build_header()
        self._build_toolbar()
        self._paned = ttk.PanedWindow(self, orient="vertical")
        self._paned.pack(side="top", fill="both", expand=True, padx=8, pady=(4, 8))
        self._build_project_area()
        self._build_log_area()
        self.bind_all("<MouseWheel>", self._on_wheel)

    def _build_header(self) -> None:
        self._header = ttk.LabelFrame(self, text="显卡 / Blender 配置", padding=8)
        self._header.pack(side="top", fill="x", padx=8, pady=(8, 4))
        self._header.columnconfigure(1, weight=1)

        for row, (label, var) in enumerate((
            ("显卡 0 可执行文件", self.var_exe_first),
            ("显卡 1 可执行文件", self.var_exe_second),
        )):
            ttk.Label(self._header, text=label).grid(row=row, column=0, sticky="w", pady=2)
            ttk.Entry(self._header, textvariable=var, width=58).grid(
                row=row, column=1, sticky="we", padx=(8, 4), pady=2)
            ttk.Button(self._header, text="浏览…", command=lambda v=var: self._pick_exe(v)).grid(
                row=row, column=2, pady=2)

        ttk.Label(self._header, text="分配比例").grid(row=2, column=0, sticky="w", pady=2)
        ratio_frame = ttk.Frame(self._header)
        ratio_frame.grid(row=2, column=1, sticky="w", padx=(8, 4), pady=2)
        first = ttk.Entry(ratio_frame, textvariable=self.var_ratio_first, width=8, justify="center")
        first.pack(side="left")
        ttk.Label(ratio_frame, text="%  :").pack(side="left", padx=(2, 2))
        second = ttk.Entry(ratio_frame, textvariable=self.var_ratio_second, width=8, justify="center")
        second.pack(side="left")
        ttk.Label(ratio_frame, text="%（两张卡合计 100）").pack(side="left", padx=(2, 0))

        self._btn_fix_encoding = ttk.Button(
            self._header, text="乱码修复：开", command=self._toggle_fix_encoding)
        self._btn_fix_encoding.grid(row=2, column=2, sticky="e", pady=2)

        self.var_ratio_first.trace_add("write", self._on_ratio_first)
        self.var_ratio_second.trace_add("write", self._on_ratio_second)

    def _build_toolbar(self) -> None:
        self._toolbar = ttk.Frame(self)
        self._toolbar.pack(side="top", fill="x", padx=8, pady=4)
        self._btn_add = ttk.Button(self._toolbar, text="添加工程", command=self.add_row)
        self._btn_start = ttk.Button(self._toolbar, text="启动渲染", command=self.on_start)
        self._btn_stop = ttk.Button(self._toolbar, text="停止渲染", command=self.on_stop, state="disabled")
        self._btn_clear = ttk.Button(self._toolbar, text="清空列表", command=self.on_clear_rows)
        self._btn_placeholders = ttk.Button(
            self._toolbar, text="清除占位文件", command=self.on_clear_placeholders)
        for button in (self._btn_add, self._btn_start, self._btn_stop,
                       self._btn_clear, self._btn_placeholders):
            button.pack(side="left", padx=(0, 6))
        ttk.Label(
            self._toolbar,
            text="续传按「前缀 + 帧号」识别，前缀要与 Blender 输出文件名一致",
            foreground="#666666",
        ).pack(side="right")

    def _build_project_area(self) -> None:
        frame = ttk.LabelFrame(self._paned, text="工程列表", padding=4)
        self._paned.add(frame, weight=3)

        container = ttk.Frame(frame)
        container.pack(fill="both", expand=True)
        container.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)

        self._canvas = tk.Canvas(container, highlightthickness=0, height=320)
        vertical = ttk.Scrollbar(container, orient="vertical", command=self._canvas.yview)
        horizontal = ttk.Scrollbar(container, orient="horizontal", command=self._canvas.xview)
        self._canvas.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self._canvas.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")

        self._table = ttk.Frame(self._canvas)
        self._canvas.create_window((0, 0), window=self._table, anchor="nw")
        self._table.bind("<Configure>", lambda _event: self._sync_scrollregion())

        headers = ("序号", "工程路径（.blend）", "输出文件夹", "命名前缀", "起始帧", "结束帧", "操作")
        for column, text in enumerate(headers):
            ttk.Label(self._table, text=text, font=("", 9, "bold")).grid(
                row=0, column=column, padx=4, pady=(2, 4), sticky="w")
        self._table.columnconfigure(1, weight=3)
        self._table.columnconfigure(2, weight=2)

    def _build_log_area(self) -> None:
        frame = ttk.LabelFrame(self._paned, text="日志 / 分配预览", padding=4)
        self._paned.add(frame, weight=1)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        self._log_text = tk.Text(frame, height=9, wrap="word", state="disabled")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self._log_text.yview)
        self._log_text.configure(yscrollcommand=scrollbar.set)
        self._log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

    def _load_icon(self) -> None:
        icon = _resource_path("icon/MultiGPU.ico")
        if not icon.is_file():
            return
        try:
            self.iconbitmap(default=str(icon))
        except tk.TclError:
            log.warning("设置窗口图标失败：%s", icon)

    def _apply_initial_geometry(self) -> None:
        if self._closed:
            return
        self.update_idletasks()
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        base_w = max(1040, self._header.winfo_reqwidth() + 48)
        base_h = max(720, self._paned.winfo_reqheight() + 200)
        width = min(int(base_w * self._scale), int(screen_w * 0.95))
        height = min(int(base_h * self._scale), int(screen_h * 0.90))
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 3)
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.minsize(min(int(900 * self._scale), width), min(int(560 * self._scale), height))

    # ------------------------------------------------------------- 配置与日志
    def _attach_log_handler(self) -> None:
        handler = _QueueLogHandler(self._queue)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logging.getLogger("multigpu").addHandler(handler)

    def _load_config(self) -> None:
        data, migrated = load_config(_program_root())
        self.var_exe_first.set(data["exe_first"])
        self.var_exe_second.set(data["exe_second"])
        self.var_ratio_first.set(format_ratio(float(data["ratio_first"])))
        self.var_ratio_second.set(format_ratio(float(data["ratio_second"])))
        self.var_fix_encoding.set(bool(data["fix_encoding"]))
        self._update_fix_encoding_button()
        for item in data["projects"][:200]:
            self.add_row(item)
        if not self.rows:
            self.add_row()
        if migrated:
            log.info("已把旧配置迁移到：%s", config_path(_program_root()))
        log.info("配置文件：%s", config_path(_program_root()))

    def _collect_config(self) -> dict:
        def as_int(var: tk.StringVar, fallback: int) -> int:
            try:
                return int(var.get().strip())
            except ValueError:
                return fallback

        projects = []
        for row in self.rows:
            projects.append({
                "proj": clean_path_text(row["proj"].get()),
                "out": clean_path_text(row["out"].get()),
                "prefix": clean_path_text(row["prefix"].get()),
                "start": as_int(row["start"], 1),
                "end": as_int(row["end"], 250),
            })
        ratio_first = parse_ratio(self.var_ratio_first.get())
        ratio_second = parse_ratio(self.var_ratio_second.get())
        if ratio_first is None or ratio_second is None:
            ratio_first, ratio_second = 60.0, 40.0
        return {
            "exe_first": clean_path_text(self.var_exe_first.get()),
            "exe_second": clean_path_text(self.var_exe_second.get()),
            "ratio_first": ratio_first,
            "ratio_second": ratio_second,
            "fix_encoding": bool(self.var_fix_encoding.get()),
            "projects": projects,
        }

    def _save_config(self, notify: bool = False) -> None:
        try:
            path = save_config(self._collect_config())
        except OSError as exc:
            log.exception("保存配置失败")
            messagebox.showerror(
                "保存配置失败", f"无法写入：\n{config_path(_program_root())}\n\n{exc}")
            return
        if notify:
            log.info("配置已保存：%s", path)

    def _log(self, message: str) -> None:
        log.info("%s", message)

    def _append_log(self, message: str) -> None:
        self._log_text.configure(state="normal")
        self._log_text.insert("end", message.rstrip("\r\n") + "\n")
        self._log_text.see("end")
        self._log_text.configure(state="disabled")

    def _poll_ui_queue(self) -> None:
        if self._closed:
            return
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "log":
                    self._append_log(str(payload))
                elif kind == "planned":
                    self._on_planned(payload)  # type: ignore[arg-type]
                elif kind == "error":
                    self._on_plan_error(str(payload))
        except queue.Empty:
            pass
        if not self._closed:
            self.after(100, self._poll_ui_queue)

    def _pick_exe(self, var: tk.StringVar) -> None:
        path = filedialog.askopenfilename(
            title="选择 Blender 可执行文件",
            filetypes=[("可执行文件", "*.exe"), ("所有文件", "*.*")],
        )
        if path:
            var.set(path)

    def _pick_proj(self, row: dict) -> None:
        path = filedialog.askopenfilename(
            title="选择工程文件",
            filetypes=[("Blender 工程", "*.blend"), ("所有文件", "*.*")],
        )
        if path:
            row["proj"].set(path)

    def _pick_out(self, row: dict) -> None:
        path = filedialog.askdirectory(title="选择输出文件夹")
        if path:
            row["out"].set(path)

    def _on_ratio_first(self, *_args) -> None:
        if self._ratio_guard:
            return
        value = parse_ratio(self.var_ratio_first.get())
        if value is None:
            return
        self._ratio_guard = True
        try:
            self.var_ratio_second.set(format_ratio(100.0 - value))
        finally:
            self._ratio_guard = False

    def _on_ratio_second(self, *_args) -> None:
        if self._ratio_guard:
            return
        value = parse_ratio(self.var_ratio_second.get())
        if value is None:
            return
        self._ratio_guard = True
        try:
            self.var_ratio_first.set(format_ratio(100.0 - value))
        finally:
            self._ratio_guard = False

    def _update_fix_encoding_button(self) -> None:
        self._btn_fix_encoding.configure(
            text=f"乱码修复：{'开' if self.var_fix_encoding.get() else '关'}")

    def _toggle_fix_encoding(self) -> None:
        self.var_fix_encoding.set(not self.var_fix_encoding.get())
        self._update_fix_encoding_button()
        self._save_config()
        log.info("乱码修复已%s", "开启" if self.var_fix_encoding.get() else "关闭")

    def _finish_entry_edit(self, _event=None):
        self.tk_focusNext().focus_set()
        return "break"

    def _on_wheel(self, event) -> None:
        if isinstance(event.widget, tk.Text):
            return
        if event.state & 0x0001:
            self._canvas.xview_scroll(int(-event.delta / 120), "units")
        else:
            self._canvas.yview_scroll(int(-event.delta / 120), "units")

    def _sync_scrollregion(self) -> None:
        self._table.update_idletasks()
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    # ------------------------------------------------------------------ 列表
    def add_row(self, data: dict | None = None) -> None:
        data = data or {}
        row = {
            "proj": tk.StringVar(value=str(data.get("proj", ""))),
            "out": tk.StringVar(value=str(data.get("out", ""))),
            "prefix": tk.StringVar(value=str(data.get("prefix", ""))),
            "start": tk.StringVar(value=str(data.get("start", 1))),
            "end": tk.StringVar(value=str(data.get("end", 250))),
            "widgets": [],
        }
        index = len(self.rows)
        grid_row = index + 1

        label = ttk.Label(self._table, text=str(index + 1), width=4, anchor="w")
        label.grid(row=grid_row, column=0, padx=(4, 2), pady=2, sticky="w")
        proj_frame = ttk.Frame(self._table)
        proj_frame.grid(row=grid_row, column=1, padx=4, pady=2, sticky="we")
        proj_entry = ttk.Entry(proj_frame, textvariable=row["proj"], width=42)
        proj_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(proj_frame, text="浏览…", width=6,
                   command=lambda r=row: self._pick_proj(r)).pack(side="left", padx=(2, 0))

        out_frame = ttk.Frame(self._table)
        out_frame.grid(row=grid_row, column=2, padx=4, pady=2, sticky="we")
        out_entry = ttk.Entry(out_frame, textvariable=row["out"], width=28)
        out_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(out_frame, text="浏览…", width=6,
                   command=lambda r=row: self._pick_out(r)).pack(side="left", padx=(2, 0))
        prefix_entry = ttk.Entry(self._table, textvariable=row["prefix"], width=12)
        prefix_entry.grid(row=grid_row, column=3, padx=4, pady=2, sticky="w")
        start_entry = ttk.Entry(self._table, textvariable=row["start"], width=6, justify="right")
        start_entry.grid(row=grid_row, column=4, padx=4, pady=2, sticky="w")
        end_entry = ttk.Entry(self._table, textvariable=row["end"], width=6, justify="right")
        end_entry.grid(row=grid_row, column=5, padx=4, pady=2, sticky="w")
        delete = ttk.Button(self._table, text="删除", width=5,
                            command=lambda r=row: self.del_row(r))
        delete.grid(row=grid_row, column=6, padx=(4, 8), pady=2, sticky="w")

        for entry in (proj_entry, out_entry, prefix_entry, start_entry, end_entry):
            entry.bind("<Return>", self._finish_entry_edit)

        row["widgets"] = [label, proj_frame, out_frame, prefix_entry,
                          start_entry, end_entry, delete]
        self.rows.append(row)
        self._sync_scrollregion()

    def del_row(self, row: dict) -> None:
        if self._busy or self._running or row not in self.rows:
            return
        for widget in row["widgets"]:
            widget.destroy()
        self.rows.remove(row)
        self._renumber()
        self._sync_scrollregion()

    def _renumber(self) -> None:
        for index, row in enumerate(self.rows, 1):
            row["widgets"][0].configure(text=str(index))

    def on_clear_rows(self) -> None:
        if self._busy or self._running or not self.rows:
            return
        if not messagebox.askyesno("清空列表", f"确定清空全部 {len(self.rows)} 个工程吗？"):
            return
        for row in self.rows:
            for widget in row["widgets"]:
                widget.destroy()
        self.rows.clear()
        self._sync_scrollregion()
        log.info("工程列表已清空")

    # ------------------------------------------------------------- 启动与计划
    def _validate(self) -> tuple[list[RenderJob], str | None]:
        exe_first = clean_path_text(self.var_exe_first.get())
        exe_second = clean_path_text(self.var_exe_second.get())
        if not exe_first or not os.path.isfile(exe_first):
            return [], f"显卡 0 的 Blender 可执行文件不存在：\n{exe_first or '（未填写）'}"
        if not exe_second or not os.path.isfile(exe_second):
            return [], f"显卡 1 的 Blender 可执行文件不存在：\n{exe_second or '（未填写）'}"

        ratio_first = parse_ratio(self.var_ratio_first.get())
        ratio_second = parse_ratio(self.var_ratio_second.get())
        if ratio_first is None or ratio_second is None:
            return [], "分配比例必须是 0 到 100 之间的数字。"
        if abs(ratio_first + ratio_second - 100.0) > 1e-6:
            return [], "两张卡的比例之和必须等于 100。"

        if not self.rows:
            return [], "请先添加至少一个工程。"

        jobs: list[RenderJob] = []
        seen: set[tuple] = set()
        duplicates = 0
        for number, row in enumerate(self.rows, 1):
            proj = clean_path_text(row["proj"].get())
            out_dir = clean_path_text(row["out"].get())
            prefix = clean_path_text(row["prefix"].get())

            if not proj:
                return [], f"第 {number} 行：请填写工程文件路径。"
            if not out_dir:
                return [], f"第 {number} 行：请填写输出文件夹。"
            try:
                validate_prefix(prefix)
            except ValueError as exc:
                return [], f"第 {number} 行的命名前缀不合法：{exc}"
            if os.path.splitext(proj)[1].lower() != ".blend":
                return [], f"第 {number} 行不是 .blend 文件：\n{proj}"
            if not os.path.isfile(proj):
                return [], f"第 {number} 行的工程文件不存在：\n{proj}"
            try:
                start = int(row["start"].get().strip())
                end = int(row["end"].get().strip())
            except ValueError:
                return [], f"第 {number} 行：起始帧和结束帧必须是整数。"
            if start < 0 or end < 0:
                return [], f"第 {number} 行：帧号不能为负数。"
            if start > end:
                return [], f"第 {number} 行：起始帧不能大于结束帧。"
            if os.path.exists(out_dir) and not os.path.isdir(out_dir):
                return [], f"第 {number} 行的输出路径不是文件夹：\n{out_dir}"
            if not prefix:
                log.warning("第 %d 行前缀为空，将只识别纯数字文件名的帧。", number)

            key = (
                os.path.normcase(os.path.abspath(proj)),
                os.path.normcase(os.path.abspath(out_dir)),
                prefix,
                start,
                end,
            )
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            jobs.append(RenderJob(proj, out_dir, prefix, start, end))

        if duplicates:
            log.warning("已跳过 %d 个重复的工程行。", duplicates)

        for job in jobs:
            try:
                os.makedirs(job.out_dir, exist_ok=True)
            except OSError as exc:
                return [], f"无法创建输出目录：\n{job.out_dir}\n\n{exc}"
        return jobs, None

    def on_start(self) -> None:
        if self._busy or self._running:
            return
        jobs, error = self._validate()
        if error:
            log.warning("启动校验失败：%s", error.replace("\n", " "))
            messagebox.showerror("无法启动", error)
            return

        exe_first = clean_path_text(self.var_exe_first.get())
        exe_second = clean_path_text(self.var_exe_second.get())
        same = os.path.normcase(os.path.abspath(exe_first)) == os.path.normcase(os.path.abspath(exe_second))
        if same and not messagebox.askyesno(
            "确认",
            "两张卡选择了同一个 Blender 可执行文件，两个进程可能抢同一块显卡。\n仍要继续吗？",
        ):
            return

        ratio = parse_ratio(self.var_ratio_first.get())
        self._exe_first = exe_first
        self._exe_second = exe_second
        self._ratio = 60.0 if ratio is None else ratio
        self._save_config()
        self._busy = True
        self._update_controls()
        self._log("开始扫描输出目录…")
        threading.Thread(
            target=self._plan_worker, args=(jobs, self._ratio), name="planner", daemon=True
        ).start()

    def _plan_worker(self, jobs: list[RenderJob], ratio: float) -> None:
        try:
            cache: dict[tuple[str, str], set[int]] = {}
            plans = []
            for index, job in enumerate(jobs, 1):
                key = (os.path.normcase(os.path.abspath(job.out_dir)), job.prefix.lower())
                if key not in cache:
                    self._queue.put(("log", f"扫描 {index}/{len(jobs)}：{job.out_dir}"))
                    cache[key] = scan_rendered_frames(job.out_dir, job.prefix)
                plans.append((job, plan_project(
                    job.out_dir, job.prefix, job.start, job.end, ratio, cache[key])))
            self._queue.put(("planned", plans))
        except Exception as exc:  # noqa: BLE001 - 统一转成界面错误
            log.exception("扫描输出目录失败")
            self._queue.put(("error", f"扫描输出目录失败：{exc}"))

    def _on_plan_error(self, message: str) -> None:
        self._busy = False
        self._update_controls()
        self._log(message)
        messagebox.showerror("扫描失败", message)

    def _on_planned(self, plans: list) -> None:
        total_missing = 0
        first_jobs: list[PsJob] = []
        second_jobs: list[PsJob] = []
        for job, plan in plans:
            label = os.path.basename(job.proj) or job.out_dir
            out_pattern = make_output_path(job.out_dir, job.prefix)
            total_missing += plan.missing
            self._log(
                f"{label}：显卡 0 待渲 {_format_gaps(plan.first_gaps)}"
                f"（{_count_gaps(plan.first_gaps)} 帧）；"
                f"显卡 1 待渲 {_format_gaps(plan.second_gaps)}"
                f"（{_count_gaps(plan.second_gaps)} 帧）"
            )
            for gap in plan.first_gaps:
                first_jobs.append(PsJob(job.proj, out_pattern, gap.start, gap.end, label))
            for gap in plan.second_gaps:
                second_jobs.append(PsJob(job.proj, out_pattern, gap.start, gap.end, label))

        if total_missing == 0:
            self._busy = False
            self._update_controls()
            self._log("所有帧都已存在，无需续渲。")
            messagebox.showinfo("无需渲染", "所有工程的帧都已存在，没有需要补渲的帧。")
            return

        self._log(f"共需补渲 {total_missing} 帧。")
        self._launch(first_jobs, second_jobs)

    def _make_script_dir(self) -> Path:
        """临时脚本放在程序目录的 temp 下，不写系统 C 盘临时目录。"""
        base = temp_dir(_program_root())
        try:
            base.mkdir(parents=True, exist_ok=True)
            return Path(tempfile.mkdtemp(dir=str(base), prefix="run_"))
        except OSError as exc:
            raise RuntimeError(f"无法在程序目录创建临时文件夹：\n{base}\n{exc}") from exc

    def _cleanup_stale_temp(self) -> None:
        base = temp_dir(_program_root())
        if base.is_dir():
            for child in base.glob("run_*"):
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)

    def _cleanup_temp(self) -> None:
        directory, self._script_dir = self._script_dir, None
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)

    def _launch(self, first_jobs: list[PsJob], second_jobs: list[PsJob]) -> None:
        try:
            self._cleanup_temp()
            temp_dir = self._make_script_dir()
            self._script_dir = temp_dir
            self._log(f"临时脚本目录：{temp_dir}")
            chcp = bool(self.var_fix_encoding.get())
            scripts: list[tuple[Path, str]] = []
            if first_jobs:
                path = temp_dir / "GPU0.ps1"
                path.write_text(
                    build_ps(self._exe_first, "MultiGPU - 显卡 0", first_jobs, chcp),
                    encoding="utf-8-sig", newline="")
                scripts.append((path, "显卡 0"))
            if second_jobs:
                path = temp_dir / "GPU1.ps1"
                path.write_text(
                    build_ps(self._exe_second, "MultiGPU - 显卡 1", second_jobs, chcp),
                    encoding="utf-8-sig", newline="")
                scripts.append((path, "显卡 1"))
            if not scripts:
                raise RuntimeError("没有需要执行的渲染命令。")

            flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
            self._processes = []
            for path, label in scripts:
                process = subprocess.Popen(
                    ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(path)],
                    creationflags=flags,
                )
                self._processes.append(process)
                self._log(f"已启动{label}渲染进程（PID {process.pid}），关闭窗口前任务视为进行中。")
            self._stop_requested = False
            self._running = True
            self._busy = False
            self._update_controls()
            self.after(1000, self._poll_processes)
        except Exception as exc:  # noqa: BLE001 - 统一提示
            log.exception("启动渲染失败")
            self._cleanup_temp()
            self._busy = False
            self._running = False
            self._update_controls()
            messagebox.showerror("启动失败", f"无法启动渲染进程：\n{exc}")

    def _update_controls(self) -> None:
        state = "disabled" if (self._busy or self._running) else "normal"
        for button in (self._btn_add, self._btn_start, self._btn_clear, self._btn_placeholders):
            button.configure(state=state)
        self._btn_stop.configure(state="normal" if self._running else "disabled")

    def _poll_processes(self) -> None:
        if self._closed or not self._processes:
            return
        if any(process.poll() is None for process in self._processes):
            self.after(1000, self._poll_processes)
            return

        processes, self._processes = self._processes, []
        self._running = False
        self._update_controls()
        failed = []
        for process in processes:
            arguments = process.args
            script = arguments[-1] if isinstance(arguments, (list, tuple)) else str(arguments)
            self._log(f"渲染进程结束：{os.path.basename(str(script))}，退出码 {process.returncode}")
            if process.returncode:
                failed.append(process.returncode)
        self._cleanup_temp()
        if self._stop_requested:
            self._stop_requested = False
            messagebox.showinfo("已停止", "渲染进程已停止。")
        elif failed:
            messagebox.showwarning(
                "渲染结束", f"有 {len(failed)} 个渲染进程以非零退出码结束，请查看对应窗口的输出。")
        else:
            messagebox.showinfo("渲染结束", "所有渲染进程已结束。")

    def on_stop(self) -> None:
        if not self._processes:
            return
        if not messagebox.askyesno(
            "停止渲染", "确定终止正在运行的渲染进程吗？\n未完成的帧会保留为占位文件，下次启动会自动补渲。"):
            return
        self._stop_requested = True
        for process in list(self._processes):
            if process.poll() is None:
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                        capture_output=True,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                except OSError as exc:
                    log.error("停止进程 %s 失败：%s", process.pid, exc)
        self._log("已发送停止命令，等待进程退出…")

    # --------------------------------------------------------------- 占位文件
    def on_clear_placeholders(self) -> None:
        if self._busy or self._running:
            return
        targets: list[str] = []
        seen: set[str] = set()
        scanned = 0
        for row in self.rows:
            out_dir = clean_path_text(row["out"].get())
            if not out_dir or not os.path.isdir(out_dir):
                continue
            scanned += 1
            prefix = clean_path_text(row["prefix"].get())
            try:
                with os.scandir(out_dir) as entries:
                    items = list(entries)
            except OSError as exc:
                log.warning("无法读取目录 %s：%s", out_dir, exc)
                continue
            for entry in items:
                try:
                    if not entry.is_file() or not is_image_name(entry.name):
                        continue
                except OSError:
                    continue
                if frame_from_name(entry.name, prefix) is None:
                    continue
                if not is_placeholder_file(entry.path):
                    continue
                absolute = os.path.abspath(entry.path)
                if absolute not in seen:
                    seen.add(absolute)
                    targets.append(absolute)

        if scanned == 0:
            messagebox.showinfo("清除占位文件", "工程列表里没有已存在的输出文件夹。")
            return
        if not targets:
            messagebox.showinfo("清除占位文件", "没有发现需要清除的占位或损坏图片。")
            return

        preview = "\n".join(targets[:12])
        if len(targets) > 12:
            preview += f"\n… 共 {len(targets)} 个文件"
        if not messagebox.askyesno(
            "确认清除",
            f"将把以下 {len(targets)} 个占位/损坏图片移到回收站：\n\n{preview}\n\n确定继续吗？",
        ):
            return
        if recycle_paths(targets):
            log.info("已清除 %d 个占位文件。", len(targets))
            messagebox.showinfo("清除完成", f"已把 {len(targets)} 个文件移入回收站。")
        else:
            log.error("清除占位文件失败，共 %d 个。", len(targets))
            messagebox.showwarning("清除失败", "部分文件无法移入回收站，请检查是否被占用。")

    # ------------------------------------------------------------------ 关闭
    def _on_close(self) -> None:
        if self._running and self._processes:
            if not messagebox.askyesno("退出", "渲染仍在进行，退出会终止渲染进程。确定退出吗？"):
                return
            for process in list(self._processes):
                if process.poll() is None:
                    try:
                        subprocess.run(
                            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                            capture_output=True,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        )
                    except OSError:
                        pass
        self._cleanup_temp()
        self._save_config()
        self._closed = True
        self.destroy()

    def report_callback_exception(self, exc_type, exc_value, tb) -> None:  # noqa: N802
        log.error("界面回调异常", exc_info=(exc_type, exc_value, tb))
        try:
            messagebox.showerror("发生错误", f"{exc_type.__name__}: {exc_value}")
        except tk.TclError:
            pass


def _excepthook(exc_type, exc_value, tb) -> None:
    log.critical("未捕获异常", exc_info=(exc_type, exc_value, tb))


def _thread_excepthook(args) -> None:
    name = args.thread.name if args.thread is not None else "?"
    log.error("线程 %s 未捕获异常", name,
              exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


def main() -> None:
    try:
        ensure_supported_platform()
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return
    setup_logging()
    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    enable_dpi_awareness()

    instance = SingleInstance()
    if not instance.acquire():
        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning("MultiGPU", "MultiGPU 已经在运行，请先关闭已有窗口。")
        root.destroy()
        instance.release()
        return
    try:
        app = MultiGPUApp(instance)
        app.mainloop()
    finally:
        instance.release()


if __name__ == "__main__":
    main()
