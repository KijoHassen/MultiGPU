# -*- coding: utf-8 -*-
"""
MultiGPU - Blender 双卡分段渲染 GUI
=========================================================
功能：
  1. 双卡设置自动记忆：exe 路径 + 比例
     保存到与程序同级 config 目录的 MultiGPU_config.json。
  2. 渲染比例两个框都能手输，连锁互补为 100%：
     改 GPU0 -> GPU1 自动 = 100-GPU0；改 GPU1 -> GPU0 自动 = 100-GPU1。
  3. 每个工程按 GPU0 的比例切成两段连续帧区间：
        GPU0 (blender_gpu0.exe):  s_in ~ cut
        GPU1 (blender_gpu1.exe):  cut+1 ~ e_in
  4. 起始帧允许为 0。
  5. 启动前扫描输出目录，用正则提取已渲染帧号（文件名里最后一组连续数字）；
     某段内已有渲染帧时（假设从段首连续渲染），
     该卡改为从“段内最大已渲染帧 + 1”继续；
     该段已全部渲完则该卡此工程不生成命令。
  6. 生成两个 PowerShell 多行脚本并自动启动，
     用 -ExecutionPolicy Bypass 运行，全程无需按 Enter。
=========================================================
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# 常见渲染输出扩展名（如需支持更多格式，往这里加即可）
IMAGE_EXTS = {
    ".png", ".jpg", ".jpeg", ".bmp", ".tga", ".tif", ".tiff",
    ".exr", ".hdr", ".dpx", ".webp", ".cin", ".psd", ".jp2", ".j2c",
}
DIGIT_RE = re.compile(r"\d+")


# ----------------------------------------------------------------------
# 占位文件识别：空文件，或内容无法作为图片打开的，都算占位文件
# ----------------------------------------------------------------------
_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_PNG_IEND = b"\x00\x00\x00\x00IEND"
_JPEG_SIG = b"\xff\xd8\xff"
_JPEG_END = b"\xff\xd9"
_TIFF_SIGS = (b"II*\x00", b"MM\x00*")
_EXR_SIG = b"\x76\x2f\x31\x01"
_HDR_SIGS = (b"#?RADIANCE", b"#?RGBE")
_DPX_SIGS = (b"SDPX", b"XPDS")
_CIN_SIGS = (b"\x80\x2a\x5f\xd7", b"\xd7\x5f\x2a\x80")
_J2C_SIG = b"\xff\x4f\xff\x51"
_JP2_SIG = b"\x00\x00\x00\x0cjP  \r\n\x87\n"


def _png_has_iend(path):
    """PNG 要求文件末尾是完整的 IEND 块；被截断/中断的文件通常没有。"""
    try:
        size = os.path.getsize(path)
        if size < 12:
            return False
        with open(path, "rb") as fh:
            fh.seek(-12, os.SEEK_END)
            return fh.read(12).startswith(_PNG_IEND)
    except OSError:
        return False


def _jpeg_has_end(path):
    """JPEG 正常文件以 FF D9（EOI）结尾。"""
    try:
        with open(path, "rb") as fh:
            fh.seek(-2, os.SEEK_END)
            return fh.read(2) == _JPEG_END
    except OSError:
        return False


def is_placeholder_file(path: str) -> bool:
    """返回 True 表示该文件是占位/无效文件，False 表示内容看起来是正常图片。"""
    try:
        if os.path.getsize(path) <= 0:
            return True
    except OSError:
        return True

    try:
        with open(path, "rb") as fh:
            head = fh.read(32)
    except OSError:
        return True
    if not head:
        return True

    # 按文件内容识别：能匹配常见图片格式头就按“正常图片”放行
    if head.startswith(_PNG_SIG):
        return not _png_has_iend(path)
    if head.startswith(_JPEG_SIG):
        return not _jpeg_has_end(path)
    if head.startswith(b"BM"):
        return False
    if head.startswith(_TIFF_SIGS[0]) or head.startswith(_TIFF_SIGS[1]):
        return False
    if head.startswith(_EXR_SIG):
        return False
    if head.startswith(_HDR_SIGS[0]) or head.startswith(_HDR_SIGS[1]):
        return False
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return False
    if head.startswith(b"8BPS"):
        return False
    if head.startswith(_JP2_SIG) or head.startswith(_J2C_SIG):
        return False
    if head.startswith(_DPX_SIGS[0]) or head.startswith(_DPX_SIGS[1]):
        return False
    if head.startswith(_CIN_SIGS[0]) or head.startswith(_CIN_SIGS[1]):
        return False

    # TGA 没有统一魔数，用 18 字节文件头里的类型字段做判断
    if os.path.splitext(path)[1].lower() == ".tga":
        if len(head) < 18:
            return True
        return not (head[1] in (0, 1) and head[2] in (1, 2, 3, 9, 10, 11))

    # 其余内容（txt、tmp、只有少量字节的残缺文件等）一律按占位处理
    return True


def move_to_recycle_bin(path: str) -> bool:
    """Windows 下把文件放进回收站；失败返回 False，不直接永久删除。"""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [
                ("hwnd", wintypes.HWND),
                ("wFunc", wintypes.UINT),
                ("pFrom", wintypes.LPCWSTR),
                ("pTo", wintypes.LPCWSTR),
                ("fFlags", wintypes.WORD),
                ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", wintypes.LPVOID),
                ("lpszProgressTitle", wintypes.LPCWSTR),
            ]

        FO_DELETE = 3
        FOF_ALLOWUNDO = 0x0040
        FOF_NOCONFIRMATION = 0x0010
        FOF_SILENT = 0x0004
        FOF_NOERRORUI = 0x0400

        src = os.path.abspath(path) + "\x00\x00"
        op = SHFILEOPSTRUCTW()
        op.hwnd = None
        op.wFunc = FO_DELETE
        op.pFrom = src
        op.pTo = None
        op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
        op.fAnyOperationsAborted = False
        op.hNameMappings = None
        op.lpszProgressTitle = None

        shell = ctypes.windll.shell32.SHFileOperationW
        shell.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
        shell.restype = ctypes.c_int
        result = shell(ctypes.byref(op))
        return result == 0 and not op.fAnyOperationsAborted
    except Exception:
        return False


# ----------------------------------------------------------------------
# 输出路径模板：文件夹 + 自定义前缀 + 4 位帧号占位符
# ----------------------------------------------------------------------
def validate_prefix(prefix):
    """去掉首尾空格；文件名非法字符/‘#’ 会打乱 Blender 帧号模板，直接报错。"""
    prefix = (prefix or "").strip()
    if re.search(r'[\\/:*?"<>|#\x00-\x1f]', prefix):
        raise ValueError(
            "命名前缀不能包含字符：\\ / : * ? \" < > | #\n"
            "（留空时输出就只有数字序号，例如 0001.png）")
    return prefix


def clean_path_text(text):
    """接受带双引号（英文 "" 或中文 “”）或不带引号的路径。"""
    text = (text or "").strip()
    if len(text) >= 2:
        if (text[0] == '"' and text[-1] == '"') or \
           (text[0] == '\u201c' and text[-1] == '\u201d'):
            text = text[1:-1].strip()
    return text


def make_output_path(out_dir, prefix):
    """生成 Blender -o 使用的完整帧路径模板。
    例如 out_dir + 'shot_' + '####' -> shot_0001.png，文件一定在 out_dir 内。"""
    out_dir = clean_path_text(out_dir)
    prefix = validate_prefix(prefix)
    return os.path.join(out_dir, prefix + "####")


# ----------------------------------------------------------------------
# 输出目录扫描：用正则提取“已渲染帧号”
# ----------------------------------------------------------------------
def scan_rendered_frames(folder: str) -> set:
    if not os.path.isdir(folder):
        return set()
    frames = set()
    try:
        names = os.listdir(folder)
    except OSError:
        return set()
    for name in names:
        ext = os.path.splitext(name)[1].lower()
        if ext not in IMAGE_EXTS:          # 只认图片/图像格式文件
            continue
        path = os.path.join(folder, name)
        if is_placeholder_file(path):      # 空占位/无效文件不算已渲染帧
            continue
        stem = os.path.splitext(name)[0]   # 去掉扩展名
        nums = DIGIT_RE.findall(stem)      # 正则：连续数字
        if not nums:
            continue
        try:
            frames.add(int(nums[-1]))      # 取最后一组数字作为帧号
        except ValueError:
            pass
    return frames


# ----------------------------------------------------------------------
# 单工程分析：切分 + 续传
# 返回 first/second 两段：
#   段为空或已全部渲染完 -> None
#   否则 -> {"s": 实际起始帧, "e": 结束帧, "max_rendered": 已渲最大帧或None}
# ----------------------------------------------------------------------
def plan_project(proj_path, out_dir, s_in, e_in, ratio):
    total = e_in - s_in + 1
    cut = s_in + int(total * ratio / 100.0) - 1   # GPU0 段的末尾
    cut = max(s_in, min(cut, e_in - 1))           # 安全夹在范围内

    rendered = scan_rendered_frames(out_dir)      # 该工程输出目录已渲帧

    def _seg(seg_s, seg_e):
        if seg_s > seg_e:
            return None
        inside = [f for f in rendered if seg_s <= f <= seg_e]
        if inside:
            start = max(inside) + 1               # 段内最大已渲帧 + 1
        else:
            start = seg_s                         # 本段全没渲，用原比例起点
        if start > seg_e:                         # 本段已全部渲完
            return None
        return {"s": start, "e": seg_e,
                "max_rendered": (max(inside) if inside else None)}

    return {
        "rendered": rendered,
        "cut": cut,
        "first":  _seg(s_in, cut),        # GPU0 / blender_gpu0.exe
        "second": _seg(cut + 1, e_in),    # GPU1 / blender_gpu1.exe
    }


# ----------------------------------------------------------------------
# 生成 PowerShell 多行脚本文本
# ----------------------------------------------------------------------
def build_ps(exe_path, card_title, jobs, chcp_utf8=False):
    """
    jobs: [(工程路径, 输出路径模板, 起始帧, 结束帧), ...]
    成功后窗口自动关闭；有失败时保留窗口等待回车，方便看错误。
    """
    total = len(jobs)
    lines = []
    if chcp_utf8:
        # 先切到 UTF-8 代码页，避免中文日志在命令行里显示成乱码
        lines.append("chcp 65001 > $null")
    lines.append('$Host.UI.RawUI.WindowTitle = "%s"' % card_title)
    lines.append('Write-Host "========== %s 开始 =========="' % card_title)
    lines.append("$failed = $false")

    for i, (proj, out_path, s, e) in enumerate(jobs, 1):
        lines.append(
            'Write-Host "[%d/%d] 工程: %s | 帧: %d - %d"'
            % (i, total, os.path.basename(proj), s, e)
        )
        # 用 & 调用操作符 + 双引号，路径带空格也没问题
        cmd = '& "%s" -b "%s" -o "%s" -s %d -e %d -a' % (exe_path, proj, out_path, s, e)
        lines.append(cmd)
        lines.append(
            'if ($LASTEXITCODE -ne 0) { '
            'Write-Host ("渲染失败，退出码=" + $LASTEXITCODE); $failed = $true }'
        )

    lines.append('Write-Host ""')
    lines.append(
        'if ($failed) { '
        'Write-Host "存在失败任务，请查看上方输出，按回车关闭窗口"; Read-Host } '
        'else { Write-Host ""; Write-Host "渲染队列已全部渲染完成"; '
        'Write-Host "按回车关闭窗口"; Read-Host }'
    )
    return "\r\n".join(lines) + "\r\n"


# ----------------------------------------------------------------------
# 主界面
# ----------------------------------------------------------------------
class MultiGPUApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("MultiGPU")
        self.geometry("1040x740")
        self.rows = []          # 每行工程的控件引用
        self.row_index = 0
        self.var_fix_encoding = tk.BooleanVar(value=False)   # chcp 65001 开关

        # ================= 顶部：双卡设置 =================
        top = ttk.LabelFrame(self, text="双卡设置")
        top.pack(fill="x", padx=8, pady=(8, 4))

        # GPU0 / blender_gpu0.exe 路径
        ttk.Label(top, text="GPU0：选择 blender_gpu0.exe（渲前半段）").grid(
            row=0, column=0, sticky="w", padx=6, pady=4)
        self.var_exe_first = tk.StringVar()
        ent_exe_first = ttk.Entry(top, textvariable=self.var_exe_first, width=58)
        ent_exe_first.grid(row=0, column=1, padx=2)
        ent_exe_first.bind("<Return>", self._finish_entry_edit)
        ttk.Button(top, text="浏览…", width=8,
                   command=lambda: self._pick_exe(self.var_exe_first)).grid(
            row=0, column=2, padx=4)

        # GPU1 / blender_gpu1.exe 路径
        ttk.Label(top, text="GPU1：选择 blender_gpu1.exe（渲后半段）").grid(
            row=1, column=0, sticky="w", padx=6, pady=4)
        self.var_exe_second = tk.StringVar()
        ent_exe_second = ttk.Entry(top, textvariable=self.var_exe_second, width=58)
        ent_exe_second.grid(row=1, column=1, padx=2)
        ent_exe_second.bind("<Return>", self._finish_entry_edit)
        ttk.Button(top, text="浏览…", width=8,
                   command=lambda: self._pick_exe(self.var_exe_second)).grid(
            row=1, column=2, padx=4)

        # 渲染比例：两个框都可手输，GPU0 + GPU1 自动互补为 100%
        self.var_ratio_first = tk.StringVar(value="60")
        self.var_ratio_second = tk.StringVar(value="40")
        self._ratio_updating = False
        self.var_ratio_first.trace_add("write", self._ratio_first_changed)
        self.var_ratio_second.trace_add("write", self._ratio_second_changed)

        ratio_row = ttk.Frame(top)
        ratio_row.grid(row=2, column=0, columnspan=3, sticky="w", padx=6, pady=6)
        ttk.Label(ratio_row, text="渲染比例：").pack(side="left")
        ttk.Label(ratio_row, text="GPU0").pack(side="left", padx=(8, 2))
        ent_ratio_first = ttk.Entry(ratio_row, textvariable=self.var_ratio_first,
                                    width=8)
        ent_ratio_first.pack(side="left")
        ent_ratio_first.bind("<Return>", self._finish_entry_edit)
        ttk.Label(ratio_row, text="%").pack(side="left")
        ttk.Label(ratio_row, text="GPU1").pack(side="left", padx=(14, 2))
        ent_ratio_second = ttk.Entry(ratio_row, textvariable=self.var_ratio_second,
                                     width=8)
        ent_ratio_second.pack(side="left")
        ent_ratio_second.bind("<Return>", self._finish_entry_edit)
        ttk.Label(ratio_row, text="%").pack(side="left")

        # 乱码修复开关：放进双卡设置并写入配置文件
        fix_row = ttk.Frame(top)
        fix_row.grid(row=3, column=0, columnspan=3,
                     sticky="w", padx=6, pady=4)
        ttk.Label(fix_row, text="乱码修复：").pack(side="left")
        self.btn_fix_encoding = ttk.Button(
            fix_row, command=self._toggle_fix_encoding)
        self.btn_fix_encoding.pack(side="left", padx=(0, 0))
        self._update_fix_encoding_button()

        # 操作按钮固定放在设置下方，不参与日志区域缩放，避免被拖动遮住
        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=8, pady=(4, 0))
        ttk.Button(btns, text="＋ 添加工程", command=self.add_row).pack(side="left")
        ttk.Button(btns, text="启动渲染", command=self.on_start).pack(
            side="left", padx=8)
        ttk.Button(btns, text="清空列表", command=self.clear_rows).pack(side="left")
        ttk.Button(btns, text="清除占位文件",
                   command=self.on_clear_placeholders).pack(
            side="left", padx=(8, 0))

        # ================= 中部工程列表 + 底部日志（可拖动分隔） =================
        self.paned = tk.PanedWindow(self, orient="vertical",
                                    sashwidth=8, sashrelief="raised",
                                    showhandle=True)
        self.paned.pack(fill="both", expand=True, padx=8, pady=(4, 0))

        project_area = ttk.Frame(self.paned)
        mid = ttk.LabelFrame(project_area, text="工程列表")
        mid.pack(fill="both", expand=True, padx=2, pady=(2, 0))

        # 滚动区
        sc = ttk.Frame(mid)
        sc.pack(fill="both", expand=True, padx=6, pady=4)

        self.vsb = ttk.Scrollbar(sc, orient="vertical")
        self.hsb = ttk.Scrollbar(sc, orient="horizontal")
        self.canvas = tk.Canvas(sc, highlightthickness=0)
        self.list_frame = ttk.Frame(self.canvas)

        # 先放滚动条，再放画布，确保两个滚动条都能正常显示
        self.vsb.pack(side="right", fill="y")
        self.hsb.pack(side="bottom", fill="x")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.canvas.configure(yscrollcommand=self.vsb.set,
                              xscrollcommand=self.hsb.set)
        self.vsb.configure(command=self.canvas.yview)
        self.hsb.configure(command=self.canvas.xview)
        self.list_frame.bind("<Configure>", self._refresh_scrollregion)
        self.canvas.create_window((0, 0), window=self.list_frame, anchor="nw")

        # 表头和下面的输入框放在同一个网格容器里，列宽天然对齐
        for text, col, span in (
            ("序号", 0, 1),
            ("工程路径", 1, 2),
            ("输出文件夹", 3, 2),
            ("命名前缀", 5, 1),
            ("起始帧", 6, 1),
            ("结束帧", 7, 1),
            ("操作", 8, 1),
        ):
            ttk.Label(self.list_frame, text=text, anchor="w").grid(
                row=0, column=col, columnspan=span, sticky="w",
                padx=(0, 4), pady=(0, 3))

        # 工程列表至少保留表头 + 一行工程；日志区不能缩到不可见
        self.paned.add(project_area, stretch="always", minsize=100)

        # ================= 底部：日志 =================
        self.logf = ttk.LabelFrame(self.paned, text="运行日志 / 分配预览")
        logf = self.logf
        self.log_text = tk.Text(logf, height=9, state="disabled", wrap="word")
        self.log_vsb = ttk.Scrollbar(logf, orient="vertical",
                                     command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=self.log_vsb.set)
        self.log_text.pack(side="left", fill="both", expand=True,
                           padx=(4, 0), pady=4)
        self.log_vsb.pack(side="right", fill="y", padx=(0, 4), pady=4)
        self.paned.add(logf, stretch="always", minsize=70)

        # 滚轮按鼠标所在区域独立滚动，项目列表与日志互不联动
        self.bind_all("<MouseWheel>", self._on_wheel)
        # 点击空白处退出输入状态
        self.bind("<Button-1>", self._on_blank_click)

        self._load_icon()
        self.add_row()   # 默认先给一行

        # 记忆功能：加载上次保存的双卡设置
        self.load_config()
        # 关闭窗口时自动保存
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # 窗口显示后按实际控件尺寸设置最小宽高
        self.after(100, self._apply_window_min_size)

    # ---------------- UI 辅助 ----------------
    def _on_wheel(self, event):
        """滚轮只滚动鼠标当前所在的区域：项目列表或日志彼此独立。"""
        try:
            widget = self.winfo_containing(event.x_root, event.y_root) \
                     or event.widget
        except Exception:
            widget = event.widget

        target = None
        node = widget
        while node is not None:
            if node is self.log_text or node is self.log_vsb or node is self.logf:
                target = "log"
                break
            if node is self.canvas or node is self.list_frame:
                target = "list"
                break
            node = getattr(node, "master", None)

        step = -1 if event.delta > 0 else 1
        if target == "log":
            self.log_text.yview_scroll(step, "units")
        elif target == "list":
            if getattr(event, "state", 0) & 0x0001:   # 按住 Shift 横向滚动
                self.canvas.xview_scroll(step, "units")
            else:
                self.canvas.yview_scroll(step, "units")
        return "break"

    def _refresh_scrollregion(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _apply_window_min_size(self):
        """限制窗口最窄/最矮：右侧保留删除按钮，上方至少保留第一行工程。"""
        try:
            self.update_idletasks()
            # 画布/滚动条之外的水平开销 + 工程行宽度
            overhead = max(self.winfo_width() - self.canvas.winfo_width(), 40)
            min_width = self.list_frame.winfo_reqwidth() + overhead + 2
            # 顶部设置 + 按钮固定占用后，paned 至少容纳“一行工程 + 分隔条 + 日志最小高度”
            min_height = self.paned.winfo_y() + 100 + 8 + 70 + 40
            self.minsize(min_width, min_height)
        except Exception:
            pass

    def _finish_entry_edit(self, event):
        """输入完成后按 Enter：确认输入并退出编辑状态，不再跳到下一个控件。"""
        self.focus_set()
        return "break"

    def _on_blank_click(self, event):
        """点击空白区域时退出输入状态；点输入框/按钮时保留系统默认行为。"""
        widget = event.widget
        if isinstance(widget, (ttk.Entry, ttk.Button, tk.Entry, tk.Button)):
            return
        self.focus_set()

    def _update_fix_encoding_button(self):
        if self.var_fix_encoding.get():
            self.btn_fix_encoding.configure(text="● 已开启（chcp 65001）")
        else:
            self.btn_fix_encoding.configure(text="○ 已关闭")

    def _toggle_fix_encoding(self):
        self.var_fix_encoding.set(not self.var_fix_encoding.get())
        self._update_fix_encoding_button()
        self.save_config()
        if self.var_fix_encoding.get():
            self._log_action("已开启命令行乱码修复（脚本开头添加 chcp 65001）")
        else:
            self._log_action("已关闭命令行乱码修复")

    def _project_root(self):
        """程序所在目录：exe 或 .py 的同级目录。"""
        if getattr(sys, "frozen", False):
            return os.path.dirname(os.path.abspath(sys.executable))
        return os.path.dirname(os.path.abspath(__file__))

    def _load_icon(self):
        """任务栏/窗口左上角图标：读取同级 icon\\MultiGPU.png。"""
        icon_path = os.path.join(self._project_root(), "icon", "MultiGPU.png")
        if not os.path.isfile(icon_path):
            return
        try:
            self._icon_image = tk.PhotoImage(file=icon_path)
            self.iconphoto(True, self._icon_image)
        except Exception:
            self._icon_image = None

    def _pick_exe(self, var):
        path = filedialog.askopenfilename(
            title="选择 Blender 可执行文件",
            filetypes=[("exe 程序", "*.exe"), ("所有文件", "*.*")])
        if path:
            var.set(path)

    def _parse_ratio(self, text):
        """把字符串解析成 0~100 之间的比例，非法返回 None"""
        try:
            v = float(text.strip())
        except ValueError:
            return None
        if not (0 < v < 100):
            return None
        return v

    def _ratio_first_changed(self, *_):
        # 用户改 GPU0 -> GPU1 自动 = 100 - GPU0
        if self._ratio_updating:
            return
        v = self._parse_ratio(self.var_ratio_first.get())
        if v is None:
            return
        self._ratio_updating = True
        self.var_ratio_second.set(str(100 - v))
        self._ratio_updating = False

    def _ratio_second_changed(self, *_):
        # 用户改 GPU1 -> GPU0 自动 = 100 - GPU1
        if self._ratio_updating:
            return
        v = self._parse_ratio(self.var_ratio_second.get())
        if v is None:
            return
        self._ratio_updating = True
        self.var_ratio_first.set(str(100 - v))
        self._ratio_updating = False

    def _log(self, text):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _log_action(self, title):
        """一次独立操作的分隔标题，带时间戳并自动空一行。"""
        self.log_text.configure(state="normal")
        if self.log_text.get("1.0", "end-1c").strip():
            self.log_text.insert("end", "\n")
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        self.log_text.insert("end",
                             "---- [%s] %s ----\n" % (stamp, title))
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # ---------------- 记忆功能：配置文件 ----------------
    def _config_dir(self):
        """配置文件统一放在程序同级目录下的 config 文件夹。"""
        folder = os.path.join(self._project_root(), "config")
        os.makedirs(folder, exist_ok=True)
        return folder

    def _config_path(self):
        return os.path.join(self._config_dir(), "MultiGPU_config.json")

    def load_config(self):
        """启动时读取上次保存的双卡设置"""
        try:
            with open(self._config_path(), "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if cfg.get("exe_first"):
                self.var_exe_first.set(clean_path_text(cfg["exe_first"]))
            if cfg.get("exe_second"):
                self.var_exe_second.set(clean_path_text(cfg["exe_second"]))
            r1 = cfg.get("ratio_first", 60)
            r2 = cfg.get("ratio_second", 40)
            if self._parse_ratio(str(r1)):
                self._ratio_updating = True
                self.var_ratio_first.set(str(r1))
                self._ratio_updating = False
            if self._parse_ratio(str(r2)):
                self._ratio_updating = True
                self.var_ratio_second.set(str(r2))
                self._ratio_updating = False
            self.var_fix_encoding.set(bool(cfg.get("fix_encoding", False)))
            self._update_fix_encoding_button()
        except Exception:
            pass   # 首次运行或配置文件损坏：保持默认值

    def save_config(self):
        """保存当前双卡设置到配置文件"""
        cfg = {
            "exe_first": clean_path_text(self.var_exe_first.get()),
            "exe_second": clean_path_text(self.var_exe_second.get()),
            "ratio_first": self._parse_ratio(self.var_ratio_first.get()) or 60,
            "ratio_second": self._parse_ratio(self.var_ratio_second.get()) or 40,
            "fix_encoding": bool(self.var_fix_encoding.get()),
        }
        try:
            with open(self._config_path(), "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _on_close(self):
        self.save_config()
        self.destroy()

    # ---------------- 工程行增删 ----------------
    def add_row(self):
        self.row_index += 1
        row = {}
        parent = self.list_frame

        row["idx"] = ttk.Label(parent, width=6, anchor="w")

        row["proj"] = tk.StringVar()
        row["proj_ent"] = ttk.Entry(parent, textvariable=row["proj"], width=46)
        row["proj_btn"] = ttk.Button(
            parent, text="…", width=3,
            command=lambda: self._pick_proj(row["proj"]))

        row["out"] = tk.StringVar()
        row["out_ent"] = ttk.Entry(parent, textvariable=row["out"], width=32)
        row["out_btn"] = ttk.Button(
            parent, text="…", width=3,
            command=lambda: self._pick_dir(row["out"]))

        row["prefix"] = tk.StringVar(value="")
        row["prefix_ent"] = ttk.Entry(parent, textvariable=row["prefix"], width=12)

        row["start"] = tk.StringVar(value="1")
        row["start_ent"] = ttk.Entry(parent, textvariable=row["start"], width=8)
        row["end"] = tk.StringVar(value="250")
        row["end_ent"] = ttk.Entry(parent, textvariable=row["end"], width=8)

        row["del_btn"] = ttk.Button(
            parent, text="删除", width=6,
            command=lambda r=row: self.del_row(r))

        # 输入框里按 Enter 相当于确认并跳到下一个输入框
        for ent in (row["proj_ent"], row["out_ent"], row["prefix_ent"],
                    row["start_ent"], row["end_ent"]):
            ent.bind("<Return>", self._finish_entry_edit)

        self.rows.append(row)
        self._relayout_rows()

    def del_row(self, row):
        for key in ("idx", "proj_ent", "proj_btn", "out_ent", "out_btn",
                    "prefix_ent", "start_ent", "end_ent", "del_btn"):
            row[key].destroy()
        self.rows.remove(row)
        self._relayout_rows()

    def clear_rows(self):
        for r in list(self.rows):
            self.del_row(r)

    def _relayout_rows(self):
        """所有数据行用网格重新摆放，序号同时更新。
        表头占用第 0 行，数据从第 1 行开始，因此列宽与表头始终一致。"""
        for i, r in enumerate(self.rows, 1):
            r["idx"].configure(text=str(i))
            r["idx"].grid(row=i, column=0, sticky="w", padx=(0, 4), pady=1)
            r["proj_ent"].grid(row=i, column=1, sticky="w",
                               padx=(0, 2), pady=1)
            r["proj_btn"].grid(row=i, column=2, sticky="w",
                               padx=(0, 4), pady=1)
            r["out_ent"].grid(row=i, column=3, sticky="w",
                              padx=(0, 2), pady=1)
            r["out_btn"].grid(row=i, column=4, sticky="w",
                              padx=(0, 4), pady=1)
            r["prefix_ent"].grid(row=i, column=5, sticky="w",
                                 padx=(0, 2), pady=1)
            r["start_ent"].grid(row=i, column=6, sticky="w",
                                padx=(0, 2), pady=1)
            r["end_ent"].grid(row=i, column=7, sticky="w",
                              padx=(0, 4), pady=1)
            r["del_btn"].grid(row=i, column=8, sticky="w", pady=1)
        self._refresh_scrollregion()

    # ---------------- 清除占位文件 ----------------
    def on_clear_placeholders(self):
        folders = []
        for r in self.rows:
            out = clean_path_text(r["out"].get())
            if out and out not in folders:
                folders.append(out)
        if not folders:
            self._log_action("清除占位文件：请先填写“输出文件夹”")
            return
        self._log_action("开始清除占位文件（输出文件夹 %d 个）" % len(folders))

        scanned = 0
        removed = 0
        failed = 0
        for folder in folders:
            if not os.path.isdir(folder):
                self._log("输出文件夹不存在，跳过：" + folder)
                continue
            try:
                names = os.listdir(folder)
            except OSError as ex:
                self._log("无法读取输出文件夹：%s（%s）" % (folder, ex))
                continue
            for name in names:
                path = os.path.join(folder, name)
                if not os.path.isfile(path):
                    continue
                scanned += 1
                if not is_placeholder_file(path):
                    continue
                if move_to_recycle_bin(path):
                    removed += 1
                    self._log("已移入回收站：" + path)
                else:
                    failed += 1
                    self._log("移入回收站失败（文件可能被占用）：" + path)

        if scanned == 0:
            self._log("扫描结果：这些输出文件夹里没有文件。")
        elif removed == 0 and failed == 0:
            self._log("扫描结果：共检查 %d 个文件，未发现占位文件。" % scanned)
        else:
            self._log("扫描结果：检查 %d 个文件，%d 个已移入回收站%s。"
                      % (scanned, removed,
                         ("，%d 个移动失败" % failed) if failed else ""))

    def _pick_proj(self, var):
        path = filedialog.askopenfilename(
            title="选择 .blend 工程",
            filetypes=[("Blender 工程", "*.blend"), ("所有文件", "*.*")])
        if path:
            var.set(path)

    def _pick_dir(self, var):
        path = filedialog.askdirectory(title="选择输出文件夹")
        if path:
            var.set(path)

    # ---------------- 启动渲染 ----------------
    def on_start(self):
        # 1) 收集并校验输入
        try:
            exe_first = clean_path_text(self.var_exe_first.get())   # GPU0
            exe_second = clean_path_text(self.var_exe_second.get()) # GPU1

            ratio = self._parse_ratio(self.var_ratio_first.get())
            if ratio is None:
                raise ValueError("GPU0 渲染比例必须是 1 ~ 99 之间的数")
            r2 = self._parse_ratio(self.var_ratio_second.get())
            if r2 is None or abs(ratio + r2 - 100) > 0.001:
                raise ValueError("GPU0 与 GPU1 的比例之和必须为 100")

            if not os.path.isfile(exe_first):
                raise ValueError("找不到 GPU0 的 blender_gpu0.exe：\n" + exe_first)
            if not os.path.isfile(exe_second):
                raise ValueError("找不到 GPU1 的 blender_gpu1.exe：\n" + exe_second)

            projects = []
            for r in self.rows:
                p = clean_path_text(r["proj"].get())
                o = clean_path_text(r["out"].get())
                prefix = r["prefix"].get()
                s = r["start"].get().strip()
                e = r["end"].get().strip()
                if not p or not o:
                    raise ValueError("工程路径 / 输出文件夹 不能为空")
                prefix = validate_prefix(prefix)
                if not p.lower().endswith(".blend"):
                    raise ValueError("工程必须是 .blend 文件：\n" + p)
                if not os.path.isfile(p):
                    raise ValueError("工程文件不存在：\n" + p)
                try:
                    s = int(s)
                    e = int(e)
                except ValueError:
                    raise ValueError("起始帧/结束帧必须是整数：\n" + p)
                if not (0 <= s <= e):          # 允许从第 0 帧开始
                    raise ValueError("帧范围无效（起始帧不能为负数）：%d - %d" % (s, e))
                os.makedirs(o, exist_ok=True)   # 输出目录不存在则先创建
                projects.append((p, o, prefix, s, e))
            if not projects:
                raise ValueError("请至少添加一个工程")
        except Exception as ex:
            messagebox.showerror("输入有误", str(ex))
            return

        # 输入有效，先记住当前双卡设置
        self.save_config()

        # 2) 逐工程分析（切分 + 续传）
        self._log_action("开始分析 %d 个工程（GPU0 %.2f%% / GPU1 %.2f%%）"
                         % (len(projects), ratio, 100 - ratio))
        jobs_first = []    # GPU0 / blender_gpu0.exe：每个工程一行
        jobs_second = []   # GPU1 / blender_gpu1.exe：每个工程一行

        for i, (p, o, prefix, s_in, e_in) in enumerate(projects, 1):
            plan = plan_project(p, o, s_in, e_in, ratio)
            has = sorted(plan["rendered"])

            self._log("工程%d：%s" % (i, os.path.basename(p)))
            self._log("  输出前缀：%s（留空 = 仅数字序号）"
                      % (prefix if prefix else "（空）"))
            self._log("  输入帧范围 %d-%d，切分点 = %d；输出目录已扫描到 %d 帧%s"
                      % (s_in, e_in, plan["cut"], len(has),
                         ("，如 " + ", ".join(str(x) for x in has[:8]) + " ...")
                         if has else ""))

            f = plan["first"]    # GPU0
            if f:
                note = ("（段内已渲到 %d，从 %d 继续）" % (f["max_rendered"], f["s"])
                        if f["max_rendered"] is not None else "（本段无已渲染帧，按原比例起点）")
                self._log("  GPU0/blender_gpu0.exe：帧 %d - %d  %s" % (f["s"], f["e"], note))
                jobs_first.append((p, make_output_path(o, prefix), f["s"], f["e"]))
            else:
                self._log("  GPU0/blender_gpu0.exe：本段无任务（已全部渲染完或区间为空）")

            sc = plan["second"]  # GPU1
            if sc:
                note = ("（段内已渲到 %d，从 %d 继续）" % (sc["max_rendered"], sc["s"])
                        if sc["max_rendered"] is not None else "（本段无已渲染帧，按原比例起点）")
                self._log("  GPU1/blender_gpu1.exe：帧 %d - %d  %s" % (sc["s"], sc["e"], note))
                jobs_second.append((p, make_output_path(o, prefix), sc["s"], sc["e"]))
            else:
                self._log("  GPU1/blender_gpu1.exe：本段无任务（已全部渲染完或区间为空）")

        if not jobs_first and not jobs_second:
            self._log_action("所有工程两段都已完成，无需渲染")
            messagebox.showinfo("完成", "所有工程的帧都已渲染完毕，无需启动 Blender。")
            return

        # 3) 生成两个 ps 文件并自动启动
        ps_dir = os.path.join(tempfile.gettempdir(), "BlenderDualGPU_ps")
        os.makedirs(ps_dir, exist_ok=True)

        launched = []
        if jobs_first:
            ps1 = os.path.join(ps_dir, "GPU0_blender_gpu0.ps1")
            with open(ps1, "w", encoding="utf-8-sig", newline="") as fh:
                fh.write(build_ps(exe_first, "GPU0 / blender_gpu0.exe",
                                  jobs_first, self.var_fix_encoding.get()))
            launched.append(ps1)

        if jobs_second:
            ps2 = os.path.join(ps_dir, "GPU1_blender_gpu1.ps1")
            with open(ps2, "w", encoding="utf-8-sig", newline="") as fh:
                fh.write(build_ps(exe_second, "GPU1 / blender_gpu1.exe",
                                  jobs_second, self.var_fix_encoding.get()))
            launched.append(ps2)

        for ps in launched:
            # -ExecutionPolicy Bypass：不弹确认框，自动执行多行命令
            cmd = ["powershell.exe", "-NoProfile",
                   "-ExecutionPolicy", "Bypass", "-File", ps]
            try:
                subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_CONSOLE)
            except Exception as ex:
                messagebox.showerror("启动失败", "启动 PowerShell 失败：\n%s" % ex)
                return

        self._log_action("渲染启动完成")
        for ps in launched:
            self._log("已启动窗口：" + ps)
        self._log("渲染已启动：共 %d 个 PowerShell 窗口，进度请直接查看渲染窗口。"
                  % len(launched))


if __name__ == "__main__":
    app = MultiGPUApp()
    app.mainloop()
