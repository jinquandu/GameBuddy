# Windows 端部署与排查（dashijie-client）

端侧 = 识别全链：`download → asr → speaker → detect → knockdowns → pickups
→ voice → pipeline → push/pull`。评估打标、打分合并、混剪在服务端（打标站
http://8.153.157.239/eval/ ），本包不含这些命令——执行会提示「命令不可用」属正常。

## 0. 前置清单

| 依赖 | 安装 | 验证 |
| --- | --- | --- |
| Python 3.11+ | `winget install Python.Python.3.11` | `python --version` |
| ffmpeg/ffprobe | `winget install Gyan.FFmpeg` | `ffmpeg -version` |
| OpenSSH 客户端 | Win10+ 内置（Apps-可选功能确认） | `ssh -V` |
| GNU rsync | `scoop install rsync`（或 MSYS2 `pacman -S rsync`）；cwRsync 亦可 | `rsync --version` ≥ 3.x |

控制台中文输出：PowerShell 里执行 `$env:PYTHONUTF8=1`（或系统环境变量永久
设置）。否则中文日志可能触发 UnicodeEncodeError。

## 1. ssh 别名（与 Mac 端同一约定）

编辑 `C:\Users\<你>\.ssh\config`：

```
Host ecs
    HostName 8.153.157.239
    User root
    IdentityFile C:\Users\<你>\.ssh\id_ed25519
    # 可选：连接复用
    ControlMaster no
```

验证：`ssh ecs 'echo ok'`。反斜杠路径必须写全或用正斜杠。

> 2026-10-14 迁移：`ecs` = 生产（8.153.157.239）；旧生产 8.133.251.179 转测试
> 环境，别名 `ecs-test`（同样的 IdentityFile 配一段 `Host ecs-test` 即可）。

## 2. 安装

```powershell
Expand-Archive dashijie-client-<日期>.zip -DestinationPath C:\tools\dashijie
cd C:\tools\dashijie\dashijie-client-<日期>
python -m venv .venv
.venv\Scripts\Activate.ps1
# 核心版（录制/下载/OCR 击倒/拾取/push 评估，约 0.6GB）：
pip install -r requirements.txt
# 语音线（asr/speaker/voice，加起来约 2.0GB；不装则 pipeline 自动跳过
# 这三阶段，其余不受影响，后补装随时启用）：
pip install -r requirements-asr.txt
copy config.example.yaml config.yaml      # server.tenant 按租户改
```

两档安装对应的安装包体积：核心版 wheel 下载 ~150MB / 解压 ~0.6GB；
补语音线再下载 ~0.5GB（GPU 档 torch 换 cu126 版另加 ~3GB）。

B 站下载还需 `bilibili_cookies.txt`（Netscape 格式，从浏览器导出，放包根目录，
**不要发给别人**）。

### 2b. exe 版（核心版，免装 Python）

不想装 Python/venv 的机器用这个：开发机跑 `build_exe.bat`（需
`pip install pyinstaller`），产物 `dist\DashijiePet\`（约 550MB）——
**整个文件夹**拷给目标机，双击 `DashijiePet.exe` 即出桌宠小窗。

- 分发用 `dist\DashijiePet-core.zip`（bat 收尾自动刷新）：内嵌从
  `config.example.yaml` 生成的净化 `config.yaml`——**不含 tenant**（未设置
  走 default，首次用桌宠界面填租户）。不要把打包机自己的 config.yaml
  直接分发（2026-10-15 实测曾带着 `tenant: "007"` 出包，有数据串号风险）；

- 布局便携：video/、data/、config.yaml 都锚定 exe 所在目录；
- 内容 = 核心版（OCR 击倒/拾取/push 全可用），ASR 语音线未打包，
  跑链自动跳过 asr/speaker/voice（日志有提示）；要语音线走源码仓安装；
- exe 也是 CLI 直通：`DashijiePet.exe pipeline <地址> …`、
  `DashijiePet.exe record 123 --once` 等价 `python -m toolbox …`；
- 打包排除表与原理见 build_exe.bat 注释；重复打包直接再跑一遍 bat。

## 3. 首次链路（walkthrough）

```powershell
# 直播录制（0 号节点）：守护模式监控房间，开播自动录、下播自动等；
# Ctrl+C 优雅收尾。产物 video/<标题>/<标题> P<k> DD日HH点MM分.mp4
python -m toolbox record "https://live.bilibili.com/14735356"
python -m toolbox record 14735356 --probe          # 先探测画质/开播状态

# 端到端：下载→转写→说话人→OCR→三线切片→推评估包→触发服务端打标站数据
python -m toolbox pipeline "https://www.bilibili.com/video/BVxxxx" --run-eval
python -m toolbox pipeline "video/<录制场次目录>"  # 录好的场次直接进识别链

# 桌面宠物客户端：素朴小窗（录像地址+开始跑链），最小化驻留系统托盘
python -m toolbox pet

# 打标完成、选中做片后，补传源片 + 收回成片
python -m toolbox push <场次目录> --mode video
python -m toolbox pull --template single_best
```

首次 ASR 会从 ModelScope 下载 ~1GB 模型（到 `%USERPROFILE%\.cache\modelscope`；
核心版未装语音线则无此步）。
识别重活很吃 CPU，4 小时素材预计 1.5-2 小时（Apple Silicon 约 1 小时内）。

## 4. 常见排查

| 症状 | 处置 |
| --- | --- |
| `rsync: unknown option -s` 或报 `--secluded-args` | rsync 版本过旧/非 GNU；`rsync --version` 确认 ≥3.x（scoop/MSYS2 的都是 3.x） |
| `需要 GNU rsync` 报错 | PATH 里没有 rsync.exe；scoop 安装后重开终端 |
| UnicodeEncodeError / 中文乱码 | `$env:PYTHONUTF8=1`；或 `chcp 65001` |
| `funasr` 模型下载慢/失败 | ModelScope 网络问题重试；缓存目录可外接 |
| `Verify return code 21` (rsync 文件校验) | --modify-window=1 已默认开启；若仍出现多为传输中断，重跑即可续传 |
| 视频打不开/黑帧 | `ffprobe <文件>` 验证；cv2 在 Windows 走 MSMF，1080p30 无已知问题 |
| `ssh: Could not resolve hostname ecs` | `.ssh\config` 位置/格式问题；用 `ssh -F` 指定路径验证 |
| 事件 src 是 `C:\...` 反斜杠 | 正常。同一场次请在同一台机器完成识别，不要 Mac/Windows 混跑 |
| detect 太慢想用 GPU | 实测 RTX3070Ti 上 OCR 走 GPU 反而慢 43%（模型小、解码占大头）。提速优先 `--workers 4-6`（长分片约 1.6x；短分片 <10min 别用，进程开销摊不平）。GPU 方案见 requirements-client.txt 尾注 |
| rsync 报「源和目标都是远端」/「connection unexpectedly closed」 | transfer.py 已内置 Windows+MSYS 配方（/cygdrive 路径转换、配套 msys ssh、MSYS2_ARG_CONV_EXCL），前提是 rsync 装在标准 msys 布局 `<dir>\usr\bin\rsync.exe` 且 ssh.exe 同目录（本机=C:\Users\quinc\rsync，从清华/中科大 MSYS2 镜像抽包组装）。scoop 源已无 rsync 包 |

## 5. 与 Mac 端的差异边界

- 平台层（rsync/cv2 后端/控制台编码）差异如上；**识别逻辑与数据契约零差异**
  （同版代码 + 同 events/transcript JSON 结构）。
- 服务端复现排查：本 zip 同款代码在服务器 Docker 容器内有镜像副本
  （Linux 环境跑逻辑层对照），平台层差异才需要 Windows 本机定位。