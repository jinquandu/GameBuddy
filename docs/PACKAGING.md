# 打包与分发要点(PACKAGING)

> 面向执行打包的人。配方=`build_exe.bat`(纯 ASCII 注释,cmd 按 GBK 解析,
> bat 里别写中文)。产物:`dist\DashijiePet\`(目录版,~1.4GB)与
> `dist\DashijiePet-core.zip`(分发版,~389MB)。本文档 2026-10-21 建立
> (REQ-20261021-003/004 出包实践沉淀),历史细节另见
> memory/exe-frozen-packaging-gotchas 与 KB REQ-012/013/014 各包血统。

## 1. 构建前检查清单(逐项过,缺一常翻车)

- [ ] **杀占句柄进程**:运行中的 DashijiePet、`watch_pipeline_log.py`
      等日志查看器、一切打开 `dist\` 下文件的程序。COLLECT 会整目录清空
      重建,一个句柄就让它中途炸(exe 已删、_internal 半成品)。
- [ ] **依赖在位**:`py -3 -c "import qrcode, PIL, PyInstaller"` +
      `qrcode.make('x')` 渲染一次(绑定对话框用;qrcode>=8.0 已入
      requirements.txt,Pillow 既有)。构建机缺依赖=包里缺模块,客户端
      才发现。
- [ ] **git 干净**:确认要打的 commit 就是当前树(bat 不做版本锚定,
      包名不带 commit——用 KB 登记对齐)。
- [ ] ffmpeg/ffprobe 在 `C:\Users\quinc\ffmpeg\bin\`(bat `--add-binary`
      硬编码路径;换机器要改 bat)。

## 2. 构建

```
cmd //c build_exe.bat        # Git Bash 下;> /tmp/build_full.log 2>&1 收全量日志
```

- 产物为 `dist\GameBuddy\`(目录版)与 `dist\GameBuddy-core.zip`(分发版);
  **r7 起改名**并改用 spec + `tools/make_release.py` 打包(见下)。
- **build_exe.bat 必须纯 ASCII**:cmd 按 GBK 解析,rem/echo 里的中文会被撕成
  命令(r7 首轮实测:`'m' 不是内部或外部命令` → 打包中断)。
- **ZIP 由 `tools/make_release.py` 生成**:PowerShell `Compress-Archive` 写出的
  条目分隔符是反斜杠(r1~r6 全部如此),违反 ZIP 规范且跨平台工具会解成怪目录。
  新脚本逐项写 `/`、校验无绝对路径/`..`/重复项,并写 `dist/release-manifest.txt`。
- 顶层目录名取源目录名(`GameBuddy/`),解压不散落。
- 用户说明 `快速开始.md` 由脚本自动放进包内。

- 耗时 ~19s(增量);**判成败只看产物**:exe 与 zip 的 mtime 都应是
  刚刚,`bat` 末尾 echo 无条件打印、管道会吃掉退出码,别信回显。
- bat 自动做:备份 `dist\DashijiePet\data` → 打包 → 拷净化 config
  (从 config.example.yaml 派生,无租户键)→ 压 zip → 还原 data。
- **构建后看到 `data\` ≠ 备份失效**(2026-10-22 复盘更正:此前一轮
  误判为「静默失效」):它要么是 bat 设计内的备份还原(构建前有 data →
  zip 后回移),要么是构建后烟测启动实例重新创建的。**真正的铁律只有
  一条:zip 泄漏扫描 0 命中**(zip 永远在还原步骤前打包,历史三轮全部
  干净)。发布目录形态前把 `data\` 移出 dist 即可。

## 3. 验收(四道关卡,按序)

1. **双产物存在+新鲜 mtime**(见上)。
2. **zip 泄漏扫描**:1086 项左右;`zipfile.namelist()` 全表扫关键词
   `device / session.json / history.json / uploads.json / .dpapi` 必须
   0 命中;顺带确认顶层是 `DashijiePet/`(压文件夹本体,平铺=错)。
3. **包内代码核验**(新功能确认进包):嵌 PYZ 不落盘,读法——
   ```python
   # ① PKG 绝对起点:
   #    cookie 位=exe 尾部 b'MEI\x0e...\x16' 反查;pkg_start=cookie位+80-pkgLen
   # ② PYZ 偏移:py -m PyInstaller.utils.cliutils.archive_viewer -l exe
   #    输出表里 'PYZ.pyz' 行的 position(相对 PKG)
   from PyInstaller.archive.readers import ZlibArchiveReader
   z = ZlibArchiveReader('exe路径', start_offset=pkg_start + pyz_off)
   z.toc  # 980 模块上下,逐一验证
   ```
   必验清单(toolbox 全家 35+ + 依赖):`toolbox.{pet,binding_ui,account,
   account_ui,folder_watch,transfer,uploader,pipeline,cli,config,
   device_identity}`、`qrcode*`(19 子模块)、`PIL.ImageTk/ImageDraw`、
   `pystray._win32`。`launch_pet` 是 CArchive 顶层脚本不在 PYZ=正常。
   ffmpeg/ffprobe 直接看 `_internal\`(9d4bed7 起捆绑,客户机 WinError2
   修复)。
4. **冻结烟测**:exe 有头启动 → 截图验 UI(`PET_SNAPSHOT=<目录>` 自动
   三页截图,免手工点击)→ Stop-Process 清理。硬指标:
   - `exe pipeline <不存在路径>`(env `PYTHONIOENCODING=gbk`)→ stderr
     合法 UTF-8 + 预期错误 + rc=1;
   - **DPI 感知**(REQ-006):对运行中进程调 `GetProcessDpiAwareness`,
     必须为 1(system aware),不能是 0(unaware);
   - **版本资源**(REQ-009):`(Get-Item exe).VersionInfo` 的 ProductName/
     FileVersion 非空;
   - `PET_SNAPSHOT` 截图确认无 `None` 之类未翻译占位、无横向裁切。

## 4. 上传分发位(免登录直链,REQ-20261015-001)

```
scp dist\DashijiePet-core.zip ecs:/opt/dashijie-eval-data/clients/<名>-uploading.zip
ssh ecs "cd .../clients && sha256sum <名>-uploading.zip && \
         mv <名>-uploading.zip <名>.zip"
```

- **临时名+mv 原子改**(半包不可见);双侧 sha256 必须一致;直链
  `http://8.153.157.239/eval/clients/<名>.zip` curl -I 验 200。
- 命名:候选包 `dashijie-pet-<日期>[-rN].zip`(rN=同日重打);正式对外
  固定 `GameBuddy-v0.1.zip`(**用户 2026-10-23 指示:链接不随版本变**,
  更新内容时用 `-uploading` 临时名 + 原子 `mv` 覆盖同名文件,并核
  sha256/Content-Length 已变)。客户核对版本看 EXE 属性或界面左下角。
  **先验证后发布**;旧包按用户要求可直接删除,回滚依赖候选包名。

## 5. 当前有效版本

| 包 | SHA256(前16) | 内容 |
| --- | --- | --- |
| **`GameBuddy-v0.1.zip`(正式位,内容=r8b)** | `d9b2710c` | **当前对外下载位(2026-10-26 替换)**。r8b=r8+对外版本恒定 V0.1(界面/EXE 属性只显示 V0.1,构建号 20261026-r8b 仅在诊断信息里);含规则服务端下发+客户端自动更新+死会话留痕等;1188 项/389,206,941 字节。候选名 `dashijie-pet-20261026-r8b.zip` 同字节 |
| **`GameBuddy-v0.1.zip`(对外正式名;内容=r7f/0.2.0)** | `3f5b465c02a8dc13` | **当前对外下载位**。文件名沿用 v0.1 是为保持已发布链接不变(2026-10-23 用户指示),内容已换成 r7f:389,140,625 字节,EXE 属性版本 0.2.0.20261022-r7f。旧 r5 包已按用户指示删除,未留存归档;如需回滚用候选包 `dashijie-pet-20261022-r5.zip`(2147251b…) |
| **`dashijie-pet-20261022-r7.zip`** | `2faac4b85c18d384` | =r6 + **REQ-006 DPI 感知(运行进程 system aware,非 0)**+ **REQ-007 组件规范**(矢量导航图标/语义按钮/空态/手机号脱敏/滚动容器)+ **REQ-008 状态可信**(完成判定分本地与云端、结果待确认、队列阶段与等待原因、移除暂停规则承诺文案)+ **REQ-009 交付规范**(EXE 版本资源、ZIP 正斜杠、快速开始.md、release-manifest)+ **REQ-010 脱敏诊断**(账号设置复制诊断信息);1188 项/389,138,967 字节。**当前推荐验证包**  |
| **`dashijie-pet-20261022-r6.zip`** | `8c985f12f20102b0` | =r5 减新手教程 + **REQ-20261022-005 环境检测**:账号设置「开始剪辑的条件」第四项「环境」(与登录/权益/微信并列)+详情弹窗+登录后自动六级实测(配置/磁盘写入/ffmpeg/demo 素材/解码/识别)+7 天复用+剪片让路;移除教程入口与 C2S-16/17 调用;升级自动清理旧教程遗留;「打开作品页」只指向真实成片。**当前推荐验证包** |
| `dashijie-pet-20261022-r5.zip` | `2147251b95be2a75` | =r4+新手教程全功能(入口/沙盒/队列拦截/守卫/C2S-16+C2S-17 微信推送接线)+队列任务化(目录级任务/任务列表/删减)+契约 1.4.1/1.4.2;**注意:其中的新手教程已被 REQ-20261022-005 环境检测替换(源码已改,待出 r6 包)** |
| `dashijie-pet-20261021-r4.zip` | `be053752136ec207` | =r3+REQ-005 成片自动化对接(produce 轮询/works_url/双挂点)+服务端 produce 状态机与 nginx 已配齐(正向全链验收过) |
| `dashijie-pet-20261021-r3.zip` | `759494cd848fbdcb` | =r2+UI 文案客户化(去契约编号/行话,用户反馈)+_render_dir 内存值优先修复 |
| `dashijie-pet-20261021-r2.zip` | `34926222a0a687c5` | UI V1 三导航+绑定对话框+规则 1-4 全端侧+run-eval 默认+日志随包(REQ-004)+二级嵌套扫描;契约 1.3.2 |
| `dashijie-pet-20261021.zip`(r1) | `9e76c7a63927447c` | =r2 减 REQ-004 两件(嵌套扫描/日志随包) |

> r1~r5 均含:014 登录错误修复、002 run-eval 默认、001 规则 1(绑定)。
>
> **对外版本恒定 V0.1(2026-10-26 用户指示)**:界面/EXE 属性一律显示
> V0.1(`version.display()`),不给用户多版本误解;迭代靠内部构建号
> BUILD 区分,只在「复制诊断信息」出现。**发布清单 latest.version 固定
> 填 "0.1"**,先后由 build 比较(version_tuple 已归一三位,0.1=0.1.0)。
> r8(0.3.0 显示)出包后按此指示重打 r8b 并直接替换正式位;更新清单已
> 改签 r8b(version 0.1/build 20261026-r8b)。此后新版=传包+签清单
>(version 恒 0.1),r8b+ 客户端自动跟进;正式位也同步换字节。

> **r8 已出包并上传(2026-10-26)**:`dashijie-pet-20261026-r8.zip`,389,205,826 字节 / SHA256 `184f0fbec8238b045c3e8c5e7835112e34ee263fca24e91762eb514046bc3fe9`。四道闸门过:PYZ 993 模块(新增 rules_sync/auto_update/update_signing/version/ui_kit/dpi/diagnostics/envcheck 显式 hiddenimports——懒加载模块静态分析收不进,spec 已列全);冻结实测 DPI=1、CLI 合法 UTF-8、**规则同步真拉通**(启动 31s 对齐 v2026-10-26-1,写入 data/client_rules.json)、自动更新线程静默判定无新版(清单仍指 r7f)、设置页出现「检查更新」与v0.3.0(20261026-r8)。**验收通过后**:①正式位 `GameBuddy-v0.1.zip` 内容替换为 r8 字节;②用私钥把 client-update.json 改签 r8——此后版本走自动更新通道。

> **r7 系列本机验收(2026-10-22)**:解压到独立目录走客户视角——首启
> (未登录/未选目录/DPI=1)、升级(拷旧 data 保留账号设备与队列)、
> 吊销会话(fixture 注入 → 提示重新登录且重启仍保持)、CLI(rc=1 + UTF-8)、
> 版本资源(`0.2.0.20261022-r7f`)、最小窗口(615×557 逻辑)无裁切。
> 过程中修了 4 处:死会话被显示成「登录✓」、清会话后侧栏残留旧账号、
> 徽标下拍退回「未登录」、错误文案重复错误码;另修测试污染真实
> `data/.device/session.json`(只 patch DEVICE_DIR 不够,四个路径都要 patch)。
> **r7f = 本机验收终版**,SHA256 `3f5b465c…`。

> **对外正式位内容更新(2026-10-23,用户确认 r7f 可用)**:
> `http://8.153.157.239/eval/clients/GameBuddy-v0.1.zip`
> (389,140,625 字节 / SHA256 `3f5b465c02a8dc1341c40f22230a19d5db6790a28ec0ecc1ba52d73ee7f52722`,
> 直链 200 / Range 206)。**文件名保持 v0.1 不变**(用户指示:已发布的
> 链接不能改),包内容替换为 r7f;未重新构建,字节与 r7f 一致。旧 r5 包
> 已删除(`archive/GameBuddy-v0.1.zip` 与临时的 `GameBuddy-v0.2.zip` 均
> 已移除,二者直链 404)。**文件名与 EXE 版本号从此不一致**:文件名 v0.1、
> 版本 0.2.0 —— 客户核对版本请看 EXE 属性或界面左下角,不要看文件名。

> **r6 已出包并上传(2026-10-22)**:
> `http://8.153.157.239/eval/clients/dashijie-pet-20261022-r6.zip`
> (388,867,310 字节 / SHA256 `8c985f12f20102b03b49a7be3c918a1ff07f1f2ec2cbb7000afe208a0a0b2f13`,
> 直链 200、Range 206)。四道闸门全过:产物新鲜、zip 1086 项零泄漏
> (顶层 `DashijiePet/`,config 无租户键)、PYZ 981 模块含 `toolbox.envcheck`
> 且 `toolbox.tutorial` 已消失、冻结态 CLI 报错合法 UTF-8 + GUI 三页截图
> 通过(设置页四条件「登录✓ 权益✓ 微信✓ 环境✓」,环境检测实测
> ok=true/0 warn:解码 124× 实时、识别 0.30s/帧)。
> **对外下载位现状**:`GameBuddy-v0.1.zip`(=r5,`2147251b…`)仍是对外
> 正式位;**r6 为待验证包,用户验收通过后才替换正式位**(先验证后发布)。

## 6. 已知坑速查(细节见 memory)

| 症状 | 根因/处置 |
| --- | --- |
| COLLECT 中途炸、exe 没了 | dist 句柄被占;杀进程重跑 |
| bat 报成功但产物旧 | echo 无条件;只信 mtime |
| 客户机「系统找不到指定的文件」 | ffmpeg 未捆绑(≤o 包);p 包起已修 |
| 重打包后登录 409 循环 | data 被抹=设备重随机;GOTCHA-3 备份(会静默失效,人工核) |
| 分发包带打包机身份 | data 还原时机;zip 泄漏扫描必做 |
| 冻结态中文乱码 | cli.main reconfigure UTF-8(8d4c8d5 起);新增子命令别绕过 main |
| 截图右侧被裁 | exe 落右下角;MoveWindow 进屏内再截 |
| 包内找不到某模块 | 先分清 PYZ(pure)vs _internal(binaries/data);用 ZlibArchiveReader 验 |
