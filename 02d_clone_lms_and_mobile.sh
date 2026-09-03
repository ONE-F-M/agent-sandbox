#!/bin/bash
# 02d_clone_lms_and_mobile.sh — fourth of four clone stages (see
# 02a_clone_public_apps.sh for why this is split into four files at all).
#
# This stage: one_lms (the ONE-F-M extension of the public frappe/lms
# cloned in 02a) and mobile_app_ionic (not a Frappe app at all — see
# clone_plain_repo in lib_clone_functions.sh). Both are small, standalone
# clones, so grouped together rather than each getting a whole stage of
# their own.
#
# GITHUB_TOKEN required for both — private repos.
#
# Usage: ./02d_clone_lms_and_mobile.sh /path/to/bench

set -e

BENCH_PATH=${1:-/home/frappe/frappe-bench}
GITHUB_ORG=${HUB_ORG:-ONE-F-M}

export UV_HTTP_TIMEOUT=300

source /lib_pin_apps.sh
source /lib_clone_functions.sh
cd "$BENCH_PATH"

clone_app one_lms https://${GITHUB_TOKEN}@github.com/${GITHUB_ORG}/one_lms.git version-15 https://github.com/${GITHUB_ORG}/one_lms.git

# --- Non-Frappe target apps (plain git clone, never bench-installed) ---
clone_plain_repo mobile_app_ionic https://${GITHUB_TOKEN}@github.com/${GITHUB_ORG}/mobile_app_ionic.git version-15 https://github.com/${GITHUB_ORG}/mobile_app_ionic.git
