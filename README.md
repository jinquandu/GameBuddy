# 陪玩大师姐 · dashijie-client（端侧工具箱）

直播游戏素材的**采集 → 识别 → 评估搬运**一体化命令行工具箱（Python 3.9+）。
当前针对 B 站《三角洲行动》直播校准，核心能力：

- **直播录制**：直连 B 站 CDN 拉流转封装，零重编码无损录制，支持无人值守守护；
- **桌面录制**：屏幕 + 系统声音（WASAPI loopback）一键录制，按日期场次
  目录/分P落盘，产物直接进识别链；
- **视频下载**：B 站分 P 视频 1080P 下载，断点续传；
- **识别链**：本地 ASR 转写（逐句时间戳）、说话人区分、OCR 逐帧行为识别
  （进入对局/入局装备/击倒/拾取/撤离）、三线切片（击倒片段/拾取会话/语音情绪）；
- **与服务端协作**：评估包上传、打标站打分、成片拉回（打标站
  http://8.133.251.179/eval/ ，账号找运营）。
  跨端接口的权威定义见 [docs/SERVER-INTERFACE.md](docs/SERVER-INTERFACE.md)
  ——新增/变更接口必须先更新该契约文档（含 changelog 与契约版本号）。

## 总体架构

识别重活（ASR/说话人/OCR/三线切片/L1 特征）在**端侧**完成；
评估打标与混剪在**服务端**。两步上传：评估包（每场 ~1-1.5G）先行，
被选中做片的场次再补传源片全量。

```
B站直播 ──record──┐                      端侧（本包）
B站视频 ──download┴─→ video/<场次>/ ─→ asr ─→ speaker ─→ detect(OCR事件池)
                                                  │
                              knockdowns / pickups / voice（三线切片）
                                                  │
                                            push（评估包）
                                                  ↓ rsync over ssh
                                      服务端：打标站评估/打分 ─→ remix 混剪
                                                  ↓
                                    pull 成片 → data/remixes/（手动上传发布）
```

## 目录结构

```
├── config.yaml               # 用户配置（从 config.example.yaml 复制后改）
├── requirements.txt          # 共用核心依赖
├── requirements-client.txt   # 端侧完整依赖（含 torch/funasr 等，3-5GB）
├── DEPLOY-WINDOWS.md         # Windows 部署与排查手册（必读）
├── docs/SERVER-INTERFACE.md  # 端侧↔服务端接口契约（接口变更必读必更新）
├── toolbox/                  # 全部代码
│   ├── recorder.py           # 0. 直播录制（CDN 直录）
│   ├── screenrec.py          # 0b. 桌面录制（屏幕+系统声音，日期场次/分P）
│   ├── download.py           # 1. B站分P下载
│   ├── asr.py                # 2. ASR 转写（funasr paraformer-zh）
│   ├── speaker.py            # 3. 说话人标注（CAM++ 声纹聚类）
│   ├── naming.py             # 共用命名约定（slug/mmss）
│   ├── chrono.py             # 跨分片基准钟 + 逐局对齐（abs_t 口径）
│   ├── events.py             # 素材收集 + 事件池获取（复用/现场检测）
│   ├── detector.py           # 4. 逐帧行为识别（RapidOCR → 事件流 JSONL）
│   ├── knockdown.py          # 5. 击倒片段链路
│   ├── pickup.py             # 6. 拾取会话链路（开箱会话小扫描）
│   ├── voice.py              # 7. 主播语音情绪评估
│   ├── pipeline.py           # 全链流水线（串起 1-7 + push）
│   ├── transfer.py           # push/pull（rsync over ssh 两步上传）
│   ├── remix.py / report.py  # 混剪/报告（服务端用，本包为兼容保留）
│   ├── ingest.py             # 素材登记（data/recordings/index.json）
│   ├── pet.py                # 桌面宠物客户端（跑链小卡：状态行/租户配置）
│   ├── pet_assets/           # 小卡圆角贴片（人设立绘素材留存备用）
│   └── cli.py                # 统一入口：python -m toolbox <子命令>
├── video/                    # 源片：video/<标题>/<标题> P<k> <分P名>.mp4
└── data/                     # 全部产物（结构见「数据契约」）
```

## 安装

### 前置依赖

| 依赖 | 安装 | 验证 |
| --- | --- | --- |
| Python 3.11+ | `winget install Python.Python.3.11` | `python --version` |
| ffmpeg/ffprobe | `winget install Gyan.FFmpeg` | `ffmpeg -version` |
| OpenSSH 客户端 | Win10+ 内置 | `ssh -V` |
| GNU rsync | MSYS2 `pacman -S rsync` 或 scoop | `rsync --version` ≥ 3.x |

### 步骤

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
# 基座（纯 CPU 链路，装机 ~2.0GB）：
pip install -r requirements-client.txt
# 有 N 卡的机器加挂 GPU 档（双链路，装机共 ~5.5GB；detect 11min/70min片、
# ASR 吃 CUDA；不装自动走 CPU 链路，同一份代码不用选包）：
pip install -r requirements-gpu.txt
copy config.example.yaml config.yaml     # server.tenant 按租户改
```

可选项：

- `bilibibili_cookies.txt`（Netscape 格式，浏览器导出，放包根目录，**不要发给别人**）：
  `download` 必需；`record` 缺它走匿名模式，个别房间高画质受限；
- `~/.ssh/config` 配置 `Host ecs` 别名（push/pull 用，详见 DEPLOY-WINDOWS.md）；
- PowerShell 执行 `$env:PYTHONUTF8=1` 避免中文日志 UnicodeEncodeError。

首次 ASR 会从 ModelScope 自动下载 ~1GB 模型（缓存
`~/.cache/modelscope`）。识别耗时（4 小时素材，i9-12900H 实测口径）：
GPU 档 detect ~35min + pickups ~25min；纯 CPU 档约 2 倍。

## 快速开始

### 直播录制（record）

```powershell
# 守护模式：监控房间，开播自动录、下播自动等，Ctrl+C 优雅收尾
python -m toolbox record "https://live.bilibili.com/14735356"
python -m toolbox record 14735356 --once          # 只录本场（下播即退出）
python -m toolbox record <URL> --probe            # 只探测开播状态与实际画质
```

录制原理与可靠性：

- **CDN 直录零重编码**：直接从 B 站 CDN 拉流（http_stream + flv + avc），
  ffmpeg `-c copy` 落 FLV 后 remux 成 mp4(+faststart)。画质 = 直播源画质，
  主播推 1080P 就是无损 1080P，无采集/编码开销，可 headless 无人值守；
- **自动分片**：默认 30 分钟一片（`--segment-min`），到点优雅停录轮转，P 号延续；
- **断流重连**：文件尺寸看门狗（默认 20s 无增长判断流），自动重拉直链续录新分片；
- **下播守护**：watch 模式下播后每 30s 轮询，再开播续录；`--once` 则单场退出；
- **画质档**：默认 qn=10000（原画 1080P），房间不支持时自动降最高可用档；
  匿名指纹 cookie 自动引导，有登录 cookie 时高画质不受限。

产物对齐场次契约：`video/<标题>/<标题> P<k> DD日HH点MM分.mp4`
（report 的跨分片基准钟认该时间戳），录完直接进识别链：

```powershell
python -m toolbox pipeline "video/<录制场次目录>"
```

### 下载 → 全链一条命令

```powershell
# 端到端：下载→转写→说话人→OCR→三线切片→推评估包→触发服务端打标站数据
python -m toolbox pipeline "https://www.bilibili.com/video/BVxxxx" --run-eval
python -m toolbox pipeline video/<场次目录> --skip download    # 素材已在本地
```

各阶段幂等（已有产物自动复用），可用 `--skip` 跳过：
`download asr speaker detect knockdowns pickups voice push`。

### 桌面宠物客户端（pet）

```powershell
python -m toolbox pet          # 置顶小窗：看进展 + 填地址一键跑全链
```

「陪玩大师姐」跑链小卡（2026-09-22 按人设图设计，v1.1 紧凑化：
**无立绘区、无头像**，纯工具卡；设计文档与视觉稿见 `docs/pet-design/`）：

- **状态行（闪现式）**：默认隐藏（标题栏下直接是最近场次条）；提示信息
  短闪 2.5s 自动收起，**出错时常驻**直到下次动作；收起态显示迷你进度
  （跑链 n/8 / REC 计时）；
- **阶段时间线**：8 阶段（下载→…→推评估包）实时推进、显示耗时，
  悬停看日志尾行；完成后读 `data/` 三线 JSON 汇总
  **击倒/拾取/语音** 片段数（空闲时也显示最近一场）；
- **桌面录制**：「● 录桌面」一键录屏幕 + 系统声音（soundcard
  loopback，缺省降级纯视频）；录制中按钮变为 **「⏸ 暂停 / ■ 结束」**，
  暂停后 **「▶ 继续 / ■ 结束」**（暂停收尾当前分P、继续开新分P，
  徽章只计有效录制时长）；**录制结束后「录桌面」上方出现
  「✂ 启动自动剪辑」**——对刚录场次一键启动全链；
  到 `segment_min` 分钟自动轮转分P（默认 15，`config.yaml` 的
  `screenrec` 块可调 fps/crf）；落盘规则：录像地址字段是**非场次**
  目录就录到该目录，否则 `video/`，结构为
  `<地址>/<YYYY-MM-DD 桌面录制>/<标题> P<k> DD日HH点MM分.mp4`
  （1级日期场次目录 / 2级分P，同日 P 号续接）；录完自动把场次目录
  回填到录像地址输入框；
  运行日志在 `data/logs/screenrec.log` 与 `data/logs/pet.log`；
- **配置录像地址**：输入 BV 号/URL/场次目录（浏览选目录），历史下拉可回选，
  勾选「推评估并打标」等价 `--run-eval`，点 ▶ 开始跑链 / ■ 停止；
- **配置租户 ID**：标题栏常驻「● 租户 xxx」徽章，配置卡可改，
  保存即回写 `config.yaml` 的 `server.tenant`（跑链中禁改）；
- 小卡无边框置顶圆角悬浮，按住标题栏拖动，`—` 收起成
  「标题栏+状态行」小条（宽度不变），`✕` 退出
  （链路运行中退出会先确认并终止整棵子进程）；
- **失败续跑**：某阶段失败后主按钮变「↻ 续跑」，自动 `--skip` 已完成
  阶段只重跑失败环节及之后；语音评估按分P隔离——个别分P无 host 语音
  （如秒级测试段）只跳过该分P，不再炸整链；
- 调试：`PET_SNAPSHOT=<目录> python -m toolbox pet` 启动后自动对各状态截屏；
  跑链全量输出落 `data/logs/pipeline.log`（排障先查这里）。

### 打标后收片

打标完成、场次被选中做片后：

```powershell
python -m toolbox push <场次目录> --mode video   # 补传源片全量（~4-5G）
python -m toolbox pull --template single_best    # 拉回服务端成片到 data/remixes/
```

## 命令一览

### 端侧（本包支持）

| 命令 | 说明 |
| --- | --- |
| `record <URL或房间号>` | 直播录制（CDN 直录零重编码，自动分片+断流重连+下播守护） |
| `screenrec [--out 目录]` | 桌面录制：屏幕+系统声音，日期场次目录/分P，Ctrl+C 优雅收尾 |
| `download <BV号或URL>` | B 站分 P 下载（1080P avc1，断点续传；`--parts 1,2 / 1-3`） |
| `asr <场次目录或视频>` | ASR 转写 → `data/asr/<场次>/transcript.json`（逐句时间戳） |
| `speaker <transcript.json>` | 说话人标注：句子区分主播/播报TTS → `speaker_labeled.json` |
| `detect <视频或id>` | 逐帧行为识别：进入对局/入局装备(干员+枪械)/击倒/拾取/撤离/跳舞 → 事件流 JSONL |
| `knockdowns <目标...>` | 击倒片段裁剪（连杀合并不切碎，前 6s 后 10s 留白） |
| `pickups <目标...>` | 拾取会话裁剪（按「一次开箱/开背包会话」口径） |
| `voice [目标...] [--html]` | 主播语音情绪评估：粗扫选段打分 → m4a 切片 + review.html |
| `pipeline <BV/URL/场次目录>` | 全链串起来（`--run-eval` 触发服务端评估） |
| `push <场次> [--mode video]` | 上传评估包（默认）/ 成片包（源片全量） |
| `pull [--template 名]` | 拉回服务端 remix 成片 + 发布包 |
| `ingest <视频...>` / `list` | 素材登记（ffprobe 元数据）/ 列出已登记视频 |

### 服务端（本包调用会提示「命令不可用」）

`import-session` / `sessions` / `eval` / `score` / `pscore` / `remix` /
`highlight` / `report` —— 评估打标、打分合并、混剪属服务端功能，
在打标站 http://8.133.251.179/eval/ 完成。

### record 参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--once` | 关 | 只录本场（下播即退出）；缺省 watch 守护模式 |
| `--qn` | 10000 | 画质档：10000=原画(1080P) 15000=2K 20000=4K |
| `--out` | `video/<房间标题>` | 输出目录 |
| `--segment-min` | 30 | 单分片分钟数，到点轮转，P 号延续 |
| `--stall-sec` | 20 | 无数据判定断流秒数，超时重拉直链续录 |
| `--probe` | — | 只探测房间状态与实际画质，不录制 |

## 配置（config.yaml）

从 `config.example.yaml` 复制，未列出的项走代码内默认值。关键块：

| 配置块 | 常用项 | 说明 |
| --- | --- | --- |
| `recorder` | `qn` / `segment_min` / `stall_sec` / `poll_sec` | 直播录制参数（同上表） |
| `screenrec` | `fps` / `crf` / `segment_min` / `mic` | 桌面录制：帧率/质量/分P分钟数/麦克风混录 |
| `download` | `cookies_file` / `qn` | cookie 文件路径与清晰度（80=1080P） |
| `server` | `ssh_alias` / `remote_root` / `tenant` / `site_url` | push/pull 的 ssh 别名、服务端数据根、租户 |
| `knockdown` | `pre_sec` / `post_sec` / `merge_sec` | 击倒片段留白与连杀合并窗口 |
| `pickup` | `merge_sec` / `look_back` / `look_fwd` / `max_sec` / `scan_workers` | 拾取会话小扫描参数 |
| `highlight` | `keywords_strong` / `keywords_weak` | 高光关键词（按主播口癖校准） |
| `detector` | `ocr_use_cuda` / `price_table` | OCR GPU 开关（实测反而慢 43%，默认关）、高价值物品估价表 |
| `detector` | `loadout_enabled` / `operator_class` / `weapon_type` | 入局装备识别开关（默认开）、干员→职业与枪械→类别映射表（可扩充） |

## 数据契约（与 Mac 端完全一致）

> 跨端文件（manifest/events/transcript/…）字段级的权威 schema 见
> [docs/SERVER-INTERFACE.md](docs/SERVER-INTERFACE.md) §5 数据契约。

- **场次目录**：`video/<标题>/<标题> P<k> <分P名>.mp4`（download 产）；
  record 产同构命名 `<标题> P<k> DD日HH点MM分.mp4`（report 跨分片基准钟
  认该时间戳），录完直接 pipeline 该目录；
- **data/asr/<场次>/**：`transcript.json`（逐句时间戳）+ `audio_16k.wav`
  （speaker/voice 链路消费）+ `speaker_labeled.json`（speaker 产）；
- **data/reports/<分片>_full/**：`events.jsonl` 事件流（detector 产，
  knockdown/pickups 按 `src` 复用）；
- **data/knockdowns|pickups|voice/<场次>/**：切片 mp4/m4a + 索引 JSON + L1 缓存；
- **data/remixes/**：pull 拉回的成片 + cover + publish.json 发布包；
- 同一场次请在**同一台机器**完成识别（不要 Mac/Windows 混跑）。

## 性能与排查

- **detect/pickups 提速（2026-09-21 两轮重构，70min 分片实测 i9-12900H+3070Ti）**：
  detect 55min→**11min**（5x），pickups 56min→**~13min**（4.3x）；
- **detect 的 GPU 批量引擎（toolbox/ocr_gpu.py）**：重写了 rapidocr 的逐帧推理
  路径——det 批量推理（8 帧一次）摊薄 H2D/同步开销，rec 复用原生
  TextRecognizer（ORT CUDA EP 大批量 rec 有宽度相关的跨设备同步放大，
  实测 96 框/批 2260ms vs 原生排序小批 397ms）；与 RapidOCR 输出逐帧
  8/8 一致（含 drop_score 过滤）。GPU worker 自动启用（`gpu_workers>=1`
  且 CUDA 可用），失败回退逐帧；分段按 GPU_WORKER_WEIGHT=4 加权。
  单 GPU worker 实测 0.148s/帧（约 6.7 帧/s）；
- **cls 方向分类全局关闭**（游戏 HUD 文字全水平）：RapidOCR 逐帧调用统一
  `use_cls=False`，CPU 路径单帧 -42%；
- **机器吞吐天花板**（混合架构笔记本 CPU）：CPU OCR 聚合上限
  ~4.5 帧/s；TensorRT FP16 实测无增益（单帧延迟由传输/同步主导而非算力；
  装 TRT 10.9 需 `pip install tensorrt-cu12==10.9.0.34` 且 PATH 加
  site-packages/tensorrt_libs）；批量引擎把 GPU 侧抬到 ~13 帧/s（2 worker）。
  要再快：桌面 CPU/服务器，或 `--fps 0.5`；
- **pickups 岛扫描不用批量引擎**：细长容器条（154x418）经 det min736
  归一化成 736x1994 巨幅输入，批量收益被内存带宽吃掉（实测反而慢 3min）；
  用 5 批量持久 worker（2 GPU 逐帧 + 3 CPU，队列按 GPU 权重装箱）+
  条带 2x 放大 + lobby 判别并行（顶部菜单带裁剪）+ 切片并行；
- 分段并行的边界语义：段前 12s 预热只积累状态不产事件；采样用绝对整秒
  格点，分段与串行一致（实测 87 条事件，击倒/入局/撤离全中，边缘拾取
  ±1、撤离偶有聚合切分差）；
- 常见问题（rsync 版本、UnicodeEncodeError、模型下载慢、ssh 别名等）
  见 **DEPLOY-WINDOWS.md**；
- 平台层差异（rsync/cv2 后端/控制台编码）在 DEPLOY-WINDOWS.md；
  **识别逻辑与数据契约跨平台零差异**。
