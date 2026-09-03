#!/bin/bash
# 02c_clone_onefm_apps_2.sh — third of four clone stages (see
# 02a_clone_public_apps.sh for why this is split into four files at all).
#
# This stage: the second half of the private ONE-F-M org apps (see
# 02b_clone_onefm_apps_1.sh for why one_fm and one_bpmn specifically sit in
# different stages).
#
# GITHUB_TOKEN required — these are private repos. The remote is rewritten
# to a credential-free URL immediately after each clone (_normalize_remote,
# in lib_clone_functions.sh), so the token itself never lands in the image.
#
# Usage: ./02c_clone_onefm_apps_2.sh /path/to/bench

set -e

BENCH_PATH=${1:-/home/frappe/frappe-bench}
GITHUB_ORG=${HUB_ORG:-ONE-F-M}

export UV_HTTP_TIMEOUT=300

source /lib_pin_apps.sh
source /lib_clone_functions.sh
cd "$BENCH_PATH"

clone_app one_bpmn https://${GITHUB_TOKEN}@github.com/${GITHUB_ORG}/one_bpmn.git staging https://github.com/${GITHUB_ORG}/one_bpmn.git
clone_app onefm_mcp https://${GITHUB_TOKEN}@github.com/${GITHUB_ORG}/onefm_mcp.git staging https://github.com/${GITHUB_ORG}/onefm_mcp.git
clone_app frappe_agile https://${GITHUB_TOKEN}@github.com/${GITHUB_ORG}/frappe_agile.git staging https://github.com/${GITHUB_ORG}/frappe_agile.git
