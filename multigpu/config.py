"""配置、日志与临时目录：全部位于程序目录（工作区），不写系统 C 盘。"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
import tempfile
from pathlib import Path

APP_NAME = "MultiGPU"
log = logging.getLogger(__name__)

DEFAULT_EXE_FIRST = "D:/ProgramFiles/Blender/Blender5.2/blender_5.2_gpu0.exe"
DEFAULT_EXE_SECOND = "D:/ProgramFiles/Blender/Blender5.2/blender_5.2_gpu1.exe"

DEFAULTS: dict = {
    "exe_first": DEFAULT_EXE_FIRST,
    "exe_second": DEFAULT_EXE_SECOND,
    "ratio_first": 60.0,
    "ratio_second": 40.0,
    "fix_encoding": True,
    "projects": [],
}


def program_root() -> Path:
    """程序所在目录；源码模式为仓库根目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def config_path(root: str | Path | None = None) -> Path:
    return Path(root or program_root()) / "config" / "config.json"


def legacy_config_path(root: str | Path | None = None) -> Path:
    return Path(root or program_root()) / "config" / "MultiGPU_config.json"


def log_dir(root: str | Path | None = None) -> Path:
    return Path(root or program_root()) / "logs"


def temp_dir(root: str | Path | None = None) -> Path:
    return Path(root or program_root()) / "temp"


def _coerce_ratio(value, fallback: float) -> float:
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        return fallback
    if not (0.0 <= ratio <= 100.0):
        return fallback
    return ratio


def _coerce_projects(value) -> list[dict]:
    projects: list[dict] = []
    if not isinstance(value, list):
        return projects
    for item in value:
        if not isinstance(item, dict):
            continue
        try:
            start = int(item.get("start", 1))
            end = int(item.get("end", 250))
        except (TypeError, ValueError):
            start, end = 1, 250
        projects.append({
            "proj": str(item.get("proj", "")),
            "out": str(item.get("out", "")),
            "prefix": str(item.get("prefix", "")),
            "start": start,
            "end": end,
        })
    return projects


def _normalize(raw: dict) -> dict:
    data = dict(DEFAULTS)
    for key in DEFAULTS:
        if key in raw:
            data[key] = raw[key]
    data["ratio_first"] = _coerce_ratio(data.get("ratio_first"), 60.0)
    data["ratio_second"] = _coerce_ratio(data.get("ratio_second"), 100.0 - data["ratio_first"])
    if abs(data["ratio_first"] + data["ratio_second"] - 100.0) > 1e-6:
        data["ratio_first"] = 60.0
        data["ratio_second"] = 40.0
    data["fix_encoding"] = bool(data.get("fix_encoding", True))
    data["exe_first"] = str(data.get("exe_first", DEFAULT_EXE_FIRST))
    data["exe_second"] = str(data.get("exe_second", DEFAULT_EXE_SECOND))
    data["projects"] = _coerce_projects(data.get("projects"))
    return data


def load_config(program_root: str | Path) -> tuple[dict, bool]:
    """读取 config\\config.json；不存在时尝试旧版 config\\MultiGPU_config.json。"""
    root = Path(program_root)
    path = config_path(root)
    if path.exists():
        try:
            with path.open(encoding="utf-8") as fh:
                return _normalize(json.load(fh)), False
        except (OSError, ValueError) as exc:
            log.warning("读取配置失败，改用默认值：%s", exc)
            return _normalize({}), False

    legacy = legacy_config_path(root)
    if legacy.exists():
        try:
            with legacy.open(encoding="utf-8") as fh:
                data = _normalize(json.load(fh))
            save_config(data, root)
            return data, True
        except (OSError, ValueError) as exc:
            log.warning("迁移旧配置失败：%s", exc)
    return _normalize({}), False


def save_config(data: dict, root: str | Path | None = None) -> Path:
    """原子写入配置；失败会抛 OSError，由调用方提示用户。"""
    path = config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return path


def setup_logging() -> logging.Logger:
    """配置程序日志（滚动文件，写入程序目录 logs\\）。"""
    logger = logging.getLogger("multigpu")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    logger.propagate = False
    try:
        directory = log_dir()
        directory.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            directory / "multigpu.log",
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s"
        ))
        logger.addHandler(handler)
    except OSError:
        # 程序目录不可写时不影响主流程，界面仍会显示日志。
        pass
    return logger
