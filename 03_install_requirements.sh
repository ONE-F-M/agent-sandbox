#!/bin/bash
# 03_install_requirements.sh — final stage (see 01_init_bench.sh and the
# 02a-02d clone stages).
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

# mobile_app_ionic isn't a Frappe app (no hooks.py, never bench-installed —
# see entrypoint.sh's INSTALL_APPS comment), so `bench setup requirements`
# above never touches it: that command walks Frappe apps, not arbitrary
# directories under apps/. Its own package.json needs its own yarn install
# so a coding-loop run targeting it can run `yarn build`/`yarn test:unit`
# without a from-scratch install eating into the run's time budget.
#
# No --frozen-lockfile: confirmed at bake time that this app's yarn.lock,
# at the pinned commit, is already out of sync with its own package.json
# (it also carries a stray package-lock.json from a prior npm run upstream)
# — --frozen-lockfile fails outright on that mismatch. The git-level pin
# in lib_pin_apps.sh already gives us a reproducible source commit; a plain
# install just resolves node_modules against package.json like any other
# fresh clone would, which is the best available given upstream's lockfile
# drift.
if [ -d "apps/mobile_app_ionic" ]; then
    echo "Installing node dependencies for mobile_app_ionic..."
    (cd apps/mobile_app_ionic && yarn install)
fi

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
