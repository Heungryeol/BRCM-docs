#!/usr/bin/env bash
# 수집을 백그라운드로 실행 (SSH 가 끊겨도 계속). 로그: data/logs/crawl-<시각>.log
#   scripts/run-background.sh                 전체 (github → urls → kb → techdocs)
#   scripts/run-background.sh -s kb --hours 8
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$DIR/data/logs"
if [[ -f "$DIR/data/crawl.pid" ]] && kill -0 "$(cat "$DIR/data/crawl.pid")" 2>/dev/null; then
  echo "이미 실행 중 (PID $(cat "$DIR/data/crawl.pid")) — 로그: $(ls -t "$DIR"/data/logs/crawl-*.log | head -1)"; exit 1
fi
LOG="$DIR/data/logs/crawl-$(date +%Y%m%d-%H%M%S).log"
nohup "$DIR/brcm-docs" crawl "$@" > "$LOG" 2>&1 &
echo $! > "$DIR/data/crawl.pid"
ln -sf "$LOG" "$DIR/data/logs/latest.log"
echo "시작 (PID $!) — 진행 보기: tail -f $DIR/data/logs/latest.log"
echo "멈추기: kill \$(cat $DIR/data/crawl.pid)   (현재 문서까지 저장 후 종료, 다시 실행하면 이어서)"
