#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")/.."

echo "=== trawl 배포 (Docker) ==="

echo "빌드 중..."
if ! docker compose build; then
    echo "빌드 실패 — 기존 컨테이너 유지"
    exit 1
fi

echo "재시작 중..."
docker compose down
docker compose up -d

echo "시작 확인 중..."
sleep 5
docker compose logs --tail=20

echo "=== 배포 완료 (http://127.0.0.1:8765/mcp) ==="
