#!/bin/bash
# 02_clone_apps.sh — stage 2 of 3 (see 01_init_bench.sh, 03_install_requirements.sh).
#
# The slow, network-bound stage — this is the ~40-minute part. Split into its
# own layer so a failure in stage 3 (bench setup requirements) doesn't force
# re-cloning all 15 apps to retry; only a change to this script, lib_pin_apps.sh,
# or an earlier layer invalidates it.
#
# PINNING: every app is checked out to the exact commit this bench (the real
# frappe-bench, not a branch tip) was actually running at capture time, not
# just cloned by branch name. Tracking a branch means the sandbox drifts from
# what's actually deployed as upstream commits land — confirmed the hard way:
# a fresh `frappe`@version-15 tip no longer exported a name `one_fm`'s pinned
# commit imports, breaking `bench build --app one_fm` with an ImportError
# that has nothing to do with any real work item. Pinning eliminates that
# whole class of false failures.
#
# `--skip-assets` on every clone defers the pip/yarn build to stage 3 —
# otherwise `bench get-app` builds against the branch tip's code before we
# get a chance to check out the pinned commit.
#
# Usage: ./02_clone_apps.sh /path/to/bench

set -e

BENCH_PATH=${1:-/home/frappe/frappe-bench}
GITHUB_ORG=${HUB_ORG:-ONE-F-M}

export UV_HTTP_TIMEOUT=300

source /lib_pin_apps.sh
cd "$BENCH_PATH"

git config --global http.postBuffer 524288000
git config --global http.lowSpeedLimit 0
git config --global http.lowSpeedTime 999999
git config --global core.compression 0

# Call ONLY after pin_app() has already run — pin_app() needs an
# authenticated `git fetch --unshallow` against the private repo (bench's
# clone is shallow, --depth 1) to reach the pinned commit at all, so the
# token-embedded URL must still be in place when it runs. Stripping the
# credential first (as an earlier version of this script did) breaks that
# fetch with "could not read Username for 'https://github.com'" — confirmed
# the hard way on a real bake, failing at onefm_sso's pin step.
#
# Once pinning is done, force the remote to a known-clean state:
#   1. Rename it to "origin" — `bench get-app` names it "upstream", but
#      dev_agent_server.py's _checkout_target_branch() runs `git fetch
#      origin <branch>` for ANY target_app (target_app_choices() offers
#      every installed app, not just the ONE-F-M ones), so every app needs
#      a remote literally named "origin" or that fetch fails outright —
#      this is what broke run DAS-91630 against one_bpmn.
#   2. Overwrite the remote URL with $clean_url (never containing
#      $GITHUB_TOKEN) — `git clone`/`bench get-app` writes whatever URL it
#      was given verbatim into .git/config, so cloning with the token
#      embedded (required for auth against a private repo) leaves the
#      token sitting in plaintext inside this Docker layer forever unless
#      it's overwritten before the RUN instruction finishes. A layer only
#      ever stores the final on-disk state, so doing this inside the same
#      clone_app() call that performed the clone (after pinning) means the
#      token never reaches the committed image.
_normalize_remote() {
    local app_name=$1
    local clean_url=$2
    local app_dir="apps/$app_name"
    local existing
    existing=$(git -C "$app_dir" remote)
    for name in $existing; do
        if [ "$name" != "origin" ]; then
            git -C "$app_dir" remote rename "$name" origin
        fi
    done
    git -C "$app_dir" remote set-url origin "$clean_url"
}

clone_app() {
    local app_name=$1
    local repo_url=$2
    local branch=$3
    # Optional 4th arg: the credential-free URL to leave in .git/config.
    # Defaults to $repo_url for the public frappe-maintained apps, where
    # it's already credential-free; the private ONE-F-M apps pass this
    # explicitly since their $repo_url has $GITHUB_TOKEN embedded for auth.
    local clean_url=${4:-$repo_url}
    local max_retries=3
    local attempt=1

    if [ ! -d "apps/$app_name" ]; then
        echo "Cloning $app_name from $repo_url (branch: $branch)..."
        until [ $attempt -gt $max_retries ]
        do
            echo "Attempt $attempt of $max_retries..."
            rm -rf "apps/$app_name"
            if bench get-app "$app_name" "$repo_url" --branch "$branch" --skip-assets || bench get-app "$repo_url" --branch "$branch" --skip-assets; then
                echo "Successfully cloned $app_name."
                pin_app "$app_name"
                _normalize_remote "$app_name" "$clean_url"
                return 0
            fi
            echo "Failed to clone $app_name. Retrying in 5 seconds..."
            attempt=$((attempt + 1))
            sleep 5
        done
        echo "ERROR: Failed to clone $app_name after $max_retries attempts."
        exit 1
    fi
}

# --- frappe/frappe-maintained apps (public) ---
clone_app telephony https://github.com/frappe/telephony.git develop
clone_app helpdesk https://github.com/frappe/helpdesk main
clone_app hrms https://github.com/frappe/hrms.git version-15
clone_app wiki https://github.com/frappe/wiki master
clone_app payments https://github.com/frappe/payments version-15
clone_app twilio_integration https://github.com/frappe/twilio-integration.git master
clone_app lending https://github.com/frappe/lending version-15

# --- ONE-F-M org apps (private — GITHUB_TOKEN required to clone; the
# remote is rewritten to a credential-free URL immediately after, so the
# token itself never lands in the image — see _normalize_remote above) ---
clone_app one_fm_password_management https://${GITHUB_TOKEN}@github.com/${GITHUB_ORG}/password_management.git version-15 https://github.com/${GITHUB_ORG}/password_management.git
clone_app one_fm https://${GITHUB_TOKEN}@github.com/${GITHUB_ORG}/one_fm.git staging https://github.com/${GITHUB_ORG}/one_fm.git
clone_app onefm_sso https://${GITHUB_TOKEN}@github.com/${GITHUB_ORG}/onefm_sso.git version-15 https://github.com/${GITHUB_ORG}/onefm_sso.git
clone_app one_bpmn https://${GITHUB_TOKEN}@github.com/${GITHUB_ORG}/one_bpmn.git staging https://github.com/${GITHUB_ORG}/one_bpmn.git
clone_app onefm_mcp https://${GITHUB_TOKEN}@github.com/${GITHUB_ORG}/onefm_mcp.git staging https://github.com/${GITHUB_ORG}/onefm_mcp.git
clone_app frappe_agile https://${GITHUB_TOKEN}@github.com/${GITHUB_ORG}/frappe_agile.git staging https://github.com/${GITHUB_ORG}/frappe_agile.git
