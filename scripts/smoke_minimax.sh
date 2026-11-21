#!/usr/bin/env bash
# 一条命令跑通 MiniMax 4 秒真任务：dry-run → 提交 → 挂机 → 校验产物
# 用法：
#   bash scripts/smoke_minimax.sh sk-xxxx        # 直接给 key
#   bash scripts/smoke_minimax.sh                # 读 .secrets/minimax.key
#   bash scripts/smoke_minimax.sh --dry-run      # 只看请求体，不花钱
set -euo pipefail
cd "$(dirname "$0")/.."

DRY=0
KEY="${MINIMAX_API_KEY:-}"
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    sk-*) KEY="$arg" ;;
    *) echo "未知参数：$arg" >&2; exit 2 ;;
  esac
done

if [ -z "$KEY" ] && [ -f .secrets/minimax.key ]; then
  KEY="$(tr -d '\r\n' < .secrets/minimax.key)"
fi
if [ -z "$KEY" ] && [ "$DRY" -eq 0 ]; then
  echo "缺少密钥：把它放进 .secrets/minimax.key，或直接 bash scripts/smoke_minimax.sh sk-xxx" >&2
  exit 2
fi
export MINIMAX_API_KEY="${KEY:-placeholder}"
export MINIMAX_DUMP=1

BIZ="SMOKE-4S"
PROMPT="一只黄色小狗在草地上奔跑，绘本插画风，暖色调"

echo "══ 1/4 请求体预览（不花钱）"
python3 scripts/minimax_video.py submit --business-id "$BIZ" --prompt "$PROMPT" \
  --duration 4 --resolution 480P --dry-run

if [ "$DRY" -eq 1 ]; then
  echo "══ dry-run 结束。去掉 --dry-run 即真提交（约 0.64 元）。"
  exit 0
fi

echo "══ 2/4 提交（约 0.64 元）"
python3 scripts/minimax_video.py submit --business-id "$BIZ" --prompt "$PROMPT" \
  --duration 4 --resolution 480P

echo "══ 3/4 等待生成（最长 20 分钟，失败会弹通知）"
python3 scripts/minimax_video.py watch --max-cny 5 --deadline-hours 1 --interval 10

echo "══ 4/4 产物校验"
OUT="var/video_out/${BIZ}.mp4"
if [ -f "$OUT" ]; then
  ls -lh "$OUT"
  if command -v ffprobe >/dev/null; then
    ffprobe -v error -show_entries format=duration,size -show_entries stream=width,height,r_frame_rate \
      -of default=noprint_wrappers=1 "$OUT"
  fi
  echo "原始响应："; ls -1 var/raw/ 2>/dev/null | tail -4
else
  echo "!! 没有拿到 $OUT —— 把 var/raw/ 里最新的 json 发给开发者校准字段名" >&2
  exit 1
fi
