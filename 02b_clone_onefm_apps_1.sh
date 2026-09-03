#!/bin/bash
# 02b_clone_onefm_apps_1.sh — second of four clone stages (see
# 02a_clone_public_apps.sh for why this is split into four files at all).
#
# This stage: the first half of the private ONE-F-M org apps. Split from
# 02c's half specifically to keep one_fm (a large, mature app) in its own
# stage rather than sharing a layer with one_bpmn (also large) — a rough
# size balance across 02b/02c, not a functional grouping.
#
# GITHUB_TOKEN required — these are private repos. The remote is rewritten
# to a credential-free URL immediately after each clone (_normalize_remote,
# in lib_clone_functions.sh), so the token itself never lands in the image.
#
# Usage: ./02b_clone_onefm_apps_1.sh /path/to/bench

set -e

BENCH_PATH=${1:-/home/frappe/frappe-bench}
GITHUB_ORG=${HUB_ORG:-ONE-F-M}

export UV_HTTP_TIMEOUT=300

source /lib_pin_apps.sh
source /lib_clone_functions.sh
cd "$BENCH_PATH"

clone_app one_fm_password_management https://${GITHUB_TOKEN}@github.com/${GITHUB_ORG}/password_management.git version-15 https://github.com/${GITHUB_ORG}/password_management.git
clone_app one_fm https://${GITHUB_TOKEN}@github.com/${GITHUB_ORG}/one_fm.git staging https://github.com/${GITHUB_ORG}/one_fm.git
clone_app onefm_sso https://${GITHUB_TOKEN}@github.com/${GITHUB_ORG}/onefm_sso.git version-15 https://github.com/${GITHUB_ORG}/onefm_sso.git
