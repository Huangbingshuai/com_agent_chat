#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_DIR="$PROJECT_DIR/python"
COMPOSE_FILE="$PROJECT_DIR/docker-compose.yml"
PROJECT_NAME="com-agent-chat"
PORT="8081"

cd "$PYTHON_DIR"

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "端口 $PORT 已被占用，请先确认并停止占用进程。" >&2
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >&2 || true
  exit 1
fi

docker compose --project-name "$PROJECT_NAME" -f "$COMPOSE_FILE" up -d neo4j chromadb zookeeper kafka
echo "等待项目数据库和消息队列就绪..."
ready=0
for _ in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:8000/api/v2/heartbeat >/dev/null \
    && curl -fsS http://127.0.0.1:7474 >/dev/null \
    && nc -z 127.0.0.1 9092 >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done
if [[ "$ready" -ne 1 ]]; then
  echo "依赖容器未能在 120 秒内全部就绪，请查看 docker compose 日志。" >&2
  docker compose --project-name "$PROJECT_NAME" -f "$COMPOSE_FILE" ps >&2
  exit 1
fi
echo "基础容器已启动。启动本地 API + 前端: http://127.0.0.1:$PORT"
exec conda run --no-capture-output -n com-agent-chat python -m uvicorn api.main:app --host 127.0.0.1 --port "$PORT"
