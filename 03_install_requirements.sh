#!/bin/bash
# 03_install_requirements.sh — stage 3 of 3 (see 01_init_bench.sh, 02_clone_apps.sh).
#
# Every app is pinned by now (stage 2). Only here do we install dependencies
# — against the pinned code, not the branch tips stage 2's --skip-assets
# deferred us past. One combined pass, not per-app, since apps can share
# build tooling (e.g. hrms's yarn workspaces).
#
# Deliberately no `bench build`. The sandbox never serves a browser UI — its
# only job is `bench run-tests` (pure backend Python) — so building JS/CSS
# bundles is pure unnecessary work, and a real failure point: helpdesk falls
# back to branch tip when its pin is unreachable, and a newer helpdesk paired
# with our deliberately-older pinned frappe broke `bench build` outright
# ("frappe-dependency ... does not satisfy required version", then a missing
# frappe/ui path in its frontend build script). Skipping the build sidesteps
# that whole class of failure for a step nothing here actually needs.
#
# Usage: ./03_install_requirements.sh /path/to/bench

set -e

BENCH_PATH=${1:-/home/frappe/frappe-bench}

# Same override as stages 1/2 — `bench setup requirements` runs `uv pip
# install` for every pinned app too, same large-package timeout risk.
export UV_HTTP_TIMEOUT=300

cd "$BENCH_PATH"

echo "Installing pip/node requirements for all pinned apps..."
bench setup requirements

# Fix OpenSSL/Cryptography compatibility issue known to cause "module 'lib' has no attribute 'GEN_EMAIL'" during bench setup
echo "Upgrading pyopenssl and cryptography..."
./env/bin/pip install --upgrade pyopenssl cryptography

# Unify Redis ports to use the single redis-server on 6379
echo "Unifying Redis configuration..."
bench set-config -g db_host 127.0.0.1
bench set-config -g redis_cache redis://127.0.0.1:6379
bench set-config -g redis_queue redis://127.0.0.1:6379
bench set-config -g redis_socketio redis://127.0.0.1:6379
bench set-config -g developer_mode 1
