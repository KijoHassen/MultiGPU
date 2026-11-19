# MultiGPU

用两张显卡同时跑 Blender 渲染的小工具：把同一个工程的帧区间按比例分给两张卡，
两边各开一个控制台窗口并行渲染；中断后再次启动时，只补渲染缺失的帧。

## 功能

- 两张卡各用一个 Blender 可执行文件，可分别指定输出比例（0–100，合计 100）。
- 按「命名前缀 + 帧号」识别已经渲好的帧，支持断点续传。
- 自动跳过损坏的占位图片；中间有缺帧时会逐段补齐，而不是从头重渲。
- 可一次排队多个工程，按工程分别计算两张卡的待渲区间。
- 渲染进程结束后在窗口里提示，支持“停止渲染”一键终止。
- 配置和工程队列自动保存，下次启动恢复。

## 环境要求

- Windows 10 / 11。
- Blender 4.x / 5.x（需要支持 `-b -o -s -e -a` 命令行参数）。
- 两张可用的显卡，以及分别绑定到两张卡的 Blender 配置。

## 准备两个 Blender 实例

Blender 的 GPU 选择保存在用户配置里，所以两张卡需要两个独立的配置目录。
最简单可靠的做法是复制一份 Blender：

1. 复制 `blender.exe` 为 `blender_gpu0.exe` 和 `blender_gpu1.exe`（放在同一目录即可，
   旁边需要有完整的 Blender 安装文件）。
2. 分别双击运行两个 exe，在 `编辑 → 偏好设置 → 系统` 里把 GPU 后端分别设为
   显卡 0 / 显卡 1（CUDA、OptiX、HIP 等按你的硬件选择），保存偏好设置。
3. 之后在 MultiGPU 里分别选择这两个 exe。

> 两个 exe 必须是不同的文件，否则两个进程会共用同一份 GPU 配置。

## 使用

1. 选择「显卡 0 / 显卡 1 可执行文件」。
2. 设置分配比例，例如 60 : 40。
3. 点「添加工程」，逐行填写（工程路径、输出文件夹都可以点行内「浏览…」选择）：
   - 工程路径：`.blend` 文件；
   - 输出文件夹：渲染结果所在目录（不存在会自动创建）；
   - 命名前缀：与 Blender 输出文件名一致，例如输出 `shot_####` 就填 `shot_`；
   - 起始帧 / 结束帧。
4. 点「启动渲染」。程序先扫描输出目录，在日志区列出两张卡各自还要渲哪些帧，
   然后各弹一个 PowerShell 窗口开始渲染。

### 命名前缀很重要

续传只认「前缀 + 数字」的图片文件：

- 前缀 `shot_` → 识别 `shot_0001.png`、`shot_0002.exr` ……
- 前缀留空 → 只识别 `0001.png` 这类纯数字文件名。

输出目录里其它工程、其它前缀的帧不会被误算成本工程的进度。

### 占位文件

Blender 开始渲染时会先创建一个空文件，渲染完成后再写入内容。程序会把
大小为 0 或内容不完整的图片当作“未完成”。「清除占位文件」只会把
**匹配当前前缀的图片**移入回收站，处理前会列出清单让你确认。

## 配置与日志

所有文件都放在程序目录（工作区）里，不写系统 C 盘：

- 配置：`config\config.json`（首次启动会尝试迁移旧的 `config/MultiGPU_config.json`）。
- 日志：`logs\multigpu.log`（自动轮转）。
- 临时脚本：`temp\`，渲染结束或退出时自动删除，启动时也会清掉上次残留。

首次启动时两张卡的 exe 默认填 `D:/ProgramFiles/Blender/Blender5.2/blender_5.2_gpu0.exe`
和 `..._gpu1.exe`；工程路径、输出文件夹保持空白，由你自行选择或填写。

## 从源码运行

```bat
python MultiGPU.py
```

## 打包

```bat
python -m pip install pyinstaller pillow
build.bat
```

生成物在 `dist\MultiGPU.exe`，约 6.9 MB（未含 UPX 时略大）。若 UPX 在 PATH 中会自动启用；排除项见 `build.bat`，如需 `hashlib`/`ssl` 等能力请删掉对应 `--exclude-module`。

## 项目结构

```
MultiGPU.py            启动入口
multigpu/
  config.py            配置读写、日志初始化
  formats.py           图片格式表与占位文件判定
  frames.py            帧扫描、分配、缺口计算
  psgen.py             PowerShell 脚本生成
  ui.py                Tkinter 界面
  util.py              路径/前缀/比例工具
  win32.py             回收站、单实例、DPI
icon/MultiGPU.ico      窗口与 exe 图标
config/MultiGPU_config.example.json
```

## 许可

MIT，见 [LICENSE](LICENSE)。
