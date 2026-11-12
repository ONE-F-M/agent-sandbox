#!/bin/bash
# verify_browser_env.sh — WI-004006 AC1/AC3. Run as the frappe user. Needs no site.
# Called by the Dockerfile (fails the build) and can be re-run:
#   docker exec -u frappe <ctr> /opt/playwright-smoke/verify_browser_env.sh
set -e

# GitHub CLI >= 2.103 (checked, not assumed from the apt repo)
GH_MIN=2.103.0
GH_VER=$(gh --version | awk 'NR==1{print $3}')
[ "$(printf '%s\n%s\n' "$GH_MIN" "$GH_VER" | sort -V | head -1)" = "$GH_MIN" ] \
    || { echo "[VERIFY] gh $GH_VER < $GH_MIN"; exit 1; }
echo "[VERIFY] gh $GH_VER"

# Exact pinned Playwright, via the very command the story names, from an unrelated directory
cd /home/frappe
PW=$(npx playwright --version)
[ "$PW" = "Version $PLAYWRIGHT_VERSION" ] || { echo "[VERIFY] got '$PW', want Version $PLAYWRIGHT_VERSION"; exit 1; }
echo "[VERIFY] $PW"

# Noto Arabic families, via fontconfig (not just "some Arabic fallback")
for fam in "Noto Naskh Arabic" "Noto Kufi Arabic" "Noto Sans Arabic"; do
    fc-list : family | grep -q "$fam" || { echo "[VERIFY] font family missing: $fam"; exit 1; }
    echo "[VERIFY] font: $fam"
done

# Chromium installed for THIS Playwright version, readable/executable by frappe, and actually launches
node -e "
const { chromium } = require('@playwright/test');
(async () => {
  const b = await chromium.launch({ args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  console.log('[VERIFY] chromium ' + b.version());
  await b.close();
})().catch(e => { console.error(e); process.exit(1); });
"
