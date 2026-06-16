#!/usr/bin/env bash
# Local test script for Teams News Agent
# Run: chmod +x test-local.sh && ./test-local.sh

set -e

PROJECT="teams-news-agent"
PORT=8080

echo "======================================"
echo "  Teams News Agent — Local Test"
echo "======================================"
echo ""

# 1. Build
echo ">>> Building Docker image (linux/amd64)..."
docker build --platform linux/amd64 -t "$PROJECT:local" .
echo "[OK] Image built: $PROJECT:local"
echo ""

# 2. Cleanup any previous test container
docker stop "${PROJECT}-test" 2>/dev/null || true
docker rm "${PROJECT}-test"   2>/dev/null || true

# Ensure data dir exists for SQLite volume mount
mkdir -p ./data

# 3. Run
echo ">>> Starting container on port $PORT..."
ENV_FLAG=""
[ -f ".env" ] && ENV_FLAG="--env-file .env"
docker run -d -p $PORT:$PORT \
  $ENV_FLAG \
  -v "$(pwd)/data:/app/data" \
  --name "${PROJECT}-test" \
  "$PROJECT:local"
echo "[OK] Container started: ${PROJECT}-test"
echo ""

# 4. Wait for health
echo ">>> Waiting for /health..."
READY=0
for i in $(seq 1 30); do
  STATUS=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:$PORT/health" 2>/dev/null || echo "000")
  if [ "$STATUS" = "200" ]; then
    echo "[OK] /health returned 200 after ${i}×2s"
    READY=1
    break
  fi
  # Check container still running
  if ! docker ps -q -f "name=${PROJECT}-test" | grep -q .; then
    echo "[FAIL] Container exited unexpectedly. Logs:"
    docker logs "${PROJECT}-test"
    exit 1
  fi
  sleep 2
done

if [ $READY -eq 0 ]; then
  echo "[FAIL] /health not ready after 60s. Logs:"
  docker logs "${PROJECT}-test"
  exit 1
fi
echo ""

# 5. Contract tests
echo ">>> Running contract tests..."
echo ""

# Health
HEALTH=$(curl -s -w "\n%{http_code}" "http://localhost:$PORT/health")
CODE=$(echo "$HEALTH" | tail -1)
[ "$CODE" = "200" ] && echo "[PASS] GET /health -> 200" || echo "[FAIL] GET /health -> $CODE"

# Invocations - basic message
echo ">>> POST /invocations (basic message)..."
RESP=$(curl -s -w "\n%{http_code}" -X POST "http://localhost:$PORT/invocations" \
  -H "Content-Type: application/json" \
  -d '{"message": "Xin chào! Cho tôi xem tin tức công nghệ mới nhất."}')
CODE=$(echo "$RESP" | tail -1)
BODY=$(echo "$RESP" | head -n -1)
if [ "$CODE" = "200" ]; then
  echo "[PASS] POST /invocations -> 200"
  echo "       Response: $(echo "$BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); print(str(d.get('response',''))[:200])" 2>/dev/null || echo "$BODY" | head -c 200)"
else
  echo "[FAIL] POST /invocations -> $CODE"
  echo "       Body: $BODY"
fi
echo ""

# Empty body test
RESP2=$(curl -s -w "\n%{http_code}" -X POST "http://localhost:$PORT/invocations" \
  -H "Content-Type: application/json" \
  -d '{}')
CODE2=$(echo "$RESP2" | tail -1)
[ "$CODE2" != "500" ] && echo "[PASS] POST /invocations {} -> $CODE2 (no 500 crash)" \
  || echo "[WARN] POST /invocations {} -> 500 (server error)"
echo ""

# Teams webhook test (no auth since TEAMS_WEBHOOK_SECRET is set to 'optional_shared_secret')
echo ">>> POST /webhook/teams (with secret header)..."
WEBHOOK_RESP=$(curl -s -w "\n%{http_code}" -X POST "http://localhost:$PORT/webhook/teams" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer optional_shared_secret" \
  -d '{
    "type": "message",
    "text": "Đặt lịch đọc báo lúc 8:25 sáng nhé",
    "id": "test-activity-001",
    "serviceUrl": "https://smba.trafficmanager.net/apac/",
    "conversation": {"id": "test-conv-001"},
    "from": {"id": "test-user-001", "name": "Test User"}
  }')
WCODE=$(echo "$WEBHOOK_RESP" | tail -1)
WBODY=$(echo "$WEBHOOK_RESP" | head -n -1)
if [ "$WCODE" = "200" ]; then
  echo "[PASS] POST /webhook/teams -> 200"
  echo "       Response: $(echo "$WBODY" | python3 -c "import sys,json; d=json.load(sys.stdin); print(str(d.get('text',''))[:300])" 2>/dev/null || echo "$WBODY" | head -c 300)"
else
  echo "[WARN] POST /webhook/teams -> $WCODE (may need Teams Bot credentials for full test)"
  echo "       Body: $WBODY" | head -c 200
fi
echo ""

# 6. Print logs summary
echo ">>> Container logs (last 20 lines):"
docker logs --tail 20 "${PROJECT}-test"
echo ""

echo "======================================"
echo "  Test complete!"
echo ""
echo "  Container still running on :$PORT"
echo "  To stop: docker stop ${PROJECT}-test && docker rm ${PROJECT}-test"
echo "  Live logs: docker logs -f ${PROJECT}-test"
echo "======================================"
