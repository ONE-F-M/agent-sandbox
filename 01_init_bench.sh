#!/bin/bash
# 01_init_bench.sh — stage 1 (see the 02a-02d clone stages and 03_install_requirements.sh).
#
# Split into its own Docker layer deliberately: this is the fast, low-risk
# part. When something breaks later in stage 2 or 3, a rebuild reuses this
# layer from cache instead of redoing it — the whole reason this is three
# scripts instead of one setup_frappe_bench.sh. A single-script version of
# this cost two separate ~45-minute full re-clones to recover from a stage-3
# failure, because Docker can only cache at RUN-instruction granularity.
#
# Usage: ./01_init_bench.sh /path/to/bench frappe_version erpnext_version

set -e

BENCH_PATH=${1:-/home/frappe/frappe-bench}
FRAPPE_VERSION=${2:-version-15}
ERPNEXT_VERSION=${3:-version-15}

export UV_HTTP_TIMEOUT=300

source /lib_pin_apps.sh

if [ ! -d "$BENCH_PATH" ] || [ ! -f "$BENCH_PATH/Procfile" ]; then
    if [ -d "$BENCH_PATH" ] && [ ! -f "$BENCH_PATH/Procfile" ]; then
        echo "$BENCH_PATH exists but is not a valid bench. Removing..."
        rm -rf "$BENCH_PATH"
    fi
    bench init "$BENCH_PATH" --frappe-branch "$FRAPPE_VERSION" --python /usr/bin/python3.11 --no-backups --skip-assets
    if [ ! -d "$BENCH_PATH" ]; then
        echo "bench init failed: $BENCH_PATH not created. Exiting."
        exit 1
    fi
    cd "$BENCH_PATH"
    pin_app frappe
else
    cd "$BENCH_PATH"
fi

if ! bench --site all list-apps | grep -q erpnext; then
    bench get-app erpnext --branch "$ERPNEXT_VERSION" --skip-assets
    pin_app erpnext
fi
