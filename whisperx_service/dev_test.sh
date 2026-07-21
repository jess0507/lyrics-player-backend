#!/usr/bin/env bash
# 本機測試 whisperx_service 的一鍵腳本:確保容器起來(必要時 build/重啟),
# 再把剩餘參數轉丟給 api_smoke.py 打 HTTP API。
#
# 用法:
#   ./dev_test.sh --endpoint health
#   ./dev_test.sh --audio song.mp3 --lyrics lyrics.txt -l zh-TW --endpoint align
#   ./dev_test.sh --audio song.mp3 --endpoint transcribe
#
# 額外旗標(需放在最前面,會被本腳本吃掉、不會轉給 api_smoke.py):
#   --rebuild   先重新 docker build image(改了 Dockerfile / requirements 才需要)
#   --logs      不測試,改成 tail 容器 log(Ctrl-C 離開)
#   --stop      不測試,把容器停掉並移除
#
# 改了 main.py / aligner.py 等原始碼不需 --rebuild:容器把本目錄掛在 /src,
# 重啟容器即讀到新碼(本腳本每次都會重啟)。

set -euo pipefail
cd "$(dirname "$0")"

IMAGE=whisperx-service
CONTAINER=whisperx-local
PORT=8080

if [[ "${1:-}" == "--stop" ]]; then
  docker rm -f "$CONTAINER" >/dev/null 2>&1 && echo "已移除容器 $CONTAINER" || echo "沒有在跑的 $CONTAINER"
  exit 0
fi

if [[ "${1:-}" == "--logs" ]]; then
  exec docker logs -f "$CONTAINER"
fi

REBUILD=0
if [[ "${1:-}" == "--rebuild" ]]; then
  REBUILD=1
  shift
fi

# 1. 確認 docker daemon 有起來(OrbStack)
if ! docker info >/dev/null 2>&1; then
  echo "Docker daemon 沒起來,嘗試開啟 OrbStack…"
  open -a OrbStack
  echo -n "等待 docker daemon"
  for _ in $(seq 1 30); do
    docker info >/dev/null 2>&1 && break
    echo -n "."
    sleep 1
  done
  echo
  if ! docker info >/dev/null 2>&1; then
    echo "Docker daemon 仍未就緒,請手動確認 OrbStack 已啟動後重試。" >&2
    exit 1
  fi
fi

# 2. build image(第一次或指定 --rebuild 時)
if [[ "$REBUILD" == "1" ]] || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "build image $IMAGE …(第一次較久,約十幾分鐘)"
  docker build -t "$IMAGE" .
fi

# 3. 重啟容器(先移除舊的,確保吃到最新原始碼與 image)
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$CONTAINER" -p "$PORT:$PORT" \
  -v "$PWD":/src -w /src \
  -e PYTHONDONTWRITEBYTECODE=1 \
  "$IMAGE" \
  python main.py >/dev/null

# 4. 等 /healthz 回應
echo -n "等待服務就緒"
for _ in $(seq 1 60); do
  if curl -sf "http://localhost:$PORT/healthz" >/dev/null 2>&1; then
    echo " OK"
    break
  fi
  echo -n "."
  sleep 1
done

if ! curl -sf "http://localhost:$PORT/healthz" >/dev/null 2>&1; then
  echo
  echo "服務逾時未就緒,看 log 排查:" >&2
  docker logs --tail 50 "$CONTAINER" >&2
  exit 1
fi

# 5. 沒帶任何參數 → 只做健康檢查後結束;否則把參數轉給 api_smoke.py
if [[ $# -eq 0 ]]; then
  echo "服務已在 http://localhost:$PORT 就緒(只做了健康檢查)。"
  echo "可加參數測試,例如:./dev_test.sh --audio song.mp3 --lyrics lyrics.txt -l zh-TW --endpoint align"
  exit 0
fi

echo "送出請求中…CPU 對齊/轉寫較久(視音檔長度可能需數分鐘),期間不會有輸出,請耐心等待。"
echo "另開一個終端機可用 './dev_test.sh --logs' 看即時進度。"
python3 api_smoke.py --url "http://localhost:$PORT" "$@"
