#!/bin/bash
# run_smoke.sh — WI-004006 AC4. Run as the frappe user inside a running sandbox container
# (MariaDB + Redis up, site already created — bake_image.sh calls this after the site exists).
# Serves the baked site with `bench serve`, opens /login in Chromium, records a WebM, stops the server.
set -e

BENCH=/home/frappe/frappe-bench
SITE="${SITE_NAME:-sandbox}"
PORT=8000
OUT=/home/frappe/pw-output

[ -f "$BENCH/sites/$SITE/site_config.json" ] || { echo "[SMOKE] site '$SITE' not found"; exit 1; }

rm -rf "$OUT"; mkdir -p "$OUT"
cd "$BENCH"
bench --site "$SITE" serve --port "$PORT" > "$OUT/bench_serve.log" 2>&1 &
SERVE_PID=$!
trap 'kill $SERVE_PID 2>/dev/null || true' EXIT

for _ in $(seq 1 60); do
    curl -sf "http://127.0.0.1:$PORT/api/method/ping" > /dev/null && break
    kill -0 $SERVE_PID 2>/dev/null || { echo "[SMOKE] bench serve died:"; tail -30 "$OUT/bench_serve.log"; exit 1; }
    sleep 2
done
curl -sf "http://127.0.0.1:$PORT/api/method/ping" > /dev/null || { echo "[SMOKE] bench serve not ready"; exit 1; }

cd /opt/playwright-smoke
SMOKE_BASE_URL="http://127.0.0.1:$PORT" npx playwright test --config playwright.config.js

WEBM=$(find "$OUT/video" -name '*.webm' -size +0 | head -1)
[ -n "$WEBM" ] || { echo "[SMOKE] no non-empty .webm in $OUT/video"; exit 1; }
echo "[SMOKE] OK — video: $WEBM ($(stat -c %s "$WEBM") bytes)"
