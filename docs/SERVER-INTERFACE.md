# 端侧 ↔ 服务端 接口契约（SERVER-INTERFACE）

> **本文档是 dashijie-client（端侧）与 dashijie-eval（服务端）之间唯一的权威接口定义。**
> 双端代码分离部署，一切跨端交互（数据搬运、远端触发、文件格式）以本文档为准；
> 代码实现与本文档冲突时，要么改代码、要么先改本文档，不允许「文档之外的事实接口」。

| 项 | 值 |
| --- | --- |
| 契约版本（CONTRACT_VERSION） | 1.3.0 |
| 初版日期 | 2026-09-20 |
| 权威源 | 端侧仓库 `docs/SERVER-INTERFACE.md`（服务端仓库同名文件为副本，变更时双端同步） |
| 端侧实现 | `toolbox/transfer.py`（push/pull/import/sessions）+ `toolbox/uploader.py`（HTTPS 上传客户端） |
| 服务端实现 | 同一套 toolbox 代码，跑在容器 `dashijie-eval` 内；HTTPS 上传服务 = cut_server `deploy/eval/upload_api.py`（Flask blueprint，宿主 gunicorn 8767） |

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
        │ C2S-1 push 评估包（rsync 内部机 / HTTPS 客户机 C2S-6~9）
        │ C2S-3 manifest
        ├──────────────────────────→  SRV-1 import-session（自动触发）
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
| HTTPS 上传 API（1.3.0 起） | 客户机 push 评估包/成片包 | `https://95188.pw/api/production/v1`（nginx TLS → 宿主 127.0.0.1:8767 cut_server）；Bearer 租户 token 鉴权，分块断点续传，complete 服务端自动 import；接口见 C2S-6~C2S-9。**客户分发包一律走此通道**（不装 ssh 私钥/rsync） |
| rsync over ssh | 数据搬运（内部机器 push/pull） | GNU rsync（须支持 `--secluded-args`/`--protect-args`，远端路径含中文/括号）；参数 `-a --partial --modify-window=1`；Windows 平台配方见 DEPLOY-WINDOWS.md。**仅限内部运维机器** |
| ssh 控制通道 | mkdir / 触发容器命令（rsync 通道配套） | Windows 固定用原生 OpenSSH（`C:\Windows\System32\OpenSSH\ssh.exe`）；HTTPS 通道不使用 |
| docker exec | 容器内跑 toolbox 命令 | `docker exec -e TOOLBOX_DATA_DIR=<租户根>/data <container> python3 -m toolbox <cmd>`；HTTPS 通道的 complete 在服务端侧同样以该形态触发 import/eval |
| 账号服务 HTTPS（1.4.0，已冻结未上线） | 统一账号：登录/设备鉴权/生产申请 | `https://95188.pw/api/account/v1`（nginx → 旧机账号服务，暂驻；见 C2S-10~12） |
| 内部互信 HTTPS（1.4.0，已冻结未上线） | cut_server ↔ 账号服务内部接口 | TLS + 专用最小权限服务凭证 + 固定目标地址（禁止公网直连/loopback 明文假内网；容器经 bridge 访问宿主服务用实际网关地址）；见 INT-1~3 |

通道选择：`config.yaml server.upload_url` 非空 → HTTPS 通道（`upload_token` 必须同时配置，否则报错，**不静默回退 SSH**）；为空 → rsync 通道。

### 2.2 配置（config.yaml `server` 块）

| 键 | 默认值 | 说明 |
| --- | --- | --- |
| `upload_url` | 空 | HTTPS 上传 API 基址（客户机配 `https://95188.pw/api/production/v1`）。非空则 push 走 HTTPS 通道，`upload_token` 必须同时配置；空 = rsync 通道（内部机） |
| `upload_token` | 空 | 上传 Bearer token（运维装机发放，每租户一个；泄露可单 token 吊销）。HTTPS 通道下 **tenant 由服务端从 token 解出**，本配置 `tenant` 不参与、客户端改写无效 |
| `ssh_alias` | `ecs` | `~/.ssh/config` 里的主机别名（仅 rsync 通道使用） |
| `remote_root` | `/opt/dashijie-eval-data` | 服务端稳定数据根（部署包外，更新不删） |
| `tenant` | 未配置=自动分配 | 租户名（多租户方案A）。用户 config 未配置时，端侧首次 push/pull 按装机时间生成 `auto-YYYYMMDD-HHMMSS` 并写回 config.yaml（2026-10-19 起；此前缺省字面 `default`，多客户会串号进同一租户） |
| `container` | `dashijie-eval` | 服务端容器名 |
| `site_url` | `http://8.153.157.239/eval/` | 打标站地址（人用，非机器接口；域名失效后统一改公网 IP，IP 变更时同步改此项） |
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
│   ├── game_reports/<场次slug>/ game_report.md（逐场报告，SRV-2 产）
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
| 逐场报告目录 | `slug(场次名, ≤60)`，与击倒/拾取同式 | `data/game_reports/接直播跑刀…/` |
| 语音目录 | `slug(asr目录名, ≤40)` | `data/voice/接直播跑刀…_P1_18/` |

---

## 3. 接口清单

| 编号 | 接口 | 方向 | 通道 | 触发方式 | 状态 |
| --- | --- | --- | --- | --- | --- |
| C2S-1 | push 评估包 | 端→服务端 | rsync / HTTPS | `toolbox push <场次>`（pipeline 末段自动） | 已实施 |
| C2S-2 | push 成片包（源片全量） | 端→服务端 | rsync / HTTPS | `toolbox push <场次> --mode video` | 已实施 |
| C2S-3 | manifest 上报 | 端→服务端 | rsync / HTTPS | 随 C2S-1/C2S-2 自动 | 已实施 |
| C2S-4 | import-session 触发 | 端→服务端 | ssh+docker exec（rsync 通道）/ 服务端自动（HTTPS 通道） | push 后自动（`--no-import` 跳过） | 已实施 |
| C2S-5 | eval 触发 | 端→服务端 | ssh+docker exec（rsync 通道）/ complete 透传（HTTPS 通道） | `push --run-eval` / `pipeline --run-eval` | 已实施 |
| C2S-6 | 上传能力声明 | 端→服务端 | HTTPS | `GET /api/production/v1/capabilities` | 已实施（1.3.0） |
| C2S-7 | 创建上传会话 | 端→服务端 | HTTPS | `POST /api/production/v1/uploads` | 已实施（1.3.0） |
| C2S-8 | 分块上传 / 状态查询 | 端→服务端 | HTTPS | `PUT .../uploads/<id>/files/<idx>?offset=N`、`GET .../uploads/<id>` | 已实施（1.3.0） |
| C2S-9 | 完成校验+自动 import | 端→服务端 | HTTPS | `POST .../uploads/<id>/complete` | 已实施（1.3.0） |
| S2C-1 | pull 成片+发布包 | 服务端→端 | rsync | `toolbox pull [--template <名>]` | 已实施 |
| SRV-1 | import-session（收口） | 服务端容器内 | CLI | C2S-4 触发 / C2S-9 服务端自动 / 服务端手动 | 已实施 |
| SRV-2 | eval（评估链） | 服务端容器内 | CLI | C2S-5 触发 / 打标站 | 已实施 |
| SRV-3 | sessions list/prune | 服务端 | CLI | 运维手动 | 已实施 |
| SRV-4 | upload-token 运维（发放/吊销/列表） | 服务端 | CLI | `deploy/eval/upload_token.py` | 已实施（1.3.0） |
| C2S-10 | PC 设备鉴权登录 | 端→账号服务 | HTTPS | `POST /api/account/v1/login`（`client=pc`+设备签名；换机 `replace_device`） | **已冻结（1.4.0，未上线）** |
| C2S-11 | 账号能力声明 | 端→账号服务 | HTTPS | `GET /api/account/v1/health`（声明 `pc_device_auth_contract`） | **已冻结（1.4.0，未上线）** |
| C2S-12 | 生产申请 | 端→账号服务 | HTTPS | `POST /api/account/v1/production-jobs` | **已冻结（1.4.0，未上线）** |
| C2S-13 | 生产任务查询 | 端→服务端 | HTTPS | `GET /api/production/v1/jobs/<job_id>` | **已冻结（1.4.0，未上线）** |
| C2S-14 | 受控产物读取 | 端→服务端 | HTTPS | `GET /api/production/v1/jobs/<job_id>/artifacts/<file_id>` | **已冻结（1.4.0，未上线）** |
| INT-1 | 原请求身份核验 | cut_server→账号服务 | 内部互信 HTTPS | `POST /internal/v1/account/authorize` | **已冻结（1.4.0，未上线）** |
| INT-2 | 生产任务提交/查重 | 账号服务→cut_server | 内部互信 HTTPS | `POST /internal/v1/production/jobs` | **已冻结（1.4.0，未上线）** |
| INT-3 | 上传回执查询 | 账号服务→cut_server | 内部互信 HTTPS | `GET /internal/v1/production/uploads/<upload_id>/receipt` | **已冻结（1.4.0，未上线）** |

> **INT 编号族**为 1.4.0 新增（内部互信接口：服务进程间调用，非 PC/端侧
> 发起），与 C2S/S2C/SRV 族并列、同样只增不改。「已冻结（未上线）」表示
> 接口定义已定、尚未实施——实施上线时随发版把 CONTRACT_VERSION → 1.4.0。

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

push（两种模式）完成后，端侧经 ssh 在服务端执行（**rsync 通道**；HTTPS 通道
下这一步不存在——C2S-9 的 complete 在服务端侧以同形态自动执行）：

```bash
docker exec -e TOOLBOX_DATA_DIR=<租户根>/data <container> \
    python3 -m toolbox import-session <场次> --remote-root <租户根>
# 仅 C2S-5（--run-eval 且 mode=eval）追加：
docker exec -e TOOLBOX_DATA_DIR=<租户根>/data <container> \
    python3 -m toolbox eval <场次> --remote-root <租户根>
```

失败（非零退出）时端侧抛 RuntimeError，stderr 截断 800 字符回显。

### C2S-6 ~ C2S-9 HTTPS 上传 API（1.3.0 起，客户机通道）

基址 = `config server.upload_url`（生产 `https://95188.pw/api/production/v1`）。
鉴权：`Authorization: Bearer <upload_token>`（C2S-6 除外）。**tenant 由服务端
从 token 解出**，任何请求不得携带 tenant 字段（携带即 400）。

**C2S-6 能力声明**（无鉴权）：

```
GET /capabilities
→ 200 {"service":"dashijie-production-upload","upload_contract":"1.3.0",
       "chunk_max":67108864,"file_max":12884901888,"files_max":512,
       "total_max_bytes":42998169600,"ttl_hours":48}
```

端侧 push 前预检 `upload_contract` 主次版本，低于本机契约 → 报错提示服务端待升级。

**C2S-7 创建上传会话**：

```
POST /uploads
{"session":"<场次名>","mode":"eval|video","run_eval":false,
 "files":[{"path":"data/asr/<分片slug>/transcript.json","size":1234,
           "sha256":"<hex>"}, …]}
→ 201 {"upload_id":"…","tenant":"<token 所属租户>","expires_at":"…",
        "chunk_max":…}
```

`path` 限**租户根相对路径**且**只含数据/源片文件**（manifest 走 C2S-9 请求体，
不占文件清单——重跑时 pushed_at 变化不破坏清单签名，断点续传才成立）。
白名单（服务端强制，越界 400 `invalid_path`）：

| mode | 允许前缀 |
| --- | --- |
| eval | `data/asr/<段>/`、`data/reports/<段>_full/`、`data/knockdowns/<段>/`、`data/pickups/<段>/`、`data/voice/<段>/` 下的安全文件名 |
| video | `sessions/<session>/video/<文件名>` |

通用拒绝：绝对路径、`..` 段、`\`、控制字符、前导点段名、单段 >255 字节、
总长 >1024、`files_max`/`file_max`/`total_max_bytes` 超限（413 `too_large`）。
`session` 规则：非空、无 `/`、非 `.`/`..`、无控制字符、≤80 字符。
上传数据落 `<租户根>/.staging/<upload_id>/`（TTL 48h，新建时清扫过期）。

**C2S-8 分块上传 / 状态查询**：

```
PUT /uploads/<id>/files/<idx>?offset=N     # body=原始字节（一个分块）
   Content-Length ≤ chunk_max；offset 必须等于该文件当前已收字节数
→ 200 {"path":"…","received":<新累计>}
   offset 不符 → 409 offset_mismatch（端侧 GET 状态对齐后续传）
GET /uploads/<id>
→ 200 {"state":"open|files_committed|imported|failed_import",
       "session":"…","mode":"…",
       "files":[{"path":"…","size":…,"received":…},…]}
```

分块为**严格追加**：短块/中断块不落盘，服务端 offset 永远停在完整块边界。
idx = create 请求 `files[]` 的下标。

**C2S-9 完成校验 + 自动 import**：

```
POST /uploads/<id>/complete
{"manifest": {…§5.2 同构…}, "run_eval":false, "no_import":false}
→ 200 {"state":"imported","upload_id":"…","session":"…","tenant":"…",
       "remapped":N,"video_ready":bool,"log":"<import 输出尾部>"}
```

服务端动作（全过才入正式目录）：①逐文件 `received==size` 且实际 SHA-256
复核（不符 → 422 `checksum_mismatch`，该文件清零重传）；②manifest 校验
（dict；`contract_version`/`session`/`mode` 与会话一致）后由**服务端落盘**
`sessions/<session>/manifest.json`；③staging 文件移入租户正式目录（覆盖
同名，幂等）；④以 C2S-4 同形态 `docker exec` 触发 import-session
（`run_eval` 且 mode=eval 时追加 eval，对应 C2S-5；`no_import=true` 跳过，
state=files_committed）。重复 complete 幂等（import_session 幂等，manifest
覆盖同义内容）。回执持久化于 `<租户根>/.staging/receipts/<upload_id>.json`
（含 import 日志，供排障/对账）。import 失败不回滚文件：state=
failed_import + 日志，端侧重跑同命令即重试 import。

**错误约定**（全接口统一机器码，端侧按 code 分支、不匹配中文文案）：

| HTTP | code | 端侧行为 |
| --- | --- | --- |
| 400 | invalid_path / invalid_request / tenant_forbidden_field | 修正清单，重建会话 |
| 401 | invalid_token | 停止并提示 token 失效（联系运维换发） |
| 403 | token_revoked | 同上 |
| 404 | not_found | upload_id 失效（TTL/不存在）→ 重新 create |
| 409 | offset_mismatch / state_conflict | GET 状态对齐后续传 / 提示 |
| 413 | too_large | 分块或清单超限，报错 |
| 422 | checksum_mismatch | 该文件从 0 重传 |
| 5xx | — / upstream_unavailable | 退避重试（端侧默认 5 次，1/2/4/8/16s） |

**SRV-4 upload-token 运维**（服务端宿主 CLI）：

```
python3 deploy/eval/upload_token.py issue <tenant> --label <装机标识>   # token 只显示一次
python3 deploy/eval/upload_token.py revoke <token前8位|label>
python3 deploy/eval/upload_token.py list
```

库存 SHA-256 于 `/opt/dashijie-eval-data/upload_tokens.json`（原子写；含
tenant/label/created_at/revoked）。**token 即租户上传身份**，按台发放可按台
吊销；统一账号（REQ-20261019-002/003）上线后本层替换为账号核验，接口形状不变。

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

评估链：击倒/拾取打分（合并 L2 人工标注）+ 逐场报告 + review 页。
输出落服务端 data 树：`scores.json`、`review.html`，以及与击倒/拾取/语音
平级的逐场报告 `data/game_reports/<场次slug>/game_report.md`（仅统计本
场次事件，目录名登记进 `session.json` 的 `lines.report`），供打标站按
场次作第四条线展示。逐局表含「干员」「枪械」两列（1.2.0 起取局窗口内
`loadout` 事件：`meta.hero` 首条、`meta.weapons` 并集保序；缺失显示 `-`）。

### SRV-3 sessions（服务端运维命令）

```bash
python3 -m toolbox sessions [list|prune] [--keep N] [--tenant <t>]
```

- `list`：按租户列出会话（imported_at、源片✓/仅评估包、占用、场次名）；
- `prune`：每租户保留最近 N 个（默认 `server.keep_sessions`=3），连带删
  该会话在 data 树的 asr/reports/knockdowns/pickups/voice/game_reports
  产物（remixes 成片不删）。

---

## 4A. 统一账号与生产准入接口（1.4.0，REQ-20261019-003）

> **状态：已冻结（2026-10-19）**。依据 REQ-20261019-003 §拟议接口边界整理，
> 经端侧评审（三条意见已落实，见 §8 changelog 1.4.0 冻结行）后按用户指示
> 冻结接口定义：**接口形状、编号、错误语义自此不得再改**，后续任何变更
> 须走 §0 版本纪律（新接口顺延编号，破坏性变更升主版本）。
> **冻结 ≠ 已实施**：本节接口尚未上线，线上 `CONTRACT_VERSION` 仍为
> 1.3.0（代码常量随双端实施发版升至 1.4.0，届时本节各接口状态改
> 「已实施」）。账号侧（用户侧）实施时以本节为开发依据，如有异议走
> 版本变更流程而非直接改约。
> 版本合并说明：旧工作副本曾有一条未发布的 1.4.0（账号/投稿扩展），
> 本节为它与线上 1.3.0 的逐项合并起点，不是按数值覆盖。

### 总则（适用本节全部接口）

1. **身份自服务端解出**：user_id/tenant 一律由服务端从已验证会话或内部
   核验结果解出；请求体/查询参数携带 `user_id`/`tenant` 即 400
   `owner_forbidden_field`（沿用 1.3.0 `tenant_forbidden_field` 语义推广）。
2. **双凭证不混用**：内部服务凭证（调用方服务身份）与原 PC 会话证明
   （Bearer+签名要素）是两套凭证，不得放进同一个 Authorization；
   PC 的 Bearer token 绝不进日志、query 或知识库。
3. **签名（DSJ-PC-1，复用现有规范）**：HTTP method、原始 path、原始
   query、原始 body 的 SHA-256、Bearer 摘要、device_id、时间戳、nonce
   全部参与签名；验证前不得重新序列化 JSON 或改写 path。nonce 一次性；
   重放 → 401 `auth_replay`。内部核验响应丢失时返回可重试失败，PC 以
   **新 nonce 重签同一业务请求**（业务幂等键不变），不取消防重放。
4. **授权不缓存**：首版每次请求现核验当前状态；注销/换机后下一次请求
   即失效。依赖不可用 → 503 `dependency_unavailable`，不匿名放行、
   不回退 SSH/共享凭据。
5. **业务幂等**：以 `(user_id, request_id)` 为幂等键；分块续传/complete
   幂等沿用 C2S-8/C2S-9 既有规则。
6. **错误语义**（机器码稳定，端侧按 code 分支）：
   `401 invalid_token/auth_replay/device_revoked`；`403 no_entitlement/
   forbidden`；`404 not_found`（资源不存在或不属当前账号，同形不泄露）；
   `409 conflict`（换机未确认/幂等内容冲突）；`413 too_large`；
   `429 rate_limited`；`503 dependency_unavailable`。
   本清单为**公共子集**；各接口专属错误码以各节定义为准（如 C2S-10 的
   `409 device_bound`），端侧不得对未列举 code 做崩溃处理。

### DSJ-PC-1 规范细则（2026-10-19 随端侧实现补录；冻结条款的必要细化，非形状变更）

**签名规范化串**（UTF-8，`\n` 连接的 9 行；验证前不得重排序/重序列化）：

```
DSJ-PC-1
<METHOD 大写>
<raw path>            # 原样 path（以 / 开头，不含 query）
<raw query>           # 原样 query 串，无则空行
<sha256_hex(body)>    # body 空 = sha256(b"") 的 hex
<sha256_hex(bearer)>  # bearer 字符串 UTF-8 的 sha256 hex；无会话 = sha256(b"")
<device_id>
<ts>                  # Unix 秒（int）
<nonce>               # 16 字节 urlsafe ASCII，一次性
```

签名 = 安装密钥（Ed25519）对上述字节串的签名。请求头：

| 头 | 值 |
| --- | --- |
| `X-DSJ-Device` | device_id |
| `X-DSJ-Timestamp` | ts（与规范化串同值） |
| `X-DSJ-Nonce` | nonce |
| `X-DSJ-Signature` | 签名的 base64url（无填充） |

**device_id** = `dv-` + sha256(原始公钥 32 字节) hex 前 32 位；安装密钥
Ed25519，Windows 下私钥经 DPAPI 落盘（`data/.device/key.bin`），非 Windows
平台 0600 明文（仅开发环境）。

**C2S-10 login 请求体**（请求本身带上述签名头；服务端以 body 内
`public_key` 验签，并核对 `device_id == dv-摘要(public_key)`）：

```
{ "client":"pc", "phone":"…", "sms_code":"…", "device_id":"dv-…",
  "public_key":"<base64url 原始公钥>", "replace_device": false }
→ 200 { "user_id":…, "bearer":"…", "expires_at":"…" }
  已绑其他设备 → 409 device_bound（附现绑设备摘要）
```

短信下发沿用既有 `POST /api/account/v1/sms`（请求同样带签名头）。

**C2S-12 production-jobs 请求体**（签名头 + 账号 Bearer）：

```
{ "request_id":"<端侧幂等号，UUID>", "upload_id":"…",
  "params": { …可选制作参数；候选数量等额度字段由服务端权益决定，端侧不得自报 } }
→ 200 { "job_id":"…", "status":"…" }
```

**C2S-13 jobs 查询响应**（最小 schema，字段只增不改）：

```
{ "job_id":"…", "status":"queued|running|done|failed|partial",
  "artifacts":[ { "file_id":"…", "kind":"video|cover|…", "name":"…" } ] }
```

**C2S-14 产物读取**：`GET .../artifacts/<file_id>` 直接回流字节
（签名头 + 账号 Bearer；归属不符 404 同形）。

### C2S-10 PC 设备鉴权登录（账号服务）

- `POST /api/account/v1/login`，`client=pc`：短信验证码 + 设备签名
  （DSJ-PC-1）。验证通过创建/恢复统一账号并绑定本机；会话恢复沿用
  既有 refresh 语义。
- **一账号一台有效 PC**：已绑定其他设备时返回 409 `device_bound`（附
  现绑设备摘要），只有用户显式 `replace_device=true` 并重新完成短信
  验证才换绑；换绑即撤销旧 PC 会话，旧机下一次请求 401 `device_revoked`。
  **不得自动抢占绑定**。web 会话（`client=web`）不能充当 PC 身份。
- 设备身份 = 安装密钥（Ed25519 + DPAPI 保护）的公钥摘要；密钥丢失按
  新设备走换机流程。

### C2S-11 账号能力声明（账号服务）

- `GET /api/account/v1/health`：仅当设备鉴权 1.0 **实际实现**才返回
  `pc_device_auth_contract: "1.0"`；端侧见未知/缺失能力必须拒绝走
  账号通道（不沿用旧 manifest「告警后继续」）。

### C2S-12 生产申请（账号服务）

- `POST /api/account/v1/production-jobs`：入参 = 上传回执（upload_id/
  receipt 引用）、`request_id`、允许的制作参数（候选数量等由服务端
  权益决定，PC 不得自报额度）。
- 账号侧受理：核验有效设备与权益 → 经 INT-3 确认上传归属且 complete
  → 按 `(user_id, request_id)` 幂等预留业务 job_id → 经 INT-2 提交
  生产 → 返回 job_id 与状态。失败可幂等重试，不留「已提交」假状态。

### C2S-13/14 生产任务查询与受控产物读取（cut_server）

- `GET /api/production/v1/jobs/<job_id>`：PC 签名请求；服务端经 INT-1
  核验后比对 job 归属（改 job_id 访问他人 → 404 同形）。
- `GET .../artifacts/<file_id>`：file_id 为服务端分配；读取前核对原任务
  归属与当前会话有效；**不走** `/eval/<tenant>/media/` 旧 alias 规则
  （媒体旁路的封堵见 REQ-20261019-003 §S2，随本节冻结一并实施）。

### INT-1 原请求身份核验（cut_server → 账号服务）

- `POST /internal/v1/account/authorize`：入参 = 内部服务凭证（独立
  Authorization）+ 原始请求证明（method、raw path、raw query、body
  的 SHA-256、PC Bearer 摘要、device、时间戳、nonce）。
- 账号服务校验：服务身份 → PC 会话与设备当前绑定 → 签名要素与原请求
  一致 → nonce 未消费（原子记账防重放）。
- 出参仅必要 principal：`user_id`、有效 `device_id`、会话有效期、权益
  摘要、**服务端映射的 tenant**。web 会话充当 PC → 显式拒绝。
- 校验的是**原请求**而非核验接口自身路径；代理改写 path 必须还原已
  验证目标，不信任公网侧 `X-Original-*` 头。

### INT-2 生产任务提交（账号服务 → cut_server）

- `POST /internal/v1/production/jobs`：账号侧专用凭证；入参 = 业务
  job_id、tenant、上传回执归属证明、制作参数。cut_server 核对三元组
  （job_id/tenant/回执）一致后持久分配 `production_task_id`、完成既有
  映射 attach，再入执行队列（S4，已上线：`jobqueue`）执行。
- 网络重试不重复制作/计费（`(user_id, request_id)` 幂等）；生产端不能
  凭任意 user_id 新建归属。

### INT-3 上传回执查询（账号服务 → cut_server）

- `GET /internal/v1/production/uploads/<upload_id>/receipt`：回执存在且
  complete（state=imported）时返回归属与校验摘要；供 C2S-12 受理时
  确认「上传属于该用户且已完成」。
- **归属落盘细则（冻结）**：账号鉴权实施起，C2S-7 create 的 meta.json
  与回执（`<租户根>/.staging/receipts/<upload_id>.json`）须增记 owner
  principal（`user_id`，取自 INT-1 核验出参）——INT-3 的归属确认以此
  为准；租户 token 时期（1.3.0）仅有 `tenant` 字段，鉴权层置换时补齐，
  不改变既有回执字段。

### 与既有接口的关系

- C2S-6 capabilities 冻结时**兼容性新增**字段：`auth`（已实现的鉴权
  能力声明，如 `pc_account`），旧端忽略未知字段不受影响；
- C2S-7~9 的 Bearer 租户 token 鉴权层届时由 INT-1 替换（`_bearer_tenant()`
  内部置换，接口形状/错误码不变）；
- S3 两阶段上传校验（预检许可+读流复核，许可绑定请求/设备/资源/摘要，
  nonce 第二阶段不可重复消费）在 S1 实现联调时随本节一并冻结细则。
- **投稿/收片接口有意后置**（冻结确认）：旧未发布 1.4.0 中的投稿
  （douyin publish 准备）与收片（daily delivery）接口**不属于本次冻结
  范围**，待用户侧收片链路联调时以新编号（C2S-15 起顺延）另行起草——
  关联 REQ-20261019-003 §S4「自动回传后续联合验收」与
  REQ-20261019-004（月度权益，当前 hold）。本节不含它们不是遗漏。

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
| `loadout` | 本局装备（干员/枪械，端侧 HUD 提取） | `hero` 干员名、`weapons` 枪械名列表 |
| `down` | 击倒（击杀信息流播报） | `target` 目标名、`text` 信息流原文 |
| `loot` | 拾取（容器计数跳变） | `container` 容器名、`context` 同窗文本、`voice_hint` 语音交叉 |
| `extract` | 撤离结算 | `profit` 本局收获、`kills_official` 官方击杀数 |
| `dance` | 跳舞 HUD | — |

`loadout` 每局 ≥1 条（通常紧随 `match_start`）；局内多条时消费方按局窗口
聚合：`hero` 取首条、`weapons` 取并集保出现顺序去重。`meta` 除关键字段外，
端侧还附带源数据字段（`operator`=hero 同值、`operator_class` 职业、
`primary_weapon`/`primary_weapon_type`、`weapon_source` hud/tab、
`weapon_reads` 全部读数 [{name,type,reads}]、`operator_reads` 干员读数投票），
供端侧本地逐局表渲染与排障，服务端消费方可忽略。旧场次（≤1.1.x）meta 仅
含源数据字段、无 hero/weapons，消费方按缺失处理显示 `-`。

示例行：

```json
{"kind": "down", "t_start": 126.3, "t_end": 126.3, "detail": "击倒 岭南人爱少",
 "confidence": 0.91, "meta": {"target": "岭南人爱少", "text": "岭南人爱少 被…击中头部"},
 "src": "/…/video/猪猪夏…/猪猪夏… P1 18日12点52分.mp4"}
{"kind": "loadout", "t_start": 12.5, "t_end": 12.5,
 "detail": "干员威龙(突击) 主武器AKM(突击步枪)", "confidence": 0.9,
 "meta": {"hero": "威龙", "weapons": ["AKM"], "operator": "威龙",
   "operator_class": "突击", "primary_weapon": "AKM",
   "primary_weapon_type": "突击步枪", "weapon_source": "hud"},
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
| `lines` | object | `knockdowns`/`pickups`：目录名或 null；`voice`：目录名列表（`report` 为服务端状态键，见 §5.3，端侧 manifest 不写） |

### 5.3 session.json（`sessions/<场次>/`，服务端写）

manifest 的超集：`session`、`imported_at`、`mode`、`video_ready`（bool，
源片是否齐）、`parts`（每项额外含 `server_video` 服务端路径）、`lines`
（较 manifest 可多出 `report`：逐场报告目录名，SRV-2 eval 写入，
import-session 幂等重跑保留）。

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
                 "loots": 21, "ok": true, "profit": 8926620 } ],   // 逐局口径见 build_matches（端侧 toolbox/chrono.py；服务端副本在 report.py）
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

`scores.json`、`review.html`、`l2/`（人工标注）、`_frames/`、报告 md
（`data/game_reports/<场次slug>/game_report.md` 逐场报告与 report 命令的
池级汇总）—— 均服务端可重建或人工产物，不进入任何上送/下拉集合。

---

## 6. 错误处理与幂等性总表

| 接口 | 失败表现 | 幂等性 |
| --- | --- | --- |
| C2S-1/2 | rsync 非零 → RuntimeError（含退出码） | 增量，重推安全 |
| C2S-4/5 | 远端命令非零 → RuntimeError + stderr（≤800 字） | import 幂等；eval 重跑覆盖报告 |
| SRV-1 | 无 manifest → FileNotFoundError | 幂等（精确匹配重映射） |
| C2S-1 | 无任何可传产物 → RuntimeError | — |
| C2S-2 | 源片缺失 → RuntimeError | — |
| C2S-6~9 | 见 §4 错误约定表（机器码） | 会话按 offset 续传；complete 幂等；同清单重跑复用未过期 upload_id |
| SRV-4 | token 不存在/已吊销 → CLI 报错 | issue 重复 label 追加多条（各有独立 token） |

---

## 7. 版本兼容策略

- 服务端读 manifest 时**忽略未知字段**、对缺失的可选字段走默认值 —— 端侧
  先升级（多发字段）不破坏服务端；
- 契约主版本升级时，服务端应兼容上一主版本至少一个发版周期（按
  `manifest.contract_version` 检测并提示）；
- 枚举值（如 `kind`）只增不改：新事件类型由消费方按未知值忽略处理。

---

## 8. 变更记录（changelog）

> **2026-10-14 并轨说明**：本表曾因双端副本各自维护出现版本号漂移——同一
> site_url 勘误双端分别记 1.1.1 / 1.0.1；服务端「逐场报告线」曾记 1.1.0，
> 与端侧 1.1.0「loadout 初版」撞号；端侧 site_url 迁移曾暂记 1.1.2。今日
> 以《cut_kb/接口约定.md》台账建立为契机并轨为下表统一编号（行内括注保留
> 原始记录），1.2.0 起严格走「先台账登记、再改权威版、双端同步副本」流程。

| 日期 | 契约版本 | 接口 | 摘要 |
| --- | --- | --- | --- |
| 2026-10-19 | 1.3.0 | C2S-6~9 / SRV-4 / §2.1 | 兼容性新增：受控 HTTPS 上传通道（`https://95188.pw/api/production/v1`，Bearer 租户 token，tenant 服务端解出、路径白名单、SHA-256 复核、分块断点续传、complete 自动 import/eval）；config 新增 `server.upload_url`/`upload_token`（非空即 HTTPS 通道，客户分发包不再携带 ssh 私钥/rsync）；rsync 通道保留内部机用，行为不变。接口形状对齐 REQ-20261019-003 拟议的 `/api/production/v1`，统一账号上线后仅替换鉴权层（REQ-20261019-005） |
| 2026-10-19 | 1.4.0（冻结） | §4A / C2S-10~14 / INT-1~3 | **接口定义冻结（按用户指示，冻结≠实施，线上契约仍 1.3.0，CONTRACT_VERSION 随双端实施发版升至 1.4.0）**；冻结后接口形状/编号/错误语义不再可改，变更须走 §0 版本纪律。同时落实端侧三条评审意见：①总则 #6 明确错误码清单为公共子集、接口专属码（如 409 device_bound）以各节为准；②INT-3 增归属落盘细则（账号鉴权实施起 create 的 meta.json/receipts 增记 owner user_id）；③确认投稿/收片接口有意后置（C2S-15 起另行起草，关联 REQ-004），非本节遗漏 |
| 2026-10-19 | 1.4.0（细则补录） | §4A DSJ-PC-1 规范细则 | 随端侧实现（REQ-002 路径③，用户指示）补录签名规范化串（9 行 LF 连接：method/raw path/raw query/body 与 bearer 的 sha256 hex/device_id/ts/nonce）、签名头（X-DSJ-Device/Timestamp/Nonce/Signature）、device_id 算式（dv-+sha256(公钥)前32位）、C2S-10/12 请求体与 C2S-13 响应最小 schema——冻结条款的必要细化，形状/编号/错误语义未变 | [REQ-20261019-002](../cut_kb/需求/REQ-20261019-002-GameBuddy统一账号与设备鉴权集成.md) |
| 2026-10-19 | 1.4.0（草案） | §4A / C2S-10~14 / INT-1~3 / §2.1 | **草案，未冻结未实施，线上契约仍 1.3.0**：统一账号与生产准入接口全集（PC 设备鉴权登录/能力声明/生产申请、生产任务查询与受控产物读取、内部原请求核验/任务提交/回执查询），含 DSJ-PC-1 签名总则、双凭证不混用、授权不缓存、`(user_id,request_id)` 幂等、稳定错误语义、INT 编号族（新增）。为旧工作副本未发布 1.4.0 与线上 1.3.0 的逐项合并起点。冻结实施时 CONTRACT_VERSION → 1.4.0（REQ-20261019-003，服务端依据需求起草，待端侧收编权威版+三方评审） |
| 2026-10-14 | 1.2.0 | §5.1 | 兼容性新增：events.jsonl kind `loadout` 补齐 `meta.hero`（干员名）/`meta.weapons`（枪械名列表，局内多条按窗口聚合：hero 取首条、weapons 并集保序）。服务端逐场报告逐局表新增「干员」「枪械」两列，缺失显示 "-"。无新接口，push 通道与排除规则不变；旧端/旧场次不破坏（REQ-20261014-002） |
| 2026-10-14 | 1.1.3 | §2.2 | `site_url` 由 `http://8.133.251.179/eval/` 改为 `http://8.153.157.239/eval/`（服务器迁移：新生产=8.153.157.239，旧 8.133.251.179 转测试环境；人用地址、非机器接口，勘误级变更，机器通道不受影响。端侧曾暂记 1.1.2，并轨顺延） |
| 2026-09-21 | 1.1.2 | SRV-2 / SRV-3 / §5.3 | 兼容性新增：逐场报告线 `data/game_reports/<场次slug>/game_report.md`（与击倒/拾取/语音平级，仅统计本场次事件）；`session.json` `lines` 新增可选键 `report`（服务端写，manifest 不含，import 幂等保留，prune 连带删除）。端侧 manifest 无需改动（服务端曾记 1.1.0，端侧副本当时未同步，2026-10-14 并轨补录、编号顺延） |
| 2026-09-21 | 1.1.1 | §2.2 | `site_url` 默认值由 `https://95188.pw/eval/` 改为 `http://8.133.251.179/eval/`（原域名失效，统一改公网 IP；人用地址、非机器接口，勘误级变更，机器通道不受影响。服务端副本同一变更误记 1.0.1，并轨归一） |
| 2026-09-20 | 1.1.0 | §5.1 | events.jsonl 新增事件类型 `loadout`（初版口径：operator/职业/主武器等源数据字段，围绕 match_start 补扫产出；兼容变更，消费方按未知 kind 忽略即可。meta 关键字段已于 1.2.0 重定义为 hero/weapons，本行仅存档） |
| 2026-09-20 | 1.0.0 | 全部 | 初版：从 transfer.py 既有实现（2026-09-17 决策）固化为契约；manifest 新增 `contract_version` 字段（兼容变更，服务端忽略未知字段即可） |
