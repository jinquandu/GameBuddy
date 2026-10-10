#!/bin/bash
# 全自动链：录制指定房间本场 -> 下播后跑识别链 -> 推送服务器
# 用法: bash _autochain_room.sh <房间号>
ROOM="${1:?用法: bash _autochain_room.sh <房间号>}"
cd "$(dirname "$0")" || exit 1
export PYTHONUTF8=1
# 本机 ffmpeg 若不在 PATH，加自定义位置（存在才加）
command -v ffmpeg >/dev/null 2>&1 || export PATH="/c/Users/quinc/ffmpeg-dl/bin:$PATH"

echo "[$(date '+%F %T')] 开始录制 $ROOM ..."
before=$(ls video/ 2>/dev/null | sort)
py -m toolbox record "$ROOM" --once 2>&1
rc=$?
echo "[$(date '+%F %T')] 录制结束 (exit=$rc)"

newdir=$(comm -13 <(echo "$before") <(ls video/ 2>/dev/null | sort) | head -1)
if [ -z "$newdir" ]; then
  echo "[错误] 未找到新场次目录，识别链不启动"
  exit 1
fi
echo "[$(date '+%F %T')] 场次：$newdir，启动识别链 ..."
# --run-eval: push 后触发服务端 toolbox eval 生成 scores.json（打标站击倒/拾取片段数来源）
py -m toolbox pipeline "video/$newdir" --skip download --run-eval 2>&1
prc=$?
if [ $prc -ne 0 ]; then
  echo "[错误] 识别链异常退出 (exit=$prc)，push 可能未执行——需人工补跑"
  exit 1
fi
echo "[$(date '+%F %T')] 全链完成（含 push）"
