#!/bin/bash
# 02a_clone_public_apps.sh — first of four clone stages (02a/02b/02c/02d),
# replacing the original single 02_clone_apps.sh. See lib_clone_functions.sh
# for why it's split this way (one giant ~6.4GB layer was un-pushable over
# a connection that can't stay open that long — this file existing at all
# is the fix for that, not a cosmetic reorganization).
#
# This stage: the public, frappe/frappe-maintained apps plus frappe/lms
# (also public — no GITHUB_TOKEN needed for any clone in this file).
#
# PINNING: every app is checked out to the exact commit this bench (the real
# frappe-bench, not a branch tip) was actually running at capture time, not
# just cloned by branch name — see lib_pin_apps.sh for why.
#
# Usage: ./02a_clone_public_apps.sh /path/to/bench

set -e

BENCH_PATH=${1:-/home/frappe/frappe-bench}
GITHUB_ORG=${HUB_ORG:-ONE-F-M}

export UV_HTTP_TIMEOUT=300

source /lib_pin_apps.sh
source /lib_clone_functions.sh
cd "$BENCH_PATH"

clone_app telephony https://github.com/frappe/telephony.git develop
clone_app helpdesk https://github.com/frappe/helpdesk main
clone_app hrms https://github.com/frappe/hrms.git version-15
clone_app wiki https://github.com/frappe/wiki master
clone_app payments https://github.com/frappe/payments version-15
clone_app twilio_integration https://github.com/frappe/twilio-integration.git master
clone_app lending https://github.com/frappe/lending version-15
# lms — cloned here (before the ONE-F-M private apps in later stages)
# because one_lms directly imports it (`from lms import plugins`); must be
# installed before one_lms in entrypoint.sh's INSTALL_APPS too.
clone_app lms https://github.com/frappe/lms develop
