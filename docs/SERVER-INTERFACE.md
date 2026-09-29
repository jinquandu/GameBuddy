# 端侧 ↔ 服务端 接口契约（SERVER-INTERFACE）

> **本文档是 dashijie-client（端侧）与 dashijie-eval（服务端）之间唯一的权威接口定义。**
> 双端代码分离部署，一切跨端交互（数据搬运、远端触发、文件格式）以本文档为准；
> 代码实现与本文档冲突时，要么改代码、要么先改本文档，不允许「文档之外的事实接口」。

| 项 | 值 |
| --- | --- |
| 契约版本（CONTRACT_VERSION） | 1.1.1 |
| 初版日期 | 2026-09-20 |
| 权威源 | 端侧仓库 `docs/SERVER-INTERFACE.md`（服务端仓库同名文件为副本，变更时双端同步） |
| 端侧实现 | `toolbox/transfer.py`（push/pull/import/sessions） |
| 服务端实现 | 同一套 toolbox 代码，跑在容器 `dashijie-eval` 内 |

---

## 0. 维护规则（新增/变更接口必读）

1. **先文档后代码**：任何一侧新增或修改跨端接口，必须先在本文档落条目
   （含 changelog 与契约版本号），再写代码。评审接口类改动时，关联本文档的
   diff 是必查项。
2. **编号不复用**：接口编号（C2S-n / S2C-n / SRV-n）只增不改；废弃接口
   条目保留并标注 `[已废弃 v x.y]`，不删除。
3. **变更分级**：
   - **破坏性**（字段删除/改名/语义变化、路径或命名规则变化、命令行参数
     不兼容）→ 契约主版本 +1（2.0.0），双端必须同步发版后才能上线；
   - **兼容性**（新增可选字段、新增接口、新增枚举值）→ 次版本 +1（1.1.0），
   - 仅文档勘误 → 修订号 +1（1.0.1）。
4. **版本落地**：`toolbox/transfer.py` 的 `CONTRACT_VERSION` 常量随本文档
   同步修改，并写入每次 push 的 `manifest.json.contract_version`，服务端
   据此检测端侧契约版本（未知/过旧版本可在打标站提示）。
5. **新增数据文件**：凡跨端传输的新文件格式，必须在 §5 数据契约补 schema
   （字段表 + 示例），并声明「谁产、谁消费、是否上送」。
6. **双端同步**：本文档变更合入端侧仓库后，同步一份到服务端仓库
   `docs/SERVER-INTERFACE.md`（内容完全一致，仅此一处允许复制）。

**新增接口 checklist**（逐项打勾才算完成）：

- [ ] §3 接口清单表加行（编号顺延，标注状态：草案 / 已实施）
- [ ] §4 补接口详情（触发方式、参数、传输内容、远端路径、错误与幂等）
- [ ] 涉及新数据文件 → §5 补 schema
- [ ] `CONTRACT_VERSION` 与 §8 changelog 更新
- [ ] 端侧 README「命令一览」同步（若有新 CLI）
- [ ] 服务端仓库同步本文档副本

---

## 1. 职责边界与数据流

| 端 | 职责 |
| --- | --- |
| 端侧（本包） | 录制/下载、ASR、说话人、OCR 事件识别、三线切片（击倒/拾取/语音）、评估包/成片包上传、成片拉回 |
| 服务端（ECS 容器） | import-session 收口、评估打分（eval）、打标站人工标注、混剪（highlight/remix）、发布包产出、会话管理（sessions/prune） |

```
端侧                                    服务端（ecs 容器 dashijie-eval）
────                                    ──────────────────────────────
record/download → asr → speaker
→ detect → knockdowns/pickups/voice
        │ C2S-1 push 评估包（rsync）
        │ C2S-3 manifest
        ├──────────────────────────→  SRV-1 import-session（ssh 触发）
        │ C2S-5 eval 触发（可选）       SRV-2 eval → 打标站评估/打分
        │                               （人工打标 L2）
        │ C2S-2 push 成片包（选中场次）  → highlight/remix 混剪
        │ S2C-1 pull 成片+发布包   ←──────────────────────────
data/remixes/（手动上传发布）
```

两步上传模型：评估包（~1-1.5G/场）先行，打标站即刻可开工；被选中做片的
场次再补传源片全量（~4-5G），服务端保持回源片自由重切。

---

## 2. 通道与寻址

### 2.1 通道

| 通道 | 用途 | 说明 |
| --- | --- | --- |
| rsync over ssh | 数据搬运（push/pull） | GNU rsync（须支持 `--secluded-args`/`--protect-args`，远端路径含中文/括号）；参数 `-a --partial --modify-window=1`；Windows 平台配方见 DEPLOY-WINDOWS.md |
| ssh 控制通道 | mkdir / 触发容器命令 | Windows 固定用原生 OpenSSH（`C:\Windows\System32\OpenSSH\ssh.exe`） |
| docker exec | 容器内跑 toolbox 命令 | `docker exec -e TOOLBOX_DATA_DIR=<租户根>/data <container> python3 -m toolbox <cmd>` |

### 2.2 配置（config.yaml `server` 块）

| 键 | 默认值 | 说明 |
| --- | --- | --- |
| `ssh_alias` | `ecs` | `~/.ssh/config` 里的主机别名 |
| `remote_root` | `/opt/dashijie-eval-data` | 服务端稳定数据根（部署包外，更新不删） |
| `tenant` | `default` | 租户名（多租户方案A） |
| `container` | `dashijie-eval` | 服务端容器名 |
| `site_url` | `http://8.133.251.179/eval/` | 打标站地址（人用，非机器接口；域名失效后统一改公网 IP，IP 变更时同步改此项） |
| `keep_sessions` | `3` | prune 每租户保留最近 N 个会话 |

### 2.3 多租户与路径布局

租户名规则：`^[A-Za-z0-9_-]{1,32}$`（越界直接报错，不落盘）。
租户根 = `<remote_root>/tenants/<tenant>`：

```
<租户根>/
├── data/                     # 服务端 toolbox 工作树（TOOLBOX_DATA_DIR 挂载点，
│   │                         #   子目录结构与端侧 data/ 完全一致）
│   ├── asr/<场次slug>/          transcript.json / speaker_labeled.json
│   ├── reports/<分片slug>_full/ events.jsonl（+ 服务端自产报告）
│   ├── knockdowns/<场次slug>/   切片 mp4 + knockdowns.json
│   ├── pickups/<场次slug>/      切片 mp4 + pickups.json
│   ├── voice/<场次slug>/        m4a 切片 + voice_scores.json
│   └── remixes/<模板名>/        成片 + cover.jpg + publish.json
└── sessions/<场次>/
    ├── manifest.json         # 最近一次 push 的清单（端侧写，C2S-3）
    ├── session.json          # 服务端会话状态（SRV-1 写/更新，持久）
    └── video/P*.mp4          # 源片（仅 C2S-2 成片包阶段上传）
```

命名规则（跨端必须一致，服务端事件池/报告只认 `*_full`）：

| 对象 | 规则 | 示例 |
| --- | --- | --- |
| 场次目录 | `video/<标题>/`，分片 `<标题> P<k> <分P名>.mp4`；record 产 `<标题> P<k> DD日HH点MM分.mp4` | `video/猪猪夏…/猪猪夏… P1 18日12点52分.mp4` |
| slug | 非 `[\w\u4e00-\u9fff]` 连续段替换为 `_`，去首尾 | `一字欧_叫我皮鞭` |
| ASR 目录 | `slug(分片stem, ≤48)` | `data/asr/一字欧_叫我皮鞭_P1_20日10点18分/` |
| 事件报告目录 | `slug(分片stem, ≤40) + "_full"` | `data/reports/…_P1_20日10点18分_full/` |
| 击倒/拾取目录 | `slug(场次名, ≤60)` | `data/knockdowns/接直播跑刀…/` |
| 语音目录 | `slug(asr目录名, ≤40)` | `data/voice/接直播跑刀…_P1_18/` |

---

## 3. 接口清单

| 编号 | 接口 | 方向 | 通道 | 触发方式 | 状态 |
| --- | --- | --- | --- | --- | --- |
| C2S-1 | push 评估包 | 端→服务端 | rsync | `toolbox push <场次>`（pipeline 末段自动） | 已实施 |
| C2S-2 | push 成片包（源片全量） | 端→服务端 | rsync | `toolbox push <场次> --mode video` | 已实施 |
| C2S-3 | manifest 上报 | 端→服务端 | rsync | 随 C2S-1/C2S-2 自动 | 已实施 |
| C2S-4 | import-session 触发 | 端→服务端 | ssh+docker exec | push 后自动（`--no-import` 跳过） | 已实施 |
| C2S-5 | eval 触发 | 端→服务端 | ssh+docker exec | `push --run-eval` / `pipeline --run-eval` | 已实施 |
| S2C-1 | pull 成片+发布包 | 服务端→端 | rsync | `toolbox pull [--template <名>]` | 已实施 |
| SRV-1 | import-session（收口） | 服务端容器内 | CLI | C2S-4 触发 / 服务端手动 | 已实施 |
| SRV-2 | eval（评估链） | 服务端容器内 | CLI | C2S-5 触发 / 打标站 | 已实施 |
| SRV-3 | sessions list/prune | 服务端 | CLI | 运维手动 | 已实施 |

服务端另有 `highlight / remix / report / score / pscore` 等纯服务端命令
（端侧包不携带，调用提示「命令不可用」），不在跨端接口范围内，混剪产物
经 S2C-1 回流。

---

## 4. 接口详情

### C2S-1 push 评估包

```powershell
python -m toolbox push <场次目录|视频> [--tenant <t>] [-n]
```

- **上传内容与目标路径**（`<租户根>` 见 §2.3）：

| 本地（端侧 data/） | 远端 | 排除（不上送） |
| --- | --- | --- |
| `asr/<分片slug>/` | `<租户根>/data/asr/<同名>/` | `audio_16k.wav`、`*.log` |
| `reports/<分片slug>_full/` | `<租户根>/data/reports/<同名>/` | — |
| `knockdowns/<场次slug>/` | `<租户根>/data/knockdowns/<同名>/` | `l2/`、`_frames/`、`review.html`、`scores.json` |
| `pickups/<场次slug>/` | `<租户根>/data/pickups/<同名>/` | `l2/`、`_frames/`、`review.html`、`scores.json`、`_scancache.json` |
| `voice/<场次slug>/` | `<租户根>/data/voice/<同名>/` | `review.html` |

排除原则：`l2/` 是服务端人工标注产物（上送会覆盖）；`scores.json /
review.html / _frames/ / _scancache.json` 是服务端可重建物，不占带宽。

- **行为**：按 `discover_session` 聚拢产物；ASR/事件流缺失**不硬阻断**，
  如实写入 manifest 的 `missing[]`（打标站显示 incomplete）；完全无可传
  产物时报错退出。
- **幂等**：rsync `-a` 增量，重推只补差异。

### C2S-2 push 成片包

```powershell
python -m toolbox push <场次> --mode video
```

- 源片 `P*.mp4` 逐个上传至 `<租户根>/sessions/<场次>/video/<原名>`；
- 源片不存在直接报错；上传完成后同样走 C2S-3 + C2S-4。

### C2S-3 manifest 上报

- 文件：`<租户根>/sessions/<场次>/manifest.json`，schema 见 §5.2；
- `contract_version` 字段标识端侧契约版本（§0.4）。

### C2S-4 / C2S-5 远端触发

push（两种模式）完成后，端侧经 ssh 在服务端执行：

```bash
docker exec -e TOOLBOX_DATA_DIR=<租户根>/data <container> \
    python3 -m toolbox import-session <场次> --remote-root <租户根>
# 仅 C2S-5（--run-eval 且 mode=eval）追加：
docker exec -e TOOLBOX_DATA_DIR=<租户根>/data <container> \
    python3 -m toolbox eval <场次> --remote-root <租户根>
```

失败（非零退出）时端侧抛 RuntimeError，stderr 截断 800 字符回显。

### S2C-1 pull 成片+发布包

```powershell
python -m toolbox pull [--template <名>] [--tenant <t>] [-n]
```

- 拉回 `<租户根>/data/remixes/<模板>/` → 本地 `data/remixes/<模板>/`，
  排除中间产物 `seg/`、`concat.txt`、`edl.json`；
- 拉回后改写 `publish.json` 里指向服务端绝对路径的字段（如 `cover`）
  为本地同目录路径（`_localize_publish`）。

### SRV-1 import-session（服务端命令）

```bash
python3 -m toolbox import-session <场次> --remote-root <租户根>
```

- 前置：`sessions/<场次>/manifest.json` 必须存在，否则 FileNotFoundError；
- 行为：①把 `transcript.json.game`、`speaker_labeled.json.game`、
  `events.jsonl` 每行 `src` 中「端侧绝对路径」精确替换为
  `sessions/<场次>/video/<文件名>`（已是服务端路径的行原样保留）；
  ②写/更新 `session.json`（§5.3）；
- **幂等**：可重跑，重映射按精确匹配不会二次改写。

### SRV-2 eval（服务端命令）

```bash
python3 -m toolbox eval <场次> [--no-html] --remote-root <租户根>
```

评估链：击倒/拾取打分（合并 L2 人工标注）+ 逐局报告 + review 页。
输出落服务端 data 树（`scores.json`、`review.html` 等），供打标站使用。

### SRV-3 sessions（服务端运维命令）

```bash
python3 -m toolbox sessions [list|prune] [--keep N] [--tenant <t>]
```

- `list`：按租户列出会话（imported_at、源片✓/仅评估包、占用、场次名）；
- `prune`：每租户保留最近 N 个（默认 `server.keep_sessions`=3），连带删
  该会话在 data 树的 asr/reports/knockdowns/pickups/voice 产物
  （remixes 成片不删）。

---

## 5. 数据契约（跨端文件 schema）

通用约定：JSON 一律 UTF-8、`ensure_ascii=false`、`indent=1`（transcript
为 2）；时间单位**秒**（float，事件时间保留 1 位小数）；`game`/`src` 等
路径字段在端侧是本地绝对路径，import 后为服务端绝对路径。

### 5.1 events.jsonl（事件流，`data/reports/<分片slug>_full/`）

JSONL，每行一个事件，write-once（文件存在即为完整结论）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `kind` | str | 枚举见下表 |
| `t_start` / `t_end` | float | 事件起止（秒，相对所在分片） |
| `detail` | str | 人读描述（如 `击倒 岭南人爱少`、撤离原文） |
| `confidence` | float | OCR 置信度 |
| `meta` | object | kind 相关附加信息，见下表 |
| `price` / `price_source` | int / str | 仅 `loot`：估价与来源（`table` 价目表 / `quality` 品质兜底）；无估价时字段缺省 |
| `src` | str | 来源分片绝对路径（import 后为服务端路径） |

| kind | 含义 | meta 关键字段 |
| --- | --- | --- |
| `match_start` | 进入对局（部署字幕） | — |
| `loadout` | 入局装备（干员/枪械，围绕 match_start 补扫） | `operator` 干员名、`operator_class` 职业（突击/支援/工程/侦察）、`primary_weapon`/`primary_weapon_type` 主武器与类别、`weapons` 全部稳定读数 [{name,type,reads}]、`weapon_source` hud/tab、`operator_reads` 干员读数投票 |
| `down` | 击倒（击杀信息流播报） | `target` 目标名、`text` 信息流原文 |
| `loot` | 拾取（容器计数跳变） | `container` 容器名、`context` 同窗文本、`voice_hint` 语音交叉 |
| `extract` | 撤离结算 | `profit` 本局收获、`kills_official` 官方击杀数 |
| `dance` | 跳舞 HUD | — |

示例行：

```json
{"kind": "down", "t_start": 126.3, "t_end": 126.3, "detail": "击倒 岭南人爱少",
 "confidence": 0.91, "meta": {"target": "岭南人爱少", "text": "岭南人爱少 被…击中头部"},
 "src": "/…/video/猪猪夏…/猪猪夏… P1 18日12点52分.mp4"}
```

### 5.2 manifest.json（`sessions/<场次>/`，端侧写）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `contract_version` | str | 端侧契约版本（本文档 §0.4） |
| `session` | str | 场次名（= sessions/ 目录名） |
| `mode` | str | `eval` / `video` |
| `pushed_at` | str | ISO8601（秒） |
| `parts` | list | 每分片：`client_video`（端侧绝对路径）、`filename`、`asr_dir`、`events_rpt`（规范 `_full` 名）、`voice_dir?` |
| `missing` | list[str] | 缺失产物说明（ASR/事件流），打标站如实显示 |
| `lines` | object | `knockdowns`/`pickups`：目录名或 null；`voice`：目录名列表 |

### 5.3 session.json（`sessions/<场次>/`，服务端写）

manifest 的超集：`session`、`imported_at`、`mode`、`video_ready`（bool，
源片是否齐）、`parts`（每项额外含 `server_video` 服务端路径）、`lines`。

### 5.4 transcript.json / speaker_labeled.json（`data/asr/<分片slug>/`）

```jsonc
// transcript.json（asr 产；speaker/voice/highlight 消费）
{ "game": "<分片绝对路径>", "model": "paraformer-zh", "duration_s": 7189.2,
  "sentences": [ { "id": 0, "start": 1.2, "end": 4.8, "text": "……" } ] }

// speaker_labeled.json（speaker 产；同句新增 speaker 字段）
{ "source": "<transcript路径>", "threshold": 0.65,
  "summary": { "clusters": 3, "host_lb": 1, "host_sec": 5320.4, "other_sec": 611.9 },
  "sentences": [ { …, "speaker": "host" } ] }   // speaker ∈ {host, other}
```

`audio_16k.wav`（16k 单声道）不上送（§C2S-1 排除），服务端如需音频另行处理。

### 5.5 knockdowns.json（`data/knockdowns/<场次slug>/`）

```jsonc
{ "session": "<场次名>",
  "clips": [ {
      "file": "01_P1_02m06s_击倒_岭南人爱少.mp4",   // NN_P<k>_MMmSSs_标签.mp4
      "part": "1", "t_start": 126.3, "t_end": 128.1,
      "cut": [120.3, 138.1],                        // 实裁 [t0, t1]（含留白）
      "targets": ["岭南人爱少"],
      "events": [ { "t": 126.3, "detail": "击倒 …", "headshot": true, "text": "…" } ]
  } ] }
```

### 5.6 pickups.json（`data/pickups/<场次slug>/`）

```jsonc
{ "session": "<场次名>",
  "matches": [ { "no": 1, "enter": 46032, "dur": 1502.0, "downs": 3,
                 "loots": 21, "ok": true, "profit": 8926620 } ],   // 逐局口径见 toolbox/chrono.py（2026-09-22 自 report.py 下沉，行为不变）
  "clips": [ {
      "file": "04_P1_07m15s_连拾x3.mp4", "part": "1",
      "t_start": 435.2, "t_end": 441.0,            // 首末拾取时刻（分片内）
      "cut": [431.9, 447.5], "ui": [433.0, 439.8], // 实裁区间 / 容器界面开合
      "src": "<分片文件名>", "abs_t": 46467.2,      // 场次绝对秒（基准钟）
      "match_no": 1, "fallback": false,
      "jumps": 3, "net_gain": 4, "organizing": false, "ui_sec": 6.8,
      "containers": ["背包"], "near_down": null,
      "events": [ { "t": 435.2, "detail": "背包 11->12", "container": "背包",
                    "prev": 11, "cur": 12, "jump": 1, "context": [], "voice_hint": null,
                    "price": null, "price_source": null } ]
  } ] }
```

`_scancache.json` 是端侧小扫描缓存（断点续跑用），**不上送**。

### 5.7 voice_scores.json（`data/voice/<场次slug>/`）

```jsonc
{ "session": "<场次名>", "audio": "<audio_16k.wav 路径>", "out_dir": "<本目录>",
  "f0_base": 142.0,                                   // 场次基频基线 Hz
  "clips": [ { "t_start": 153.2, "t_end": 175.9, "cut": [153.2, 175.9],
               "file": "01_02m33s_….m4a",             // NN_MMmSSs_首句摘要.m4a
               "signals": { … }, "evidence": [ { "text": "…" } ], "words": [ … ],
               "dims": { … }, "score": 78.5, "grade": "A" } ] }
```

`signals/dims` 为端侧评估信号明细（分维度），`grade` A/B/C/…，`review.html`
不上送（服务端可重建）。

### 5.8 publish.json（服务端 remixes 产，S2C-1 拉回）

```jsonc
{ "video": "<成片绝对路径>",            // 拉回后由端侧改写为本地路径
  "title": "…", "tags": ["三角洲行动", …],
  "cover": "<cover.jpg 绝对路径>",      // 同上
  "duration_s": 61.4, "loudness_lufs": -14.0, "measured_before_lufs": -18.2,
  "template": "fast_cut", "vars": { … } }
```

### 5.9 服务端自产、不回流端侧

`scores.json`、`review.html`、`l2/`（人工标注）、`_frames/`、报告 md —— 均
服务端可重建或人工产物，不进入任何上送/下拉集合。

---

## 6. 错误处理与幂等性总表

| 接口 | 失败表现 | 幂等性 |
| --- | --- | --- |
| C2S-1/2 | rsync 非零 → RuntimeError（含退出码） | 增量，重推安全 |
| C2S-4/5 | 远端命令非零 → RuntimeError + stderr（≤800 字） | import 幂等；eval 重跑覆盖报告 |
| SRV-1 | 无 manifest → FileNotFoundError | 幂等（精确匹配重映射） |
| C2S-1 | 无任何可传产物 → RuntimeError | — |
| C2S-2 | 源片缺失 → RuntimeError | — |

---

## 7. 版本兼容策略

- 服务端读 manifest 时**忽略未知字段**、对缺失的可选字段走默认值 —— 端侧
  先升级（多发字段）不破坏服务端；
- 契约主版本升级时，服务端应兼容上一主版本至少一个发版周期（按
  `manifest.contract_version` 检测并提示）；
- 枚举值（如 `kind`）只增不改：新事件类型由消费方按未知值忽略处理。

---

## 8. 变更记录（changelog）

| 日期 | 契约版本 | 接口 | 摘要 |
| --- | --- | --- | --- |
| 2026-09-21 | 1.1.1 | §2.2 | `site_url` 默认值由 `https://95188.pw/eval/` 改为 `http://8.133.251.179/eval/`（原域名失效，统一改公网 IP；人用地址、非机器接口，勘误级变更，机器通道不受影响） |
| 2026-09-20 | 1.1.0 | §5.1 | events.jsonl 新增事件类型 `loadout`（入局装备：干员/职业/主武器/枪械类别，围绕 match_start 补扫产出；兼容变更，消费方按未知 kind 忽略即可） |
| 2026-09-20 | 1.0.0 | 全部 | 初版：从 transfer.py 既有实现（2026-09-17 决策）固化为契约；manifest 新增 `contract_version` 字段（兼容变更，服务端忽略未知字段即可） |
