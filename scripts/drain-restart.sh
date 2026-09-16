#!/bin/bash
# 排空后重启：先让服务停止派发新单，等在跑的 AI 批次自然收尾，再重启服务。
#
# 为什么需要：2026-09-16 两次为上线重启，每次都把正在跑的 AI 批次打断，服务重启后把它们
# 重新排队再跑一遍，白花了约半小时的 AI 时间（在跑的批次本身是要跑完的，浪费的是已经花掉的
# 那一部分）。直接等队列清空也不行 —— 轮询一直在补新单，常常等不到。
#
# 机制：数据目录放一个 drain 标志文件，服务侧（poller.poll_once / Worker._claim_agent_budget）
# 见到就不再派新单，在跑的批次不受影响；重启后自动清掉标志。硬超时 15 分钟，卡住的批次不能
# 把部署无限期挡住（超时就照旧重启，被重新排队）。
set -u

PROJECT_DIR="${PROJECT_DIR:-/Users/ab/company/projects/zentao-auto-fixer-server}"
# 脚本被强杀也要摘掉标志，否则服务会一直不派新单（服务侧还有 1 小时的超龄兜底）
trap 'rm -f "${PROJECT_DIR:-/Users/ab/company/projects/zentao-auto-fixer-server}/.auto-fixer/drain"' EXIT
DATA_DIR="$PROJECT_DIR/.auto-fixer"
LABEL="${LABEL:-com.dashu.zentao-auto-fixer}"
DRAIN_TIMEOUT="${DRAIN_TIMEOUT:-900}"
FLAG="$DATA_DIR/drain"

running_batches() {
  python3 - "$DATA_DIR/state.sqlite3" <<'PY'
import sqlite3, sys
conn = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True)
print(conn.execute(
    "SELECT COUNT(*) FROM bug_runs WHERE status IN ('running','writeback_queued')"
).fetchone()[0])
PY
}

touch "$FLAG"
echo "$(date '+%F %T') 排空开始：不再派发新单，等在跑批次收尾（上限 ${DRAIN_TIMEOUT}s）"
deadline=$(( $(date +%s) + DRAIN_TIMEOUT ))
while :; do
  busy=$(running_batches)
  if [ "$busy" = "0" ]; then
    echo "$(date '+%F %T') 在跑批次已清空，重启不会打断任何任务"
    break
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "$(date '+%F %T') 排空超时：仍有 $busy 个批次在跑，按约定直接重启（这些批次会被重新排队重跑）"
    break
  fi
  sleep 10
done

launchctl kickstart -k "gui/$(id -u)/$LABEL"
for _ in $(seq 1 30); do
  if launchctl list | grep -q "$LABEL\$"; then break; fi
  sleep 2
done
rm -f "$FLAG"
echo "$(date '+%F %T') 重启完成，排空标志已清除，派单恢复"
