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
4. **冻结烟测**:exe 有头启动 → PowerShell MoveWindow 拖进屏内
   (默认落右下角会被屏幕裁掉,截图前必移)→ 截图验 UI(导航三页/
   徽标「未选择文件夹」「未登录」=净化 config 生效)→ Stop-Process
   清理。更硬的行为级:`exe pipeline <不存在路径>`(env
   PYTHONIOENCODING=gbk)→ stderr 合法 UTF-8+预期错误文本+rc=1。

## 4. 上传分发位(免登录直链,REQ-20261015-001)

```
scp dist\DashijiePet-core.zip ecs:/opt/dashijie-eval-data/clients/<名>-uploading.zip
ssh ecs "cd .../clients && sha256sum <名>-uploading.zip && \
         mv <名>-uploading.zip <名>.zip"
```

- **临时名+mv 原子改**(半包不可见);双侧 sha256 必须一致;直链
  `http://8.153.157.239/eval/clients/<名>.zip` curl -I 验 200。
- 命名:`dashijie-pet-<日期>[-rN].zip`(rN=同日重打);正式对外用
  `DashijiePet-core-<日期>.zip` 惯例名。**先验证后发布**:用户验收
  通过才替换官网正式位,旧正式位不动。

## 5. 当前有效版本

| 包 | SHA256(前16) | 内容 |
| --- | --- | --- |
| **`GameBuddy-v0.1.zip`(对外正式名,=r5)** | `2147251b95be2a75` | 同 r5;对外分发用此名 |
| `dashijie-pet-20261022-r5.zip` | `2147251b95be2a75` | =r4+新手教程全功能(入口/沙盒/队列拦截/守卫/C2S-16+C2S-17 微信推送接线)+队列任务化(目录级任务/任务列表/删减)+契约 1.4.1/1.4.2;**注意:其中的新手教程已被 REQ-20261022-005 环境检测替换(源码已改,待出 r6 包)** |
| `dashijie-pet-20261021-r4.zip` | `be053752136ec207` | =r3+REQ-005 成片自动化对接(produce 轮询/works_url/双挂点)+服务端 produce 状态机与 nginx 已配齐(正向全链验收过) |
| `dashijie-pet-20261021-r3.zip` | `759494cd848fbdcb` | =r2+UI 文案客户化(去契约编号/行话,用户反馈)+_render_dir 内存值优先修复 |
| `dashijie-pet-20261021-r2.zip` | `34926222a0a687c5` | UI V1 三导航+绑定对话框+规则 1-4 全端侧+run-eval 默认+日志随包(REQ-004)+二级嵌套扫描;契约 1.3.2 |
| `dashijie-pet-20261021.zip`(r1) | `9e76c7a63927447c` | =r2 减 REQ-004 两件(嵌套扫描/日志随包) |

> r1~r5 均含:014 登录错误修复、002 run-eval 默认、001 规则 1(绑定)。
>
> **待出 r6(源码已就绪,2026-10-22)**:REQ-20261022-005 环境检测替换新手
> 教程——工作台「新手教程」入口移除,账号设置「开始剪辑的条件」新增第四项
> 「环境」(与登录/权益/微信并列),登录后自动六级实测(配置/磁盘/视频工具/
> demo 素材/解码/识别)+详情弹窗;升级自动清理旧教程遗留。「打开作品页」
> 只指向真实成片。打包时无需新依赖:cv2/rapidocr/ffmpeg 均已在包内
> (envcheck 复用 detector 的 OCR 与 SCAN_BAND)。
> **r5 已以正式名 `GameBuddy-v0.1.zip` 双发(同 SHA256 `2147251b…`),
> 即当前对外下载位**;旧官网位 `DashijiePet-core-20261020.zip`(p 包)
> 并存,待 r5 验收期满后下架。

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
